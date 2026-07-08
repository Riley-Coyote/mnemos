"""Step 1 retrieval event, retrieval-why, and citation logging tests."""

from __future__ import annotations

from mnemos.core.engram import Engram
from mnemos.instrumentation.receipts import ORIGIN_INFERENCE
from mnemos.cli import main
from mnemos.interface.context_packet import build_context_packet
from mnemos.interface.prompt_builder import PromptBuilder
from mnemos.retrieval.reactive import ReactiveRetriever
from mnemos.store.sqlite_store import EngramStore


def _seed(store: EngramStore, content: str) -> Engram:
    engram = Engram(
        content=content, owner_agent_id="default", origin_stamp=ORIGIN_INFERENCE
    )
    store.save_engram(engram)
    return engram


def test_retrieval_logs_events_and_receipts_without_scalar_mutation(tmp_path):
    store = EngramStore(tmp_path / "retrieval.db")
    try:
        engram = _seed(store, "Step one instrumentation should be record only")
        before = store.get_engram(engram.id)

        retriever = ReactiveRetriever(store, reconsolidation_enabled=False)
        results = retriever.retrieve("instrumentation record", max_results=2)

        after = store.get_engram(engram.id)
        assert results
        assert results[0].retrieval_event_id
        assert results[0].retrieval_why["path"] == "fts"
        assert before.access_count == after.access_count
        assert before.reconsolidation_count == after.reconsolidation_count
        assert before.stability == after.stability
        assert before.accessibility == after.accessibility

        events = store.get_retrieval_events()
        assert len(events) == 1
        assert events[0]["surfaced_engram_ids"] == [engram.id]

        receipts = store.get_runtime_receipts(kind="retrieval-why")
        assert len(receipts) == 1
        assert receipts[0]["engram_refs"] == [engram.id]
        assert receipts[0]["immediacy"] == "remembered"
        assert receipts[0]["payload"]["event_id"] == results[0].retrieval_event_id
    finally:
        store.close()


def test_context_packet_marks_citations_only_for_serialized_engrams(tmp_path):
    store = EngramStore(tmp_path / "citations.db")
    try:
        engram = _seed(store, "Context packets cite surfaced Step one memories")

        packet = build_context_packet(
            store,
            "Step one memories",
            include_prompt=False,
            max_engrams=1,
        )

        assert [item["id"] for item in packet["mnemos_engrams"]] == [engram.id]
        assert packet["mnemos_engrams"][0]["retrieval_why"]["path"] == "fts"
        assert "event_id" not in packet["mnemos_engrams"][0]["retrieval_why"]

        citations = store.get_retrieval_citations()
        assert len(citations) == 1
        assert citations[0]["engram_id"] == engram.id
        assert citations[0]["surface"] == "context_packet"
        assert citations[0]["metadata"]["tier"] == "rendered"
        assert citations[0]["metadata"]["fitting_eligible"] is False
    finally:
        store.close()


def test_prompt_builder_marks_rendered_citations_as_not_fitting_eligible(tmp_path):
    store = EngramStore(tmp_path / "prompt.db")
    try:
        engram = _seed(store, "Prompt builder renders Step one memories")

        prompt = PromptBuilder(store).build("Step one memories")

        assert "Prompt builder renders" in prompt
        citations = store.get_retrieval_citations()
        assert len(citations) == 1
        assert citations[0]["engram_id"] == engram.id
        assert citations[0]["surface"] == "prompt_builder"
        assert citations[0]["metadata"]["tier"] == "rendered"
        assert citations[0]["metadata"]["fitting_eligible"] is False
    finally:
        store.close()


def test_cli_search_marks_operator_visible_citations_as_not_fitting_eligible(
    tmp_path, capsys
):
    db = tmp_path / "cli.db"
    store = EngramStore(db)
    try:
        engram = _seed(store, "CLI search displays Step one memories")
    finally:
        store.close()

    assert main(["--db-path", str(db), "search", "Step one"]) == 0
    assert "CLI search displays" in capsys.readouterr().out

    store = EngramStore(db)
    try:
        citations = store.get_retrieval_citations()
        assert len(citations) == 1
        assert citations[0]["engram_id"] == engram.id
        assert citations[0]["surface"] == "cli_search"
        assert citations[0]["metadata"]["tier"] == "operator-visible"
        assert citations[0]["metadata"]["fitting_eligible"] is False
    finally:
        store.close()


def test_cli_search_survives_citation_and_failure_counter_errors(
    tmp_path, capsys, monkeypatch
):
    db = tmp_path / "cli-best-effort.db"
    store = EngramStore(db)
    try:
        _seed(store, "CLI search still displays when citations fail")
    finally:
        store.close()

    def raise_unavailable(*_args, **_kwargs):
        raise RuntimeError("instrumentation unavailable")

    monkeypatch.setattr(EngramStore, "mark_retrieval_citation", raise_unavailable)
    monkeypatch.setattr(
        EngramStore, "record_instrumentation_failure", raise_unavailable
    )

    assert main(["--db-path", str(db), "search", "citations fail"]) == 0
    assert "CLI search still displays" in capsys.readouterr().out
