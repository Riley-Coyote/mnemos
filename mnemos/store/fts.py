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
