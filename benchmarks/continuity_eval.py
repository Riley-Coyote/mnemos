"""Does the session-start packet carry the right thing — and keep doing it?

The continuity assay proves a capture survives a restart. That is the
floor, not the bar. The real question is whether the *relevant* note is
in the packet when a session opens, and whether that stays true as the
store fills up.

Living with the system for a week cannot answer this. A week adds maybe
a dozen notes, and at small N everything fits in the packet regardless of
how good selection is. The failure arrives months later, quietly: the
packet keeps returning eight notes, they are simply the wrong eight.

So this evaluates at the sizes a real store reaches — 10, 50, 200, 500
notes — and reports recall@k for cues that a session might actually open
with.

    python benchmarks/continuity_eval.py
    python benchmarks/continuity_eval.py --sizes 10,100 --json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mnemos.interface.context_packet import build_context_packet  # noqa: E402
from mnemos.simple_runtime import MnemosRuntime  # noqa: E402
from mnemos.store.sqlite_store import EngramStore  # noqa: E402


# (note the agent captures, cue a later session might open with)
# Cues deliberately avoid reusing the note's distinctive words, because an
# agent orienting itself does not phrase things the way the note was
# written. Keyword overlap that only works verbatim is not recall.
PROBES: list[tuple[str, str]] = [
    ("Riley prefers cold brew, never hot coffee, and drinks it black",
     "what should I know about his drinks"),
    ("Deploys always go to staging first, never straight to production",
     "how do releases reach users"),
    ("Riley works best between 2 and 4am and dislikes morning meetings",
     "when is he most productive"),
    ("The design language is monochrome; colour only comes from the pixel world",
     "what are the visual rules for this project"),
    ("Never force-push to main; every change lands through a pull request",
     "what are the rules about git history"),
    ("Riley is a systems engineer, not a traditional coder, and works through agents",
     "who am I working with"),
    ("Tests must fail on the previous code before a fix is considered done",
     "what counts as a finished bug fix"),
    ("The Sanctuary is one wing of a larger platform, not the whole product",
     "how does the sanctuary relate to everything else"),
    ("Riley dislikes warm or brown tones in any interface",
     "which colours should I avoid"),
    ("Continuity matters more than the memory graph; the graph is not the product",
     "what does he care about most"),
]

# Filler resembles real continuity: plausible, same domain, not the answer.
FILLER = [
    "The build pipeline caches dependencies between runs to save time",
    "Session transcripts are stored as JSONL under the project directory",
    "The changelog records breaking changes with their migration inline",
    "Icons are drawn at 16px and scaled, never the other way around",
    "Long documents get a table of contents when they exceed five sections",
    "Background jobs write their output to a log file under the home directory",
    "Configuration lives in a single JSON file that the CLI can rewrite",
    "The archive holds material that predates the current platform",
]


def _populate(runtime: MnemosRuntime, target: int, *, differentiate: bool = True) -> None:
    """Fill a store to `target` notes, with the probes mixed throughout.

    Probes are spread across the whole store rather than appended, so
    recency alone cannot carry them into the packet.

    ``differentiate`` controls whether durable notes are captured as more
    important than routine ones. Real use differentiates — that is what
    the importance argument on capture is for. Setting it False models a
    store where everything claims to matter equally, which is a genuinely
    different (and much harder) retrieval problem, and worth measuring
    separately rather than by accident.
    """
    probe_contents = [note for note, _cue in PROBES]
    filler_needed = max(0, target - len(probe_contents))
    stream: list[tuple[str, str]] = []
    every = max(1, filler_needed // max(1, len(probe_contents)))
    probes = list(probe_contents)
    for i in range(filler_needed):
        stream.append((f"{FILLER[i % len(FILLER)]} (note {i})",
                       "low" if differentiate else "high"))
        if probes and i % every == 0:
            stream.append((probes.pop(0), "high"))
    stream.extend((p, "high") for p in probes)

    for content, importance in stream[:target]:
        runtime.capture(content, importance=importance)


def evaluate(size: int, max_hypomnema: int = 8, *, differentiate: bool = True) -> dict:
    """Two numbers, because the packet and recall answer different questions.

    ``packet_recall`` — with no cue, as a session actually opens, does the
    packet carry the durable notes? This is the product.

    ``cue_recall`` — given a specific question, does the matching note
    surface? This is what mnemos_recall is for, and it is the harder
    problem because an agent's phrasing rarely reuses the note's words.
    """
    tmp = Path(tempfile.mkdtemp())
    db = str(tmp / "eval.db")
    runtime = MnemosRuntime(db_path=db, agent_id="eval", use_dedicated_model=False)
    try:
        _populate(runtime, size, differentiate=differentiate)
        scope = runtime.scope
    finally:
        runtime.close()

    def _carries(entries, note: str) -> bool:
        key = " ".join(note.split()[:6])[:40]
        return any(key in " ".join(e["content"].split()) for e in entries)

    store = EngramStore(db)
    packet_hits = 0
    try:
        # The packet as a session actually receives it: no real cue.
        opening = build_context_packet(
            store, "",
            agent_id=scope.agent_id, person_id=scope.person_id,
            project_scope=scope.project_scope,
            include_engrams=False, max_hypomnema=max_hypomnema,
            token_budget=100_000,
        )["hypomnema"]
        packet_hits = sum(1 for note, _ in PROBES if _carries(opening, note))
    finally:
        pass

    hits = 0
    misses: list[str] = []
    try:
        for note, cue in PROBES:
            packet = build_context_packet(
                store,
                cue,
                agent_id=scope.agent_id,
                person_id=scope.person_id,
                project_scope=scope.project_scope,
                include_engrams=False,
                max_hypomnema=max_hypomnema,
                token_budget=100_000,  # isolate selection from truncation
            )
            # Match on a distinctive fragment; the packet clips long notes.
            if _carries(packet["hypomnema"], note):
                hits += 1
            else:
                misses.append(cue)
    finally:
        store.close()
        shutil.rmtree(tmp, ignore_errors=True)

    n = len(PROBES)
    return {
        "store_size": size,
        "probes": n,
        "packet_hits": packet_hits,
        "packet_recall": round(packet_hits / n, 3),
        "cue_hits": hits,
        "cue_recall": round(hits / n, 3),
        "misses": misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="10,50,200,500")
    parser.add_argument("--k", type=int, default=8, help="notes the packet may carry")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--mind", metavar="DB", help="Measure the five shifts against a real store")
    parser.add_argument(
        "--flat", action="store_true",
        help="Model a store where every note claims equal importance",
    )
    args = parser.parse_args()
    args.differentiate = not args.flat

    if args.mind:
        return print_mind_state(args.mind)

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    results = [
        evaluate(size, max_hypomnema=args.k, differentiate=args.differentiate)
        for size in sizes
    ]

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    label = "importance differentiated" if args.differentiate else "everything equally important"
    print(f"Continuity evaluation @k={args.k}  ({label})")
    print("=" * 66)
    ceiling = min(args.k, len(PROBES)) / len(PROBES)
    print(f"{'store size':>12}  {'packet':>10}  {'cue recall':>12}")
    print("-" * 66)
    for r in results:
        at_ceiling = " (ceiling)" if r["packet_recall"] >= ceiling else ""
        print(
            f"{r['store_size']:>12}  {r['packet_recall']:>9.0%}  {r['cue_recall']:>11.0%}"
            f"   ({r['packet_hits']}/{r['probes']}, {r['cue_hits']}/{r['probes']}){at_ceiling}"
        )
    print()
    print(f"The packet holds {args.k} notes and there are {len(PROBES)} probes, so "
          f"{ceiling:.0%} is a perfect score.")
    print()
    print("packet     — what a session actually receives, with no cue. This is")
    print("             the product: does it carry the durable things?")
    print("cue recall — does a specific question find its note? This is what")
    print("             mnemos_recall does, and it is the harder problem: an")
    print("             agent's phrasing rarely reuses the note's own words.")
    worst = min(results, key=lambda r: r["cue_recall"])
    if worst["misses"]:
        print()
        print(f"Cues that found nothing at size {worst['store_size']}:")
        for cue in worst["misses"][:5]:
            print(f'  - "{cue}"')
    return 0



# ── Mind state ────────────────────────────────────────────────────────────
#
# The five shifts each have a code path. The question that matters is whether
# they leave a trace in a real store. Four of five were dormant when this was
# first measured: 0 identity rows, 0 DISTILLED_INTO edges, surprise disabled
# at every call site, and 96% of connections a single relation type.
#
#     python benchmarks/continuity_eval.py --mind ~/.mnemos/agent.db

def mind_state(db_path: str) -> dict:
    """Measure the five shifts against a real store."""
    import sqlite3

    conn = sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731

    engrams = q("SELECT COUNT(*) FROM engrams") or 0
    connections = q("SELECT COUNT(*) FROM connections") or 0
    # Shift 1 asks for a trace of how understanding changed. A phrase the
    # server chose from a fixed list is not that, however well it fills the
    # column — counting it would make the metric flatter the system exactly
    # where the system is weakest. Only impacts nothing templated could have
    # written are counted.
    boilerplate = (
        "Foundational continuity for future interactions.",
        "Recurring pattern worth carrying across sessions.",
        "Long-arc context that should shape future work.",
        "Current working context for continuity.",
        "Preference to respect in future decisions.",
        "Durable continuity captured from the session.",
        "Stable scoped continuity promoted from hypomnema.",
        "Stable continuity promoted during simple maintenance.",
        "Correction to earlier continuity.",
        "Corrected continuity for future interactions.",
    )
    placeholders = ",".join("?" * len(boilerplate))
    with_impact = conn.execute(
        f"SELECT COUNT(*) FROM engrams WHERE impact != '' AND impact IS NOT NULL "
        f"AND impact NOT IN ({placeholders})",
        boilerplate,
    ).fetchone()[0]
    distilled = q("SELECT COUNT(*) FROM connections WHERE relation = 'distilled_into'")
    supports = q("SELECT COUNT(*) FROM connections WHERE relation = 'supports'")
    try:
        identities = q("SELECT COUNT(*) FROM agent_identity")
    except sqlite3.Error:
        identities = 0
    relation_kinds = q("SELECT COUNT(DISTINCT relation) FROM connections")
    conn.close()

    return {
        "engrams": engrams,
        "connections": connections,
        "shift_1_traces": {
            "impact_coverage": round(with_impact / engrams, 3) if engrams else 0.0,
            "alive": engrams > 0 and with_impact / engrams >= 0.5,
        },
        "shift_2_lessons": {"distilled_into": distilled, "alive": distilled > 0},
        "shift_4_resonance": {
            "relation_kinds": relation_kinds,
            "supports_share": round(supports / connections, 3) if connections else 0.0,
            "alive": connections > 0 and supports / connections < 0.8,
        },
        "shift_5_identity": {"identity_rows": identities, "alive": identities > 0},
    }


def print_mind_state(db_path: str) -> int:
    state = mind_state(db_path)
    print(f"Mind state — {db_path}")
    print("=" * 66)
    print(f"  {state['engrams']} engrams, {state['connections']} connections\n")
    rows = [
        ("1. traces, not records", state["shift_1_traces"],
         f"{state['shift_1_traces']['impact_coverage']:.0%} carry a non-templated impact"),
        ("2. forgetting that teaches", state["shift_2_lessons"],
         f"{state['shift_2_lessons']['distilled_into']} distilled-into edges"),
        ("4. resonance, not search", state["shift_4_resonance"],
         f"{state['shift_4_resonance']['relation_kinds']} relation kinds, "
         f"{state['shift_4_resonance']['supports_share']:.0%} 'supports'"),
        ("5. identity from the graph", state["shift_5_identity"],
         f"{state['shift_5_identity']['identity_rows']} identity rows"),
    ]
    for label, data, detail in rows:
        mark = "alive " if data["alive"] else "DORMANT"
        print(f"  [{mark}] {label:<28} {detail}")
    print()
    print("  Shift 3 (surprise) is observable only at encode time; see the")
    print("  surprise value returned by a genuinely novel capture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
