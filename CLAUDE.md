# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
docker compose up -d           # Postgres 18 + pgvector on localhost:5433
uv sync
uv run pytest                  # full suite
uv run pytest tests/test_extraction_service.py::test_name   # one test
uv run pytest -m 'not db'      # skip everything that needs Postgres
uv tool install --editable .   # puts `bag` on PATH (see below)
bag db up                    # create the database and run migrations
bag db status                # applied vs pending migrations
```

DB-backed tests `pytest.skip` with an explanatory message when Postgres is unreachable - a
green run does not mean the DB tests ran. Check the skip count.

`uv tool install --editable .` is effectively mandatory for any work touching the Claude
Code integration: the MCP server and both hooks are registered as bare `bag`, and a
`bag` that only exists in the project venv produces a config that silently does nothing
(hooks are fail-soft, the MCP server never starts).

## `bag serve --http`

`bag serve` is stdio by default: the agent starts the process, so the
process's working directory is the agent's and `_default_project()` resolves
correctly from it.

`--http` serves the same seven tools over streamable-HTTP, for an agent that
cannot start a local process - a container, or a remote host. Two things
change, both in `mcp_server.py`:

- The project is pinned by `--project` instead of derived from the working
  directory. Without it, every write files under whatever directory the server
  was launched in, succeeds, and never appears in a knowledge base again.
- The session id is null. `CLAUDE_SESSION_ID` in the server's environment
  belongs to whatever launched it, not to the agent calling the tool.

The listener is stateless and allowlists exactly the `host:port` it bound.
That allowlist is load-bearing: the SDK enables DNS-rebinding protection by
default, so a non-loopback bind without it refuses every request.

There is no authentication. Do not bind this to an address you do not control
the network of. saddle's use binds it to a per-session `--internal` network's
gateway, which has no internet route.

## Architecture

Strict layering, and the seams are deliberate. Each layer may call downward only:

```
frontends:  cli.py (Typer)  mcp_server.py (FastMCP)  agents/claude_code/hook.py
services:   services/{write,search,kb,record,extraction}.py <- every policy decision lives here
store:      store.py (Protocol)  -> backends/postgres/store.py (all SQL)
domain:     domain.py (pure dataclasses/enums, no I/O)
```

- **Frontends parse and format; they never decide.** A rule enforced in a service is one
  every frontend gets for free (query clamping, the search tier chain, record opt-in checks). If
  you find yourself adding a policy branch in `cli.py`, it belongs in `services/`.
- **`backends/postgres/sqltext.as_sql()` is the only place SQL text is
  asserted to be SQL.** psycopg types `execute`'s query as `LiteralString`
  so a string built from user data can never arrive as SQL; this backend
  does build queries with f-strings, but every interpolation is one of its
  own constants (`entry_columns()`, a `where` fragment) and every value
  goes through a parameter. `as_sql` is a named function rather than an
  inline cast so the exemption is greppable - interpolating anything that
  is not a module constant means calling it, deliberately. It lives in its
  own module because `store.py` and `migrate.py` both need it and two
  copies would drift.
- **`store.py` is the portability seam** - a `Protocol`, with Postgres as the only
  implementation. Ownership is enforced *inside* the store (`NotOwner`), not by callers.
- **`session.open_session()` is the only way to reach the database.** It connects,
  ensures the principal, and hands back a `Session`. It does **not** run migrations -
  `bag db up` is the only thing that applies them, and `bag db status` names what is
  pending. A schema behind the code therefore presents as a raw `UndefinedTable` from an
  ordinary command rather than as anything self-healing, so check `db status` before
  concluding a new feature is broken. Nothing outside
  `session.py`/`backends/` should import psycopg. `autocommit=True` is for long-running
  work that records its own progress (`bag events process`) - in a single transaction a failed
  statement poisons the connection and the final COMMIT becomes a ROLLBACK.
- **`agents/base.py` + `agents/registry.py` are the pluggability seam** - adapters register
  under the `saddlebag.agents` entry point group and load lazily; a broken third-party adapter
  warns rather than breaking saddlebag. Two adapters ship today: `claude-code` (an MCP
  registration plus four hook entries in settings the adapter owns and merges) and
  `opencode` (one generated file, `plugin.js`, dropped into a directory opencode scans).
  The contrast is deliberate - the second adapter proves the seam by looking nothing like
  the first.

### Data model

`Entry` (kind: note/doc/rule, origin: human/agent/extracted/handoff) is the unit of knowledge.
`Collection` ("knowledge base") membership is two things unioned: a smart `CollectionQuery`
(project + tags + kinds) plus explicitly pinned entries. An empty query matches *nothing*,
forever - `kb.advisories()` exists to say so at creation time.

Search is three tiers - exact full-text (`tsvector` generated column, weighted
title/body/tags), then semantic (pgvector cosine distance over `entry_vectors`), then
trigram similarity - and each runs **only when the one above returned nothing**, never
blended. Semantic sits above trigram because a query that matches nothing lexically is
far more often a different wording than a typo.

Measured 2026-09-15 against a 165-question instrument, and the numbers moved this
reasoning rather than confirming it. The tier ORDER is a **cost** decision, not a
ranking-quality one: the full cascade scores 59.4% hit@1 against semantic-only at
58.2% - discordant 8-6, p=0.79, indistinguishable. What exact-first actually buys is
that 47.3% of queries are answered before an embedder exists at all, and constructing
one imports fastembed, builds an ONNX session and can download ~130MB. Against
exact-only the chain is unambiguous, 41-0, p<0.0001. So do not defend the order on
the grounds that it ranks better, and do not reorder it on the grounds that it does
not - the argument is what the middle tier costs to reach.

Never blending survives the measurement, but on the other of its two justifications.
Reciprocal-rank fusion of exact and semantic scores 60.6% against the cascade's 59.4%
- discordant 3-1, p=0.625, inert rather than harmful. Blending is therefore not
declined because it trades precision away; it is declined because a fused result set
makes `Hit.match` unanswerable and the rankings behind it are not comparable, which is
the paragraph below.

On this corpus the trigram tier never fires at all - zero answers out of 165 - and
forced to run alone it is statistically indistinguishable from the exact tier (36.4%
against 34.5%, discordant 25-22, p=0.77). Neither figure argues for moving it up: the
instrument's questions are all well spelled, and typos are the tier's entire job, so
its real workload is unmeasured here. Both are facts about this corpus on this date,
not about the design.

Every hit carries `Hit.match` (`Match.EXACT`/`SEMANTIC`/`FUZZY`) and every frontend must
surface it - `~`/`?` markers in the CLI, `"match"` in `--json` and in MCP `recall`. An
agent handed an unmarked approximate match cites it as certain. There is no compatibility
shim for the `Hit.fuzzy` boolean this replaced, and a test asserts its absence.

The embedder is an **optional dependency** (the `[embed]` extra) and vectors are written
only by `bag embed`. Missing either one costs the middle tier and nothing else: search
degrades to the two tiers it always had, silently and exiting 0. `bag embed` is the
opposite - fail-loud - because an unavailable embedder is its entire job failing.
`services/search.shared_embedder()` owns both halves of that policy, memoises the
embedder per model name, and is called **from inside the semantic tier**: constructing a
`LocalEmbedder` imports fastembed, builds an ONNX session and can download ~130MB, and a
search the exact tier answers must never pay for any of it. Frontends pass `embed_model`,
never an embedder.

Queries and entries are embedded **asymmetrically**. `embed.QUERY_PREFIXES` gives a
query the retrieval instruction its model was trained with; entries are always embedded
bare, so adding a model to that table never makes a stored vector stale. Anything that
embeds a search query - the semantic tier, `scripts-eval-retrieval.py` - goes through
`embed.query_text`, or it measures a convention search does not use.

The default stays `bge-small`, and that was decided on measurement (2026-09-16, the
165-question instrument, paired through `find`). The prefix alone took hit@1 from 58.8%
to 61.2%. `bge-base` reached 64.2% and `bge-large` 66.7% - the only one significantly
ahead of the unprefixed baseline (17-4, p=0.007), but 9-5 against `bge-base` and 14-5
against prefixed `bge-small`, neither significant - while a semantic-tier search went
from 0.36s to 0.41s and 0.79s, and the download from 0.067GB to 0.21GB and 1.2GB. hit@5
was flat across all of them: bigger models reorder the top of the list, they do not find
more. `BAG_EMBED_MODEL` switches models per machine; the 0.55 floor needed no retune for
either. Chunking long bodies and title-only vectors were tried and did not help, so the
paraphrase gap is the model's, not the entry text's.

### Deduplication

`bag dedupe report` names entries that say the same thing twice. Two
tiers, and unlike search they **both always run and are never blended**: a
report asks what duplication exists, so suppressing one tier because the
other found something would hide most of the answer. A pair already reported
as an identical body is not reported again with a score.

Exact duplicates are **groups** (identical checksums are transitive); near
duplicates are **pairs, never chained** - similarity is not transitive, and a
union-find over the pairs would produce mega-groups nobody could defend.

The checksum is `md5(btrim(body, E' \t\n\r'))`: the body **alone**, because
two entries holding one fact under different titles are duplicates and the
extractor's title is never a human's. That is deliberately the opposite of
`ingest`, which compares title and body - it asks whether a chunk needs
re-indexing, and there the title is half the embedding text. The trim
character set is spelled out because one-argument `btrim` strips spaces only,
and the trailing-newline case is the one it exists for. There is no stored
hash and no migration.

The near tier has its **own** threshold (0.95), not
`config.semantic_threshold`, which is a search-recall floor: sharing the knob
would mean tuning recall silently retunes what counts as a duplicate. It
never constructs an embedder - a report must not download ~130MB or fail on a
missing optional extra - so coverage is partial by default and
*embedded / total* is **always** printed. Zero embedded renders as "not
checked", never as an empty section, the same rule `bag doctor` follows;
`--json` says so as `near_checked`. `report` exits 0 even when it finds
duplicates: an exit code that is non-zero on every run is one people learn to
ignore. `--limit` bounds the near tier only. Suppression and truncation are
reported as **separate sentences**: a single "showing N of M" covering both
told a reader whose list had merely been deduplicated that their output was
cut short, which is the first thing running this command against a real
store caught.

Nothing is ever merged automatically. `bag dedupe resolve <drop> --keep
<keep>` is the one write, fail-loud, and it exists because `supersede` cannot
express this: `supersede` requires a title and mints a **new** entry for
knowledge that stopped being true, while here both entries exist and one
should point at the other. That primitive is `store.set_superseded`, which
the ingest orphan sweep already calls directly for the same reason. It
refuses five ways: the same id twice, an unknown `drop`, an unknown `keep`,
an already-superseded `drop`, and a `keep` that is itself superseded.
Cross-owner is refused inside the store as `NotOwner`.

Resolving an entry that carries a `mem:<name>` tag drops it from its
collection, so the next `bag memory sync` will want to delete that file.
That is correct, and the sync's checksum gate still protects a copy edited by
hand.

### Migrations

Numbered `.sql` files in `backends/postgres/migrations/`, applied in filename order and
tracked in `schema_migrations`. Add a new numbered file; never edit an applied one.
`migrate()` runs inside the *caller's* transaction - the caller owns commit/rollback.
`applied_versions()` is deliberately read-only (inspecting a DB must not write to it).

Timestamps use `clock_timestamp()`, not `now()`: tests run inside one rolled-back
transaction, where `now()` gives every row an identical `created_at` and makes
`order by created_at desc` non-deterministic.

### Events and extraction

Raw per-tool-call events, recorded from a harness and extracted into entries later.
Supersedes capture - `bag capture enable|disable|status|drain` still work as hidden
aliases that warn once and delegate, for muscle memory and shell history, but the
real commands are `bag record` and `bag events`.

Recording is opt-in per project - that gate is the entire safety story, and it is
checked in `services/record.py`. Events are stored **in full** and kept
**indefinitely**; nothing prunes them on a schedule. `bag events prune --before`
is the only thing that ever deletes one, and only on request.

Flow: a harness hook (or plugin) does exactly one INSERT via `bag record event`
(everything fragile is deferred), and `bag events process` - run from cron, or
spawned by `hookio.spawn_process` at any harness's session start - extracts
entries from sessions that have gone quiet, via
`claude -p` in `extract/claude_cli.py`, writing entries with `origin='extracted'`
and `entry_events` provenance rows.

Extraction is triggered by **idleness**, not a session-end hook: a session is
extractable once it has unextracted events and none newer than
`BAG_IDLE_MINUTES` (default 20). Two of the three harnesses saddlebag targets have
no end-of-session hook, so a clock is the only trigger all of them share; a hook
that never fires would strand a session forever, while a clock always ticks. The
attempt-cap "gave up" rule lives in exactly one place, `extraction.awaiting_sessions`
- both `bag events process` and `bag record status` route through it, so they
cannot disagree about which sessions are stuck.

Invariants worth not breaking:
- `entry_events.event_id` has **no foreign key**, deliberately, and no read path may
  dereference it: pruning must be able to delete an event out from under its
  provenance row without touching `entries`, leaving the row visibly dangling rather
  than blocked or cascading. `covers_through` (not `entry_events`) is what makes
  "already extracted" answerable **per event** rather than per session.
- Extracted entries are **excluded from knowledge base context blocks** (`kb.resolve`
  filters `Origin.EXTRACTED`) so machine text never crowds out hand-written rules.
  They do appear in `search`/`recall`. Promote one with `bag kb pin`.
  `search.DEFAULT_ORIGINS` must gain any future origin or that origin silently
  vanishes from search.
- `extract/base.py` treats all model output as untrusted: shape-checked, capped
  (`MAX_ENTRIES`/`MAX_TITLE`/`MAX_BODY`), filtered before it reaches the store.
- The renderer spends its budget on **coverage before detail**. `render_events`
  hoists payload keys that are constant across the batch into one note, caps
  every oversized value (`MAX_FIELD_BYTES`, then `TIGHT_FIELD_BYTES`), and only
  then drops whole events off the front. Measured: 40KB of tail (18 of 112
  events) returned nothing in three runs, where the whole session with values
  capped returned entries in five of five. Both the hoist and the cap are keyed
  on the batch and on value size, never on a table of key names - the module
  renders Cursor and opencode events too, and their constants are different
  keys. A cut `tool_response` still says what the tool did; a dropped event
  says nothing.
- The extraction model is **pinned** (`BAG_EXTRACT_MODEL`, default `sonnet`), not
  inherited from the session, so cost/behaviour do not drift. Haiku was measured and
  rejected on judgment, not JSON validity. `BAG_CAPTURE_MODEL` is read for one
  release and warns to stderr naming the replacement - never both silently.
- `CHILD_ENV_VAR` (`BAG_EXTRACT_CHILD`) is set on the spawned `claude -p` so its own
  hooks refuse to recurse. Three hooks check it. **Rename it on every side in the
  same commit or not at all** - renaming one side leaves the extractor's own child
  recording events, which the next extraction reads, without bound.
- Jobs stop retrying after `MAX_ATTEMPTS`; `bag events process --job ID` retries by
  id. Failures record the reason *and* the model's raw output, both separately
  truncated.
- `install()` performs a live database round-trip (it proves the record/extract
  path actually works), which is why the install tests are marked `db`.

### Checking the install

`bag doctor` answers the one question a fail-soft pipeline cannot ask
itself: **does the installed config actually register the hooks this adapter
installs?** It reads files and opens no database - a diagnostic that needs
the system healthy is no use when it is not.

Each adapter answers with facts (`hook_state()`, a probed optional
capability returning `HookState`); `services/doctor.py` makes every
judgement, so all adapters agree on what "missing" means and one
computation feeds `bag doctor`, its `--json`, and one advisory line in
`bag record status`.

Three rules worth not breaking:

- **One table per adapter.** `HOOK_ENTRIES` is read by both `install()` and
  `hook_state()`. Two tables kept in step would drift, and drift is the
  whole bug: settings.json held three hooks for the life of the events
  pipeline and nothing could see it.
- **Unchecked never renders as `ok`.** opencode ships a plugin file rather
  than hook configuration, so it does not implement `hook_state()` and the
  report says "no hook registration to check". Reporting success for
  something never verified is the failure this command exists to catch.
- **Only a missing *required* hook exits non-zero.** `STALE` and
  `DUPLICATED` still fire the hook; `UNCHECKED` reports the absence of a
  check. Exiting non-zero for "I could not tell" trains people to ignore
  the exit code.
- **No confident answer about a scope that was not examined.** With no
  `--scope`, `check()` sweeps `SCOPES` (saddlebag's own constant - the
  vocabulary is closed and every adapter hardcodes it; `UnsupportedScope`
  is the skip signal) and reports one row per *(adapter, scope) that is
  installed*, so a half-install in one scope cannot hide behind a healthy
  other. "Not installed" is said once per adapter and **names every path it
  looked at**. With `--scope X` the question is exactly X, and
  `UnsupportedScope` stays `UNCHECKED`-with-a-warning rather than becoming
  a skip - sweeping there would make `bag doctor claude-code --scope
  project` print nothing and exit 0. This is not hypothetical tidiness:
  defaulting to user scope made `bag doctor` report cursor "not
  installed" on the machine where cursor was installed at project scope and
  recording events. Every pointer carries its scope too - the advisory line
  and the `Fix:` line both - because a pointer that leads to a
  contradictory screen teaches the user the line lies.

`doctor` and `verify` are a pair and neither subsumes the other: doctor asks
whether the harness will ever call saddlebag, `verify` asks whether saddlebag works
when called.

### What the context block carries

Rules render as **title plus summary**, never their bodies. Rule bodies here
are essays - the incident that produced the rule, the reasoning, the lesson -
and shipping all of them is what made the block outgrow `BAG_MAX_CHARS`
twice in three days (18,249 chars on 2026-09-02, 25,008 by 2026-09-04). The
body stays one `recall` away and the rendered `_id:` line is how to reach it.

A rule with no summary renders **title only**. That is the floor, not a
fallback to the body: rules written before the requirement must keep
rendering, and rendering their bodies is the failure being fixed. Backfill one
with `bag update --summary`, which edits in place - `supersede` would mint a
replacement and churn the memory file whose frontmatter `description` this
same field feeds.

Deriving the short form from the body was measured and rejected: a rule's
first paragraph is the incident, not the instruction.

Backfilling a summary with `bag update --summary` does not regenerate the
entry's Claude Code memory file, because the sync watermark hashes the body
alone - the new description appears on disk the next time that entry's body
changes.

`services/write.remember` raises `RuleNeedsSummary` for a rule without one.
That check is in the service and not in `cli.py` because `mcp_server`'s
`remember_tool` takes a `kind` and would otherwise write a summary-less rule
straight past a frontend check.

The check is gated on `origin in INJECTED_ORIGINS`, not on `Kind.RULE` alone,
because `kb.resolve` filters blocks to those same origins - an `EXTRACTED` rule
can never render in one, and requiring a summary on it would break extraction
for no gain. One deliberate exception survives that reasoning: `kb.resolve`
applies `INJECTED_ORIGINS` only to the **query** half, and `store.pinned_entries`
has no origin filter by design (pinning is the documented way to promote a
machine-written entry), so `bag kb pin` on an `EXTRACTED` rule does put a
summary-less rule into a block, rendering as a bare title. Parked rather than
fixed: it is the same floor the renderer already guarantees, it takes a human
pin, and `bag update --summary` fixes it for any origin.

The never-truncate invariant is unchanged: `RulesExceedBudget` still raises
rather than shipping a partial rule set, because an agent given part of the
conventions proceeds believing it has all of them. Short forms make that
exception rare; they do not soften it.

### Handoffs

A handoff is an `Entry` with `origin='handoff'`, `kind=doc`, and a `topic:<slug>`
tag - no separate table. Invariants:

- Writing one supersedes the prior live handoff for the same `(project, topic)`.
  Only the newest is ever live; the chain is the history.
- Excluded from context blocks (`kb.resolve` filters origins) and from search
  unless `include_handoffs=True`. `search.DEFAULT_ORIGINS` must gain any future
  origin or that origin silently vanishes from search.
- `bag handoff write` is fail-loud, unlike every hook in this repo: the user
  is about to `/clear`.
- `session_size.py` is Postgres-free and agent-neutral; it runs on every user
  prompt via the `UserPromptSubmit` hook. Warn state lives in the platform
  cache dir and fails toward warning, never toward silence.

### Ingested documents

`bag ingest <path>` loads markdown in as one entry per `h1`-`h3` heading,
plus an anchor entry per file. Identity is two tags, `src:<path>` and
`sec:<slug>`, so re-ingest is idempotent: unchanged sections are skipped
without a write, edited ones supersede their previous version, and sections
that vanished from the file are superseded **by that file's anchor** -
`set_superseded` needs a replacement id and a deleted heading has none. The
sweep calls `store.set_superseded` directly rather than `write.supersede`,
which would create a replacement the orphan does not have.

Splitting is on headings and only on headings. A size-based sub-splitter
would cut through fenced code, which is most of what a plan contains. The
only fence logic in `markdown.py` is a boolean for heading detection, so a
`#` comment inside a code block is not mistaken for a section.

