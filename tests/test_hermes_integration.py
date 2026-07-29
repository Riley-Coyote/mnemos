"""Tests for the Hermes memory-provider integration."""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from mnemos.integrations.hermes import HermesMnemosConfig, MnemosMemoryProviderCore
from mnemos.integrations.hermes.installer import (
    build_diagnostics,
    install_hermes_plugin,
    provider_plugin_dirs,
    quickstart_hermes,
)


def _new_provider(tmp_path, *, agent_id="coder", person_id="riley", project_scope="mnemos"):
    return MnemosMemoryProviderCore(
        HermesMnemosConfig(
            db_path=str(tmp_path / "mnemos.db"),
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            auto_bootstrap=False,
            deep_maintenance=False,
        )
    )


def _install_yaml_stub(monkeypatch):
    yaml_stub = types.ModuleType("yaml")

    def safe_load(text):
        raw = str(text or "")
        try:
            return json.loads(raw)
        except Exception:
            pass
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("provider:"):
                return {"memory": {"provider": stripped.split(":", 1)[1].strip()}}
        if "description:" in raw:
            return {"description": "Local-first Mnemos identity-continuity provider for Hermes"}
        return {}

    yaml_stub.safe_load = safe_load
    yaml_stub.safe_dump = lambda data, **kwargs: json.dumps(data)
    monkeypatch.setitem(sys.modules, "yaml", yaml_stub)


def test_provider_imports_and_exposes_hermes_tools(tmp_path):
    provider = _new_provider(tmp_path)
    provider.initialize("session-1", hermes_home=tmp_path)

    try:
        schemas = provider.get_tool_schemas()
    finally:
        provider.shutdown()

    names = {schema["name"] for schema in schemas}
    assert names == {
        "mnemos_identity_capture",
        "mnemos_identity_recall",
        "mnemos_identity_correct",
        "mnemos_identity_report",
    }


def test_provider_defaults_are_small_identity_continuity_defaults():
    config = HermesMnemosConfig()

    assert config.max_recall_results == 4
    assert config.max_context_chars == 2200


def test_provider_context_stays_under_configured_budget(tmp_path):
    provider = MnemosMemoryProviderCore(
        HermesMnemosConfig(
            db_path=str(tmp_path / "mnemos.db"),
            agent_id="coder",
            person_id="riley",
            project_scope="mnemos",
            auto_bootstrap=False,
            max_context_chars=800,
            max_recall_results=4,
        )
    )
    provider.initialize("session-1", hermes_home=tmp_path)

    try:
        for idx in range(8):
            provider.handle_tool_call(
                "mnemos_identity_capture",
                {
                    "content": (
                        f"Riley identity-continuity preference {idx}: keep Hermes Mnemos "
                        "packets compact, scoped, local-first, and non-destructive. " * 4
                    ),
                    "importance": "high",
                },
            )
        prompt_block = provider.system_prompt_block()
        prefetched = provider.prefetch("identity-continuity preference compact scoped local-first")
    finally:
        provider.shutdown()

    assert len(prompt_block) <= 800
    assert len(prefetched) <= 800


def test_provider_can_capture_and_recall_in_temp_home(tmp_path):
    provider = _new_provider(tmp_path)
    provider.initialize("session-1", hermes_home=tmp_path)

    try:
        captured = json.loads(provider.handle_tool_call(
            "mnemos_identity_capture",
            {
                "content": "Riley prefers Hermes Mnemos continuity to stay local-first.",
                "importance": "high",
            },
        ))
        recalled = json.loads(provider.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "local-first continuity", "max_results": 4},
        ))
    finally:
        provider.shutdown()

    assert captured["ok"] is True
    assert "Captured continuity" in captured["result"]
    assert recalled["ok"] is True
    assert "local-first" in recalled["result"]


def test_hermes_agent_scope_does_not_leak_between_agents(tmp_path):
    db_path = str(tmp_path / "shared.db")
    nova = MnemosMemoryProviderCore(
        HermesMnemosConfig(
            db_path=db_path,
            person_id="riley",
            project_scope="mnemos",
            auto_bootstrap=False,
        )
    )
    vektor = MnemosMemoryProviderCore(
        HermesMnemosConfig(
            db_path=db_path,
            person_id="riley",
            project_scope="mnemos",
            auto_bootstrap=False,
        )
    )
    nova.initialize("session-a", hermes_home=tmp_path, agent_identity="nova")
    vektor.initialize("session-b", hermes_home=tmp_path, agent_identity="vektor")

    try:
        nova.handle_tool_call(
            "mnemos_identity_capture",
            {"content": "Nova remembers a scoped Hermes identity note.", "importance": "high"},
        )
        leaked = json.loads(vektor.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "scoped Hermes identity note"},
        ))
        found = json.loads(nova.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "scoped Hermes identity note"},
        ))
    finally:
        nova.shutdown()
        vektor.shutdown()

    assert "No relevant continuity found" in leaked["result"]
    assert "Nova remembers" in found["result"]


