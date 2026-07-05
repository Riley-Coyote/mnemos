"""Twin-consistency lock: scripts/mnemos-decide (stdlib TCB) must compute
byte-identical content hashes and identical chain verdicts to
mnemos/vault/journal.py.

The two are deliberately separate copies — the TCB stays self-contained stdlib
so David can audit the whole file — so this test is the only thing keeping them
from drifting. Vectors required by Fable review 008 §4: unicode content, empty
payload, and a 3-line chain with a tampered middle line (both must reject).
"""

from __future__ import annotations

import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader

from mnemos.vault import journal as pkg


def _load_tcb():
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "mnemos-decide"
    loader = SourceFileLoader("mnemos_decide_tcb", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(module)
    return module


TCB = _load_tcb()


_VECTORS = [
    {  # unicode content — exercises ensure_ascii=False
        "id": "p-unicode",
        "agent_id": "oliver",
        "person_id": "david",
        "project_scope": "pai",
        "source_authority": "user_stated",
        "kind": "semantic",
        "domain": "identity",
        "blast_radius": "identity",
        "target_surface": "hypomnema_entries",
        "target_id": None,
        "transition": "Sono Oliver — l'agente di David. ἀρετή. 線香花火.",
        "payload": {"content": "David è अपना. φιλία, non servizio."},
        "provenance_ids": ["soul-md-1", "città"],
    },
    {  # empty payload
        "id": "p-empty",
        "agent_id": "oliver",
        "person_id": "david",
        "project_scope": "pai",
        "source_authority": "observed",
        "kind": "episodic",
        "domain": "foundational",
        "blast_radius": "foundational",
        "target_surface": "beliefs",
        "target_id": "b-1",
        "transition": "",
        "payload": {},
        "provenance_ids": [],
    },
]


def test_content_hash_twin_matches_on_vectors():
    for vec in _VECTORS:
        assert TCB.canonical_content_sha256(vec) == pkg.canonical_content_sha256(vec)


def test_row_hash_twin_matches():
    row = {
        "id": "b-legacy",
        "content": "Sono Oliver. David è अपना.",
        "domain": "identity",
        "tier": "foundational",
    }
    assert TCB.canonical_row_sha256("beliefs", row) == pkg.canonical_row_sha256(
        "beliefs", row
    )
    hrow = {"id": "h1", "content": "x", "domain": "topical", "foundational": 1}
    assert TCB.canonical_row_sha256("hypomnema_entries", hrow) == (
        pkg.canonical_row_sha256("hypomnema_entries", hrow)
    )


def test_genesis_and_line_hash_twin_match():
    assert TCB.genesis_prev_hash() == pkg.genesis_prev_hash()
    line = {
        "v": 1,
        "proposal_id": "p1",
        "content_sha256": pkg.canonical_content_sha256(_VECTORS[0]),
        "decision": "approved",
        "scope": "identity",
        "prev_sha256": pkg.genesis_prev_hash(),
    }
    assert TCB.line_hash(line) == pkg.line_hash(line)


def _build_chain():
    g = pkg.genesis_prev_hash()
    l0 = {"proposal_id": "a", "content_sha256": "x", "decision": "approved",
          "prev_sha256": g}
    l1 = {"proposal_id": "b", "content_sha256": "y", "decision": "approved",
          "prev_sha256": pkg.line_hash(l0)}
    l2 = {"proposal_id": "c", "content_sha256": "z", "decision": "approved",
          "prev_sha256": pkg.line_hash(l1)}
    return [l0, l1, l2]


def test_verify_chain_twin_accepts_intact():
    chain = _build_chain()
    assert TCB.verify_chain(chain) == pkg.verify_chain(chain) == (True, -1)


def test_verify_chain_twin_rejects_tampered_middle():
    chain = _build_chain()
    # Tamper the middle line's content — its line_hash changes, so line 2's
    # back-link no longer matches. Both twins must report the break at index 2.
    chain[1]["content_sha256"] = "TAMPERED"
    tcb_verdict = TCB.verify_chain(chain)
    pkg_verdict = pkg.verify_chain(chain)
    assert tcb_verdict == pkg_verdict
    assert pkg_verdict[0] is False
    assert pkg_verdict[1] == 2
