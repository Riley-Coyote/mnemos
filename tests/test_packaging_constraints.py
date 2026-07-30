"""The declared dependencies must not let a fresh install resolve a broken one.

`pip install mnemos-continuity` resolves dependencies live from PyPI, ignoring
this project's lockfile. When `mcp` was declared as `>=1.0.0` with no ceiling,
a fresh install pulled mcp 2.0 — which removed `mcp.server.fastmcp`, the module
every server entrypoint imports — and produced a server that died on import.
Every CI check passed, because CI installs from the pinned lockfile.

This test guards the *declaration*, so removing the upper bound fails in the
suite. The wheel smoke job in `.github/workflows/release-hardening.yml` guards
the other half — that the bounded range actually resolves to something that
imports when installed unlocked. One without the other is how this shipped.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _dependencies() -> list[Requirement]:
    data = tomllib.loads(PYPROJECT.read_text())
    return [Requirement(dep) for dep in data["project"]["dependencies"]]


def _requirement(name: str) -> Requirement:
    for req in _dependencies():
        if req.name == name:
            return req
    raise AssertionError(f"{name} is not a declared dependency")


class TestMcpIsBoundedBelowTheBreakingMajor:
    def test_mcp_has_an_upper_bound(self):
        """An unbounded range is what let a fresh install pull the broken 2.0."""
        req = _requirement("mcp")
        upper = [s for s in req.specifier if s.operator in ("<", "<=", "==", "~=")]
        assert upper, (
            f"mcp is declared as '{req}', with no upper bound. A fresh "
            "`pip install` will resolve the latest major, which is exactly how "
            "the fastmcp import break shipped."
        )

    def test_the_bound_excludes_2_0(self):
        """The specific version that removed mcp.server.fastmcp must be refused."""
        req = _requirement("mcp")
        assert not req.specifier.contains(Version("2.0.0"), prereleases=True), (
            f"mcp constraint '{req.specifier}' still admits 2.0.0, which does "
            "not ship mcp.server.fastmcp"
        )

    def test_a_working_version_is_still_allowed(self):
        """The bound must not be so tight it forbids a version that works."""
        req = _requirement("mcp")
        assert req.specifier.contains(Version("1.29.0")), (
            f"mcp constraint '{req.specifier}' excludes 1.29.0, a version whose "
            "mcp.server.fastmcp imports cleanly"
        )


class TestTheImportThatBrokeStillWorks:
    def test_fastmcp_entrypoint_imports_in_this_environment(self):
        """The construction that fails under mcp 2.0 must succeed under the pin.

        Importing the module constructs the FastMCP server and registers every
        simple tool at module scope, so a clean import is a real check of the
        surface, not just of the dependency line.
        """
        pytest.importorskip("mcp", reason="mcp extra not installed")
        import asyncio

        from mnemos.simple_mcp import SIMPLE_TOOL_NAMES, simple_mcp

        tools = asyncio.run(simple_mcp.list_tools())
        registered = {t.name for t in tools}
        assert registered == set(SIMPLE_TOOL_NAMES), (
            f"registered tools {sorted(registered)} do not match the declared "
            f"simple surface {sorted(SIMPLE_TOOL_NAMES)}"
        )