Chunk titles come from the document's opening **`h1`**, falling back to the
filename stem when a file has none - "Ingest design § Decisions" rather than
"2026-09-01-doc-ingest-design § Decisions". Only an h1 that opens the file
counts; one further down is an ordinary section, since taking the title from it
would rename the document halfway through. The stem still IDENTIFIES the
document - a headingless file's `sec:` slug is its stem - so `split` takes
`doc_name` and separates naming from identity the same way `ingest_file`'s
`root` separates where a file is read from what identifies it. Retitling a
document therefore never duplicates its chunks.

Because the title is half the embedding text and the highest-weighted field in
the tsvector, "changed" compares **title and body**, not body alone. A renamed
document supersedes every one of its chunks on the next ingest; comparing
bodies alone would leave the old titles standing until each section's prose
happened to change.

Retrieval quality for ingested content is measured at the **document**, not the
chunk. A 165-question eval on 2026-09-15 scored ingested chunks at 43.6% hit@1
against the exact gold section and 69.1% against the right document - the same
as hand-written entries (69.0%). Almost all of the apparent deficit is
right-document/wrong-section: 28 of 31 misses lost to another ingested chunk,
14 of those to a sibling section of the same file. The tempting explanation -
that a `Doc § Section` title dilutes the highest-weighted tsvector field - was
tested and rejected: 12 of the 15 chunks that never surfaced rank first when
queried with their own title verbatim. Do not retune titles or field weights on
the strength of the chunk-level number. What dominates those misses is a
paraphrase penalty that is corpus-wide (+30 to +50 points against keyword
queries in **every** origin), not an ingest trait.

