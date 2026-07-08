# Retrieval quality benchmarks

Spreading-activation retrieval ships with five tunable constants (activation
depth 3, decay 0.5, threshold 0.1, confidence floor 0.3, reconsolidation
deltas) that previously had zero empirical grounding. This harness grounds
them with reproducible numbers.

## Method

`retrieval_benchmark.py` seeds **3 synthetic lives** (archivist, gardener,
engineer — 224 engrams total) with disjoint topic vocabularies, giving every
probe query a known ground-truth relevance set (an engram is relevant iff it
belongs to the query's topic). Within-topic support chains and periodic
cross-topic association edges give spreading activation real paths that can
both help and hurt. Everything is seeded (`random.Random(42)`); no LLM, no
network.

- **Grid phase** measures precision/recall@k over depth × decay × threshold
  with reconsolidation *disabled*, so combos are deterministic and don't
  contaminate each other. R@k here is capped recall (denominator
  `min(|relevant|, k)`), which is why R@5 equals P@5 when every topic has
  more than 5 relevant engrams.
- **Drift phase** answers the rich-get-richer question: with default
  parameters and reconsolidation *enabled*, 100 sessions of skewed retrieval
  (~80% of queries hit 2 "hot" topics per life) run against the store, and
  quality across *all* topics is re-measured every 10 sessions with
  reconsolidation off. "Intrusion" = fraction of top-5 results for a
  cold-topic query that belong to a hot topic.

Reproduce:

```bash
python benchmarks/retrieval_benchmark.py            # both phases (~1 min)
python benchmarks/retrieval_benchmark.py --grid     # grid only
python benchmarks/retrieval_benchmark.py --drift    # drift only
python benchmarks/retrieval_benchmark.py --grid \
  --record-db ./copy.db --record-json               # also persist Step 1 rows
```

`--record-db` opens the given SQLite store, registers the Step 1 drift-eval
instrument manifest, and records the shipped default retrieval metrics as
`drift_eval_runs` / `drift_eval_observations`. It refuses live `~/.mnemos`
databases unless `--allow-live-db` is passed for an explicitly authorized live
rollout. `--record-json` prints the recorded benchmark run row.

## Results (seed 42, 2026-06-10, post co_activated fix)

### Parameter grid

| depth | decay | threshold | P@5 | R@5 | P@10 | R@10 |
|---|---|---|---|---|---|---|
| 2 | 0.3 | 0.05 | 0.952 | 0.952 | 0.869 | 0.869 |
| 2 | 0.3 | 0.1 | 0.955 | 0.955 | 0.889 | 0.889 |
| 2 | 0.3 | 0.2 | 0.940 | 0.940 | 0.724 | 0.724 |
| 2 | 0.5 | 0.05 | 0.950 | 0.950 | 0.863 | 0.863 |
| 2 | 0.5 | 0.1 | 0.952 | 0.952 | 0.864 | 0.864 |
| 2 | 0.5 | 0.2 | 0.950 | 0.950 | 0.875 | 0.875 |
| 2 | 0.7 | 0.05 | 0.910 | 0.910 | 0.815 | 0.815 |
| 2 | 0.7 | 0.1 | 0.914 | 0.914 | 0.835 | 0.835 |
| 2 | 0.7 | 0.2 | 0.924 | 0.924 | 0.849 | 0.849 |
| 3 | 0.3 | 0.05 | 0.952 | 0.952 | 0.870 | 0.870 |
| 3 | 0.3 | 0.1 | 0.955 | 0.955 | 0.889 | 0.889 |
| 3 | 0.3 | 0.2 | 0.940 | 0.940 | 0.724 | 0.724 |
| **3** | **0.5** | **0.1** | **0.945** | **0.945** | **0.862** | **0.862** |
| 3 | 0.5 | 0.05 | 0.943 | 0.943 | 0.856 | 0.856 |
| 3 | 0.5 | 0.2 | 0.952 | 0.952 | 0.881 | 0.881 |
| 3 | 0.7 | 0.05 | 0.840 | 0.840 | 0.727 | 0.727 |
| 3 | 0.7 | 0.1 | 0.860 | 0.860 | 0.752 | 0.752 |
| 3 | 0.7 | 0.2 | 0.883 | 0.883 | 0.786 | 0.786 |
| 4 | 0.3 | 0.05 | 0.952 | 0.952 | 0.870 | 0.870 |
| 4 | 0.3 | 0.1 | 0.955 | 0.955 | 0.889 | 0.889 |
| 4 | 0.3 | 0.2 | 0.940 | 0.940 | 0.724 | 0.724 |
| 4 | 0.5 | 0.05 | 0.921 | 0.921 | 0.852 | 0.852 |
| 4 | 0.5 | 0.1 | 0.943 | 0.943 | 0.862 | 0.862 |
| 4 | 0.5 | 0.2 | 0.952 | 0.952 | 0.881 | 0.881 |
| 4 | 0.7 | 0.05 | 0.755 | 0.755 | 0.624 | 0.624 |
| 4 | 0.7 | 0.1 | 0.781 | 0.781 | 0.665 | 0.665 |
| 4 | 0.7 | 0.2 | 0.855 | 0.855 | 0.718 | 0.718 |

**Readings:**

- **The shipped defaults (3, 0.5, 0.1) are sound**: P@5 0.945 / R@10 0.862,
  within ~1 point of the best combo. They were guessed, but they were
  guessed well.
- **The best combo on this graph is (2, 0.3, 0.1)**: P@5 0.955 / R@10 0.889.
  Shallower, more conservative propagation wins slightly — most relevance
  lives within 2 hops; the third hop mostly imports neighbors' neighbors.
- **Aggressive propagation is the failure mode, not timid propagation**:
  depth 4 + decay 0.7 collapses to P@5 0.755 (activation bleeding across
  topic boundaries). High threshold (0.2) with low decay (0.3) instead
  starves recall (R@10 0.724) by pruning genuine mid-distance relevance.
- Depth beyond 3 buys nothing anywhere in the grid.

### Reconsolidation drift (the rich-get-richer answer)

| session | P@5 all | P@5 hot | P@5 cold | hot→cold intrusion |
|---|---|---|---|---|
| 0 | 0.945 | 0.992 | 0.927 | 0.021 |
| 10 | 0.938 | 0.967 | 0.927 | 0.021 |
| 20 | 0.943 | 0.983 | 0.927 | 0.021 |
| 30 | 0.943 | 0.983 | 0.927 | 0.021 |
| 40 | 0.945 | 0.992 | 0.927 | 0.021 |
| 50 | 0.943 | 0.983 | 0.927 | 0.021 |
| 60 | 0.936 | 0.983 | 0.917 | 0.032 |
| 70 | 0.936 | 0.983 | 0.917 | 0.032 |
| 80 | 0.938 | 0.992 | 0.917 | 0.032 |
| 90 | 0.936 | 0.983 | 0.917 | 0.032 |
| 100 | 0.936 | 0.983 | 0.917 | 0.032 |

**The empirical answer: rich-get-richer is real but mild and bounded.**
After 100 heavily-skewed sessions, cold-topic precision declined one point
(0.927 → 0.917) and hot-engram intrusion into cold queries rose from 2.1% to
3.2% — measurable crowding, no collapse, and the drift plateaus by session
~60 rather than compounding. Two mechanisms bound it: the accessibility
floor benefits retrieved memories without suppressing quiet ones (decay is
what suppresses, and it's topic-blind), and co-retrieval edges are
`co_activated` (activation weight 0.6) rather than `supports` (1.0), so
frequently-retrieved clusters don't become activation superhighways. Worth
re-running at 1,000 sessions before declaring the question closed.

## Honest limits

- Topic vocabularies are disjoint, so FTS seeding is cleaner than real life;
  the benchmark primarily measures graph-propagation behavior, not lexical
  ambiguity. Real-life overlap would lower all absolute numbers.
- 224 engrams is a young graph. Constants that win here may not win at
  20,000 engrams; re-run the grid as real corpora become available.
- Ground truth is topic membership, which under-credits genuinely useful
  cross-topic associations.
