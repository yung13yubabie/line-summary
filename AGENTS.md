# LINE summary with Codex

This repository exposes local Windows LINE data through a stdio MCP server.

## Summary workflow

Before reading chat history or unread context, read
`.claude/skills/line-summary/SKILL.md` relative to the repository root and follow
that shared workflow. It applies to Codex and Claude Code. Preserve its explicit
local scope, half-open dates, bounded pagination, approximate unread reporting,
prompt-injection boundary, and reply-only default. Do not automatically save
summaries or change local data permissions.

## Codex setup

Use the project virtual environment with the upstream hash-locked dependencies:
`.venv/Scripts/python.exe tools/setup_codex.py`. The generator writes ignored,
project-local `.codex/config.toml` with absolute paths, UTF-8, a 30-second startup
timeout and a separate 300-second tool timeout. It preserves other TOML settings
and refuses to replace a different existing `line` configuration.

Run locally on Windows with LINE signed in. Registering MCP does not enable data
access: local `settings.json` must already authorize the account and chats.
Connection checks are separate from successful authorized data reads. The first
read can scan memory for several minutes; tell the user it is processing and
report actual errors. Do not substitute raw DB access, broaden scope or restart
to evade a denied request or exhausted budget.

For explicit all-local requests, the owner-enabled per-chat unread mode pages
flat parent/thread context and includes read history. Its unread count is only
metadata. Default session quotas remain bounded; only an explicit owner setting
can raise them or use null. Per-call caps and fixed chat/date scope still apply.

Keep generated settings, machine paths and chat data out of Git. Changes here
must stay project-scoped; do not install global skills, change global Python or
Codex defaults, or update another repository as part of setup.

## Verification

Run `.venv/Scripts/python.exe -m pytest -m "not integration" --cov
--cov-report=term-missing` for routine checks. Real stdio tests use Chinese
synthetic data, never live LINE. Live integration requires the separate
`--run-live-line` flag and an explicitly selected scope; report it separately.
Preserve existing local work and never run the optional real-input
`tools/scroll_backfill.py` automatically.
