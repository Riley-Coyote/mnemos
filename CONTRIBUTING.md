# Contributing to Mnemos

## Development Setup

```bash
git clone https://github.com/Riley-Coyote/mnemos.git
cd mnemos
python -m venv .venv
source .venv/bin/activate
pip install -e ".[all,dev]"
```

## Running Tests

```bash
pytest tests/
```

The shared test harness clears ambient Mnemos/provider environment variables,
disables dotenv reads, refuses the live `~/.mnemos/memory.db`, and redirects
vault alert output to per-test temp directories unless a test overrides it.

## Code Style

- Python 3.10+
- Type hints on all public functions
- Docstrings on all public classes and methods
- `logging.getLogger(__name__)` in every module

## Submitting Changes

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run `pytest` to verify
5. Submit a pull request

## Architecture

Mnemos is organized into these layers:

- `core/` — Data structures (Engram, Belief, EmotionalState, Identity)
- `store/` — SQLite persistence + embedding index
- `encoding/` — Memory formation pipeline
- `retrieval/` — Spreading activation retrieval
- `consolidation/` — Offline processing (decay, softening, belief review, reflection)
- `substrate/` — Cognitive tick loop + handlers (dreaming, wandering, etc.)
- `indexer/` — Session transcript → memory extraction
- `interface/` — Prompt building, export, session tracking
- `multiagent/` — Shared pools, relationships, federation stubs
- `advanced/` — Experimental cognitive modules (opt-in)

## License

MIT
