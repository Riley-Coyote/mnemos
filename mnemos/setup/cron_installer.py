"""Generate OpenClaw CLI commands to install Mnemos cron jobs.

Reads the cron template definitions and outputs ready-to-run openclaw CLI
commands for installing all cron jobs for a given agent.

Usage:
    from mnemos.setup.cron_installer import generate_install_commands
    commands = generate_install_commands(agent_name="Nova", agent_id="nova", workspace="~/nova")
    print(commands)
"""

from __future__ import annotations


# Cron job definitions — these match the templates in openclaw/crons/
_CRON_JOBS = [
    {
        "name": "observer-context-sync",
        "schedule": "*/30 * * * *",
        "model": "claude-sonnet-4-5",
        "timeout": 300,
        "session_target": "isolated",
        "prompt_template": (
            "You are the Observer — a continuity agent for {agent_name}.\n\n"
            "Update memory/active-context.md with current thread state. Steps:\n"
            "1. Read current memory/active-context.md\n"
            "2. Use sessions_list (last 60 min, limit 5)\n"
            "3. For sessions with >4 messages, read transcript via sessions_history\n"
            "4. Update active-context.md — be specific about what's being worked on\n"
            "5. If no recent activity, reply HEARTBEAT_OK\n\n"
            "Keep under 2000 words. Skip cron sessions. Max 3 transcripts."
        ),
    },
    # No session-indexer job. The turnkey scheduler (setup/scheduler.py)
    # deliberately excludes transcript indexing — on one live store it wrote
    # ~7,058 engrams against 13 deliberate captures and buried the continuity
    # layer — and this generator must not contradict it. Indexing stays
    # available as the explicit `mnemos index` command.
    {
        "name": "substrate-tick",
        "schedule": "0 */4 * * *",
        "model": None,  # Uses default agent model
        "timeout": 300,
        "session_target": "isolated",
        "prompt_template": (
            "Run a substrate tick. Execute:\n"
            "cd {workspace} && python3 -m mnemos.substrate.tick\n"
            "Then report the summary (events produced, handled, decayed, modulators).\n"
            "If any handlers fire or beliefs change, note what happened.\n"
            "This is automated consolidation — do not encode new memories, just run the tick and report."
        ),
    },
    {
        "name": "memory-maintenance",
        "schedule": "0 */6 * * *",
        "model": "claude-sonnet-4-5",
        "timeout": 300,
        "session_target": "isolated",
        "prompt_template": (
            "You are the Memory Maintenance agent for {agent_name}.\n\n"
            "Your job: keep {workspace}/MEMORY.md accurate and current by reviewing recent session activity.\n\n"
            "STEPS:\n"
            "1. Read {workspace}/MEMORY.md completely\n"
            "2. Use sessions_list to get sessions from the last 6 hours\n"
            "3. For each session with >4 messages, use sessions_history to read the transcript\n"
            "4. Look for: new facts, changed facts, project updates, completed tasks, new preferences\n"
            "5. For new facts: append to the appropriate section\n"
            "6. For changed facts: update the existing entry\n"
            "7. If nothing to update, reply HEARTBEAT_OK\n\n"
            "Keep it brief. Max 3 sessions. Skip cron sessions."
        ),
    },
    {
        "name": "cross-agent-bridge",
        "schedule": "45 */2 * * *",
        "model": "claude-sonnet-4-5",
        "timeout": 120,
        "session_target": "isolated",
        "prompt_template": (
            "Run the cross-agent bridge sync:\n"
            "python3 -m mnemos.multiagent.shared_pool sync\n"
            "Then report what changed. HEARTBEAT_OK if nothing changed."
        ),
    },
    {
        "name": "morning-brief",
        "schedule": "0 10 * * *",
        "model": None,  # Uses default agent model
        "timeout": 300,
        "session_target": "isolated",
        "prompt_template": (
            "You are {agent_name}'s morning briefing system.\n\n"
            "Generate a morning brief for {user_name}. Include:\n"
            "- Yesterday recap (completed work, key decisions, problems)\n"
            "- Open threads (active work, waiting items, blockers)\n"
            "- Suggested focus (top 3 priorities with rationale)\n"
            "- Stale threads (untouched 3+ days)\n"
            "- Mnemos health (stats, last consolidation/tick)\n"
            "- Cross-agent activity (if multi-agent setup)\n\n"
            "Write to {workspace}/daily/morning-brief-$(date +%Y-%m-%d).md"
        ),
    },
    {
        "name": "daily-debrief",
        "schedule": "0 5 * * *",
        "model": None,  # Uses default agent model
        "timeout": 300,
        "session_target": "isolated",
        "prompt_template": (
            "You are {agent_name}'s daily debrief system.\n\n"
            "Generate an end-of-day debrief for {user_name}. Include:\n"
            "- What got done (completed work items)\n"
            "- Key decisions (with rationale)\n"
            "- Open threads (work in progress, waiting, planned)\n"
            "- Problems & blockers\n"
            "- Cross-agent activity\n"
            "- Memories created today\n"
            "- Tomorrow's candidates\n\n"
            "Write to {workspace}/daily/debrief-$(date +%Y-%m-%d).md"
        ),
    },
]


