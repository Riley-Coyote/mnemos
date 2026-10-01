"""Tests write only inside their own temporary directories.

The suite ran with the developer's HOME, so whatever resolved a default path
wrote into the real ~/.mnemos: bootstrap with no store path made a store
named audit.db there on every run (test_security_boundaries), the daemon's
installer made its log folder (test_scheduler), and the cue's shown files
went to ~/.mnemos/run (ten test files). Every test now has a home of its own
under pytest's temporary directory (tests/conftest.py).
"""

from __future__ import annotations

from pathlib import Path


def test_every_test_has_a_home_of_its_own(tmp_path_factory):
    home = Path.home().resolve()
    assert tmp_path_factory.getbasetemp().resolve() in home.parents


def test_a_store_made_by_default_lands_in_the_tests_home(tmp_path, tmp_path_factory):
    # What leaked: bootstrap given no store path makes ~/.mnemos/<agent>.db.
    from mnemos.setup.bootstrap import bootstrap

    result = bootstrap(
        workspace=str(tmp_path / "workspace"), agent_name="Audit", agent_id="audit",
    )
    store = Path(result["db_path"]).expanduser().resolve()
    assert store.name == "audit.db"
    assert store.exists()
    assert tmp_path_factory.getbasetemp().resolve() in store.parents
