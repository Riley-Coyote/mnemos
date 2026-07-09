"""
Substrate configuration.

Centralized knobs for the substrate tick cycle. Defaults are conservative;
tune after observation.
"""

import os
from dataclasses import dataclass, field


def _default_db_path() -> str:
    from mnemos.simple_scope import resolve_scope

    return resolve_scope().db_path


@dataclass
class SubstrateConfig:
    """Configuration for the substrate tick cycle."""

    # ── Agent identity ──
    agent_id: str = "default"
    agent_name: str = "Agent"
    db_path: str = field(default_factory=_default_db_path)

    # ── Consolidation ──
    decay_rate: float = 0.02  # How much vividness fades per tick
    connection_discovery_limit: int = (
        20  # Max engrams to check for new connections per tick
    )
    belief_review_limit: int = 5  # Max beliefs to review per tick

    # ── Modulators ──
    base_temperature: float = 0.7  # LLM temperature baseline
    temperature_range: float = 0.3  # Max deviation from baseline (0.4 - 1.0)
    recent_window_hours: int = 24  # Weight recent events over this window

    # ── Handlers ──
    max_cascade_depth: int = 2  # Max handler chain depth per tick
    reflection_cooldown_hours: int = 12  # Min time between revisions of same belief
    max_confidence_change: float = 0.15  # Max belief confidence change per reflection
    dreaming_collision_threshold: float = (
        0.3  # Min vividness difference for dream collision
    )
    silence_threshold_hours: int = (
        6  # Hours without encoding before silence_extended fires
    )

    # ── Throttles ──
    max_dreams_per_week: int = 10  # Max dream engrams per 7-day window
    max_wanderings_per_week: int = 5  # Max wandering engrams per 7-day window

    # ── Safety ──
    max_engrams_per_tick: int = (
        3  # Max new engrams a tick can produce (prevents runaway)
    )
    skip_surprise_on_handler_output: bool = (
        True  # All handler outputs skip surprise detection
    )

    # ── Introspection (opt-in self-audit of recent outputs) ──
    introspection_enabled: bool = (
        False  # Audit recent responses for performed-vs-genuine markers
    )
    introspection_window_hours: int = 24  # Look-back window for recent responses
    introspection_max_per_tick: int = 5  # Max responses audited per tick
    introspection_min_tokens: int = 50  # Skip responses shorter than this (in words)

    # ── Logging ──
    log_dir: str = "~/.mnemos/logs/"
    verbose: bool = True

    # ── LLM ──
    llm_models: dict = field(
        default_factory=lambda: {
            "extraction": "flash",
            "softening": "flash",
            "connection": "flash",
            "consolidation": "flash",
            "creative_association": "flash",
            "wandering": "flash",
        }
    )

    @classmethod
    def from_env(cls) -> "SubstrateConfig":
        """Create config from environment variables.

        Reads:
            MNEMOS_AGENT_ID — agent identifier
            MNEMOS_AGENT_NAME — agent display name
            MNEMOS_DB_PATH — path to SQLite database
            MNEMOS_LOG_DIR — path to log directory
        """
        kwargs = {}
        if v := os.environ.get("MNEMOS_AGENT_ID"):
            kwargs["agent_id"] = v
        if v := os.environ.get("MNEMOS_AGENT_NAME"):
            kwargs["agent_name"] = v
        if v := os.environ.get("MNEMOS_DB_PATH"):
            kwargs["db_path"] = v
        if v := os.environ.get("MNEMOS_LOG_DIR"):
            kwargs["log_dir"] = v
        if os.environ.get("MNEMOS_INTROSPECTION", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            kwargs["introspection_enabled"] = True
        return cls(**kwargs)
