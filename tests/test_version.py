"""The package must not misreport its own version.

`mnemos.__version__` was hardcoded to "0.1.0" while pyproject declared
0.2.0. Nothing failed: the suite passed, CI was green, the wheel built and
installed, and the readiness pass reported ready. A released package would
have told every user and every bug report it was a version it was not, and
a PyPI version can never be replaced once uploaded.

This is the same shape as every serious bug this project has had — a layer
reporting success while carrying something wrong — so it gets a test rather
than a corrected constant.
"""

from __future__ import annotations

import re
from pathlib import Path

import mnemos

# tomllib is 3.11+, and this package supports 3.10, so the parse degrades to a
# regex rather than adding a dependency for one assertion.
try:  # pragma: no cover - import branch
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


def _declared_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if tomllib is not None:
        with pyproject.open("rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    text = pyproject.read_text(encoding="utf-8")
    project = re.search(r"^\[project\]$(.*?)^\[", text, re.M | re.S)
    section = project.group(1) if project else text
    match = re.search(r'^version\s*=\s*"([^"]+)"', section, re.M)
    assert match, "could not find version in pyproject.toml"
    return match.group(1)


def test_version_matches_pyproject():
    """The runtime version and the declared version cannot drift apart."""
    assert mnemos.__version__ == _declared_version()


def test_version_is_not_the_unknown_fallback():
    """Running against an installed package, metadata must actually resolve.

    The fallback exists for a bare source tree. If it ever fires in a real
    install, __version__ is silently wrong again — which is the failure this
    module exists to prevent.
    """
    assert mnemos.__version__ != "0.0.0+unknown"


def test_version_is_a_plausible_release_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", mnemos.__version__), mnemos.__version__