Two origins, because `search.DEFAULT_ORIGINS` is an allowlist and an
exclude filter was deliberately declined: `INGESTED` (specs, notes,
decisions) is in that list, `ARCHIVED` (plans, written with `--archive`) is
not and needs `--archived`. Plans are the minority by count (119 chunks
against 211) and three times the volume, and their bulk is source code that
now lives in `src/`. `DEFAULT_ORIGINS` must gain any future origin or that
origin silently vanishes from search.

`markdown.py` is pure - no I/O, no store - so its tests carry no `db`
marker and run on CI. Whether a chunk changed is answered by comparing
bodies, not by a stored hash.

Re-ingest also runs **automatically**, because manual meant it drifted:
two days of doc writing once left 32 chunks unindexed. `bag reingest
designate <paths> [--archive]` records which paths a project re-ingests
(migration 016), and `hookio.spawn_ingest` starts a detached `saddlebag
reingest run` from the same two places `spawn_process` starts extraction -
Claude Code's `SessionStart` and `bag hook context` - which is the one
trigger all three harnesses share. Fixed once, not per install path.

- These are **not** subcommands of `ingest`. `bag ingest <path>` is a
  bare command taking positional paths, so a sub-app of that name cannot
  coexist with it, and breaking the documented manual command to make room
  for the automatic one is the wrong trade. `ingest` and `embed` keep their
  fail-loud contracts - a person asked for those.
- The designation holds a value rather than a boolean, like
  `memory designate`, with `archive` **in the primary key**: the refresh is
  genuinely two invocations with different origins, so a project has at
  most two rows and they clear independently. No row means the spawned run
  does nothing, and that silence is the entire opt-in.
- Paths are stored **repo-relative** and resolved against the git root at
  run time. Unlike `memory_settings` this needs no recorded working
  directory - ingest already resolves its project from the git common dir,
  so a worktree and its main checkout share both project and relative
  paths. `designate` refuses an absolute path or a `..` escape, loudly,
  because that is the one moment there is a human to tell.
- `reingest run` is fail-soft in the strongest form this repo has: it exits
  0 on every path, prints nothing to stdout, and explains itself only to
  stderr behind `BAG_HOOK_DEBUG`. It is the one place in `cli.py` that
  catches `BaseException` - `_session` turns an unreachable database into
  `typer.Exit(1)`, a `SystemExit` that would otherwise sail past
  `except Exception` and out of a hook-spawned command as a non-zero exit.
- `services.embed.backfill_if_pending` exists so the common case costs
  nothing. `backfill` takes an Embedder already built, which is right when
  a user asked for it; here the backlog is empty almost every time, and
  constructing a `LocalEmbedder` imports fastembed, builds an ONNX session
  and can download ~130MB. The model **name** is enough to ask whether
  there is work, which is what makes the check possible before the cost -
  the same policy `services.search.shared_embedder` applies inside the
  semantic tier. It returns `None` for "nothing to do", distinct from an
  `EmbedResult` with `embedded=0`, and lets `load` raise so the caller
  decides whether an absent embedder is fatal.
- An unavailable embedder loses the semantic tier and nothing else, so
  `refresh` records it in `embed_error` and keeps the entries it wrote.
  `bag embed` still exits 1 there, on purpose.

Every ingest leaves a row in `ingest_runs` (migration 017), written by the
service for both the spawned refresh (`trigger='auto'`) and `bag ingest`
(`trigger='manual'`): counts, per-path failures, twins, the embed error.
The refresh starts its row **before reading any file** and `reingest run`
opens its session with `autocommit=True` so that a process which dies
mid-run leaves a started, unfinished row - "crashed", not "never ran". A
Python exception is recorded as a failure with path `*` and re-raised.
`bag reingest status` renders the latest row in four distinct spellings
(never, clean, with failures, did not finish) and checks designated paths on
disk **only for the project the current directory resolves to** - the
designation stores no working directory, so any other project reads "paths
not checked" rather than letting silence pass for "all present". A project
with no designation at all still shows its latest run - a plain `saddlebag
ingest` writes a row too - as the not-designated sentence plus the run line
and no disk-check line, since nothing was designated to check.
`bag record status` carries one advisory line per unhealthy **designated**
project - an undesignated project's manual run shows in `reingest status` but
never raises one.

Inside a repository, `bag ingest` identifies a chunk by its path relative
to the working tree's top level (`project.toplevel`, not `repo_root`, which
would resolve a worktree to the main checkout), so a subdirectory run or an
absolute path produces the same `src:` tag the refresh does. A path outside
the repository is refused. Outside any repository, identity stays the path
as typed. When a file comes in entirely new and a live
anchor with the same filename exists under another `src:` path,
`Report.twins` names it: printed to stderr with exit 0 by the CLI, recorded
in the run row by the refresh. Nothing supersedes a twin automatically - a
moved file and a document ingested twice look identical from here.

### Session transcripts

The Claude Code adapter registers four hooks and only two record:
`PostToolUse` -> `tool_call` and `SessionEnd` -> `session_end`. `claude-code`
contributes zero `message` rows, where cursor and opencode both record them.
So what lands in Postgres is `tool_input`, `tool_response`, `cwd` and a tool
name - the part of a session that carries *why* something was done is not
captured by the pipeline whose purpose is to capture why. Claude Code
already writes the rest, in full, to `~/.claude/projects/<slug>/<session
id>.jsonl`; nothing here has ever read one.

This finishes the events design's decision rather than reversing it. That
design replaced transcript-based capture on the rule that derived data lives
apart from its source, is recomputable, and never overwrites it - its
complaint was that capture kept a path instead of the raw material, so a bad
extraction could never be re-run. Events fixed that for the tool layer; this
fixes it for the rest. It does not touch the reason a per-session table was
rejected before: a transcript exists in one harness of three, so this
surface is strictly **additive**. Nothing downstream may require one,
extraction keeps working for a session with only `tool_call` events, and the
tables are simply empty for cursor and opencode.

