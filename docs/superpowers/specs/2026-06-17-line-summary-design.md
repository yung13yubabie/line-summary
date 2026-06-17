# LINE Summary MCP Server — Design Spec
**Date:** 2026-06-17  
**Status:** Approved  
**Platform:** Windows 11

---

## 目標

打造一套以 LINE PC（Windows）為資料來源的訊息摘要系統。透過 MCP Server 讓 Claude 可直接呼叫工具取得 LINE 聊天記錄，再由 Claude Skill 產出每日/區間結構化摘要。支援個人聊天與群組，無需手動匯出。

---

## 架構總覽

```
LINE.exe（執行中）
    ↓ ctypes ReadProcessMemory（Win32）
key_extractor.py  ──→  32-char hex 金鑰（快取於 process 記憶體，不落磁碟）
    ↓
db_reader.py      ──→  解密 .edb（wxSQLite3）並查詢訊息
    ↓
line_mcp_server.py ──→  FastMCP 暴露 3 個工具給 Claude
    ↓
line-summary SKILL.md  ──→  三輪 AI 摘要，輸出 Markdown 檔
```

---

## 元件說明

### 1. `key_extractor.py`

**職責：** 從 LINE.exe 進程記憶體取得 wxSQLite3 加密金鑰。

**原理：**
- LINE PC Windows 使用 wxSQLite3 加密本地 `.edb` 資料庫
- 金鑰為 32-char hex 字串，LINE 執行時快取於進程記憶體中
- 依據 2024 年數位鑑識研究論文（MDPI）確認的掃描方式

**實作：**
```
1. EnumProcesses() → 找 LINE.exe PID
2. OpenProcess(PROCESS_VM_READ) → 取得 handle
3. VirtualQueryEx() → 迭代所有記憶體區段
4. ReadProcessMemory() → 讀取每個可讀區段
5. 正則搜尋 [0-9a-f]{32} → 找金鑰候選
6. 用候選金鑰嘗試開 .edb → 驗證正確性
```

**前提：** LINE.exe 必須在執行中。MCP Server 啟動時執行一次，快取金鑰直到 server 重啟。

---

### 2. `db_reader.py`

**職責：** 使用金鑰解密 `.edb`，提供結構化訊息查詢介面。

**資料庫位置：**
```
C:\Users\<user>\AppData\Local\LINE\Data\db\<hash>.edb
```

**依賴：** pysqlcipher3 或 sqlcipher-python，配合 wxSQLite3 相容的 PRAGMA 設定。

**查詢目標表（依研究論文逆向結果）：**
- `chat` / `group`：聊天室清單、群組資訊
- `message`：訊息內容、發送者、時間戳、訊息類型
- `contact`：聯絡人顯示名稱對應

**回傳格式（`line_get_history`）：**
```json
[
  {
    "type": "text",
    "sender": "王小明",
    "content": "你看這個 https://youtu.be/xxx",
    "urls": ["https://youtu.be/xxx"],
    "sent_at": "2026-06-17T14:30:00+08:00"
  },
  {
    "type": "image",
    "sender": "王小明",
    "content": null,
    "local_path": "C:/Users/.../bgChat/img_xxx.jpg",
    "sent_at": "2026-06-17T14:30:05+08:00"
  },
  {
    "type": "link",
    "sender": "李小美",
    "url": "https://news.cts.com.tw/xxx",
    "title": "新聞標題",
    "description": "新聞摘要...",
    "sent_at": "2026-06-17T15:00:00+08:00"
  },
  {
    "type": "sticker",
    "sender": "陳阿美",
    "content": "[貼圖]",
    "sent_at": "2026-06-17T15:01:00+08:00"
  },
  {
    "type": "file",
    "sender": "張大偉",
    "content": null,
    "filename": "合約.pdf",
    "sent_at": "2026-06-17T15:05:00+08:00"
  }
]
```

**維護隔離：** wxSQLite3 PRAGMA 設定集中於此模組。LINE 版本更新時只需改這一個檔案。

---

### 3. `line_mcp_server.py`

**職責：** FastMCP server，暴露三個工具給 Claude。

