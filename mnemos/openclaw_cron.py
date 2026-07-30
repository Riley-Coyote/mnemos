"""
OpenClaw cron job templates for Mnemos consolidation.

Generates cron job entries in OpenClaw's jobs.json format. Jobs work by
sending messages to the agent, which then calls Mnemos MCP tools.

Jobs:
- mnemos-shallow: Every 4h — shallow consolidation (decay + connections)
- mnemos-deep: Daily 3am — deep consolidation (all passes)
- mnemos-export: Every 2h — export updated workspace files
- mnemos-substrate-tick: Every 4h — cognitive substrate tick (handlers, modulators)

Transcript indexing is deliberately not scheduled here (it buries the
continuity layer); it stays available as the explicit `mnemos index` command.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path


def generate_cron_jobs(
    agent_id: str = "main",
    timezone: str = "America/New_York",
) -> list[dict]:
    """Generate cron job definitions for OpenClaw.

    Args:
        agent_id: The OpenClaw agent ID to target.
        timezone: Timezone for scheduling.

    Returns:
        List of job dicts in OpenClaw's jobs.json format.
    """
    return [
        {
            "id": f"mnemos-shallow-{uuid.uuid4().hex[:12]}",
            "agentId": agent_id,
            "name": "mnemos-shallow-consolidation",
            "enabled": True,
            "schedule": {
                "kind": "cron",
                "expr": "0 */4 * * *",  # Every 4 hours
                "tz": timezone,
            },
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": (
                    "Run a shallow Mnemos consolidation cycle. "
                    "Use the mnemos_consolidate tool with deep=false. "
                    "Report the results briefly."
                ),
            },
        },
        {
            "id": f"mnemos-deep-{uuid.uuid4().hex[:12]}",
            "agentId": agent_id,
            "name": "mnemos-deep-consolidation",
            "enabled": True,
            "schedule": {
                "kind": "cron",
                "expr": "0 3 * * *",  # Daily at 3am
                "tz": timezone,
            },
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": (
                    "Run a deep Mnemos consolidation cycle. "
                    "Use the mnemos_consolidate tool with deep=true. "
                    "This includes decay, softening, belief review, and reflection. "
                    "After consolidation, export updated workspace files using mnemos_export."
                ),
            },
        },
        {
            "id": f"mnemos-export-{uuid.uuid4().hex[:12]}",
            "agentId": agent_id,
            "name": "mnemos-export-workspace",
            "enabled": True,
            "schedule": {
                "kind": "cron",
                "expr": "30 */2 * * *",  # Every 2 hours at :30
                "tz": timezone,
            },
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": (
                    "Export Mnemos memory to workspace files. "
                    "This updates MEMORY.md, daily logs, and topic files "
                    "so they're fresh for the next conversation."
                ),
            },
        },
        {
            "id": f"mnemos-substrate-{uuid.uuid4().hex[:12]}",
            "agentId": agent_id,
            "name": "mnemos-substrate-tick",
            "enabled": True,
            "schedule": {
                "kind": "cron",
                "expr": "15 */4 * * *",  # Every 4 hours at :15 (offset from consolidation)
                "tz": timezone,
            },
            "sessionTarget": "isolated",
            "payload": {
                "kind": "command",
                "message": "mnemos substrate-tick",
            },
        },
        # No session-indexer job. The turnkey scheduler (setup/scheduler.py)
        # deliberately excludes transcript indexing — on one live store it
        # wrote ~7,058 engrams against 13 deliberate captures and buried the
        # continuity layer — so this generator must not schedule it either.
        # Indexing stays available as the explicit `mnemos index` command.
    ]


def install_cron_jobs(
    jobs: list[dict],
    jobs_file: str = "~/.openclaw/cron/jobs.json",
) -> dict:
    """Install cron jobs into OpenClaw's jobs.json.

    Merges new jobs with existing ones, avoiding duplicates by name.

    Args:
        jobs: List of job dicts to install.
        jobs_file: Path to OpenClaw's jobs.json.

    Returns:
        Result dict with success status and count.
    """
    path = Path(jobs_file).expanduser()

    if not path.exists():
        return {"success": False, "error": f"jobs.json not found: {path}"}

    try:
        existing = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {"success": False, "error": f"Failed to read jobs.json: {e}"}

    # Ensure existing is a list (OpenClaw stores jobs as array)
    if isinstance(existing, dict):
        # Some versions use {"jobs": [...]}
        job_list = existing.get("jobs", [])
    elif isinstance(existing, list):
        job_list = existing
    else:
        return {"success": False, "error": "Unexpected jobs.json format"}

    # Remove existing mnemos jobs (by name prefix)
    existing_names = {j.get("name", "") for j in job_list}
    mnemos_names = {j["name"] for j in jobs}

    # Remove old mnemos jobs
    job_list = [j for j in job_list if not j.get("name", "").startswith("mnemos-")]

    # Add new jobs
    job_list.extend(jobs)

    # Write back
    try:
        # Write in same format as found
        if isinstance(existing, dict):
            existing["jobs"] = job_list
            path.write_text(json.dumps(existing, indent=2))
        else:
            path.write_text(json.dumps(job_list, indent=2))
    except OSError as e:
        return {"success": False, "error": f"Failed to write jobs.json: {e}"}

    return {
        "success": True,
        "jobs_added": len(jobs),
        "total_jobs": len(job_list),
    }
