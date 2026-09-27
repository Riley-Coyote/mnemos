# Changelog

## 0.3.1 (unreleased)

### One capture, one object; corrections that land once

A capture writes two things, a continuity note (what the briefing is built
from) and a memory (what the graph holds), and each correction path updated
one and not the other. Correcting by the note's id left the memory saying the
old thing; correcting by the memory's id left the note saying it in the
briefing; two corrections of one note left two live replacements; correcting
and then forgetting left the corrected memory live. No correction recorded
what it replaced. The capture itself committed the memory, the note and the
link between them separately, so a failure between them left half a pair. On
a copy of a real store (2026-09-27), `claude-code/user/global` held one pair
whose note and memory said different things (a note corrected by its id), and
three live notes over memories a correction by memory id had replaced, each
replacement without a note.

- A capture saves its memory and its note in one transaction, the note
  pointing at the memory; the memory's vector is written once that
  transaction commits (inside it, the vector's own connection would wait on
  the lock and fail, and encoding swallows that failure). Either id reaches
  both.
- Every correction path (the note's id, the memory's id, a query) retires the
  note and its memory together and writes the replacement pair, in one
  transaction; a forget retires both and writes nothing. The old pair is kept,
  archived, never deleted. An id a correction already replaced reaches its
  current version, so correcting or forgetting twice never leaves two live.
- A correction records what it replaced: a `supersedes` link from the new
  memory to the old, `lineage.supersedes` / `lineage.superseded_by` on both,
  the old note's `superseded_by`, and a version on the new memory keeping the
  old words (`change_reason` 'correction'), written only when the words
  change. The replacement pair and its version are signed with the
  corrector's model and session; a retired note's revision records who
  retired it (`revised_by`, `revised_by_session`).
