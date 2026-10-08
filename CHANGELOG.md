# Changelog

## 2026-10-08 — Core safety repair (breaking API)

Based on `bd3569e80af56cff342cab9690e6a6d0cb2f4f93`.

### Changed

- All MCP tools return page envelopes with explicit continuation/coverage metadata.
- History uses timestamp plus unique message ID keyset pagination, signed query-bound cursors, and millisecond `[since, until)` ranges accepting positive/negative timezone offsets.
- Unread samples explicitly report approximation and unknown synchronization; removed misleading `fully_synced`, `available_count`, and `missing_count` fields.
- Default-disabled server policy requires an explicit account database and chat allowlist. Discovery/contacts are separate opt-ins. Date-scoped configurations deny unread sampling. Per-call/process message and JSON-byte budgets are enforced.
- Chat/contact metadata pagination and malformed-ID checks fail closed; oversized items are reported without silently skipping them.
- The project skill now lives under `.claude/skills/line-summary/`, rejects instructions embedded in chat data, and defaults to replying without saving summary files.
- Runtime/test requirements are separate, exact-version/hash locked; MCP stays on 1.x and APSW on the 3.50 cipher line.
- Ordinary pytest, including `-m integration` alone, skips all real LINE tests before platform/process detection. Explicit `--run-live-line` is required for live access.
- Added synthetic-only CI for Linux/Windows and Python 3.11/3.12. No live LINE, credentials, production database, UI automation, or artifact upload is used by CI.

### Verification and limits

- Local Linux Python 3.12: 122 passed, 5 live tests skipped; runtime coverage 88.81% with branch measurement, retaining the 85% threshold.
- The patch was checked, applied to a clean baseline, and retested successfully. Independent security review reproduced and verified fixes for scope, cursor, minimum-byte-budget and null-ID regressions.
- CI results must be read from the exact PR head checks; a workflow definition alone is not a passing run.
- The upstream FastMCP/Pydantic lifespan warning remains documented. No real Windows LINE/schema/keys/network behavior was validated.
- History remains a live scan, not a snapshot or cloud-sync guarantee. Rowid reuse, edits and deletions can affect pagination while the database changes.
- Local read-only access does not prevent MCP host/model transmission or retention; memory-only key handling does not rule out OS swap or crash dumps.
- Message full-text search, a ChatGPT @ plugin, remote bridging, and de-identification/sensitive-data filtering are not implemented by this repair.

See [MIGRATION.md](MIGRATION.md) before upgrading clients or local settings.
