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

**Range rules (avoid silently capping the day):**
- "今天" spans the WHOLE day (`...T23:59:59`), NOT "up to the current moment" —
  the DB reads live data (WAL), so a full-day `until` captures everything so far.
- If the user gives no range, default to 今天 and TELL them the window you used.
- ALWAYS state the exact `since ~ until` window in the summary header, so the
  covered range is never ambiguous.

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

Scannable at a glance: an identity block (WHICH chat), topic sections
(the MESSAGES), then a distinct links section (the LINKS). Keep the three
visually separate so a reader instantly finds chat / content / links.

```markdown
## 📋 {chat name} — {date} 每日摘要
**類型：** {個人/群組/開放聊天室}　**時間範圍：** {since} ~ {until}　**訊息數：** {N} 則

### 🧵 話題一：{topic}（HH:MM–HH:MM）
**主要發言：** 王小明、李小美

{2-3 句摘要}

> 「{直接引用}」— 王小明 14:30

{結論或決定}

### 🔗 分享連結
| 連結 | 分享者 | 時間 |
|------|--------|------|
| {title 或 URL} | 王小明 | 14:30 |

### 💡 今日乾貨與延伸
{先萃取對話裡真的可行動、可學的知識點（工具、做法、踩過的坑），
每點一句話。然後在能加值的地方，加上你自己的研判與延伸——相關工具、
更進一步的做法、要注意的風險。像站在他們的討論上再往前推一步。
沒有值得學的就寫「今日無」，不要硬湊。}

### 📊 發言統計
| 發言者 | 訊息數 |
|--------|--------|
| 王小明 | 23 |
```

延伸段落只在有料時寫，且要標清楚哪些是原對話、哪些是你補的判斷，
不要把自己的推論混進引用裡。

Notes:
- 個人/群組聊天的發言者名稱來自 `_contact`；開放聊天室來自 `_squareMember`
  (both resolved by db_reader). Always show the resolved NAME, never the raw mid.
- 連結一律獨立成「🔗 分享連結」表，不埋在話題內文，方便一眼掃到。

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