def test_hermes_scope_does_not_leak_between_people_or_projects(tmp_path):
    db_path = str(tmp_path / "shared.db")
    riley = MnemosMemoryProviderCore(
        HermesMnemosConfig(
            db_path=db_path,
            agent_id="coder",
            person_id="riley",
            project_scope="mnemos",
            auto_bootstrap=False,
        )
    )
    alex = MnemosMemoryProviderCore(
        HermesMnemosConfig(
            db_path=db_path,
            agent_id="coder",
            person_id="alex",
            project_scope="mnemos",
            auto_bootstrap=False,
        )
    )
    other_project = MnemosMemoryProviderCore(
        HermesMnemosConfig(
            db_path=db_path,
            agent_id="coder",
            person_id="riley",
            project_scope="other-project",
            auto_bootstrap=False,
        )
    )
    riley.initialize("session-riley", hermes_home=tmp_path)
    alex.initialize("session-alex", hermes_home=tmp_path)
    other_project.initialize("session-other", hermes_home=tmp_path)

    try:
        riley.handle_tool_call(
            "mnemos_identity_capture",
            {"content": "Riley's Mnemos Hermes scope contains a private identity note.", "importance": "high"},
        )
        person_leak = json.loads(alex.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "private identity note"},
        ))
        project_leak = json.loads(other_project.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "private identity note"},
        ))
        found = json.loads(riley.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "private identity note"},
        ))
    finally:
        riley.shutdown()
        alex.shutdown()
        other_project.shutdown()

    assert "Riley's Mnemos Hermes scope contains a private identity note" not in person_leak["result"]
    assert "Riley's Mnemos Hermes scope contains a private identity note" not in project_leak["result"]
    assert "private identity note" in found["result"]


def test_bootstrap_reads_soul_without_overwriting_it(tmp_path):
    soul = tmp_path / "SOUL.md"
    original = "You are Hermes-Coder, a precise local-first engineering agent."
    soul.write_text(original, encoding="utf-8")
    provider = MnemosMemoryProviderCore(
        HermesMnemosConfig(
            db_path=str(tmp_path / "mnemos.db"),
            auto_bootstrap=True,
            deep_maintenance=False,
        )
    )
    provider.initialize("session-1", hermes_home=tmp_path, agent_identity="coder")

    try:
        recalled = json.loads(provider.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "Hermes-Coder local-first engineering"},
        ))
    finally:
        provider.shutdown()

    assert soul.read_text(encoding="utf-8") == original
    assert "Hermes-Coder" in recalled["result"]


def test_builtin_memory_files_are_never_overwritten(tmp_path):
    files = {
        "SOUL.md": "You are Hermes-Coder, a precise local-first engineering agent.",
        "MEMORY.md": "Hermes built-in long-term memory stays under Hermes control.",
        "USER.md": "Riley is the user profile managed by Hermes built-in memory.",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    provider = MnemosMemoryProviderCore(
        HermesMnemosConfig(
            db_path=str(tmp_path / "mnemos.db"),
            auto_bootstrap=True,
            mirror_builtin_memory=True,
            deep_maintenance=False,
        )
    )
    provider.initialize("session-1", hermes_home=tmp_path, agent_identity="coder")

    try:
        provider.on_memory_write(
            "add",
            "user",
            "Riley wants Hermes built-in memory mirroring to remain non-destructive.",
            metadata={"source": "builtin"},
        )
    finally:
        provider.shutdown()

    for name, content in files.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == content


def test_builtin_memory_write_is_mirrored_and_can_be_forgotten(tmp_path):
    provider = _new_provider(tmp_path)
    provider.initialize("session-1", hermes_home=tmp_path)

    try:
        provider.on_memory_write(
            "add",
            "user",
            "Riley wants identity-memory corrections to supersede stale claims.",
            metadata={"tool_name": "memory", "session_id": "session-1"},
        )
        before = provider.prefetch("identity-memory corrections")
        provider.on_memory_write("remove", "user", "identity-memory corrections", metadata={})
        after = json.loads(provider.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "identity-memory corrections"},
        ))
    finally:
        provider.shutdown()

    assert "supersede stale claims" in before
    assert "No relevant continuity found" in after["result"]


