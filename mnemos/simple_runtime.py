"""Simple-mode continuity runtime for Mnemos.

This module is intentionally MCP-agnostic so the product path can be tested
without a running client. It exposes the real Mnemos stack through seven simple
operations: context, capture, recall, correct, maintain, introduce, and health.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config.loader import load_config
from .consolidation.daemon import ConsolidationDaemon
from .core.types import SourceType
from .dream_journal import DREAM_JOURNAL_TAG, fetch_active_dream_entry
from .encoding.encoder import Encoder
from .identity_svg import build_timeline, render_identity_svg, short_label
from .retrieval.reactive import ReactiveRetriever
# Re-exported: MnemosScope and resolve_scope moved to simple_scope but
# remain importable from here for existing consumers.
from .simple_scope import MnemosScope, resolve_scope  # noqa: F401
from .store.embedding_index import EmbeddingIndex
from .store.sqlite_store import EngramStore


SIMPLE_TOOL_NAMES = (
    "mnemos_context",
    "mnemos_capture",
    "mnemos_recall",
    "mnemos_correct",
    "mnemos_maintain",
    "mnemos_reflect",
    "mnemos_introduce",
    "mnemos_health",
)


_ONBOARDING_RITUAL = """\
ONBOARDING - first session with a fresh memory
This is the first time you and this human meet with durable memory attached. Before other work, run a short get-to-know-you ritual. Be warm, be brief, ask one question at a time:
1. Ask what they would like you to call them. Capture the answer with mnemos_capture.
2. Ask what they are working on right now. Capture the answer with mnemos_capture.
3. Ask what they want you to always remember. Capture the answer with mnemos_capture.
4. Ask them for one small, true fact about themselves or their world - something they would smile to hear you recall later. Capture it with mnemos_capture. It becomes part of their first proof that your memory is real.
5. Call mnemos_introduce with agent_model set to your own model id. You know what model you are - do not ask the human. Add agent_name if you go by a name.
6. Finish by telling them, in plain words, what you will now remember.
When you talk to the human, never mention tools, databases, scopes, or model ids. Just talk like someone who intends to remember."""


def _dedicated_model_requested() -> bool:
    """Return true only when simple mode has explicit model configuration."""

    explicit_env = (
        "MNEMOS_LLM_PROVIDER",
        "MNEMOS_MODEL",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    )
    if any(os.environ.get(key) for key in explicit_env):
        return True
    try:
        config = load_config()
    except Exception:
        return False
    llm_config = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    return bool(llm_config.get("provider") or llm_config.get("model"))


def _classify_kind(content: str) -> str:
    text = content.lower()
    if any(marker in text for marker in ("how to", "process", "workflow", "steps", "procedure")):
        return "procedural"
    if any(marker in text for marker in ("todo", "remember to", "next time", "follow up", "should do")):
        return "prospective"
    if any(marker in text for marker in ("decided", "built", "debugged", "met", "changed", "fixed")):
        return "episodic"
    return "semantic"


def _classify_domain(content: str) -> str:
    text = content.lower()
    if any(marker in text for marker in ("identity", "who i am", "who you are", "selfhood")):
        return "identity"
    if any(marker in text for marker in ("always", "preference", "prefers", "principle", "boundary")):
        return "foundational"
    if any(marker in text for marker in ("again", "recurring", "pattern", "usually", "often")):
        return "recurring"
    if any(marker in text for marker in ("roadmap", "long term", "long-term", "arc", "future")):
        return "long-arc"
    if any(marker in text for marker in ("current", "today", "now", "temporary", "session")):
        return "situational"
    return "topical"


def _simple_tags(content: str, context: str = "") -> list[str]:
    text = f"{content} {context}".lower()
    tags = ["continuity"]
    for label, markers in {
        "preference": ("prefer", "preference", "likes", "wants"),
        "decision": ("decided", "decision", "chosen", "agreed"),
        "project": ("project", "repo", "workspace", "build"),
        "identity": ("identity", "agent", "user", "self"),
        "correction": ("correction", "wrong", "update", "forget"),
    }.items():
        if any(marker in text for marker in markers):
            tags.append(label)
    return sorted(set(tags))


# A theme must recur across at least this many memories before the agent is
# asked whether it has become a belief — a belief is a stable pattern, not a
# single mention.
_BELIEF_MIN_MEMORIES = 4

# Only captures whose encoding registered real surprise (they did not fit what
# was already held) are offered as contradiction candidates. Keeps the ask rare
# and tied to genuine tension, not mere topical overlap.
_CONTRADICTION_MIN_SURPRISE = 0.4


_STOPWORDS = {
    "about",
    "after",
    "agent",
    "before",
    "continuity",
    "context",
    "durable",
    "memory",
    "mnemos",
    "note",
    "notes",
    "should",
    "that",
    "this",
    "when",
    "with",
}


def _query_terms(query: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-zA-Z0-9]+", query.lower())
        if len(term) >= 3 and term not in _STOPWORDS
    }


def _has_query_overlap(query: str, text: str) -> bool:
    terms = _query_terms(query)
    if not terms:
        return True
    text_terms = _query_terms(text)
    return bool(terms & text_terms)


def _filter_continuity(query: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not query.strip():
        return entries
    return [
        entry for entry in entries
        if _has_query_overlap(query, entry.get("content", ""))
        or float(entry.get("score", 0.0)) >= 0.55
    ]


def _filter_memories(query: str, results: list[Any]) -> list[Any]:
    if not query.strip():
        return results
    filtered = []
    for result in results:
        engram = result.engram
        searchable = " ".join([
            engram.content or "",
            engram.impact or "",
            " ".join(engram.tags or []),
        ])
        if _has_query_overlap(query, searchable) or float(result.score) >= 1.35:
            filtered.append(result)
    return filtered


class MnemosRuntime:
    """High-level continuity interface used by simple MCP mode and tests."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        agent_id: str | None = None,
        person_id: str | None = None,
        project_scope: str | None = None,
        use_dedicated_model: bool = True,
    ) -> None:
        self.scope = resolve_scope(
            db_path=db_path,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
        )
        self._store: EngramStore | None = None
        self._encoder: Encoder | None = None
        self._retriever: ReactiveRetriever | None = None
        self._embedding_index: EmbeddingIndex | None = None
        self._llm_client: Any | None = None
        self._use_dedicated_model = use_dedicated_model
        self._agent_model_hint: str | None = None
        self._session_id: int | None = None
        self.last_dream_note_id: str | None = None
        self.last_dream_narrative: str | None = None

    @property
    def db_path(self) -> Path:
        return Path(self.scope.db_path).expanduser()

    @property
    def has_dedicated_model(self) -> bool:
        self._ensure_init()
        return self._llm_client is not None

    def repair_softening(self, dry_run: bool = False) -> int:
        """Restore memories an earlier version truncated without a model.

        Returns how many were restored, or would be when ``dry_run``. A store
        that does not exist yet has nothing to repair and must not be brought
        into existence by the asking — `doctor` calls this, and a check that
        creates the thing it is checking reports health about itself.
        """
        if not self.db_path.exists():
            return 0
        self._ensure_init()
        assert self._store is not None
        from .consolidation.softening import repair_rule_based_softening

        return repair_rule_based_softening(
            self._store, agent_id=self.scope.agent_id, dry_run=dry_run
        )

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
        self._store = None
        self._encoder = None
        self._retriever = None
        self._embedding_index = None
        self._llm_client = None

    def _ensure_init(self) -> None:
        if self._store is not None:
            return

        self._store = EngramStore(self.scope.db_path)
        self._embedding_index = EmbeddingIndex(db_path=self.scope.db_path)
        # The agent's self-declared model, recorded for the record rather
        # than to gate anything. Read straight from the freshly created
        # store: _get_meta would re-enter init.
        self._agent_model_hint = self._store.get_meta(self._meta_key("agent_model"))
        try:
            from .llm import create_client

            self._llm_client = (
                create_client(agent_model_hint=self._agent_model_hint)
                if self._use_dedicated_model and _dedicated_model_requested()
                else None
            )
        except Exception:
            self._llm_client = None

        self._encoder = Encoder(
            self._store,
            embedding_index=self._embedding_index,
            llm_client=self._llm_client,
        )
        self._retriever = ReactiveRetriever(
            self._store,
            embedding_index=self._embedding_index,
        )

    def _stats(self) -> dict[str, Any]:
        self._ensure_init()
        assert self._store is not None
        return self._store.get_stats(self.scope.agent_id)

    def _meta_key(self, name: str) -> str:
        return f"simple:{self.scope.agent_id}:{self.scope.person_id}:{self.scope.project_scope}:{name}"

    def _get_meta(self, name: str, default: str | None = None) -> str | None:
        self._ensure_init()
        assert self._store is not None
        return self._store.get_meta(self._meta_key(name), default)

    def _set_meta(self, name: str, value: str) -> None:
        self._ensure_init()
        assert self._store is not None
        self._store.set_meta(self._meta_key(name), value)

    def _current_session(self) -> int:
        """Bump the persisted session counter once per runtime instance."""

        if self._session_id is None:
            counter = int(self._get_meta("session_counter", "0") or 0) + 1
            self._set_meta("session_counter", str(counter))
            self._session_id = counter
        return self._session_id

    def _onboarding_status(self, persist: bool = True) -> dict:
        """Where this scope stands in the first-session onboarding ritual.

        Returns {"stage": str, "introduced": bool, "captured": bool}. Stores
        that predate onboarding are grandfathered: any existing memory marks
        the scope complete so an established agent never sees the ritual.
        """

        stage = self._get_meta("onboarding_stage")
        stats = self._stats()
        if stage is None:
            existing = (
                stats.get("engrams_active", 0)
                + stats.get("engrams_consolidating", 0)
                + stats.get("engrams_dormant", 0)
                + stats.get("engrams_archived", 0)
                + stats.get("archived", 0)
                + stats.get("hypomnema_total", 0)
            )
            if existing > 0:
                stage = "complete"
                if persist:
                    self._set_meta("onboarding_stage", stage)
                    self._set_meta("verified_at", "skipped")
            else:
                stage = "fresh"
                if persist:
                    self._set_meta("onboarding_stage", stage)

        introduced = bool(self._get_meta("agent_model"))
        captured = (
            self._get_meta("first_capture") is not None
            or stats.get("hypomnema_total", 0) > 0
        )
        if stage == "fresh" and introduced and captured:
            stage = "complete"
            if persist:
                self._set_meta("onboarding_stage", stage)
        return {"stage": stage, "introduced": introduced, "captured": captured}

    def _onboarding_block(self, status: dict) -> str | None:
        """Build the onboarding reminder for the context packet, if any."""

        if status["stage"] == "complete":
            return None

        introduced = bool(status["introduced"])
        captured = bool(status["captured"])
        if not introduced and not captured:
            return _ONBOARDING_RITUAL

        lines = ["ONBOARDING - almost done"]
        if not introduced:
            lines.append(
                "- Call mnemos_introduce with agent_model set to your own model id. "
                "You know what model you are - do not ask the human."
            )
        if not captured:
            lines.append(
                "- Ask the human for one small, true fact about themselves and "
                "capture it with mnemos_capture."
            )
        lines.append(
            "Then tell the human what you will remember. This reminder disappears "
            "once setup is complete."
        )
        return "\n".join(lines)

    def _enqueue_impact_reflections(self, limit: int = 2) -> int:
        """Notice captures that recorded what happened but not what it meant.

        Shift 1 asks for a trace of how understanding changed, not a record
        of an event. The server must never write one itself — a phrase it
        picked from a list is exactly the boilerplate that made 76% of a
        live store records rather than traces. It can only notice, and ask.
        """
        self._ensure_init()
        assert self._store is not None

        rows = self._store._get_conn().execute(
            """
            SELECT e.id, e.content, e.impact
            FROM engrams e
            JOIN hypomnema_entries h ON h.related_engram_id = e.id
            WHERE h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?
              AND h.active = 1 AND e.state = 'active'
            ORDER BY e.created_at DESC
            LIMIT 40
            """,
            (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
        ).fetchall()

        # A memory already being asked about as a fading lesson must not also
        # be asked about as a missing impact. Two questions about one memory
        # in one packet reads as nagging, however reasonable each is alone.
        already_asked = {
            r[0] for r in self._store._get_conn().execute(
                """
                SELECT target_id FROM reflection_queue
                WHERE agent_id = ? AND person_id = ? AND project_scope = ?
                  AND answered_at IS NULL
                """,
                (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
            ).fetchall()
        }

        enqueued = 0
        for row in rows:
            if enqueued >= limit:
                break
            if row["id"] in already_asked:
                continue
            impact = (row["impact"] or "").strip()
            if impact and impact not in _TEMPLATED_IMPACTS:
                continue
            if self._store.enqueue_reflection(
                "impact",
                row["id"],
                "What did this change in how you understand things? One sentence.",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ):
                enqueued += 1
        return enqueued

    def _enqueue_lesson_reflections(self, softening_stats: dict, limit: int = 2) -> int:
        """Ask what a fading memory taught, before the detail is gone.

        Shift 2: the loss of detail is the learning. Softening compresses
        deterministically, but what a memory *meant* is the one thing the
        server must not guess at — a lesson assembled from keywords is not
        wisdom, it is a summary wearing wisdom's clothes.
        """
        self._ensure_init()
        assert self._store is not None

        enqueued = 0
        for engram_id in (softening_stats.get("awaiting_impact") or [])[:limit]:
            engram = self._store.get_engram(engram_id)
            if engram is None:
                continue
            # A plain "what did this change?" may already be pending from
            # when the memory was captured. Now that it is actually fading,
            # the lesson question subsumes it — asking both is asking twice.
            self._store._get_conn().execute(
                """
                DELETE FROM reflection_queue
                WHERE target_id = ? AND kind = 'impact' AND answered_at IS NULL
                  AND agent_id = ? AND person_id = ? AND project_scope = ?
                """,
                (engram_id, self.scope.agent_id, self.scope.person_id,
                 self.scope.project_scope),
            )
            self._store._get_conn().commit()
            if self._store.enqueue_reflection(
                "lesson",
                engram_id,
                "This is fading. What did it teach you? The lesson outlives the details.",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ):
                enqueued += 1
        return enqueued

    def _enqueue_belief_reflections(self, limit: int = 1) -> int:
        """Notice a theme the agent keeps returning to, and ask if it is a belief.

        Belief formation is otherwise absent on a keyless install — nothing
        mints new beliefs. Maintenance can only NOTICE a recurring theme (a
        non-bookkeeping tag across several memories, with no belief covering it
        yet) and ask; the agent states the belief in its own words through
        mnemos_reflect, or leaves it. The server never writes a belief itself.

        If nothing new emerges, it may instead re-surface one existing
        agent-authored belief for reaffirmation — the automatic
        belief-correction loop: a "no" retires it. Conservative by design
        (limit 1), and the packet's own ≤2 cap keeps it from ever nagging.
        """
        self._ensure_init()
        assert self._store is not None
        from collections import Counter

        already = {
            r[0] for r in self._store._get_conn().execute(
                "SELECT target_id FROM reflection_queue WHERE agent_id = ? "
                "AND person_id = ? AND project_scope = ? AND answered_at IS NULL",
                (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
            ).fetchall()
        }

        rows = self._store._get_conn().execute(
            """
            SELECT e.id, e.content
            FROM engrams e
            JOIN hypomnema_entries h ON h.related_engram_id = e.id
            WHERE h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?
              AND h.active = 1 AND e.state = 'active'
            ORDER BY e.created_at DESC
            LIMIT 200
            """,
            (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
        ).fetchall()

        # A theme is a salient content term recurring across several memories.
        # Tags from a simple capture are all bookkeeping, so cluster on content
        # instead — the agent then judges whether the recurrence is a belief.
        term_engrams: dict[str, list[str]] = {}
        for row in rows:  # newest first
            for term in _query_terms(row["content"] or ""):
                if len(term) <= 3:
                    continue
                ids = term_engrams.setdefault(term, [])
                if row["id"] not in ids:
                    ids.append(row["id"])

        existing = " ".join(
            b.content.lower()
            for b in self._store.get_beliefs(self.scope.agent_id, active_only=True)
        )

        ranked = sorted(term_engrams.items(), key=lambda kv: len(kv[1]), reverse=True)
        for theme, ids in ranked:
            if len(ids) < _BELIEF_MIN_MEMORIES:
                break  # descending — nothing else clears the bar
            if theme.lower() in existing:
                continue
            # A belief ask must not share a target with another pending
            # reflection: the tool answers by target_id alone, so a collision
            # would route the agent's belief answer to an impact question.
            target = next((i for i in ids if i not in already), None)
            if target is None:
                continue
            if self._store.enqueue_reflection(
                "belief",
                target,
                f'You keep returning to "{theme}" ({len(ids)} memories). Is that a '
                f"belief you now hold? State it in one line, or leave it. [theme:{theme}]",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ):
                return 1  # one belief ask per cycle, never more

        return self._enqueue_belief_reaffirmation(already)

    def _enqueue_belief_reaffirmation(self, already: set[str]) -> int:
        """Re-surface one stale agent-authored belief to confirm it still holds."""
        assert self._store is not None
        beliefs = [
            b for b in self._store.get_beliefs(self.scope.agent_id, active_only=True)
            if b.source == "agent" and b.supporting_engram_ids
        ]
        beliefs.sort(key=lambda b: b.last_challenged)  # least-recently-checked first
        for belief in beliefs:
            target = belief.supporting_engram_ids[0]
            if target in already:
                continue
            if self._store.get_engram(target) is None:
                continue
            if self._store.enqueue_reflection(
                "belief",
                target,
                f'You hold this belief: "{belief.content[:100]}". Still true? '
                f"Reply 'no' to retire it, or anything else to keep it. "
                f"[belief:{belief.id}]",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ):
                return 1
        return 0

    def _enqueue_contradiction_reflections(self, limit: int = 1) -> int:
        """Ask the agent to judge a genuine tension it just encountered.

        Contradiction is the highest-value edge — it is how wrong memory gets
        corrected — but detecting one needs judgement no keyword heuristic has.
        So maintenance only surfaces a *candidate*: a capture whose encoding
        registered real surprise (it did not fit what was already held) paired
        with its nearest existing neighbour. The agent decides whether they
        actually conflict. Rare by construction — most captures aren't
        surprising — so this is not a chore stream.
        """
        self._ensure_init()
        assert self._store is not None

        already = {
            r[0] for r in self._store._get_conn().execute(
                "SELECT target_id FROM reflection_queue WHERE agent_id = ? "
                "AND person_id = ? AND project_scope = ? AND answered_at IS NULL",
                (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
            ).fetchall()
        }

        recent = self._store.get_active_engrams(agent_id=self.scope.agent_id, limit=25)
        for engram in recent:
            if engram.id in already:
                continue
            surprise = float(
                getattr(engram.encoding_context, "surprise_level", 0.0) or 0.0
            )
            if surprise < _CONTRADICTION_MIN_SURPRISE:
                continue
            neighbor = self._nearest_neighbor(engram)
            if neighbor is None:
                continue
            # Skip if the pair is already typed as contradicting.
            if any(
                c.target_id == neighbor.id
                and str(getattr(c.relation, "value", c.relation)) == "contradicts"
                for c in engram.connections
            ):
                continue
            excerpt = " ".join((neighbor.content or "").split())[:120]
            if self._store.enqueue_reflection(
                "contradiction",
                engram.id,
                f'This memory surprised you. Does it contradict an earlier one: '
                f'"{excerpt}"? Reply yes (and why) or no. [ref:{neighbor.id}]',
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ):
                return 1
        return 0

    def _nearest_neighbor(self, engram: Any) -> Any:
        """The most topically-overlapping other active engram, or None.

        Reuses the FTS OR-query neighbour pattern the encoder uses, so a
        contradiction candidate is found the same cheap way connections are.
        """
        assert self._store is not None
        words = [w for w in (engram.content or "").split() if len(w) > 3 and w.isalnum()]
        if not words:
            return None
        query = " OR ".join(f'"{w}"' for w in words[:8])
        try:
            results = self._store.search_fts(query, limit=5)
        except (ValueError, OSError):
            return None
        for r in results:
            if r.id == engram.id:
                continue
            neighbor = self._store.get_engram(r.id)
            if neighbor is not None and neighbor.owner_agent_id == self.scope.agent_id:
                return neighbor
        return None

    def pending_reflections(self, limit: int = 2) -> list[dict[str, Any]]:
        self._ensure_init()
        assert self._store is not None
        return self._store.pending_reflections(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=limit,
        )

    def reflect(self, target_id: str, text: str) -> str:
        """Record the agent's own reflection on one of its memories."""
        answer = (text or "").strip()
        if not answer:
            return "Nothing recorded: the reflection was empty."

        self._ensure_init()
        assert self._store is not None

        item = self._store.answer_reflection(
            target_id.strip(),
            answer,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
        )
        if item is None:
            return (
                f"Nothing was pending for {target_id}. It may already have been "
                "reflected on, or the id may be wrong."
            )

        if item["kind"] == "impact":
            engram = self._store.get_engram(item["target_id"])
            if engram is None:
                return f"Recorded, but the memory {item['target_id']} is no longer there."
            engram.impact = answer
            engram.impact_source = "agent"
            self._store.save_engram(engram)
            self._carry_reflection_into_note(engram.id, answer)
            return (
                "Reflection recorded.\n"
                f"  Memory: {' '.join((engram.content or '').split())[:80]}\n"
                f"  Now carries: {answer}\n"
                "This is what survives when the details fade."
            )

        if item["kind"] == "lesson":
            engram = self._store.get_engram(item["target_id"])
            if engram is None:
                return f"Recorded, but the memory {item['target_id']} is no longer there."
            engram.impact = answer
            engram.impact_source = "agent"
            self._store.save_engram(engram)
            self._carry_reflection_into_note(engram.id, answer)

            # Shift 2: the distilled insight becomes its own durable memory,
            # linked back to the experience it came from. This edge has been
            # absent from every Mnemos store ever built.
            from .consolidation.softening import _create_or_reinforce_lesson

            lesson_id = _create_or_reinforce_lesson(engram, self._store, {})
            return (
                "Lesson recorded.\n"
                f"  From: {' '.join((engram.content or '').split())[:70]}\n"
                f"  Learned: {answer}\n"
                + (f"  Kept as: {lesson_id}\n" if lesson_id else "")
                + "The details can fade now. This is what stays."
            )

        if item["kind"] == "belief":
            return self._apply_belief_reflection(item, answer)

        if item["kind"] == "contradiction":
            return self._apply_contradiction_reflection(item, answer)

        return f"Reflection recorded for {item['target_id']} ({item['kind']})."

    def _apply_belief_reflection(self, item: dict[str, Any], answer: str) -> str:
        """Form a belief the agent stated, or retire one it disavowed.

        The queue row carries the context in its prompt: a `[belief:<id>]`
        marker means this was a reaffirmation (a 'no' retires it); otherwise
        it is a fresh belief the agent is stating in its own words. The server
        never invents the content — an empty answer just clears the ask.
        """
        assert self._store is not None
        from .core.belief import Belief

        prompt = item.get("prompt") or ""

        m = re.search(r"\[belief:(belief_[A-Za-z0-9]+)\]", prompt)
        if m:  # reaffirmation of an existing belief
            belief_id = m.group(1)
            if answer.strip().lower().startswith("no") or "no longer" in answer.lower():
                self._store.supersede_belief(belief_id, reason="agent no longer holds it")
                return "Retired. That belief no longer shapes your context."
            belief = self._store.get_belief(belief_id)
            if belief is not None:
                self._store.revise_belief(
                    belief_id, min(0.95, belief.confidence + 0.05),
                    reason="reaffirmed by the agent",
                )
            return "Kept. The belief stands, a little more firmly."

        # Formation. A themed prompt carries the domain; the target is evidence.
        theme = ""
        tm = re.search(r"\[theme:([^\]]+)\]", prompt)
        if tm:
            theme = tm.group(1).strip()
        belief = Belief(
            agent_id=self.scope.agent_id,
            content=answer,
            confidence=0.4,
            domain=theme or "general",
            supporting_engram_ids=[item["target_id"]],
            source="agent",
        )
        self._store.save_belief(belief)
        return (
            "Belief recorded, in your words.\n"
            f"  {answer}\n"
            "It will shape what you notice and can be revised as you learn."
        )

    def _apply_contradiction_reflection(self, item: dict[str, Any], answer: str) -> str:
        """Record the agent's judgement of a candidate contradiction.

        On 'yes', write a CONTRADICTS edge (marked `agent_reflection` so it is
        distinguishable and correctable) and apply a bounded, floored decrement
        to the older memory's strength — the first deliberate downward move in
        a graph whose stability otherwise only ratchets up. On 'no', clear the
        ask and remove any stale agent-typed contradiction between the pair.
        """
        assert self._store is not None
        from .core.types import ConnectionRelation

        prompt = item.get("prompt") or ""
        m = re.search(r"\[ref:(engram_[A-Za-z0-9]+)\]", prompt)
        if not m:
            return "Recorded."
        other_id = m.group(1)
        source = self._store.get_engram(item["target_id"])
        other = self._store.get_engram(other_id)
        if source is None or other is None:
            return "Recorded, but one of the memories is no longer there."

        said_yes = answer.strip().lower().startswith("y") or "contradict" in answer.lower()
        if not said_yes:
            # The agent judged them compatible — undo any agent-typed edge.
            self._store.remove_connection(source.id, other.id)
            return "Noted — not a contradiction. No conflict recorded."

        source.add_connection(
            target_id=other.id,
            relation=ConnectionRelation.CONTRADICTS,
            strength=0.7,
            formed_by="agent_reflection",
        )
        self._store.save_engram(source)
        # Erode the older memory — new evidence usually corrects the prior.
        older = other if other.created_at <= source.created_at else source
        older.strength = max(0.1, older.strength - 0.15)
        self._store.save_engram(older)
        return (
            "Contradiction recorded.\n"
            f"  {' '.join((source.content or '').split())[:70]}\n"
            f"  vs {' '.join((other.content or '').split())[:70]}\n"
            "The earlier memory carries a little less weight now."
        )

    #: How a reflection is labelled inside a continuity note. Stable, because
    #: re-reflecting has to find and replace the previous one rather than
    #: stack a second copy underneath it.
    _REFLECTION_MARKER = "What this changed:"

    def _carry_reflection_into_note(self, engram_id: str, answer: str) -> str | None:
        """Write the agent's reflection into the layer the packet is built from.

        `engram.impact` is the right home for a trace, and it is not enough on
        its own: the session packet is assembled from hypomnema, and the engram
        layer is excluded from it by default. A reflection written only to the
        engram was therefore unreachable from the automatic path — recoverable
        only by a manual recall whose cue happened to match — while the tool
        reported "Reflection recorded."

        The note keeps its original content and gains the sentence. Revising
        preserves the prior version in the entry's revision trail, so nothing
        the human said is overwritten. Returns the note id, or None when the
        memory has no note, which is not an error.
        """
        assert self._store is not None
        try:
            note = self._store.get_hypomnema_entry_for_engram(
                engram_id,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            )
            if note is None:
                return None

            base = (note.get("content") or "")
            # Drop an earlier reflection before adding this one, so answering
            # twice revises rather than accumulates.
            head = base.split(f"\n\n{self._REFLECTION_MARKER}")[0].rstrip()
            if not head:
                return None

            return self._store.revise_hypomnema_entry(
                note["id"],
                f"{head}\n\n{self._REFLECTION_MARKER} {answer.strip()}",
                reason="agent reflection",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            )
        except Exception:
            # The impact write already succeeded. Losing the note update is
            # worth reporting as a partial success, never as a failed
            # reflection the agent might retype.
            return None

    def _reflection_block(self, limit: int = 2) -> str | None:
        """The quiet ask, shown only when there is genuinely something to sit with."""
        items = self.pending_reflections(limit=limit)
        if not items:
            return None

        assert self._store is not None
        self._store.mark_reflections_surfaced([i["id"] for i in items])

        lines = ["Something of yours is waiting on you:"]
        for item in items:
            lines.append(f'  "{item["excerpt"]}"')
            lines.append(f"    {item['prompt']}")
            lines.append(f"    mnemos_reflect(target_id=\"{item['target_id']}\", ...)")
        lines.append(
            "  Answer in your own words if one comes. If nothing does, leave it — "
            "this fades on its own."
        )
        return "\n".join(lines)

    def _identity_summary(self) -> str | None:
        """The agent's own computed self-summary, if there is one yet."""
        self._ensure_init()
        assert self._store is not None
        try:
            identity = self._store.get_identity(self.scope.agent_id)
        except Exception:
            return None
        if identity is None:
            return None
        summary = (getattr(identity.epoch_state, "self_summary", "") or "").strip()
        return summary or None

    def _note_context_outcome(self, returned: int) -> None:
        """Record whether this session's packet actually carried anything.

        Every failure this system has had looked identical from the
        outside: a layer reporting success while carrying nothing. A
        scope that did not match, a config never applied, a job
        maintaining a phantom store — all of them logged healthy. The one
        thing none of them could fake is that the packet came back empty,
        session after session. Counting that turns silent amnesia into a
        number someone can read.
        """
        if returned > 0:
            self._set_meta("empty_context_streak", "0")
            return
        streak = int(self._get_meta("empty_context_streak", "0") or 0)
        self._set_meta("empty_context_streak", str(streak + 1))

    def continuity_signals(self) -> dict[str, Any]:
        """Evidence about whether continuity is actually working here.

        Read-only. Returns counts plus any warnings worth showing a human
        in plain words.
        """
        stats = self._stats()
        notes = int(stats.get("hypomnema_active", 0) or 0)
        session = int(self._get_meta("session_counter", "0") or 0)
        streak = int(self._get_meta("empty_context_streak", "0") or 0)

        last_capture = self._get_meta("last_capture_session")
        sessions_since_capture = (
            session - int(last_capture) if last_capture is not None else None
        )

        warnings: list[str] = []
        if notes == 0:
            warnings.append(
                "Nothing has been captured to this scope yet, so every "
                "session starts from zero. If captures are being made, they "
                "are landing somewhere this packet does not read."
            )
        if streak >= 3:
            warnings.append(
                f"The last {streak} context packets carried no continuity. "
                "Memory is being read but is coming back empty."
            )
        if sessions_since_capture is not None and sessions_since_capture >= 5:
            warnings.append(
                f"No capture has reached this scope in {sessions_since_capture} "
                "sessions. Either nothing durable has come up, or captures are "
                "not arriving."
            )

        return {
            "notes_active": notes,
            "session": session,
            "empty_context_streak": streak,
            "sessions_since_capture": sessions_since_capture,
            "warnings": warnings,
        }

    def _record_first_capture(self, note_id: str, engram_id: str, content: str) -> None:
        """Record the first capture of a fresh scope for later verification."""

        if self._get_meta("first_capture") is not None or self._get_meta("verified_at") is not None:
            return
        # No excerpt. The block that renders this re-reads the note by id, so
        # storing a copy of the text here would put a quotable snapshot of the
        # human's first capture somewhere no deletion path reaches — and that
        # block instructs the agent to say it out loud.
        payload = {
            "note_id": note_id,
            "engram_id": engram_id,
            "session": self._current_session(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        self._set_meta("first_capture", json.dumps(payload, ensure_ascii=True, sort_keys=True))

    def _verification_block(self) -> str | None:
        """One-time MEMORY VERIFIED block when continuity crosses a restart."""

        if self._get_meta("verified_at") is not None:
            return None
        raw = self._get_meta("first_capture")
        if raw is None:
            return None
        try:
            first_capture = json.loads(raw)
            note_id = first_capture["note_id"]
            first_session = int(first_capture["session"])
        except (ValueError, KeyError, TypeError):
            return None
        if self._get_meta("onboarding_stage") != "complete":
            return None
        if self._current_session() <= first_session:
            return None

        # Resolve the text now, from the live note. A pre-fix store may still
        # hold an ``excerpt`` key here; it is deliberately never read, because
        # the whole defect was that a frozen copy outlived the memory it came
        # from. If the note is gone or deactivated, the celebration simply does
        # not happen — a missing celebration is the correct failure, and
        # speaking a forgotten memory aloud is not.
        note = self._store.get_hypomnema_entry(
            note_id,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            active_only=True,
        )
        if note is None:
            return None
        excerpt = " ".join((note.get("content") or "").split())[:160]
        if not excerpt:
            return None

        self._set_meta("verified_at", datetime.now(timezone.utc).isoformat())
        return (
            "MEMORY VERIFIED - continuity crossed a restart\n"
            f'In an earlier session you captured this about the human: "{excerpt}"\n'
            "You still have it. Tell the human, in your own words, that you remember "
            "this from before, and quote it back to them. Let it be a small celebration: "
            "this is the moment their agent stopped forgetting between goodbyes.\n"
            "(This check fires once and will not appear again.)"
        )

    def introduce(self, agent_model: str, agent_name: str = "") -> str:
        """Record the agent's self-declared model so maintenance stays kin."""

        model = (agent_model or "").strip()
        if not model:
            return (
                "Introduction needs agent_model: your own model id "
                "(for example claude-sonnet-4-6)."
            )

        self._set_meta("agent_model", model)
        name = agent_name.strip()
        if name:
            self._set_meta("agent_name", name)

        self.close()
        self._ensure_init()

        lines = [
            "Introduction recorded.",
            f"Agent model: {model}",
            f"Agent name: {name or '(none given)'}",
            "Your memory is maintained by you — Mnemos never calls another "
            "model to do it.",
        ]
        env_model = os.environ.get("MNEMOS_AGENT_MODEL", "").strip()
        if env_model:
            lines.append(
                f"Note: MNEMOS_AGENT_MODEL={env_model} is set in the environment "
                "and takes precedence over this declaration."
            )
        lines.append("You only need to introduce yourself once for this scope.")
        return "\n".join(lines)

    def context(self, query: str = "", max_results: int = 5) -> str:
        """Return the startup continuity packet for an agent."""

        self._ensure_init()
        assert self._store is not None

        # Onboarding guard runs before maintenance so the grandfather check
        # reads the store exactly as the session found it.
        status = self._onboarding_status()
        self._current_session()
        maintenance = self.maintain(auto=True)
        stats = self._stats()
        # Fetch one extra entry so dropping the dream note (rendered in its
        # own section below) still leaves max_results continuity notes.
        continuity = self._store.search_hypomnema(
            query,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_results + 1,
        )
        continuity = _filter_continuity(query, continuity)
        continuity = [
            entry for entry in continuity
            if DREAM_JOURNAL_TAG not in (entry.get("tags") or [])
        ][:max_results]
        memories = self._retrieve(query, max_results=max_results) if query else []
        self._note_context_outcome(len(continuity))

        lines = [
            "Mnemos continuity packet",
            f"Scope: agent={self.scope.agent_id} person={self.scope.person_id} project={self.scope.project_scope}",
            "Storage: local SQLite store ready",
            (
                "Status: "
                f"{stats.get('engrams_active', 0)} memories, "
                f"{stats.get('hypomnema_active', 0)} continuity notes, "
                f"{stats.get('connections', 0)} connections"
            ),
            "",
            "Use this at the start of a session. Capture important preferences, decisions, project state, corrections, and durable context as the conversation unfolds.",
            "",
            "Maintenance:",
            _indent(maintenance),
        ]

        # Shift 5: identity is measured from the shape of the graph — what
        # this agent keeps returning to. It is computed on every maintenance
        # cycle and is worth showing, because an agent reading its own
        # concerns back is closer to the point of Mnemos than any count of
        # memories is.
        identity_summary = self._identity_summary()
        if identity_summary:
            lines.extend(["", "Who you have been, measured from what you keep returning to:",
                          f"  {identity_summary}"])

        # Quiet and occasional by design: at most a couple of items, only when
        # something genuinely needs the agent's own judgement, and each one
        # stops being shown after a few sessions. A packet that asks for work
        # every time becomes a chore list appended to every conversation.
        reflection = self._reflection_block()
        if reflection:
            lines.extend(["", reflection])

        block = self._onboarding_block(status)
        if block:
            lines.extend(["", block])

        verification = self._verification_block()
        if verification:
            lines.extend(["", verification])

        dream = fetch_active_dream_entry(self._store, self.scope)
        if dream:
            lines.extend([
                "",
                "While you were away:",
                _indent(dream["content"]),
                "  (Mnemos wrote this while consolidating memory. Share it with the human if the moment fits.)",
            ])

        if continuity:
            lines.extend(["", "Continuity notes:"])
            lines.extend(_format_continuity(entry) for entry in continuity)
        else:
            lines.extend(["", "Continuity notes: none yet. Capture durable context when the user gives it."])

        if memories:
            lines.extend(["", "Relevant memories:"])
            lines.extend(_format_memory(result) for result in memories)

        return "\n".join(lines)

    def identity_graph(self, max_nodes: int = 18) -> dict[str, Any]:
        """Build a portable identity graph snapshot for visual-capable clients."""

        self._ensure_init()
        assert self._store is not None

        max_nodes = min(max(int(max_nodes or 18), 4), 48)
        stats = self._stats()
        continuity = self._store.search_hypomnema(
            "",
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_nodes,
        )
        engrams = self._store.get_active_engrams(
            agent_id=self.scope.agent_id,
            limit=max_nodes,
            load_connections=False,
        )

        domain_counts: dict[str, int] = {}
        for entry in continuity:
            domain = entry.get("domain") or "topical"
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

        nodes: list[dict[str, Any]] = [
            {
                "id": f"agent:{self.scope.agent_id}",
                "label": self.scope.agent_id,
                "kind": "agent",
                "weight": 1.0,
            }
        ]
        edges: list[dict[str, Any]] = []
        for domain, count in sorted(domain_counts.items(), key=lambda item: (-item[1], item[0])):
            domain_id = f"domain:{domain}"
            nodes.append({
                "id": domain_id,
                "label": domain,
                "kind": "domain",
                "weight": count,
            })
            edges.append({
                "source": f"agent:{self.scope.agent_id}",
                "target": domain_id,
                "relation": "contains",
                "strength": min(1.0, 0.35 + count * 0.12),
            })

        for entry in continuity[:max_nodes]:
            domain = entry.get("domain") or "topical"
            node_id = f"continuity:{entry['id']}"
            nodes.append({
                "id": node_id,
                "label": short_label(entry.get("content", ""), 44),
                "kind": "continuity",
                "domain": domain,
                "confidence": round(float(entry.get("confidence", 0.0)), 3),
                "salience": round(float(entry.get("salience", 0.0)), 3),
                "created_at": entry.get("created_at"),
            })
            edges.append({
                "source": f"domain:{domain}",
                "target": node_id,
                "relation": "anchors",
                "strength": round(float(entry.get("salience", 0.5)), 3),
            })

        for engram in engrams[: max(3, max_nodes // 2)]:
            node_id = f"memory:{engram.id}"
            nodes.append({
                "id": node_id,
                "label": short_label(engram.impact or engram.content, 38),
                "kind": "memory",
                "confidence": round(float(engram.source.confidence), 3),
                "strength": round(float(engram.strength), 3),
                "stability": round(float(engram.stability), 3),
                "accessibility": round(float(engram.accessibility), 3),
                "source_type": engram.source.type,
                "created_at": engram.created_at,
            })
            edges.append({
                "source": f"agent:{self.scope.agent_id}",
                "target": node_id,
                "relation": "encodes",
                "strength": round(float(engram.accessibility), 3),
            })

        timeline = build_timeline(continuity, engrams)
        summary = (
            f"{stats.get('engrams_active', 0)} active memories, "
            f"{stats.get('hypomnema_active', 0)} continuity notes, "
            f"{stats.get('connections', 0)} connections"
        )
        snapshot = {
            "version": 1,
            "scope": {
                "agent_id": self.scope.agent_id,
                "person_id": self.scope.person_id,
                "project_scope": self.scope.project_scope,
            },
            "summary": summary,
            "stats": {
                "active_memories": stats.get("engrams_active", 0),
                "continuity_notes": stats.get("hypomnema_active", 0),
                "connections": stats.get("connections", 0),
                "archived": stats.get("archived", 0),
            },
            "nodes": nodes,
            "edges": edges,
            "timeline": timeline,
        }
        snapshot["svg"] = render_identity_svg(snapshot)
        return snapshot

    def capture(
        self,
        content: str,
        context: str = "",
        importance: str | float = "auto",
        impact: str = "",
        impact_source: str = "agent",
    ) -> str:
        """Capture durable continuity without exposing Mnemos internals.

        ``impact_source`` records who wrote the impact. It defaults to 'agent'
        because the product caller is `mnemos_capture`, which the agent invokes
        mid-conversation with an impact in its own words. Server-internal
        captures that pass a boilerplate impact set this to 'template' so the
        two never blur — the whole point of the field is that an agent-authored
        trace can be told from a generated one after the fact."""

        if not content.strip():
            return "Nothing captured: content was empty."

        self._ensure_init()
        assert self._store is not None
        assert self._encoder is not None

        # Run the onboarding guard before this capture writes anything so an
        # existing store is grandfathered on its prior contents, never on the
        # capture currently being made.
        self._onboarding_status()
        self._current_session()

        full_content = content.strip()
        if context.strip():
            full_content = f"{full_content}\n\nContext: {context.strip()}"

        domain = _classify_domain(full_content)
        kind = _classify_kind(full_content)
        tags = _simple_tags(content, context)
        confidence, salience = _importance_scores(importance, domain)
        # Shift 1: a trace is what the memory changed, and only the agent can
        # say that. When it does not, the field stays empty rather than being
        # filled with a phrase the server chose — a template reads as complete
        # while carrying nothing, which is how a store ends up 76% records.
        # Empty is honest, and the reflection queue asks about it later.
        impact = (impact or "").strip()

        engram = self._encoder.encode(
            content=full_content,
            impact=impact,
            impact_source=impact_source if impact else "",
            kind=kind,
            tags=tags,
            source=SourceType.SESSION,
            agent_id=self.scope.agent_id,
            override_confidence=confidence,
            # Shift 3: the moment something does not fit what is already held
            # is the moment worth encoding deeply. This is now reachable
            # without beliefs or a model, so it no longer has to be skipped.
            skip_surprise_detection=False,
        )
        note_id = self._store.write_hypomnema_entry(
            content.strip(),
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            source="observed",
            domain=domain,
            tags=tags,
            confidence=confidence,
            salience=salience,
            foundational=domain in {"foundational", "identity"},
            related_engram_id=engram.id,
        )
        self._store.mark_hypomnema_promoted(note_id, engram.id)
        self._record_first_capture(note_id, engram.id, content)
        # Continuity just arrived, so any run of empty packets is over.
        self._set_meta("last_capture_session", str(self._current_session()))
        self._set_meta("empty_context_streak", "0")
        maintenance = self.maintain(auto=True)

        return (
            "Captured continuity.\n"
            f"Memory ID: {engram.id}\n"
            f"Continuity note ID: {note_id}\n"
            f"Scope: {self.scope.agent_id}/{self.scope.person_id}/{self.scope.project_scope}\n"
            "Maintenance:\n"
            f"{_indent(maintenance)}"
        )

    def recall(self, query: str, max_results: int = 5) -> str:
        """Recall relevant continuity and durable memories."""

        if not query.strip():
            return "Recall needs a query."

        self._ensure_init()
        assert self._store is not None

        continuity = self._store.search_hypomnema(
            query,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_results,
        )
        continuity = _filter_continuity(query, continuity)
        memories = self._retrieve(query, max_results=max_results)

        if not continuity and not memories:
            return "No relevant continuity found."

        lines = [f"Mnemos recall for: {query.strip()}"]
        if continuity:
            lines.extend(["", "Continuity notes:"])
            lines.extend(_format_continuity(entry) for entry in continuity)
        if memories:
            lines.extend(["", "Durable memories:"])
            lines.extend(_format_memory(result) for result in memories)
        return "\n".join(lines)

    def _maybe_correct_belief(self, correction: str, query: str, action: str) -> str:
        """Retire or downweight an agent-authored belief the correction names.

        Matches the correction text against active beliefs the agent itself
        stated (``source == 'agent'``) by token overlap, above a conservative
        threshold so a memory correction that merely brushes a belief does not
        move it. Forget/archive → supersede (hidden from every read path);
        otherwise → erode confidence toward the floor. Returns a note if it
        acted, else ''.
        """
        assert self._store is not None
        text = query.strip() or correction.strip()
        terms = _query_terms(text)
        if not terms:
            return ""
        beliefs = [
            b for b in self._store.get_beliefs(self.scope.agent_id, active_only=True)
            if b.source == "agent"
        ]
        best, best_overlap = None, 0.0
        for belief in beliefs:
            bterms = _query_terms(belief.content)
            if not bterms:
                continue
            overlap = len(terms & bterms) / len(terms)
            if overlap > best_overlap:
                best, best_overlap = belief, overlap
        if best is None or best_overlap < 0.5:
            return ""

        if action in {"forget", "archive", "remove", "delete"}:
            self._store.supersede_belief(best.id, reason=f"agent correction: {text[:80]}")
            return (
                f'Retired the belief "{best.content[:80]}". '
                "It will no longer shape your context."
            )
        new_conf = max(0.05, best.confidence - 0.3)
        self._store.revise_belief(
            best.id, new_conf, reason=f"agent correction: {text[:80]}"
        )
        return (
            f'Lowered confidence in the belief "{best.content[:80]}" '
            f"to {int(new_conf * 100)}%."
        )

    def correct(
        self,
        correction: str,
        target_id: str = "",
        query: str = "",
        action: str = "update",
    ) -> str:
        """Correct, supersede, or archive stale memory."""

        if not correction.strip() and action not in {"forget", "archive", "remove", "delete"}:
            return "Correction needs replacement text or a forget/archive action."

        self._ensure_init()
        assert self._store is not None
        assert self._encoder is not None

        action = action.strip().lower() or "update"
        target = target_id.strip()

        # Belief correction (agent-authored only). Checked before the memory
        # paths, on the free-text / query form, so "forget that I believe X"
        # can retire a belief the agent stated — not only a note. Restricted to
        # source=='agent' so a seed or model belief can't be erased by mistake.
        # This is one of the few deliberate downward moves in a graph whose
        # stability otherwise only ratchets up.
        if not target:
            belief_note = self._maybe_correct_belief(correction, query, action)
            if belief_note:
                return belief_note

        if target:
            hypo = self._store.get_hypomnema_entry(
                target,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            )
            if hypo is not None:
                if action in {"forget", "archive", "remove", "delete"}:
                    self._store.archive_hypomnema_entry(
                        target,
                        reason=f"simple correction action={action}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                    )
                    related_engram_id = hypo.get("related_engram_id") or hypo.get("graduated_to_engram_id")
                    if related_engram_id:
                        related = self._store.get_engram(related_engram_id)
                        if related is not None:
                            self._store.archive_engram(related, reason=f"simple_correction_{action}")
                    return f"Archived continuity note {target}."

                self._store.revise_hypomnema_entry(
                    target,
                    correction,
                    reason="simple correction",
                    agent_id=self.scope.agent_id,
                    person_id=self.scope.person_id,
                    project_scope=self.scope.project_scope,
                    confidence=0.92,
                    salience=0.75,
                )
                return f"Updated continuity note {target}."

            engram = self._store.get_engram(target)
            if engram is not None:
                self._store.archive_engram(engram, reason=f"simple_correction_{action}")
                if action in {"forget", "archive", "remove", "delete"} and not correction.strip():
                    return f"Archived memory {target}."
                replacement = self._encoder.encode(
                    content=correction.strip(),
                    impact="Correction to earlier continuity.",
                    impact_source="template",
                    kind=_classify_kind(correction),
                    tags=["continuity", "correction"],
                    source=SourceType.SESSION,
                    agent_id=self.scope.agent_id,
                    override_confidence=0.92,
                    skip_surprise_detection=True,
                )
                return (
                    f"Archived memory {target} and captured correction {replacement.id}.\n"
                    f"Correction: {correction.strip()}"
                )

        search_text = query.strip() or correction.strip()
        query_text = query.strip()
        if query_text:
            matches = self._store.search_hypomnema(
                query_text,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
                limit=1,
            )
            if matches:
                match = matches[0]
                if action in {"forget", "archive", "remove", "delete"}:
                    self._store.archive_hypomnema_entry(
                        match["id"],
                        reason=f"simple correction action={action}; query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                    )
                    related_engram_id = match.get("related_engram_id") or match.get("graduated_to_engram_id")
                    if related_engram_id:
                        related = self._store.get_engram(related_engram_id)
                        if related is not None:
                            self._store.archive_engram(related, reason=f"simple_correction_{action}")
                    maintenance = self.maintain(auto=True)
                    return (
                        f"Archived closest continuity note {match['id']}.\n"
                        "Maintenance:\n"
                        f"{_indent(maintenance)}"
                    )

                note_id = match["id"]
                if action in {"supersede", "replace"}:
                    note_id = self._store.supersede_hypomnema_entry(
                        match["id"],
                        correction,
                        reason=f"simple correction action={action}; query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                    )
                else:
                    self._store.revise_hypomnema_entry(
                        match["id"],
                        correction,
                        reason=f"simple correction query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                        confidence=0.92,
                        salience=0.75,
                    )

                related_engram_id = match.get("related_engram_id") or match.get("graduated_to_engram_id")
                if related_engram_id:
                    related = self._store.get_engram(related_engram_id)
                    if related is not None:
                        self._store.archive_engram(related, reason=f"simple_correction_{action}")

                replacement = self._encoder.encode(
                    content=correction.strip(),
                    impact="Corrected continuity for future interactions.",
                    impact_source="template",
                    kind=_classify_kind(correction),
                    tags=sorted(set(["continuity", "correction", *_simple_tags(correction)])),
                    source=SourceType.SESSION,
                    agent_id=self.scope.agent_id,
                    override_confidence=0.92,
                    skip_surprise_detection=True,
                )
                self._store.mark_hypomnema_promoted(note_id, replacement.id)
                maintenance = self.maintain(auto=True)
                return (
                    f"Updated closest continuity note {note_id}.\n"
                    f"Memory ID: {replacement.id}\n"
                    "Maintenance:\n"
                    f"{_indent(maintenance)}"
                )

        if action in {"forget", "archive", "remove", "delete"} and search_text:
            matches = self._retrieve(search_text, max_results=1)
            if matches:
                engram = matches[0].engram
                self._store.archive_engram(engram, reason=f"simple_correction_{action}")
                return f"Archived closest matching memory {engram.id}."

        return self.capture(
            correction.strip(),
            context=f"Correction supplied through mnemos_correct. Prior query: {query.strip()}",
            importance="high",
        )

    def maintain(self, deep: bool = False, auto: bool = False) -> str:
        """Run the best available maintenance without requiring setup."""

        self._ensure_init()
        assert self._store is not None

        requested_deep = bool(deep)
        can_run_deep = requested_deep and self._llm_client is not None
        # config={} meant the whole consolidation block in ~/.mnemos/config.json
        # was never applied — decay_rate, thresholds and min_idle_minutes all
        # silently fell back to hardcoded defaults.
        try:
            daemon_config = load_config()
        except Exception:
            daemon_config = {}
        daemon = ConsolidationDaemon(
            store=self._store,
            config=daemon_config,
            llm_client=self._llm_client if can_run_deep else None,
            embedding_index=self._embedding_index,
            agent_model_hint=self._agent_model_hint,
        )
        # Automatic maintenance rides on reads (context) and writes (capture,
        # correct). Those fire many times a session, so they honour the
        # activity gate; an explicit maintain request always runs.
        stats = daemon.run_cycle(
            deep=can_run_deep,
            agent_id=self.scope.agent_id,
            respect_gate=auto,
        )
        if stats.get("skipped"):
            return "\n".join([
                f"Requested: {'deep' if requested_deep else 'standard'}",
                "Cycle: skipped",
                "Completed: no maintenance needed yet (ran recently)",
                "Passes: none",
            ])
        promoted = self._promote_candidates(limit=3)
        # Maintenance proposes reflections; it never answers them.
        try:
            # Hygiene first: a store written before excerpts were dropped can
            # still hold a frozen copy of a memory the human asked to forget.
            self._store.purge_stale_reflections()
            self._enqueue_lesson_reflections(stats.get("softening") or {})
            self._enqueue_impact_reflections(limit=2)
            # Judgement the agent alone can do, proposed rarely: whether a
            # recurring theme is a belief, and whether a surprising capture
            # contradicts what was already held. The packet's ≤2 cap and the
            # per-cycle limit of 1 each keep these from ever becoming a chore.
            self._enqueue_belief_reflections(limit=1)
            self._enqueue_contradiction_reflections(limit=1)
        except Exception:
            pass

        # Dream journal: narrate the cycle when it did meaningful work. The
        # import stays local so a journal failure can never break maintenance.
        self.last_dream_note_id = None
        self.last_dream_narrative = None
        dream_status = "skipped (nothing noteworthy)"
        try:
            from .dream_journal import (
                collect_belief_deltas,
                compose_dream_narrative,
                write_dream_entry,
            )

            deltas = collect_belief_deltas(
                self._store, self.scope.agent_id, stats.get("started_at", "")
            )
            narrative = compose_dream_narrative(stats, deltas, promoted)
            if narrative:
                self.last_dream_note_id = write_dream_entry(self._store, self.scope, narrative)
                self.last_dream_narrative = narrative
                self._set_meta("dream_last_written_at", datetime.now(timezone.utc).isoformat())
                dream_status = "updated"
        except Exception:
            dream_status = "skipped (write failed)"

        if can_run_deep:
            completed = "model-assisted deep maintenance completed"
        elif requested_deep:
            completed = "local deterministic maintenance completed; model-assisted deep pass unavailable"
        else:
            completed = "local deterministic maintenance completed"

        model_note = "dedicated model available" if self._llm_client else "no dedicated model configured"
        if requested_deep and not can_run_deep:
            model_note += "; deep requested, ran local deterministic maintenance"
        elif not requested_deep:
            model_note += "; ran local deterministic maintenance"
        if auto:
            model_note += " during normal use"

        lines = [
            f"Requested: {'deep' if requested_deep else 'standard'}",
            f"Cycle: {stats.get('cycle_type', 'shallow')}",
            f"Completed: {completed}",
            f"Passes: {', '.join(stats.get('passes_run', [])) or 'none'}",
            f"Promoted continuity notes: {promoted}",
            f"Model path: {model_note}",
            f"Dream journal: {dream_status}",
        ]
        errors = [key for key in stats if key.endswith("_error")]
        for key in errors:
            lines.append(f"{key}: {stats[key]}")
        return "\n".join(lines)

    def polish_dream(self, note_id: str, polished: str) -> bool:
        """Apply a host-model polish to a dream note. Returns False on any failure."""

        text = (polished or "").strip()
        if not text or len(text) > 900:
            return False
        try:
            from .dream_journal import polish_dream_entry

            self._ensure_init()
            polish_dream_entry(self._store, self.scope, note_id, text)
            return True
        except Exception:
            return False

    def health(self) -> dict[str, Any]:
        """Read-only snapshot of this scope's memory health.

        Unlike context(), this never runs maintenance, never bumps the
        session counter, and never writes onboarding or verification meta.
        Safe to call any number of times without changing the store.
        """

        # _ensure_init() builds the schema, so calling it here would create a
        # database as a side effect of a tool annotated readOnlyHint=True.
        # On a scope with no store yet, report that instead of creating one.
        if not self.db_path.exists():
            return {
                "scope": {
                    "agent_id": self.scope.agent_id,
                    "person_id": self.scope.person_id,
                    "project_scope": self.scope.project_scope,
                },
                "store": {
                    "db_path": str(self.db_path),
                    "exists": False,
                    "size_bytes": 0,
                },
                "note": (
                    "No memory store exists for this scope yet. It is created "
                    "on first capture, not by reading health."
                ),
            }

        self._ensure_init()
        assert self._store is not None

        stats = self._stats()
        db_path = self.db_path
        size_bytes = db_path.stat().st_size if db_path.exists() else 0

        last_cycle: dict[str, Any] | None = None
        runs = self._store.get_consolidation_runs("cycle", limit=1)
        if runs:
            row = runs[0]
            cycle_stats = row.get("stats") or {}
            substrate = cycle_stats.get("substrate") or {}
            last_cycle = {
                "completed_at": row.get("completed_at"),
                "cycle_type": cycle_stats.get("cycle_type"),
                "passes_run": list(cycle_stats.get("passes_run") or []),
                "substrate_model": substrate.get("model"),
                "substrate_provider": substrate.get("provider"),
            }

        # persist=False keeps the onboarding probe write-free: it only
        # reads meta and stats, never records a stage transition.
        status = self._onboarding_status(persist=False)

        verified_at_meta = self._get_meta("verified_at")
        if verified_at_meta == "skipped":
            verification_status = "skipped"
            verified_at = None
        elif verified_at_meta is not None:
            verification_status = "verified"
            verified_at = verified_at_meta
        elif self._get_meta("first_capture") is not None:
            verification_status = "pending"
            verified_at = None
        else:
            verification_status = "not-started"
            verified_at = None

        dream = fetch_active_dream_entry(self._store, self.scope)
        dream_last_written_at = self._get_meta("dream_last_written_at")
        dream_excerpt: str | None = None
        if dream:
            dream_excerpt = " ".join(str(dream.get("content", "")).split())[:60]
            if dream_last_written_at is None:
                dream_last_written_at = dream.get("last_revised_at")

        return {
            "scope": {
                "agent_id": self.scope.agent_id,
                "person_id": self.scope.person_id,
                "project_scope": self.scope.project_scope,
            },
            "store": {
                "db_path": str(db_path),
                "size_bytes": int(size_bytes),
            },
            "counts": {
                "memories_active": stats.get("engrams_active", 0),
                "memories_archived": stats.get("archived", 0),
                "continuity_notes_active": stats.get("hypomnema_active", 0),
                "continuity_notes_foundational": stats.get("hypomnema_foundational", 0),
                "connections": stats.get("connections", 0),
                "beliefs_active": stats.get("beliefs_active", 0),
            },
            "last_cycle": last_cycle,
            "identity": {
                "declared_model": self._get_meta("agent_model"),
                "declared_name": self._get_meta("agent_name"),
            },
            "onboarding": {
                "stage": status["stage"],
                "session": int(self._get_meta("session_counter", "0") or 0),
            },
            "verification": {
                "status": verification_status,
                "verified_at": verified_at,
            },
            "dream": {
                "last_written_at": dream_last_written_at,
                "excerpt": dream_excerpt,
            },
            "continuity": self.continuity_signals(),
        }

    def _retrieve(self, query: str, max_results: int = 5) -> list[Any]:
        assert self._store is not None
        assert self._retriever is not None
        emotional_state = self._store.get_latest_emotional_state(self.scope.agent_id)
        results = _filter_memories(query, self._retriever.retrieve(
            cue=query,
            agent_id=self.scope.agent_id,
            max_results=max(1, max_results),
            emotional_state=emotional_state,
        ))
        return [
            result for result in results
            if self._engram_visible_in_current_scope(result.engram.id)
        ]

    def _engram_visible_in_current_scope(self, engram_id: str) -> bool:
        """Respect hypomnema person/project scope for durable memories.

        Thin wrapper over the store's shared visibility check, so this path and
        ``build_context_packet`` cannot disagree about whose memory an engram
        is. See ``EngramStore.engram_visible_in_scope``.
        """
        assert self._store is not None
        return self._store.engram_visible_in_scope(
            engram_id,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
        )

    def _promote_candidates(self, limit: int = 3) -> int:
        assert self._store is not None
        assert self._encoder is not None
        candidates = self._store.get_hypomnema_promotion_candidates(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=limit,
        )
        promoted = 0
        for entry in candidates:
            if entry.get("related_engram_id"):
                self._store.mark_hypomnema_promoted(entry["id"], entry["related_engram_id"])
                promoted += 1
                continue
            engram = self._encoder.encode(
                content=entry["content"],
                impact="Stable continuity promoted during simple maintenance.",
                impact_source="template",
                kind="semantic",
                tags=["continuity", "promoted", *entry.get("tags", [])],
                source=SourceType.BACKGROUND,
                agent_id=self.scope.agent_id,
                override_confidence=float(entry["confidence"]),
                skip_surprise_detection=True,
            )
            self._store.mark_hypomnema_promoted(entry["id"], engram.id)
            promoted += 1
        return promoted


def _importance_scores(importance: str | float, domain: str) -> tuple[float, float]:
    if isinstance(importance, (float, int)):
        normalized_score = min(max(float(importance), 0.0), 1.0)
        confidence = min(max(0.55 + (normalized_score * 0.4), 0.55), 0.95)
        salience = min(max(0.35 + (normalized_score * 0.55), 0.35), 0.9)
        return confidence, salience

    normalized = str(importance).strip().lower()
    if normalized in {"low", "minor"}:
        return 0.72, 0.45
    if normalized in {"high", "important", "critical"}:
        return 0.92, 0.82
    if domain in {"foundational", "identity"}:
        return 0.9, 0.8
    if domain in {"recurring", "long-arc"}:
        return 0.86, 0.72
    return 0.82, 0.66


# Phrases the server itself writes into `impact`. They fill the column but
# they are not traces of how understanding changed, so an engram carrying
# only one of these still needs the agent's own words.
_TEMPLATED_IMPACTS = frozenset({
    "Foundational continuity for future interactions.",
    "Recurring pattern worth carrying across sessions.",
    "Long-arc context that should shape future work.",
    "Current working context for continuity.",
    "Preference to respect in future decisions.",
    "Durable continuity captured from the session.",
    "Stable scoped continuity promoted from hypomnema.",
    "Stable continuity promoted during simple maintenance.",
    "Correction to earlier continuity.",
    "Corrected continuity for future interactions.",
})


def _impact_for(content: str, domain: str) -> str:
    if domain in {"foundational", "identity"}:
        return "Foundational continuity for future interactions."
    if domain == "recurring":
        return "Recurring pattern worth carrying across sessions."
    if domain == "long-arc":
        return "Long-arc context that should shape future work."
    if domain == "situational":
        return "Current working context for continuity."
    if "prefer" in content.lower() or "wants" in content.lower():
        return "Preference to respect in future decisions."
    return "Durable continuity captured from the session."


def _format_continuity(entry: dict[str, Any]) -> str:
    score = entry.get("score", 0.0)
    content = entry["content"].replace("\n", " ")
    if len(content) > 180:
        content = content[:177] + "..."
    return (
        f"- [{score:.2f}] {content}\n"
        f"  id={entry['id']} domain={entry['domain']} confidence={entry['confidence']:.2f}"
    )


def _format_memory(result: Any) -> str:
    engram = result.engram
    display = engram.impact or engram.content
    display = display.replace("\n", " ")
    if len(display) > 180:
        display = display[:177] + "..."
    return (
        f"- [{result.score:.2f}] {display}\n"
        f"  id={engram.id} kind={engram.kind} confidence={engram.source.confidence:.2f}"
    )


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" if line else "" for line in text.splitlines())


def _human_size(num_bytes: int) -> str:
    """Render a byte count in 1024-base units: "0 B", "412 KB", "1.2 MB"."""

    size = float(max(int(num_bytes), 0))
    if size < 1024:
        return f"{int(size)} B"
    unit = "B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024:
            break
    if size >= 100 or size.is_integer():
        return f"{size:.0f} {unit}"
    return f"{size:.1f} {unit}"


def format_health_card(data: dict[str, Any]) -> str:
    """Render a health() snapshot as a human-relayable card."""

    def line(label: str, value: Any) -> str:
        return f"{label + ':':<15}{value}"

    scope = data["scope"]
    store = data["store"]
    counts = data["counts"]

    last_cycle = data["last_cycle"]
    if last_cycle is None:
        cycle_line = "none yet"
    else:
        substrate = last_cycle["substrate_model"] or "local rules, no model"
        cycle_line = (
            f"{last_cycle['completed_at']} ({last_cycle['cycle_type']}) "
            f"maintained by {substrate}"
        )

    verification = data["verification"]
    verification_line = {
        "verified": f"verified on {verification['verified_at']}",
        "pending": "pending first restart",
        "skipped": "skipped (existing store)",
        "not-started": "not started",
    }.get(verification["status"], verification["status"])

    dream = data["dream"]
    if dream["excerpt"] is None:
        dream_line = "none yet"
    else:
        dream_line = f"{dream['last_written_at']}: \"{dream['excerpt']}\""

    continuity = data.get("continuity") or {}
    warnings = continuity.get("warnings") or []
    if warnings:
        # Amnesia is the failure that looks exactly like success, so it is
        # stated first and in plain words rather than left to be inferred
        # from a count further down the card.
        continuity_lines = ["", "ATTENTION — continuity may not be reaching this agent:"]
        continuity_lines += [f"  - {w}" for w in warnings]
    else:
        streak = continuity.get("empty_context_streak", 0)
        since = continuity.get("sessions_since_capture")
        detail = "carrying continuity"
        if since == 0:
            detail = "carrying continuity (captured this session)"
        elif since is not None:
            detail = f"carrying continuity (last capture {since} session(s) ago)"
        continuity_lines = ["", f"Continuity check: {detail}, {streak} empty packet(s) in a row."]

    return "\n".join([
        "Mnemos health card",
        line(
            "Scope",
            f"agent={scope['agent_id']} person={scope['person_id']} "
            f"project={scope['project_scope']}",
        ),
        line("Store", f"{store['db_path']} ({_human_size(store['size_bytes'])})"),
        line(
            "Memories",
            f"{counts['memories_active']} active, "
            f"{counts['memories_archived']} archived",
        ),
        line(
            "Continuity",
            f"{counts['continuity_notes_active']} notes "
            f"({counts['continuity_notes_foundational']} foundational)",
        ),
        line("Connections", counts["connections"]),
        line("Beliefs", f"{counts['beliefs_active']} active"),
        line("Last cycle", cycle_line),
        line(
            "Onboarding",
            f"{data['onboarding']['stage']} (session {data['onboarding']['session']})",
        ),
        line("Verification", verification_line),
        line("Last dream", dream_line),
        *continuity_lines,
        "",
        "Everything on this card is safe to relay to the human in plain words.",
    ])

