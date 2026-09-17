# Usage statistics

Design, 2026-09-16.

## The problem

saddlebag cannot say whether it is being used, or whether what it does is
any good. The SessionStart banner is one line - knowledge base, rule and
note counts, recording on/off, the live handoff - and nothing anywhere
answers:

- how often a session recalls anything, and how often that finds something;
- which search tier answers (exact, semantic, trigram);
- how much context each session is handed, and whether any of it is opened;
- whether vectors, extraction, transcripts, memory sync and ingest are
  keeping up;
- what was written to the store recently.

Most of this is not derivable from what is stored today. Measured on
2026-09-16 over 38 recorded Claude Code sessions: the MCP `recall` tool was
called **once**, while `bag search` / `bag handoff` ran 41 times in 10
sessions via Bash. Recall is overwhelmingly a CLI act, and the CLI's output
is text in a `tool_response` - the tier that answered and the hit count are
not recoverable from it. Parsing Bash command strings out of `events` would
also get slower as events accumulate and would only ever cover the one
project that records.

So the numbers are recorded at the moment they happen, in the service every
frontend already goes through, and read back by one stats service that both
the banner and a new `bag stats` command render.

## Decisions

- **Show everything at session start**, as a multi-line `systemMessage`,
  plus `bag stats` for the same facts on demand. The banner reaches the user
  only; `additionalContext` is unchanged, so the model pays no tokens for it.
- **Record at the service, not parse events** (approach A). Rejected: a
  precomputed snapshot written by a spawned job (always one session stale),
  and parsing `events` payloads (fragile, slow, cannot answer tier mix).
- **Metadata only.** The query's length is stored, never its text. These
  logs are about saddlebag's own use and are not gated by the per-project
  record opt-in, which is exactly why they must not carry content.
- **Log only when asked.** `source=` is an explicit argument; tests, the
  eval script and internal callers pass nothing and are never counted.
- **Fail-soft where the hook is, loud where a person asked.**

## Section 1 - what gets recorded (migration 026)

### `access_log`

One row per read of the store by a frontend.

| column | type | notes |
|---|---|---|
| `id` | uuid pk | uuid7 |
| `owner_id` | uuid not null | references `principals` |
| `project` | text | the project the caller resolved; null when none |
| `session_id` | text | null when the caller has none |
| `source` | text not null | `cli`, `mcp`, `hook` |
| `op` | text not null | `search`, `get`, `handoff` |
| `query_len` | int | characters of the query text; null for `get` |
| `tier` | text | `exact`, `semantic`, `fuzzy`, `none`; null for `get`/`handoff` |
| `hits` | int not null | results returned (0 or 1 for `get`/`handoff`) |
| `entry_ids` | uuid[] not null | ids returned or opened |
| `elapsed_ms` | int not null | wall time inside the service |
| `at` | timestamptz not null | `clock_timestamp()` |

Index: `(owner_id, at)`, and `(owner_id, session_id)` for the follow-through
join.

- `services/search.find` gains `source: str | None = None`, plus
  `session_id` and `project` keyword arguments used only for the log. When
  `source` is None nothing is written. `tier` is the tier that produced the
  returned hits (`Hit.match` of the first hit), or `none` for an empty
  result. A listing query (no text) is logged with `tier` null.
- `get_entry` (CLI `bag get` / MCP `get_entry`) and `bag handoff latest`
  log through the same service helper, `services/usage.log_access`.
- **The insert is fail-soft inside the service**: any exception is
  swallowed (and reported to stderr behind `BAG_HOOK_DEBUG`). A search
  must never fail because its log row could not be written. The insert
  runs in a savepoint so a failure does not poison the caller's
  transaction.

### `injection_log`

One row per session start that reached the database.

| column | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `owner_id` | uuid not null | |
| `project` | text not null | |
| `harness` | text not null | `claude-code`, `opencode`, `cursor` |
| `session_id` | text | |
| `found` | bool not null | a knowledge base existed |
| `rules`, `notes` | int not null | what the rendered block carried |
| `chars` | int not null | length of the block |
| `tokens_est` | int not null | `chars / 4`, always labelled an estimate |
| `budget_chars` | int not null | `BAG_MAX_CHARS` at the time |
| `entry_ids` | uuid[] not null | entries rendered into the block |
| `at` | timestamptz not null | |

- `context.injection()` gains `log: InjectionLog | None = None` (harness and
  session id). The Claude Code hook and `bag hook context` pass it; nothing
  else does. Same fail-soft insert as above.
- `Injection` gains `entry_ids` so the log does not re-resolve the block.
- `RulesExceedBudget` raises before anything is logged, as it raises before
  anything is known today. That failure stays visible through
  `bag record status` and the statusline, not here.

### Session ids

The CLI reads `CLAUDE_CODE_SESSION_ID`, which Claude Code sets in the Bash
environment. `mcp_server.py` currently reads `CLAUDE_SESSION_ID`; the
implementation checks which name the MCP server's environment actually
carries and fixes the read if it is wrong. Neither frontend guesses: absent
means null.

## Section 2 - `services/stats.py`

`collect(store, owner_id, project, now, window=timedelta(days=7)) -> Stats`,
a frozen dataclass with one field per section. Each section is collected
independently; one that raises becomes `Unavailable(reason)` and the rest
still render.

Collection runs with `statement_timeout = 1500ms` set locally for its reads,
so the whole thing stays well under the SessionStart hook's 10-second
timeout.

Store and retrieval numbers are given for **this project** and **all
projects**; the pipeline sections are global, as `bag record status` is.

