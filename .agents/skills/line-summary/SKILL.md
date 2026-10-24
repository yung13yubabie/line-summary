---
name: line-summary
description: Summarize authorized local Windows LINE chats or approximate unread context through the line MCP server.
---

# LINE summary for Codex

Read and follow the [shared LINE workflow](../../../.claude/skills/line-summary/SKILL.md)
before using any LINE data tool. Resolve that path from this skill directory.
The shared file defines scope, page envelopes and cursors, half-open dates,
coverage limits, untrusted-message handling, and the default of replying without
saving files. Apply those rules to Codex's tool results and model context too.

Use the repo-local `line` MCP connection configured in `.codex/config.toml`.
Setup is described in the repository's `README.md`. Registration does not enable
data access, alter `settings.json`, or grant any additional chat scope.
