"""Which version of the rules this code writes and maintains memory by.

A Claude Code session keeps the Mnemos code it imported when it started, so a
server that has run for days still maintains memory by the rules of the day it
began: its decay, linking, softening, lessons, questions, belief changes and
identity. Once newer code has changed those rules, an older server keeps
applying the old ones to the same store, and every layer reports success.

So each store remembers the newest version that has opened it
(``meta.min_code_version``). Code whose version is lower no longer maintains
that store. It still takes the agent's own writes (captures, handoffs,
reflections, corrections), because refusing those would lose memories.

Bump ``MAINTENANCE_CODE_VERSION`` in any change to how memory is written or
maintained. Never lower it: a store already raised past a version stays there.
"""

from __future__ import annotations

MAINTENANCE_CODE_VERSION = 1

OLDER_CODE_MESSAGE = (
    "This session runs older Mnemos code than the store expects. "
    "Restart the session."
)