def test_uncertain_capture_goes_to_review_inbox(tmp_path):
    provider = _new_provider(tmp_path)
    provider.initialize("session-1", hermes_home=tmp_path)

    try:
        queued = json.loads(provider.handle_tool_call(
            "mnemos_identity_capture",
            {
                "content": "Maybe remember that the launch label could be Night Glass.",
                "review": True,
            },
        ))
        inbox = json.loads(provider.handle_tool_call(
            "mnemos_identity_report",
            {"kind": "inbox"},
        ))
    finally:
        provider.shutdown()

    assert queued["status"] == "queued_for_review"
    assert inbox["inbox"]
    assert inbox["inbox"][0]["confidence"] < 0.62
    assert "Night Glass" in inbox["inbox"][0]["content"]


def test_pre_compression_preserves_identity_critical_facts(tmp_path):
    provider = _new_provider(tmp_path)
    provider.initialize("session-1", hermes_home=tmp_path)

    try:
        contribution = provider.on_pre_compress([
            {
                "role": "user",
                "content": "Remember that Riley prefers Hermes identity packets to stay concise.",
            },
            {
                "role": "assistant",
                "content": "Verified the concise identity packet behavior for the Hermes provider.",
            },
        ])
        recalled = json.loads(provider.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "concise identity packet"},
        ))
    finally:
        provider.shutdown()

    assert "Mnemos preserved" in contribution
    assert "concise identity packet" in recalled["result"]


def test_provider_mode_activation_sets_mnemos_provider(tmp_path):
    result = install_hermes_plugin(hermes_home=tmp_path, mode="provider", activate=True)
    diagnostics = build_diagnostics(tmp_path)

    assert result.activated is True
    assert result.active_provider == "mnemos"
    assert diagnostics["mode"] == "provider"
    assert diagnostics["provider_mode_active"] is True
    assert diagnostics["active_memory_provider"] == "mnemos"
    for plugin_dir in provider_plugin_dirs(tmp_path):
        assert (plugin_dir / "__init__.py").exists()
        assert (plugin_dir / "plugin.yaml").exists()
    assert len(diagnostics["provider_shim_ready_dirs"]) == 2


def test_sidecar_mode_preserves_existing_external_provider_and_adds_mcp(tmp_path, monkeypatch):
    _install_yaml_stub(monkeypatch)
    (tmp_path / "config.yaml").write_text("memory:\n  provider: honcho\n", encoding="utf-8")

    result = install_hermes_plugin(
        hermes_home=tmp_path,
        mode="sidecar",
        activate=True,
        agent_id="coder",
        person_id="riley",
        project_scope="mnemos",
    )
    diagnostics = build_diagnostics(tmp_path)

    assert result.activated is False
    assert result.active_provider == "honcho"
    assert result.mcp_configured is True
    assert any("ignoring --activate" in warning for warning in result.warnings)
    assert diagnostics["mode"] == "sidecar"
    assert diagnostics["active_memory_provider"] == "honcho"
    assert diagnostics["mcp_server_configured"] is True
    assert diagnostics["mcp_server"]["args"] == [
        "serve",
        "--mode",
        "simple",
        "--agent-id",
        "coder",
        "--person-id",
        "riley",
        "--project-scope",
        "mnemos",
    ]
    assert not (tmp_path / "plugins" / "mnemos" / "__init__.py").exists()