- An impact given with a correction becomes a lesson about the mistake,
  drawn from the replacement (only the agent's own words make one).
- A correction never moves a belief by the words it shares with one: the
  word-overlap heuristic is gone. `mnemos_correct(target_id="belief_...")`
  retires a belief the agent stated (`action=forget`), or replaces it with the
  agent's words, formed at 40% as a stated belief is, the old one retired and
  pointing at it.
- Only one capture writing them together makes a note and a memory a pair
  (`graduated_to_engram_id`). A note that only references a memory
  (`related_engram_id`, as the advanced tools' summaries do) is corrected and
  forgotten on its own, never shares that memory's fate, and is never
  promoted into it. A corrected summary still references what it summarises.
- A note shares its memory's fate. When decay takes the memory dormant or
  into the archive, the note stops showing (briefing, recall, counts); when
  the memory wakes or `resharpen` restores it, the note is back. Nothing is
  copied between them. This is only what is shown: a correction or a forget
  by query still finds such a pair, and retires it, so waking it later
  cannot bring the old words back. Recalling the note by its id brings its
  memory back as recalling the memory's id would. The id of a note or memory
  a correction replaced says so and names the version now in use, never the
  old words.
- `MAINTENANCE_CODE_VERSION` is 6. Code older than the store still records a
  correction in the agent's words and retires what it names, and writes no
  link, lineage, version, lesson or belief change.

Migration (schema 13). The first open by this code adds `author_model` and
`author_session` to `versions` (empty for every existing row) and two indexes
on the note-to-memory columns, after the usual verified pre-migration backup.
Once per store (recorded in `meta.capture_pairs_linked`), it pairs a capture
note that named its memory only as a reference, when the note's words are
the memory's words as a capture writes them and the two were written within
10 seconds of each other (one capture call); no other row changes. On a fresh
copy of the real store it opened in under a second and paired none: its 200
capture notes were already paired, and its 9 notes with only a reference are
summaries whose words differ (all in `claude-field`).

Changed behaviour, and what to do:

- A correction by note id now answers with the new note's and memory's ids
  ("Continuity note ID", "Memory ID"); the note id changes, as the memory id
  always did. An impact given that way is saved.
- Correcting a belief needs its id; words alone no longer retire or lower one.
- Forgetting a note that only references a memory no longer archives that
  memory.
- Notes over dormant or archived memories leave the briefing. On the copy,
  its live notes went from 200 to 181: 16 over dormant memories and 3 over
  memories replaced by a correction.
- Pairs split before this change stay as they are; repairing them is a
  separate, approved step.

### Every memory says who wrote it

The memory could not tell the agent's words from a tool's. No memory recorded
its author. On a copy of a real store (2026-09-26), 103 of the 469 memories in
`claude-code/user/global` were the transcript indexer's model's words: memories
it extracted and lessons copied from them. A lesson copies the impact it is
drawn from, so a model's words landed in the most durable tier, and identity,
belief questions and lessons were all measured from them. And the signature
came from one declared model per scope: a Grok session that introduced itself
became the store's declared model for everyone.

- Every memory records `author_kind` (`agent`, `tool`, `system`, `import` or
  `unknown`), `author_model` and `author_session` (the harness session,
  `CLAUDE_CODE_SESSION_ID`), when it is written. A later save never changes
  them. Captures, corrections, reflections, handoffs and lessons are the
  agent's, and so is what it keeps through the advanced tools
  (`mnemos_remember`, `mnemos_ingest`, which take `signed_as` too). The
  transcript indexer's and the substrate's memories are a tool's; the deep
  reflection pass's thoughts are a tool's (with a model) or Mnemos's
  (without). A path that doesn't say is `unknown`, never guessed. Continuity notes already
  recorded their kind of writer (`authored_by`) and model; captures now also
  record the session.
- The signature is resolved on every write, because the model can change
  mid-session: `signed_as` (a new parameter on `mnemos_capture`,
  `mnemos_correct`, `mnemos_reflect` and `mnemos_handoff`, which the server
  instructions ask the agent to fill with its own model id), then
  `MNEMOS_AGENT_MODEL`, then the model *this* session last introduced itself
  as (kept per harness session, so another process of the session signs the
  same way, and never another session's), then the session's transcript.
  The SessionStart hook finds its reader the same way when the harness names
  no model. The health card and `mnemos doctor` show whom this session
  introduced itself as (model and name), or "none this session"; they no
  longer show the scope's last introduction, which named a Grok session.
- Only the agent's own words make it who it is: identity (the identity pass,
  `mnemos identity diff`, the identity graph), belief questions and theme
  mining read only `author_kind = 'agent'`, and a lesson is drawn only from an
  impact the agent wrote (`impact_source` 'agent') on a memory the agent wrote.
  The agent's evidence never strengthens a tool's "lesson", and the agent is
  not asked what a tool's memory taught. The packet signs a lesson a tool
  wrote "from a tool, not yours".
- `memory_trace`: one row per tool call (context, capture, recall, correct,
  reflect, handoff, introduce, maintain; health stays read-only), with the
  session, the signing model, the ids the call showed or returned and the ids
  it wrote (a memory recall restores from the archive counts as written). Ids
  only, never text. Rows older than 90 days are dropped.
- `mnemos repair quarantine-tool-written` moves the tool-written memories in
  a scope into the legacy quarantine (no person or project; their words,
  links and history stay), dry run unless `--write`, after a verified
  backup. `--undo --write` brings back exactly what it moved; `mnemos
  adopt-legacy --include indexer` brings them back with all the other
  tool-written memories the quarantine holds. It only runs by hand. On the
  copy it listed 103 (96 active, 7 dormant), moved them, and `--undo` restored
  the scope to the same 469 ids. The health card and `adopt-legacy` now call
  that class "written by a tool".

Migration (schema 12). The first open by this code adds the three columns,
after the usual verified pre-migration backup, and labels what the store held
once, from what each row carries: `tool` when tagged `session-indexed`;
`agent` for a capture or correction made through the agent's own tools (a
`session` memory tagged `continuity`) and for a lesson whose words are exactly
an agent-written impact of a memory it was drawn from; everything else
`unknown`. The counts are kept in `meta.engram_authors_labeled`. On the copy:
355 agent, 103 tool, 11 unknown in `claude-code/user/global` (356, 6,989 and
135 in the whole file), in under two seconds.

Changed behaviour, and what to do:

- `MAINTENANCE_CODE_VERSION` is 5. Sessions still running older code stop
  maintaining the store once this code opens it (restart them); what they
  capture meanwhile is recorded as `unknown` and so stays out of identity,
  belief questions and lessons.
- `mnemos daemon install` no longer schedules `substrate-tick`, whose handlers
  write memories in a model's words; installing or uninstalling removes one an
  earlier install scheduled. Neither do the OpenClaw generators
  (`mnemos setup-openclaw`, whose install replaces an earlier `mnemos-*`
  substrate job, and the bootstrap's cron commands); the
  `openclaw/crons/substrate-tick.md` template is gone. `mnemos
  substrate-tick` and `mnemos index` still run by hand, and say that they
  write a model's words.
- `health()["identity"]` is now `{session, model, name}` for this session's
  own introduction, in place of `declared_model` and `declared_name`.
- The legacy quarantine counts lessons distilled from indexer output as the
  indexer's (they copy its words), so a default `mnemos adopt-legacy` leaves
  them hidden; name `--include indexer` to bring them back.
- To move the indexer's memories out of recall and the packet: `mnemos repair
  quarantine-tool-written` (dry run), then `--write`.

### No one-way doors for memories that fade

A memory could leave the active set and never come back, with every check
green. Decay read active memories only, so a dormant one was never touched
again: it could neither finish fading nor come back. Recall seeded from
active memories only, so a dormant memory stayed out however exactly a cue
named it: on a copy of a real store, a recall of each dormant memory's own
distinctive words brought back none of its 24. A dormant or archived memory
reached through a link still passed activation on to its neighbours.
`archive.resharpen` had no caller, an archived memory's id returned nothing,
and the dream report said dormant memories were "ready to wake if needed".

- Recall seeds dormant memories that match the cue, at half the activation
  an active match would start with (at most 10, beside the active seeds), so
  an equal active match comes first. A dormant memory that is returned wakes:
  it is active again, with the accessibility any return gives it. Waking is
  part of the return, so it happens once per session, and a dormant memory
  recalled by its id wakes too. On that copy, the same recalls now bring back
  all 24, 19 of them first.
- Dormant and archived memories take no part in resonance: they pass no
  activation on, and none reaches them through a link. A dormant memory comes
  back for its own match, or not at all. Results show "(it had gone quiet)"
  beside one.
- Decay keeps running over dormant memories until they wake or reach the
  archive: every one of them, read a page at a time apart from the active
  ones, so none waits behind the limit on how many active memories one pass
  reads (10,000). The floors that hold an active memory up (recent use,
  `foundational`, `active_project`) never lift a dormant one. The decay stats
  count dormant memories apart, and `engrams_dormant` still counts only the
  ones that went dormant in that pass. On another copy, one maintenance cycle
  lowered all 24 and archived none.
- A memory that faded into the archive (archived by decay) comes back by its
  exact id, or through `mnemos_recall` with `include_archived=true` when the
  query names it (half its meaningful words, and two when it has two or more:
  the bar a correction's query clears). Every faded memory in the scope is
  weighed, however many there are. Either way it is restored with
  `resharpen` and counts as returned. A memory the agent forgot, or replaced
  with a correction, stays where it was put, by id and by flag.
- `resharpen` restores only a memory that is archived, in one transaction,
  and at full resolution when it brings back the original words, keeping the
  worn wording as a version. An archive row whose memory is active or dormant
  again is left alone; a copy of that store held 1,392 such rows.
- `mnemos_health` and `mnemos doctor` count dormant memories, and the faded
  ones an ordinary recall cannot reach, with the call that reaches them:
  `mnemos_recall("<its words>", include_archived=true)`. Doctor still opens
  the store read-only.
- The dream report says "N memories went quiet. A strong match brings them
  back." A cycle that archives some memories and quiets others reports both.
- `MAINTENANCE_CODE_VERSION` is 4. Code older than the store still reads
  dormant and faded memories, but wakes, fades and restores none of them. The
  decay pass checks the store's minimum itself, so callers without the
  runtime's gate (the bridge, the advanced server) leave dormant memories
  alone too.

Changed behaviour, and what to do:

- Recall and `mnemos_context` can return a dormant memory, marked "(it had
  gone quiet)", and returning it wakes it.
- `mnemos_recall` takes `include_archived` (default false). With it, what
  faded into the archive appears after the durable memories under "From the
  archive:", and comes back.
- `mnemos_health` returns `counts.memories_dormant` and `unreachable`
  (`count` and `command`), and the card's memories line counts dormant ones.
- Dormant memories no longer stay dormant indefinitely: unless recalled, they
  keep fading and reach the archive, where their id or `include_archived`
  finds them.

### One briefing, written for the one reading it

The session-start packet is where this memory does its work, and two builders
made it: the SessionStart hook's and `mnemos_context`'s. It showed each belief
three times (as a core belief, as a living question and in the list), printed
sections that are always empty in simple mode (scope, functional memory,
review queue, "How To Use"), ranked notes against a fixed sentence so recency
decided, and had lost its maintenance report: the report was looked for among
the 50 top-scoring notes, where it no longer ranks, so each cycle wrote a new
one instead of replacing the last. On a copy of a real store (2026-09-26) the
hook's packet ran to its 10,364-character ceiling and was cut off in the
middle of its question, and 117 reports were active at once.

- One builder, `build_context_packet`, makes the packet for the hook and for
  `mnemos_context`. Given the same session, model and folder they return the
  same text (checked on that copy: 5,975 characters from each).
- Six sections, in this order, each left out when it has nothing: **Where you
  left off** (the reader's own handoff whole, then up to two notes from the
  last three days), **Who you're with** (three foundational notes), **What
  you're carrying** (three notes or lessons), **Beliefs** (each once, with its
  confidence), **One question** (at most one, with the verdict call it takes)
  and **While you were away** (the latest maintenance report, only when that
  cycle changed something). A memory with nothing to carry gives no packet.
- A handoff another session of the same model left is framed as the reader's
  own ("Yours (Opus 5.5), from another session"). Only a different model's
  note is a colleague's and carries "don't claim its work as yours" (Riley's
  decision). A note whose author or reader can't be placed says so.
- What you're carrying is ranked by the distinctive words it shares with the
  names of the session's folder and repository (a worktree counts as its
  repository), then by recency. The hook reads the folder from its payload
  (`cwd`); `mnemos_context` uses its server's working folder, which Claude
  Code sets to the session's. The folder only ranks; it never chooses the
  scope. One of the three is always a concrete, dated episode (a note that
  names someone or something and something specific) when there is one.
- The packet stays under 6,000 characters (`--token-budget` now defaults to
  1500 tokens), and so does anything appended to it: `--include-graph` recall
  and `mnemos_context`'s results for a query get only the room left, and an
  entry that doesn't fit is left out whole. The reader's handoff is never cut.
  Notes are cut at a sentence boundary and end with their id, and
  `mnemos_recall("<id>")` returns any note the packet cut, a handoff, a
  continuity note or a lesson, whole. A forgotten note does not come back by
  its id.
- Asking for a memory by its id is a use: `mnemos_recall("<engram id>")`
  reinforces an active memory as a query that returns it would, once a
  session and never on code older than the store. A note read by its id is
  not reinforced (notes have no reinforcement), and the packet showing a
  memory is not a use: the briefing reinforces nothing. Graph recall
  reinforces only the entries it shows.
- The maintenance report is found by its tag, so each new report replaces the
  last again. Reports that piled up stay as they are.
- Code older than the store shows no question and spends no showing on the
  hook's path too, as `mnemos_context` already did.

Changed behaviour, and what to do:

- `mnemos_context` runs no maintenance. Upkeep still rides on captures and
  corrections, and `mnemos_maintain` runs it on demand. The tool is no longer
  annotated destructive. With a query, it appends what else matches after the
  packet (`### For "<query>"`), as recall finds it, leaving out everything the
  packet rendered (notes, handoffs and lessons); what the budget left out of
  the packet can come back here.
- The packet no longer shows the identity summary, functional memory, the
  review queue, the scope or identity-divergence notes (`mnemos recall` finds
  those). `build_context_packet` no longer returns `identity`, `signers`,
  `functional_memory`, `review_queue`, `session` or `stats`; it still accepts
  `session_id`, `max_functional` and `max_hypomnema` and ignores them, and
  `query` is used only for `include_engrams` graph recall.
- `MAINTENANCE_CODE_VERSION` stays 3: this changes what the packet shows, not
  how memory is written or maintained.

### Returns that mean something, and versions only when words change

Recall strengthens a memory because it came back to the one reading it. But
reconsolidation ran inside retrieval, before the runtime's own filters, so
memories the reader was never shown were strengthened, and linked to the ones
it was shown. Nothing limited how often a memory was reinforced: on a copy of
a real store, the most-returned memory had been reinforced 31,517 times.
Maintenance counted its own reinforcing of a lesson as an access. And every
return appended a full copy of the unchanged memory as a new version, while
every save wrote the whole history again: 124,493 of that copy's 125,431
version rows were such copies, and one maintenance cycle rewrote 29,379 of
them.

- Only what a result shows is reinforced. `mnemos_recall` and
  `mnemos_context` reconsolidate exactly the memories they return, after every
  filter, and link as co-activated only memories returned together. Finding a
  memory in order to forget it (`mnemos_correct` with a forget action)
  reinforces nothing.
- A memory is reinforced at most once per session: the Claude Code session
  (`CLAUDE_CODE_SESSION_ID`) when there is one, recorded in the store so every
  process of that session agrees; otherwise the server process. A second
  return in the same session changes nothing: no access count, timestamp,
  strength or link. This holds on every surface that reconsolidates through
  `ReactiveRetriever`.
- Maintenance never records an access. Reinforcing a lesson while softening
  no longer counts as reading it.
- A return writes no version. It updates the memory's access record and trace
  in place. A save writes a version only when it changes the memory's
  content, impact or resolution.
- Saving appends versions and never writes one again. A new version is
  numbered after the last one stored, not by its place in a list that may be
  partial: an engram found by text search carries no versions, and its
  snapshot used to overwrite version 1. On a copy of a real store, one
  maintenance cycle now writes no version rows (29,379 before) and took 3.12 s
  instead of 3.33 s (median of three runs each).
- `mnemos repair-versions` removes the copies returns left behind. A row goes
  only when a return wrote it and it repeats the row just before it in that
  memory's history; the first row of every run stays, and so does every row
  written for another reason (softening, its repair). It is a dry run unless
  `--write`, and reads the store read-only until then. With `--write`, a
  verified backup comes first (`backups/<db>.pre-repair-versions-<stamp>.db`),
  then the copies go in one transaction and nothing else changes. A second
  run finds nothing. On a copy of a real store: 124,321 of 125,431 rows go
  and 1,110 stay. The file keeps its size, since SQLite reuses the space; a
  `VACUUM` took that copy from 162.8 MB to 101.3 MB.
- A new table, `session_reinforcements`, records which session has reinforced
  which memory. The schema script creates it when a store opens. Nothing is
  migrated, so the schema version stays 11, and an older Mnemos ignores it.
- `MAINTENANCE_CODE_VERSION` is 3. Code older than the store reinforces
  nothing on any path, and records no session's reinforcement, so current
  code still makes the one that session is due.
- Every writable open of a store now records the code version, after its
  migrations and only ever raising it. Only a simple-mode server recorded it,
  when it started. The session-start hook, `mnemos search`, the prompt
  builder, the bridge, the advanced server and the shared pool open their
  own stores and reinforce by current rules, so a store they wrote could stay
  marked for older code, and a server running that code never stood down. If
  another process holds the write lock, the store still opens and the next
  opener records it. A read-only open (`mnemos doctor`) records nothing.

### Beliefs change only for real reasons

A belief is something the agent said yes to. Mnemos guessed instead: any
answer to a belief question formed a belief, a declined one included; the
answer's first letters decided the rest, so "Now more than ever" retired a
belief and "No, they don't contradict" recorded a contradiction; a "no" to a
contradiction question deleted every link from one memory to the other; and
without a model, a capture sharing one word with a belief and containing
"not" (which also matches "note") lowered it by 0.05 and linked the capture as
contradicting it. Nothing could raise a belief: a reaffirmation question could
never be asked, because the answered question that formed the belief held its
place in the queue.

- `mnemos_reflect` takes a `verdict`, and the verdict alone decides what
  happens. The words are kept as written and never read for a yes or a no.
  - "Is that a belief you hold?": `hold` forms it from the agent's words at
    0.4; `decline` forms nothing; `not_now` leaves the question open.
  - "Still true?": `hold` raises it by 0.05 (never past 0.99); `retire` sets
    it to 0 and stops it shaping context, keeping the belief; each is a
    revision entry carrying the agent's words. `decline` leaves it as it is;
    `not_now` leaves the question open. Nothing is deleted.
  - A contradiction: `contradicts` leaves exactly one CONTRADICTS link between
    the two memories and changes nothing else. It no longer lowers the
    earlier memory's strength: the verdict says the two conflict, not which
    one is wrong. `compatible` removes only a contradiction link from this
    memory to the other, and every other link stays; `unsure` changes
    nothing.
  - A lesson or what a memory changed: `answer`, or `skip` to close the
    question and leave the memory as it is.
- Without a verdict, a belief or contradiction answer is kept on its
  question, which stays open, and nothing is formed, retired or linked. The
  result says which verdicts the question takes, and both packets (the
  session's and the session-start hook's) show the call for such a question
  with `verdict="…"` and a line naming its verdicts. A lesson or impact answer
  without one is still its answer. Hosts calling `reflect` through the host
  mutation protocol pass `verdict` in its arguments to act on belief and
  contradiction questions.
- Without a model, a capture never lowers a belief and never writes a
  CONTRADICTS link. The model-configured path is unchanged.
- `mnemos repair keyword-contradictions` undoes what that check wrote, for
  one agent across its scopes. It is a dry run unless `--write`, and reads
  the store read-only until then. Its revisions are found by their reason
  ("Contradicted by new evidence: ", where the model path writes "... (impact
  0.60): ...") and their 0.05 step. Its links (CONTRADICTS, formed at
  encoding, strength 0.7, to the memory a belief rested on) have the model
  path's exact shape, so one counts only when its note shows it was saved
  without a model: a link formed by keyword overlap at encoding, or a
  revision of the check that names it. Links whose note a model weighed, and
  links nothing explains, are listed and left. With `--write`, after a
  verified backup (`backups/<db>.pre-repair-keyword-contradictions-<stamp>.db`),
  the check's links go and each active belief it lowered gets back what those
  revisions took, as a new revision that says so. No history is deleted, other
  revisions stand, retired beliefs keep their confidence, and a second run
  finds nothing. On a copy of a real store: 32 links from 16 notes and 4
  revisions; both beliefs 0.30 -> 0.40; 4 links of the shape left as
  ambiguous.
- A belief the agent has not formed or reaffirmed for 30 days may be asked
  about again ("Still true?"), at most once a month, as its own kind of
  question (`reaffirm`), under the packet's usual two-question cap. A theme
  the agent declined is not asked about again.
- Schema v11. SQLite cannot widen a CHECK in place, so opening a v10 store
  rebuilds its reflection queue with every row kept, after a verified backup
  (`backups/<db>.pre-v11-<stamp>.db`). The queue's unique index keeps its
  name, so an older Mnemos still opens the store.
- `MAINTENANCE_CODE_VERSION` is 2. Code older than the store answers only
  impact and lesson questions, as before. Every other question (belief,
  reaffirmation, contradiction, or a kind it does not know) stays open
  whatever the verdict, and the agent's words and verdict are kept as a
  signed note. It asks no reaffirmation.

### Old sessions stop maintaining a memory newer code has moved on from

A Claude Code session keeps the Mnemos code it imported when it started, and a
session can run for days. Every server started before an upgrade went on
maintaining the same store by the old rules (decay, linking, softening,
lessons, questions, beliefs, identity) after newer code had replaced them, and
nothing in the store could tell it to stop. `mnemos doctor` also wrote to the
store it was checking: it built a context packet, which ran maintenance and
used up the showings of pending questions. On a copy of a real store, one
doctor run logged a maintenance cycle, used two question showings and moved
the session counter.

- Each store records the newest maintenance code version that has opened it
  (`min_code_version` in its meta table). A server raises it when it starts
  and never lowers it.
- Code older than the store records the agent's words and applies no rules
  to them. It takes the agent's own captures, handoffs, reflections and
  corrections, because refusing those would lose memories, but:
  - it runs no maintenance, whether through a session or `mnemos consolidate`;
  - recall and the context packet still return what they find, but a return
    changes nothing: no access count, strength, version row or co-activation
    link;
  - a capture or a correction is saved in its own shape (classification,
    full-text index, vector) with no links to other memories and no weighing
    against beliefs. Maintenance under current code links it later: on a copy
    of a real store, one pass linked five such captures;
  - a correction lands on the memory it names, but never lowers or retires a
    belief, and gets no placeholder where its meaning would go;
  - questions wait for a current session. The packet shows none and spends
    no showings. Only an answer about what a memory changed or taught is
    taken: it lands on that memory, and filing it as a lesson waits for
    current code. Any other answer, to a belief or contradiction question or
    to a kind of question newer code added that this code has never heard
    of, is kept as a signed continuity note that names the question, which
    stays open;
  - a handoff replaces only its own session's note and retires no other
    session's.

  Old code never moves a belief by any path.
- A store's `schema_version`, like its `min_code_version`, only ever rises.
  Every open used to stamp the opener's own version, so older code lowered
  what newer code had recorded. Opening a store that is already up to date
  no longer changes the file at all.
- Every result it returns (capture, recall, context, reflect, correct, handoff,
  introduce) ends with one line: "This session runs older Mnemos code than
  the store expects. Restart the session." The agent inside a stale session
  is the only one who can see it. With current code nothing changes.
- `mnemos_health` and `mnemos doctor` show the running version and the
  store's minimum. When a session is older they say so, and what to do:
  restart the session, and if it still says so, update Mnemos or reset the
  minimum.
- `mnemos repair min-code-version` shows both versions and, with
  `--set N --write`, lowers or raises the minimum. It first makes a verified
  backup of the store exactly as it found it
  (`backups/<db>.pre-repair-min-code-version-<stamp>.db`). It refuses N below
  1, and only a human runs it.
- `mnemos doctor` now opens the store read-only and changes nothing. It no
  longer prints a context packet.

Servers started before this change have no gate: they keep maintaining by
their old rules until they are restarted. The gate protects every later bump.
Each change to how memory is written or maintained raises
`MAINTENANCE_CODE_VERSION` in `mnemos/code_version.py`, and from then on an
older server stops maintaining any store the newer code has opened.

### Parallel sessions keep their own handoffs

There was one handoff slot per scope, so when several sessions worked the same
scope at once, each session's handoff replaced whatever another session had
left, and the next session in either thread was handed the other thread's note
first. On one real store, 61 of 208 handoff replacements came from a different
session, and 18 of those replaced a note no session had read. Agents had
started writing combined handoffs to carry each other's threads.

- A handoff now belongs to the session that wrote it. Claude Code gives every
  MCP server `CLAUDE_CODE_SESSION_ID` and gives the SessionStart hook the same
  id, so nothing is asked of the agent or the human. A new handoff replaces
  only the note its own session left before; other sessions' notes stay.
  Clients that don't identify their session keep one shared note, as before.
- The packet hands a session its own note first (after compaction or a resume
  it gets its own thread back), otherwise the newest note, whole. Up to two
  other sessions' notes from the last three days follow as one short, signed
  line each, with the id to read one whole through `mnemos_recall`.
- A note from another session is framed as a colleague's even when the same
  model wrote it, and "the same model as you… Carry on from it" is said only of
  a note this session left.
- At most eight sessions' notes stay active per scope; beyond that the oldest
  is retired, with its prose kept in history.
- Handoffs no longer take continuity slots: the packet, `mnemos_context` and
  `mnemos_recall` leave them out of the ranked continuity search.
- Fixed: `mnemos_correct` with a `query` could land on the active handoff and
  overwrite its exact text with the correction, or forget it. A correction
  found by searching now never touches a handoff.

Schema v10 adds `hypomnema_entries.author_session` and lets the handoff index
hold one active note per session. The store upgrades itself on open, after a
verified backup (`backups/<db>.pre-v10-<stamp>.db`); on a 162 MB real store
that took two seconds. Existing notes keep an empty session and stay as they
are. An older Mnemos can still open a v10 store: the index keeps its name, and
while an unidentified note is active an older writer replaces that one rather
than a session's. A session opened before the upgrade runs the older code until
it restarts.

### Memories the scope migration hid

Schema v6 (0.2.1) gave every engram a person and a project. A legacy engram
linked to exactly one continuity scope was backfilled into it; every other one
was left unscoped and quarantined from scoped reads, so it could never be shown
to the wrong person. The quarantine is right and stays the default. What was
missing was any sign of it and any way out. Unscoped rows never reach recall,
never seed or carry spreading activation, and are skipped by scoped maintenance,
while every count reported only the scoped rows. On one real store the health
card read "186 active" over a file holding about 7,000 engrams, 105 of them
lessons distilled from experience.

- `mnemos_health` and `mnemos doctor` now say how many older memories never
  reach recall, split into lessons, other memories, and transcript-indexer
  output. Doctor raises ATTENTION only while lessons or other memories remain
  hidden; indexer output left out is the recommended state.
- `mnemos adopt-legacy` brings them back, and only a human runs it. It is a dry
  run unless `--write`, shows what would return, makes a verified backup
  (`backups/<db>.pre-adopt-legacy-<stamp>.db`) before anything moves, and
  adopts into the resolved scope. By default it brings back lessons and other
  memories, never archived rows, and never transcript-indexer output unless
  named with `--include indexer`. On a copy of the real store, adopting
  everything took all five recall slots for three of eight ordinary questions,
  while lessons and other memories changed two of sixty.
- It will not choose between people: when the agent holds memory for anyone
  other than the target person, it refuses until `--person-id` and
  `--project-scope` are given explicitly.

To recover an older store: `mnemos adopt-legacy --agent-id <agent>` to review,
then the same command with `--write`.

### Signed notes

One scope is often shared by several models. The same `claude-code` store is
written by whichever model the human is running that day, and every handoff
used to arrive as "From your previous session, in your own words", so each new
model inherited the previous one's first person and carried on as if it had
done that work. Every note is now signed with the model that wrote it.

- Schema v9 adds `hypomnema_entries.author_model`. It is added in place on open
  with one verified pre-migration backup; existing notes stay unsigned, and
  nothing is backfilled or guessed.
- Signatures come from `MNEMOS_AGENT_MODEL`, then `mnemos_introduce` in the
  current session, then the harness. Claude Code is detected from the session
  transcript (`CLAUDE_CODE_SESSION_ID`), reading only the tail of the file for
  the latest assistant turn's model id. Otherwise the note is unsigned.
- `mnemos_introduce` now signs the introducing session only. It used to be
  stored once per scope, so the last model to introduce itself stood for all.
- The handoff heading names its author: "Left by Fable 5.1 (claude-fable-5-1),
  18 hours ago." When the reader is known (a `model` field in the SessionStart
  payload, or detection in `mnemos_context`), the packet says whether it is the
  same model. An unsigned handoff says so and asks the reader not to assume it
  wrote it.
