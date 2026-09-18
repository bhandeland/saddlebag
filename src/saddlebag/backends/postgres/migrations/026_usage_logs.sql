-- Usage metadata: what saddlebag was asked and what it handed out. Written by
-- the services only when a frontend passes an explicit `source`, so tests,
-- the eval script and internal callers never appear here.
--
-- METADATA ONLY. The query's length is stored, never its text, and no entry
-- content is copied - ids only. That is what lets these tables sit outside
-- the per-project record opt-in (services/record.py): they describe
-- saddlebag's own use, not the user's work.
--
-- Nothing prunes these. Rows are small (one per read, one per session
-- start) and `bag events prune` deliberately does not touch them.

create table access_log (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text,
  session_id text,
  source text not null check (source in ('cli', 'mcp', 'hook')),
  op text not null check (op in ('search', 'get', 'handoff')),
  query_len int,
  -- The tier that produced the returned hits, 'none' for an empty search,
  -- null for a listing query or a non-search op.
  tier text check (tier in ('exact', 'semantic', 'fuzzy', 'none')),
  hits int not null,
  entry_ids uuid[] not null default '{}',
  elapsed_ms int not null,
  at timestamptz not null default clock_timestamp()
);

create index access_log_at_idx on access_log (owner_id, at);
-- The follow-through join: which injected ids did this session open later.
create index access_log_session_idx on access_log (owner_id, session_id)
  where session_id is not null;

create table injection_log (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  harness text not null,
  session_id text,
  found boolean not null,
  rules int not null,
  notes int not null,
  chars int not null,
  -- chars / 4, an estimate and always labelled as one. Stored rather than
  -- derived so a future better estimate does not rewrite history.
  tokens_est int not null,
  budget_chars int not null,
  entry_ids uuid[] not null default '{}',
  at timestamptz not null default clock_timestamp()
);

create index injection_log_at_idx on injection_log (owner_id, at);
