"""Native background scheduling for Mnemos maintenance.

Memory that only works while a session happens to be open is not a memory
system. Consolidation, decay, connection discovery and the substrate tick
are what make continuity feel alive between conversations — and until now
the only way to schedule them was OpenClaw's cron templates, which most
users do not have.

None of that work was ever OpenClaw-specific. ``mnemos consolidate``,
``mnemos substrate-tick`` and ``mnemos index`` are plain CLI commands;
only the *scheduling* was bound to OpenClaw. This module supplies the
missing half with whatever the host actually provides: launchd on macOS,
systemd user timers on Linux, and plain crontab where systemd is absent.

Everything here is pure generation. Nothing touches the system until the
CLI decides to write, which keeps the whole surface testable without
shelling out to ``launchctl`` or ``systemctl``.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SchedulerJob:
    """One scheduled maintenance job.

    Either ``interval_seconds`` (run every N seconds) or ``daily_at``
    (run once a day at HH:MM) must be set, never both.
    """

    name: str
    args: tuple[str, ...]
    description: str
    interval_seconds: int | None = None
    daily_at: tuple[int, int] | None = None
    requires_model: bool = False

    def __post_init__(self) -> None:
        if (self.interval_seconds is None) == (self.daily_at is None):
            raise ValueError(
                f"job {self.name}: set exactly one of interval_seconds or daily_at"
            )

    @property
    def schedule_description(self) -> str:
        if self.daily_at is not None:
            return f"daily at {self.daily_at[0]:02d}:{self.daily_at[1]:02d}"
        assert self.interval_seconds is not None
        hours, remainder = divmod(self.interval_seconds, 3600)
        minutes = remainder // 60
        if hours and minutes:
            return f"every {hours}h {minutes}m"
        if hours:
            return f"every {hours}h"
        return f"every {minutes}m"


# The default schedule. Deliberately conservative: these run on someone
# else's machine, unattended, forever.
JOBS: tuple[SchedulerJob, ...] = (
    SchedulerJob(
        name="maintain",
        args=("consolidate",),
        description="Decay and connection discovery",
        interval_seconds=4 * 3600,
    ),
    SchedulerJob(
        name="maintain-deep",
        args=("consolidate", "--deep"),
        description="Softening, belief review and reflection",
        daily_at=(3, 0),
    ),
    SchedulerJob(
        name="substrate-tick",
        args=("substrate-tick",),
        description="Cognitive substrate cycle: handlers and modulators",
        interval_seconds=4 * 3600,
    ),
    SchedulerJob(
        name="index",
        args=("index",),
        description="Index recent session transcripts into memory",
        interval_seconds=30 * 60,
        # Extraction is model-mediated; without a provider this job would
        # wake up every half hour to do nothing.
        requires_model=True,
    ),
)


def jobs_for(*, has_model: bool) -> list[SchedulerJob]:
    """The jobs worth scheduling given what this install can actually do."""
    return [job for job in JOBS if has_model or not job.requires_model]


def model_is_configured() -> bool:
    """Whether a dedicated model provider is available for model-mediated jobs."""
    try:
        from ..simple_runtime import _dedicated_model_requested

        return bool(_dedicated_model_requested())
    except Exception:
        return False


def detect_backend(system: str | None = None) -> str:
    """Pick the scheduler this host actually provides.

    Returns "launchd", "systemd", "crontab", or "unsupported".
    """
    system = (system or sys.platform).lower()
    if system.startswith("darwin"):
        return "launchd"
    if system.startswith("linux"):
        if shutil.which("systemctl"):
            return "systemd"
        if shutil.which("crontab"):
            return "crontab"
        return "unsupported"
    if shutil.which("crontab"):
        return "crontab"
    return "unsupported"


def label_for(agent_id: str, job: SchedulerJob) -> str:
    """Stable identifier for a job, namespaced per agent.

    Agent-scoped so several agents can each keep their own maintenance on
    one machine without overwriting each other's units.
    """
    return f"com.mnemos.{agent_id}.{job.name}"


def log_path_for(agent_id: str, job: SchedulerJob) -> str:
    return str(Path.home() / ".mnemos" / "logs" / f"{agent_id}-{job.name}.log")


def command_for(
    job: SchedulerJob,
    *,
    mnemos_command: str,
    scope_args: tuple[str, ...] = (),
) -> list[str]:
    """The full argv this job runs.

    Scope flags are global options on the Mnemos CLI, so they must precede
    the subcommand. Appending them instead produces a command that installs
    cleanly and then fails with "unrecognized arguments" on every scheduled
    run, into a log nobody reads.
    """
    return [mnemos_command, *scope_args, *job.args]


# ─────────────────────────── launchd (macOS) ───────────────────────────


def launchd_plist(
    job: SchedulerJob,
    *,
    agent_id: str,
    mnemos_command: str,
    scope_args: tuple[str, ...] = (),
) -> bytes:
    """A launchd user-agent plist for one job."""
    label = label_for(agent_id, job)
    log = log_path_for(agent_id, job)
    payload: dict[str, object] = {
        "Label": label,
        "ProgramArguments": command_for(
            job, mnemos_command=mnemos_command, scope_args=scope_args
        ),
        # Maintenance must never race a login or fight for resources at
        # boot; it runs on its own schedule, not at load.
        "RunAtLoad": False,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
        # Missed runs (laptop asleep) should still happen once on wake
        # rather than being silently skipped until the next interval.
        "LowPriorityIO": True,
    }
    if job.daily_at is not None:
        hour, minute = job.daily_at
        payload["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    else:
        payload["StartInterval"] = job.interval_seconds
    return plistlib.dumps(payload, sort_keys=True)


def launchd_plist_path(agent_id: str, job: SchedulerJob) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label_for(agent_id, job)}.plist"


# ─────────────────────────── systemd (Linux) ───────────────────────────


def systemd_units(
    job: SchedulerJob,
    *,
    agent_id: str,
    mnemos_command: str,
    scope_args: tuple[str, ...] = (),
) -> tuple[str, str]:
    """The (service, timer) unit files for one job."""
    argv = command_for(job, mnemos_command=mnemos_command, scope_args=scope_args)
    exec_start = " ".join(argv)
    service = "\n".join([
        "[Unit]",
        f"Description=Mnemos {job.name} ({job.description})",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={exec_start}",
        "",
    ])
    if job.daily_at is not None:
        hour, minute = job.daily_at
        on = f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00"
    else:
        assert job.interval_seconds is not None
        on = f"OnUnitActiveSec={job.interval_seconds}s\nOnBootSec=15min"
    timer = "\n".join([
        "[Unit]",
        f"Description=Mnemos {job.name} schedule ({job.schedule_description})",
        "",
        "[Timer]",
        on,
        # A laptop that was asleep at 03:00 should still consolidate.
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    return service, timer


def systemd_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def systemd_unit_names(agent_id: str, job: SchedulerJob) -> tuple[str, str]:
    stem = f"mnemos-{agent_id}-{job.name}"
    return f"{stem}.service", f"{stem}.timer"


# ─────────────────────────── crontab (fallback) ───────────────────────────

CRON_MARKER = "# mnemos-scheduler"


def cron_line(
    job: SchedulerJob,
    *,
    agent_id: str,
    mnemos_command: str,
    scope_args: tuple[str, ...] = (),
) -> str:
    """One crontab line, tagged so we can find our own entries again."""
    if job.daily_at is not None:
        hour, minute = job.daily_at
        schedule = f"{minute} {hour} * * *"
    else:
        assert job.interval_seconds is not None
        minutes = job.interval_seconds // 60
        if minutes < 60:
            schedule = f"*/{minutes} * * * *"
        else:
            schedule = f"0 */{minutes // 60} * * *"
    argv = command_for(job, mnemos_command=mnemos_command, scope_args=scope_args)
    log = log_path_for(agent_id, job)
    return (
        f"{schedule} {' '.join(argv)} >> {log} 2>&1 "
        f"{CRON_MARKER}:{agent_id}:{job.name}"
    )


def merge_cron_lines(existing: str, new_lines: list[str], agent_id: str) -> str:
    """Replace this agent's Mnemos entries, preserving everything else.

    A user's crontab is theirs. We only ever remove lines we previously
    wrote, identified by our own marker, and never reorder or drop
    anything else in the file.
    """
    tag = f"{CRON_MARKER}:{agent_id}:"
    kept = [line for line in existing.splitlines() if tag not in line]
    while kept and not kept[-1].strip():
        kept.pop()
    merged = kept + new_lines
    return "\n".join(merged) + "\n"


def read_crontab() -> str:
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return ""
    return result.stdout if result.returncode == 0 else ""


# macOS restricts these directories through TCC. A launchd agent does not
# inherit the Full Disk Access a terminal may have been granted, so a
# Mnemos installed under one of them runs fine by hand and fails on every
# scheduled invocation with EPERM — silently, into a log file.
_TCC_PROTECTED_DIRS = ("Documents", "Desktop", "Downloads")


def tcc_warning(mnemos_command: str, *, backend: str) -> str | None:
    """Warn when scheduled jobs will be blocked by macOS privacy protection.

    Returns None when there is nothing to worry about.
    """
    if backend != "launchd":
        return None
    try:
        resolved = Path(mnemos_command).resolve()
        relative = resolved.relative_to(Path.home())
    except (ValueError, OSError):
        return None

    top = relative.parts[0] if relative.parts else ""
    if top not in _TCC_PROTECTED_DIRS:
        return None

    return (
        f"Warning: this Mnemos runs from ~/{top}, which macOS protects.\n"
        "  Scheduled jobs do not inherit Full Disk Access, so they will fail\n"
        "  with a permission error even though the same command works by hand.\n"
        "  Either install Mnemos outside that folder (pipx install\n"
        "  mnemos-continuity), or grant Full Disk Access to the interpreter\n"
        "  in System Settings > Privacy & Security."
    )


def plan(
    *,
    agent_id: str,
    mnemos_command: str,
    scope_args: tuple[str, ...] = (),
    has_model: bool | None = None,
    backend: str | None = None,
) -> dict:
    """Describe exactly what an install would do, without doing any of it."""
    backend = backend or detect_backend()
    if has_model is None:
        has_model = model_is_configured()
    selected = jobs_for(has_model=has_model)
    skipped = [job for job in JOBS if job not in selected]

    entries: list[dict] = []
    for job in selected:
        entry: dict = {
            "job": job,
            "schedule": job.schedule_description,
            "command": " ".join(
                command_for(job, mnemos_command=mnemos_command, scope_args=scope_args)
            ),
            "log": log_path_for(agent_id, job),
        }
        if backend == "launchd":
            entry["path"] = launchd_plist_path(agent_id, job)
            entry["content"] = launchd_plist(
                job,
                agent_id=agent_id,
                mnemos_command=mnemos_command,
                scope_args=scope_args,
            )
        elif backend == "systemd":
            service_name, timer_name = systemd_unit_names(agent_id, job)
            service, timer = systemd_units(
                job,
                agent_id=agent_id,
                mnemos_command=mnemos_command,
                scope_args=scope_args,
            )
            entry["units"] = {
                systemd_unit_dir() / service_name: service,
                systemd_unit_dir() / timer_name: timer,
            }
            entry["timer_name"] = timer_name
        elif backend == "crontab":
            entry["line"] = cron_line(
                job,
                agent_id=agent_id,
                mnemos_command=mnemos_command,
                scope_args=scope_args,
            )
        entries.append(entry)

    return {
        "backend": backend,
        "agent_id": agent_id,
        "entries": entries,
        "skipped": skipped,
        "has_model": has_model,
    }