**Ownership of a directory is proven, not guessed.** A transcript's filename
*is* a session id, and `events` already records which project each session
belongs to, so intersecting the two proves a directory belongs to a
project without a name-matching heuristic to get wrong - matching directory
names would have missed most of this project's own history, filed under an
older binary name. That intersection only ever proves *part* of a directory,
because recording started partway through its history, and it cannot find a
directory at all whose sessions were never recorded - claimable only by a
human who knows it exists. That is why `bag transcripts discover` proposes
claims with their evidence and writes nothing; `bag transcripts designate
<dir>` is the human act that decides.

**A session is more than one file.** Beside `<session id>.jsonl`, Claude
Code writes `<session id>/subagents/agent-<agent id>.jsonl` for every
subagent the session dispatched, and those are the only copy of those
conversations - the parent transcript holds none of their lines. The first
import globbed `*.jsonl` non-recursively in four places and reported
"clean, backlog 0" while leaving more bytes on disk than it stored.
`transcripts.transcript_files()` is now the one owner of the layout, and
nothing may derive identity from `path.stem`: for a subagent the stem is
`agent-<id>`, and both places that once read it went wrong without
raising. Identity is `(session_id, agent_id)` with `agent_id` NULL for the
session's own file (migration 024, `nulls not distinct` - load-bearing, or
the session row stops being unique), and a subagent row's `session_id` is
its parent's, which is what its own lines say and what `events` records
its tool calls under. `agent_id` alone is not an identity; real ones repeat
across parents. Any question about *sessions* - `irrecoverable`, the
`backlog` figure, `discover`'s proof - must filter to `agent_id is null`,
and subagent counts are reported beside session counts rather than folded
into them. `tool-results/` (hook stdout) is not read.

**The `agent-<id>.meta.json` sidecar is stored on its subagent's row**
(`transcripts.meta`, migration 025) - its type, description and model,
small, and the only record of what that subagent was for. Raw `bytea`, not
`jsonb`, because it is source. `transcript_files()` pairs it by name. One
rule governs reading it: **the file has one and the row has none**, checked
after the transcript whatever its plan, so the rows imported before 025
(all `SKIP`) backfill without a special path. It is read once and never
again - sidecars were measured write-once - never cleared when it vanishes,
validated as a JSON object before storing (a torn one kept under
never-overwrite would be kept forever), and spends none of the refresh cap.
`metas_written` on the run row and `meta_backlog` in `status` are what make
a sidecar-only backfill visible. If Claude Code starts rewriting sidecars,
the read-once rule is the one to revisit.

**Claiming a directory is a second, separate opt-in.** The per-project
record gate governs recording going forward; designating a directory backfills
everything already in it, including sessions that predate the pipeline
entirely. Nothing auto-claims, so widening scope is always a human act.

`transcript_lines` is derived from `content` and must stay droppable -
nothing may store anything only there. Labels and any future training
signal reference `(transcript_id, seq)` from their own tables, because the
day one lives on `transcript_lines` itself, rebuilding the parse destroys
data the "derived lives apart from its source" rule exists to protect.

"Derived and rebuildable" is only true if something rebuilds, so an
unchanged file with **zero** stored lines is re-read rather than skipped.
The content and the lines are separate statements under `autocommit=True`,
so a Ctrl-C mid-backfill - or a line Postgres refuses as `jsonb`, a NUL
byte inside a string being the realistic one - strands the bytes with an
empty derived half, which every later run would classify `SKIP` forever.
The guard is `> 0` and deliberately **not** a comparison against an
expected count: a torn final line and a `do nothing` seq conflict are
accepted fidelity warts that leave fewer rows than the file has lines, so
an exact check would re-parse those files on every run.

A shrunk file is an anomaly, not a signal to follow: the stored copy is more
complete than what is on disk, and the entire purpose of the source row is
that a rotating or truncated file does not destroy the session it recorded.
Two sessions already recorded in `events` have no transcript anywhere on
disk - permanently, since nothing prunes on a schedule here either - which
is the rotation risk this design was meant to catch, already realized twice.

A session whose events were recorded under a **different** project than the
one claiming its directory is the second anomaly, and the file is stored
anyway. A directory can hold sessions from more than one project if a
working directory moved, and the bytes are the scarce thing here - a
session Claude Code has since deleted cannot be fetched again - so refusing
to store them to protect a label would trade the irreplaceable half for the
repairable one. The label is made stable instead: `put_transcript` does
**not** carry `project = excluded.project` through its `on conflict`, so a
transcript keeps the project it was first filed under. Overwriting it
re-homed transcripts silently, and every count on both sides
(`stored_transcripts`, the backlog, `status`) is project-scoped, so one
project's numbers dropped and the other's rose with nothing recorded
anywhere. Every entry in `Report.anomalies` therefore names its `reason` -
a reader must not have to tell the two apart by which keys arrived.

Where Claude Code keeps its transcripts is a fact about the harness, so
`transcripts.transcript_root()` owns it beside `HARNESS` and resolves
`CLAUDE_CONFIG_DIR` the way `claude_code.memory` already did. A frontend
building `~/.claude/projects` by hand is a frontend deciding, and it is
silently wrong for anyone who sets that variable: `discover` proposes
nothing with no evidence and no error, and `status` calls every recorded
session irrecoverable. `discover` and `status` still take `root` as a
parameter - only the default moved.

**One file's failure is that file's.** An exception while importing a file
is recorded under its path and the run moves on - a single line `jsonb`
refused (U+0000) once stopped a project's imports for good, because the
zero-lines repair re-read that file first on every run. `parse` now names
such a line as a failure, and `_run_body` isolates each file regardless.
`MAX_CONSECUTIVE_FILE_FAILURES` (3) in a row is not a bad file but a broken
database, so the run raises there rather than recording one failure per
file. A failing file spends the refresh budget, since it may have read
before raising. Isolation is only safe under the autocommit session both
CLI paths already open.

The refresh is bounded before it reads; `import` is not. A session start
must never pay for a backfill, so `bag transcripts refresh` stats first and
skips a file whose size has not changed, capped at a per-run file count
enforced before any read - the same bargain `bag reingest run` and `bag
memory sync` strike between a spawned hook and a person's typed command.
`bag transcripts import` is where the bulk backfill happens, typed, once,
fail-loud like `ingest` and `embed`.

`run()` for a project with no claim records nothing at all, matching what
`bag memory refresh` does for an undesignated project: the spawned refresh
fires at every session start on every project, so the common, unclaimed
case must cost nothing.

Both commands open their session with `autocommit=True`, the same rule `bag
reingest run` and `bag memory sync` follow: the started run row must commit
before any file is read, or a mid-run failure poisons the transaction, the
finishing UPDATE raises in place of the original error, and the row rolls
back - making "crashed" indistinguishable from "never ran".

Redaction is deferred, deliberately. Transcripts are stored raw for the same
reason every other capture boundary here is: filtering at capture caps what
any future extractor could ever see, and extraction is the layer meant to be
fixable and re-run. That reasoning covers capture only - redaction belongs
at export or publish, and neither exists yet, so the boundary does not
either. Said here so the absence is a recorded decision and not an
oversight: a transcript carries far more secret material than a tool-call
event does, and the day this corpus is meant to leave the machine, that is
the first problem to solve.

### Importing claude-mem

`bag import claude-mem <path>` reads claude-mem's sqlite file directly and
writes its rows in as `Entry`s with `origin='imported'`. This is deliberately
the opposite of `ingest`'s network-shaped worries: saddlebag runs on the same
machine that holds the file, so there is no transport to secure and
therefore no `--host`, no remote mode, and nothing to authenticate. A path
is the whole interface.

- Identity is the `cmem:<id>` tag, playing exactly the role `src:`/`sec:`
  plays for ingest: the same source row imported twice produces one entry,
  not two, because `import_.run` looks the tag up before deciding whether to
  create, supersede, or skip. `NAMESPACE` ("cmem") prefixes every tag this
  importer mints, so a future Obsidian or other importer can never collide
  with it on identity.
- **No orphan sweep**, and that is the deliberate opposite of `ingest`'s
  sweep-by-anchor behaviour. Ingest's source is a living directory that the
  same run re-reads in full, so a heading that vanished from the file really
  did vanish and superseding it is correct. An import's source is a tool
  being decommissioned - nothing here re-reads claude-mem's rows on a
  schedule, and a row a person deletes from it after the fact says nothing
  about whether the knowledge it captured is still true. Deleting on their
  behalf would be guessing; leaving the entry alone is not.
- Session summaries become `kind=doc`, not `origin=handoff`, even though a
  handoff is the closer-sounding concept. `bag handoff write` supersedes
  the prior *live* handoff for the same `(project, topic)` on every write,
  because a handoff is deliberately singular - only the newest is ever live,
  the rest are history reachable only by asking for it. Importing fifteen
  historical claude-mem summaries as handoffs under one topic would run that
  invariant fifteen times in a row: fourteen of them would supersede each
  other before a person ever saw them, and `search` excludes non-live
  handoffs by default. A `doc` has no such singularity - all fifteen stay
  independently live and independently searchable, which is what a migration
  owes rows that already existed as distinct records on the other side.
- User prompts become **one entry per session**, not one per prompt, on
  *both* schemas. `claude_mem._prompts` does the grouping - by
  `content_session_id` for the legacy `user_prompts` table, by
  `server_session_id` for a `kind='prompt'` row in the modern
  `memory_items` table, resolved through the same `_column` name-fallback
  that already reconciles `project`/`project_name` - and renders the
  ordered numbered list as the body. A single prompt is frequently a slash
  command - not knowledge on its own - and one entry each would be dozens of
  near-empty entries competing in search against real memories. The
  *sequence* of a session's prompts is the signal worth keeping, and that
  only exists at the session grain. The rule is stated once in `_prompts`
  and reached from both readers, deliberately: a defect once let the modern
  reader route every `kind='prompt'` row through the generic per-row mapping
  instead, exactly what this rule exists to prevent.
