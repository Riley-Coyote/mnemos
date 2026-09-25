"""Words as the full-text index sees them.

``engrams_fts`` uses FTS5's default ``unicode61`` tokenizer: a token is a run of
letters and digits, and anything else separates tokens. Queries were built from
whitespace-split words that also had to pass ``str.isalnum()``, which silently
dropped any word with punctuation attached: "alive?", "residents'", "house:",
"decline." — usually the last word of a sentence, and often the one that
mattered. Splitting the way the index splits keeps them.
"""

from __future__ import annotations

import re

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
""".split())


def distinctive_terms(text: str) -> set[str]:
    """What a text is about, as a set: its words of four letters or more, lower-cased,
    without the common ones."""
    return {w.lower() for w in fts_words(text, min_len=4) if w.lower() not in _COMMON}


def overlap(a: set[str], b: set[str]) -> float:
    """How much of the smaller set the larger one shares (the overlap coefficient):
    saying the same thing at greater length still reads as the same thing."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))