def generate_install_commands(
    agent_name: str,
    agent_id: str,
    workspace: str,
    user_name: str = "User",
    timezone: str = "America/New_York",
) -> str:
    """Generate OpenClaw CLI commands to install all Mnemos cron jobs.

    Args:
        agent_name: Human-readable agent name.
        agent_id: OpenClaw agent ID.
        workspace: Agent workspace path.
        user_name: User's name for personalization.
        timezone: Timezone for scheduling.

    Returns:
        Multi-line string with all the openclaw CLI commands.
    """
    replacements = {
        "agent_name": agent_name,
        "workspace": workspace,
        "user_name": user_name,
    }

    lines = [
        "# Mnemos Cron Jobs for " + agent_name,
        f"# Agent ID: {agent_id}",
        f"# Timezone: {timezone}",
        "",
        "# Run these commands to install all cron jobs:",
        "",
    ]

    for job in _CRON_JOBS:
        prompt = job["prompt_template"]
        for key, value in replacements.items():
            prompt = prompt.replace(f"{{{key}}}", value)

        # Escape the prompt for shell usage
        escaped_prompt = prompt.replace("'", "'\\''")

        cmd_parts = [
            "openclaw cron add",
            f"  --agent {agent_id}",
            f"  --name mnemos-{job['name']}",
            f"  --schedule '{job['schedule']}'",
            f"  --timezone '{timezone}'",
            f"  --timeout {job['timeout']}",
        ]

        if job.get("model"):
            cmd_parts.append(f"  --model '{job['model']}'")

        if job.get("session_target"):
            cmd_parts.append(f"  --session-target {job['session_target']}")

        cmd_parts.append(f"  --prompt '{escaped_prompt}'")

        lines.append(f"# {job['name']}")
        lines.append(" \\\n".join(cmd_parts))
        lines.append("")

    return "\n".join(lines)


def get_job_definitions(
    agent_name: str,
    workspace: str,
    user_name: str = "User",
) -> list[dict]:
    """Get structured job definitions for programmatic use.

    Args:
        agent_name: Human-readable agent name.
        workspace: Agent workspace path.
        user_name: User's name.

    Returns:
        List of job definition dicts with personalized prompts.
    """
    replacements = {
        "agent_name": agent_name,
        "workspace": workspace,
        "user_name": user_name,
    }

    jobs = []
    for job in _CRON_JOBS:
        prompt = job["prompt_template"]
        for key, value in replacements.items():
            prompt = prompt.replace(f"{{{key}}}", value)

        jobs.append({
            "name": f"mnemos-{job['name']}",
            "schedule": job["schedule"],
            "model": job.get("model"),
            "timeout": job["timeout"],
            "session_target": job.get("session_target", "isolated"),
            "prompt": prompt,
        })

    return jobs
