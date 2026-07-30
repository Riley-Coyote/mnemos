#!/usr/bin/env python
"""Distribution-readiness proof for Mnemos, run against the real artifact.

Not a unit test — this exercises the shipped wheel the way a stranger would,
and measures the five philosophical shifts on a keyless store, so a launch can
claim what is actually true. Run from the repo root with the dev environment:

    uv run --extra dev --extra mcp python scripts/readiness_check.py

It prints a PASS/FAIL line per check and a JSON block of the mind-state numbers
for the readiness report, and exits non-zero if anything fails.

Checks
  C1  a fresh, keyless `pip install` of the built wheel yields a server whose
      8 simple tools list over the real stdio protocol, and a capture survives
      a process restart — the amnesia gate against the actual artifact.
  C2  on a keyless store, all five shifts are alive (impact coverage,
      distilled-into edges, typed edges beyond 'supports', identity rows, and
      surprise on a genuinely novel capture).
  C3  `mnemos doctor` reports a real store honestly.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


# ─────────────────────────── C1: the shipped wheel ───────────────────────────

def build_and_install_wheel(workdir: Path) -> Path:
    """Build the wheel and install it UNLOCKED into a clean venv.

    Unlocked (no uv.lock) so dependencies resolve from PyPI exactly as a user's
    `pip install` does — the only way to catch a bad declared dependency range.
    Returns the venv's `mnemos` executable path.
    """
    dist = workdir / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=REPO, check=True, capture_output=True, text=True,
    )
    wheel = next(dist.glob("mnemos*.whl"))

    venv = workdir / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                   capture_output=True)
    pip = venv / "bin" / "pip"
    subprocess.run([str(pip), "install", "--quiet", "--upgrade", "pip"],
                   check=True, capture_output=True)
    subprocess.run([str(pip), "install", "--quiet", str(wheel)],
                   check=True, capture_output=True, text=True)
    return venv / "bin" / "mnemos"


def c1_stdio_and_restart(mnemos_bin: Path, home: Path) -> None:
    """Drive the installed server over real stdio, then prove a restart remembers."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    db = str(home / ".mnemos" / "readiness.db")
    token = f"cold-brew-{uuid.uuid4().hex[:12]}"
    env = {
        "HOME": str(home),
        "PATH": f"{mnemos_bin.parent}:/usr/bin:/bin",
        "MNEMOS_DISABLE_DOTENV": "1",
    }

    def params():
        return StdioServerParameters(
            command=str(mnemos_bin),
            args=["serve", "--mode", "simple", "--db-path", db,
                  "--agent-id", "readiness", "--person-id", "tester",
                  "--project-scope", "check"],
            env=env,
        )

    async def journey():
        # Session one: list the tools, then capture.
        async with stdio_client(params()) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                check("C1 stdio: 8 simple tools list over the wire",
                      len(tools) == 8, f"{len(tools)} tools")
                await s.call_tool(
                    "mnemos_capture",
                    {"content": f"Riley drinks {token} every morning"},
                )
        # Session two: a fresh process. Nothing carried but the filesystem.
        async with stdio_client(params()) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                result = await s.call_tool("mnemos_recall", {"query": "morning drink"})
                text = "\n".join(
                    b.text for b in result.content
                    if getattr(b, "type", None) == "text"
                )
                check("C1 restart: a capture survives a new process (real wheel)",
                      token in text, "recall returned the earlier capture")

    asyncio.run(journey())


# ─────────────────────────── C2: the five shifts ─────────────────────────────