- `Origin.IMPORTED` is in `search.DEFAULT_ORIGINS` (searchable, same as
  `EXTRACTED` and `INGESTED`) and deliberately not in `INJECTED_ORIGINS`
  (never rendered into a context block, same reasoning as `EXTRACTED`) -
  imported rows are somebody else's history, not this project's agreed
  conventions, and `kb.pin` is still the way to promote one deliberately.
  `DEFAULT_ORIGINS` must gain any future origin or that origin silently
  vanishes from search - the same warning `ingest` and `extraction` both
  carry, repeated here because it is exactly as true a third time.
- Both the pre-33 (`observations` / `session_summaries` / `user_prompts`)
  and schema-33-and-later (`memory_items`) shapes are read, chosen by
  probing `sqlite_master` for the tables each shape has rather than by a
  version column or a `--schema` flag: the version installed on whatever
  machine produced this file cannot be verified from here, and refusing a
  valid database because this importer guessed wrong about its age would be
  a worse failure than reading two shapes.
- A row `read()` cannot map - today, only an unrecognised `kind` value -
  is not a crash and not a silent drop. It is named in `ReadResult.skipped`
  (table, row id, and why) and carried through `import_.Report.skipped` to
  the CLI, which prints the count and every line. A migration that drops
  knowledge and never says so is unacceptable; a migration that names
  exactly what it could not carry across is the honest version of the same
  job.
- No embedder is constructed here, for the same reason `ingest` and
  `dedupe report` decline to build one: this command's job is to get rows
  into Postgres, not to decide an ONNX session and a possible ~130MB
  download belong to every import. `bag embed` fills in vectors
  afterwards, and the CLI says so when it created or updated anything.
- **`--project` only applies when an entry is first created.** A second run
  over rows that already exist does not move them, even with a different
  `--project`: the changed-body path goes through `write.supersede`, which
  carries the *existing* entry's `project` (and `kind`, and `tags`) forward
  unchanged - the same primitive `bag update --summary` and the memory
  sync's rename re-tag rely on to keep everything a caller does not restate
  intact. An unchanged body writes nothing at all, for the same reason. The
  report reflects this: `by_project` counts where each entry is filed
  **after** the run, not what `--project` asked for, so it can never claim a
  move that did not happen. To re-home an import that already ran, either
  supersede the affected entries by hand or re-import under a fresh
  `NAMESPACE` so a new identity tag forces fresh creates - there is no
  in-place "move a batch of imported entries" operation today.
- A record whose source has no project - every prompt group, legacy or
  modern, since `_prompts` never reads one - is counted under the literal
  bucket name `(no project)` in the report rather than being silently
  omitted from `by_project`: the real rehearsal's own numbers summed to 67
  against 70 records before this bucket existed, and nothing said where the
  other three had gone.
- A v33 database can still carry the pre-33 tables -
  `memory_items.legacy_observation_id` is direct evidence that in-place
  migration is one way a v33 database comes to exist, and claude-mem's own
  migration is not guaranteed to have dropped them. `read()` never reads
  both: doing so risks double-importing rows the migration already copied
  into `memory_items`. Instead each leftover legacy table is named in
  `skipped`, with its row count, exactly as an unrecognised `kind` is -
  silently dropping them would be the loss `skipped` exists to prevent.

### Claude Code memory

`bag memory sync` owns `~/.claude/projects/<cwd-slug>/memory/` - Claude
Code's file-based memory - as a generated view of a designated collection.
Unlike opencode's `saddlebag.js` and cursor's `saddlebag.mdc`, this generated file
set has a second writer that cannot be told to stop, so the sync **adopts
before it regenerates**: anything on disk saddlebag has not seen becomes an
entry first.

- Opt-in per project, holding a value rather than a boolean: which
  collection. An undesignated project generates nothing, which is what
  keeps `MEMORY.md` from double-loading against the `SessionStart` block.
- The designation also records the **working directory it was made from**
  (migration 015), because the two halves are keyed on different things:
  the designation on the project, the memory directory on the absolute
  cwd. Neither derives the other - a worktree and its main checkout share
  a project and have two memory directories - so `bag memory sync --all`
  is only expressible because the answer is stored. `designate` therefore
  refuses a `--project` naming anything but the current directory's
  project: recording a working directory that has nothing to do with the
  designation would surface much later, as a sync writing to the wrong
  place. Rows written before 015 read back as `None` and `--all` skips
  them by name rather than guessing; re-designating is the fix, and the
  skip exits non-zero because a directory that was not synced is a
  definite statement, not an "I could not tell".
- `.saddlebag-sync.json` is what makes "which side moved" answerable. This is
  deliberately the opposite of `ingest`, which compares bodies and stores no
  hash - ingest has one writer, so "differs" and "the file changed" are the
  same statement. Here both sides write.
- Two gates and they are the whole safety story: a file whose checksum does
  not match its watermark is never deleted, and a file changed on both sides
  is never overwritten. Conflicts write saddlebag's version alongside as
  `<name>.saddlebag-conflict.md` and exit non-zero.
- Identity is the **filename stem**, recorded as a `mem:<name>` tag. An
  entry written by hand has no such tag, so the export mints one from the
  title and writes it back before the cases run - without that pre-pass the
  export is only ever what the sync adopted off disk, which is silently the
  "directory owns it, saddlebag ingests" design the spec rejected. The
  frontmatter `name:` is the user's field, carried through a regenerate
  rather than rewritten.
- A file **renamed** on disk is followed rather than duplicated, by a second
  pre-pass (`_follow_renames`). Identity being the stem, `classify` can only
  see a rename as two independent names - an `ADOPT_NEW` for the new stem and
  a `REGENERATE` for the old - which minted a second entry holding the same
  body and wrote the deleted file back out. Both gates behave correctly
  throughout, which is why the duplication went unnoticed: the failure is
  duplication, not data loss. The proof of a rename is **byte equality with
  the watermark**, which records what saddlebag itself last wrote under the old
  name, so it is provable rather than guessed; nothing looser is permitted,
  because matching on titles or near-identical bodies would re-tag entries on
  a coincidence. Ambiguity in **either** direction (two identical files for
  one missing name, or two watermarks matching one arriving file) is refused
  and reported, never guessed, exactly as `_adopt_names` refuses two entries
  sharing a name. An unparseable file is excluded as a rename source - it is
  absent from `files` but very much present on disk. The floor, stated rather
  than papered over: a rename **and** an edit in the same interval still
  duplicates, because that is precisely the case the proof cannot cover. The
  re-tag uses `update`, not `supersede` - the knowledge did not change, only
  the name it is filed under.
- A collection that resolves at `kb.RESOLVE_LIMIT` refuses to sync. Past the
  cap an entry saddlebag cannot see is indistinguishable from one that left the
  collection, and the delete gate would pass.
- `memory_file.py` is pure, so its tests carry no `db` marker and run on CI.
  The round trip is load-bearing rather than cosmetic, but whole-file byte
  equality is not the property - a real fixture disproved it. What the sync
  needs is that the **body** round-trips byte for byte (the watermark hashes
  the body alone, so frontmatter whitespace can never read as a content
  change), that `render` is idempotent, and that no `metadata:` key parse
  saw is ever dropped.
- The generated `MEMORY.md` uses an em dash between link and hook, against
  this repo's convention, because that line's format belongs to Claude Code.
- Not in `bag doctor`: the designation lives in the database and doctor
  opens no connection. The overlap count lives in `bag memory status`,
  along with a count of `.saddlebag-conflict.md` sidecars still on disk from a
  past sync - nothing ever deletes one automatically, since doing so risks
  destroying the copy the user needs, so `status` is what keeps an
  unresolved conflict from going unnoticed between syncs.

- Every sync leaves a row in `memory_runs` (migration 018), written by the
  service so that `bag memory sync`, `--all`, and any future hook-spawned
  run record identically - the last of those being the caller with no
  terminal, and the reason the table was built before it exists. One row per
  **project**: `--all` writes one each, and a single row could not say which
  one failed. A `--dry-run` writes none, because a row for it would make
  "last run" describe a state that never existed.
- `bag memory sync` therefore opens with `autocommit=True`, like `saddlebag
  reingest run`: the started row must be committed before any file is read,
  or a crash rolls it back and "crashed" is indistinguishable from "never
  ran". This also stops sync's two halves disagreeing - files are written to
  disk as it goes, so a single transaction only ever rolled the store back
  and left the disk moved. Sync is idempotent and re-runnable, and both
  gates hold on the next run exactly as they held on this one. `sync()` is
  split into a recording wrapper and `_sync_body`, which is where the
  algorithm lives: the wrapper owns the row, and the body is passed the
  `Report` it mutates so a partial run is still recorded when it raises.
- Sync also runs **automatically**, for the same reason re-ingest does:
  the directory has a second writer that cannot be told to stop, so a
  designated project drifts between manual syncs. `hookio.spawn_memory`
  starts a detached `bag memory refresh` from the same two places
  `spawn_process` and `spawn_ingest` start theirs - Claude Code's
  `SessionStart` and `bag hook context` - which is the one trigger all
  three harnesses share. It covers the **current project only**, like
  `bag reingest run`: syncing all eight designated projects from any
  session start would write into seven directories the user is not
  looking at.
- `bag memory refresh` is the spawned half and `bag memory sync` stays
  the typed one, keeping its loud contract: a person asked for that, and it
  exits non-zero on a conflict. `refresh` exits 0 on every path, prints
  nothing to stdout, explains itself only to stderr behind
  `BAG_HOOK_DEBUG`, and is the only caller that records
  `trigger='auto'`. An undesignated project - the common case - does
  nothing and records nothing. A conflict under `refresh` writes its
  sidecar and says nothing at the time; `bag memory status` and the
  `memory.advisories()` line in `bag record status` are what surface it,
  which is what that layer was built for. Both safety gates are unchanged
  and hold identically unattended.
