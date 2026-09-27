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
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping

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
) -> list[tuple[str, float]]:
    """Rank ``texts`` (id to words) by the words of ``cue`` worth searching for,
    with bm25 as FTS5 computes it, over these texts alone.

    For what recall ranks beside the memories that has no full-text index of
    its own: handoffs. The cue's words are ``search_words``, as the memories'
    search uses; a word counts ignoring case. Best first, each with its score
    (higher is better). A text holding none of the words is left out.
    """
    terms = list(dict.fromkeys(word.lower() for word in search_words(cue)))
    if not terms or not texts:
        return []
    counted = {
        key: Counter(word.lower() for word in _TOKEN.findall(text or ""))
        for key, text in texts.items()
    }
    lengths = {key: sum(counts.values()) for key, counts in counted.items()}
    average = sum(lengths.values()) / len(lengths) or 1.0
    holding = {term: sum(1 for counts in counted.values() if term in counts) for term in terms}
    scored: list[tuple[str, float]] = []
    for key, counts in counted.items():
        score = 0.0
        for term in terms:
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
