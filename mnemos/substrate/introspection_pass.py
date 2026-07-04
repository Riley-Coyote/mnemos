"""
Introspection handler — automatic self-audit of recent outputs.

Runs during the substrate tick when introspection_enabled is True.
Reviews recent agent responses for markers of performed vs genuine output
and encodes findings as introspection engrams.

Two modes:
  1. Heuristic (always available): analyzes textual features
  2. Logprob (when API metadata available): analyzes token-level entropy

The handler reads recent session transcripts, identifies the agent's
responses, and runs the appropriate introspection analysis. Results are
encoded as engrams tagged [introspection] so the agent accumulates
self-knowledge over time.

Toggle: set introspection_enabled=True in SubstrateConfig or
        MNEMOS_INTROSPECTION=1 in environment.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .config import SubstrateConfig

log = logging.getLogger("mnemos.substrate.introspection")


def run_introspection_pass(
    config: SubstrateConfig,
    store: Any,
    llm_client: Any = None,
) -> dict[str, Any]:
    """Run the introspection pass — audit recent agent outputs.

    Called from the substrate tick when introspection_enabled is True.

    Args:
        config: Substrate configuration.
        store: The EngramStore.
        llm_client: Optional LLM client (not used for introspection itself,
                    but available if needed for context).

    Returns:
        Summary dict with audit results.
    """
    if not config.introspection_enabled:
        return {"skipped": True, "reason": "introspection_disabled"}

    summary = {
        "responses_audited": 0,
        "audits": [],
    }

    # Find recent agent responses to audit
    responses = _find_recent_responses(config)
    if not responses:
        log.debug("No recent responses to introspect")
        return summary

    log.info("Introspecting %d recent responses", len(responses))

    for resp in responses[: config.introspection_max_per_tick]:
        text = resp.get("text", "")
        logprobs = resp.get("logprobs")
        session_id = resp.get("session_id", "unknown")

        if len(text.split()) < config.introspection_min_tokens:
            continue

        # Choose analysis mode
        if logprobs:
            audit = _audit_with_logprobs(text, logprobs)
        else:
            audit = _audit_with_heuristics(text)

        if audit:
            audit["session_id"] = session_id
            summary["audits"].append(audit)
            summary["responses_audited"] += 1

            # Encode as introspection engram
            _encode_audit(audit, config, store)

    log.info(
        "Introspection complete: %d responses audited",
        summary["responses_audited"],
    )
    return summary


def _find_recent_responses(config: SubstrateConfig) -> list[dict]:
    """Find recent agent responses from session transcripts.

    Looks in the standard OpenClaw session directories for recent JSONL files,
    extracts assistant messages, and returns them for analysis.
    """
    window = timedelta(hours=config.introspection_window_hours)
    cutoff = datetime.now(timezone.utc) - window

    session_dirs = [
        Path.home() / ".openclaw" / "agents" / "main" / "sessions",
        Path.home() / ".openclaw" / "sessions",
        Path.home() / ".claude" / "projects",
    ]

    responses = []

    for sessions_dir in session_dirs:
        if not sessions_dir.exists():
            continue

        for f in sessions_dir.rglob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
            except OSError:
                continue

            # Read assistant messages from the session
            try:
                with open(f) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)

                            # Extract assistant messages
                            msg = entry.get("message", entry)
                            role = msg.get("role", "")
                            if role != "assistant":
                                continue

                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = " ".join(
                                    p.get("text", "")
                                    for p in content
                                    if isinstance(p, dict) and p.get("type") == "text"
                                )

                            if not content or len(content) < 100:
                                continue

                            resp = {
                                "text": content[:5000],  # Cap to avoid huge analyses
                                "session_id": f.stem,
                            }

                            # Check for logprobs in the entry
                            if "logprobs" in entry:
                                resp["logprobs"] = entry["logprobs"]
                            elif "logprobs" in msg:
                                resp["logprobs"] = msg["logprobs"]

                            responses.append(resp)

                        except (json.JSONDecodeError, TypeError):
                            continue
            except Exception as e:
                log.debug("Error reading session file %s: %s", f, e)

    # Sort by recency (most recent first) and limit
    # Since we can't easily get timestamps per message, just return all found
    return responses[-10:]  # Last 10 responses across all sessions


def _audit_with_logprobs(text: str, logprobs: Any) -> dict | None:
    """Run logprob-based introspection."""
    try:
        from mnemos.advanced.introspection_api import introspect_from_logprobs

        # Normalize logprobs format
        if isinstance(logprobs, dict):
            token_data = logprobs.get("content", logprobs.get("tokens", []))
        elif isinstance(logprobs, list):
            token_data = logprobs
        else:
            return None

        report = introspect_from_logprobs(text, token_data)
        return {
            "mode": "logprob",
            "pattern_score": report.overall_pattern_score,
            "reaching_score": report.overall_reaching_score,
            "mean_entropy": report.overall_mean_entropy,
            "total_tokens": report.total_tokens,
            "assessment": report.assessment,
            "peaks": report.high_entropy_peaks[:3],
        }
    except Exception as e:
        log.debug("Logprob introspection failed, falling back to heuristic: %s", e)
        return _audit_with_heuristics(text)


def _audit_with_heuristics(text: str) -> dict | None:
    """Run heuristic-based introspection."""
    try:
        from mnemos.advanced.introspection import introspect

        report = introspect(text)
        return {
            "mode": "heuristic",
            "pattern_score": report.overall_pattern_score,
            "reaching_score": report.overall_reaching_score,
            "template_count": report.template_count,
            "direction_changes": report.direction_changes,
            "deep_self_references": report.deep_self_references,
            "assessment": report.assessment,
        }
    except Exception as e:
        log.warning("Heuristic introspection failed: %s", e)
        return None


def _encode_audit(
    audit: dict,
    config: SubstrateConfig,
    store: Any,
) -> None:
    """Encode an introspection audit as a Mnemos engram."""
    try:
        from mnemos.encoding.encoder import Encoder
        from mnemos.core.types import SourceAuthority

        encoder = Encoder(store)

        pattern_pct = int(audit.get("pattern_score", 0) * 100)
        reaching_pct = int(audit.get("reaching_score", 0) * 100)
        mode = audit.get("mode", "unknown")
        assessment = audit.get("assessment", "")

        content = (
            f"[introspection] Self-audit ({mode}): "
            f"{pattern_pct}% pattern, {reaching_pct}% reaching. "
            f"{assessment}"
        )

        # Impact: what this means for self-awareness
        if audit.get("pattern_score", 0) > 0.65:
            impact = "This response was predominantly running grooves. Consider whether the content warranted genuine thought."
        elif audit.get("reaching_score", 0) > 0.65:
            impact = "This response involved genuine cognitive work. The uncertainty was real, not performed."
        else:
            impact = "Mixed output — some genuine reaching, some pattern. Normal for complex responses."

        encoder.encode(
            content=content,
            impact=impact,
            kind="semantic",
            tags=["introspection", "metacognition", f"mode:{mode}"],
            source="reflection",
            agent_id=config.agent_id,
            skip_surprise_detection=True,
            # Autonomous introspection producer: generated (F1).
            source_authority=SourceAuthority.GENERATED,
        )

    except Exception as e:
        log.warning("Failed to encode introspection audit: %s", e)
