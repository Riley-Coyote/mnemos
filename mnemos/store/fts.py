"""Words as the full-text index sees them, and the one list of common words.

``engrams_fts`` uses FTS5's default ``unicode61`` tokenizer: a token is a run of
letters and digits, and anything else separates tokens. Queries were built from
whitespace-split words that also had to pass ``str.isalnum()``, which silently
dropped any word with punctuation attached: "alive?", "residents'", "house:",
"decline." — usually the last word of a sentence, and often the one that
mattered. Splitting the way the index splits keeps them.

Every place that turns text into the words that matter reads this module's one
list of common words: the search a cue seeds recall from, the links and lessons
made from shared words, how a note is scored against a query, the filters
recall and corrections apply, and identity's comparisons. There used to be
three lists (this one, the simple runtime's and identity's), and they
disagreed: "notes" and "mnemos" were noise to recall's filters but still seeded
its search, and "whatever" was noise to recall but a word to identity.

A word no list names can still be common in one store: a name, a project, a
year. ``word_shares`` measures it there, in the memories' words and lessons
(WP-R08c). On a copy of the live store "riley" is in 270 of the 464 live
memories of its scope (58%), "2026" in 239 (52%) and "real" in 158 (34%): the
words outside the lists over ``COMMON_SHARE``.
"""

from __future__ import annotations

import math
import re
import sqlite3
import weakref
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# letters and digits; underscore separates, as it does for unicode61
_TOKEN = re.compile(r"[^\W_]+")


def fts_words(text: str, min_len: int = 3) -> list[str]:
    """The words of ``text`` as FTS5 indexes them: in order, each once (ignoring
    case), at least ``min_len`` characters long. Safe to quote as FTS5 phrases."""
    seen: set[str] = set()
    words: list[str] = []
    for word in _TOKEN.findall(text or ""):
        key = word.lower()
        if len(word) >= min_len and key not in seen:
            seen.add(key)
            words.append(word)
    return words


def or_query(words: list[str]) -> str:
    """An FTS5 query matching any of ``words``, each quoted as a phrase."""
    return " OR ".join(f'"{w}"' for w in words)


# Words of four letters or more that say nothing about what a text is about.
# Shorter words are already too short to count. The second group was found
# winning belief themes on real stores: reporting verbs, connectives, numbers,
# the halves of contractions ("didn't" splits into "didn" and "t") and URL
# schemes. A word that can name a subject stays out, however often it comes up.
# The third group is the words about memory itself, which every note here
# shares: a note is not about "notes" or "memory" because it is one (the simple
# runtime's list). The fourth is what identity's list added: words that name
# nothing in a statement of who one is ("I exist", "my work").
_COMMON = frozenset("""
    about above after again against also although always another anything around away back been before
    being below between both came come could does doing done down during each even ever every from have
    having here into just keep kept know like made make many more most much must never next once only onto
    other ought over same should since some still such take than that their them then there these they
    thing things this those though through till together under until upon very want were what whatever
    when where whether which while will with within without would your yours
""".split() + """
    asked asks said says told wants
    actually already because currently either else exactly instead rather unless
    anyone everything none nothing someone theirs
    first second third three four five seven zero
    didn doesn http https
""".split() + """
    agent agents context continuity durable memories memory mnemos note notes
""".split() + """
    exist exists hers itself makes myself ours really section two way whom work
""".split())


# Words of three letters or fewer that say nothing about what a cue is after.
# Words this short never count as distinctive, so only a search made from a cue,
# and identity's comparison (which keeps two-letter words), need them.
_COMMON_SHORT = frozenset("""
    all and any are but can did don for get got had has her him his how its let nor not now off one our
    out own per say she the too via was who why yet you
""".split() + """
    a am an as at be by do he i if in is it me my no of on or so to up we
""".split())


def distinctive_terms(text: str) -> set[str]:
    """What a text is about, as a set: its words of four letters or more, lower-cased,
    without the common ones."""
    return {w.lower() for w in fts_words(text, min_len=4) if w.lower() not in _COMMON}


