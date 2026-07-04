#!/usr/bin/env python
"""
Mnemos end-to-end health harness.

Re-runnable proof that the living-memory system is structurally and behaviorally
intact. Establishes a GREEN baseline that can be re-run after a code change to
detect regressions.

USAGE
    .venv/bin/python scripts/health_check.py            # full run
    .venv/bin/python scripts/health_check.py --no-pytest # skip slow Phase 7

Agent DBs are discovered automatically from ~/.mnemos/ (any *.db with an
engrams table). Per-deployment baselines for pre-existing data-hygiene WARNs
live in an untracked scripts/health_baseline.local.json (see _load_baseline).

SAFETY (enforced by construction)
  * NEVER constructs an EngramStore / runs consolidation / encodes against a
    live ~/.mnemos/*.db. Every mutating phase operates on a fresh COPY placed
    in /tmp/mnemos_hh/.
  * Read-only live checks use sqlite3 `file:...?mode=ro` URIs only.
  * Does not touch ~/.mnemos/config.json, plists, or git.

Each check prints a `PASS`/`FAIL`/`SKIP` line with one-line evidence. The
process exits 0 only if every non-skipped check passed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIVE_DIR = Path.home() / ".mnemos"
WORK_DIR = Path(tempfile.gettempdir()) / "mnemos_hh"

CANONICAL_ENGRAM_COLUMNS = {
    "id",
    "content",
    "content_at_encoding",
    "impact",
    "resolution",
    "kind",
    "tags",
    "schema_refs",
    "strength",
    "stability",
    "accessibility",
    "encoding_context",
    "source",
    "lineage",
    "owner_agent_id",
    "visibility",
    "state",
    "created_at",
    "last_accessed",
    "access_count",
    "reconsolidation_count",
}

CORE_TABLES = {
    "engrams",
    "engrams_fts",
    "connections",
    "versions",
    "beliefs",
    "hypomnema_entries",
    "memory_sessions",
    "functional_memories",
    "emotional_state_history",
    "agent_identity",
    "archive",
    "consolidation_log",
    "meta",
}

UNIT_RANGE_COLUMNS = ("strength", "stability", "accessibility", "resolution")

# The live schema version, read from the code so the harness tracks the source.
try:
    from mnemos.store.sqlite_store import SCHEMA_VERSION as CURRENT_SCHEMA_VERSION
except Exception:
    CURRENT_SCHEMA_VERSION = 3


# Per-deployment data-hygiene baselines. A deployment may carry tiny benign
# anomalies (a few legacy engrams with no FTS row, a dangling embedding). Record
# accepted counts in an untracked scripts/health_baseline.local.json so they
# surface as WARN while any INCREASE escalates to FAIL. Shape:
#   {"fts_gap": {"<db>.db": N}, "emb_orphans": {"<db>.db": N}}
#   * fts_gap: active engrams missing an FTS row.
#   * emb_orphans: embedding rows whose engram_id no longer exists.
def _load_baseline() -> dict:
    f = REPO / "scripts" / "health_baseline.local.json"
    if not f.exists():
        return {}
    try:
        import json

        return json.loads(f.read_text())
    except Exception:
        return {}


_BASELINE = _load_baseline()
BASELINE_FTS_GAP = _BASELINE.get("fts_gap", {})
BASELINE_EMB_ORPHANS = _BASELINE.get("emb_orphans", {})


# ─────────────────────────── result plumbing ───────────────────────────


class Harness:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []  # (id, label, status, evidence)
        self._anomalies: list[str] = []

    def record(self, ident: str, label: str, status: str, evidence: str) -> None:
        self.rows.append((ident, label, status, evidence))
        marker = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "WARN": "WARN"}[
            status
        ]
        print(f"[{marker}] {ident:<5} {label}: {evidence}")

    def check(self, ident: str, label: str, fn) -> bool:
        """Run fn() -> (ok: bool, evidence: str). Harness exceptions become FAIL
        with a clear marker so a harness bug is visible, not silently green."""
        try:
            ok, evidence = fn()
        except Exception as exc:  # noqa: BLE001
            self.record(
                ident, label, "FAIL", f"HARNESS-ERROR {type(exc).__name__}: {exc}"
            )
            return False
        self.record(ident, label, "PASS" if ok else "FAIL", evidence)
        return ok

    def skip(self, ident: str, label: str, reason: str) -> None:
        self.record(ident, label, "SKIP", reason)

    def anomaly(self, msg: str) -> None:
        self._anomalies.append(msg)

    @property
    def anomalies(self) -> list[str]:
        return self._anomalies

    def summary(self) -> int:
        passed = sum(1 for r in self.rows if r[2] == "PASS")
        failed = sum(1 for r in self.rows if r[2] == "FAIL")
        skipped = sum(1 for r in self.rows if r[2] == "SKIP")
        warned = sum(1 for r in self.rows if r[2] == "WARN")
        print("\n" + "=" * 72)
        print(
            f"SUMMARY  {passed} passed  {failed} failed  {warned} warned  "
            f"{skipped} skipped  ({len(self.rows)} checks)"
        )
        if self._anomalies:
            print("\nANOMALIES / NOTES:")
            for a in self._anomalies:
                print(f"  - {a}")
        if failed:
            verdict = "RED"
        elif warned:
            verdict = "GREEN (with pre-existing WARN data-hygiene notes — see above)"
        else:
            verdict = "GREEN"
        print(f"\nVERDICT: {verdict}")
        print("=" * 72)
        return 0 if failed == 0 else 1


def ro_connect(path: Path) -> sqlite3.Connection:
    """Open a live DB strictly read-only (cannot write, cannot create)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _engram_schema_version(path: Path) -> int | None:
    """schema_version of a DB with an 'engrams' table, or None if not a mnemos store."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if not c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='engrams'"
            ).fetchone():
                return None
            row = c.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            c.close()
    except Exception:
        return None


def discover_agent_dbs() -> tuple[list[str], list[tuple[str, "int | None"]]]:
    """Partition ~/.mnemos/*.db into (current, legacy).

    current = mnemos stores at the live SCHEMA_VERSION — they get full invariant
              checks. legacy = mnemos-ish DBs at an older schema (e.g. old test
              DBs); these are SKIPPED, not failed. Non-mnemos/empty files ignored.
    """
    current: list[str] = []
    legacy: list[tuple[str, "int | None"]] = []
    if not LIVE_DIR.exists():
        return current, legacy
    for p in sorted(LIVE_DIR.glob("*.db")):
        if p.stat().st_size == 0:
            continue
        sv = _engram_schema_version(p)
        if sv is None:
            continue
        (current if sv == CURRENT_SCHEMA_VERSION else legacy).append(
            p.name if sv == CURRENT_SCHEMA_VERSION else (p.name, sv)
        )
    return current, legacy


def primary_owner(conn: sqlite3.Connection) -> str:
    """The owner_agent_id owning the most engrams in this DB (else 'default')."""
    row = conn.execute(
        "SELECT owner_agent_id FROM engrams GROUP BY owner_agent_id "
        "ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    return row[0] if row and row[0] else "default"


def pick_data_db() -> tuple[str | None, str | None]:
    """Largest discovered agent DB and its primary owner (for copy-based tests)."""
    current, _ = discover_agent_dbs()
    if not current:
        return None, None
    name = max(current, key=lambda n: (LIVE_DIR / n).stat().st_size)
    conn = ro_connect(LIVE_DIR / name)
    try:
        owner = primary_owner(conn)
    finally:
        conn.close()
    return name, owner


def copy_db(name: str) -> Path:
    """Copy a live DB (+ wal/shm) into the work dir; return the copy path."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    src = LIVE_DIR / name
    dst = WORK_DIR / name
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = LIVE_DIR / (name + suffix)
        if side.exists():
            shutil.copy2(side, WORK_DIR / (name + suffix))
    return dst


# ─────────────────────────── Phase 0 ───────────────────────────


def phase0(h: Harness) -> None:
    def h0a():
        import mnemos.indexer.session_indexer as s

        p = Path(s.__file__).resolve()
        ok = str(p).startswith(str(REPO.resolve()))
        return ok, f"session_indexer.__file__={p}"

    h.check("H0a", "indexer module resolves to live repo", h0a)

    def h0b():
        import asyncio
        from mnemos import mcp_server, simple_mcp

        advanced = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
        simple = {t.name for t in asyncio.run(simple_mcp.simple_mcp.list_tools())}
        # The codebase's own canonical invariant (tests/test_mcp_surface.py) is
        # Structural invariant: simple is a fixed 7-tool surface and the advanced
        # server is a SUPERSET (simple ⊆ advanced). The advanced COUNT is NOT
        # pinned — it grows as features add tools; specific-tool presence is
        # covered by tests/test_mcp_surface.py.
        diff = simple - advanced
        ok = len(simple) == 7 and diff == set() and len(advanced) > len(simple)
        return ok, (
            f"advanced={len(advanced)} simple={len(simple)} "
            f"simple⊆advanced={diff == set()} (count not pinned)"
        )

    h.check("I24", "MCP surface: simple⊆advanced, 7 simple", h0b)


# ─────────────────────────── Phase 1 ───────────────────────────


def phase1(h: Harness) -> None:
    current, legacy = discover_agent_dbs()
    for name, sv in legacy:
        h.skip(
            f"SKIP:{name}", name, f"legacy schema_version={sv} — not a current store"
        )
    if not current:
        h.skip(
            "I1-I7",
            "per-DB integrity",
            f"no current-schema (v{CURRENT_SCHEMA_VERSION}) mnemos DBs under {LIVE_DIR}",
        )
        return
    for name in current:
        conn = ro_connect(LIVE_DIR / name)
        try:
            _phase1_db(h, name, conn)
        finally:
            conn.close()


def _phase1_db(h: Harness, name: str, conn: sqlite3.Connection) -> None:
    tag = name

    # I1 integrity
    def i1():
        res = conn.execute("PRAGMA integrity_check").fetchone()[0]
        return res == "ok", f"integrity_check={res}"

    h.check(f"I1:{tag}", "integrity_check ok", i1)

    # I2 schema_version
    def i2():
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        v = row[0] if row else None
        return v == str(CURRENT_SCHEMA_VERSION), f"schema_version={v}"

    h.check(f"I2:{tag}", f"schema_version == {CURRENT_SCHEMA_VERSION}", i2)

    # I3 core tables
    def i3():
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
        missing = CORE_TABLES - tables
        return not missing, (
            "all core tables present" if not missing else f"missing={sorted(missing)}"
        )

    h.check(f"I3:{tag}", "core tables present", i3)

    # I4 canonical engram columns (subset; extra 'attachments' allowed)
    def i4():
        cols = {r[1] for r in conn.execute("PRAGMA table_info(engrams)").fetchall()}
        missing = CANONICAL_ENGRAM_COLUMNS - cols
        extra = cols - CANONICAL_ENGRAM_COLUMNS
        ok = not missing
        ev = f"{len(cols)} cols; all 21 canonical present"
        if extra:
            ev += f"; extra(allowed)={sorted(extra)}"
        if missing:
            ev = f"MISSING canonical: {sorted(missing)}"
        return ok, ev

    h.check(f"I4:{tag}", "21 canonical engram columns present", i4)

    # I5 unit-range bounds
    def i5():
        bad = []
        for col in UNIT_RANGE_COLUMNS:
            row = conn.execute(f"SELECT MIN({col}), MAX({col}) FROM engrams").fetchone()
            lo, hi = row[0], row[1]
            if lo is None:  # empty table
                continue
            if lo < 0.0 or hi > 1.0:
                bad.append(f"{col}[{lo},{hi}]")
        return not bad, (
            "all of strength/stability/accessibility/resolution ∈ [0,1]"
            if not bad
            else f"out-of-range: {bad}"
        )

    h.check(f"I5:{tag}", "trace values within [0,1]", i5)

    # I6 FTS coverage — every active engram has an FTS row (superset OK).
    # PASS when 0 gaps. WARN (not FAIL) when the gap equals the known pre-existing
    # baseline. FAIL only on an INCREASE over baseline (a real regression).
    active = conn.execute(
        "SELECT COUNT(*) FROM engrams WHERE state='active'"
    ).fetchone()[0]
    active_without_fts = conn.execute(
        "SELECT COUNT(*) FROM engrams e WHERE e.state='active' "
        "AND NOT EXISTS (SELECT 1 FROM engrams_fts f WHERE f.id = e.id)"
    ).fetchone()[0]
    fts_total = conn.execute("SELECT COUNT(*) FROM engrams_fts").fetchone()[0]
    baseline_gap = BASELINE_FTS_GAP.get(name, 0)
    ev6 = (
        f"active={active} fts_rows={fts_total} active_without_fts={active_without_fts} "
        f"(baseline={baseline_gap}; FTS⊇active)"
    )
    if active_without_fts == 0:
        h.record(f"I6:{tag}", "FTS covers all active engrams", "PASS", ev6)
    elif active_without_fts <= baseline_gap:
        h.record(
            f"I6:{tag}",
            "FTS covers all active engrams",
            "WARN",
            ev6
            + " — known pre-existing gap (orphaned engrams: no FTS/embedding/edges)",
        )
        h.anomaly(
            f"I6:{tag} (WARN, pre-existing): {active_without_fts} active engrams lack an "
            f"FTS row — unreachable by recall; not caused by this harness; ≤ baseline"
        )
    else:
        h.record(
            f"I6:{tag}",
            "FTS covers all active engrams",
            "FAIL",
            ev6 + " — REGRESSION: gap exceeds baseline",
        )
        h.anomaly(
            f"I6:{tag} (FAIL): active_without_fts={active_without_fts} > baseline={baseline_gap}"
        )

    # I7 embedding orphans + dim consistency (only if embeddings table has rows).
    # Dim inconsistency is always a hard FAIL. Orphans WARN at baseline, FAIL on increase.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "embeddings" not in tables:
        h.record(
            f"I7:{tag}",
            "embedding orphans==0 & dims consistent",
            "PASS",
            "no embeddings table (n/a)",
        )
    else:
        n = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        if n == 0:
            h.record(
                f"I7:{tag}",
                "embedding orphans==0 & dims consistent",
                "PASS",
                "embeddings table empty (n/a)",
            )
        else:
            orphans = conn.execute(
                "SELECT COUNT(*) FROM embeddings em "
                "WHERE NOT EXISTS (SELECT 1 FROM engrams e WHERE e.id = em.engram_id)"
            ).fetchone()[0]
            dim_rows = conn.execute(
                "SELECT model_name, COUNT(DISTINCT dims) FROM embeddings GROUP BY model_name"
            ).fetchall()
            inconsistent = [r[0] for r in dim_rows if r[1] != 1]
            baseline_orph = BASELINE_EMB_ORPHANS.get(name, 0)
            ev7 = (
                f"rows={n} orphans={orphans} (baseline={baseline_orph}) "
                f"models={len(dim_rows)} dim_consistent={not inconsistent}"
            )
            if inconsistent:
                h.record(
                    f"I7:{tag}",
                    "embedding orphans==0 & dims consistent",
                    "FAIL",
                    ev7 + f" — inconsistent dims for {inconsistent}",
                )
                h.anomaly(
                    f"I7:{tag} (FAIL): inconsistent embedding dims for models {inconsistent}"
                )
            elif orphans == 0:
                h.record(
                    f"I7:{tag}", "embedding orphans==0 & dims consistent", "PASS", ev7
                )
            elif orphans <= baseline_orph:
                h.record(
                    f"I7:{tag}",
                    "embedding orphans==0 & dims consistent",
                    "WARN",
                    ev7
                    + " — known pre-existing dangling embedding(s); retriever skips defensively",
                )
                h.anomaly(
                    f"I7:{tag} (WARN, pre-existing): {orphans} embedding row(s) reference a "
                    f"missing engram — harmless (retriever skips via get_engram→None); ≤ baseline"
                )
            else:
                h.record(
                    f"I7:{tag}",
                    "embedding orphans==0 & dims consistent",
                    "FAIL",
                    ev7 + " — REGRESSION: orphans exceed baseline",
                )
                h.anomaly(
                    f"I7:{tag} (FAIL): orphans={orphans} > baseline={baseline_orph}"
                )


# ─────────────────────────── Phase 2 ───────────────────────────


def phase2_recall_roundtrip(h: Harness) -> None:
    """I8–I11: encode a probe into a COPY, retrieve it, verify reconsolidation."""
    from mnemos.store.sqlite_store import EngramStore
    from mnemos.encoding.encoder import Encoder
    from mnemos.retrieval.reactive import ReactiveRetriever

    src, owner = pick_data_db()
    if not src:
        h.skip("I8-I11", "recall round-trip", "no agent DB available to copy")
        return
    db = copy_db(src)
    token = f"hhprobe{uuid.uuid4().hex[:12]}"
    store = EngramStore(str(db))
    try:
        encoder = Encoder(store, llm_client=None)
        retriever = ReactiveRetriever(store, embedding_index=None)

        probe_content = f"Health harness probe sentinel {token} unique token marker."
        engram = encoder.encode(
            content=probe_content,
            kind="semantic",
            tags=["health-harness", "probe"],
            agent_id=owner,
            source_authority="observed",  # R1: diagnostic probe writer (T3)
        )
        eid = engram.id

        # I8: get_engram round-trips content_at_encoding
        loaded = store.get_engram(eid)
        ok8 = loaded is not None and loaded.content_at_encoding == probe_content
        h.record(
            "I8",
            "encode→get_engram content_at_encoding match",
            "PASS" if ok8 else "FAIL",
            f"id={eid} cae_match={ok8}",
        )

        # snapshot BEFORE
        def snap():
            row = (
                store._get_conn()
                .execute(
                    "SELECT strength, accessibility, access_count, reconsolidation_count "
                    "FROM engrams WHERE id=?",
                    (eid,),
                )
                .fetchone()
            )
            vcount = (
                store._get_conn()
                .execute("SELECT COUNT(*) FROM versions WHERE engram_id=?", (eid,))
                .fetchone()[0]
            )
            return dict(
                strength=row["strength"],
                accessibility=row["accessibility"],
                access_count=row["access_count"],
                reconsolidation_count=row["reconsolidation_count"],
                versions=vcount,
            )

        before = snap()

        # retrieve via the unique token (FTS seed will hit only the probe)
        results = retriever.retrieve(token, agent_id=owner)
        returned = [r for r in results if r.engram.id == eid]
        after = snap()

        # I9: exactly one reconsolidation — strength +0.05 (cap 1.0)
        exp_strength = min(1.0, round(before["strength"] + 0.05, 6))
        got_strength = round(after["strength"], 6)
        ok9 = (
            len(returned) >= 1
            and abs(got_strength - exp_strength) < 1e-6
            and after["reconsolidation_count"] == before["reconsolidation_count"] + 1
            and after["access_count"] == before["access_count"] + 1
            and after["versions"] == before["versions"] + 1
        )
        h.record(
            "I9",
            "retrieve reconsolidates exactly once (+0.05 strength, +1 counts/version)",
            "PASS" if ok9 else "FAIL",
            f"returned={len(returned)} strength {before['strength']}→{after['strength']} "
            f"(exp {exp_strength}) recon {before['reconsolidation_count']}→"
            f"{after['reconsolidation_count']} access {before['access_count']}→"
            f"{after['access_count']} versions {before['versions']}→{after['versions']}",
        )

        # I10: accessibility floor ≥ 0.8 after retrieval
        ok10 = after["accessibility"] >= 0.8 - 1e-9
        h.record(
            "I10",
            "accessibility ≥ 0.8 after retrieval",
            "PASS" if ok10 else "FAIL",
            f"accessibility={after['accessibility']}",
        )

        # I11: any retrieval-formed edges are CO_ACTIVATED, never SUPPORTS
        rows = (
            store._get_conn()
            .execute(
                "SELECT relation, formed_by FROM connections WHERE source_id=?", (eid,)
            )
            .fetchall()
        )
        retrieval_edges = [r for r in rows if r["formed_by"] == "retrieval"]
        bad_edges = [
            r["relation"] for r in retrieval_edges if r["relation"] != "co_activated"
        ]
        # also assert no retrieval edge is a 'supports' edge specifically
        ok11 = not bad_edges
        h.record(
            "I11",
            "retrieval co-edges are 'co_activated' (never 'supports')",
            "PASS" if ok11 else "FAIL",
            f"retrieval_edges={len(retrieval_edges)} non_co_activated={bad_edges or 'none'}",
        )
        if bad_edges:
            h.anomaly(f"I11: retrieval formed non-co_activated edges: {bad_edges}")
    finally:
        store.close()


# ─────────────────────────── Phase 3 ───────────────────────────


def phase3_wakeup_packet(h: Harness) -> None:
    """I12–I14: context packet content, blank-query resilience, scope isolation."""
    from mnemos.store.sqlite_store import EngramStore
    from mnemos.encoding.encoder import Encoder
    from mnemos.interface.context_packet import build_context_packet

    db = WORK_DIR / f"packet_{uuid.uuid4().hex[:8]}.db"
    store = EngramStore(str(db))
    try:
        encoder = Encoder(store, llm_client=None)
        probe = f"wakeprobe{uuid.uuid4().hex[:10]}"

        # Seed scope A: agent=alpha/person=p1/project=projA
        store.write_functional_memory(
            f"alpha functional note about {probe}",
            agent_id="alpha",
            person_id="p1",
            project_scope="projA",
            memory_type="fact",
        )
        store.write_hypomnema_entry(
            f"alpha hypomnema continuity {probe}",
            agent_id="alpha",
            person_id="p1",
            project_scope="projA",
            domain="topical",
        )
        encoder.encode(
            content=f"alpha engram seed {probe} marker",
            kind="semantic",
            tags=["wake"],
            agent_id="alpha",
            source_authority="observed",  # R1: diagnostic seed writer (T3)
        )
        # Seed scope B (different agent/person/project) with a distinct secret
        secretB = f"betasecret{uuid.uuid4().hex[:10]}"
        store.write_functional_memory(
            f"beta-only functional {secretB}",
            agent_id="beta",
            person_id="p2",
            project_scope="projB",
            memory_type="fact",
        )
        store.write_hypomnema_entry(
            f"beta-only hypomnema {secretB}",
            agent_id="beta",
            person_id="p2",
            project_scope="projB",
            domain="topical",
        )

        # I12: non-empty prompt w/ header + seeded functional+hypomnema text
        pktA = build_context_packet(
            store, query=probe, agent_id="alpha", person_id="p1", project_scope="projA"
        )
        prompt = pktA["prompt"]
        ok12 = (
            "## Mnemos Context Packet" in prompt
            and "alpha functional note" in prompt
            and "alpha hypomnema continuity" in prompt
            and len(prompt) > 0
        )
        h.record(
            "I12",
            "packet has header + seeded functional & hypomnema text",
            "PASS" if ok12 else "FAIL",
            f"prompt_len={len(prompt)} header={'## Mnemos Context Packet' in prompt} "
            f"functional={'alpha functional note' in prompt} "
            f"hypomnema={'alpha hypomnema continuity' in prompt}",
        )

        # I13: blank query → no engrams, but functional/hypomnema still render,
        # no crash when emotional state is missing
        pktBlank = build_context_packet(
            store, query="", agent_id="alpha", person_id="p1", project_scope="projA"
        )
        ok13 = (
            pktBlank["mnemos_engrams"] == []
            and "## Mnemos Context Packet" in pktBlank["prompt"]
            and "alpha functional note" in pktBlank["prompt"]
            and "alpha hypomnema continuity" in pktBlank["prompt"]
        )
        h.record(
            "I13",
            "blank query → 0 engrams; functional/hypomnema still render; no crash",
            "PASS" if ok13 else "FAIL",
            f"engrams={len(pktBlank['mnemos_engrams'])} "
            f"functional_rendered={'alpha functional note' in pktBlank['prompt']} "
            f"hypomnema_rendered={'alpha hypomnema continuity' in pktBlank['prompt']}",
        )

        # I14: scope isolation — scope A packet shows none of scope B's entries
        leak = secretB in pktA["prompt"] or secretB in pktBlank["prompt"]
        ok14 = not leak
        h.record(
            "I14",
            "scope isolation (no cross-scope leakage)",
            "PASS" if ok14 else "FAIL",
            f"betaB_secret_leaked_into_alpha_packet={leak}",
        )
        if leak:
            h.anomaly(
                "I14: cross-scope leakage — scope B content appeared in scope A packet"
            )
    finally:
        store.close()


# ─────────────────────────── Phase 4 ───────────────────────────


def phase4_hypomnema(h: Harness) -> None:
    """I15–I18: hypomnema write / revise / supersede / promotion candidacy."""
    from mnemos.store.sqlite_store import EngramStore

    db = WORK_DIR / f"hypo_{uuid.uuid4().hex[:8]}.db"
    store = EngramStore(str(db))
    scope = dict(agent_id="hyA", person_id="hyP", project_scope="hyProj")
    try:
        # I15: write → active, revision_count 0; invalid inputs raise ValueError
        hid = store.write_hypomnema_entry(
            "initial continuity entry", **scope, domain="topical"
        )
        row = store.get_hypomnema_entry(hid, **scope)
        write_ok = (
            row is not None and row["active"] is True and row["revision_count"] == 0
        )
        raises = {"bad_source": False, "bad_domain": False, "empty": False}
        try:
            store.write_hypomnema_entry("x", **scope, source="not-a-source")
        except ValueError:
            raises["bad_source"] = True
        try:
            store.write_hypomnema_entry("x", **scope, domain="not-a-domain")
        except ValueError:
            raises["bad_domain"] = True
        try:
            store.write_hypomnema_entry("   ", **scope)
        except ValueError:
            raises["empty"] = True
        ok15 = write_ok and all(raises.values())
        h.record(
            "I15",
            "write: active/rev0; invalid source/domain/empty → ValueError",
            "PASS" if ok15 else "FAIL",
            f"active={row['active'] if row else None} rev={row['revision_count'] if row else None} "
            f"raises={raises}",
        )

        # I16: revise → same id, rev+1, prior_content audited; empty reason ValueError; unknown id KeyError
        rid = store.revise_hypomnema_entry(
            hid, "revised continuity entry", reason="clarify", **scope
        )
        revrow = store.get_hypomnema_entry(hid, **scope)
        audited = any(
            r.get("prior_content") == "initial continuity entry"
            for r in revrow["revisions"]
        )
        empty_reason_raises = False
        try:
            store.revise_hypomnema_entry(hid, "more", reason="   ", **scope)
        except ValueError:
            empty_reason_raises = True
        unknown_raises = False
        try:
            store.revise_hypomnema_entry("no-such-id", "x", reason="r", **scope)
        except KeyError:
            unknown_raises = True
        ok16 = (
            rid == hid
            and revrow["revision_count"] == 1
            and audited
            and empty_reason_raises
            and unknown_raises
        )
        h.record(
            "I16",
            "revise: same id, rev+1, prior audited; empty reason→ValueError; unknown→KeyError",
            "PASS" if ok16 else "FAIL",
            f"same_id={rid == hid} rev={revrow['revision_count']} audited={audited} "
            f"empty_reason_raises={empty_reason_raises} unknown_raises={unknown_raises}",
        )

        # I17: supersede → new id; old inactive + superseded_by==new; new.source=='synthesized';
        #      old recoverable via include_inactive
        new_id = store.supersede_hypomnema_entry(
            hid, "superseding entry", reason="replace", **scope
        )
        oldrow = store.get_hypomnema_entry(hid, **scope)  # active_only False default
        newrow = store.get_hypomnema_entry(new_id, **scope)
        recoverable = any(
            r["id"] == hid
            for r in store.search_hypomnema(
                "", **scope, include_inactive=True, limit=50
            )
        )
        ok17 = (
            new_id != hid
            and oldrow is not None
            and oldrow["active"] is False
            and oldrow["superseded_by"] == new_id
            and newrow is not None
            and newrow["source"] == "synthesized"
            and recoverable
        )
        h.record(
            "I17",
            "supersede: new id; old inactive+linked; new source='synthesized'; old recoverable",
            "PASS" if ok17 else "FAIL",
            f"new!=old={new_id != hid} old_active={oldrow['active'] if oldrow else None} "
            f"superseded_by_ok={oldrow['superseded_by'] == new_id if oldrow else None} "
            f"new_source={newrow['source'] if newrow else None} recoverable={recoverable}",
        )

        # I18: promotion candidate iff active ∧ not graduated ∧ conf≥0.82 ∧ sal≥0.65 ∧ (rev≥1 ∨ foundational)
        # Build a positive case: write then revise (rev≥1) with high conf/sal
        pos = store.write_hypomnema_entry(
            "promote me", **scope, domain="identity", confidence=0.9, salience=0.9
        )
        store.revise_hypomnema_entry(
            pos, "promote me v2", reason="bump", confidence=0.9, salience=0.9, **scope
        )
        # Negative: high conf/sal but rev=0 and not foundational → excluded
        neg_lowrev = store.write_hypomnema_entry(
            "no revisions yet", **scope, confidence=0.9, salience=0.9
        )
        # Negative: foundational satisfies rev requirement but low confidence → excluded
        neg_lowconf = store.write_hypomnema_entry(
            "foundational but unsure",
            **scope,
            foundational=True,
            confidence=0.5,
            salience=0.9,
        )
        # Positive via foundational: high conf/sal + foundational (rev not required)
        pos_found = store.write_hypomnema_entry(
            "foundational and sure",
            **scope,
            foundational=True,
            confidence=0.9,
            salience=0.9,
        )
        cand_ids = {
            c["id"] for c in store.get_hypomnema_promotion_candidates(**scope, limit=50)
        }
        ok18 = (
            pos in cand_ids
            and pos_found in cand_ids
            and neg_lowrev not in cand_ids
            and neg_lowconf not in cand_ids
        )
        h.record(
            "I18",
            "promotion candidacy predicate (conf≥.82 ∧ sal≥.65 ∧ (rev≥1 ∨ foundational))",
            "PASS" if ok18 else "FAIL",
            f"pos_rev∈={pos in cand_ids} pos_found∈={pos_found in cand_ids} "
            f"neg_rev0∉={neg_lowrev not in cand_ids} neg_lowconf∉={neg_lowconf not in cand_ids}",
        )
    finally:
        store.close()


# ─────────────────────────── Phase 5 ───────────────────────────


def phase5_indexer(h: Harness) -> None:
    """I19–I21: project resolution in a subprocess with Path.home monkeypatched
    to a throwaway tmp HOME so NO live config is read or written."""
    script = r"""
import json, os, sys, tempfile
from pathlib import Path

tmp_home = Path(tempfile.mkdtemp(prefix="mnemos_hh_home_"))
_real_home = Path.home
Path.home = staticmethod(lambda: tmp_home)  # type: ignore[assignment]

import mnemos.indexer.session_indexer as si

out = {}

# H5a: env-provided known projects, no config arg
os.environ["MNEMOS_KNOWN_PROJECTS"] = "a,b"
idx = si.SessionIndexer(agent_id="hh", db_path=str(tmp_home / "hh.db"))
out["h5a_known"] = idx.known_projects
del os.environ["MNEMOS_KNOWN_PROJECTS"]

# H5b: no env, no config, empty tmp HOME
os.environ.pop("MNEMOS_KNOWN_PROJECTS", None)
os.environ.pop("MNEMOS_ACTIVE_PROJECTS", None)
idx2 = si.SessionIndexer(agent_id="hh2", db_path=str(tmp_home / "hh2.db"))
out["h5b_known"] = idx2.known_projects
out["h5b_active"] = idx2.active_projects
out["h5b_constructed"] = True

# H5c: malformed JSON at <tmpHOME>/.mnemos/config.json → construction must still succeed
cfg_dir = tmp_home / ".mnemos"
cfg_dir.mkdir(parents=True, exist_ok=True)
(cfg_dir / "config.json").write_text("{ this is not valid json ::: ")
try:
    idx3 = si.SessionIndexer(agent_id="hh3", db_path=str(tmp_home / "hh3.db"))
    out["h5c_constructed"] = True
    out["h5c_error"] = None
except Exception as e:
    out["h5c_constructed"] = False
    out["h5c_error"] = f"{type(e).__name__}: {e}"

# safety self-check: confirm we never read the live config
out["home_was_tmp"] = str(Path.home()) == str(tmp_home)
print("HH_RESULT " + json.dumps(out))
"""
    proc = subprocess.run(
        [str(REPO / ".venv/bin/python"), "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("HH_RESULT "):
            import json as _json

            payload = _json.loads(line[len("HH_RESULT ") :])
            break

    if payload is None:
        ev = f"subprocess produced no result (rc={proc.returncode}); stderr={proc.stderr[-300:]}"
        for ident, lbl in (
            ("I19", "H5a env known_projects"),
            ("I20", "H5b empty HOME → []"),
            ("I21", "H5c malformed config still constructs"),
        ):
            h.record(ident, lbl, "FAIL", ev)
        return

    h.check(
        "I19",
        "H5a MNEMOS_KNOWN_PROJECTS='a,b' → ['a','b']",
        lambda: (
            payload["h5a_known"] == ["a", "b"],
            f"known_projects={payload['h5a_known']}",
        ),
    )
    h.check(
        "I20",
        "H5b no env/config, empty HOME → known=[] active=[] constructs",
        lambda: (
            payload["h5b_known"] == []
            and payload["h5b_active"] == []
            and payload["h5b_constructed"],
            f"known={payload['h5b_known']} active={payload['h5b_active']} "
            f"constructed={payload['h5b_constructed']} home_tmp={payload['home_was_tmp']}",
        ),
    )

    def i21():
        ok = payload["h5c_constructed"] is True
        if not ok:
            h.anomaly(
                f"I21: malformed config.json broke SessionIndexer construction: "
                f"{payload['h5c_error']}"
            )
        return ok, (
            f"constructed={payload['h5c_constructed']} err={payload['h5c_error']} "
            f"(regression gate for upcoming load_config change)"
        )

    h.check("I21", "H5c malformed config.json → SessionIndexer still constructs", i21)


# ─────────────────────────── Phase 6 ───────────────────────────


def phase6_consolidation(h: Harness) -> None:
    """I22–I23: deep consolidation cycle on a COPY with llm_client=None."""
    from mnemos.store.sqlite_store import EngramStore
    from mnemos.consolidation.daemon import ConsolidationDaemon

    src, owner = pick_data_db()
    if not src:
        h.skip("I22-I23", "consolidation tick", "no agent DB available to copy")
        return
    db = copy_db(src)
    store = EngramStore(str(db))
    AGENT = owner
    try:
        conn = store._get_conn()

        def counts():
            engr = conn.execute("SELECT COUNT(*) FROM engrams").fetchone()[0]
            arch = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
            clog = conn.execute("SELECT COUNT(*) FROM consolidation_log").fetchone()[0]
            cae = conn.execute(
                "SELECT COUNT(*) FROM engrams WHERE content_at_encoding IS NOT NULL "
                "AND content_at_encoding != ''"
            ).fetchone()[0]
            return dict(engrams=engr, archive=arch, clog=clog, cae=cae)

        before = counts()
        daemon = ConsolidationDaemon(store=store, config={}, llm_client=None)
        stats = daemon.run_cycle(deep=True, agent_id=AGENT)
        after = counts()

        expected_passes = [
            "connection_discovery",
            "decay",
            "softening",
            "belief_review",
            "reflection",
        ]
        passes_run = stats.get("passes_run", [])
        error_keys = [k for k in stats if k.endswith("_error")]

        # I22: all five passes ran, no *_error keys
        ok22 = passes_run == expected_passes and not error_keys
        h.record(
            "I22",
            "deep cycle runs all 5 passes with no *_error",
            "PASS" if ok22 else "FAIL",
            f"passes_run={passes_run} error_keys={error_keys or 'none'}",
        )
        if error_keys:
            for k in error_keys:
                h.anomaly(f"I22: consolidation pass error [{k}]: {stats[k]}")

        # I23: totals non-decreasing, exactly one new log row, no hard deletes
        ok23 = (
            after["engrams"] >= before["engrams"]
            and after["archive"] >= before["archive"]
            and after["clog"] == before["clog"] + 1
            and after["cae"] >= before["cae"]
        )
        h.record(
            "I23",
            "totals non-decreasing; +1 log row; no hard deletes",
            "PASS" if ok23 else "FAIL",
            f"engrams {before['engrams']}→{after['engrams']} "
            f"archive {before['archive']}→{after['archive']} "
            f"clog {before['clog']}→{after['clog']} (Δ should be 1) "
            f"non_null_cae {before['cae']}→{after['cae']}",
        )
    finally:
        store.close()


# ─────────────────────────── Phase 7 ───────────────────────────


def phase7_pytest(h: Harness, enabled: bool) -> None:
    """I25: regression suite. conftest isolates env to tmp_path; never touches ~/.mnemos."""
    if not enabled:
        h.skip("I25", "regression suite (pytest)", "skipped via --no-pytest")
        return

    def run():
        proc = subprocess.run(
            [str(REPO / ".venv/bin/python"), "-m", "pytest", "tests/", "-q"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        tail = proc.stdout.strip().splitlines()
        last = tail[-1] if tail else ""
        # last summary line looks like '189 passed, 1 skipped in 12.34s'
        ok = proc.returncode == 0 and "failed" not in proc.stdout.lower()
        return ok, f"rc={proc.returncode} :: {last}"

    h.check("I25", "regression suite passes (baseline ~189 passed/1 skipped)", run)


# ─────────────────────────── main ───────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Mnemos end-to-end health harness")
    ap.add_argument(
        "--no-pytest",
        action="store_true",
        help="skip Phase 7 (the slow regression suite)",
    )
    args = ap.parse_args()

    # Ensure we import the live repo package, not any installed copy.
    sys.path.insert(0, str(REPO))
    os.chdir(str(REPO))

    print("Mnemos Health Harness")
    print(f"  repo     : {REPO}")
    print(f"  live DBs : {LIVE_DIR}")
    print(f"  work dir : {WORK_DIR} (copies only — live DBs never mutated)")
    print("=" * 72)

    # Clean prior work dir to avoid stale copies skewing results.
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    h = Harness()

    print("\n── Phase 0: surface sanity ──")
    phase0(h)

    print("\n── Phase 1: per-DB integrity & invariants (read-only live) ──")
    phase1(h)

    print("\n── Phase 2: recall round-trip (copy of memory.db) ──")
    phase2_recall_roundtrip(h)

    print("\n── Phase 3: wake-up packet (temp store) ──")
    phase3_wakeup_packet(h)

    print("\n── Phase 4: hypomnema lifecycle (temp store) ──")
    phase4_hypomnema(h)

    print("\n── Phase 5: indexer project resolution (subprocess, tmp HOME) ──")
    phase5_indexer(h)

    print("\n── Phase 6: consolidation tick (copy of memory.db) ──")
    phase6_consolidation(h)

    print("\n── Phase 7: regression suite ──")
    phase7_pytest(h, enabled=not args.no_pytest)

    return h.summary()


if __name__ == "__main__":
    raise SystemExit(main())