- The `except BaseException` in `refresh` and in `reingest run` is **not**
  there for the reason first recorded against it. `typer.Exit` is a
  `RuntimeError`, so the `typer.Exit(1)` `_session` raises for an
  unreachable database is caught by `except Exception` too - measured. What
  BaseException adds is a genuine `SystemExit` from any library that calls
  `sys.exit()`, and `KeyboardInterrupt`. A test asserts that, because the
  unreachable-database case passes under either handler and proves nothing.
- `bag memory status` renders the latest run in four distinct spellings
  (never, clean, with conflicts or failures, did not finish). Those describe
  what last *happened*; `stale`, `overlap` and `conflicts` describe the
  directory *now*, and a reader must not have to infer one from the other.
  Every spelling that has a run names its **trigger**, as `reingest status`
  does: a spawned `refresh` and a typed `sync` leave identical rows, so
  without it a reader cannot tell an unattended run from their own, and
  believing the automatic half ran when only a manual one had is precisely
  the false premise this line exists to make visible. The trigger rides on
  the failure count in `advisories()` and deliberately not on the sidecar
  count - a sidecar outlives every run, so naming the last one beside it
  would attribute it to a sync that may not have written it.
- `bag memory status --json` emits **one object, not a list**, which is
  the deliberate departure from `bag reingest status --json`: that one
  sweeps every designated project and so returns an array, while this
  command resolves exactly one project by construction. The keys are the
  same in every state - an undesignated project is a null `collection` and
  a null `run`, not a shorter document - so a consumer checks a field for
  null rather than branching on which keys arrived. That is the one place
  the JSON and the text output differ, the text path having an early
  return for it. Timestamps are a raw `isoformat()`: the offset travels in
  the string, so the local-time conversion `render_run` does for a human
  reading beside `reingest status` would only rewrite it for no reader.

- `memory.advisories()` raises one line per unhealthy designated project in
  `bag record status`: an unresolved conflict sidecar, a run that did not
  finish, failures in the last run, or a designation that has never synced.
  Unlike `reingest status`, it checks **every** designated project's
  directory, because `memory_settings` records the working directory
  (migration 015) - the answer is stored, not guessed. A pre-015 row with no
  directory is named and told to re-designate, never guessed at, and a
  recorded directory that no longer exists is reported as missing rather
  than as clean. The sweep over projects lives in `advisories()` because
  `memory.status()` answers for one project at a time, unlike
  `ingest.status()`.

### Settings

`bag config` reads and writes two files from one command, routed by key
name: `BAG_*` keys land in saddlebag's `config.toml`, a curated set of Claude
Code environment variables lands in the `env` block of `settings.json`.

- The table of Claude Code variables lives on the **adapter**
  (`agents/claude_code/env_vars.py`), not in `services/`. It is a fact about
  Claude Code, not about saddlebag, and keeping it there is what lets a future
  adapter ship its own.
- `env_settings()` and `settings_path()` are **optional adapter capabilities**,
  probed with `getattr` in `settings.resolve_targets` and documented on the
  Protocol rather than declared on it. They go together: an adapter with only
  one of them reports "no settable env vars" rather than falling back to
  another agent's file. The frontend resolves `--agent` and nothing else.
  A capability that *raises* lands where a missing one lands - warn, degrade to
  the saddlebag half, and keep going. Same contract as `agents/registry.discover`:
  a broken third-party adapter must never be why `bag config` will not run.
- **No credential and no endpoint variable is ever settable.** Their absence
  from the table is the enforcement; `tests/test_env_vars.py` asserts it.
  Neither is `CLAUDE_CONFIG_DIR` or `BAG_CONFIG` - each names the file that
  would store it.
- The two targets resolve in **opposite directions**: the environment beats
  saddlebag's `config.toml`, while `settings.json` beats a shell export. `set`
  says so when the key it just wrote is also exported, because writing a
  shadowed saddlebag key is otherwise a silent no-op.
- **Every write backs the file up first and says where the backup went.** A
  rewrite of `config.toml` loses comments and formatting, so the writers
  return the backup path and the CLI echoes it - a `.bak<timestamp>` nobody
  is told about is barely a safety net.
- This is the one service that opens no database connection. It must keep
  working with Postgres down.

### Hooks are fail-soft, and that is a hard contract

`hook.session_start` / `session_end` exit 0 unconditionally, print nothing on error, and
never raise. A knowledge tool must never be why a session will not start. Because silence
is ambiguous, `BAG_HOOK_DEBUG=1` writes the reason to **stderr** - stdout is the context
block and nothing else.

The SessionStart hook writes a **JSON document**, not the bare block:
`hookSpecificOutput.additionalContext` carries the block to the model, exactly as
plain stdout used to, and `systemMessage` carries a one-line banner Claude Code
shows the user as `SessionStart:startup says: ...`. JSON is the only shape that
can do both, and once stdout is JSON plain text is no longer read as context -
so never write both. The banner is `context.banner()`, rendered from
`context.Injection` (the facts: knowledge base found, rules and notes the block
actually carried, recording on/off, live handoff), never parsed back out of the
block. `context.block()` is `injection().text` and is what `bag hook context`,
opencode and Cursor still use - they have no channel to a human. Fail-soft is
unchanged: anything that never reached the database is `None` and silence. A
project with **no** knowledge base is not that case - the database answered - so
it gets a banner naming the slug and `bag kb new`, which is the likeliest reason
a session gets no context and was invisible before. `RulesExceedBudget` still
raises before anything is known and stays silent here; `bag record status` is
where that one shows.

The banner also carries the `bag stats` lines, and they are **user-visible
only**: they go to `systemMessage` and never to `additionalContext`, so the
model pays no tokens for them and the block it receives is unchanged whether
stats were collected or not. See "Usage statistics" below.

The SessionStart hook injects the knowledge base whose slug is exactly the session
directory's name. Writes, by contrast, resolve `--project` from the git repository root
(`project.resolve_project`, via `--git-common-dir`) so subdirectories and worktrees file
under the repository they belong to.

### The opencode adapter

`plugin.js` (`src/saddlebag/agents/opencode/plugin.js`) is hand-written and shipped as
package data; the INSTALLED copy - `saddlebag.js`, in the directory `plugin_dir` names for
the chosen scope - is what is generated and machine-owned. `install()` overwrites that
installed copy unconditionally, no merge, no version marker, no prompt. It imports
nothing, because `$` arrives on `PluginInput`: there is no npm dependency to install,
pin, or keep in step with opencode's own releases. A user who wants local edits to
`saddlebag.js` is asking for the wrong file - saddlebag owns it.

opencode has no session-start hook, so `bag hook context --agent <name>` exists as the
harness-neutral half of what `hook.session_start` does for Claude Code: given whatever
payload a harness has on hand, the adapter's `identity()` turns it into a project and the
command prints the knowledge base block for that session, fail-soft like every other
hook. The opencode plugin calls it from `experimental.chat.system.transform`, which fires
on every message - the plugin holds an in-process `Set` of session ids so the block is
fetched once per session, not once per turn, the same bargain Claude Code's SessionStart
makes by construction.

The hook-contract test (`tests/test_opencode_hooks_contract.py`) is deliberately two
tests, not one: `test_the_plugin_subscribes_only_to_hooks_opencode_emits` always runs, on
CI and everywhere else, and catches `plugin.js` subscribing to a hook name opencode does
not emit - the exact failure mode of a competing tool's opencode integration that reported
success for months while recording nothing. `test_the_vendored_list_still_matches_the_installed_types`
is marked `@pytest.mark.opencode` and may skip; it only guards the freshness of saddlebag's own
vendored copy of opencode's `Hooks` interface (`HOOK_NAMES`, `PLUGIN_TYPES_VERSION`), which
needs opencode's plugin types installed to check. Collapsing them into one test would
produce a guard that skips on CI - the same failure mode the `db` markers already taught
this project to distrust.

### The cursor adapter

`src/saddlebag/agents/cursor/`, four modules and no generated script of any kind -
`hooks.json` names the `bag` command directly, because `bag record event`
already reads its payload as JSON on stdin. It sits between the two adapters
that shipped before it and deliberately borrows from each: `.cursor/hooks.json`
is user-owned and shared - other tools write there too - so it gets Claude
Code's treatment (read, merge, back up first, echo the backup path), while the
generated `.cursor/rules/saddlebag.mdc` is machine-owned and overwritten
unconditionally, like opencode's `saddlebag.js`. A user who wants local edits to
the `.mdc` is asking for the wrong file.