| section | contents | source |
|---|---|---|
| `store` | live entries by kind and by origin, superseded count, knowledge bases, distinct projects | new `Store.entry_counts` |
| `retrieval` | reads by source and op, hit rate (`hits > 0`), tier mix, median `elapsed_ms`, sessions that read at least once out of sessions injected | new `Store.access_stats` |
| `injection` | sessions injected, mean rules / notes / `tokens_est`, current budget fraction (the `kb.rules_chars` arithmetic), **follow-through** | new `Store.injection_stats` |
| `vectors` | embedded / total for the configured model, backlog | existing embed pending count |
| `extraction` | jobs by state, entries with origin `extracted` created in the window, sessions awaiting, model | `extraction.awaiting_sessions` + job counts |
| `transcripts` | last run, sessions and subagent files stored, backlog | `transcripts.status` |
| `memory` | last run spelling and trigger, conflict sidecars | `memory` status |
| `ingest` | last run spelling | `ingest` status |
| `recent` | last N entries written: age, kind, origin, project, id prefix, title | new `Store.recent_entries` |

Wherever a status function already exists it is reused, so `bag stats` and
the command that already reports that thing can never disagree.

**Follow-through** is the share of injected entry ids that the same session
later opened (appeared in an `access_log.entry_ids` for that `session_id`
after the injection). It is a proxy for "was the injected context useful",
not a quality score: a rule can be obeyed from its summary without ever
being opened. It is rendered with that name and nothing grander.

**Since.** When the earliest `access_log` / `injection_log` row is inside
the window, those sections say "since <date>" instead of "7d", so a fresh
install does not read as "nobody recalls anything". There is no backfill.

Owner scoping: every new query filters on `owner_id`; a second principal's
rows are never counted.

## Section 3 - rendering, CLI, errors, testing

### Banner

`context.banner(got, stats=None)`. The first line is exactly today's. With
`stats`, labelled lines follow:

```
saddlebag · kb saddlebag: 22 rules, 8 notes · recording on · handoff: transcript-capture (5m ago)
store     1,204 live (this project 612) · 41 rules · 9 kbs · 12 projects · 318 superseded
recall    7d: 48 reads in 11/14 sessions · hit 92% · exact 55% semantic 41% fuzzy 4% · p50 180ms
inject    7d: 14 sessions · ~4.1k tokens avg · budget 61% · follow-through 6%
vectors   1,158/1,204 embedded (bge-small) · backlog 46
extract   43 done · 0 failed · 0 waiting · 7d +12 entries (sonnet)
transcr   last import 2h ago · 155 sessions, 288 subagents · backlog 0
memory    last sync 1d ago (auto) · 0 conflicts
ingest    last run 3h ago, clean
recent    2m   note  01a0ac4c  Reranker follow-up: candidate depth...
          40m  note  01a0ac2e  Reranker experiment: cross-encoder...
```

(Figures illustrative.) An `Unavailable` section renders as
`<label>  unavailable`. The recent list shows 10 entries.

`hook.session_start` collects stats in the same database session as the
injection, after it, and hands both to `render_output`. If collection
fails as a whole the banner is today's single line. A missing knowledge
base still gets its banner and still gets stats.

Rendering is pure (`stats.render(stats, include_injection=True) ->
list[str]`) and shared by the banner and the CLI. Prose uses spaced hyphens
and `·`; no em dashes.

### `bag stats`

- `--project` (default: the current directory's project), `--window`
  (`7d`, `24h`, `30d`), `--recent N` (default 10), `--json`.
- Same rendering as the banner, without the kb/recording/handoff line.
- `--json` is one object whose keys are identical in every state; an
  unavailable section is `null` with its reason under `unavailable`.
- Fail-loud: an unreachable database exits 1. An unavailable section still
  exits 0 - "I could not tell" is not a failure.

### Frontend wiring

- `cli.py` `search`, `get`, `handoff latest`: pass `source="cli"`,
  `session_id` from `CLAUDE_CODE_SESSION_ID`, and the resolved project.
- `mcp_server.py` `recall`, `get_entry`: `source="mcp"`.
- `bag hook context` and the Claude Code SessionStart hook: pass
  `InjectionLog` with their harness.
- `scripts-eval-retrieval.py`: unchanged, therefore never logged.

### Testing

- `stats.render` - pure, no `db` marker: every section, `Unavailable`,
  "since" labelling, zero-denominator rates, the 10-line recent list.
- Store methods - `db`: counts, tier mix, median, follow-through join;
  each with a second principal's rows present, asserted never counted.
- `find` writes a row only when `source` is given; a log insert that raises
  does not fail the search or poison the transaction. Watch this guard go
  red by reverting it before trusting it.
- `injection_log` written by both the hook and `bag hook context`, and not
  when `RulesExceedBudget` raises.
- A section that raises becomes `Unavailable` and the others render.
- Banner falls back to one line when collection fails; `additionalContext`
  is byte-identical with and without stats.
- `bag stats --json` has the same keys in every state.
- Any test reaching `hook.session_start` keeps stubbing the spawn helpers.
- `make check` green with a zero skip count for `db` tests.

### Documentation

CLAUDE.md gains a "Usage statistics" section recording: metadata only,
never query text; logging only on an explicit `source`; follow-through is a
proxy; stats are fail-soft in the hook and loud in `bag stats`; the logs
are not behind the record opt-in and why that is acceptable.

## Out of scope

- Backfilling from `events`.
- Pruning the logs (they are small; `bag events prune` does not touch them).
- Logging query text, or any quality judgement beyond follow-through.
- A reranker (measured 2026-09-16, notes 01a0ac2e and 01a0ac4c; parked).
