"""
Identity diff: computed identity (the graph) vs declared identity (SOUL.md).

SOUL.md is authored; the graph is grown. The system's deepest claim — that
identity is computed from the graph — is only proven the day the computed
identity can be compared against the declared one, and the collision is
handled deliberately rather than arriving as a bug.

`mnemos identity diff` compares the two and reports:

- alignments        — declared traits the graph actually supports
- divergences       — both directions:
                        declared-but-not-grown (the soul claims something
                        the graph shows no trace of), and
                        grown-but-contradicting (the graph holds as uncertain
                        or contradicted something the soul declares settled)
- emergences        — things the agent has become that the soul never declared

Divergences are surfaced to the agent itself at the next `mnemos_context`
via a hypomnema continuity note — the agent participates in resolving who
it is. An accepted divergence triggers an epoch transition
(`mnemos identity accept --divergence N`).

The comparison is deterministic and lexical by design: it works with no LLM,
its findings are reproducible, and it labels its own signal quality rather
than faking confidence. An optional model-assisted pass may annotate
findings but never changes bucket membership or finding ids.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ulid as _ulid_mod

from .core.identity import AgentIdentity, MemoryProfile
from .store.read_visibility import READ_VISIBILITY_OPERATIONAL

if TYPE_CHECKING:
    from .simple_runtime import MnemosScope
    from .store.sqlite_store import EngramStore


def _gen_id(prefix: str) -> str:
    if hasattr(_ulid_mod, "new"):
        return f"{prefix}_{_ulid_mod.new()}"
    from ulid import ULID

    return f"{prefix}_{ULID()}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Soul parsing ─────────────────────────────────────────────────────

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_H2_RE = re.compile(r"^##\s+(.+)$")
_H3_RE = re.compile(r"^###\s+(.+)$")
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}|^\.{3,}$|\.\.\.$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class SoulClaim:
    """One declared trait: a bullet or sentence from a SOUL.md section."""

    section: str
    text: str
    is_placeholder: bool


@dataclass
class SoulDocument:
    path: str
    sections: list[str] = field(default_factory=list)
    claims: list[SoulClaim] = field(default_factory=list)
    placeholder_sections: list[str] = field(default_factory=list)

    @property
    def real_claims(self) -> list[SoulClaim]:
        return [c for c in self.claims if not c.is_placeholder]


def _is_placeholder_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if _PLACEHOLDER_RE.search(stripped) and len(_tokens(stripped)) < 4:
        return True
    return stripped in ("...", "…") or set(stripped) <= {".", "…"}


def parse_soul_file(path: str | Path) -> SoulDocument:
    """Parse a SOUL.md into sectioned claims.

    Free-text markdown: H2/H3 headings become section names
    (H3 nests as "Parent > Child"), bullets and sentences become claims.
    Unfilled template sections are recorded as placeholders so the diff
    can report them as "never declared" rather than as divergence.
    """
    p = Path(path).expanduser()
    raw = p.read_text(encoding="utf-8")
    text = _COMMENT_RE.sub("", raw)

    doc = SoulDocument(path=str(p))
    current_h2: str | None = None
    current_section: str | None = None
    section_claims: dict[str, list[SoulClaim]] = {}

    for line in text.splitlines():
        h2 = _H2_RE.match(line)
        h3 = _H3_RE.match(line)
        if h2:
            current_h2 = h2.group(1).strip()
            current_section = current_h2
            doc.sections.append(current_section)
            section_claims.setdefault(current_section, [])
            continue
        if h3:
            sub = h3.group(1).strip()
            current_section = f"{current_h2} > {sub}" if current_h2 else sub
            doc.sections.append(current_section)
            section_claims.setdefault(current_section, [])
            continue
        if current_section is None:
            continue

        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            fragments = [stripped[2:].strip()]
        else:
            fragments = [s.strip() for s in _SENTENCE_SPLIT_RE.split(stripped)]
        for frag in fragments:
            if not frag:
                continue
            claim = SoulClaim(
                section=current_section,
                text=frag,
                is_placeholder=_is_placeholder_text(frag),
            )
            section_claims[current_section].append(claim)
            doc.claims.append(claim)

    for section, claims in section_claims.items():
        if not claims or all(c.is_placeholder for c in claims):
            doc.placeholder_sections.append(section)

    return doc


def resolve_soul_path(explicit: str | None) -> Path | None:
    """Resolve the SOUL.md to diff against.

    Precedence: explicit --soul > $MNEMOS_WORKSPACE/SOUL.md > ./SOUL.md.
    Returns None when nothing resolvable exists.
    """
    import os

    if explicit:
        return Path(explicit).expanduser()
    workspace = os.environ.get("MNEMOS_WORKSPACE", "").strip()
    if workspace:
        candidate = Path(workspace).expanduser() / "SOUL.md"
        if candidate.exists():
            return candidate
    local = Path.cwd() / "SOUL.md"
    if local.exists():
        return local
    return None


# ── Graph-derived identity ───────────────────────────────────────────

VALUE_CONFIDENCE_FLOOR = 0.7
QUESTION_CONFIDENCE_CEIL = 0.4
CONCERN_MATERIALITY = 3
PREOCCUPATION_MATERIALITY = 2
HUB_MATERIALITY = 3


@dataclass
class GraphItem:
    """One facet of who the graph says the agent is."""

    facet: str  # concern | value | question | preoccupation | hub
    text: str
    weight: float


@dataclass
class ComputedIdentity:
    agent_id: str
    items: list[GraphItem] = field(default_factory=list)
    engram_count: int = 0
    belief_count: int = 0
    contradiction_edges: list[tuple[str, str]] = field(default_factory=list)
    """(hub_text, contradicting_text) pairs from `contradicts` connections."""

    def facet(self, name: str) -> list[GraphItem]:
        return [i for i in self.items if i.facet == name]


def compute_graph_identity(store: EngramStore, agent_id: str) -> ComputedIdentity:
    """Compute the graph-derived identity profile for an agent.

    Reuses the reflection pass's profile computation (concerns from tag
    frequency, hubs from connection counts) and layers the diff-specific
    thresholds on top: beliefs >= 0.7 as values, beliefs <= 0.4 as living
    questions, top-reconsolidated engrams as preoccupations.
    """
    from .consolidation.reflection import compute_identity_profile

    engrams = store.get_active_engrams(agent_id=agent_id, limit=1000)
    identity = store.get_identity(agent_id)
    if identity is None:
        identity = AgentIdentity(memory_profile=MemoryProfile(agent_id=agent_id))

    profile = compute_identity_profile(store, engrams, identity)
    beliefs = store.get_beliefs(agent_id, active_only=True)

    computed = ComputedIdentity(
        agent_id=agent_id,
        engram_count=len(engrams),
        belief_count=len(beliefs),
    )

    for tag, count in profile.persistent_concerns:
        computed.items.append(GraphItem("concern", tag, float(count)))

    for b in beliefs:
        if b.confidence >= VALUE_CONFIDENCE_FLOOR:
            computed.items.append(GraphItem("value", b.content, b.confidence))
        elif b.confidence <= QUESTION_CONFIDENCE_CEIL:
            computed.items.append(GraphItem("question", b.content, b.confidence))

    for display, n_conn in profile.hub_concepts:
        computed.items.append(GraphItem("hub", display, float(n_conn)))

    by_recon = sorted(
        (e for e in engrams if e.reconsolidation_count >= 1),
        key=lambda e: e.reconsolidation_count,
        reverse=True,
    )
    for e in by_recon[:5]:
        display = e.impact or e.content
        computed.items.append(
            GraphItem("preoccupation", display, float(e.reconsolidation_count))
        )

    # Evidence held against ideas: contradicts edges off any engram.
    by_id = {e.id: e for e in engrams}
    for e in engrams:
        for conn in e.connections:
            if conn.relation == "contradicts" and conn.target_id in by_id:
                target = by_id[conn.target_id]
                computed.contradiction_edges.append(
                    (e.impact or e.content, target.impact or target.content)
                )

    return computed


# ── Lexical comparison ───────────────────────────────────────────────

_STOPWORDS = frozenset(
    """a about after all also am an and any are as at be because been being but
    by can did do does doing don for from had has have having he her here hers
    him his how i if in into is it its itself just me more most my myself no
    nor not of off on once only or other our ours out over own same she should
    so some such than that the their theirs them then there these they this
    those through to too under until up very was we were what when where which
    while who whom why will with you your yours
    agent agents exist exists thing things section work make makes made way
    one two like get got really""".split()
)

STRONG_OVERLAP = 0.6
WEAK_OVERLAP = 0.3


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9_-]+", text.lower())
    out = set()
    for w in words:
        if w in _STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s") and w[:-1] not in _STOPWORDS:
            w = w[:-1]
        out.add(w)
    return out


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# ── Findings ─────────────────────────────────────────────────────────

@dataclass
class DiffFinding:
    kind: str  # alignment | divergence_declared | divergence_grown | emergence
    finding_id: str
    soul_section: str | None
    soul_claim: str | None
    graph_facet: str | None
    graph_item: str | None
    score: float
    confidence_label: str  # strong | moderate | weak
    note: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "finding_id": self.finding_id,
            "soul_section": self.soul_section,
            "soul_claim": self.soul_claim,
            "graph_facet": self.graph_facet,
            "graph_item": self.graph_item,
            "score": self.score,
            "confidence_label": self.confidence_label,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DiffFinding:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _finding_id(kind: str, soul_claim: str | None, graph_item: str | None) -> str:
    digest = hashlib.sha1(
        f"{kind}|{soul_claim or ''}|{graph_item or ''}".encode("utf-8")
    ).hexdigest()
    return digest[:8]


@dataclass
class IdentityDiffReport:
    agent_id: str
    soul_path: str
    computed_at: str = field(default_factory=_now_iso)
    alignments: list[DiffFinding] = field(default_factory=list)
    divergences: list[DiffFinding] = field(default_factory=list)
    emergences: list[DiffFinding] = field(default_factory=list)
    placeholder_sections: list[str] = field(default_factory=list)
    signal_quality: str = "lexical only"
    graph_stats: dict = field(default_factory=dict)
    soul_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "soul_path": self.soul_path,
            "computed_at": self.computed_at,
            "alignments": [f.to_dict() for f in self.alignments],
            "divergences": [f.to_dict() for f in self.divergences],
            "emergences": [f.to_dict() for f in self.emergences],
            "placeholder_sections": self.placeholder_sections,
            "signal_quality": self.signal_quality,
            "graph_stats": self.graph_stats,
            "soul_stats": self.soul_stats,
        }

    @classmethod
    def from_dict(cls, d: dict) -> IdentityDiffReport:
        return cls(
            agent_id=d.get("agent_id", "default"),
            soul_path=d.get("soul_path", ""),
            computed_at=d.get("computed_at", ""),
            alignments=[DiffFinding.from_dict(f) for f in d.get("alignments", [])],
            divergences=[DiffFinding.from_dict(f) for f in d.get("divergences", [])],
            emergences=[DiffFinding.from_dict(f) for f in d.get("emergences", [])],
            placeholder_sections=d.get("placeholder_sections", []),
            signal_quality=d.get("signal_quality", "lexical only"),
            graph_stats=d.get("graph_stats", {}),
            soul_stats=d.get("soul_stats", {}),
        )


def diff_identity(
    soul: SoulDocument,
    graph: ComputedIdentity,
    llm_client: Any | None = None,
) -> IdentityDiffReport:
    """Compare declared identity (soul) against computed identity (graph).

    Deterministic lexical core; the optional LLM pass only annotates
    finding notes and labels — bucket membership and ids never change.
    """
    report = IdentityDiffReport(
        agent_id=graph.agent_id,
        soul_path=soul.path,
        placeholder_sections=list(soul.placeholder_sections),
        graph_stats={
            "engram_count": graph.engram_count,
            "belief_count": graph.belief_count,
            "values": len(graph.facet("value")),
            "questions": len(graph.facet("question")),
            "concerns": len(graph.facet("concern")),
            "hubs": len(graph.facet("hub")),
            "preoccupations": len(graph.facet("preoccupation")),
        },
        soul_stats={
            "sections": len(soul.sections),
            "claims": len(soul.real_claims),
        },
    )

    claims = [c for c in soul.real_claims if len(_tokens(c.text)) >= 2]
    item_tokens = {id(i): _tokens(i.text) for i in graph.items}
    claim_tokens = {id(c): _tokens(c.text) for c in claims}
    small_graph = graph.engram_count < 10

    # 1. Grown-but-contradicting — two deterministic signals only. These
    #    are computed first because a claim in grown tension belongs to
    #    exactly one bucket: it is neither aligned nor merely "not grown".
    # 1a. Settledness inversion: the soul declares as settled what the
    #     graph holds uncertain (a low-confidence belief lexically
    #     matching a confident soul claim).
    grown: list[DiffFinding] = []
    grown_claim_texts: set[str] = set()
    for item in graph.facet("question"):
        it = item_tokens[id(item)]
        for claim in claims:
            score = _overlap(claim_tokens[id(claim)], it)
            if score >= WEAK_OVERLAP:
                grown.append(
                    DiffFinding(
                        kind="divergence_grown",
                        finding_id=_finding_id("divergence_grown", claim.text, item.text),
                        soul_section=claim.section,
                        soul_claim=claim.text,
                        graph_facet="question",
                        graph_item=item.text,
                        score=round(score, 3),
                        confidence_label="moderate",
                        note=(
                            "soul declares as settled what the graph holds "
                            f"uncertain (belief confidence {int(item.weight * 100)}%)"
                        ),
                    )
                )
                grown_claim_texts.add(claim.text)
                break

    # 1b. Contradiction edges: the graph holds evidence against a
    #     declared trait (a `contradicts` connection off an engram
    #     that matches a soul claim).
    for source_text, contradicting_text in graph.contradiction_edges:
        st = _tokens(source_text)
        for claim in claims:
            score = _overlap(claim_tokens[id(claim)], st)
            if score >= WEAK_OVERLAP:
                grown.append(
                    DiffFinding(
                        kind="divergence_grown",
                        finding_id=_finding_id(
                            "divergence_grown", claim.text, contradicting_text
                        ),
                        soul_section=claim.section,
                        soul_claim=claim.text,
                        graph_facet="contradiction",
                        graph_item=contradicting_text,
                        score=round(score, 3),
                        confidence_label="weak",
                        note=(
                            "the graph holds evidence against this declared "
                            "trait (contradicts edge) — weak signal at the "
                            "lexical level"
                        ),
                    )
                )
                grown_claim_texts.add(claim.text)
                break

    # 2. Per-claim best match → alignment or declared-but-not-grown.
    #    Matching counts only support-bearing facets: a claim that
    #    matches a low-confidence belief is in tension, not aligned.
    support_items = [i for i in graph.items if i.facet != "question"]
    declared: list[DiffFinding] = []
    for claim in claims:
        if claim.text in grown_claim_texts:
            continue
        ct = claim_tokens[id(claim)]
        best_item: GraphItem | None = None
        best_score = 0.0
        for item in support_items:
            score = _overlap(ct, item_tokens[id(item)])
            if score > best_score:
                best_score, best_item = score, item

        if best_item is not None and best_score >= WEAK_OVERLAP:
            label = "strong" if best_score >= STRONG_OVERLAP else "moderate"
            report.alignments.append(
                DiffFinding(
                    kind="alignment",
                    finding_id=_finding_id("alignment", claim.text, best_item.text),
                    soul_section=claim.section,
                    soul_claim=claim.text,
                    graph_facet=best_item.facet,
                    graph_item=best_item.text,
                    score=round(best_score, 3),
                    confidence_label=label,
                    note=f"declared trait has graph support ({best_item.facet}, weight {best_item.weight:g})",
                )
            )
        else:
            note = "no graph trace of this declared trait"
            label = "moderate"
            if small_graph:
                note += " — graph may be too young to judge"
                label = "weak"
            declared.append(
                DiffFinding(
                    kind="divergence_declared",
                    finding_id=_finding_id("divergence_declared", claim.text, None),
                    soul_section=claim.section,
                    soul_claim=claim.text,
                    graph_facet=None,
                    graph_item=best_item.text if best_item else None,
                    score=round(best_score, 3),
                    confidence_label=label,
                    note=note,
                )
            )

    # Grown-contradicting findings lead the divergence list; dedupe by id.
    seen_ids = set()
    ordered: list[DiffFinding] = []
    for f in sorted(grown, key=lambda f: f.score, reverse=True) + declared:
        if f.finding_id not in seen_ids:
            seen_ids.add(f.finding_id)
            ordered.append(f)
    report.divergences = ordered

    # 3. Emergences: material graph items the soul never declared.
    materiality = {
        "concern": CONCERN_MATERIALITY,
        "preoccupation": PREOCCUPATION_MATERIALITY,
        "hub": HUB_MATERIALITY,
        "value": 0,  # all values (conf >= 0.7) are material by construction
    }
    claim_token_sets = [_tokens(c.text) for c in claims]
    for item in graph.items:
        floor = materiality.get(item.facet)
        if floor is None or item.weight < floor:
            continue
        it = item_tokens[id(item)]
        if not it:
            continue
        best = max((_overlap(ct, it) for ct in claim_token_sets), default=0.0)
        if best < WEAK_OVERLAP:
            note = "the agent has become this; the soul never declared it"
            report.emergences.append(
                DiffFinding(
                    kind="emergence",
                    finding_id=_finding_id("emergence", None, item.text),
                    soul_section=None,
                    soul_claim=None,
                    graph_facet=item.facet,
                    graph_item=item.text,
                    score=round(best, 3),
                    confidence_label="moderate",
                    note=note,
                )
            )

    if not grown:
        report.signal_quality = (
            "lexical only — no grown-contradicting divergences detectable "
            "at this level"
        )

    if llm_client is not None:
        _enrich_with_model(report, llm_client)

    return report


def _enrich_with_model(report: IdentityDiffReport, llm_client: Any) -> None:
    """Optional model-assisted annotation. Notes only — never membership."""
    findings = report.divergences + report.emergences
    if not findings:
        return
    lines = "\n".join(
        f"{f.finding_id}: [{f.kind}] declared={f.soul_claim!r} grown={f.graph_item!r}"
        for f in findings[:12]
    )
    prompt = (
        "You are reviewing identity-diff findings for an AI agent: its "
        "authored self-description vs what its memory graph shows it keeps "
        "returning to. For each finding id, give a one-line honest assessment "
        "of whether the tension looks real or a lexical artifact. Format: "
        "'<finding_id>: <assessment>'. Nothing else.\n\n" + lines
    )
    try:
        raw = llm_client.complete(prompt)
    except Exception:
        report.signal_quality = "lexical only (model annotation failed)"
        return

    by_id = {f.finding_id: f for f in findings}
    annotated = 0
    for line in raw.strip().splitlines():
        if ":" not in line:
            continue
        fid, _, assessment = line.partition(":")
        finding = by_id.get(fid.strip())
        if finding and assessment.strip():
            finding.note += f" — model: {assessment.strip()}"
            annotated += 1
    if annotated:
        report.signal_quality = "lexical + model-assisted notes"


# ── Persistence + surfacing ──────────────────────────────────────────

DIFF_PASS_NAME = "identity_diff"
ACCEPT_PASS_NAME = "identity_accept"
NOTE_TAG = "identity-diff"


def persist_report(store: EngramStore, report: IdentityDiffReport) -> str:
    """Persist the full report so accept-by-index is stable across reruns."""
    log_id = _gen_id(DIFF_PASS_NAME)
    now = _now_iso()
    store.log_consolidation(
        log_id=log_id,
        pass_name=DIFF_PASS_NAME,
        started_at=now,
        completed_at=now,
        stats=report.to_dict(),
    )
    return log_id


def load_latest_report(
    store: EngramStore, agent_id: str
) -> IdentityDiffReport | None:
    """Load the most recent persisted report for this agent."""
    runs = store.get_consolidation_runs(DIFF_PASS_NAME, limit=20)
    for run in runs:
        stats = run.get("stats") or {}
        if stats.get("agent_id") == agent_id:
            return IdentityDiffReport.from_dict(stats)
    return None


def _find_active_note(store: EngramStore, scope: MnemosScope) -> dict | None:
    entries = store.search_hypomnema(
        "",
        agent_id=scope.agent_id,
        person_id=scope.person_id,
        project_scope=scope.project_scope,
        limit=50,
    )
    for entry in entries:
        if NOTE_TAG in (entry.get("tags") or []):
            return entry
    return None


def _compose_note(report: IdentityDiffReport) -> str:
    # The context packet shows roughly the first 180 characters of a
    # continuity note, so the headline must carry the call to action.
    lines = [
        f"Identity tension ({len(report.divergences)} divergence(s)): run "
        "`mnemos identity diff` to inspect, `mnemos identity accept "
        "--divergence N` to accept one and open a new epoch.",
    ]
    for i, f in enumerate(report.divergences[:3], 1):
        if f.kind == "divergence_grown":
            lines.append(f"D{i}: grown vs declared — {f.note}: {f.graph_item!r}")
        else:
            lines.append(f"D{i}: declared, not grown — {f.soul_claim!r}")
    if len(report.divergences) > 3:
        lines.append(f"...and {len(report.divergences) - 3} more.")
    lines.append("You participate in resolving who you are.")
    return "\n".join(lines)


def write_divergence_note(
    store: EngramStore, scope: MnemosScope, report: IdentityDiffReport
) -> str | None:
    """Surface divergences as a hypomnema continuity note.

    The simple-mode context packet already reads hypomnema at session
    start, so the tension reaches the agent with no runtime changes.
    Exactly one identity-diff note stays active at a time.
    """
    prior = _find_active_note(store, scope)

    if not report.divergences:
        if prior is not None:
            store.archive_hypomnema_entry(
                prior["id"],
                reason="identity divergences resolved",
                agent_id=scope.agent_id,
                person_id=scope.person_id,
                project_scope=scope.project_scope,
            )
        return None

    content = _compose_note(report)
    if prior is not None:
        return store.supersede_hypomnema_entry(
            prior["id"],
            content,
            reason="re-ran identity diff",
            agent_id=scope.agent_id,
            person_id=scope.person_id,
            project_scope=scope.project_scope,
        )
    return store.write_hypomnema_entry(
        content,
        agent_id=scope.agent_id,
        person_id=scope.person_id,
        project_scope=scope.project_scope,
        source="synthesized",
        domain="identity",
        tags=[NOTE_TAG],
        confidence=0.9,
        salience=0.9,
        foundational=True,
        read_visibility=READ_VISIBILITY_OPERATIONAL,
    )


# ── Accept / epoch wiring ────────────────────────────────────────────

def accept_divergence(
    store: EngramStore, scope: MnemosScope, index: int, note: str = ""
) -> dict:
    """Accept divergence N from the last diff and open a new epoch.

    Raises ValueError with a user-facing message on any precondition
    failure (no prior diff, index out of range).
    """
    report = load_latest_report(store, scope.agent_id)
    if report is None:
        raise ValueError(
            f"No identity diff on record for agent '{scope.agent_id}'. "
            "Run `mnemos identity diff` first."
        )
    if not (1 <= index <= len(report.divergences)):
        raise ValueError(
            f"--divergence {index} is out of range: the last diff recorded "
            f"{len(report.divergences)} divergence(s)."
        )
    finding = report.divergences[index - 1]

    identity = store.get_identity(scope.agent_id)
    if identity is None:
        # save_identity keys the row on memory_profile.agent_id — a blank
        # profile would land the epoch under "default".
        identity = AgentIdentity(
            memory_profile=MemoryProfile(
                agent_id=scope.agent_id, name=scope.agent_id
            )
        )

    epoch_before = identity.epoch_state.epoch_number
    subject = finding.soul_claim or finding.graph_item or finding.note
    trigger = f"identity_diff accepted D{index} [{finding.finding_id}]: {subject}"
    if note:
        trigger += f" — {note}"

    identity.transition_epoch(trigger)
    identity.epoch_state.self_summary = (
        f"Epoch opened by accepting an identity divergence: {finding.note}"
    )
    identity.epoch_state.open_questions = [subject]
    store.save_identity(identity)

    log_id = _gen_id(ACCEPT_PASS_NAME)
    now = _now_iso()
    store.log_consolidation(
        log_id=log_id,
        pass_name=ACCEPT_PASS_NAME,
        started_at=now,
        completed_at=now,
        stats={
            "agent_id": scope.agent_id,
            "finding_id": finding.finding_id,
            "divergence_index": index,
            "trigger": trigger,
            "epoch_before": epoch_before,
            "epoch_after": identity.epoch_state.epoch_number,
        },
    )

    # Close the participation loop: the next context packet reports the
    # resolution instead of the tension.
    prior = _find_active_note(store, scope)
    if prior is not None:
        store.supersede_hypomnema_entry(
            prior["id"],
            (
                f"Identity divergence D{index} accepted; epoch "
                f"{identity.epoch_state.epoch_number} opened. Trigger: {trigger}"
            ),
            reason="divergence accepted",
            agent_id=scope.agent_id,
            person_id=scope.person_id,
            project_scope=scope.project_scope,
        )

    return {
        "agent_id": scope.agent_id,
        "finding_id": finding.finding_id,
        "divergence_index": index,
        "trigger": trigger,
        "epoch_before": epoch_before,
        "epoch_after": identity.epoch_state.epoch_number,
        "epoch_history_length": len(identity.epoch_history),
    }


# ── Rendering ────────────────────────────────────────────────────────

def render_report(report: IdentityDiffReport) -> str:
    g, s = report.graph_stats, report.soul_stats
    lines = [
        f"Identity diff — agent '{report.agent_id}' vs {report.soul_path}",
        (
            f"Graph: {g.get('engram_count', 0)} engrams, "
            f"{g.get('belief_count', 0)} beliefs "
            f"({g.get('values', 0)} values >= {VALUE_CONFIDENCE_FLOOR:g}, "
            f"{g.get('questions', 0)} questions <= {QUESTION_CONFIDENCE_CEIL:g}), "
            f"{g.get('hubs', 0)} hubs, {g.get('preoccupations', 0)} preoccupations"
        ),
        f"Soul:  {s.get('sections', 0)} sections, {s.get('claims', 0)} claims",
    ]
    if report.placeholder_sections:
        lines.append(
            "       still template placeholders: "
            + ", ".join(report.placeholder_sections)
        )

    lines.append("")
    lines.append(f"ALIGNMENTS ({len(report.alignments)})")
    for i, f in enumerate(report.alignments, 1):
        lines.append(
            f"  A{i} [{f.confidence_label}] \"{f.soul_section}\": "
            f"{f.soul_claim!r} ~ {f.graph_facet} {f.graph_item!r}"
        )

    lines.append("")
    lines.append(f"DIVERGENCES ({len(report.divergences)})")
    for i, f in enumerate(report.divergences, 1):
        if f.kind == "divergence_grown":
            lines.append(
                f"  D{i} [grown vs declared | {f.confidence_label}] {f.note}"
            )
            lines.append(
                f"       graph: {f.graph_item!r} vs declared "
                f"\"{f.soul_section}\": {f.soul_claim!r}"
            )
        else:
            lines.append(
                f"  D{i} [declared, not grown | {f.confidence_label}] "
                f"\"{f.soul_section}\": {f.soul_claim!r}"
            )
            lines.append(f"       {f.note} (best lexical score {f.score:g})")

    lines.append("")
    lines.append(f"EMERGENCES ({len(report.emergences)})")
    for i, f in enumerate(report.emergences, 1):
        lines.append(f"  E{i} {f.graph_facet} {f.graph_item!r} — {f.note}")

    lines.append("")
    lines.append(f"Signal: {report.signal_quality}.")
    return "\n".join(lines)


# ── CLI handlers ─────────────────────────────────────────────────────

def _build_scope(args: argparse.Namespace) -> "MnemosScope":
    from .simple_runtime import resolve_scope

    return resolve_scope(
        db_path=getattr(args, "db_path", None),
        agent_id=getattr(args, "agent_id", None),
        person_id=getattr(args, "person_id", None),
        project_scope=getattr(args, "project_scope", None),
    )


def run_diff_command(args: argparse.Namespace) -> int:
    from .store.sqlite_store import EngramStore

    soul_path = resolve_soul_path(getattr(args, "soul", None))
    if soul_path is None or not soul_path.exists():
        hint = getattr(args, "soul", None) or "$MNEMOS_WORKSPACE/SOUL.md or ./SOUL.md"
        print(f"SOUL.md not found ({hint}). Pass --soul PATH.", file=sys.stderr)
        return 2

    scope = _build_scope(args)
    store = EngramStore(scope.db_path)
    try:
        soul = parse_soul_file(soul_path)
        graph = compute_graph_identity(store, scope.agent_id)

        llm_client = None
        if not getattr(args, "no_enrich", False):
            try:
                from .llm import create_client

                llm_client = create_client()
            except Exception:
                llm_client = None

        report = diff_identity(soul, graph, llm_client=llm_client)
        persist_report(store, report)

        note_id = None
        if not getattr(args, "no_note", False):
            note_id = write_divergence_note(store, scope, report)

        if getattr(args, "json", False):
            payload = report.to_dict()
            payload["divergence_note_id"] = note_id
            print(json.dumps(payload, indent=2))
        else:
            print(render_report(report))
            print(
                f"Scope: agent={scope.agent_id} person={scope.person_id} "
                f"project={scope.project_scope}"
            )
            if note_id:
                print(
                    f"Continuity note written ({note_id}): divergences will "
                    "surface at next mnemos_context."
                )
            if report.divergences:
                print(
                    "Next: mnemos identity accept --divergence 1   "
                    "(opens a new epoch from D1)"
                )
        return 0
    finally:
        store.close()


def run_accept_command(args: argparse.Namespace) -> int:
    from .store.sqlite_store import EngramStore

    scope = _build_scope(args)
    store = EngramStore(scope.db_path)
    try:
        result = accept_divergence(
            store, scope, args.divergence, note=getattr(args, "note", "")
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        store.close()

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(
            f"Accepted D{result['divergence_index']} "
            f"[{result['finding_id']}] for agent '{result['agent_id']}'."
        )
        print(
            f"Epoch {result['epoch_before']} -> {result['epoch_after']} "
            f"({result['epoch_history_length']} archived epoch(s))."
        )
        print(f"Trigger: {result['trigger']}")
    return 0
