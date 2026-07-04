"""Retrieval quality benchmark for Mnemos spreading-activation retrieval.

Spreading activation has tunable constants (depth, decay, threshold) that
shipped with zero empirical grounding. This harness grounds them:

1. GRID — seed 3 synthetic "lives" (>= 200 engrams total) with known
   ground-truth relevance sets, then measure precision/recall@k across a
   parameter grid. Measurement runs with reconsolidation DISABLED so the
   grid is deterministic and combos don't contaminate each other.

2. DRIFT — with default parameters and reconsolidation ENABLED, simulate
   100 sessions of skewed retrieval (some topics queried far more often),
   measuring retrieval quality every 10 sessions. This answers the
   rich-get-richer question empirically: do heavily-retrieved engrams
   crowd out relevant-but-quiet ones?

Run:
    .venv/bin/python benchmarks/retrieval_benchmark.py            # both phases
    .venv/bin/python benchmarks/retrieval_benchmark.py --grid     # grid only
    .venv/bin/python benchmarks/retrieval_benchmark.py --drift    # drift only

Everything is seeded (random.Random(42)); no LLM, no network. Output is a
markdown report on stdout — the numbers in benchmarks/README.md are a
checked-in run of exactly this script.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mnemos.core.types import ConnectionRelation  # noqa: E402
from mnemos.encoding.encoder import Encoder  # noqa: E402
from mnemos.retrieval.reactive import ReactiveRetriever  # noqa: E402
from mnemos.store.sqlite_store import EngramStore  # noqa: E402

SEED = 42

# ── Synthetic lives ──────────────────────────────────────────────────
# Three agents with disjoint topic vocabularies. Ground truth: an engram
# is relevant to a query iff it belongs to the query's topic.

LIVES: dict[str, dict[str, list[str]]] = {
    "archivist": {
        "letterpress": [
            "letterpress",
            "kerning",
            "typeface",
            "ligature",
            "galley",
            "platen",
        ],
        "manuscripts": [
            "manuscript",
            "vellum",
            "marginalia",
            "codex",
            "folio",
            "scribe",
        ],
        "bindings": [
            "binding",
            "spine",
            "endpaper",
            "headband",
            "buckram",
            "sewing-frame",
        ],
        "catalogs": [
            "catalog",
            "accession",
            "shelfmark",
            "finding-aid",
            "provenance",
            "deaccession",
        ],
        "inks": ["ink", "pigment", "iron-gall", "lampblack", "sizing", "mordant"],
        "restoration": [
            "restoration",
            "deacidification",
            "tear-repair",
            "humidification",
            "encapsulation",
            "foxing",
        ],
        "exhibits": [
            "exhibit",
            "vitrine",
            "lux-level",
            "mount",
            "caption",
            "loan-agreement",
        ],
    },
    "gardener": {
        "soil": ["loam", "compost", "mycorrhizae", "tilth", "humus", "topdressing"],
        "pruning": [
            "pruning",
            "pollarding",
            "espalier",
            "deadheading",
            "coppicing",
            "thinning-cut",
        ],
        "propagation": [
            "cutting",
            "scion",
            "rootstock",
            "stratification",
            "layering",
            "division",
        ],
        "pests": ["aphid", "whitefly", "ladybird", "neem", "thrips", "leafminer"],
        "perennials": [
            "perennial",
            "rhizome",
            "crown",
            "hosta",
            "salvia",
            "overwintering",
        ],
        "water": [
            "irrigation",
            "drip-line",
            "swale",
            "mulch-basin",
            "greywater",
            "wicking-bed",
        ],
        "greenhouse": [
            "greenhouse",
            "cold-frame",
            "ventilation-louvre",
            "shade-cloth",
            "heat-mat",
            "glazing",
        ],
    },
    "engineer": {
        "retrieval": [
            "retrieval",
            "activation",
            "spreading",
            "recall-cue",
            "salience",
            "priming",
        ],
        "storage": [
            "sqlite",
            "schema",
            "migration",
            "index-scan",
            "vacuum",
            "write-ahead",
        ],
        "consolidation": [
            "consolidation",
            "decay-curve",
            "softening",
            "dormancy",
            "archival",
            "rehearsal",
        ],
        "agents": [
            "agent-scope",
            "identity-kernel",
            "epoch",
            "substrate",
            "affinity",
            "daemon",
        ],
        "graphs": [
            "graph",
            "edge-weight",
            "hub-node",
            "traversal",
            "clustering",
            "adjacency",
        ],
        "testing": [
            "pytest",
            "fixture",
            "regression",
            "invariant",
            "coverage",
            "flake",
        ],
        "deploy": [
            "deploy",
            "rollback",
            "canary",
            "healthcheck",
            "cron-job",
            "observability",
        ],
    },
}

FILLER = [
    "spent the afternoon with",
    "kept returning to",
    "finally understood",
    "made notes about",
    "argued with myself over",
    "quietly fixed",
    "sketched a plan for",
    "compared two approaches to",
    "watched",
    "rebuilt",
]

ENGRAMS_PER_TOPIC = {"archivist": 10, "gardener": 10, "engineer": 12}
QUERIES_PER_TOPIC = 4
K_VALUES = (5, 10)

GRID_DEPTH = (2, 3, 4)
GRID_DECAY = (0.3, 0.5, 0.7)
GRID_THRESHOLD = (0.05, 0.1, 0.2)

DRIFT_SESSIONS = 100
DRIFT_MEASURE_EVERY = 10
HOT_TOPICS_PER_LIFE = 2  # first N topics absorb most session queries


def build_life(
    db_path: Path, agent: str, topics: dict[str, list[str]], rng: random.Random
):
    """Seed one synthetic life. Returns (store, ground_truth, queries)."""
    store = EngramStore(db_path)
    encoder = Encoder(store, llm_client=None)

    ground_truth: dict[str, set[str]] = {}
    topic_engrams: dict[str, list] = {}

    for topic, vocab in topics.items():
        ids: set[str] = set()
        engrams = []
        n = ENGRAMS_PER_TOPIC[agent]
        for i in range(n):
            words = rng.sample(vocab, 3)
            filler = rng.choice(FILLER)
            content = (
                f"{filler} the {words[0]} work — {words[1]} and {words[2]} "
                f"session {i} taught me something about {topic}"
            )
            e = encoder.encode(
                content=content,
                kind="semantic",
                tags=[topic],
                agent_id=agent,
                session_id=f"{agent}-{topic}-{i}",
                skip_surprise_detection=True,
                source_authority="observed",  # R1: synthetic benchmark writer (T3)
            )
            ids.add(e.id)
            engrams.append(e)
        ground_truth[topic] = ids
        topic_engrams[topic] = engrams

    # Cross-topic association edges give spreading activation real paths
    # that can both help (within-topic hops) and hurt (cross-topic bleed).
    topic_list = list(topics)
    for ti, topic in enumerate(topic_list):
        engrams = topic_engrams[topic]
        for i, e in enumerate(engrams):
            # within-topic chain
            nxt = engrams[(i + 1) % len(engrams)]
            e.add_connection(
                nxt.id, ConnectionRelation.SUPPORTS, strength=0.6, formed_by="seed"
            )
            # occasional cross-topic association
            if i % 4 == 0:
                other = topic_engrams[topic_list[(ti + 1) % len(topic_list)]]
                e.add_connection(
                    rng.choice(other).id,
                    ConnectionRelation.ANALOGOUS_TO,
                    strength=0.5,
                    formed_by="seed",
                )
            store.save_engram(e)

    queries: list[tuple[str, str]] = []  # (topic, query)
    for topic, vocab in topics.items():
        for _ in range(QUERIES_PER_TOPIC):
            queries.append((topic, " ".join(rng.sample(vocab, 2))))

    return store, ground_truth, queries


def precision_recall_at_k(retrieved_ids: list[str], relevant: set[str], k: int):
    top = retrieved_ids[:k]
    hits = sum(1 for rid in top if rid in relevant)
    precision = hits / k
    recall = hits / min(len(relevant), k) if relevant else 0.0
    return precision, recall


def evaluate(store, agent, ground_truth, queries, *, depth, decay, threshold):
    """Measure P/R@k with reconsolidation OFF (no mutation during measurement)."""
    retriever = ReactiveRetriever(
        store,
        activation_depth=depth,
        activation_decay=decay,
        activation_threshold=threshold,
        reconsolidation_enabled=False,
    )
    metrics = {f"p@{k}": [] for k in K_VALUES}
    metrics.update({f"r@{k}": [] for k in K_VALUES})
    for topic, query in queries:
        results = retriever.retrieve(query, agent_id=agent, max_results=max(K_VALUES))
        ids = [r.engram.id for r in results]
        for k in K_VALUES:
            p, r = precision_recall_at_k(ids, ground_truth[topic], k)
            metrics[f"p@{k}"].append(p)
            metrics[f"r@{k}"].append(r)
    return {m: statistics.mean(v) for m, v in metrics.items()}


def run_grid() -> list[str]:
    lines = [
        "## Parameter grid (reconsolidation off, deterministic)",
        "",
        f"3 lives, {sum(len(t) * ENGRAMS_PER_TOPIC[a] for a, t in LIVES.items())} engrams, "
        f"{sum(len(t) * QUERIES_PER_TOPIC for t in LIVES.values())} probe queries, seed {SEED}.",
        "",
        "| depth | decay | threshold | P@5 | R@5 | P@10 | R@10 |",
        "|---|---|---|---|---|---|---|",
    ]
    rng = random.Random(SEED)
    with tempfile.TemporaryDirectory() as tmp:
        stores = {}
        for agent, topics in LIVES.items():
            store, gt, queries = build_life(
                Path(tmp) / f"{agent}.db", agent, topics, rng
            )
            stores[agent] = (store, gt, queries)

        rows = []
        for depth in GRID_DEPTH:
            for decay in GRID_DECAY:
                for threshold in GRID_THRESHOLD:
                    agg = {f"{m}@{k}": [] for m in ("p", "r") for k in K_VALUES}
                    for agent, (store, gt, queries) in stores.items():
                        res = evaluate(
                            store,
                            agent,
                            gt,
                            queries,
                            depth=depth,
                            decay=decay,
                            threshold=threshold,
                        )
                        for key, val in res.items():
                            agg[key].append(val)
                    row = {k: statistics.mean(v) for k, v in agg.items()}
                    rows.append(((depth, decay, threshold), row))
                    lines.append(
                        f"| {depth} | {decay} | {threshold} "
                        f"| {row['p@5']:.3f} | {row['r@5']:.3f} "
                        f"| {row['p@10']:.3f} | {row['r@10']:.3f} |"
                    )

        for agent in stores:
            stores[agent][0].close()

    best = max(rows, key=lambda item: item[1]["p@5"] + item[1]["r@10"])
    defaults = next(r for r in rows if r[0] == (3, 0.5, 0.1))
    lines += [
        "",
        f"Best combo by P@5+R@10: depth={best[0][0]} decay={best[0][1]} "
        f"threshold={best[0][2]} (P@5 {best[1]['p@5']:.3f}, R@10 {best[1]['r@10']:.3f}).",
        f"Shipped defaults (3, 0.5, 0.1): P@5 {defaults[1]['p@5']:.3f}, "
        f"R@10 {defaults[1]['r@10']:.3f}.",
    ]
    return lines


def run_drift() -> list[str]:
    lines = [
        "## Reconsolidation drift over 100 sessions (rich-get-richer probe)",
        "",
        f"Default parameters, reconsolidation ON during sessions. Per life, the first "
        f"{HOT_TOPICS_PER_LIFE} topics receive ~80% of session queries ('hot'); "
        "quality is measured every "
        f"{DRIFT_MEASURE_EVERY} sessions across ALL topics with reconsolidation off.",
        "",
        "| session | P@5 all | P@5 hot | P@5 cold | hot→cold intrusion |",
        "|---|---|---|---|---|",
    ]
    rng = random.Random(SEED)
    with tempfile.TemporaryDirectory() as tmp:
        lives = {}
        for agent, topics in LIVES.items():
            store, gt, queries = build_life(
                Path(tmp) / f"{agent}.db", agent, topics, rng
            )
            hot = set(list(topics)[:HOT_TOPICS_PER_LIFE])
            lives[agent] = {
                "store": store,
                "gt": gt,
                "queries": queries,
                "hot": hot,
                "topics": topics,
            }

        def measure(session_label):
            all_p, hot_p, cold_p, intrusion = [], [], [], []
            for agent, life in lives.items():
                retriever = ReactiveRetriever(
                    life["store"], reconsolidation_enabled=False
                )
                hot_ids = set().union(*(life["gt"][t] for t in life["hot"]))
                for topic, query in life["queries"]:
                    results = retriever.retrieve(query, agent_id=agent, max_results=10)
                    ids = [r.engram.id for r in results]
                    p, _ = precision_recall_at_k(ids, life["gt"][topic], 5)
                    all_p.append(p)
                    if topic in life["hot"]:
                        hot_p.append(p)
                    else:
                        cold_p.append(p)
                        top5 = ids[:5]
                        if top5:
                            intrusion.append(
                                sum(1 for i in top5 if i in hot_ids) / len(top5)
                            )
            return (
                f"| {session_label} | {statistics.mean(all_p):.3f} "
                f"| {statistics.mean(hot_p):.3f} | {statistics.mean(cold_p):.3f} "
                f"| {statistics.mean(intrusion):.3f} |"
            )

        lines.append(measure(0))

        for session in range(1, DRIFT_SESSIONS + 1):
            for agent, life in lives.items():
                retriever = ReactiveRetriever(
                    life["store"], reconsolidation_enabled=True
                )
                # ~80% of queries hit the hot topics
                topic_pool = list(life["topics"])
                if rng.random() < 0.8:
                    topic = rng.choice(sorted(life["hot"]))
                else:
                    topic = rng.choice(topic_pool)
                vocab = life["topics"][topic]
                query = " ".join(rng.sample(vocab, 2))
                retriever.retrieve(query, agent_id=agent, max_results=5)
            if session % DRIFT_MEASURE_EVERY == 0:
                lines.append(measure(session))

        for life in lives.values():
            life["store"].close()

    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Mnemos retrieval benchmark")
    parser.add_argument(
        "--grid", action="store_true", help="run the parameter grid only"
    )
    parser.add_argument(
        "--drift", action="store_true", help="run the drift simulation only"
    )
    args = parser.parse_args()
    run_both = not (args.grid or args.drift)

    print("# Mnemos retrieval benchmark\n")
    if args.grid or run_both:
        print("\n".join(run_grid()))
        print()
    if args.drift or run_both:
        print("\n".join(run_drift()))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