def c2_mind_state(workdir: Path) -> dict:
    """Build a keyless store where every shift should fire, then measure it."""
    sys.path.insert(0, str(REPO))
    import sqlite3

    from benchmarks.continuity_eval import mind_state
    from mnemos.consolidation.softening import run_softening_pass
    from mnemos.simple_runtime import MnemosRuntime

    db = str(workdir / "mind.db")
    rt = MnemosRuntime(db_path=db, agent_id="mind", person_id="tester",
                       project_scope="check")

    # Captures each carrying a real (agent) impact → shift-1 coverage.
    facts = [
        ("Riley ships through PRs and never pushes to main", "changes how I land work"),
        ("Riley prefers concise answers with no preamble", "changes how I write to him"),
        ("Mnemos is a continuity layer, not a memory system", "reframes the whole product"),
        ("The release SOP is branch, PR, CI watch, merge", "changes how I ship"),
        ("Riley works best at night, 2-4am peak", "changes when I expect deep work"),
        ("Verify by running, never by reading code", "the core discipline here"),
    ]
    for content, impact in facts:
        rt.capture(content=content, importance="high", impact=impact)

    # Surprise is an encode-time signal: a genuinely novel capture should
    # register non-zero prediction error.
    engram = rt._encoder.encode(
        content="An entirely unrelated fact about deep-sea bioluminescent squid",
        agent_id="mind",
    )
    surprise = float(getattr(engram.encoding_context, "surprise_level", 0.0) or 0.0)

    # Age every engram and drop its accessibility so softening fires and
    # distills lessons (DISTILLED_INTO) — shift 2, which only manifests once
    # memory has begun to fade.
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE engrams SET created_at='2020-01-01T00:00:00+00:00', "
        "last_accessed='2020-01-01T00:00:00+00:00', accessibility=0.2"
    )
    conn.commit()
    conn.close()
    run_softening_pass(store=rt._store, config={}, llm_client=None, agent_id="mind")
    rt.maintain(deep=False)  # identity pass + connection discovery
    rt.close()

    state = mind_state(db)
    state["shift_3_surprise"] = {"surprise_level": round(surprise, 3),
                                 "alive": surprise > 0.0}

    check("C2 shift 1 — traces (impact coverage)",
          state["shift_1_traces"]["alive"],
          f"{state['shift_1_traces']['impact_coverage']:.0%}")
    check("C2 shift 2 — forgetting that teaches (distilled-into)",
          state["shift_2_lessons"]["alive"],
          f"{state['shift_2_lessons']['distilled_into']} edges")
    check("C2 shift 3 — surprise as growth (encode-time)",
          state["shift_3_surprise"]["alive"],
          f"surprise={state['shift_3_surprise']['surprise_level']}")
    check("C2 shift 4 — resonance (typed edges, not all 'supports')",
          state["shift_4_resonance"]["alive"],
          f"{state['shift_4_resonance']['supports_share']:.0%} supports, "
          f"{state['shift_4_resonance']['relation_kinds']} kinds")
    check("C2 shift 5 — identity from the graph",
          state["shift_5_identity"]["alive"],
          f"{state['shift_5_identity']['identity_rows']} rows")
    return state


# ─────────────────────────── C3: doctor honesty ──────────────────────────────

def c3_doctor(mnemos_bin: Path, home: Path) -> None:
    db = str(home / ".mnemos" / "readiness.db")  # the C1 store, now populated
    out = subprocess.run(
        [str(mnemos_bin), "doctor", "--db-path", db, "--agent-id", "readiness",
         "--person-id", "tester", "--project-scope", "check"],
        capture_output=True, text=True,
        env={"HOME": str(home), "PATH": f"{mnemos_bin.parent}:/usr/bin:/bin",
             "MNEMOS_DISABLE_DOTENV": "1"},
    ).stdout
    check("C3 doctor: reports a real store honestly",
          "DB exists:    yes" in out and "Continuity:" in out)


def main() -> int:
    print("Mnemos readiness check — against the built wheel and a keyless store")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        home = workdir / "home"
        (home / ".mnemos").mkdir(parents=True)

        print("\nC1 — fresh keyless install of the built wheel")
        mnemos_bin = build_and_install_wheel(workdir)
        c1_stdio_and_restart(mnemos_bin, home)

        print("\nC2 — the five shifts on a keyless store")
        state = c2_mind_state(workdir)

        print("\nC3 — doctor")
        c3_doctor(mnemos_bin, home)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 70)
    print(f"Readiness: {passed}/{total} checks passed")
    print("\nMind-state baseline (for the readiness report):")
    print(json.dumps(state, indent=2))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
