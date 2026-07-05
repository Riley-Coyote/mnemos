#!/usr/bin/env python3
"""mnemos-vault-watchdog — periodic identity-tier ↔ journal reconciliation.

Extends the mnemos-identity-watchdog pattern (checksum tripwire) with the T4
vault's journal reconciliation: it walks the decision journal against the
identity-tier rows in memory.db, in BOTH directions, and re-quarantines any row
that no longer matches a witnessed decision (orphan, forged ref, de-tiered,
content-mutated, lifecycle-hidden, or unverified). It also reports a
present-but-agent-owned journal as `journal_untrusted` and fails closed the same
way it does for corruption. Findings are written to a log and surfaced to
Oliver Inbox.

Scheduling and installation are David's (a LaunchAgent, like the existing
watchdog). This wrapper only runs one pass. It uses the ordinary EngramStore, so
it must run inside the mnemos venv (unlike the TCB, which is stdlib-only).

Usage:
    mnemos-vault-watchdog.py

DB and journal paths are pinned below. Environment variables cannot redirect
the trust-bearing read paths; only log/alert destinations are env-configurable.

Exit codes:
    0 — reconciled clean (no findings)
    2 — memory.db missing
    5 — findings present (re-quarantine applied where possible)
"""

from __future__ import annotations

import datetime
import os
import pathlib
import sys

# 008m Addition 1 + 008r-review (watchdog-production-redirect-flags): the
# watchdog is the INDEPENDENT DETECTOR. It must read the REAL journal + DB
# regardless of any agent process's environment OR command line — otherwise the
# same redirection that points the store at a fake journal points the detector
# there too, and the "caught within one cycle" tamper-evidence claim silently
# becomes false. So NO env honor AND NO --db/--journal flags: the watchdog runs
# under launchd (no sudo to gate its args), so an exposed flag is an UNGUARDED
# redirect vector for the detector. The canonical paths are PINNED. Tests inject
# by loading this module and overriding DB_PATH / JOURNAL_PATH directly — a seam
# a subprocess or launchd never reaches into.
CANONICAL_DB_PATH = "/Users/davidef/.mnemos/memory.db"
CANONICAL_JOURNAL_PATH = "/usr/local/var/mnemos-vault/decisions.jsonl"

DB_PATH = pathlib.Path(CANONICAL_DB_PATH)
JOURNAL_PATH = CANONICAL_JOURNAL_PATH
# Log/alert dirs stay env-configurable — they're output sinks, not the
# trust-bearing read paths; poisoning them can only misdirect an alert, not
# hide a divergence (the exit code still reflects the real reconcile).
LOG_DIR = pathlib.Path(
    os.environ.get(
        "MNEMOS_WATCHDOG_LOG_DIR", os.path.expanduser("~/Library/Logs/mnemos")
    )
)
ALERT_DIR = pathlib.Path(
    os.environ.get("MNEMOS_WATCHDOG_ALERT_DIR", os.path.expanduser("~/Oliver Inbox"))
)


def _log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (LOG_DIR / "vault-watchdog.log").open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def _alert(report) -> None:
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    path = ALERT_DIR / f"{today}-vault-watchdog-alert.md"
    lines = [
        "Oliver — vault watchdog found identity-tier divergence.",
        "",
        f"Journal: {JOURNAL_PATH}",
        f"DB: {DB_PATH}",
        f"Chain OK: {report.chain_ok} (break at {report.chain_break_index})",
        "",
        "## Findings",
    ]
    for finding in report.findings:
        lines.append(
            f"- [{finding['severity']}] {finding['kind']}: {finding['detail']} "
            f"(table={finding['table']}, row={finding['row_id']}, "
            f"proposal={finding['proposal_id']})"
        )
    lines.append("")
    lines.append(f"## Re-quarantined ({len(report.requarantined)})")
    for item in report.requarantined:
        lines.append(f"- {item['table']}/{item['row_id']}: {item['detail']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    if not DB_PATH.exists():
        sys.stderr.write(f"memory.db not found: {DB_PATH}\n")
        return 2
    from mnemos.store.sqlite_store import EngramStore

    # 008m Addition 1 + 008r/008y: the watchdog reads the PINNED canonical
    # journal path, NOT an environment variable, command-line flag, or resolver
    # decision. An env/arg-poisonable detector defeats the independence that
    # makes tamper-evidence real. If the canonical journal is absent,
    # unreadable, corrupt, or agent-owned, that itself is a divergence; reconcile
    # against the canonical path fails closed and quarantines, which is the
    # correct alarm.
    resolved = os.path.expanduser(str(JOURNAL_PATH))
    if not resolved:
        sys.stderr.write("no canonical journal path configured\n")
        return 2
    # 008g-r5 #3: catch reconcile failures — a corrupt journal, permission
    # error, or reconcile bug must land in the log AND Oliver Inbox (so David
    # sees it), not in launchd stderr where it disappears. Fail-loud is the
    # correct posture: a watchdog that silently exits non-zero on a broken
    # journal is worse than no watchdog.
    try:
        store = EngramStore(str(DB_PATH))
        report = store.reconcile_identity_vault(resolved)
    except Exception as exc:
        _log(f"WATCHDOG_ERROR: reconcile crashed: {type(exc).__name__}: {exc}")
        ALERT_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.date.today().isoformat()
        (ALERT_DIR / f"{today}-vault-watchdog-error.md").write_text(
            "Oliver — vault watchdog crashed. Journal may be corrupt or unreadable.\n\n"
            f"Journal: {resolved}\nDB: {DB_PATH}\n\n"
            f"Error: {type(exc).__name__}: {exc}\n\n"
            "Investigate before the next session-start reconcile fires.\n",
            encoding="utf-8",
        )
        return 6
    if report.ok:
        _log("clean: identity tier reconciled against journal")
        return 0
    _log(
        f"DIVERGENCE: {len(report.findings)} finding(s), "
        f"{len(report.requarantined)} re-quarantined"
    )
    _alert(report)
    return 5


if __name__ == "__main__":
    sys.exit(main())