def is_common(word: str) -> bool:
    """Whether a word says nothing about what a text or a cue is about."""
    word = word.lower()
    return word in _COMMON or word in _COMMON_SHORT


def meaningful_words(text: str) -> set[str]:
    """The words that say what a text or a cue is about, as a set: its words of three
    letters or more, lower-cased, without the common ones."""
    return {w.lower() for w in fts_words(text) if not is_common(w)}


def search_words(cue: str) -> list[str]:
    """The words of a cue worth searching for, in order: its words as the index sees
    them, less the common ones. A cue made only of common words keeps them all, so
    there is always something to search for."""
    words = fts_words(cue)
    meaningful = [w for w in words if not is_common(w)]
    return meaningful or words


# A word held by more than this share of the live memories in a scope says
# little about which of them a cue is after: recall leaves it out of its words
# query, and the cue does not count it as a distinctive word shared with a
# message. Reciprocal rank fusion keeps only each list's order, not bm25's
# weight, so a words match on such a word counted as much as one on a rare
# word. On a copy of the live store, each of the five memories recall returned
# for "how does Riley like to be told about mistakes" matched by words on
# "Riley" alone (none holds "mistakes"; one was 18th by meaning), and the best
# words match was a note about a page footer's link. Left out, all five come
# by meaning. The cut is 25%, not lower: the 72 words between 8% and 25% of
# that scope ("polyphonic", "room", "sanctuary", "page") name the work itself.
# Cut at 8% (counting words alone), such words cost the cue 5 of the 20 lines
# that bore on the lab's development prompts and changed none of the lab's 29
# facts.
COMMON_SHARE = 0.25
# Below this many live memories in a scope no word is cut: a share of a handful
# of memories is noise (of 10 memories, a word 3 of them hold is over a
# quarter), and a small store has little for meaning to rank.
COMMON_MIN_MEMORIES = 100


@dataclass
class _Counted:
    """Word counts for one scope, as one connection saw the store at one
    write generation. ``lessons`` holds the live memories' lessons (read on
    first need, or given by recall, which reads them anyway), ``lowered``
    the same lower-cased, and ``lesson_words`` each one's words, split only
    when a word searched for is in its text at all."""

    conn: Any
    generation: tuple[int, int]
    live: int
    counts: dict[str, int]
    lessons: dict[str, str] | None = None
    lowered: dict[str, str] = field(default_factory=dict)
    lesson_words: dict[str, set[str]] = field(default_factory=dict)

    def lessons_holding(self, word: str) -> set[str]:
        """The memories whose lesson holds ``word``, split into words as
        ``rank_by_words`` splits a lesson."""
        holding = set()
        for memory_id, lesson in (self.lessons or {}).items():
            if word not in self.lowered[memory_id]:
                continue  # not even inside another word: no need to split it
            words = self.lesson_words.get(memory_id)
            if words is None:
                words = self.lesson_words[memory_id] = {w.lower() for w in _TOKEN.findall(lesson)}
            if word in words:
                holding.add(memory_id)
        return holding


# Each word's count of live memories, for this process: per store (held
# weakly, so it goes with the store) and scope, the connection they were
# counted on, its write generation then, the scope's live count and
# {word: count}. The generation is SQLite's own: ``PRAGMA data_version`` moves
# when another connection (another process included) commits to the file,
# and the connection's ``total_changes`` when it writes itself. So a
# correction that swaps one word for another, leaving the count of live
# memories as it was, is counted afresh.
_WORD_COUNTS: weakref.WeakKeyDictionary[Any, dict[tuple[str, str, str], _Counted]] = (
    weakref.WeakKeyDictionary()
)

_LIVE_IN_SCOPE = (
    "e.state IN ('active', 'dormant') AND e.owner_agent_id = ? "
    "AND e.person_id = ? AND e.project_scope = ?"
)


