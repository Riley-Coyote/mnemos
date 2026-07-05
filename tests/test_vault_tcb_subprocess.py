"""Subprocess smoke test of scripts/mnemos-decide — the TCB's DB layer.

The twin test pins hash+chain; this executes the actual script David runs:
_pending_identity_proposals / _hydrate / _render / the decision loop / the
journal append. Env overrides work outside sudo, so we can drive it with
MNEMOS_DB_PATH + MNEMOS_VAULT_JOURNAL and stdin.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

from mnemos.store.sqlite_store import EngramStore
from mnemos.vault import journal as vj

_TCB = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "mnemos-decide"


def _fixture_store(tmp_path):
    store = EngramStore(tmp_path / "tcb.db")
    proposal = store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="install identity claim",
        domain="identity",
        blast_radius="identity",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        payload={"content": "I am Oliver. Sono l'agente di David."},
        provenance_ids=["soul-md-1"],
        proposal_id="prop-tcb-1",
    )
    return store, proposal


def _run_tcb(db_path, journal_path, stdin_text, args=()):
    # 008r/review: the TCB no longer honors MNEMOS_DB_PATH / MNEMOS_VAULT_JOURNAL
    # env (a redirectable writer). Tests inject via --db/--journal, which sudo
    # and launchd never pass.
    # 008r-review (tcb-recreates-missing-journal): the installer always creates
    # the journal (touch); the TCB now refuses an absent one. Mirror the
    # installed state so tests exercise the write path (an explicit
    # missing-journal test overrides this by deleting it).
    import pathlib as _pl
    _pl.Path(journal_path).parent.mkdir(parents=True, exist_ok=True)
    _pl.Path(journal_path).touch(exist_ok=True)
    return subprocess.run(
        [
            sys.executable, str(_TCB),
            "--db", str(db_path),
            "--journal", str(journal_path),
            *args,
        ],
        input=stdin_text,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )


def test_tcb_lists_and_hashes_matches_twin_on_skip(tmp_path):
    store, _ = _fixture_store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "s\n")
    assert result.returncode == 0, result.stderr
    # The pending identity proposal is listed.
    assert "prop-tcb-1" in result.stdout
    # The rendered content hash equals the twin's hash for the same row.
    expected = vj.canonical_content_sha256(store.get_proposal("prop-tcb-1"))
    printed = re.search(r"content SHA-256 \(computed live\): ([0-9a-f]{64})", result.stdout)
    assert printed is not None, result.stdout
    assert printed.group(1) == expected
    # Skip wrote nothing to the journal.
    assert not journal.exists() or journal.read_text() == ""


def test_tcb_approve_appends_chain_verifiable_line(tmp_path):
    store, _ = _fixture_store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "a\n")
    assert result.returncode == 0, result.stderr
    lines = vj.read_journal(journal)
    assert len(lines) == 1
    ok, brk = vj.verify_chain(lines)
    assert ok, brk
    assert lines[0]["decision"] == "approved"
    assert lines[0]["proposal_id"] == "prop-tcb-1"
    assert lines[0]["content_sha256"] == vj.canonical_content_sha256(
        store.get_proposal("prop-tcb-1")
    )


def test_tcb_witness_legacy_appends_and_agent_stamps(tmp_path):
    """End-to-end DAVID-9(c): the real script witnesses a legacy row, and the
    agent-side apply_legacy_witness stamps it operational."""
    store = EngramStore(tmp_path / "tcb.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility) VALUES"
        " ('legacy-b', 'oliver', 'I am Oliver.', 0.9, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context')"
    )
    conn.commit()
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "y\n", args=["--witness-legacy"])
    assert result.returncode == 0, result.stderr
    lines = vj.read_journal(journal)
    assert len(lines) == 1
    assert lines[0]["witness"] == "legacy"
    assert lines[0]["table"] == "beliefs"
    ok, brk = vj.verify_chain(lines)
    assert ok, brk
    # Agent stamps from the witness lines. 008r: inject the fixture journal via
    # the resolver seam — production apply has no _journal_path_override param.
    from mnemos.store import sqlite_store as _sq
    _prev_resolve = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        res = store.apply_legacy_witness()
    finally:
        _sq.resolve_vault_journal_path = _prev_resolve
    assert res["stamped"] == ["legacy-b"]
    stamped = EngramStore(store.db_path, vault_active=True)
    assert any(b.content == "I am Oliver." for b in stamped.get_beliefs("oliver"))


def test_tcb_ignores_env_journal_and_db_redirect(tmp_path):
    """008r/review (redirectable-tcb-journal-path): the TCB must NOT honor
    MNEMOS_VAULT_JOURNAL / MNEMOS_DB_PATH from the environment. A redirected
    writer would print 'Recorded' while appending to a noncanonical journal
    that apply/reconcile/watchdog never treat as ground truth. The --journal /
    --db flags win; env is ignored — the writer has no redirectable path."""
    store = EngramStore(tmp_path / "tcb.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility) VALUES"
        " ('legacy-b', 'oliver', 'I am Oliver.', 0.9, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context')"
    )
    conn.commit()
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")  # installed journal (008r-review G)
    fake = tmp_path / "fake.jsonl"
    result = subprocess.run(
        [
            sys.executable, str(_TCB),
            "--db", str(store.db_path),
            "--journal", str(real),
            "--witness-legacy",
        ],
        input="y\n",
        capture_output=True,
        text=True,
        env={
            # env tries to redirect BOTH paths to fakes; the flags must win.
            "MNEMOS_VAULT_JOURNAL": str(fake),
            "MNEMOS_DB_PATH": str(tmp_path / "wrong.db"),
            "PATH": "/usr/bin:/bin",
        },
    )
    assert result.returncode == 0, result.stderr
    lines = vj.read_journal(real)
    assert len(lines) == 1 and lines[0]["witness"] == "legacy", (
        "witness line did not land in the --journal path — flag ignored"
    )
    assert not fake.exists(), (
        "TCB honored MNEMOS_VAULT_JOURNAL env — the writer is redirectable"
    )


def test_tcb_refuses_redirect_flags_under_sudo(tmp_path):
    """008r/review (sudoers-allows-tcb-redirect-args): --db/--journal are
    tests-only. Under sudo (SUDO_USER set — every real ceremony invocation) the
    TCB must REFUSE them, so a redirect flag can never reach production even if
    the sudoers rule were mis-scoped. (--db points at a tmp DB so DB_PATH never
    resolves to David's real ~/.mnemos.)"""
    store = EngramStore(tmp_path / "tcb.db")
    real = tmp_path / "real.jsonl"
    result = subprocess.run(
        [
            sys.executable, str(_TCB),
            "--db", str(store.db_path),
            "--journal", str(real),
            "--witness-legacy",
        ],
        input="y\n",
        capture_output=True,
        text=True,
        env={"SUDO_USER": "davidef", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 2, (
        f"TCB did not refuse redirect flags under sudo (rc={result.returncode})"
    )
    assert "tests-only" in result.stderr.lower(), result.stderr
    assert not real.exists(), "TCB wrote despite refusing the redirect under sudo"


def test_tcb_refuses_missing_journal(tmp_path):
    """008r-review (tcb-recreates-missing-journal): if the installed journal is
    absent (deleted or replaced), the TCB must REFUSE — appending would silently
    start a fresh genesis chain and erase the prior audit history. It must NOT
    create the file. (Direct subprocess: does not use _run_tcb, which touches
    the journal to mirror an installed vault.)"""
    store = EngramStore(tmp_path / "tcb.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility) VALUES"
        " ('legacy-b', 'oliver', 'I am Oliver.', 0.9, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context')"
    )
    conn.commit()
    missing = tmp_path / "deleted.jsonl"  # never created
    result = subprocess.run(
        [
            sys.executable, str(_TCB),
            "--db", str(store.db_path),
            "--journal", str(missing),
            "--witness-legacy",
        ],
        input="y\n",
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 2, (
        f"TCB did not refuse a missing journal (rc={result.returncode})"
    )
    assert "MISSING" in result.stderr, result.stderr
    assert not missing.exists(), (
        "TCB created the journal instead of refusing — the audit chain would "
        "silently restart"
    )
