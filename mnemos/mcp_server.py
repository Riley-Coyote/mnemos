"""
Mnemos MCP Server.

Exposes Mnemos memory operations as MCP tools that any agent can call.
Uses the Anthropic MCP Python SDK (FastMCP).

Tools:
    mnemos_session_start — Start or resume a functional-memory session
    mnemos_functional_update — Store live working context
    mnemos_functional_list — List live working context
    mnemos_session_close — Compress functional context into hypomnema
    mnemos_context_packet — Build the turnkey prompt/context packet
    mnemos_review_queue — Show confirmations and promotion candidates
    mnemos_proposal_audit — Inspect audit-only proposal ledger rows
    mnemos_visual_snapshot — Generate an inline Mermaid memory map
    mnemos_remember     — Encode a new memory
    mnemos_ingest       — Ingest content from external sources
    mnemos_recall       — Retrieve relevant memories
    mnemos_hypomnema_write   — Write scoped continuity before promotion
    mnemos_hypomnema_search  — Search operational scoped continuity
    mnemos_hypomnema_revise  — Revise scoped continuity
    mnemos_hypomnema_supersede — Replace stale scoped continuity
    mnemos_hypomnema_candidates — List operational promotion-ready continuity
    mnemos_hypomnema_promote — Promote stable continuity into Mnemos
    mnemos_inspect      — View full details of a memory
    mnemos_introspect   — Audit text for metacognitive pattern markers
    mnemos_status       — Get memory system status
    mnemos_beliefs      — List reviewed current beliefs
    mnemos_forget       — Archive a specific memory
    mnemos_consolidate  — Trigger a consolidation cycle

Usage:
    mnemos serve        — Start as stdio MCP server (for agent config)
"""

from __future__ import annotations

import json
import logging
import hashlib
import os
import signal
import sys

from mcp.server.fastmcp import FastMCP

from .core.types import SourceAuthority, SourceType
from .simple_runtime import _classify_domain, escalate_domain
from .store.sqlite_store import (
    EngramStore,
    READ_VISIBILITY_OPERATIONAL,
    READ_VISIBILITY_REVIEW,
)
from .store.embedding_index import EmbeddingIndex
from .encoding.encoder import Encoder
from .retrieval.reactive import ReactiveRetriever
from .consolidation.daemon import ConsolidationDaemon
from .interface.context_packet import build_context_packet
from .interface.visual_snapshot import build_memory_visual_snapshot
from .config.loader import load_config, save_config
from .simple_mcp import configure_runtime, register_simple_tools

logger = logging.getLogger("mnemos.mcp")

# Global state — initialized when server starts
_store: EngramStore | None = None
_encoder: Encoder | None = None
_retriever: ReactiveRetriever | None = None
_embedding_index: EmbeddingIndex | None = None
_shared_pool = None
_llm_client = None
_config: dict | None = None
_default_agent_id = "default"
_default_person_id = "user"
_default_project_scope = "global"

mcp = FastMCP("mnemos")
register_simple_tools(mcp, include_recall=False)


# ═══════════════════════════════════════════════════
# ONBOARDING WIZARD — 10-Step Setup-Gate Pattern
# ═══════════════════════════════════════════════════

ONBOARDING_PROMPTS = {
    0: (
        "Let's set up Mnemos as a complete memory system for this agent.\n\n"
        "It has three main layers:\n"
        "1. Functional memory: the live working context of this session.\n"
        "2. Hypomnema: scoped continuity that survives sessions and can still be revised.\n"
        "3. Mnemos: the long-term graph of engrams, beliefs, decay, and reconsolidation.\n\n"
        "During setup I'll learn the agent identity, the human relationship, active projects, "
        "review preferences, and whether the background substrate should run."
    ),
    1: "What should this agent be called?",
    2: "Who is the primary human this memory should be scoped to?",
    3: (
        "Tell me the important starting context for this relationship. "
        "Who is the human, what matters to them, and what should the agent be careful to remember?"
    ),
    4: (
        "What projects or ongoing work should this agent recognize immediately? "
        "Use commas or separate lines."
    ),
    5: (
        "Do you have conversation history, notes, or project files to import? "
        "Share a local path, or say 'skip' to start fresh."
    ),
    6: None,  # Generated dynamically from steps 3-4
    7: (
        "Should the cognitive substrate run in the background? "
        "If enabled, Mnemos can decay, consolidate, reflect, and surface review cues between sessions. "
        "It works without this, but the system feels more alive with it on."
    ),
    8: (
        "Optional: add an LLM provider for richer classification, reflection, and consolidation.\n\n"
        "Format: provider:key (e.g., openrouter:sk-or-v1-abc123)\n"
        "Or paste an OpenRouter key. Say 'skip' to use local/rule-based fallbacks."
    ),
    9: None,  # Generated dynamically — the "alive" message
}


def _get_config() -> dict:
    """Get or load the global config."""
    global _config
    if _config is None:
        try:
            _config = load_config()
        except Exception:
            _config = {}
    return _config


def _is_setup_complete() -> bool:
    """Check if onboarding has been completed."""
    return _get_config().get("setup_complete", False)


def _setup_gate() -> str | None:
    """Returns a redirect message if setup is incomplete, None if ready."""
    if not _is_setup_complete():
        return "Mnemos isn't configured yet — call mnemos_setup to get started."
    return None


def _effective_agent_id(agent_id: str = "default") -> str:
    """Resolve advanced tools to the configured server identity by default."""
    if agent_id and agent_id != "default":
        return agent_id
    config = _get_config()
    configured = config.get("agent_id") or os.environ.get("MNEMOS_AGENT_ID")
    return str(configured or _default_agent_id or "default")


def _effective_person_id(person_id: str = "user") -> str:
    """Resolve advanced tools to the configured server person by default.

    Mirrors _effective_agent_id: the literal default sentinel ("user") means
    "unspecified — inherit the server's configured --person-id". An explicit
    non-default value is respected.
    """
    if person_id and person_id != "user":
        return person_id
    config = _get_config()
    configured = config.get("person_id") or os.environ.get("MNEMOS_PERSON_ID")
    return str(configured or _default_person_id or "user")


def _effective_project_scope(project_scope: str = "global") -> str:
    """Resolve advanced tools to the configured server project by default.

    Mirrors _effective_agent_id: the literal default sentinel ("global") means
    "unspecified — inherit the server's configured --project-scope". An explicit
    non-default value is respected.
    """
    if project_scope and project_scope != "global":
        return project_scope
    config = _get_config()
    configured = config.get("project_scope") or os.environ.get("MNEMOS_PROJECT_SCOPE")
    return str(configured or _default_project_scope or "global")


def _effective_scope(
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
) -> tuple[str, str, str]:
    """Resolve all three scope dimensions to the configured server scope.

    Advanced tools default their scope params to the literal sentinels
    ("default"/"user"/"global"); unless the caller passes an explicit override,
    each dimension inherits the server's configured --agent-id/--person-id/
    --project-scope (the same scope the simple runtime already inherits). Without
    this, scoped reads/writes silently miss data stored under the server scope.
    """
    return (
        _effective_agent_id(agent_id),
        _effective_person_id(person_id),
        _effective_project_scope(project_scope),
    )