- Continuity notes show `by <model>`, `unsigned`, `co-formed`, or `Mnemos`.
  When a scope has notes from more than one model, the identity section says
  it is shared.
- Corrections re-sign the corrected note and record the prior signer. A
  reflection keeps the note's signature and names its own author inline.
- `mnemos_capture` and `mnemos_handoff` answer with the signature they wrote,
  or with how to sign when they could not tell.

Host adapters can now execute durable Core mutations through a versioned,
host-neutral exactly-once contract. An idempotency claim, every canonical
SQLite effect, and the serialized result commit atomically; a retry returns the
original result, while reuse of a key for a different request fails closed.

- Adds `MnemosRuntime.execute_host_mutation()` protocol v1 for `capture`,
  `correct`, `maintain`, `reflect`, and `introduce`.
- Adds the schema-v8 `host_mutations` replay ledger without changing the v7
  handoff model or migration behavior.
- Makes store commits transaction-aware so multi-table Core operations roll
  back completely after exceptions or process interruption.
- Treats embeddings as a rebuildable cache outside the canonical transaction;
  host mutation execution suppresses embedding writes until the transaction is
  complete.
- Adds replay, conflict, concurrent-delivery, upgrade, and failure-injection
  coverage, including crashes after each multi-table capture stage.

## 0.3.0 (2026-08-01)

