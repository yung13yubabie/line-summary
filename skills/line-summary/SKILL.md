# line-summary

Summarizes LINE PC chat history using the `line` MCP server tools.

## Prerequisites
- LINE PC is running
- MCP server `line` registered in `.claude/settings.json`

## Time Conversion (Skill layer -- NEVER pass natural language to MCP tools)

Convert all time references to ISO 8601 with `+08:00` before calling tools:

| User says | ISO 8601 |
|-----------|----------|
| 今天 | {today}T00:00:00+08:00 to {today}T23:59:59+08:00 |
| 昨天 | {yesterday}T00:00:00+08:00 to {yesterday}T23:59:59+08:00 |
| 最近 N 天 | {today-N}T00:00:00+08:00 to now |
| 上週 | last Monday T00:00:00+08:00 to last Sunday T23:59:59+08:00 |

## Round 1 -- Find Chat and Fetch Messages

1. Call `line_list_chats(query="<chat name from user>")`.
   If multiple results, ask user to confirm which one.
   Use `chat_type` to filter: "personal", "group", "multi", "official", "open"

2. Call `line_get_history(chat_id=<id>, since=<ISO>, until=<ISO>)`.
   If result > 1000 messages, split into daily calls.

## Round 2 -- Build Skeleton (internal, not shown to user)

```
話題清單:
1. [題目] -- 主要發言人 -- HH:MM-HH:MM
2. ...

發言統計: 王小明 N則, 李小美 N則 ...

連結: [title 或 URL] -- 分享人
媒體事件: HH:MM [發言人] 傳了 [圖片/貼圖/檔名]
```

## Round 3 -- Full Summary

For each topic expand with quotes and context:

```markdown
## {date} LINE 摘要 -- {chat name}

### 話題一：{topic}
**時間：** HH:MM - HH:MM　**參與：** 王小明、李小美

{2-3 句摘要}

> 「{直接引用}」-- 王小明 14:30

{結論或決定}

---

### 分享連結
- {title 或 URL} -- {發言人} HH:MM

### 發言統計
| 姓名 | 訊息數 |
|------|--------|
| 王小明 | 23 |
```

## Round 4 -- Audit Before Output

- [ ] 每個話題骨架都有對應段落
- [ ] 引用名稱與原始資料一致
- [ ] 有連結訊息則有「分享連結」段
- [ ] 媒體事件有出現在上下文中（非靜默忽略）

## Save Output

Path: `~/line-summary/output/<chat_id>/<YYYY-MM-DD>.md`
Range: `~/line-summary/output/<chat_id>/<YYYY-MM-DD>_<YYYY-MM-DD>.md`

Update `~/line-summary/output/metadata.json`:
```json
{ "<chat_id>": "<display name>" }
```