```python
@mcp.tool()
def line_list_chats(query: str = "", limit: int = 50) -> list[dict]:
    """列出聊天室（群組＋個人），支援模糊查詢。
    回傳: [{chat_id, name, type, member_count, last_message_at}]
    """

@mcp.tool()
def line_get_history(
    chat_id: str,
    since: str,           # ISO 8601 或自然語言如 "2天前"
    until: str = "now",
    limit: int = 500
) -> list[dict]:
    """取得指定聊天室的訊息串流（含所有媒體類型及 URL）。"""

@mcp.tool()
def line_get_contacts(query: str = "") -> list[dict]:
    """列出聯絡人，用於名稱解析。
    回傳: [{contact_id, display_name}]
    """
```

---

### 4. `settings.json`

```json
{
  "db_path": "",
  "output_dir": "~/line-summary/output",
  "media_mode": "placeholder",
  "url_extraction": true
}
```

| 欄位 | 說明 |
|------|------|
| `db_path` | `.edb` 路徑，留空自動偵測 |
| `output_dir` | 摘要輸出目錄 |
| `media_mode` | `placeholder`（預設）/ `vision`（圖片傳 Claude Vision）/ `skip` |
| `url_extraction` | 永遠 `true`，文字 URL 用 regex，Link Preview 從 DB 取 |

> **注意：** `vision` 模式僅對本機有快取圖片的訊息有效；貼圖、影片仍使用佔位符。後續整合網站擷取/搜尋時，URL 處理邏輯在此擴充。

---

### 5. `skills/line-summary/SKILL.md`

**三輪摘要流程（仿 baoyu-wechat-summary）：**

- **第一輪（骨架）：** 掃描所有訊息，列出話題清單、發言統計、連結清單
- **第二輪（充實）：** 展開每個話題的完整引文與歸屬，整合圖片/連結脈絡
- **第三輪（稽核）：** 驗證骨架與完成稿對應，確認名稱、引文、分類正確

**輸出路徑：**
```
~/line-summary/output/<chat_name>/
├── 2026-06-17.md               # 單日摘要
├── 2026-06-15_2026-06-17.md    # 區間摘要
├── history.json                # 最近摘要指標（增量用）
└── profiles/                   # 群友畫像（跨次累積）
```

---

## 錯誤處理

| 情境 | 處理 |
|------|------|
| LINE.exe 未執行 | 回傳 `{"error": "LINE is not running"}` |
| 記憶體掃描找不到金鑰 | 重試 3 次，建議用戶重新登入 LINE |
| wxSQLite3 PRAGMA 不符（LINE 版本更新） | 明確錯誤訊息，提示更新 `db_reader.py` |
| 聊天室名稱查無結果 | 回傳空清單，建議用 `line_list_chats` 確認名稱 |
| 圖片本機路徑不存在（vision 模式） | 降級為 `[圖片]` 佔位符，不中斷流程 |

---

## 專案結構

```
C:\Users\LIN\line-summary\
├── line_mcp_server.py
├── key_extractor.py
├── db_reader.py
├── settings.json
├── requirements.txt
├── skills/
│   └── line-summary/
│       └── SKILL.md
├── output/                     # 摘要輸出（gitignore）
└── docs/
    └── superpowers/
        └── specs/
            └── 2026-06-17-line-summary-design.md
```

---

## 安裝與啟動

```bash
pip install -r requirements.txt
# LINE.exe 必須先執行
python line_mcp_server.py
```

**Claude Code MCP 設定（`.claude/settings.json`）：**
```json
{
  "mcpServers": {
    "line": {
      "command": "python",
      "args": ["C:/Users/LIN/line-summary/line_mcp_server.py"]
    }
  }
}
```

---

## 測試策略

| 層次 | 內容 |
|------|------|
| `key_extractor` | Mock ReadProcessMemory，驗證 hex pattern 辨識邏輯 |
| `db_reader` | 用已知明文測試 `.db` 驗證查詢 SQL 正確 |
| MCP 工具 | FastMCP test client，驗證回傳格式符合 skill 預期 |
| 整合測試 | LINE 執行中，跑 `line_list_chats` 確認回傳真實資料 |

---

## 已知限制與未來擴充

- **LINE 版本依賴：** wxSQLite3 金鑰格式若改變需重新驗證
- **Windows 限制：** key_extractor 僅支援 Windows（wx-cli 同樣限制）
- **URL 後續：** 網站內容擷取與搜尋整合留待下一輪設計
- **媒體後續：** 影片描述、音訊轉文字留待下一輪設計