Agent-written session handoffs. The agent can now leave one exact, private note
for its next session with `mnemos_handoff`. The newest handoff is delivered
first at startup, in full and only once in the packet, with its age and a quiet
instruction to continue naturally.

- One active handoff per agent/person/project scope, enforced in SQLite and
  replaced atomically while prior versions remain recoverable.
- Handoffs are never summarized, rewritten, promoted, decayed, or expired.
- Schema v7 records entry kind, authorship, author identity, last delivery, and
  delivery count. Ambiguous legacy writing remains `unknown`.
- Deterministic maintenance now uses neutral system language and is clearly
  separated from the agent's own words.
- `mnemos_health` reports handoff save/delivery/authorship state and warns
  when stored continuity is not reaching sessions.
- Claude Code startup injection remains supported. Codex now has an idempotent
  `mnemos hooks install codex --write` path covering startup, resume, clear,
  and post-compaction reinjection while preserving unrelated hooks.
- The installed-wheel audit now expects nine tools and performs a real
  session-A → handoff → fresh session-B transition.

## 0.2.1 (2026-08-01)

Production-hardening release. This release closes security, privacy, scope,
recovery, and installation gaps found during the 0.2.0 release audit. The
complete implementation and verification checklist lives in
`docs/production-hardening-0.2.1.md`.