def _generation(conn: sqlite3.Connection) -> tuple[int, int]:
    """Changes whenever anything is written to the file: by this connection
    (``total_changes``) or by any other one (``data_version``)."""
    return conn.execute("PRAGMA data_version").fetchone()[0], conn.total_changes


def word_shares(
    store: Any,
    words: Iterable[str],
    *,
    agent_id: str,
    person_id: str,
    project_scope: str,
    lessons: Mapping[str, str] | None = None,
) -> dict[str, float]:
    """Each of ``words``' share of the live memories (active and dormant) in
    one scope, lower-cased: how many of them hold it, in their words as the
    full-text index matches it or in their lesson (``live_memory_lessons``:
    what recall ranks beside their words, and the cue reads), each memory
    once, over how many there are. The index holds no lesson, so a word most
    lessons hold would otherwise count as rare.

    Counted and cached for this process, per store and scope, until anything
    is written to the store, by any connection. ``lessons`` are the scope's
    ``live_memory_lessons`` when the caller has just read them from this
    store. Empty when the scope holds fewer than ``COMMON_MIN_MEMORIES`` live
    memories, or the index can't be read: then no word is common by its
    share. Reads only."""
    wanted = list(dict.fromkeys(word.lower() for word in words if word))
    if not wanted:
        return {}
    scope = (agent_id, person_id, project_scope)
    try:
        conn = store._get_conn()
        generation = _generation(conn)
        try:
            kept = _WORD_COUNTS.setdefault(store, {})
        except TypeError:
            kept = {}  # a store that can't be held weakly is counted each time
        counted = kept.get(scope)
        if counted is None or counted.conn is not conn or counted.generation != generation:
            live = conn.execute(
                f"SELECT COUNT(*) FROM engrams e WHERE {_LIVE_IN_SCOPE}", scope,
            ).fetchone()[0]
            counted = kept[scope] = _Counted(conn, generation, live, {})
        if counted.live < COMMON_MIN_MEMORIES:
            return {}
        for word in wanted:
            if word not in counted.counts:
                if counted.lessons is None:
                    counted.lessons = {
                        memory_id: lesson or ""
                        for memory_id, lesson in (
                            lessons if lessons is not None else _live_lessons(store, *scope)
                        ).items()
                    }
                    counted.lowered = {
                        memory_id: lesson.lower() for memory_id, lesson in counted.lessons.items()
                    }
                phrase = '"' + word.replace('"', '""') + '"'
                holding = {
                    row[0] for row in conn.execute(
                        "SELECT DISTINCT e.id FROM engrams_fts f JOIN engrams e ON e.id = f.id "
                        f"WHERE engrams_fts MATCH ? AND {_LIVE_IN_SCOPE}",
                        (phrase, *scope),
                    )
                }
                counted.counts[word] = len(holding | counted.lessons_holding(word))
    except (sqlite3.Error, AttributeError, TypeError):
        return {}
    return {word: counted.counts[word] / counted.live for word in wanted}


def _live_lessons(
    store: Any, agent_id: str, person_id: str, project_scope: str,
) -> Mapping[str, str]:
    """The live memories' lessons in one scope (``live_memory_lessons``);
    none from a store that can't give them."""
    lessons_of = getattr(store, "live_memory_lessons", None)
    if lessons_of is None:
        return {}
    return lessons_of(agent_id=agent_id, person_id=person_id, project_scope=project_scope)


def common_words(
    store: Any,
    words: Iterable[str],
    *,
    agent_id: str,
    person_id: str,
    project_scope: str,
    share: float = COMMON_SHARE,
    lessons: Mapping[str, str] | None = None,
) -> set[str]:
    """Which of ``words`` (lower-cased) are in more than ``share`` of the live
    memories in the scope (``word_shares``). Below ``COMMON_MIN_MEMORIES``
    live memories, none is."""
    shares = word_shares(
        store, words, agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        lessons=lessons,
    )
    return {word for word, part in shares.items() if part > share}


# Ids asked about in one statement, well under SQLite's variable limit.
_ID_CHUNK = 400


