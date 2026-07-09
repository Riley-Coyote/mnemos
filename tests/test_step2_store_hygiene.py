"""Step 2 store-hygiene regression guards."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


IMPLICIT_STORE_ENTRYPOINTS = (
    "mnemos/cli.py",
    "mnemos/mcp_server.py",
    "mnemos/bridge.py",
    "mnemos/importer/operator.py",
    "mnemos/importer/watcher.py",
    "mnemos/substrate/config.py",
    "mnemos/substrate/tick.py",
    "mnemos/substrate/handlers/dreaming.py",
    "mnemos/substrate/handlers/initiation.py",
    "mnemos/substrate/handlers/wandering.py",
    "mnemos/substrate/modulators.py",
    "mnemos/indexer/claude_code_adapter.py",
    "mnemos/indexer/session_indexer.py",
    "mnemos/setup/bootstrap.py",
    "mnemos/simple_mcp.py",
    "mnemos/simple_runtime.py",
    "mnemos/identity_diff.py",
    "mnemos/inner_life/preflight.py",
    "mnemos/inner_life/scheduler.py",
    "mnemos/soak/preflight.py",
    "mnemos/soak/tick.py",
    "mnemos/visualization/data.py",
    "mnemos/visualization/app.py",
    "benchmarks/retrieval_benchmark.py",
)

APPROVED_CANONICAL_SENTINELS = {
    ("mnemos/importer/operator.py", "DEFAULT_LIVE_DB_PATH"),
}


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _string_literals(node: ast.AST) -> list[str]:
    values: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            values.append(child.value)
    return values


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for elt in node.elts:
            names.update(_target_names(elt))
        return names
    return set()


def _mentions_canonical_store(strings: list[str]) -> bool:
    joined = "/".join(strings)
    return "memory.db" in joined and (".mnemos" in joined or "~" in joined)


def _rogue_path_nodes(tree: ast.AST, relpath: str) -> list[str]:
    rogue: list[str] = []
    approved_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = set().union(*(_target_names(target) for target in node.targets))
            if any(
                (relpath, target) in APPROVED_CANONICAL_SENTINELS for target in targets
            ):
                approved_lines.add(node.lineno)
        if isinstance(node, ast.AnnAssign):
            targets = _target_names(node.target)
            if any(
                (relpath, target) in APPROVED_CANONICAL_SENTINELS for target in targets
            ):
                approved_lines.add(node.lineno)

    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in approved_lines:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None and _mentions_canonical_store(
                    _string_literals(default)
                ):
                    rogue.append(f"{relpath}:{node.lineno} default")

        if isinstance(node, ast.Assign) and _mentions_canonical_store(
            _string_literals(node.value) if node.value is not None else []
        ):
            rogue.append(f"{relpath}:{node.lineno} assignment")

        if isinstance(node, ast.AnnAssign):
            strings = _string_literals(node.value) if node.value is not None else []
            if _mentions_canonical_store(strings):
                rogue.append(f"{relpath}:{node.lineno} annotated assignment")

        if isinstance(node, ast.Call):
            func = _name_of(node.func)
            strings = _string_literals(node)
            if func.endswith("sqlite3.connect") and _mentions_canonical_store(strings):
                rogue.append(f"{relpath}:{node.lineno} sqlite3.connect")
            if func.endswith("Path") and _mentions_canonical_store(strings):
                rogue.append(f"{relpath}:{node.lineno} Path constructor")
            if func.endswith("EngramStore") and _mentions_canonical_store(strings):
                rogue.append(f"{relpath}:{node.lineno} EngramStore constructor")
            if func.endswith("expanduser") and _mentions_canonical_store(strings):
                rogue.append(f"{relpath}:{node.lineno} expanduser constructor")
            if (
                func == "os.environ.get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "MNEMOS_DB_PATH"
                and len(node.args) > 1
                and _mentions_canonical_store(_string_literals(node.args[1]))
            ):
                rogue.append(f"{relpath}:{node.lineno} MNEMOS_DB_PATH fallback")

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            strings = _string_literals(node)
            if "memory.db" in strings and any(".mnemos" in s for s in strings):
                rogue.append(f"{relpath}:{node.lineno} Path / memory.db")

    return rogue


def test_implicit_store_entrypoints_do_not_construct_canonical_paths():
    """Mutation guard: adding Path(...)/memory.db outside resolve_scope goes red."""

    offenders: list[str] = []
    for relpath in IMPLICIT_STORE_ENTRYPOINTS:
        tree = ast.parse((REPO_ROOT / relpath).read_text(encoding="utf-8"))
        offenders.extend(_rogue_path_nodes(tree, relpath))
    assert offenders == []


def test_source_type_references_resolve_to_real_enum_members():
    """Mutation guard: SourceType.USER_EXPLICIT fails before runtime."""

    from mnemos.core.types import SourceType

    missing: list[str] = []
    for path in (REPO_ROOT / "mnemos").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "SourceType"
                and node.attr not in SourceType.__members__
            ):
                missing.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.attr}"
                )
    assert missing == []


def test_cli_and_mcp_inspect_label_engram_strength_as_s0(tmp_path, monkeypatch, capsys):
    from mnemos import mcp_server
    from mnemos.cli import main
    from mnemos.core.engram import Engram
    from mnemos.store.sqlite_store import EngramStore

    db = tmp_path / "s0-labels.db"
    store = EngramStore(db)
    engram = Engram(content="S0 label proof")
    store.save_engram(engram)
    store.close()

    assert main(["--db-path", str(db), "inspect", engram.id]) == 0
    out = capsys.readouterr().out
    assert "S0 (initial, frozen at encoding):" in out
    assert "Strength:" not in out

    mcp_store = EngramStore(db)
    monkeypatch.setattr(mcp_server, "_store", mcp_store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: None)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    try:
        mcp_out = mcp_server.mnemos_inspect(engram.id)
    finally:
        mcp_store.close()
    assert "S0 (initial, frozen at encoding):" in mcp_out
    assert "Strength:" not in mcp_out


def test_visualization_surfaces_label_engram_strength_as_s0():
    app = (REPO_ROOT / "mnemos/visualization/app.py").read_text(encoding="utf-8")
    data = (REPO_ROOT / "mnemos/visualization/data.py").read_text(encoding="utf-8")
    assert "S0 (initial, frozen)" in app
    assert "S0 distribution" in app
    assert 'project-memory-meta">S0' in app
    assert "strength distribution" not in app
    assert "S0 > 0.70" in data
    assert "S0 0.40-0.70" in data
    assert "S0 < 0.40" in data
    for legacy in ("strong (>0.7)", "moderate (0.4-0.7)", "weak (<0.4)"):
        assert legacy not in data


def test_s0_strength_has_no_post_encoding_mutators():
    forbidden = {
        "mnemos/retrieval/reconsolidation.py": ["engram.strength ="],
        "mnemos/consolidation/decay.py": ["new_strength", "engram.strength ="],
        "mnemos/substrate/tick.py": [
            "strength = MAX",
            "accessibility * strength",
        ],
        "mnemos/consolidation/softening.py": ["candidate.strength ="],
        "mnemos/encoding/encoder.py": ["engram.strength ="],
    }
    for relpath, needles in forbidden.items():
        text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, f"{relpath} still mutates frozen S0"


def test_reconsolidation_preserves_s0_strength(store):
    from mnemos.core.engram import Engram
    from mnemos.retrieval.reconsolidation import reconsolidate

    engram = Engram(content="S0 should survive retrieval.", strength=0.42)
    store.save_engram(engram)

    updated = reconsolidate(
        engram=store.get_engram(engram.id),
        current_context="retrieval",
        co_retrieved_ids=[],
        store=store,
    )

    assert updated.strength == pytest.approx(0.42)
    assert updated.stability > engram.stability
    assert store.get_engram(engram.id).strength == pytest.approx(0.42)


def test_engram_status_keyword_only_preserves_positional_strength_abi():
    from mnemos.core.engram import EncodingContext, Engram

    engram = Engram(
        "stable-id",
        "2026-01-01T00:00:00+00:00",
        "Current content.",
        1.0,
        "Encoded content.",
        "Impact.",
        EncodingContext(),
        "episodic",
        ["tag"],
        ["schema"],
        0.73,
    )

    assert engram.status is None
    assert engram.strength == pytest.approx(0.73)


def test_bridge_default_agent_stays_default_while_db_uses_scope(tmp_path, monkeypatch):
    from mnemos.bridge import MnemosBridge

    configured = tmp_path / "configured.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MNEMOS_STORE_DB_PATH", str(configured))
    monkeypatch.delenv("MNEMOS_AGENT_ID", raising=False)
    bridge = MnemosBridge()
    assert bridge.agent_id == "default"
    assert bridge.db_path == str(configured)


def test_advanced_server_default_scope_stays_legacy_sentinels(tmp_path, monkeypatch):
    from mnemos import mcp_server

    configured = tmp_path / "advanced.db"
    defaults = {}
    runtime = {}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MNEMOS_STORE_DB_PATH", str(configured))
    monkeypatch.delenv("MNEMOS_AGENT_ID", raising=False)
    monkeypatch.delenv("MNEMOS_PERSON_ID", raising=False)
    monkeypatch.delenv("MNEMOS_PROJECT_SCOPE", raising=False)
    monkeypatch.setattr(mcp_server, "_get_config", lambda: {})
    monkeypatch.setattr(
        mcp_server,
        "_set_server_defaults",
        lambda agent_id, person_id, project_scope: defaults.update(
            {
                "agent_id": agent_id,
                "person_id": person_id,
                "project_scope": project_scope,
            }
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "configure_runtime",
        lambda **kwargs: runtime.update(kwargs),
    )
    monkeypatch.setattr(mcp_server, "_init_store", lambda db_path=None: None)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: None)

    mcp_server.run_server()

    expected_scope = {
        "agent_id": "default",
        "person_id": "user",
        "project_scope": "global",
    }
    assert defaults == expected_scope
    assert runtime == {"db_path": str(configured), **expected_scope}


def test_advanced_server_explicit_scope_overrides_cached_config(tmp_path, monkeypatch):
    from mnemos import mcp_server

    configured = tmp_path / "advanced.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MNEMOS_STORE_DB_PATH", str(configured))
    monkeypatch.delenv("MNEMOS_AGENT_ID", raising=False)
    monkeypatch.delenv("MNEMOS_PERSON_ID", raising=False)
    monkeypatch.delenv("MNEMOS_PROJECT_SCOPE", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "load_config",
        lambda: {
            "agent_id": "from-config",
            "person_id": "config-user",
            "project_scope": "config-project",
        },
    )
    monkeypatch.setattr(mcp_server, "_config", None)
    monkeypatch.setattr(mcp_server, "configure_runtime", lambda **_kwargs: None)
    monkeypatch.setattr(mcp_server, "_init_store", lambda db_path=None: None)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: None)

    mcp_server.run_server(
        agent_id="explicit-agent",
        person_id="explicit-person",
        project_scope="explicit-project",
    )

    assert mcp_server._effective_scope() == (
        "explicit-agent",
        "explicit-person",
        "explicit-project",
    )


def test_advanced_serve_cli_preserves_configured_agent_without_explicit_agent(
    tmp_path, monkeypatch
):
    import json

    from mnemos import cli, mcp_server

    captured = {}
    home = tmp_path / "home"
    db = tmp_path / "configured.db"
    config_dir = home / ".mnemos"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "agent_id": "from-config",
                "person_id": "config-user",
                "project_scope": "config-project",
                "store": {"db_path": str(db)},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MNEMOS_AGENT_ID", raising=False)
    monkeypatch.delenv("MNEMOS_PERSON_ID", raising=False)
    monkeypatch.delenv("MNEMOS_PROJECT_SCOPE", raising=False)
    monkeypatch.delenv("MNEMOS_DB_PATH", raising=False)
    monkeypatch.delenv("MNEMOS_STORE_DB_PATH", raising=False)
    monkeypatch.setattr(
        mcp_server, "run_server", lambda **kwargs: captured.update(kwargs)
    )

    assert cli.main(["serve", "--mode", "advanced"]) == 0

    assert captured == {
        "db_path": str(db),
        "agent_id": None,
        "person_id": None,
        "project_scope": None,
    }


def test_doctor_reports_schema_version_and_pending_migration_count(tmp_path, capsys):
    from mnemos.cli import main
    from mnemos.store.sqlite_store import EngramStore

    db = tmp_path / "doctor.db"
    EngramStore(db).close()
    assert main(["--db-path", str(db), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "Schema version:" in out
    assert "Pending migrations:" in out


def test_doctor_reads_migration_status_before_context_initializes_store(
    tmp_path, monkeypatch, capsys
):
    import types

    from mnemos import cli, simple_runtime

    events: list[str] = []
    db = tmp_path / "doctor-order.db"
    scope = types.SimpleNamespace(
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        db_path=str(db),
    )

    class FakeRuntime:
        def __init__(self, **_kwargs):
            self.scope = scope
            self.db_path = db
            self.has_dedicated_model = False

        def context(self):
            events.append("context")
            return "fake packet"

        def close(self):
            pass

    def fake_status(path):
        assert path == db
        events.append("migration-status")
        return ("12", "1")

    monkeypatch.setattr(simple_runtime, "MnemosRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "_migration_doctor_status", fake_status)
    monkeypatch.setattr(cli, "_mcp_available", lambda: False)
    monkeypatch.setattr(cli, "_print_affinity_status", lambda: None)
    assert cli.main(["doctor", "--db-path", str(db)]) == 0
    assert events[:2] == ["migration-status", "context"]
    out = capsys.readouterr().out
    assert "Schema version: 12" in out
    assert "Pending migrations: 1" in out


def test_doctor_fails_archived_open_prospective_rows(tmp_path, capsys):
    from mnemos.cli import main
    from mnemos.core.engram import Engram
    from mnemos.store.sqlite_store import EngramStore

    db = tmp_path / "doctor-prospective.db"
    store = EngramStore(db)
    try:
        want = Engram(content="Do not hide this open want.", kind="prospective")
        store.save_engram(want)
        store._get_conn().execute(
            "UPDATE engrams SET state = 'archived' WHERE id = ?",
            (want.id,),
        )
        store._get_conn().commit()
    finally:
        store.close()

    assert main(["--db-path", str(db), "doctor"]) == 1
    out = capsys.readouterr().out
    assert f"FAIL: archived open prospective engram: {want.id}" in out


def test_setup_seed_encode_failures_are_counted(monkeypatch, store):
    pytest.importorskip("mcp.server.fastmcp")
    from mnemos import mcp_server

    class FailingEncoder:
        def encode(self, **_kwargs):
            raise ValueError("forced setup encode failure")

    config = {
        "setup_step": 3,
        "agent_id": "vektor",
        "person_id": "riley",
        "agent_name": "Vektor",
        "user_name": "Riley",
    }
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_encoder", FailingEncoder())
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "save_config", lambda updated: None)
    monkeypatch.setattr(mcp_server, "_config_invalidate", lambda: None)

    out = mcp_server.mnemos_setup(
        "Riley is a careful collaborator who values evidence."
    )

    assert "Encoded 0 seed memories." in out
    assert store.instrumentation_failure_counts()["setup_seed_encode"] >= 1


def test_prospective_capture_defaults_open_and_nonprospective_status_fails(store):
    from mnemos.core.engram import Engram

    want = Engram(content="Remember to close the loop.", kind="prospective")
    store.save_engram(want)
    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded is not None
    assert loaded.status == "open"

    with pytest.raises(ValueError, match="only valid for prospective"):
        Engram(content="Semantic rows cannot carry goal status.", status="open")


def test_prospective_transition_is_receipted_and_terminal(store):
    from mnemos.core.engram import Engram

    want = Engram(content="Send the evidence report.", kind="prospective")
    store.save_engram(want)

    result = store.transition_prospective_status(
        want.id,
        "fulfilled",
        actor="oliver",
        runtime="pytest",
        session_id="step2",
        reason="done",
    )

    assert result["from_status"] == "open"
    assert result["to_status"] == "fulfilled"
    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded is not None
    assert loaded.status == "fulfilled"
    assert loaded.state == "archived"
    assert [hit.id for hit in store.search_fts("evidence", read_visibility=None)] == []
    archive_hits = store.search_archive("evidence", read_visibility=None)
    assert archive_hits[0]["id"] == want.id
    assert archive_hits[0]["archive_reason"] == "prospective_status:fulfilled"
    receipts = store.get_runtime_receipts(kind="prospective-status-transition")
    assert len(receipts) == 1
    assert receipts[0]["engram_refs"] == [want.id]
    assert receipts[0]["payload"]["to_status"] == "fulfilled"

    with pytest.raises(ValueError, match="already terminal"):
        store.transition_prospective_status(
            want.id,
            "retired",
            actor="oliver",
            runtime="pytest",
        )
    with pytest.raises(ValueError, match="cannot be mutated"):
        store.save_engram(
            Engram(
                id=want.id,
                content="Retargeted terminal row.",
                kind="prospective",
                status="fulfilled",
            )
        )
    with pytest.raises(ValueError, match="do not reopen"):
        store.save_engram(
            Engram(id=want.id, content=want.content, kind="prospective", status="open")
        )


@pytest.mark.parametrize("state", ["archived", "dormant"])
def test_direct_save_rejects_open_prospective_lifecycle_state_changes(store, state):
    from mnemos.core.engram import Engram

    want = Engram(content="Do not hide an open want.", kind="prospective")
    store.save_engram(want)
    stale = store.get_engram(want.id, read_visibility=None)
    assert stale is not None
    stale.state = state

    with pytest.raises(ValueError, match="transition_prospective_status"):
        store.save_engram(stale)

    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded is not None
    assert loaded.status == "open"
    assert loaded.state == "active"
    assert store.search_archive("open want", read_visibility=None) == []
    assert store.get_runtime_receipts(kind="prospective-status-transition") == []

    result = store.transition_prospective_status(
        want.id,
        "retired",
        actor="oliver",
        runtime="pytest",
        session_id="step2",
        reason="operator close",
    )
    assert result["to_status"] == "retired"
    transitioned = store.get_engram(want.id, read_visibility=None)
    assert transitioned is not None
    assert transitioned.status == "retired"
    assert transitioned.state == "archived"


def test_direct_save_allows_open_prospective_content_edits(store):
    from mnemos.core.engram import Engram

    want = Engram(
        content="Keep this open want editable.",
        kind="prospective",
        tags=["initial"],
    )
    store.save_engram(want)
    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded is not None
    loaded.content = "Keep this open want editable with clearer wording."
    loaded.tags = ["initial", "edited"]
    store.save_engram(loaded)

    updated = store.get_engram(want.id, read_visibility=None)
    assert updated is not None
    assert updated.content == "Keep this open want editable with clearer wording."
    assert updated.tags == ["initial", "edited"]
    assert updated.status == "open"
    assert updated.state == "active"
    assert store.get_runtime_receipts(kind="prospective-status-transition") == []


def test_prospective_transition_archives_linked_hypomnema(store):
    from mnemos.core.engram import Engram

    want = Engram(content="Send the linked evidence report.", kind="prospective")
    store.save_engram(want)
    entry_id = store.write_hypomnema_entry(
        "Send the linked evidence report before Friday.",
        related_engram_id=want.id,
    )
    assert [hit["id"] for hit in store.search_hypomnema("linked evidence")] == [
        entry_id
    ]

    store.transition_prospective_status(
        want.id,
        "fulfilled",
        actor="oliver",
        runtime="pytest",
        session_id="step2",
        reason="done",
    )

    assert store.search_hypomnema("linked evidence", read_visibility=None) == []
    archived = store.get_hypomnema_entry(
        entry_id,
        active_only=False,
        read_visibility=None,
    )
    assert archived is not None
    assert archived["active"] is False
    assert archived["revision_count"] == 1
    assert (
        archived["revisions"][-1]["reason"] == "archived: prospective_status:fulfilled"
    )
    receipts = store.get_runtime_receipts(kind="prospective-status-transition")
    assert receipts[0]["payload"]["archived_hypomnema_ids"] == [entry_id]


def test_generic_archive_rejects_open_prospective_without_receipt(store):
    from mnemos.core.engram import Engram

    want = Engram(content="Do not archive wants silently.", kind="prospective")
    store.save_engram(want)

    with pytest.raises(ValueError, match="transition_prospective_status"):
        store.archive_engram(want, reason="user_requested")

    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded.status == "open"
    assert loaded.state == "active"
    assert store.get_runtime_receipts(kind="prospective-status-transition") == []


def test_generic_archive_rejects_stale_object_for_stored_prospective(store):
    from mnemos.core.engram import Engram

    want = Engram(content="Do not archive stored wants silently.", kind="prospective")
    store.save_engram(want)
    stale = Engram(
        id=want.id,
        content="Stale non-prospective view.",
        kind="semantic",
    )

    with pytest.raises(ValueError, match="transition_prospective_status"):
        store.archive_engram(stale, reason="user_requested")

    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded.status == "open"
    assert loaded.state == "active"
    assert store.search_archive("stored wants", read_visibility=None) == []
    assert store.get_runtime_receipts(kind="prospective-status-transition") == []


def test_decay_does_not_archive_open_prospective_without_receipt(store):
    from datetime import datetime, timedelta, timezone

    from mnemos.consolidation.decay import run_decay_pass
    from mnemos.core.engram import Engram

    want = Engram(
        content="An old want still needs an explicit close.",
        kind="prospective",
        accessibility=0.001,
    )
    want.last_accessed = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store.save_engram(want)

    stats = run_decay_pass(
        store,
        {"decay_rate": 1.0, "archive_threshold": 0.01, "dormant_threshold": 0.0},
    )

    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded.status == "open"
    assert loaded.state == "active"
    assert loaded.accessibility == pytest.approx(0.001)
    assert stats["engrams_processed"] == 0
    assert stats["engrams_decayed"] == 0
    assert stats["engrams_archived"] == 0
    assert store.get_runtime_receipts(kind="prospective-status-transition") == []


def test_decay_does_not_make_open_prospective_dormant_without_receipt(store):
    from datetime import datetime, timedelta, timezone

    from mnemos.consolidation.decay import run_decay_pass
    from mnemos.core.engram import Engram

    want = Engram(
        content="A visible want should not go dormant silently.",
        kind="prospective",
        accessibility=0.04,
    )
    want.last_accessed = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store.save_engram(want)

    stats = run_decay_pass(
        store,
        {"decay_rate": 0.0, "archive_threshold": 0.01, "dormant_threshold": 0.05},
    )

    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded.status == "open"
    assert loaded.state == "active"
    assert loaded.accessibility == pytest.approx(0.04)
    assert stats["engrams_processed"] == 0
    assert stats["engrams_decayed"] == 0
    assert stats["engrams_dormant"] == 0
    assert stats["engrams_archived"] == 0
    assert store.get_runtime_receipts(kind="prospective-status-transition") == []


def test_deep_softening_does_not_mutate_open_prospective_without_receipt(store):
    from mnemos.consolidation.softening import run_softening_pass
    from mnemos.core.engram import Engram

    want = Engram(
        content=(
            "A future-directed want should remain sharp and visible until "
            "an explicit prospective status transition closes it."
        ),
        kind="prospective",
        accessibility=0.05,
        resolution=1.0,
    )
    store.save_engram(want)

    stats = run_softening_pass(store, {}, None, agent_id="default")

    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded.status == "open"
    assert loaded.state == "active"
    assert loaded.content == want.content
    assert loaded.resolution == pytest.approx(1.0)
    assert loaded.impact == ""
    assert loaded.versions == []
    assert stats["engrams_evaluated"] == 0
    assert stats["engrams_softened"] == 0
    assert store.get_runtime_receipts(kind="prospective-status-transition") == []


def test_softening_lesson_reinforcement_skips_open_prospective(store):
    from mnemos.consolidation.softening import run_softening_pass
    from mnemos.core.engram import Engram

    open_lesson = Engram(
        content="Planning evidence closure teaches explicit status transitions.",
        kind="prospective",
        tags=["lesson"],
        stability=0.2,
    )
    fading = Engram(
        content=(
            "Planning evidence closure teaches explicit status transitions. "
            "The memory should soften into a procedural lesson."
        ),
        accessibility=0.05,
        resolution=1.0,
        impact="Planning evidence closure teaches explicit status transitions.",
    )
    store.save_engram(open_lesson)
    store.save_engram(fading)

    stats = run_softening_pass(
        store,
        {"minimum_resolution": 0.1},
        None,
        agent_id="default",
    )

    loaded_open_lesson = store.get_engram(open_lesson.id, read_visibility=None)
    assert loaded_open_lesson.status == "open"
    assert loaded_open_lesson.state == "active"
    assert loaded_open_lesson.stability == pytest.approx(0.2)
    assert loaded_open_lesson.access_count == 0
    assert stats["lessons_reinforced"] == 0
    assert stats["lessons_created"] == 1


def test_substrate_tick_does_not_decay_or_soften_open_prospective(
    tmp_path, monkeypatch
):
    from mnemos.core.engram import Engram
    from mnemos.store.sqlite_store import EngramStore
    from mnemos.substrate.config import SubstrateConfig
    from mnemos.substrate.events import EventType
    from mnemos.substrate.tick import Substrate

    db = tmp_path / "tick-prospective.db"
    store = EngramStore(db)
    want = Engram(
        content="Keep this want visible until explicitly closed.",
        kind="prospective",
        accessibility=0.14,
    )
    ordinary = Engram(
        content="Ordinary memory can decay.",
        accessibility=0.8,
    )
    store.save_engram(want)
    store.save_engram(ordinary)
    store.close()
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")

    substrate = Substrate(
        SubstrateConfig(
            agent_id="default",
            db_path=str(db),
            log_dir=str(tmp_path / "logs"),
            decay_rate=0.02,
            silence_threshold_hours=999999,
        )
    )
    try:
        summary: dict = {}
        events = substrate._consolidate(summary)
    finally:
        substrate.store.close()

    reopened = EngramStore(db)
    try:
        loaded_want = reopened.get_engram(want.id, read_visibility=None)
        loaded_ordinary = reopened.get_engram(ordinary.id, read_visibility=None)
    finally:
        reopened.close()

    assert loaded_want.status == "open"
    assert loaded_want.state == "active"
    assert loaded_want.accessibility == pytest.approx(0.14)
    assert loaded_ordinary.accessibility == pytest.approx(0.78)
    assert summary["engrams_decayed"] == 1
    softened_ids = [
        event.payload.get("engram_id")
        for event in events
        if event.event_type == EventType.MEMORY_SOFTENED
    ]
    assert want.id not in softened_ids


def test_mcp_forget_routes_open_prospective_to_status_transition(store, monkeypatch):
    from mnemos import mcp_server
    from mnemos.core.engram import Engram

    want = Engram(content="Close me explicitly.", kind="prospective")
    store.save_engram(want)
    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: None)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)

    out = mcp_server.mnemos_forget(want.id)

    assert "Prospective memory is still open" in out
    assert "mnemos prospective status" in out
    loaded = store.get_engram(want.id, read_visibility=None)
    assert loaded.status == "open"
    assert loaded.state == "active"


def test_prospective_direct_upsert_cannot_erase_status(store):
    from mnemos.core.engram import Engram

    want = Engram(content="Keep the prospective boundary.", kind="prospective")
    store.save_engram(want)

    with pytest.raises(ValueError, match="cannot change kind"):
        store.save_engram(Engram(id=want.id, content=want.content, kind="semantic"))


def test_cli_prospective_receipt_actor_uses_resolved_config(
    tmp_path, monkeypatch, capsys
):
    import json

    from mnemos.cli import main
    from mnemos.core.engram import Engram
    from mnemos.store.sqlite_store import EngramStore

    home = tmp_path / "home"
    config_dir = home / ".mnemos"
    db = tmp_path / "configured.db"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"agent_id": "oliver", "store": {"db_path": str(db)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MNEMOS_AGENT_ID", raising=False)
    monkeypatch.delenv("MNEMOS_DB_PATH", raising=False)
    monkeypatch.delenv("MNEMOS_STORE_DB_PATH", raising=False)

    store = EngramStore(db)
    want = Engram(content="Receipt actor comes from config.", kind="prospective")
    store.save_engram(want)
    store.close()

    assert main(["prospective", "status", want.id, "fulfilled"]) == 0
    out = capsys.readouterr().out
    assert "Prospective status updated" in out

    reopened = EngramStore(db)
    try:
        receipts = reopened.get_runtime_receipts(kind="prospective-status-transition")
    finally:
        reopened.close()
    assert receipts[0]["actor"] == "oliver"


def test_cli_prospective_receipt_actor_uses_legacy_default_without_config(
    tmp_path, monkeypatch, capsys
):
    from mnemos.cli import main
    from mnemos.core.engram import Engram
    from mnemos.store.sqlite_store import EngramStore

    home = tmp_path / "home"
    db = tmp_path / "default.db"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MNEMOS_AGENT_ID", raising=False)
    monkeypatch.delenv("MNEMOS_DB_PATH", raising=False)
    monkeypatch.delenv("MNEMOS_STORE_DB_PATH", raising=False)

    store = EngramStore(db)
    want = Engram(
        content="Receipt actor defaults to legacy CLI default.", kind="prospective"
    )
    store.save_engram(want)
    store.close()

    assert (
        main(["--db-path", str(db), "prospective", "status", want.id, "fulfilled"]) == 0
    )
    out = capsys.readouterr().out
    assert "Prospective status updated" in out

    reopened = EngramStore(db)
    try:
        receipts = reopened.get_runtime_receipts(kind="prospective-status-transition")
    finally:
        reopened.close()
    assert receipts[0]["actor"] == "default"


def test_prospective_status_migration_lints():
    from mnemos.store.migration_runner import lint_migration_sql

    sql = (REPO_ROOT / "mnemos/store/migrations/0013_prospective_status.sql").read_text(
        encoding="utf-8"
    )
    assert lint_migration_sql(sql) == ["ALTER TABLE ADD COLUMN"]