Cursor emits 21 hooks (vendored in `agents/cursor/hooks.py`, read out of
`Cursor.app`'s minified bundle); this adapter subscribes to exactly four:
`sessionStart` (injects, via `bag hook context --agent cursor`),
`postToolUse` (`EventKind.TOOL_CALL`), and `beforeSubmitPrompt` /
`afterAgentResponse` (both `EventKind.MESSAGE`). Six hooks **block** - Cursor
waits on them for a permission decision on their stdout
(`beforeShellExecution`, `beforeMCPExecution`, `beforeReadFile`,
`beforeTabFileRead`, `subagentStart`, `preToolUse`) - and this adapter is
deliberately wired to none of them, checked in as `hooks.BLOCKING_HOOKS` so an
edit that reaches for one fails a test instead of shipping a hook that can
deny a permission by failing. The remaining exclusions are `afterAgentThought`
(reasoning text: high-volume, low-signal for extraction) and the specific
`afterShellExecution`/`afterMCPExecution`/`afterFileEdit` hooks, folded into
the generic `postToolUse` instead - one parser instead of three, and no gap
opens when Cursor adds a tool type.

**Injection is one of seven optional adapter capabilities** -
`inject(self, block, payload) -> str | None`, probed with `getattr` exactly as
`event()`, `env_settings()`, `settings_path()`, `verify()`, `hook_state()` and
`memory_dir()` are (the full seven, with the reasoning for each, are the comment block on
`agents/base.py`'s Protocol - keep the count there and here in step, because
an author who learns a capability exists by accident is how the missing
`PostToolUse` survived). Cursor needs the
context block written to a file rather than printed or returned, so `cli.py`
never has to know what an `.mdc` is; Claude Code simply does not implement
`inject()`. This is the one probed capability where a **missing**
implementation is the default, not a degradation - printing to stdout at
`SessionStart` is exactly what Claude Code is supposed to do, not a fallback
from something richer. `sessionStart` fires once, so the `.mdc` is written
once per session by construction, with no state to keep anywhere - the third
time this project has needed "once per session" and the third different
mechanism: Claude Code gets it free from `SessionStart`, opencode keeps an
in-process `Set` of session ids because it has no session hook at all, and
Cursor needed neither once the stale "no `SessionStart` equivalent" premise
from the events-and-recall spec was corrected.

Writing the `.mdc` is also the first time saddlebag puts context into the user's
**working tree** rather than a stream, which is why `agents/cursor/rules.py`
adds `.cursor/rules/saddlebag.mdc` to **`.git/info/exclude`, not `.gitignore`**:
`.gitignore` is tracked and reviewed, so appending to it hands the user a diff
they did not ask for, and in a shared repository that diff lands in somebody's
pull request; `info/exclude` is local-only and exactly the mechanism git
provides for "ignore this here, not for everyone." The append is idempotent
(checked line-by-line, not by a whitespace-token membership test, so a
commented-out line doesn't count as already-present) and resolves through
`git rev-parse --git-common-dir` rather than assuming `.git` is a directory -
in a linked worktree or a submodule `.git` is a *file* holding a `gitdir:`
pointer, and `info/exclude` lives under the real common directory that
pointer names, not under the worktree. saddlebag's own development happens inside
a worktree, so this is the ordinary case here, not an edge case. If there is
no repository at all, the `.mdc` is still written and a note (not a warning)
says the exclude was skipped - a workspace outside a repository is an
ordinary thing.

The always-runs half of the hook-contract check follows opencode's shape
exactly: every hook named in the generated `hooks.json` and every key of
`EVENT_KINDS` is checked against the vendored `HOOK_NAMES` -
`tests/test_cursor_install.py` and `tests/test_cursor_event.py`
respectively, both always running - which is what would have caught
subscribing to a hook Cursor does not emit.
`tests/test_cursor_hooks_contract.py` holds the other half: one
`@pytest.mark.cursor` test, which may skip, that re-reads the installed
`Cursor.app` bundle and checks the vendored list is still fresh. Cursor
auto-updates itself, so expect the freshness half to fire eventually, the
same way opencode's did mid-branch.

A Cursor-only install extracts as well as records. `bag events process` used
to be spawned from exactly one place, Claude Code's `SessionStart` hook, so a
Cursor-only or opencode-only setup recorded events forever and never extracted
one. It is now spawned from `hookio.spawn_process`, called both from that hook
and from `bag hook context` - the session-start analogue opencode and Cursor
already call once per session, which makes it the single trigger all three
harnesses share. Fixed once, rather than bolted onto each new install path.

The call sits in a `finally`, so it runs on every path through `hook context`
including the early returns for unusable stdin, an unknown agent and an
unresolvable project: the backlog is global, and whether *this* payload
produced a block says nothing about whether extraction has work waiting.

Extraction shells out to `claude -p`, which a Cursor-only user may well not
have installed. Those jobs **fail and record the reason** rather than being
skipped - `MAX_ATTEMPTS` stops the retries and `bag record status` shows why.
A probe for the extractor was considered and rejected: it can be wrong about
where `claude` lives, while a recorded failure cannot.

Every Cursor hook payload also carries `user_email` (read straight out of
Cursor's payload constructor) and saddlebag stores events in full and
indefinitely, so a recorded Cursor event carries the user's email address as
a side effect of this design - unlike Claude Code's events today. Nothing in
this adapter filters it; the payload is passed through whole, on purpose, for
the same reason every other adapter here does: extraction is the layer meant
to be fixable and re-run without re-recording anything, and pruning fields at
the recording boundary caps what any future extractor could ever see.

**This adapter has recorded real events from a real Cursor session** (2026-08-30,
Cursor 3.18.9). Every payload key `identity()`/`event()` reads (`session_id`,
`workspace_roots`, `hook_event_name`, `tool_name`) was originally derived from
reading Cursor's payload-constructing code in the shipped app bundle, confirmed
by a second independent reading, and has since been confirmed against live
payloads: nine events over two turns, both `EventKind`s, real tool names, and
`workspace_roots` resolving to the right project. The source reading was
correct. See `docs/superpowers/notes/2026-08-29-cursor-payloads.md` for that
reading and `docs/superpowers/notes/2026-08-29-cursor-proof.md` for exactly
what is and is not proven - three of the four proof criteria are closed, and
only one remains open: the block confirmed present in the outbound request,
which cannot be shown from this machine because Cursor's local logs carry no
request bodies. Closing it would take a TLS-intercepting proxy in front of
Cursor, and it is the least valuable of the four now that live payloads have
disproved the failure it stood in for.

Two things the live session taught that are not about Cursor at all. **Cursor
loads Claude Code's `~/.claude/settings.json` hooks and runs them with Cursor
payloads** - so saddlebag's Claude-Code hooks fire inside Cursor, are handed a shape
they cannot parse, and exit 0 in silence. Nothing is broken by it today; it is
undecided territory rather than a bug, and it means the two adapters are not as
independent as the seam suggests. And a `kb.RulesExceedBudget` failure is
**invisible**, because every hook is fail-soft: when the knowledge base outgrew
`BAG_MAX_CHARS`, context injection silently died on *every* harness, Claude
Code included, with a 0 exit and no output. Fail-soft is still the right
contract, but the budget is the one failure it hides that a user would want to
know about - so `kb.budget_advisories` says so in `bag record status`, the
place that already carries the doctor, ingest and memory advisories. See
"The context block budget" below.

### The context block budget

`kb.budget_advisories` is the one place a dead context block becomes
visible. Fail-soft stays: `RulesExceedBudget` still escapes into hooks that
exit 0 and print nothing, and this is the fail-loud half asked for on
demand, exactly as `record status` is for recording.

- It measures **rules plus header** (`kb.rules_chars`), never the rendered
  block's length. Rules never truncate and notes are dropped whole to make
  room, so a shipped block is pinned at or under the budget by
  construction - and is zero characters for a knowledge base that raised.
  Block length is uncorrelated with the failure in both directions; only
  rules plus header predicts it, and only pruning rules moves it back.
- `rules_chars` and the raise site both sum `_header_and_rules`. Two
  copies of that arithmetic drifting apart would mean the advisory
  reporting healthy on a knowledge base that is raising, which is the
  whole bug.
- Two tiers, and the warning below the limit (`BUDGET_WARN_FRACTION`,
  0.8) is the valuable one: by the time the hard failure fires, injection
  has already been dead in every session since the rule that tipped it
  over was written.
- **Not in `bag doctor`**, deliberately: doctor reads files and opens no
  database so that it still works when the system does not, and this
  question cannot be answered without resolving a collection.

### Usage statistics

Two logs (migration 026) and one reader. `access_log` is one row per read a
frontend performed - a `search`, a `get`, a `handoff` lookup - and
`injection_log` is one row per session start that reached the database.
`bag stats` and the SessionStart banner are what read them. The question
they exist to answer is the one saddlebag could never answer about itself:
whether anything it stores is ever retrieved, and whether the rules it
injects into every session are ever acted on.

**Both tables are metadata only, and that is load-bearing rather than
tidy.** `access_log` stores the query's **length** and never its text;
both tables store entry **ids** and never entry content. That is precisely
why they can sit outside the per-project record opt-in that governs
`events`. The opt-in in `services/record.py` exists because an event holds
`tool_input` and `tool_response` - the user's actual work, in full, kept
indefinitely. A row saying "a 23-character search ran, hit the exact tier,
returned these four ids in 31ms" describes **saddlebag's** use, not the
user's work, and gating it behind the same opt-in would have meant the
statistics were blank on exactly the projects nobody had thought to
configure. If a field is ever added here that carries content, that
reasoning collapses and the opt-in question has to be re-asked - so do not
add one casually.

**Logging happens only when a frontend passes `source`.** `search.find`,
the `get` path and the handoff read all take `source` defaulting to
`None`, and `None` writes nothing. Tests, `scripts-eval-retrieval.py` and
every internal caller therefore never appear in the numbers, which is what
keeps a 165-question eval run from reading as a very productive Tuesday.
The cost of that design is the usual one and it is stated here so nobody
discovers it the hard way: **a new frontend that does not pass a `source`
is invisible** - it works perfectly, logs nothing, and its users silently
do not exist in `bag stats`. This is the same warning
`search.DEFAULT_ORIGINS` carries three times in this file, for the same
reason: an allowlist keeps the wrong thing out by keeping everything out,
and the new thing is always the thing that was forgotten. Today the
allowlist is `('cli', 'mcp', 'hook')`, checked in the migration, so at
least a typo fails loudly rather than filing rows under a fourth source
nothing renders.

**Every log write is savepointed and fail-soft** (`services/usage.py`).
Fail-soft is obvious - a search must never fail because its log row could
not be written - but the savepoint is the part worth not removing. These
writes happen **inside the caller's transaction**, so without
`store.transaction()` wrapping them a failed insert leaves the connection
in `InFailedSqlTransaction` and the statement that raises is not the log
write at all: it is the **caller's next statement**, somewhere else
entirely, with a message about a transaction being aborted. A
try/except around the insert alone would catch the log's own failure and
hand the damage to whoever ran next. Failures are explained to stderr
behind `BAG_HOOK_DEBUG`, the same gate `hookio.debug` uses, and never to
stdout - which may be the MCP protocol stream.

**`CLAUDE_CODE_SESSION_ID` is the variable Claude Code actually sets**, in
every process it starts - Bash tool calls and MCP servers alike.
`mcp_server.py` read `CLAUDE_SESSION_ID`, which is never set by anything,
so every entry written through an MCP `remember` before this change
carries a null session id: 60 of them, measured 2026-09-16. The name lives
once, as `usage.SESSION_ENV`, because the failure was silent in both
directions - nothing errors when an environment variable is absent, and a
null column looks like an ordinary optional field. Two caveats survive the
fix. The MCP server is started **once per Claude Code process**, so after a
`/clear` it still reports the id its process started with; that is still
right far more often than null was, which is why it is kept rather than
blanked. And over `--http` the environment belongs to whatever launched
the server rather than to the agent calling the tool, so the id is not
merely absent but **wrong** - `_session_id()` returns `None` there, the
same reasoning that pins the project with `--project`.

**Follow-through is a proxy, not a quality score.** The
`injected`/`opened` figure joins the ids a session was handed against the
ids that session later read, and a low number does not mean the rules were
ignored: rules render as title plus summary, and **a rule obeyed from its
summary is never opened** - that is the whole point of the short form (see
"What the context block carries"). What it measures is how often the block
was insufficient on its own. Read it as a signal about the summaries, not
as a grade for the knowledge base, and do not tune anything to make the
number go up.

**Fail-soft per section in the hook, loud about the database in `bag
stats`, and an unavailable section is never an exit code.** Each section in
`stats.collect` runs inside its own savepoint with a 1.5s
`statement_timeout`, so one slow or broken query costs **one line** and not
the banner - the SessionStart hook has ten seconds for everything it does,
and without the per-section savepoint the first failure aborts the
transaction and takes every later section with it. `Unavailable(reason)` is
a third state deliberately distinct from zero: a read count of zero and a
read count nobody could measure are different statements. `bag stats`
exits non-zero for an unreachable database - a person typed it and deserves
to know - but exits **0** with an `unavailable (...)` line for a section
that could not answer, the rule `bag doctor` follows for `UNCHECKED`:
exiting non-zero for "I could not tell" trains people to ignore the exit
code. Two consequences of that savepoint worth knowing before writing a
third caller. The `set local statement_timeout` **outlives the savepoint**
- `set local` lasts until the enclosing *transaction* ends - so it caps
every later statement in that transaction including the caller's own:
**collect last**. Both callers today do, and nothing enforces it. And when
**every** section comes back unavailable with the *same* reason - a
connection lost between the injection and the collection is one problem,
not seven - `render` returns no lines at all, so the banner is exactly
today's single line; `bag stats` prints `stats.collapsed_line` instead,
because a person typed that one and an empty screen with a zero exit is
not an answer.

**A session is not a row, and the two halves of a ratio come from one
population.** Claude Code fires `SessionStart` on startup, resume, clear
*and* compact, so one session writes several `injection_log` rows:
`injection_summary` counts `distinct session_id` and dedupes the
follow-through pairs per `(session, entry)`, or a user who compacts often
reports several sessions and a follow-through dragged toward zero by their
own `/compact`. The `N/M sessions` figure on the recall line is likewise
**both** counted from `injection_log` - injected sessions that then read,
out of injected sessions. Drawing the numerator from `access_log` instead
compared two different populations: the CLI reads its session id from
`CLAUDE_CODE_SESSION_ID`, which only Claude Code sets, so a cursor or
opencode read carries no session at all and could never enter the
numerator while `bag hook context` still wrote an injection row into the
denominator - a permanent under-report for two of the three harnesses
saddlebag targets, and a ratio that could render `1/0`. A session that read
without being injected is outside the ratio entirely.

**Five recorded columns are deliberately unread.** `access_log.query_len`
and `injection_log.found`, `chars`, `budget_chars` and `harness` are
written and never rendered, here or in `--json`. That is the same rule the
rest of this file applies to capture - derived data lives apart from its
source, and filtering at the recording boundary caps what any future
reader could ever see - not an oversight, and adding a query for one of
them needs no migration. `harness` is the one worth naming, because the
question it answers is a real one nobody has asked yet: **which harness is
actually getting context**. `bag hook context` threads the agent name
through precisely so cursor and opencode are distinguishable; the data is
there and the query is missing. `source = 'hook'` is reserved in the same
spirit - migration 026's check constraint allows it, nothing writes it
today, and it is there for a read performed on a hook path (`bag hook
context` writes an injection row, not an access row, so there is nothing
to wire it to yet). An applied migration is never edited, so the value
stays and this sentence is why.

**The banner counts a smaller backlog than a typed command does.**
`stats.collect` takes an `awaiting_limit`; `bag stats` and `bag record
status` use `events.STATUS_AWAITING_LIMIT` (1000, justified for a command
a person typed), and the SessionStart hook passes
`stats.BANNER_AWAITING_LIMIT` (25). The cost is one aggregate over the
never-pruned `events` table plus one `extract_job_for_session` round-trip
per candidate, and the 1.5s `statement_timeout` is no backstop there - it
bounds each statement, not a thousand of them. A banner line only has to
say whether extraction is behind, and a count that hit its limit renders
as `25+` rather than as a tidy twenty-five.

**Percentages floor, they never round.** `_pct` reports 37 of 40 as 92%,
not the 93% rounding would give it - every number saddlebag shows errs
toward claiming less than it knows. The epsilon in `_pct` is the opposite
case and is **not** a rounding fudge: a fraction arriving as a float - the
budget does - can be 0.29, and `100 * 0.29` is 28.999999999999996 in binary
floating point, which floors to 28% and is not conservative but wrong. The
epsilon is far smaller than any difference a whole percent can show, so it
corrects binary representation and nothing else. A test pins 0.29 rendering
as 29% against anyone simplifying it away.

**`since <date>` rather than a window that was never measured, and
nothing is backfilled.** When the earliest logged row is younger than the
window, the period reads `since 2026-09-16` instead of `7d`. These numbers
begin when logging began - there is no history to reconstruct, because
`entries` and `events` record what was written, never what was read - so a
window spanning days that were not logged would report a quiet week that
simply had no log. The first week after this ships looks sparse, and that
is honest rather than broken.

**Stats never reach `additionalContext`.** `render_output` passes the
rendered lines only to `systemMessage`, via `context.banner`; the block the
model receives is byte-identical whether or not stats were collected. The
model pays no tokens for them, which is also why the lines are free to be
this dense - they are for the person reading the banner. `context.py`
takes rendered lines rather than a `Stats` and never imports
`services.stats`, so it collects nothing on its own.

## Verification

`make check` is the only verification command. It runs, in order,
`ruff format` -> `ruff check --fix` -> `pyrefly check` -> `pytest`, and
**stops at the first stage that fails**, so a lint error never arrives
buried under a wall of type errors and test output. Run it; read the one
failure; run it again.

- **Never hand-edit anything ruff fixes.** `make check` runs
  `ruff format` and `ruff check --fix` before it runs anything else, so
  import order, spacing, quotes and the rest are already correct by the
  time you see output. Editing them by hand is work the gate has
  already done, and it fights the formatter on the next run.
- **The output is the interface.** pytest runs `-q --no-header --tb=line
  -x --lf`, ruff uses `output-format = "concise"`, and pyrefly uses
  `--output-format=min-text` - one line per diagnostic, everywhere, and
  a failing loop reruns only what failed. Do not add flags that make it
  chattier; CI is where the full picture belongs, and `.gitlab-ci.yml`
  overrides `-x` with `--maxfail=0` for exactly that reason.
- Coverage is **CI-only**, on purpose. It roughly doubles the local run
  for a number nobody reads mid-loop.
- **stdout belongs to the MCP protocol.** Under the stdio transport the
  server speaks JSON-RPC on stdout, so one stray `print()` corrupts the
  stream for the whole session. `T20` is selected as an error for that
  reason and there are no exemptions - `config.py`'s deprecation notices
  use `sys.stderr.write`, as `hookio.debug` always has. The CLI's own
  output goes through `typer.echo`, which the rule does not touch. The
  same discipline is why every hook prints its diagnostics to stderr
  behind `BAG_HOOK_DEBUG`.
- Line width is 88 and the **formatter owns it**. The prose comments
  keep their older, narrower hand-wrapping; ruff does not reflow
  comments and neither should you, for a diff's sake.

## Conventions

- Python 3.14 (`uuid7` from stdlib, `StrEnum`, `from __future__ import annotations`).
- Comments explain *why*, at length, especially where a decision looks arbitrary. Match
  that density; a subtle invariant with no comment reads as an accident to the next reader.
- Prefer failing loudly over quietly doing something else - `UnsupportedScope` is raised
  for `--scope project` rather than falling back.
- In prose and docs: spaced hyphens ` - `, never em dashes.

## Design record

Spec and plan under `docs/superpowers/`; `decisions-2026-08-26*.md` hold the reasoning
behind rulings git history does not capture. Read the relevant decision record before
reversing something that looks odd.