def search_among(
    store: Any,
    query: str,
    ids: Collection[str],
    *,
    agent_id: str,
    person_id: str,
    project_scope: str,
    state: str = "active",
    limit: int = 30,
) -> list[tuple[str, float]]:
    """The memories among ``ids`` in one scope and ``state`` that match the
    full-text ``query``, best first, at most ``limit``: ``(id, rank)``, where
    rank is FTS5's bm25 rank as ``search_fts_ranked`` gives it (lower is
    better, on the same scale for the same index). Recall searches, with
    every word, the memories meaning can't find. Reads only."""
    found: list[tuple[str, float]] = []
    ordered = sorted(set(ids))
    conn = store._get_conn()
    for start in range(0, len(ordered), _ID_CHUNK):
        chunk = ordered[start:start + _ID_CHUNK]
        found.extend(
            (row[0], row[1]) for row in conn.execute(
                "SELECT e.id, f.rank FROM engrams e JOIN engrams_fts f ON e.id = f.id "
                "WHERE engrams_fts MATCH ? AND e.state = ? AND e.owner_agent_id = ? "
                "AND e.person_id = ? AND e.project_scope = ? "
                f"AND e.id IN ({', '.join('?' for _ in chunk)}) ORDER BY rank LIMIT ?",
                (query, state, agent_id, person_id, project_scope, *chunk, limit),
            ).fetchall()
        )
    found.sort(key=lambda item: item[1])
    return found[:limit]


def overlap(a: set[str], b: set[str]) -> float:
    """How much of the smaller set the larger one shares (the overlap coefficient):
    saying the same thing at greater length still reads as the same thing."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# bm25's constants as FTS5 uses them.
_BM25_K1 = 1.2
_BM25_B = 0.75


def rank_by_words(
    cue: str, texts: Mapping[str, str], limit: int | None = None,
    *, terms: Iterable[str] | None = None, every_word_for: Collection[str] = (),
) -> list[tuple[str, float]]:
    """Rank ``texts`` (id to words) by the words of ``cue`` worth searching for,
    with bm25 as FTS5 computes it, over these texts alone.

    For what recall ranks beside the memories that has no full-text index of
    its own: handoffs. The cue's words are ``search_words``, as the memories'
    search uses, or ``terms`` when given (what recall searched the memories
    for, common words left out; none, and nothing is ranked); a word counts
    ignoring case. The texts ``every_word_for`` names (the ones meaning can't
    find) are ranked by every one of ``search_words`` all the same, on the same
    scale. Best first, each with its score (higher is better). A text holding
    none of its words is left out.
    """
    searched = list(dict.fromkeys(
        word.lower() for word in (search_words(cue) if terms is None else terms)
    ))
    every = (
        list(dict.fromkeys(word.lower() for word in search_words(cue)))
        if every_word_for else searched
    )
    if not (searched or (every and every_word_for)) or not texts:
        return []
    counted = {
        key: Counter(word.lower() for word in _TOKEN.findall(text or ""))
        for key, text in texts.items()
    }
    lengths = {key: sum(counts.values()) for key, counts in counted.items()}
    average = sum(lengths.values()) / len(lengths) or 1.0
    holding = {
        term: sum(1 for counts in counted.values() if term in counts)
        for term in dict.fromkeys([*searched, *every])
    }
    scored: list[tuple[str, float]] = []
    for key, counts in counted.items():
        score = 0.0
        for term in (every if key in every_word_for else searched):
            found = counts.get(term, 0)
            if not found:
                continue
            rarity = math.log(1 + (len(counted) - holding[term] + 0.5) / (holding[term] + 0.5))
            length = 1 - _BM25_B + _BM25_B * lengths[key] / average
            score += rarity * found * (_BM25_K1 + 1) / (found + _BM25_K1 * length)
        if score > 0:
            scored.append((key, score))
    scored.sort(key=lambda item: -item[1])
    return scored if limit is None else scored[:limit]