## 0.2.0 (2026-07-31)

Two things happen in this release. Continuity starts arriving on its own —
loaded at session start and captured as work happens, with nobody asking for
it. And memory stops being a notebook the agent writes to and becomes something
the agent *maintains*, in its own voice, on an install with no API key.

The first half is plumbing that was missing: scope that matches between the
write and the read, a session-start hook, background maintenance, and an
honest signal for when memory comes back empty. The second half is the one the
project was named for — the five shifts that were supposed to make this a mind
rather than a log, four of which did not actually run until now.

**Start here if you are upgrading:** two breaking changes are listed under
*Changed* — the default database path and the distribution name. Existing
stores migrate in place.

### What Mnemos is
Clarified throughout, because the previous framing caused a real failure. Mnemos
is a continuity and identity layer for the agent itself — it carries what the
agent should know about you and how you work together, across sessions. It is
**not** a general memory or retrieval system, and it runs alongside whatever you
already use for that. Pointing general recall at it buries the continuity layer:
on one live install the transcript indexer had written ~7,058 engrams against 13
deliberate captures, and a session packet spent five of six long-term slots on
paraphrases of a single harvested fact.

### A memory that maintains itself
Mnemos's design came from a session that asked one model what it would want if
the memory were its own to inhabit. Five shifts came out of that, and the
explainer has always claimed them — *"most AI memory is a notebook; this is a
mind."* Measured against a real store, four of the five did not run. Each one
needed a model, and the install the README advertises has none.

