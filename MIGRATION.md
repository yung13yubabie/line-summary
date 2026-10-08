# Safety migration

This is a breaking local safety repair. Update clients and the skill together;
old clients that expect bare arrays or automatic saving are incompatible. No
real Windows/LINE integration test was run for this repair, and there is no
claim that the live LINE database schema or process-memory access was verified.

## 1. Replace the project skill

The only tracked project skill is now:

```text
.claude/skills/line-summary/SKILL.md
```

The old `skills/line-summary/SKILL.md` was moved, not retained as a second source.
The new file has YAML `name` and `description` frontmatter. If you previously
made a personal copy under `~/.claude/skills/`, review and update it yourself so
it does not override the repaired project skill. MCP user-scope registration
does not install a user-scope skill. See the official [Claude Code skills
locations](https://code.claude.com/docs/en/skills#choose-where-skills-load) and
[MCP project scope](https://code.claude.com/docs/en/mcp#project-scope) references.

## 2. Opt in to a specific local account and scope

Copy `settings.example.json` to `settings.json` next to `line_mcp_server.py`, then
edit it yourself. Missing settings now mean access is disabled. Review existing
settings rather than merging blindly: unknown keys fail validation, and the old
`require_consent` key is no longer accepted. An interactive consent prompt cannot
run safely on the MCP stdio protocol; explicit local enablement and scope are
now the configuration boundary.

- Set `enabled: true` only after reviewing the data flow and permissions.
- Set `db_path` to the intended account's local `.edb` (prefer an absolute path).
  Automatic largest-file/account selection is no longer used by the server.
- Set `allowed_chat_ids` to the explicit IDs to summarize. The empty default
  grants no chat access. Up to 100 IDs are supported; there is no wildcard grant.
- Leave `allow_chat_discovery: false` normally. If you need IDs, temporarily
  enable it and query only the relevant names. This exposes chat metadata to
  the host/model, but never authorizes history or unread content outside the
  allowlist. After selecting IDs, disable discovery again.
- Leave `allow_contacts: false` unless you separately need address-book lookup.
  Disabling that tool does not redact already-resolved sender names from
  permitted messages.
- Optional `allowed_since` / `allowed_until` accept aware ISO 8601 or `null` and
  constrain history requests. If either is configured, unread requests are
  denied so an approximate sample cannot escape that date scope. Use authorized
  date-scoped history instead.

Configuration is frozen on the first tool call, including a denied call. The
user must restart the server after an intentional configuration change. An
assistant must not edit permissions or restart/reset it to get around a denial
or depleted budget. This policy is not an OS sandbox against direct file or
shell access; configure the host's separate permissions appropriately.

### Caps

| Setting | Default and hard maximum | Minimum |
| --- | ---: | ---: |
| `max_messages_per_call` | 500 | 1 |
| `max_response_bytes` | 262144 | 2048 |
| `max_session_messages` | 5000 | 1 |
| `max_session_bytes` | 2097152 | 2048 |
| `max_range_days` | 31 | 1 |

“Session” means one server process, shared across tools. Byte accounting uses
serialized JSON payload bytes, not MCP transport framing. Contact/chat metadata
counts against bytes; history and unread also consume message counts. Repeated
reads consume the budget again. Caps may be lowered, not raised above these
ceilings. Exhaustion requires an explicit partial-coverage result, not automatic
retries, date splitting, tool switching, or process restarts.

## 3. Consume page envelopes

This repair does not add message-body keyword search, a ChatGPT @ plugin, or a
remote bridge. Chat-name lookup is not full-text search. Those are separate
future scopes; no new endpoint, remote authorization, or public service was created.

All four tools now return an object with these fields:

```json
{
  "items": [],
  "has_more": false,
  "next_cursor": null,
  "content_complete": true
}
```

`content_complete` means delivered items were not cut short; it is not an
all-pages, full-history, unread-boundary, or sync guarantee. Tool-specific
coverage and consistency fields also apply. Follow `next_cursor` unchanged while
`has_more` is true, preserving the same query/filter scope. Cursors are signed,
process-local, and query-bound; a changed scope or restarted reader invalidates
them. Metadata pages use stable ID order rather than the old latest-chat order,
and remain live between calls.

Oversized JSON pages lose trailing **whole items**, not parts of strings. The
cursor reflects the last item actually delivered. If the first remaining item
cannot fit, the result is:

```json
{
  "items": [],
  "has_more": true,
  "next_cursor": null,
  "content_complete": false,
  "blocked_reason": "item_exceeds_byte_budget"
}
```

Stop on that result and report incomplete coverage. Do not loop on the page,
skip the item, or claim success. Also stop on authorization, budget, cursor, or
other errors and distinguish the received data from what remains unexamined.

### Tool signatures and page sizes

| Tool | Arguments / defaults | Item meaning |
| --- | --- | --- |
| `line_list_chats` | `query=""`, `chat_type=""`, `limit=50`, `cursor=None`; max 500 rows | Chat metadata |
| `line_get_history` | required `chat_id`, `since`, `until`; `limit=100`, `cursor=None`; max 500 messages | Message with `message_id` |
| `line_get_unread` | `limit_chats=20`, `include_official=False`, `per_chat_limit=50`, `cursor=None`; max 100 chats / 500 messages per chat, subject to 500 total messages per call | Chat with approximate local message sample |
| `line_get_contacts` | `query=""`, `limit=50`, `cursor=None`; max 100 rows | Contact ID and display name |

Actual results can be smaller because of local policy, remaining process
budgets, byte limits, or available local rows. Contact lookup is opt-in. Unread
cursors bind `include_official` and `per_chat_limit` as well as the allowlist.

## 4. Change history windows to `[since, until)`

The end is now exclusive. To request a day in Taiwan:

```text
since = 2026-10-08T00:00:00+08:00
until = 2026-10-09T00:00:00+08:00
```

Do not use `23:59:59`; that loses subsecond messages in the final second. Inputs
must include a timezone: positive/negative offsets and `Z` are accepted. The
server converts to UTC epoch milliseconds without floating-point rounding;
sub-millisecond instants are rounded upward to the next integer millisecond for
both bounds, preserving comparison semantics for integer database timestamps.
More than six fractional digits, date-only or naive values are rejected. `until` must be later than `since` and
the interval must stay within the configured range and fixed date scope.

Direct `DbReader.get_history` callers must replace seconds-based `since_ts` /
`until_ts` arguments with **millisecond** `since_ms` / `until_ms` arguments.
Message output timestamps remain `+08:00`; convert them for other display zones.

History pages sort by `(_createdTime, _id)` so equal timestamps do not create
page gaps in an unchanged database. The initial rowid ceiling reduces ordinary
appends; it does not exclude every later insert because SQLite can reuse rowids
after deletion. Existing rows can also be edited or deleted between pages.
`consistency: "live_keyset_scan"`,
`database_changes_may_affect_pagination: true`, and
`coverage: "local_rows_only"` describe this live scan, not a frozen snapshot.
Use `message_id` for deduplication, which does not recover gaps caused by live
changes. A completed scan never proves cloud synchronization or coverage of
messages absent from the local database.

## 5. Remove false unread certainty

Remove reliance on `available_count`, `missing_count`, and `fully_synced`. Each
unread chat now includes:

```text
unread_count                Local LINE unread counter
returned_count              Number of messages actually returned
messages                    Most recent selected local rows
selection                   "latest_local_approximation"
sync_status                 "unknown"
unread_boundary_verified    false
messages_limited            Unread counter exceeds this selection's cap
more_local_messages         Additional local rows exist beyond the sample
```

The returned rows may all be already read. `unread_count - returned_count` is
not a proven count of unsynced messages. Matching counts are not proof of full
sync either. `messages_limited` can be true even when fewer local bodies exist;
it does not prove omitted bodies are available. `more_local_messages` says
nothing about their unread state.

Unread pagination advances across **chats**, not messages within each chat.
Reaching the final chat page cannot repair a limited per-chat sample. The
coverage is `approximate_recent_local_messages`; report unread counters, sample
sizes, unknown sync status, and all limitations. Official-account filtering is
schema-dependent, not a reliable authorization boundary.

## 6. Saving and operational boundaries

The repaired skill replies in the conversation by default. It does not create
`output/`, summaries, or `metadata.json`. Existing outputs are not deleted.
Saving requires an explicit user request, a chosen destination, and a retention
strategy (for example, a private folder with user-managed deletion after a
specific date). There is no automatic cleanup job. `.gitignore` is not access
control and does not stop cloud backups, synchronization, or already tracked
files from being shared.

The server does not intentionally persist or expose keys, but memory may enter
OS paging, hibernation, or crash dumps; secure erasure is not guaranteed. Tool
results enter Claude Code and the configured model host, so a local read-only DB
connection does not make the entire workflow offline or control host retention.

The optional `tools/scroll_backfill.py` remains a separate real-input script.
It cannot verify the visible chat and is not covered by the server allowlist or
budgets. It can cause network downloads, local persistence, and changes in read
status. Never run it automatically to make a summary look complete; read
[its warnings](tools/README.md) before deciding whether to use it.

## 7. Install locked dependencies and keep tests offline

Runtime and test dependencies now have separate hash locks. In the reviewed
Python environment, install runtime packages with:

```text
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
```

For development/testing, install both locks and run the synthetic suite:

```text
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-test.txt
python -m pytest -m "not integration" --cov --cov-report=term-missing
```

The direct inputs are `requirements.in`, `requirements-platform.in`, and
`requirements-test.in`. Runtime pins MCP 1.29.0 and apsw-sqlite3mc 3.50.4.0;
the test lock pins pytest 8.4.2 and pytest-cov 6.2.1. Unused pytest-asyncio was
removed. pywin32 remains a Windows-only transitive MCP dependency, constrained
to 310 for Python below 3.14 and 311 for Python 3.14 and later. These pins do
not constitute live Windows validation.

Maintainers can regenerate the locks with uv 0.12.19, review all changes, and
retest before accepting them:

```text
uv pip compile requirements.in --universal --python-version 3.11 --generate-hashes --no-build -o requirements.txt
uv pip compile requirements-test.in --universal --python-version 3.11 --generate-hashes --no-build -o requirements-test.txt
```

Wheels must exist for the selected platform/Python combination. If a locked
wheel is unavailable, stop and investigate compatibility rather than disabling
hash checks or silently building another source package. The optional
`uiautomation` tool dependency remains outside these locks.

### Verification boundary

Use the synthetic/mock suite for routine checks. Ordinary pytest, including
`-m integration` by itself, skips live tests before platform/LINE detection.
The explicit `--run-live-line` flag is required because those tests access
actual LINE process memory and chat data; the fixture checks this opt-in too.
Do not enable it just because LINE is open, or interpret skipped integration
tests as a live pass. The authored CI workflow is configured for synthetic
Ubuntu and Windows tests on Python 3.11/3.12, never that flag, with no test-artifact uploads;
a workflow file is not evidence that a remote CI run passed.
Publication of the repair branch and a draft PR is separate from validation.
No merge, release, deployment, administrator escalation, or real LINE operation
is performed by the repair workflow.
