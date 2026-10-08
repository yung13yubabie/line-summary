# Changelog

## 2026-10-08 — Core safety repair (breaking API)

### Changed

- All MCP tools return page envelopes with explicit continuation/coverage metadata.
- History uses timestamp plus unique message ID keyset pagination, signed query-bound cursors, and millisecond `[since, until)` ranges accepting positive/negative timezone offsets.
- Unread samples explicitly report approximation and unknown synchronization; removed misleading `fully_synced`, `available_count`, and `missing_count` fields.
- Default-disabled server policy requires an explicit account database and chat allowlist. Discovery/contacts are separate opt-ins. Date-scoped configurations deny unread sampling. Per-call/process message and JSON-byte budgets are enforced.
- Chat/contact metadata pagination and malformed-ID checks fail closed; oversized items are reported without silently skipping them.
- The project skill now lives under `.claude/skills/line-summary/`, rejects instructions embedded in chat data, and defaults to replying without saving summary files.
- Runtime/test requirements are separate, exact-version/hash locked; MCP stays on 1.x and APSW on the 3.50 cipher line.
- Automated tests default to synthetic data; real LINE access requires separate explicit opt-in.
- CI uses synthetic data only, without real LINE access or artifact uploads.

### Compatibility and limits

- Real Windows LINE access, database schema compatibility, and network behavior remain unverified by this release.
- The upstream FastMCP/Pydantic lifespan warning remains.
- History remains a live scan, not a snapshot or cloud-sync guarantee. Rowid reuse, edits and deletions can affect pagination while the database changes.
- Local read-only access does not prevent MCP host/model transmission or retention; memory-only key handling does not rule out OS swap or crash dumps.
- Message full-text search, a ChatGPT @ plugin, remote bridging, and de-identification/sensitive-data filtering are not implemented.

See [MIGRATION.md](MIGRATION.md) before upgrading clients or local settings.