def _set_server_defaults(
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> None:
    """Persist the server's configured scope so advanced tools inherit it.

    The advanced tools resolve their scope via _effective_* against these
    module globals / env vars. Before this ran, only agent_id was persisted, so
    --person-id/--project-scope were dropped and scoped reads/writes silently
    fell back to the user/global defaults.
    """
    global _default_agent_id, _default_person_id, _default_project_scope
    if agent_id:
        _default_agent_id = agent_id
        os.environ["MNEMOS_AGENT_ID"] = agent_id
    if person_id:
        _default_person_id = person_id
        os.environ["MNEMOS_PERSON_ID"] = person_id
    if project_scope:
        _default_project_scope = project_scope
        os.environ["MNEMOS_PROJECT_SCOPE"] = project_scope


def _init_store(db_path: str = "~/.mnemos/memory.db") -> None:
    """Initialize the global store, helpers, and auto-detect LLM client."""
    global _store, _encoder, _retriever, _llm_client, _embedding_index, _shared_pool
    if _store is None:
        _store = EngramStore(db_path)
        # Initialize embedding index (same DB path — gracefully degrades)
        _embedding_index = EmbeddingIndex(db_path=db_path)
        # Auto-detect LLM client from env vars (before encoder, which uses it)
        from .llm import create_client

        _llm_client = create_client()
        # Initialize shared memory pool
        from .multiagent.shared_pool import SharedPool

        _shared_pool = SharedPool()  # defaults to ~/.mnemos/shared.db
        _encoder = Encoder(
            _store,
            embedding_index=_embedding_index,
            llm_client=_llm_client,
            shared_pool=_shared_pool,
        )
        _retriever = ReactiveRetriever(
            _store,
            embedding_index=_embedding_index,
            shared_store=_shared_pool._store,
        )


def _ensure_store() -> EngramStore:
    """Get the store, initializing if needed."""
    if _store is None:
        _init_store()
    return _store  # type: ignore


def _reconcile_vault_on_session_start() -> None:
    """Reconcile identity-tier rows against the vault journal at session start.

    008i — the previous ``except Exception: pass`` **swallow is dead**. It was
    exactly how the corrupt-journal fail-open hole existed: an unreadable
    journal became "we didn't reconcile" silently. Now:
      - resolver returns None → vault not armed, nothing to do
      - journal read raises → catch, classify, ALERT (Oliver Inbox), let
        ``reconcile_identity_vault`` handle the quarantine per 008i
      - any other exception → catch + alert; session start must not crash,
        but neither can it silently proceed as if the vault verified
    """
    if _store is None:
        return
    from mnemos.store.sqlite_store import resolve_vault_journal_path
    journal_path = resolve_vault_journal_path()
    if not journal_path:
        return
    # 008-r14 #3: _vault_active is frozen at store construction. A long-running
    # MCP server started BEFORE the vault journal existed keeps
    # _vault_active=False; once install creates the journal, session-start
    # resolves it (above) but the read APIs would still skip the gate. Refresh
    # the flag now that a journal has resolved, so the read-path gate arms for
    # the rest of this process's life without needing a restart.
    _store._vault_active = True
    # 008i-r10 #1: apply_legacy_witness and reconcile MUST NOT share a try —
    # a corrupt/broken-chain journal raises inside apply_legacy_witness and,
    # if wrapped with reconcile, jumps to the outer handler BEFORE the
    # reconciler classifies + quarantines. That reintroduces the fail-open
    # 008i was ruled to close. Stamp first (best-effort), then ALWAYS reconcile.
    stamp_error: Exception | None = None
    try:
        _store.apply_legacy_witness()
    except Exception as exc:
        stamp_error = exc
    try:
        report = _store.reconcile_identity_vault(journal_path)
        # 008r/review (session-start-drops-high-vault-findings): alert on ANY
        # non-clean reconcile outcome, not only `critical`. The reconciler emits
        # `high` findings for orphan/forged/missing witnessed rows and may
        # re-quarantine them; gating the alert on `critical` let those
        # divergences be handled SILENTLY until the watchdog cron next ran.
        # Session-start sees them first — it must not stay silent. Pass the full
        # findings list so every severity (chain break, corrupt journal,
        # tampered witness, orphan/forged/missing) surfaces to David.
        if report.findings or report.requarantined or stamp_error is not None:
            _alert_vault_findings(
                journal_path,
                report.findings,
                report.requarantined,
                stamp_error=stamp_error,
            )
    except Exception as exc:  # session start must not crash
        _alert_vault_error(journal_path, exc, stamp_error=stamp_error)


def _alert_vault_findings(journal_path, findings, requarantined, stamp_error=None) -> None:
    """Write a vault-critical alert to Oliver Inbox (008i requirement).

    008-r14 review (#3): ``stamp_error`` MUST appear in the written alert. The
    caller fires this whenever critical reconcile findings **or** a legacy-stamp
    failure occurred (``if critical or stamp_error is not None``). If only the
    stamp failed — reconcile clean, ``apply_legacy_witness`` raised — an alert
    that omitted ``stamp_error`` would write an empty Findings list and silently
    hide the actual session-start failure. Surface it, and make the header say
    which case fired.
    """
    try:
        import datetime
        import pathlib
        alert_dir = pathlib.Path(
            os.environ.get(
                "MNEMOS_WATCHDOG_ALERT_DIR",
                os.path.expanduser("~/Oliver Inbox"),
            )
        )
        alert_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.date.today().isoformat()
        path = alert_dir / f"{today}-vault-session-start-alert.md"
        header = (
            "Vault: session-start reconcile surfaced critical findings."
            if findings
            else "Vault: session-start legacy-stamp failed "
            "(no critical reconcile findings)."
        )
        lines = [header, f"Journal: {journal_path}"]
        if stamp_error is not None:
            lines += [
                "",
                "## Legacy-stamp error (apply_legacy_witness raised)",
                f"- {type(stamp_error).__name__}: {stamp_error}",
            ]
        lines += ["", f"## Findings ({len(findings)})"]
        for f in findings:
            lines.append(
                f"- [{f['severity']}] {f['kind']}: {f['detail']} "
                f"(table={f.get('table')}, row={f.get('row_id')})"
            )
        lines.append(f"\n## Re-quarantined ({len(requarantined)})")
        for item in requarantined:
            lines.append(f"- {item['table']}/{item['row_id']}: {item['detail']}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass  # alerting must not crash


def _alert_vault_error(journal_path, exc, stamp_error=None) -> None:
    """Alert on a session-start reconcile exception (couldn't classify)."""
    try:
        import datetime
        import pathlib
        alert_dir = pathlib.Path(
            os.environ.get(
                "MNEMOS_WATCHDOG_ALERT_DIR",
                os.path.expanduser("~/Oliver Inbox"),
            )
        )
        alert_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.date.today().isoformat()
        path = alert_dir / f"{today}-vault-session-start-error.md"
        body = (
            f"Vault: session-start reconcile raised.\n\n"
            f"Journal: {journal_path}\nError: {type(exc).__name__}: {exc}\n"
        )
        # 008-r14 review #3 (audit-the-class): _alert_vault_findings dropped
        # stamp_error; this peer handler had the same latent drop. If reconcile
        # RAISED and the legacy stamp ALSO failed, surface both — else the stamp
        # failure is hidden behind the reconcile exception.
        if stamp_error is not None:
            body += (
                f"Legacy-stamp error (apply_legacy_witness raised): "
                f"{type(stamp_error).__name__}: {stamp_error}\n"
            )
        path.write_text(body, encoding="utf-8")
    except Exception:
        pass


def _slugify(value: str, fallback: str = "default") -> str:
    """Make a stable lowercase ID from a human label."""
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    clean = "-".join(part for part in clean.split("-") if part)
    return clean or fallback


def _format_functional_entry(entry: dict) -> str:
    flags = []
    if entry.get("pinned"):
        flags.append("pinned")
    if entry.get("needs_confirmation"):
        flags.append("needs confirmation")
    flag_text = f" flags={','.join(flags)}" if flags else ""
    return (
        f"- {entry['content']}\n"
        f"  id={entry['id']} type={entry['memory_type']} "
        f"confidence={entry['confidence']:.2f} salience={entry['salience']:.2f} "
        f"scope={entry['agent_id']}/{entry['person_id']}/{entry['project_scope']}"
        f"{flag_text}"
    )


def _format_hypomnema_entry(entry: dict) -> str:
    tags = ", ".join(entry.get("tags", [])) or "(none)"
    promoted = entry.get("graduated_to_engram_id") or "not promoted"
    return (
        f"- {entry['content']}\n"
        f"  id={entry['id']} domain={entry['domain']} source={entry['source']} "
        f"confidence={entry['confidence']:.2f} salience={entry['salience']:.2f} "
        f"scope={entry['agent_id']}/{entry['person_id']}/{entry['project_scope']}\n"
        f"  tags={tags} revisions={entry['revision_count']} promoted={promoted}"
    )


def _format_proposal_entry(entry: dict) -> str:
    payload = entry.get("payload") or {}
    if isinstance(payload, dict) and payload.get("content"):
        payload_text = str(payload["content"])
    else:
        payload_text = str(payload)
    provenance = ", ".join(entry.get("provenance_ids") or []) or "none"
    return (
        f"- {payload_text}\n"
        f"  id={entry['id']} authority={entry['source_authority']} "
        f"kind={entry['kind']} domain={entry['domain']} "
        f"target={entry['target_surface']} transition={entry['transition']} "
        f"blast={entry['blast_radius']} status={entry['status']} "
        f"visibility={entry['read_visibility']} provenance={provenance}"
    )


@mcp.tool()
def mnemos_setup(response: str = "") -> str:
    """Onboarding wizard for Mnemos. Call this to configure the memory system.

    On first call, returns a welcome message. On subsequent calls, pass the
    user's response to advance through the setup steps.

    Args:
        response: The user's answer to the current setup step. Empty on first call.
    """
    config = _get_config()
    step = config.get("setup_step", 0)

    # Step 0: Welcome (no response needed, just show the prompt)
    if step == 0 and not response:
        config["setup_step"] = 1
        save_config(config)
        _config_invalidate()
        return ONBOARDING_PROMPTS[0] + "\n\n" + ONBOARDING_PROMPTS[1]

    # Step 1: Agent name
    if step == 1:
        agent_name = response.strip() or "Agent"
        config["agent_name"] = agent_name
        config["agent_id"] = _slugify(agent_name)
        config["setup_step"] = 2
        save_config(config)
        _config_invalidate()
        return ONBOARDING_PROMPTS[2]

    # Step 2: User name
    if step == 2:
        user_name = response.strip() or "User"
        config["user_name"] = user_name
        config["person_id"] = _slugify(user_name, fallback="user")
        config["setup_step"] = 3
        save_config(config)
        _config_invalidate()
        return ONBOARDING_PROMPTS[3]

    # Step 3: User description → encode seed engrams
    if step == 3:
        config["user_description"] = response.strip()
        config["setup_step"] = 4
        save_config(config)
        _config_invalidate()

        # Encode seed engrams from the description
        _ensure_store()
        agent_id = config.get("agent_id", "default")
        person_id = config.get("person_id", "user")
        agent_name = config.get("agent_name", "Agent")
        user_name = config.get("user_name", "User")
        session = _store.start_memory_session(  # type: ignore
            session_id=config.get("onboarding_session_id") or None,
            agent_id=agent_id,
            person_id=person_id,
            project_scope="onboarding",
            title="Mnemos onboarding",
            source="mnemos_setup",
        )
        config["onboarding_session_id"] = session["id"]

        seeds = [
            f"My user is {user_name}. {response.strip()[:500]}",
            f"I am {agent_name}. {user_name} and I are beginning to work together.",
        ]
        # Extract key phrases for additional seed engrams
        sentences = [
            s.strip()
            for s in response.replace(".", ".\n").split("\n")
            if len(s.strip()) > 20
        ]
        for s in sentences[:3]:
            seeds.append(f"{user_name} told me: {s}")

        encoded = 0
        for seed in seeds[:5]:
            try:
                _encoder.encode(
                    content=seed,
                    impact=f"Foundation memory from initial setup with {user_name}.",
                    kind="semantic",
                    tags=["identity", "setup"],
                    source=SourceType.USER_EXPLICIT,
                    agent_id=agent_id,
                    skip_surprise_detection=True,
                    # Setup-wizard seed is built from tool-call content, not a
                    # reviewed David assertion; observed keeps user_stated
                    # un-mintable outside U4 (F1 ruling).
                    source_authority=SourceAuthority.OBSERVED,
                )
                encoded += 1
            except Exception as e:
                logger.warning(f"Failed to encode seed engram: {e}")

        try:
            _store.write_hypomnema_entry(  # type: ignore
                f"{user_name} starting context: {response.strip()[:1200]}",
                agent_id=agent_id,
                person_id=person_id,
                project_scope="global",
                source="co-formed",
                domain="identity",
                tags=["onboarding", "identity", "relationship"],
                confidence=0.82,
                salience=0.8,
                foundational=True,
                related_session_id=session["id"],
            )
            _store.write_functional_memory(  # type: ignore
                "Complete Mnemos onboarding and verify the agent can use functional memory, hypomnema, context packets, and review tools.",
                session_id=session["id"],
                agent_id=agent_id,
                person_id=person_id,
                project_scope="onboarding",
                memory_type="working",
                confidence=0.9,
                salience=0.75,
                pinned=True,
                source="mnemos_setup",
            )
        except Exception as e:
            logger.warning(f"Failed to seed onboarding continuity: {e}")

        save_config(config)
        _config_invalidate()
        return f"Encoded {encoded} seed memories.\n\n" + ONBOARDING_PROMPTS[4]

    # Step 4: Projects
    if step == 4:
        projects = [
            p.strip() for p in response.replace(",", "\n").split("\n") if p.strip()
        ]
        if "indexer" not in config:
            config["indexer"] = {}
        config["indexer"]["known_projects"] = projects
        config["indexer"]["active_projects"] = projects
        config["setup_step"] = 5
        save_config(config)
        _config_invalidate()

        # Encode project context
        _ensure_store()
        agent_id = config.get("agent_id", "default")
        person_id = config.get("person_id", "user")
        session_id = config.get("onboarding_session_id")
        for proj in projects[:5]:
            try:
                _encoder.encode(
                    content=f"Active project: {proj}",
                    impact="Part of the current work context.",
                    kind="semantic",
                    tags=["project", "context"],
                    source=SourceType.USER_EXPLICIT,
                    agent_id=agent_id,
                    skip_surprise_detection=True,
                    source_authority=SourceAuthority.OBSERVED,  # setup-wizard seed (F1)
                )
            except Exception:
                pass
            try:
                _store.write_hypomnema_entry(  # type: ignore
                    f"Active project for this relationship: {proj}",
                    agent_id=agent_id,
                    person_id=person_id,
                    project_scope=proj,
                    source="co-formed",
                    domain="topical",
                    tags=["onboarding", "project"],
                    confidence=0.78,
                    salience=0.7,
                    related_session_id=session_id,
                    read_visibility=READ_VISIBILITY_OPERATIONAL,
                )
                _store.write_functional_memory(  # type: ignore
                    f"Onboarding project context: {proj}",
                    session_id=session_id,
                    agent_id=agent_id,
                    person_id=person_id,
                    project_scope="onboarding",
                    memory_type="project",
                    confidence=0.85,
                    salience=0.7,
                    source="mnemos_setup",
                )
            except Exception as e:
                logger.warning(f"Failed to seed project continuity: {e}")

        return ONBOARDING_PROMPTS[5]

    # Step 5: History import (optional)
    if step == 5:
        config["setup_step"] = 6
        save_config(config)
        _config_invalidate()

        resp_lower = response.strip().lower()
        if resp_lower in ("skip", "no", "fresh", "start fresh", ""):
            pass  # Skip history import
        else:
            # TODO: Run extraction pipeline on the provided path
            logger.info(f"History import requested: {response.strip()}")

        # Generate seed beliefs from what we know
        _ensure_store()
        agent_id = config.get("agent_id", "default")
        user_name = config.get("user_name", "User")
        user_desc = config.get("user_description", "")
        projects = config.get("indexer", {}).get("known_projects", [])

        beliefs_created = []
        if user_desc:
            # Extract key themes for beliefs
            belief1 = f"{user_name} is deeply invested in their work"
            if projects:
                belief1 = f"{user_name} is deeply invested in {projects[0]}"
            try:
                from .core.belief import Belief

                b = Belief(
                    content=belief1,
                    domain="identity",
                    confidence=0.70,
                    agent_id=agent_id,
                )
                _store.save_belief(b)
                beliefs_created.append(f'"{belief1}" — confidence: 70%')
            except Exception as e:
                logger.warning(f"Failed to create belief: {e}")

        if projects and len(projects) > 1:
            belief2 = f"The work centers on {', '.join(projects[:3])}"
            try:
                b = Belief(
                    content=belief2,
                    domain="context",
                    confidence=0.65,
                    agent_id=agent_id,
                )
                _store.save_belief(b)
                beliefs_created.append(f'"{belief2}" — confidence: 65%')
            except Exception as e:
                logger.warning(f"Failed to create belief: {e}")

        belief_text = (
            "\n".join(f"  - {b}" for b in beliefs_created)
            if beliefs_created
            else "  (none yet — they'll form as we talk)"
        )
        return (
            "From what you've told me, I'm forming my first beliefs. "
            "These aren't facts — they're things I think are true based on what I know so far. "
            "They'll shift as I learn more:\n\n"
            f"{belief_text}\n\n"
            "When something contradicts a belief, I don't just delete it. I examine it. "
            "That's how I grow.\n\n" + ONBOARDING_PROMPTS[7]
        )

    # Step 6: Inner life toggle
    if step == 6:
        resp_lower = response.strip().lower()
        enabled = resp_lower in (
            "yes",
            "y",
            "sure",
            "yeah",
            "on",
            "enable",
            "true",
            "absolutely",
            "do it",
        )
        if "substrate" not in config:
            config["substrate"] = {}
        config["substrate"]["enabled"] = enabled
        config["setup_step"] = 7
        save_config(config)
        _config_invalidate()

        status = (
            "enabled — I'll dream between our conversations"
            if enabled
            else "disabled — I'll remember, but I won't dream"
        )
        return f"Inner life: {status}.\n\n" + ONBOARDING_PROMPTS[8]

    # Step 7: LLM provider
    if step == 7:
        resp = response.strip()
        resp_lower = resp.lower()
        provider = "openrouter"
        api_key = resp

        if "llm" not in config:
            config["llm"] = {}

        if resp_lower in ("skip", "no", "none", "local", "rule-based", ""):
            config["llm"]["provider"] = "none"
            config["llm"]["api_key_env"] = ""
            api_key = ""
        elif ":" in resp and not resp.startswith("sk-"):
            parts = resp.split(":", 1)
            provider = parts[0].strip().lower()
            api_key = parts[1].strip()
            config["llm"]["provider"] = provider
            config["llm"]["api_key_env"] = f"{provider.upper()}_API_KEY"
        else:
            config["llm"]["provider"] = provider
            config["llm"]["api_key_env"] = f"{provider.upper()}_API_KEY"

        # Store the key in-process for the current MCP server if one was supplied.
        import os

        if api_key and provider == "openrouter":
            os.environ["OPENROUTER_API_KEY"] = api_key
        elif api_key and provider == "anthropic":
            os.environ["ANTHROPIC_API_KEY"] = api_key

        config["setup_step"] = 8
        save_config(config)
        _config_invalidate()

        # Count what we've built
        _ensure_store()
        agent_id = config.get("agent_id", "default")
        person_id = config.get("person_id", "user")
        project_scope = "global"
        try:
            stats = _store.get_stats(
                agent_id,
                person_id=person_id,
                project_scope=project_scope,
                read_visibility=READ_VISIBILITY_OPERATIONAL,
            )
            engram_count = stats.get("engrams_active", 0)
            belief_count = stats.get("beliefs_active", 0)
            conn_count = stats.get("connections", 0)
            functional_count = stats.get("functional_active", 0)
            hypomnema_count = stats.get("hypomnema_active", 0)
        except Exception:
            engram_count = belief_count = conn_count = functional_count = (
                hypomnema_count
            ) = 0

        agent_name = config.get("agent_name", "Agent")

        # Final step — set setup complete
        config["setup_complete"] = True
        config["setup_step"] = 9
        save_config(config)
        _config_invalidate()

        return (
            f"{agent_name} is ready.\n\n"
            f"{functional_count} functional memories active, {hypomnema_count} hypomnema entries seeded, "
            f"{engram_count} engrams formed, {belief_count} beliefs taking shape, "
            f"{conn_count} connections emerging.\n\n"
            "Recommended next call:\n"
            f'mnemos_context_packet(query="what should I know before this session?", '
            f'agent_id="{agent_id}", person_id="{person_id}", project_scope="{project_scope}")'
        )

    # Already complete
    return "Setup is already complete. All memory tools are active."


def _config_invalidate():
    """Invalidate the cached config so it's reloaded next time."""
    global _config
    _config = None


@mcp.tool()
def mnemos_remember(
    content: str,
    impact: str = "",
    kind: str = "semantic",
    tags: str = "",
    agent_id: str = "default",
    source_type: str = "session",
    visibility: str = "private",
    skip_surprise_detection: bool = False,
) -> str:
    """Encode a new memory into the Mnemos living memory system.

    Use this to store important information, user preferences, decisions,
    insights, or anything worth remembering across sessions.

    Args:
        content: What happened — the event, information, or observation.
        impact: What it meant — how it changed understanding. Optional but valuable.
            Example: "After this, I understand that patience with debugging is essential."
            When provided, this lasting insight survives even as details fade over time.
        kind: Memory type — "episodic" (experiences), "semantic" (facts/knowledge),
              "procedural" (how-to knowledge). Default: "semantic".
        tags: Comma-separated tags for categorization. Example: "python,debugging,preferences"
        agent_id: Which agent's memory to store in. Default: "default".
        source_type: How the memory was captured — "session", "browser_extraction", etc.
        visibility: Memory visibility — "private", "shared", or "public". Default: "private".

    Source authority is not caller-settable on the MCP surface. This tool
    stamps observed authority from the tool channel; content claiming
    user_stated/imported authority remains untrusted payload text.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    engram = _encoder.encode(  # type: ignore
        content=content,
        impact=impact,
        kind=kind,
        tags=tag_list,
        source=source_type,
        agent_id=agent_id,
        skip_surprise_detection=skip_surprise_detection,
        # Model/MCP agent asserting a memory: observed. This tool exposes no
        # authority parameter, so a caller cannot self-stamp (R1); content
        # claiming "source:user_stated" is payload text, not authority.
        source_authority=SourceAuthority.OBSERVED,
    )

    if visibility != "private":
        engram.visibility = visibility
        _store.save_engram(engram)

    return (
        f"Remembered: {engram.id}\n"
        f"  Confidence: {engram.source.confidence}\n"
        f"  Connections: {len(engram.connections)} discovered\n"
        f"  Tags: {', '.join(engram.tags) or '(none)'}"
    )


@mcp.tool()
def mnemos_ingest(
    content: str,
    impact: str = "",
    kind: str = "semantic",
    tags: str = "",
    agent_id: str = "default",
    source_url: str = "",
    encoding_depth: str = "moderate",
    confidence: float = 0.0,
    skip_surprise: bool = False,
) -> str:
    """Ingest content from an external source into Mnemos.

    Use this for feeding knowledge from external pipelines, documents,
    APIs, or any non-conversational source. Content enters through the
    full encoding pipeline (surprise detection, belief comparison,
    connection discovery) unless encoding_depth is set to "shallow".

    Args:
        content: The knowledge or information to ingest.
        impact: Lasting insight — what this means, not just what it says.
        kind: Memory type — "semantic" (facts), "episodic" (events),
              "procedural" (how-to). Default: "semantic".
        tags: Comma-separated tags. Example: "research,memory-systems"
        agent_id: Which agent's memory to store in. Default: "default".
        source_url: URL or path of the original source (for provenance).
        encoding_depth: Processing depth — "shallow" (store only),
              "moderate" (full pipeline), "deep" (full + belief check).
        confidence: Override confidence score (0.0 = use source-based default).
        skip_surprise: Skip surprise detection during encoding.

    Source authority is not caller-settable on the MCP surface. External MCP
    ingest is stamped observed; curated PAI import is the imported-authority
    path.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    skip = skip_surprise or (encoding_depth == "shallow")
    override_conf = confidence if confidence > 0.0 else None

    engram = _encoder.encode(  # type: ignore
        content=content,
        impact=impact,
        kind=kind,
        tags=tag_list,
        source=SourceType.EXTERNAL,
        agent_id=agent_id,
        override_confidence=override_conf,
        skip_surprise_detection=skip,
        # External pipeline feed is observed, not user-authored (R1/F1).
        source_authority=SourceAuthority.OBSERVED,
    )

    if source_url:
        engram.encoding_context.source_url = source_url
        _store.save_engram(engram)

    return (
        f"Ingested: {engram.id}\n"
        f"  Source: external{f' ({source_url})' if source_url else ''}\n"
        f"  Confidence: {engram.source.confidence}\n"
        f"  Connections: {len(engram.connections)} discovered\n"
        f"  Depth: {encoding_depth}\n"
        f"  Tags: {', '.join(engram.tags) or '(none)'}"
    )


@mcp.tool()
def mnemos_recall(
    query: str,
    max_results: int = 5,
    agent_id: str = "default",
) -> str:
    """Retrieve memories relevant to a query.

    Searches across all stored memories using full-text search and
    connection graph traversal. Results are scored by relevance,
    recency, strength, connections, and emotional congruence.

    Every recalled memory is reconsolidated — its connections and
    strength are updated based on this retrieval context.

    Args:
        query: What to search for. Natural language works best.
        max_results: Maximum number of results (default: 5).
        agent_id: Which agent's memory to search. Default: "default".
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    emotional_state = _store.get_latest_emotional_state(agent_id)  # type: ignore

    results = _retriever.retrieve(  # type: ignore
        cue=query,
        agent_id=agent_id,
        max_results=max_results,
        emotional_state=emotional_state,
    )

    if not results:
        return "No relevant memories found."

    lines = []
    for r in results:
        # Prefer impact (the lesson) over content (what happened)
        display = r.engram.impact if r.engram.impact else r.engram.content
        if len(display) > 150:
            display = display[:147] + "..."
        pct = int(r.engram.source.confidence * 100)
        lines.append(
            f"[{r.score:.2f}] {display}\n"
            f"       id={r.engram.id[:25]}... kind={r.engram.kind} confidence={pct}%"
        )

    return f"Found {len(results)} memories:\n\n" + "\n\n".join(lines)


@mcp.tool()
def mnemos_session_start(
    session_id: str = "",
    title: str = "",
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    source: str = "mcp",
) -> str:
    """Start or resume a functional-memory session.

    Call this near the beginning of a conversation, task, or work block. The
    returned session_id is the live working-memory scope for the agent.
    Default scope args inherit the server's configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    _reconcile_vault_on_session_start()
    session = _store.start_memory_session(  # type: ignore
        session_id=session_id or None,
        title=title,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        source=source,
    )
    return (
        f"Functional-memory session active: {session['id']}\n"
        f"  Title: {session.get('title') or '(untitled)'}\n"
        f"  Scope: {agent_id}/{person_id}/{project_scope}\n"
        f"  Status: {session['status']}"
    )


@mcp.tool()
def mnemos_functional_update(
    content: str,
    memory_id: str = "",
    session_id: str = "",
    memory_type: str = "working",
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    confidence: float = 0.65,
    salience: float = 0.5,
    needs_confirmation: bool = False,
    pinned: bool = False,
    source: str = "agent_observed",
    tags: str = "",
) -> str:
    """Write or revise functional memory for the current session/task.

    Use this for live task state, active preferences, open questions,
    corrections, commitments, and other context the agent should not lose
    during the current work block. Default scope args inherit the server's
    configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    try:
        entry = _store.write_functional_memory(  # type: ignore
            content,
            memory_id=memory_id or None,
            session_id=session_id or None,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            memory_type=memory_type,
            confidence=confidence,
            salience=salience,
            needs_confirmation=needs_confirmation,
            pinned=pinned,
            source=source,
            metadata={"tags": tag_list},
        )
    except ValueError as exc:
        return f"Functional memory update failed: {exc}"

    return "Functional memory updated:\n" + _format_functional_entry(entry)


@mcp.tool()
def mnemos_functional_list(
    query: str = "",
    session_id: str = "",
    memory_type: str = "",
    max_results: int = 12,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    needs_confirmation_only: bool = False,
) -> str:
    """List or search functional memory for a session/person/project scope.

    Default scope args inherit the server's configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    try:
        entries = _store.load_functional_memories(  # type: ignore
            query,
            session_id=session_id or None,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            memory_type=memory_type or None,
            needs_confirmation_only=needs_confirmation_only,
            limit=max_results,
        )
    except ValueError as exc:
        return f"Functional memory search failed: {exc}"
    if not entries:
        return "No functional memory entries found."

    lines = []
    for entry in entries:
        lines.append(f"[{entry['score']:.2f}] " + _format_functional_entry(entry))
    return f"Found {len(entries)} functional memory entries:\n\n" + "\n\n".join(lines)


@mcp.tool()
def mnemos_session_close(
    session_id: str,
    synthesis: str = "",
    promote_to_hypomnema: bool = True,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
) -> str:
    """Close a functional-memory session.

    By default, active functional memories are compressed into one hypomnema
    continuity note and removed from the live working set. Default scope args
    inherit the server's configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    try:
        if promote_to_hypomnema:
            result = _store.close_session_to_hypomnema(  # type: ignore
                session_id,
                synthesis=synthesis,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
            )
            hypomnema_id = result.get("hypomnema_id") or "(none)"
            return (
                f"Session closed: {session_id}\n"
                f"  Functional memories compressed: {result['functional_memories']}\n"
                f"  Hypomnema entry: {hypomnema_id}\n"
                f"  Continuity: {result['content'][:500]}"
            )

        session = _store.close_memory_session(session_id, status="closed")  # type: ignore
    except (KeyError, ValueError) as exc:
        return f"Session close failed: {exc}"
    if session is None:
        return f"Session not found: {session_id}"
    return f"Session closed without hypomnema promotion: {session_id}"


@mcp.tool()
def mnemos_context_packet(
    query: str,
    session_id: str = "",
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    token_budget: int = 3000,
    include_json: bool = False,
    packet_mode: str = "operational",
) -> str:
    """Build the complete memory context an agent should read before answering.

    This is the turnkey call for agent integrations: it combines functional
    memory, hypomnema, long-term Mnemos recall, beliefs, and review cues in
    the order an agent should reason over them. packet_mode="operational"
    withholds review prose; packet_mode="review" exposes candidate prose with
    provenance labels. Default scope args inherit the server's configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    try:
        packet = build_context_packet(
            _store,  # type: ignore
            query,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            session_id=session_id,
            token_budget=max(500, token_budget),
            include_prompt=True,
            packet_mode=packet_mode,
        )
    except ValueError as exc:
        return f"Context packet failed: {exc}"
    if include_json:
        return json.dumps(packet, indent=2, ensure_ascii=True, default=str)
    return packet["prompt"]


@mcp.tool()
def mnemos_review_queue(
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    max_results: int = 8,
) -> str:
    """Show memory items that need human review or promotion decisions.

    Default scope args inherit the server's configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    functional = _store.load_functional_memories(  # type: ignore
        "",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        needs_confirmation_only=True,
        limit=max_results,
    )
    candidates = _store.get_hypomnema_promotion_candidates(  # type: ignore
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_results,
        read_visibility=(READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW),
    )
    proposals = _store.list_proposals(  # type: ignore
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        status="pending_review",
        limit=max_results,
    )
    if not functional and not candidates and not proposals:
        return "Review queue is clear."

    lines = []
    if functional:
        lines.append("Functional memories needing confirmation:")
        lines.extend(_format_functional_entry(entry) for entry in functional)
    if candidates:
        if lines:
            lines.append("")
        lines.append("Hypomnema promotion candidates:")
        lines.extend(_format_hypomnema_entry(entry) for entry in candidates)
    if proposals:
        if lines:
            lines.append("")
        lines.append("Proposal candidates:")
        lines.extend(_format_proposal_entry(entry) for entry in proposals)
    return "\n".join(lines)


@mcp.tool()
def mnemos_proposal_audit(
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    max_results: int = 20,
) -> str:
    """Explicit audit/admin read of audit-only proposal ledger rows."""
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    proposals = _store.list_audit_proposals(  # type: ignore
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_results,
    )
    if not proposals:
        return "Proposal audit ledger has no audit-only rows for this scope."
    return "Audit-only proposal ledger rows:\n\n" + "\n\n".join(
        _format_proposal_entry(entry) for entry in proposals
    )


@mcp.tool()
def mnemos_visual_snapshot(
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    session_id: str = "",
    max_items: int = 6,
) -> str:
    """Generate an inline Markdown/Mermaid visual snapshot of memory state.

    Default scope args inherit the server's configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    return build_memory_visual_snapshot(
        _store,  # type: ignore
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        session_id=session_id,
        max_items=max(1, min(max_items, 12)),
    )


@mcp.tool()
def mnemos_hypomnema_write(
    content: str,
    source: str = "observed",
    domain: str = "topical",
    tags: str = "",
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    density: float = 0.5,
    confidence: float = 0.6,
    salience: float = 0.5,
    foundational: bool = False,
    related_session_id: str = "",
    related_engram_id: str = "",
) -> str:
    """Write a scoped hypomnema entry.

    Hypomnema is the durable continuity layer between functional session
    memory and Mnemos engrams. Use it for what an agent is "sitting with":
    stable-enough context that should survive sessions, stay scoped to a
    person/project relationship, and remain revisable before promotion.
    Default scope args inherit the server's configured scope.

    Args:
        content: Continuity note to preserve.
        source: "observed", "synthesized", or "co-formed".
        domain: "foundational", "identity", "recurring", "long-arc",
            "topical", or "situational".
        tags: Comma-separated tags.
        agent_id: Agent scope; default inherits the configured server agent.
        person_id: Person/relationship scope; default inherits the configured server person.
        project_scope: Project or workspace scope; default inherits the configured server project.
        density: How compressed the entry is (0.0 sparse, 1.0 dense).
        confidence: How reliable the entry is.
        salience: How important it is for future continuity.
        foundational: Whether this should anchor the relationship/model.
        related_session_id: Optional external session identifier.
        related_engram_id: Optional Mnemos engram this entry interprets.

    The caller-supplied domain may only escalate above the content classifier,
    never de-escalate below it. Underclaimed identity/foundational content is
    stored at the effective domain, routed to review, and recorded as a deduped
    pending proposal for the scoped content claim.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )

    # R2 (T3): the caller-supplied domain may only escalate, never de-escalate
    # below the classifier. A caller labelling identity-bearing content
    # "topical" cannot dodge review — the effective domain is the more-severe
    # of {caller, classifier}, and identity/foundational routes the write to
    # review_only via classify_hypomnema_read_visibility.
    # Normalize the caller domain ONCE so trailing whitespace ("topical ") is
    # not mistaken for an escalation (review finding r2-domain-claim-raw-domain).
    caller_domain = (domain or "").strip()
    classifier_domain = _classify_domain(content)
    effective_domain = escalate_domain(caller_domain, classifier_domain)
    domain_claim_detected = effective_domain != caller_domain

    # Finding B (T3, completion of D4): flood-prevention idempotency runs BEFORE
    # the write, scoped to the quarantine path. When an escalated write matches
    # an already-pending identical claim (scope + claimed domain + effective
    # domain + content), skip BOTH the duplicate hypomnema entry and the
    # duplicate proposal. Deduping only the proposal row is not enough — a
    # claim-spam loop still floods the review queue with duplicate review_only
    # hypomnema candidates, and the review queue is the gate's true attack
    # surface (D4's stated purpose governs its letter).
    claim_key = None
    if domain_claim_detected:
        # Key on the CANONICAL effective domain + content + scope — NOT the raw
        # caller-claimed domain (T3 review domain-claim-key-not-canonical). The
        # claimed label is caller-varied and unbounded; keying on it would let a
        # caller mint distinct claim IDs (topical/situational/bogus/whitespace)
        # for the same effective-domain+content and duplicate review rows,
        # bypassing D4 flood-prevention. The effective domain is harness-derived
        # and canonical, so same-effective+same-content collapses to one row.
        claim_key = hashlib.sha256(
            "\x00".join(
                [
                    "hypomnema_write",
                    agent_id,
                    person_id,
                    project_scope,
                    effective_domain,
                    # Normalize content in the key so leading/trailing whitespace
                    # variants collapse to one claim row rather than being used to
                    # bypass D4 flood-prevention (T3 review domain-claim-key-raw-content).
                    content.strip(),
                ]
            ).encode("utf-8")
        ).hexdigest()[:16]
        existing_claim = _store.get_proposal(f"domain-claim-{claim_key}")  # type: ignore
        if (
            existing_claim is not None
            and existing_claim.get("status") == "pending_review"
        ):
            return (
                "Duplicate quarantined domain claim; no new entry written.\n"
                f"  Existing continuity note ID: {existing_claim.get('target_id')}\n"
                f"  Domain: {effective_domain} (claimed {caller_domain}); already pending review"
            )

    try:
        entry_id = _store.write_hypomnema_entry(  # type: ignore
            content,
            # Finding B / review d4-domain-claim-idempotency-race: the quarantine
            # path uses a DETERMINISTIC entry_id derived from the claim key, so
            # two identical underclaimed writes that both slip past the pre-write
            # check still target the SAME row (SQLite serializes the UPSERT) —
            # one hypomnema entry, not a duplicate. Normal writes keep a fresh id.
            entry_id=(f"claim-{claim_key}" if claim_key is not None else None),
            source=source,
            domain=effective_domain,
            tags=tags,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            density=density,
            confidence=confidence,
            salience=salience,
            foundational=foundational,
            related_session_id=related_session_id or None,
            related_engram_id=related_engram_id or None,
        )
    except ValueError as exc:
        return f"Hypomnema write failed: {exc}"

    # D4 (T3): a caller domain claim below the classifier is evidence of risk,
    # not authority — record it as a pending_review proposal so the reviewer
    # sees the claim beside the harness truth, keyed by the same scoped claim_key
    # computed above (scope + claimed domain + effective domain + content). The
    # scope keeps a claim in another agent/person/project from overwriting this
    # row; entry_id is deliberately excluded (the surface mints a fresh entry_id
    # per call, so keying on it would defeat flood-prevention — the pre-write
    # idempotency above already prevents the duplicate entry).
    if domain_claim_detected:
        try:
            _store.write_proposal(  # type: ignore
                proposal_id=f"domain-claim-{claim_key}",
                source_authority=SourceAuthority.OBSERVED,
                kind="semantic",
                target_surface="hypomnema_entries",
                transition="hypomnema_write_domain_claim",
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                domain=effective_domain,
                blast_radius=(
                    "identity"
                    if effective_domain in ("identity", "foundational")
                    else "medium"
                ),
                read_visibility=READ_VISIBILITY_REVIEW,
                status="pending_review",
                reason=(
                    f"caller-claimed domain={caller_domain!r} on {classifier_domain}-classified "
                    f"content at mnemos_hypomnema_write; quarantined to review "
                    f"(effective domain={effective_domain!r})"
                ),
                target_id=entry_id,
                # Populate payload/provenance so the claim is visible in the
                # review queue, not buried in reason/target_id (T3 review
                # domain-claim-proposal-not-legible).
                payload={
                    "surface": "mnemos_hypomnema_write",
                    "claimed_domain": caller_domain,
                    "classifier_domain": classifier_domain,
                    "effective_domain": effective_domain,
                    "target_entry_id": entry_id,
                },
                provenance_ids=[entry_id],
            )
        except ValueError:
            # A proposal-ledger write failure must not lose the quarantined
            # hypomnema (already written at review tier); the claim record is
            # best-effort telemetry on top of the enforced routing.
            pass

    # Read back the just-written row to display its assigned visibility. This
    # is an admin read of the write's own result: a quarantined (review_only/
    # audit_only) write must still be able to report its tier, so opt into
    # unfiltered access (R5, T3/D8-A). This surfaces the tier to the caller;
    # it does not place the prose into any operational read path.
    entry = _store.get_hypomnema_entry(  # type: ignore
        entry_id,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        read_visibility=None,
    )
    visibility = entry["read_visibility"] if entry is not None else "unknown"

    # R2 (T3): report the effective (stored) domain, and when the caller's label
    # was escalated, name the override so the quarantine decision is legible
    # rather than hidden behind the caller's claimed label.
    if domain_claim_detected:
        domain_display = (
            f"{effective_domain} (escalated from {caller_domain}; routed to review)"
        )
    else:
        domain_display = effective_domain

    return (
        f"Hypomnema written: {entry_id}\n"
        f"  Scope: {agent_id}/{person_id}/{project_scope}\n"
        f"  Domain: {domain_display}\n"
        f"  Source: {source}\n"
        f"  Visibility: {visibility}\n"
        f"  Confidence: {confidence:.2f}\n"
        f"  Salience: {salience:.2f}"
    )


@mcp.tool()
def mnemos_hypomnema_search(
    query: str = "",
    max_results: int = 8,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    include_inactive: bool = False,
) -> str:
    """Search operational scoped hypomnema continuity entries.

    Default scope args inherit the server's configured scope.
    Review-only promotion candidates are excluded; use mnemos_review_queue for
    deliberate review inspection.

    Args:
        query: Optional natural-language query. Empty returns strongest entries.
        max_results: Maximum entries to return.
        agent_id: Agent scope; default inherits the configured server agent.
        person_id: Person/relationship scope; default inherits the configured server person.
        project_scope: Project or workspace scope; default inherits the configured server project.
        include_inactive: Include superseded entries if true.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    entries = _store.search_hypomnema(  # type: ignore
        query,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_results,
        include_inactive=include_inactive,
        exclude_promotion_candidates=True,
    )
    if not entries:
        return "No hypomnema entries found."

    lines = []
    for entry in entries:
        lines.append(f"[{entry['score']:.2f}] " + _format_hypomnema_entry(entry))
    return f"Found {len(entries)} hypomnema entries:\n\n" + "\n\n".join(lines)


@mcp.tool()
def mnemos_hypomnema_revise(
    entry_id: str,
    content: str,
    reason: str,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    confidence: float = -1.0,
    salience: float = -1.0,
) -> str:
    """Revise a hypomnema entry while preserving its prior version.

    Use this when scoped continuity is still true but needs sharper wording,
    corrected evidence, or a better compression. Default scope args inherit the
    server's configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    try:
        _store.revise_hypomnema_entry(  # type: ignore
            entry_id,
            content,
            reason=reason,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            confidence=confidence if confidence >= 0 else None,
            salience=salience if salience >= 0 else None,
            read_visibility=READ_VISIBILITY_OPERATIONAL,
        )
    except (KeyError, ValueError) as exc:
        return f"Hypomnema revision failed: {exc}"

    return f"Hypomnema revised: {entry_id}\n  Reason: {reason}"


@mcp.tool()
def mnemos_hypomnema_supersede(
    entry_id: str,
    content: str,
    reason: str,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
) -> str:
    """Supersede a hypomnema entry with a replacement entry.

    Use this when an old continuity note should stop participating in active
    retrieval but its audit trail should remain visible. Default scope args
    inherit the server's configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    try:
        new_id = _store.supersede_hypomnema_entry(  # type: ignore
            entry_id,
            content,
            reason=reason,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            read_visibility=READ_VISIBILITY_OPERATIONAL,
        )
    except (KeyError, ValueError) as exc:
        return f"Hypomnema supersession failed: {exc}"

    return (
        f"Hypomnema superseded: {entry_id}\n  Replacement: {new_id}\n  Reason: {reason}"
    )


@mcp.tool()
def mnemos_hypomnema_promote(
    entry_id: str,
    dry_run: bool = True,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
) -> str:
    """Promote stable hypomnema into a Mnemos engram.

    Promotion is explicit and dry-run by default because hypomnema is scoped
    continuity. The promoted engram is lightly de-identified and tagged as
    hypomnema/promoted/continuity. Promotion stamps observed source authority;
    hypomnema itself does not mint user_stated authority. Default scope args
    inherit the server's configured scope.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    entry = _store.get_hypomnema_entry(  # type: ignore
        entry_id,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        active_only=True,
        read_visibility=READ_VISIBILITY_OPERATIONAL,
    )
    if entry is None:
        return f"Active operational hypomnema entry not found: {entry_id}"

    deidentified = entry["content"].replace(person_id, "the collaborator")
    content = "[promoted from hypomnema; de-identified] " + deidentified
    if dry_run:
        return (
            f"Hypomnema promotion dry run: {entry_id}\n"
            f"  Would encode as Mnemos engram:\n  {content}"
        )

    engram = _encoder.encode(  # type: ignore
        content=content,
        impact="Stable scoped continuity promoted from hypomnema.",
        kind="semantic",
        tags=["hypomnema", "promoted", "continuity", project_scope],
        source=SourceType.USER_EXPLICIT,
        agent_id=agent_id,
        skip_surprise_detection=True,
        # Promotion cannot mint authority; hypomnema carries none under
        # Reading B, so promotion stamps observed (F2 ruling).
        source_authority=SourceAuthority.OBSERVED,
    )
    _store.mark_hypomnema_promoted(entry_id, engram.id)  # type: ignore
    return (
        f"Hypomnema promoted: {entry_id}\n"
        f"  Engram: {engram.id}\n"
        f"  Connections: {len(engram.connections)} discovered"
    )


@mcp.tool()
def mnemos_hypomnema_candidates(
    max_results: int = 10,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
) -> str:
    """List operational hypomnema entries that meet promotion thresholds.

    Default scope args inherit the server's configured scope.
    Review-only candidates remain pending in mnemos_review_queue.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    entries = _store.get_hypomnema_promotion_candidates(  # type: ignore
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_results,
    )
    if not entries:
        return "No hypomnema entries currently meet promotion thresholds."
    return f"{len(entries)} promotion candidates:\n\n" + "\n\n".join(
        _format_hypomnema_entry(entry) for entry in entries
    )


@mcp.tool()
def mnemos_inspect(engram_id: str) -> str:
    """View full details of a specific memory.

    Shows content, metadata, connections, version history, and
    the original content at encoding time.

    Args:
        engram_id: The memory ID to inspect.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    engram = _store.get_engram(  # type: ignore
        engram_id,
        read_visibility=READ_VISIBILITY_OPERATIONAL,
    )
    if engram is None:
        return f"Memory not found: {engram_id}"

    lines = [
        f"ID: {engram.id}",
        f"Content: {engram.content}",
        f"Impact: {engram.impact or '(not yet distilled)'}",
        f"Kind: {engram.kind}",
        f"Tags: {', '.join(engram.tags) or '(none)'}",
        f"State: {engram.state}",
        f"Resolution: {engram.resolution}",
        f"Strength: {engram.strength:.4f}",
        f"Stability: {engram.stability:.4f}{' (long-term)' if engram.stability >= 0.8 else ' (consolidating)' if engram.stability >= 0.5 else ''}",
        f"Accessibility: {engram.accessibility:.4f}",
        f"Confidence: {engram.source.confidence} ({engram.source.confidence_source})",
        f"Created: {engram.created_at}",
        f"Last accessed: {engram.last_accessed}",
        f"Access count: {engram.access_count}",
        f"Reconsolidations: {engram.reconsolidation_count}",
        f"Connections: {len(engram.connections)}",
    ]
    for c in engram.connections:
        lines.append(f"  → {c.target_id[:30]}... ({c.relation}, str={c.strength:.2f})")
    lines.append(f"Versions: {len(engram.versions)}")
    if engram.content != engram.content_at_encoding:
        lines.append(f"Original: {engram.content_at_encoding[:150]}...")

    return "\n".join(lines)


@mcp.tool()
def mnemos_introspect(text: str) -> str:
    """Audit a piece of text for "performed/groove" vs "genuine/reaching" markers.

    A metacognitive self-audit: scores how much of the text reads as template-driven
    pattern-completion versus genuine in-the-moment reasoning (sentence-length
    variance, hedge distribution, self-reference depth, embodied-vs-abstract language,
    clean-resolution detection, structural repetition). Pure analysis — reads no
    memory and writes nothing; safe to call any time on your own recent output.

    Args:
        text: The text to introspect (e.g. one of your own recent responses).
    """
    from .advanced.introspection import introspect

    if not (text or "").strip():
        return "Nothing to introspect (empty text)."
    return introspect(text).to_summary()


@mcp.tool()
def mnemos_status(
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
) -> str:
    """Get memory system status and statistics.

    Shows counts of active/dormant/archived memories, connections,
    beliefs, reconsolidation events, and accessibility distribution.

    Args:
        agent_id: Which agent's status to show. Default: "default".
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id, person_id, project_scope = _effective_scope(
        agent_id, person_id, project_scope
    )
    stats = _store.get_stats(  # type: ignore
        agent_id,
        person_id=person_id,
        project_scope=project_scope,
        read_visibility=READ_VISIBILITY_OPERATIONAL,
    )

    # Count long-term (stability >= 0.8) engrams
    active_engrams = _store.get_active_engrams(agent_id=agent_id, limit=10000)  # type: ignore
    longterm_count = sum(1 for e in active_engrams if e.stability >= 0.8)
    consolidating_count = sum(1 for e in active_engrams if 0.5 <= e.stability < 0.8)

    lines = [
        f"Mnemos Status (agent: {agent_id})",
        f"  Active engrams: {stats.get('engrams_active', 0)}",
        f"    Long-term (stability >= 0.8): {longterm_count}",
        f"    Consolidating (0.5-0.8): {consolidating_count}",
        f"  Dormant: {stats.get('engrams_dormant', 0)}",
        f"  Archived: {stats.get('archived', 0)}",
        f"  Connections: {stats.get('connections', 0)}",
        f"  Active beliefs: {stats.get('beliefs_active', 0)}",
        f"  Functional memory active: {stats.get('functional_active', 0)}",
        f"    Pinned: {stats.get('functional_pinned', 0)}",
        f"    Needs confirmation: {stats.get('functional_needs_confirmation', 0)}",
        f"    Active sessions: {stats.get('functional_sessions_active', 0)}",
        f"  Hypomnema active: {stats.get('hypomnema_active', 0)}",
        f"    Foundational: {stats.get('hypomnema_foundational', 0)}",
        f"    Promotion candidates: {stats.get('hypomnema_promotion_candidates', 0)}",
        f"    Promoted: {stats.get('hypomnema_promoted', 0)}",
        f"  Reconsolidations: {stats.get('reconsolidation_events', 0)}",
    ]
    if "accessibility_avg" in stats:
        lines.append(f"  Avg accessibility: {stats['accessibility_avg']:.3f}")

    es = _store.get_latest_emotional_state(agent_id)  # type: ignore
    if es:
        lines.append(
            f"  Emotional state: curiosity={es.curiosity:.1f} "
            f"clarity={es.clarity:.1f} warmth={es.warmth:.1f}"
        )

    return "\n".join(lines)


@mcp.tool()
def mnemos_beliefs(agent_id: str = "default", domain: str = "") -> str:
    """List reviewed current beliefs with confidence levels.

    Beliefs with ``confidence_pending_review`` or review-only visibility are
    hidden until the belief review pass opts in, clears pending state, and
    restores operational read visibility.

    Args:
        agent_id: Which agent's beliefs to show. Default: "default".
        domain: Filter by domain (e.g., "engineering", "social"). Empty = all.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    beliefs = _store.get_beliefs(  # type: ignore
        agent_id=agent_id,
        domain=domain or None,
        active_only=True,
    )

    if not beliefs:
        return "No active beliefs found."

    lines = []
    for b in beliefs:
        pct = int(b.confidence * 100)
        revisions = len(b.revision_history)
        lines.append(f"- {b.content} [{b.domain}, {pct}%, {revisions} revisions]")

    return f"{len(beliefs)} active beliefs:\n\n" + "\n".join(lines)


@mcp.tool()
def mnemos_shared(
    query: str = "",
    max_results: int = 10,
    agent_id: str = "default",
) -> str:
    """Get memories shared by other agents in the shared memory pool.

    Shows what other agents have learned, decided, built, or discovered.
    Use this to stay in sync with the team's shared knowledge.

    Args:
        query: Optional search query. If empty, returns most recent shared memories.
        max_results: Maximum number of results (default: 10).
        agent_id: Your agent ID (used for attribution, not filtering).
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    if not _shared_pool:
        return "Shared memory pool not initialized."

    shared = _shared_pool.get_shared(
        agent_id=agent_id,
        limit=max_results,
        query=query or None,
    )

    if not shared:
        return "No shared memories found."

    lines = []
    for engram in shared:
        display = engram.impact if engram.impact else engram.content
        if len(display) > 150:
            display = display[:147] + "..."
        pct = int(engram.source.confidence * 100)
        lines.append(
            f"[{engram.owner_agent_id}] {display}\n"
            f"       id={engram.id[:25]}... kind={engram.kind} confidence={pct}%"
        )

    return f"Found {len(shared)} shared memories:\n\n" + "\n\n".join(lines)


@mcp.tool()
def mnemos_forget(engram_id: str) -> str:
    """Archive a specific memory (soft delete).

    The memory moves to cold storage. It can be restored via resharpen
    if triggered by relevant context in the future.

    Args:
        engram_id: The memory ID to archive.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    engram = _store.get_engram(  # type: ignore
        engram_id,
        read_visibility=READ_VISIBILITY_OPERATIONAL,
    )
    if engram is None:
        return f"Memory not found: {engram_id}"

    _store.archive_engram(engram, reason="user_requested")  # type: ignore
    return f"Archived: {engram_id}\n  Content was: {engram.content[:100]}..."


@mcp.tool()
def mnemos_consolidate(deep: bool = False, agent_id: str = "default") -> str:
    """Run a memory consolidation cycle.

    Shallow cycle: decay + connection discovery (fast, ~1 second)
    Deep cycle: adds softening + belief review + reflection (slower, may use LLM)

    Args:
        deep: If true, run deep consolidation with all passes.
        agent_id: Which agent's memory to consolidate. Default: "default".
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    daemon = ConsolidationDaemon(
        store=_store,
        config={},
        llm_client=_llm_client,
        embedding_index=_embedding_index,
    )  # type: ignore
    stats = daemon.run_cycle(deep=deep, agent_id=agent_id)

    lines = [
        f"Consolidation complete ({stats.get('cycle_type', 'unknown')})",
        f"  Passes: {', '.join(stats.get('passes_run', []))}",
    ]

    if "decay" in stats:
        d = stats["decay"]
        lines.append(
            f"  Decay: {d.get('engrams_decayed', 0)} decayed, {d.get('engrams_archived', 0)} archived"
        )
    if "connection_discovery" in stats:
        cd = stats["connection_discovery"]
        lines.append(
            f"  Connections: {cd.get('connections_created', 0)} new, {cd.get('connections_strengthened', 0)} strengthened"
        )
    if "softening" in stats:
        lines.append(
            f"  Softened: {stats['softening'].get('engrams_softened', 0)} memories"
        )
    if "reflection" in stats:
        ref = stats["reflection"]
        lines.append(f"  Thoughts: {ref.get('thoughts_generated', 0)} generated")

    errors = [k for k in stats if k.endswith("_error")]
    for e in errors:
        lines.append(f"  ERROR: {e}: {stats[e]}")

    return "\n".join(lines)


def run_server(
    db_path: str = "~/.mnemos/memory.db",
    *,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> None:
    """Start the MCP server in stdio mode.

    Persist the configured scope so advanced tools can inherit it when callers
    leave their scope args at defaults.
    """
    _set_server_defaults(agent_id, person_id, project_scope)
    configure_runtime(
        db_path=db_path,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
    )

    def _shutdown(signum, frame):
        logger.info("Shutting down MCP server...")
        if _store:
            try:
                _store.close()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _init_store(db_path)
    logger.info("Mnemos MCP server starting (stdio mode)")
    mcp.run()