They run now, because the direction inverted: **the server never calls a model.
It asks the agent.** Maintenance proposes the work that needs judgement, and the
agent answers in its own turn, in its own words — that answer becomes part of
its memory. Consolidation stopped being something done *to* the agent and became
something it does. This works in every MCP client, needs no key, and made the
whole affinity system that used to police which outside model was allowed to
maintain an agent's memory unnecessary — that question is answered by
construction now (201 lines removed).

- **`mnemos_reflect`** — the eighth simple tool, and the only one whose entire
  job is to let the agent's own voice into its own memory. The context packet
  may quietly raise a question — what a capture changed, what a fading memory
  taught — and the agent answers it here. Nothing is written on its behalf; if
  no true answer comes, the prompt fades on its own. Restraint is enforced, not
  hoped for: at most two requests per packet, each shown at most three times
  then dropped, quiet scopes show nothing.
- **Traces, not records** — a capture can carry an `impact`: not what happened,
  but what it changed. That sentence is what survives when the details fade, and
  only the agent can write it, so the server never fills it with a template. An
  empty impact is left empty and asked about later.
- **Forgetting that teaches** — as a memory fades, the lesson in it is distilled
  and kept while the detail softens. With no provider the words are left intact
  and the fade lives in ranking, never a rewrite the store cannot undo.
