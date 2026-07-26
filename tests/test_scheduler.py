"""Background maintenance scheduled by the OS, not by OpenClaw.

These jobs run unattended, on someone else's machine, forever. Nobody
reads their logs. A job that installs cleanly and then fails on every
invocation is indistinguishable from one that works, so the generation
is asserted directly rather than trusted.
"""

import json
import plistlib

import pytest

from mnemos.setup import scheduler


def _seed_home(tmp_path):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    return home


class TestJobSelection:
    def test_model_dependent_jobs_are_omitted_without_a_provider(self):
        """A job that can only no-op should not wake up every 30 minutes."""
        without = {job.name for job in scheduler.jobs_for(has_model=False)}
        with_model = {job.name for job in scheduler.jobs_for(has_model=True)}

        assert "index" not in without
        assert "index" in with_model
        # Everything deterministic runs regardless.
        assert {"maintain", "maintain-deep", "substrate-tick"} <= without

    def test_every_job_has_exactly_one_schedule(self):
        for job in scheduler.JOBS:
            assert (job.interval_seconds is None) != (job.daily_at is None), job.name

    def test_a_job_cannot_declare_two_schedules(self):
        with pytest.raises(ValueError):
            scheduler.SchedulerJob(
                name="bad",
                args=("consolidate",),
                description="",
                interval_seconds=3600,
                daily_at=(3, 0),
            )


class TestCommandGeneration:
    def test_scope_flags_precede_the_subcommand(self):
        """The Mnemos CLI takes scope as *global* options.

        Appending them instead produces a command that installs fine and
        then fails with "unrecognized arguments" on every scheduled run,
        into a log nobody reads. This is the failure this test exists for.
        """
        job = scheduler.JOBS[0]
        argv = scheduler.command_for(
            job,
            mnemos_command="/usr/local/bin/mnemos",
            scope_args=("--agent-id", "nova", "--db-path", "/tmp/nova.db"),
        )

        assert argv[0] == "/usr/local/bin/mnemos"
        subcommand_index = argv.index("consolidate")
        assert argv.index("--agent-id") < subcommand_index
        assert argv.index("--db-path") < subcommand_index

    def test_deep_maintenance_keeps_its_subcommand_flag_after_the_subcommand(self):
        deep = next(j for j in scheduler.JOBS if j.name == "maintain-deep")
        argv = scheduler.command_for(
            deep, mnemos_command="mnemos", scope_args=("--agent-id", "nova")
        )
        assert argv.index("--deep") > argv.index("consolidate")


class TestLaunchd:
    def test_interval_job_uses_start_interval(self):
        job = next(j for j in scheduler.JOBS if j.name == "maintain")
        payload = plistlib.loads(
            scheduler.launchd_plist(job, agent_id="nova", mnemos_command="mnemos")
        )

        assert payload["Label"] == "com.mnemos.nova.maintain"
        assert payload["StartInterval"] == 4 * 3600
        assert "StartCalendarInterval" not in payload
        # Maintenance must not fight for resources at login.
        assert payload["RunAtLoad"] is False

    def test_daily_job_uses_a_calendar_interval(self):
        job = next(j for j in scheduler.JOBS if j.name == "maintain-deep")
        payload = plistlib.loads(
            scheduler.launchd_plist(job, agent_id="nova", mnemos_command="mnemos")
        )

        assert payload["StartCalendarInterval"] == {"Hour": 3, "Minute": 0}
        assert "StartInterval" not in payload

    def test_labels_are_namespaced_per_agent(self):
        """Two agents on one machine must not overwrite each other's jobs."""
        job = scheduler.JOBS[0]
        nova = scheduler.launchd_plist_path("nova", job)
        vektor = scheduler.launchd_plist_path("vektor", job)
        assert nova != vektor


class TestSystemd:
    def test_interval_timer_reruns_and_survives_downtime(self):
        job = next(j for j in scheduler.JOBS if j.name == "maintain")
        service, timer = scheduler.systemd_units(
            job, agent_id="nova", mnemos_command="mnemos"
        )

        assert "Type=oneshot" in service
        assert "ExecStart=mnemos" in service
        assert "OnUnitActiveSec=14400s" in timer
        # A laptop asleep at the scheduled time should still consolidate.
        assert "Persistent=true" in timer
        assert "WantedBy=timers.target" in timer

    def test_daily_timer_uses_oncalendar(self):
        job = next(j for j in scheduler.JOBS if j.name == "maintain-deep")
        _service, timer = scheduler.systemd_units(
            job, agent_id="nova", mnemos_command="mnemos"
        )
        assert "OnCalendar=*-*-* 03:00:00" in timer


