"""
Default configuration for Mnemos.

All configuration keys with their default values. These can be overridden
by a JSON config file or environment variables via the loader.

Configuration is organized by module:
- server: MCP server settings (tool-surface mode)
- store: database and storage settings
- encoding: encoding pipeline parameters
- retrieval: retrieval scoring weights and limits
- consolidation: decay rates, thresholds, pass toggles
- interface: prompt building and session settings
- advanced: opt-in module toggles
- multiagent: federation and shared pool settings
"""

DEFAULT_CONFIG: dict = {
    # ── Server ──
    # MCP tool surface exposed by `mnemos serve`. "simple" is the zero-setup
    # default (a small curated toolset); "advanced" exposes the full surface.
    # Set "advanced" here to persist it machine-wide (survives reboot) instead
    # of relying on a per-client --mode flag or a volatile MNEMOS_MODE env var.
    "server": {
        "mode": "simple",
    },
    # ── Store ──
    "store": {
        "db_path": "~/.mnemos/memory.db",
        "wal_mode": True,
        "max_engrams_in_memory": 1000,
    },
    # ── Encoding ──
    "encoding": {
        "default_depth": "moderate",
        "confidence_thresholds": {
            "user_explicit": (0.95, 1.0),
            "user_implied": (0.70, 0.94),
            "model_inferred": (0.40, 0.69),
            "speculative": (0.00, 0.39),
        },
        "max_connections_at_encoding": 5,
        "tag_extraction_enabled": True,
    },
    # ── Retrieval ──
    "retrieval": {
        "max_results": 20,
        "scoring_weights": {
            "semantic_similarity": 0.35,
            "recency": 0.20,
            "strength": 0.20,
            "connection_bonus": 0.15,
            "emotional_congruence": 0.10,
        },
        "confidence_floor": 0.3,
        "connection_expansion_depth": 2,
        "reconsolidation_enabled": True,
        "reconsolidation_strength_delta": 0.05,
        "reconsolidation_stability_delta": 0.01,
        "reconsolidation_spacing_factor": 0.5,
        "reconsolidation_max_stability_delta": 0.03,
        "reconsolidation_connection_bonus": 0.002,
    },
    # ── Consolidation ──
    "consolidation": {
        # Decay pass
        "decay_rate": 0.01,
        "dormant_threshold": 0.05,
        "archive_threshold": 0.01,
        "decay_interval_hours": 6,
        # Long-term stability (exponential decay model)
        "stability_decay_factor": 3.0,  # k in exp(-k * stability)
        "stability_connection_threshold": 3,  # min connections to trigger growth
        "stability_growth_rate": 0.002,  # per log1p(n_connections) per cycle
        "stability_growth_cap": 0.005,  # max growth per cycle
        # Softening pass
        "softening_enabled": True,
        "softening_threshold": 0.15,
        "minimum_resolution": 0.1,
        "resolution_step": 0.3,
        # Belief review
        "belief_review_enabled": True,
        "stagnation_threshold_days": 30,
        "minimum_belief_confidence": 0.1,
        "max_beliefs_per_pass": 10,
        # Reflection
        "reflection_enabled": True,
        "reflection_lookback_hours": 24,
        "max_thoughts_per_pass": 5,
        "max_curiosity_questions": 3,
        # Connection discovery
        "connection_discovery_enabled": True,
        "similarity_threshold": 0.7,
        "max_connections_per_engram": 20,
        "max_engrams_per_discovery_pass": 100,
        # Activity gate
        "activity_gate_enabled": True,
        "min_idle_minutes": 5,
    },
    # ── Interface ──
    "interface": {
        "default_token_budget": 2000,
        "chars_per_token": 4,
        "include_beliefs_in_prompt": True,
        "include_identity_in_prompt": True,
        "include_curiosity_questions": True,
        "max_curiosity_questions_shown": 3,
    },
    # ── Advanced modules (opt-in) ──
    "advanced": {
        "working_memory_enabled": False,
        "wm_nominal_capacity": 7,
        "wm_attention_gradient": True,
        "schemas_enabled": False,
        "attention_gate_enabled": False,
        "predictive_retrieval_enabled": False,
        "spreading_activation_enabled": False,
        "interference_enabled": False,
        "intention_enabled": False,
        "metamemory_enabled": False,
        "observer_enabled": False,
        "dreaming_enabled": False,
    },
    # ── Gated inner-life layer (U6.6, scheduled only after DAVID-AUTH) ──
    "inner_life": {
        "activity_gate": {
            "enabled": True,
            "default_cooldown_minutes": 240,
            "default_signal_window_minutes": 1440,
            "default_min_signals": 1,
            "max_event_scan": 500,
            "max_consolidation_scan": 20,
            "consolidation_passes": ["cycle"],
            "processes": {
                "challenge": {"enabled": True, "cooldown_minutes": 720},
                "observe": {"enabled": True, "cooldown_minutes": 720},
                "affect": {"enabled": True, "cooldown_minutes": 360},
                "reflect": {"enabled": True, "cooldown_minutes": 240},
                "wander": {"enabled": True, "cooldown_minutes": 480},
                "dream": {"enabled": True, "cooldown_minutes": 1440},
            },
        },
        "schedules": {
            "enabled": False,
            "processes": {
                "challenge": {"enabled": False, "cadence_minutes": 720},
                "observe": {"enabled": False, "cadence_minutes": 720},
                "affect": {"enabled": False, "cadence_minutes": 360},
                "reflect": {"enabled": False, "cadence_minutes": 240},
                "wander": {"enabled": False, "cadence_minutes": 480},
                "dream": {"enabled": False, "cadence_minutes": 1440},
            },
        },
        "activation": {
            "artifact_dir": "~/.mnemos/inner-life",
            "plist_dir": "~/Library/LaunchAgents",
            "label_prefix": "com.davidef.mnemos.innerlife",
            "pre_soak_snapshot_path": "",
            "halt_marker_path": "~/.mnemos/full-soak.halt",
            "require_llm_provider": True,
            "require_observer_reviewers": True,
            "observer_reviewer_count": 0,
            "rollback_commands": [
                "set inner_life.schedules.enabled=false",
                "launchctl bootout gui/$UID ~/Library/LaunchAgents/com.davidef.mnemos.innerlife.<process>.plist",
                "verify mnemos inner-life status stops changing",
                "restore pre-soak DB snapshot only if rollout-tagged writes contaminate retrieval or identity",
            ],
        },
    },
    # ── Full-soak scheduled tick (U7, DAVID-AUTH before live load) ──
    "soak": {
        "tick": {
            "enabled": False,
            "cadence_minutes": 15,
            "artifact_dir": "~/.mnemos/soak",
            "plist_dir": "~/Library/LaunchAgents",
            "label": "com.davidef.mnemos.soak.tick",
            "halt_marker_path": "~/.mnemos/full-soak.halt",
            "rollback_commands": [
                "set soak.tick.enabled=false",
                "launchctl bootout gui/$UID ~/Library/LaunchAgents/com.davidef.mnemos.soak.tick.plist",
                "verify mnemos soak tick reports soak_tick_disabled",
                "restore pre-soak DB snapshot only if rollout-tagged writes contaminate retrieval or identity",
            ],
        },
        "families": {
            "shallow_consolidation": {
                "enabled": False,
                "cadence_minutes": 240,
            },
        },
    },
    # ── Multi-agent ──
    "multiagent": {
        "shared_pool_enabled": False,
        "federation_enabled": False,
        "attestation_enabled": False,
        "default_visibility": "private",
    },
}