- **Surprise as growth** — a capture that does not fit what is already held is
  encoded more deeply. This no longer depends on pre-existing beliefs or a
  model, so it is no longer skipped.
- **Resonance over search** and **identity from the graph** — retrieval spreads
  through typed connections, and identity is measured from what the agent keeps
  returning to rather than narrated. Honest limit worth stating: without a
  configured provider, edges the encoder cannot judge are labelled
  `co_activated` — *these came up together* — rather than guessed into a
  semantic type. The graph is honestly labelled, not richly typed; semantic
  relations still need semantic judgement, which is the one thing a provider now
  buys.

### Proof
- **The continuity assay** — spawns real subprocesses and passes no scope
  arguments, because defaults are what an agent actually gets. Covers a capture
  surviving into a later session, a capture written in one directory being
  readable from another, earlier captures not being displaced, reading never
  creating a store, and a corrupt store never failing a session. Reintroducing
  the cwd-derived scope makes it fail; the fix makes it pass
- **Health that reports absence** — `mnemos_health` and `mnemos doctor` now
  report notes held, empty-packet streak, and sessions since the last capture,
  and say plainly when memory is being read but coming back empty. Every failure
  this system has had looked like success from the outside; this is the signal
  none of them could fake
- **Retrieval mode is legible** — `doctor` states whether recall is semantic or
  keyword-only instead of leaving it to chance

### Background Maintenance Without OpenClaw
- `mnemos daemon {install,status,uninstall}` — schedules maintenance with whatever the host provides: launchd on macOS, systemd user timers on Linux, crontab as a fallback. Background continuity previously existed only as OpenClaw cron templates, so every user without OpenClaw had a memory system that did nothing between sessions. The job logic was never OpenClaw-specific — `mnemos consolidate`, `mnemos substrate-tick` and `mnemos index` are plain CLI commands, and only the scheduling was bound to OpenClaw
- Jobs are namespaced per agent, so several agents keep separate maintenance on one machine. Reinstall replaces rather than stacks, and the crontab backend only ever removes lines it wrote
- The model-mediated `index` job is omitted unless a provider is configured, rather than waking every 30 minutes to do nothing
- Nothing is scheduled without `--write`; `mnemos doctor` reports whether background maintenance is active
- On macOS, warns when Mnemos is installed under `~/Documents`, `~/Desktop` or `~/Downloads` — scheduled jobs do not inherit Full Disk Access, so they fail with a permission error even though the same command works by hand
- OpenClaw is now documented as one optional integration for agent-mediated jobs (observer sync, `MEMORY.md` upkeep, briefs) rather than as the way background work happens

### Continuity Without Manual Triggering
- Server instructions — both MCP surfaces now ship `instructions=` to the client, so an agent is told to load context at session start, capture durable things as they appear, and correct rather than contradict. Previously nothing instructed the agent to use memory at all; continuity only happened when a human asked for it
- `mnemos hooks install [--write]` — registers a Claude Code `SessionStart` hook that injects the continuity packet before the first turn. Preserves unrelated hooks and settings keys, replaces its own entry on reinstall rather than stacking, and refuses to overwrite an unparseable settings file
- `mnemos hook session-start` — the subcommand the hook runs. Injection logic ships with the package instead of going stale inside a generated script. Fails silent by design: any error, or a missing or corrupt store, exits 0 with no output
- Reading memory no longer creates a database as a side effect — a mistyped `--db-path` used to mint an empty store at every session start and then report a healthy, permanently empty packet

