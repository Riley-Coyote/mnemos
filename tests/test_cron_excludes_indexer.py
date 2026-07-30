"""No default cron schedule may run the transcript indexer.

The turnkey scheduler (`mnemos/setup/scheduler.py`) pointedly excludes
transcript indexing, citing a live incident where it wrote ~7,058 engrams
against 13 deliberate captures (543:1) and buried the continuity layer. Two
legacy OpenClaw cron generators still scheduled it every 30 minutes,
contradicting that stance — so a user who installed the legacy jobs got the
flood the scheduler refuses to schedule.

Indexing stays available as the explicit `mnemos index` command; it just must
not be on any default automatic schedule.
"""

from __future__ import annotations


def _blob(obj) -> str:
    import json

    return json.dumps(obj, default=str).lower()


class TestOpenclawCronDefaultsDoNotIndex:
    def test_generate_cron_jobs_has_no_indexer(self):
        from mnemos.openclaw_cron import generate_cron_jobs

        jobs = generate_cron_jobs(agent_id="test")
        names = " ".join(j.get("name", "") for j in jobs).lower()
        assert "index" not in names, f"an indexer job is still scheduled: {names}"
        assert "mnemos index" not in _blob(jobs)
        assert "session_indexer" not in _blob(jobs)


class TestCronInstallerDefaultsDoNotIndex:
    def test_job_definitions_have_no_indexer(self):
        from mnemos.setup.cron_installer import get_job_definitions

        jobs = get_job_definitions(agent_name="Nova", workspace="~/nova")
        names = " ".join(j.get("name", "") for j in jobs).lower()
        assert "index" not in names, f"an indexer job is still scheduled: {names}"
        assert "session_indexer" not in _blob(jobs)

    def test_install_commands_do_not_index(self):
        from mnemos.setup.cron_installer import generate_install_commands

        commands = generate_install_commands(
            agent_name="Nova", agent_id="nova", workspace="~/nova"
        )
        text = (commands if isinstance(commands, str) else "\n".join(commands)).lower()
        assert "session_indexer" not in text
        assert "indexer.session_indexer" not in text
