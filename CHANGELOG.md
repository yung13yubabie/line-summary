# Changelog

## Unreleased — Process identity and read-only active WAL repair

### Changed

- Preserve the original stdio entry and four MCP tool names, descriptions and input schemas. Default-disabled policy, explicit database/chat scope and output budgets remain in place.
- Check the target process identity before memory scanning and validate candidates by actual encrypted database reads; a matching hex format alone is not a verified key. Startup and recovery diagnostics do not echo private input or raw backend exceptions.
- Run encrypted reads in a dedicated Windows worker with pinned DB/WAL/SHM identities, read-only mappings and native transaction/lock checks. Missing SHM or inconsistent committed-WAL evidence fails closed instead of returning a stale base database.
- Use bounded worker I/O, cooperative cancellation and explicit cleanup-incomplete errors. An OS process spawn cannot be forcibly bounded by Python timers; a forced native exit does not prove immediate source release.
- Pin MCP 1.29.0 and apsw-sqlite3mc 3.53.3.1 (SQLite 3.53.3); the tested reader combination is Windows x86-64 / CPython 3.11.9.

### Verification and limits

- Independent synthetic core review: 519 passed, 0 failed, 5 live tests deselected, using pytest 8.4.2. Real guarded stdio confirmed the full four-tool contract.
- Four additional native-reader counterexamples used valid synthetic keys and retained SHM while clearing/truncating committed WAL. They first demonstrated a successful encrypted read, then verified rejection, unchanged source bytes and clean child exit after damage.
- Preserved legacy identity (44 passed / 2 failed) and strict candidate-model (632 passed / 32 failed) counterexamples remain reported as failures of those older models; they were not rewritten to inflate the passing source suite. Review groups overlap and must not be summed.
- Local results are separate from CI status. Live LINE accounts, community threads, multimedia, clean-machine installation, other ABI/platform combinations and network drives are unverified.
- No ChatGPT facade, tunnel activation or mock-adapter PR is included. The separate facade remains disabled. Cached data does not prove current LINE login or complete cloud synchronization; lost WAL/SHM history cannot be reconstructed by this repair.

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