### Fixed
- **Scope split-brain.** The simple tools resolved scope through `resolve_scope` while the advanced tools took their literal parameter defaults (`default`/`user`/`global`). `mnemos_capture` wrote continuity into one partition and `mnemos_context_packet` read from another, both reporting success, so an agent silently had no memory. All 13 scoped tools now resolve through one shared resolver
- **`project_scope` no longer derives from the process working directory.** An MCP server's cwd is chosen by whichever client spawned it, so a cwd-derived scope partitioned one agent's memory by launch location. It now defaults to `global`; explicit arguments, `MNEMOS_PROJECT_SCOPE` and config still win
- **The CLI, simple mode, and advanced mode used three different combinations of agent id and database.** `mnemos stats` and `mnemos serve` reported on different stores. All entry points now share one resolver
- `ConsolidationDaemon(config={})` in the `mnemos_consolidate` tool, the CLI `consolidate` command, and `bridge.py` silently dropped the entire `consolidation` block of `config.json` — decay rate, thresholds, and `min_idle_minutes` all fell back to hardcoded defaults
- A function-local `Belief` import left the name undefined for the second seed belief in setup step 5; the resulting `NameError` was swallowed and the belief silently dropped
- Duplicated `foundational, foundational` label in the context packet
- **`pip install` produced a dead server.** The `mcp[cli]` dependency was declared `>=1.0.0` with no ceiling, so a fresh install resolved mcp 2.0 — which removed `mcp.server.fastmcp`, the module every server entrypoint imports — and the server died on import. CI never caught it because it installs from the pinned lockfile. Bounded to `<2`, with a wheel smoke job that installs unlocked from PyPI, plus a unit test guarding the declared ceiling
- **`forget` did not forget.** A successful `mnemos_correct(action="forget")` archived the memory and left `recall` silent — and the text was still read back to the agent, on both delivery paths: the session-start hook replayed it for three sessions from a frozen queue excerpt, and `mnemos_context` quoted it in the verification block with an instruction to say it aloud. Two snapshots taken at write time that no deletion reached. The rule now: no packet block renders a frozen copy of note text; every block re-reads by id and skips a memory that is gone. Older stores are cleaned during maintenance
- **A reflection did not reach the agent.** `mnemos_reflect` wrote the answer only to the engram, and the session packet is built from the continuity layer, which excludes the engram graph by default — so the one sentence the whole inversion exists to obtain was unreachable from the automatic path. It now lands in the continuity note as well; answering twice revises rather than stacks
- **Softening could erase what it could not read.** With no provider, the fade step truncated a memory to "An impression related to X... [faded]" and left the impact empty, with no way back. The default now leaves the words intact and lets the fade live in ranking; `mnemos repair-softening` (and a `mnemos doctor` prompt) restore memories an earlier version truncated, from each memory's own pre-fade snapshot
- **Identity reported its own bookkeeping.** "Persistent concerns" counted every tag, including the classifier and indexer labels Mnemos stamps on nearly every memory, so an agent read back `trace-type:fact, session-indexed, decision` as who it was. Those are excluded now; when nothing meaningful remains, the line is omitted rather than fabricated

### Changed
- **BREAKING:** advanced mode and the CLI now default to `~/.mnemos/<agent>.db` rather than `~/.mnemos/memory.db`, matching what simple mode already did. Pass `--db-path` or set `store.db_path` in `config.json` to keep reading an existing store
- Distribution renamed from `mnemos-memory` to `mnemos-continuity`. `mnemos-memory` is a different author's package on PyPI, and the published install instructions pointed users at it. The import package and CLI command are unchanged
- Engrams now record `impact_source` — who wrote the trace: `agent` (via `mnemos_reflect`/`mnemos_capture`), `model` (extracted by a configured provider), or `template` (server boilerplate). The product's claim is that only the agent can say what a memory changed, and this makes an agent-authored impact distinguishable from a generated one instead of something later reconstructed from a boilerplate denylist. Schema version 3 → 4; existing stores migrate in place and their prior impacts read as unknown, never back-filled with a guess

### Simple Mode (five tools → eight)
- Onboarding ritual — a fresh scope's first context packet walks the agent through a short get-to-know-you script (name, current work, durable facts); stores that predate onboarding are grandfathered and never see it
- mnemos_introduce — the agent declares its own model id and name, so its memory knows whose it is (an explicit MNEMOS_AGENT_MODEL still takes precedence)
- mnemos_reflect — the agent answers, in its own words, a reflection the packet raised; see *A memory that maintains itself* above
- Cross-session memory verification — the first context packet after a real restart quotes the very first capture back to the human, once, as proof that memory survived the goodbye. It re-reads that memory live, so a capture the human later forgot is never resurfaced
- Dream journal — consolidation cycles that did meaningful work leave a short first-person narrative, surfaced in the next context packet ("While you were away")
- mnemos_health — truly read-only, human-relayable health card: store location and size, memory counts, last maintenance cycle and who performed it, onboarding and verification progress, latest dream entry, and whether any memories are recoverable from an earlier version's truncation

## 0.1.0 (2026-04-05)

Initial release.

### Core Memory Engine
- Engram model with dual-trace (strength/stability/accessibility)
- 7 typed connections (supports, contradicts, causes, extends, parallels, synthesizes, grounds)
- Beliefs with confidence tracking, revision history, epistemic bounds [0.05, 0.95]
- 6-dimensional emotional state (curiosity, clarity, warmth, tension, surprise, focus)
- Graph-based identity computation
- SQLite backend with FTS5 full-text search and WAL mode

### MCP Server (9 tools)
- mnemos_setup — 10-step conversational onboarding wizard
- mnemos_remember — encode memories with impact, confidence, connection discovery
- mnemos_recall — spreading activation retrieval with emotional biasing
- mnemos_inspect — full engram details with version history
- mnemos_status — system health and statistics
- mnemos_beliefs — list beliefs with confidence and revision count
- mnemos_shared — query shared memory pool
- mnemos_forget — graceful archiving (soft delete)
- mnemos_consolidate — trigger decay, connection discovery, softening, belief review, reflection

### Cognitive Substrate
- Background tick loop (configurable interval, default 4h)
- 6 handlers: dreaming, wandering, surprise, reflection, insight, initiation
- Cognitive modulators (arousal, resolution, openness, selection_threshold, social_drive)
- Production guardrails: skip_surprise_detection on all handler outputs except surprise, per-handler throttles, confidence change caps

### CLI
- mnemos init, serve, stats, search, inspect, consolidate, export
- mnemos substrate-tick, index, bridge {status|recall|remember}
- mnemos setup-openclaw

### Multi-Agent
- Shared memory pool with visibility controls
- Agent relationship tracking with trust scores
- Per-agent isolation with optional cross-pollination

### Embedding Support
- Google Gemini embeddings (3072 dims)
- Local sentence-transformers fallback (384 dims)
- Graceful degradation to FTS5-only when no embedding backend available
