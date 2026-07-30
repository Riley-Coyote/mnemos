"""The packet's engram section must respect person/project scope.

Engrams carry only ``owner_agent_id``, so retrieval filters by agent alone —
but continuity is scoped by the full three-tuple, and every capture links its
engram to a scoped hypomnema row. The simple path
(`MnemosRuntime._retrieve`) already post-filters engrams by that link, so two
people sharing one agent don't see each other's durable memories.

`build_context_packet` — the path the advanced `mnemos_context` tool uses,
with ``include_engrams=True`` by default — did not. Its hypomnema, functional
and reflection sections were correctly scoped, so the packet looked healthy
while the engram section leaked the other person's memories: the signature
failure of a layer reporting success while carrying something it should not.

Usage is solo-per-agent, so this is a correctness/consistency fix rather than
a live multi-tenant emergency — but the two read paths must not disagree about
whose memory this is, or the fixed one silently drifts from the leaking one.
"""

from __future__ import annotations

import pytest

from mnemos.interface.context_packet import build_context_packet, format_context_packet
from mnemos.simple_runtime import MnemosRuntime

AGENT = "shared-agent"
CUE = "the secret launch project"


@pytest.fixture
def two_person_store(tmp_path):
    """One agent db, two people, each with a private engram-bearing memory."""
    db = str(tmp_path / "shared.db")

    alice = MnemosRuntime(db_path=db, agent_id=AGENT, person_id="alice",
                          project_scope="global")
    alice.capture(content="Alice's secret launch project is codenamed APOLLO",
                  importance="high")
    alice.close()

    bob = MnemosRuntime(db_path=db, agent_id=AGENT, person_id="bob",
                        project_scope="global")
    bob.capture(content="Bob's secret launch project is codenamed ZEPHYR",
                importance="high")
    bob.close()

    return db


def _packet_as(db, person_id):
    from mnemos.store.sqlite_store import EngramStore

    store = EngramStore(db)
    try:
        return format_context_packet(
            build_context_packet(
                store,
                query=CUE,
                agent_id=AGENT,
                person_id=person_id,
                project_scope="global",
                include_engrams=True,
            )
        )
    finally:
        store.close()


class TestThePacketDoesNotLeakAcrossPersons:
    def test_alice_never_sees_bobs_engram(self, two_person_store):
        packet = _packet_as(two_person_store, "alice")
        assert "ZEPHYR" not in packet, (
            "the packet surfaced another person's durable memory:\n" + packet
        )

    def test_bob_never_sees_alices_engram(self, two_person_store):
        packet = _packet_as(two_person_store, "bob")
        assert "APOLLO" not in packet, (
            "the packet surfaced another person's durable memory:\n" + packet
        )

    def test_each_person_can_still_see_their_own(self, two_person_store):
        # The filter must exclude the other person, not everything.
        assert "APOLLO" in _packet_as(two_person_store, "alice")
        assert "ZEPHYR" in _packet_as(two_person_store, "bob")


class TestBothReadPathsAgree:
    def test_the_store_helper_matches_the_runtime_filter(self, two_person_store):
        """The shared visibility check is the single source of truth.

        Both `MnemosRuntime._retrieve` and `build_context_packet` must consult
        it, so a memory visible on one path is visible on the other.
        """
        from mnemos.store.sqlite_store import EngramStore

        store = EngramStore(two_person_store)
        try:
            rows = store._get_conn().execute(
                "SELECT id, content FROM engrams"
            ).fetchall()
            apollo = next(r["id"] for r in rows if "APOLLO" in r["content"])
            zephyr = next(r["id"] for r in rows if "ZEPHYR" in r["content"])

            # Alice sees APOLLO, not ZEPHYR.
            assert store.engram_visible_in_scope(
                apollo, agent_id=AGENT, person_id="alice", project_scope="global"
            )
            assert not store.engram_visible_in_scope(
                zephyr, agent_id=AGENT, person_id="alice", project_scope="global"
            )
        finally:
            store.close()