def test_installed_shim_is_discoverable_by_local_hermes(tmp_path, monkeypatch):
    hermes_root = Path("/Users/rileycoyote/Hermes")
    if not hermes_root.exists():
        pytest.skip("local Hermes checkout is not available")

    install_hermes_plugin(hermes_home=tmp_path, activate=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.syspath_prepend(str(hermes_root))
    if "yaml" not in sys.modules:
        yaml_stub = types.ModuleType("yaml")

        def safe_load(text):
            if "provider: mnemos" in str(text):
                return {"memory": {"provider": "mnemos"}}
            if "description:" in str(text):
                return {"description": "Local-first Mnemos identity continuity memory provider"}
            return {}

        yaml_stub.safe_load = safe_load
        yaml_stub.safe_dump = lambda data, **kwargs: json.dumps(data)
        monkeypatch.setitem(sys.modules, "yaml", yaml_stub)

    for name in list(sys.modules):
        if name == "plugins" or name.startswith("plugins.memory") or name.startswith("_hermes_user_memory"):
            sys.modules.pop(name, None)

    memory_plugins = importlib.import_module("plugins.memory")
    provider = memory_plugins.load_memory_provider("mnemos")

    assert provider is not None
    assert provider.name == "mnemos"
    assert provider.is_available() is True


def test_sync_turn_accepts_forward_compatible_messages_payload(tmp_path):
    provider = _new_provider(tmp_path)
    provider.initialize("session-1", hermes_home=tmp_path)

    try:
        provider.sync_turn(
            "ok",
            "ok",
            session_id="session-1",
            messages=[
                {
                    "role": "user",
                    "content": "Remember that Riley wants Hermes sync_turn message payloads handled safely.",
                },
                {
                    "role": "assistant",
                    "content": "Verified the Hermes sync_turn message payload handling.",
                },
            ],
        )
        recalled = json.loads(provider.handle_tool_call(
            "mnemos_identity_recall",
            {"query": "sync_turn message payloads"},
        ))
    finally:
        provider.shutdown()

    assert recalled["ok"] is True
    assert "sync_turn message payload" in recalled["result"]


def test_cli_hermes_sidecar_smoke_preserves_provider(tmp_path, capsys, monkeypatch):
    _install_yaml_stub(monkeypatch)
    from mnemos.cli import main

    (tmp_path / "config.yaml").write_text("memory:\n  provider: honcho\n", encoding="utf-8")

    code = main(["hermes", "install", "--hermes-home", str(tmp_path), "--mode", "sidecar"])
    out = capsys.readouterr().out
    doctor_code = main(["hermes", "doctor", "--hermes-home", str(tmp_path)])
    doctor = capsys.readouterr().out

    assert code == 0
    assert doctor_code == 0
    assert "Sidecar Mode" in out
    assert "memory.provider: honcho" in out
    assert "Mode:        Sidecar Mode" in doctor
    assert "Provider:    honcho" in doctor


def test_cli_hermes_provider_smoke_activates_provider(tmp_path, capsys, monkeypatch):
    _install_yaml_stub(monkeypatch)
    from mnemos.cli import main

    code = main([
        "hermes",
        "install",
        "--hermes-home",
        str(tmp_path),
        "--mode",
        "provider",
        "--activate",
    ])
    out = capsys.readouterr().out
    doctor_code = main(["hermes", "doctor", "--hermes-home", str(tmp_path)])
    doctor = capsys.readouterr().out

    assert code == 0
    assert doctor_code == 0
    assert "Provider Mode active: yes" in out
    assert "Mode:        Provider Mode" in doctor
    assert "Provider:    mnemos" in doctor


def test_quickstart_defaults_to_sidecar_with_existing_provider(tmp_path, monkeypatch):
    _install_yaml_stub(monkeypatch)
    (tmp_path / "config.yaml").write_text("memory:\n  provider: honcho\n", encoding="utf-8")

    result = quickstart_hermes(hermes_home=tmp_path)
    diagnostics = build_diagnostics(tmp_path)

    assert result.ok is True
    assert result.mode == "sidecar"
    assert result.preserved_provider == "honcho"
    assert diagnostics["active_memory_provider"] == "honcho"
    assert diagnostics["mcp_server_configured"] is True
    assert diagnostics["provider_shim_ready"] is False


def test_quickstart_agent_safe_never_changes_memory_provider(tmp_path, monkeypatch):
    _install_yaml_stub(monkeypatch)
    (tmp_path / "config.yaml").write_text("memory:\n  provider: supermemory\n", encoding="utf-8")

    result = quickstart_hermes(
        hermes_home=tmp_path,
        agent_safe=True,
        agent_id="coder",
        person_id="riley",
        project_scope="mnemos",
    )
    diagnostics = build_diagnostics(tmp_path)

    assert result.ok is True
    assert result.mode == "sidecar"
    assert diagnostics["active_memory_provider"] == "supermemory"
    assert diagnostics["mcp_server_configured"] is True
    assert diagnostics["mcp_server"]["args"] == [
        "serve",
        "--mode",
        "simple",
        "--agent-id",
        "coder",
        "--person-id",
        "riley",
        "--project-scope",
        "mnemos",
    ]
    assert "Preserved: SOUL.md, MEMORY.md, USER.md, AGENTS.md, and memory.provider" in result.summary()


def test_quickstart_provider_mode_requires_explicit_flag(tmp_path, monkeypatch):
    _install_yaml_stub(monkeypatch)

    default_result = quickstart_hermes(hermes_home=tmp_path)
    default_diagnostics = build_diagnostics(tmp_path)
    provider_result = quickstart_hermes(hermes_home=tmp_path, provider=True)
    provider_diagnostics = build_diagnostics(tmp_path)

    assert default_result.mode == "sidecar"
    assert default_diagnostics["active_memory_provider"] == ""
    assert default_diagnostics["mcp_server_configured"] is True
    assert provider_result.mode == "provider"
    assert provider_result.ok is True
    assert provider_diagnostics["active_memory_provider"] == "mnemos"
    assert provider_diagnostics["provider_mode_active"] is True


def test_quickstart_agent_safe_refuses_risky_mcp_replacement(tmp_path, monkeypatch):
    _install_yaml_stub(monkeypatch)
    existing = {
        "memory": {"provider": "honcho"},
        "mcp_servers": {"mnemos": {"command": "custom-mnemos", "args": ["serve"]}},
    }
    (tmp_path / "config.yaml").write_text(json.dumps(existing), encoding="utf-8")

    result = quickstart_hermes(hermes_home=tmp_path, agent_safe=True)
    config = json.loads((tmp_path / "config.yaml").read_text(encoding="utf-8"))

    assert result.ok is False
    assert config["memory"]["provider"] == "honcho"
    assert config["mcp_servers"]["mnemos"]["command"] == "custom-mnemos"
    assert any("agent-safe mode left it unchanged" in warning for warning in result.warnings)


def test_cli_hermes_quickstart_agent_safe_smoke(tmp_path, capsys, monkeypatch):
    _install_yaml_stub(monkeypatch)
    from mnemos.cli import main

    code = main(["hermes", "quickstart", "--hermes-home", str(tmp_path), "--agent-safe"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Mnemos Hermes quickstart" in out
    assert "Mode: Sidecar Mode" in out
    assert "Agent-safe: yes" in out
    assert "Doctor summary:" in out
    assert "MCP sidecar: configured" in out
    assert "Restart: yes" in out


def test_cli_hermes_quickstart_provider_smoke(tmp_path, capsys, monkeypatch):
    _install_yaml_stub(monkeypatch)
    from mnemos.cli import main

    code = main(["hermes", "quickstart", "--hermes-home", str(tmp_path), "--provider"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Mode: Provider Mode" in out
    assert "memory.provider: mnemos" in out
    assert "Provider shim: installed" in out


def test_docs_include_safe_hermes_agent_prompt():
    root = Path(__file__).resolve().parents[1]
    install_doc = (root / "HERMES_INSTALL.md").read_text(encoding="utf-8")
    integration_doc = (root / "docs" / "hermes-integration.md").read_text(encoding="utf-8")

    assert "Install Mnemos for yourself" in install_doc
    assert "quickstart --agent-safe" in install_doc
    assert "Do not overwrite SOUL.md, MEMORY.md, USER.md, AGENTS.md" in install_doc
    assert "memory.provider=mnemos" in install_doc or "provider: mnemos" in install_doc
    assert "Sidecar Mode is the default safe path" in install_doc
    assert "quickstart --agent-safe" in integration_doc


def test_durable_memories_do_not_leak_between_people_sharing_an_agent(tmp_path, monkeypatch):
    """Engrams carry only owner_agent_id; continuity is scoped by three.

    Two people talking to one agent each build their own continuity, but
    retrieval filtered engrams by agent alone — so one person's private
    durable memories surfaced in the other's recall while the continuity
    layer above them stayed correctly separated.

    The leak was masked: a templated `impact` was crowding the private
    memory out of the ranked results by accident. Removing the template is
    what made it visible.
    """
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    from mnemos.simple_runtime import MnemosRuntime

    db = str(tmp_path / "shared.db")
    secret = "Riley's private salary negotiation notes"

    riley = MnemosRuntime(db_path=db, agent_id="hermes", person_id="riley",
                          project_scope="work", use_dedicated_model=False)
    try:
        riley.capture(secret, importance="high")
        assert secret in riley.recall("salary negotiation notes"), (
            "the owner cannot see their own memory"
        )
    finally:
        riley.close()

    alex = MnemosRuntime(db_path=db, agent_id="hermes", person_id="alex",
                         project_scope="work", use_dedicated_model=False)
    try:
        assert secret not in alex.recall("private salary negotiation notes"), (
            "one person read another person's private memory"
        )
    finally:
        alex.close()

    other_project = MnemosRuntime(db_path=db, agent_id="hermes", person_id="riley",
                                  project_scope="personal", use_dedicated_model=False)
    try:
        assert secret not in other_project.recall("private salary negotiation notes"), (
            "a memory scoped to one project surfaced in another"
        )
    finally:
        other_project.close()