class TestCrontab:
    def test_interval_and_daily_lines(self):
        maintain = next(j for j in scheduler.JOBS if j.name == "maintain")
        deep = next(j for j in scheduler.JOBS if j.name == "maintain-deep")

        assert scheduler.cron_line(
            maintain, agent_id="nova", mnemos_command="mnemos"
        ).startswith("0 */4 * * *")
        assert scheduler.cron_line(
            deep, agent_id="nova", mnemos_command="mnemos"
        ).startswith("0 3 * * *")

    def test_merge_preserves_a_users_own_crontab(self):
        """A user's crontab is theirs; we only remove lines we wrote."""
        existing = "\n".join([
            "0 9 * * * /usr/bin/backup.sh",
            "*/5 * * * * /home/me/poll.sh  # unrelated",
        ])
        new = [scheduler.cron_line(
            scheduler.JOBS[0], agent_id="nova", mnemos_command="mnemos"
        )]

        merged = scheduler.merge_cron_lines(existing, new, "nova")

        assert "/usr/bin/backup.sh" in merged
        assert "/home/me/poll.sh" in merged
        assert scheduler.CRON_MARKER in merged

    def test_reinstall_replaces_rather_than_stacks(self):
        line = scheduler.cron_line(
            scheduler.JOBS[0], agent_id="nova", mnemos_command="mnemos"
        )
        merged = scheduler.merge_cron_lines("", [line], "nova")
        for _ in range(3):
            merged = scheduler.merge_cron_lines(merged, [line], "nova")

        assert merged.count(scheduler.CRON_MARKER) == 1

    def test_another_agents_jobs_are_left_alone(self):
        nova_line = scheduler.cron_line(
            scheduler.JOBS[0], agent_id="nova", mnemos_command="mnemos"
        )
        vektor_line = scheduler.cron_line(
            scheduler.JOBS[0], agent_id="vektor", mnemos_command="mnemos"
        )
        existing = scheduler.merge_cron_lines("", [nova_line], "nova")

        merged = scheduler.merge_cron_lines(existing, [vektor_line], "vektor")

        assert ":nova:" in merged
        assert ":vektor:" in merged

    def test_uninstall_removes_only_our_lines(self):
        line = scheduler.cron_line(
            scheduler.JOBS[0], agent_id="nova", mnemos_command="mnemos"
        )
        existing = scheduler.merge_cron_lines("0 9 * * * /usr/bin/backup.sh", [line], "nova")

        cleared = scheduler.merge_cron_lines(existing, [], "nova")

        assert "/usr/bin/backup.sh" in cleared
        assert scheduler.CRON_MARKER not in cleared


class TestBackendDetection:
    def test_macos_uses_launchd(self):
        assert scheduler.detect_backend("darwin") == "launchd"

    def test_linux_prefers_systemd_then_crontab(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/bin/systemctl")
        assert scheduler.detect_backend("linux") == "systemd"

        monkeypatch.setattr(
            "shutil.which", lambda name: "/bin/crontab" if name == "crontab" else None
        )
        assert scheduler.detect_backend("linux") == "crontab"

        monkeypatch.setattr("shutil.which", lambda name: None)
        assert scheduler.detect_backend("linux") == "unsupported"


class TestTccWarning:
    """macOS blocks scheduled jobs from reading ~/Documents and friends.

    The same command works by hand and fails on every scheduled run with a
    permission error, into a log file. Found by actually installing and
    kickstarting a job rather than by reading the code.
    """

    def test_warns_for_a_protected_install_location(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        protected = tmp_path / "Documents" / "repo" / ".venv" / "bin" / "mnemos"
        protected.parent.mkdir(parents=True)
        protected.touch()

        warning = scheduler.tcc_warning(str(protected), backend="launchd")

        assert warning is not None
        assert "Documents" in warning

    def test_silent_for_a_normal_install_location(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert scheduler.tcc_warning("/opt/homebrew/bin/mnemos", backend="launchd") is None

    def test_only_applies_to_launchd(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        protected = tmp_path / "Documents" / "bin" / "mnemos"
        protected.parent.mkdir(parents=True)
        protected.touch()
        assert scheduler.tcc_warning(str(protected), backend="systemd") is None


class TestPlan:
    def test_plan_describes_without_touching_the_system(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))

        blueprint = scheduler.plan(
            agent_id="nova",
            mnemos_command="/usr/local/bin/mnemos",
            scope_args=("--agent-id", "nova"),
            has_model=False,
            backend="launchd",
        )

        assert blueprint["backend"] == "launchd"
        assert {e["job"].name for e in blueprint["entries"]} == {
            "maintain", "maintain-deep", "substrate-tick",
        }
        assert [j.name for j in blueprint["skipped"]] == ["index"]
        for entry in blueprint["entries"]:
            assert not entry["path"].exists(), "plan() must not create anything"


class TestDaemonCli:
    def test_dry_run_schedules_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        assert main(["daemon", "install", "--agent-id", "nova"]) == 0
        out = capsys.readouterr().out
        assert "preview" in out.lower()
        assert not (tmp_path / "home" / "Library" / "LaunchAgents").exists()

    def test_status_reports_when_nothing_is_scheduled(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        assert main(["daemon", "status", "--agent-id", "nova"]) == 0
        out = capsys.readouterr().out
        assert "not scheduled" in out
        assert "mnemos daemon install --write" in out
