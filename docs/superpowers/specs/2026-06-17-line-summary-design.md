# LINE Summary MCP Server — Design Spec
**Date:** 2026-06-17
**Status:** Draft / Spike Required
**Platform:** Windows 11

---

## 本次具體異動

| 調整點 | 更新內容 |
|--------|----------|
| 安全限制 | `key_extractor` 加互動式確認、禁止落磁碟、錯誤訊息不得含金鑰/聊天內容 |
| Spike 前置 | Status 改 `Draft / Spike Required`，頂部加 Phase 0 五步驟關卡 |
| MCP 套件 | 明確使用 `from mcp.server.fastmcp import FastMCP`，`requirements.txt` 待 Phase 0 後 pin 版本 |
| 時間格式 | MCP tool 層只收嚴格 ISO 8601，自然語言由 Skill 層轉換，時區 `Asia/Taipei` |
| 輸出路徑 | 改為 `chat_id_hash/`，加 `metadata.json` 對照，`.gitignore` 明列 `output/`、`profiles/`、`settings.json` |
| 測試策略 | Phase 0 spike 測試獨立一節，五步驟全通過才進 Phase 1 |

---

## 目標

打造一套以 LINE PC（Windows）為資料來源的訊息摘要系統。透過 MCP Server 讓 Claude 可直接呼叫工具取得 LINE 聊天記錄，再由 Claude Skill 產出每日/區間結構化摘要。支援個人聊天與群組，無需手動匯出。

---

## ⚠️ Spike Required — 開發前必須先通過 Phase 0

**下列 Phase 0 驗證全部本機完成且通過後，才允許開始 MCP Server 與 Skill 開發：**

1. 偵測 LINE.exe PID
2. 讀取可讀 memory region
3. 從記憶體中找到候選 32-char hex 金鑰
4. 用候選金鑰成功解開 `.edb`（wxSQLite3 PRAGMA 確認相容）
5. 列出至少 1 個聊天室名稱

**Phase 0 限制：**
- 不得將金鑰寫入任何檔案或 log
- 不得輸出候選金鑰或任何聊天內容至 terminal（除測試人員本機確認用的最小輸出）
- 通過後立即清除測試輸出

---

## 架構總覽

```
LINE.exe（執行中）
    ↓ ctypes ReadProcessMemory（Win32，本機限定）
key_extractor.py  ──→  32-char hex 金鑰（快取於 process 記憶體，不落磁碟）
    ↓
db_reader.py      ──→  解密 .edb（wxSQLite3）並查詢訊息
    ↓
line_mcp_server.py ──→  MCP Server（官方 mcp Python SDK）暴露 3 個工具
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
6. 用候選金鑰嘗試開 .edb → 驗證正確性（取第一個可成功解密者）
```

**安全限制（強制）：**
- 僅限本機執行，不得透過網路暴露金鑰或資料庫存取介面
- 首次執行必須顯示互動式確認提示，使用者明確同意後才繼續
- 金鑰不得寫入任何檔案、log、環境變數或 stdout
- 錯誤訊息不得輸出候選金鑰或任何聊天內容
- MCP Server 不得將金鑰以任何形式回傳給工具呼叫端

**前提：** LINE.exe 必須在執行中。MCP Server 啟動時執行一次，快取金鑰直到 server 重啟。

---

### 2. `db_reader.py`

**職責：** 使用金鑰解密 `.edb`，提供結構化訊息查詢介面。

**資料庫位置（自動偵測，可於 settings.json 覆寫）：**
```
C:\Users\<user>\AppData\Local\LINE\Data\db\<hash>.edb
```

**依賴（Phase 0 spike 決定最終選擇）：**

| 候選 | 說明 | Windows 風險 |
|------|------|-------------|
| `pysqlcipher3` | 官方，需系統有 libsqlcipher | 高，Windows 常見卡點 |
| `sqlcipher3-binary` / wheels | 預編譯，較易裝 | 中，需確認 wxSQLite3 PRAGMA 相容 |

**wxSQLite3 PRAGMA 設定集中於此模組**。LINE 版本更新時只需改這一個檔案。

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

---

### 3. `line_mcp_server.py`

**依賴：** 官方 `mcp` Python SDK（`from mcp.server.fastmcp import FastMCP`）

**版本 pin（requirements.txt）：**
```
mcp==1.x.x   # 確認 Phase 0 完成後 pin 具體版本
```

```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("line-summary")

@mcp.tool()
def line_list_chats(query: str = "", limit: int = 50) -> list[dict]:
    """列出聊天室（群組＋個人），支援模糊查詢。
    回傳: [{chat_id, name, type, member_count, last_message_at}]
    """

@mcp.tool()
def line_get_history(
    chat_id: str,
    since: str,     # 嚴格 ISO 8601，含時區，例：2026-06-15T00:00:00+08:00
    until: str,     # 嚴格 ISO 8601，含時區
    limit: int = 500
) -> list[dict]:
    """取得指定聊天室的訊息串流（含所有媒體類型及 URL）。"""

@mcp.tool()
def line_get_contacts(query: str = "") -> list[dict]:
    """列出聯絡人，用於名稱解析。
    回傳: [{contact_id, display_name}]
    """
```

**時區規則：**
- MCP tool 層只接受嚴格 ISO 8601（含明確時區偏移）
- 自然語言時間（「2天前」、「上週」）由 Skill 層轉換為 ISO 8601 後再呼叫工具
- 所有時間以 `Asia/Taipei`（UTC+8）為基準

---

### 4. `settings.json`

```json
{
  "db_path": "",
  "output_dir": "~/line-summary/output",
  "media_mode": "placeholder",
  "url_extraction": true,
  "timezone": "Asia/Taipei"
}
```

| 欄位 | 說明 |
|------|------|
| `db_path` | `.edb` 路徑，留空自動偵測 |
| `output_dir` | 摘要輸出根目錄 |
| `media_mode` | `placeholder`（預設）/ `vision`（圖片傳 Claude Vision）/ `skip` |
| `url_extraction` | 永遠 `true`，文字 URL 用 regex，Link Preview 從 DB 取 |
| `timezone` | 時間顯示基準，預設 Asia/Taipei |

---

### 5. `skills/line-summary/SKILL.md`

**前置：Skill 層負責時間轉換**
- 使用者說「最近 3 天」→ Skill 計算為 ISO 8601 再呼叫 `line_get_history`
- 時區一律用 `Asia/Taipei`

**三輪摘要流程：**
- **第一輪（骨架）：** 掃描所有訊息，列出話題清單、發言統計、連結清單
- **第二輪（充實）：** 展開每個話題的完整引文與歸屬，整合圖片/連結脈絡
- **第三輪（稽核）：** 驗證骨架與完成稿對應，確認名稱、引文、分類正確

**輸出路徑（以 chat_id hash 為目錄名，避免群名含非法字元或洩漏）：**
```
~/line-summary/output/
├── metadata.json               # chat_id → 顯示名稱對照（本機維護）
├── <chat_id_hash>/
│   ├── 2026-06-17.md
│   ├── 2026-06-15_2026-06-17.md
│   ├── history.json
│   └── profiles/
│       └── <contact_id_hash>.json
```

---

## .gitignore（必須包含）

```
output/
*.edb
*.db
profiles/
history.json
metadata.json
settings.json
```

---

## 錯誤處理

| 情境 | 處理 |
|------|------|
| LINE.exe 未執行 | 回傳 `{"error": "LINE is not running"}` |
| 記憶體掃描找不到金鑰 | 重試 3 次，建議使用者重新登入 LINE；錯誤訊息不含候選值 |
| wxSQLite3 PRAGMA 不符（LINE 版本更新） | 明確錯誤訊息，提示更新 `db_reader.py`；不輸出 DB 內容 |
| 聊天室查無結果 | 回傳空清單，建議用 `line_list_chats` 確認 ID |
| 圖片本機路徑不存在（vision 模式） | 降級為 `[圖片]` 佔位符，不中斷流程 |
| since/until 非 ISO 8601 | MCP tool 層直接回傳格式錯誤，不嘗試解析 |

---

## 專案結構

```
C:\Users\LIN\line-summary\
├── line_mcp_server.py
├── key_extractor.py
├── db_reader.py
├── settings.json           # gitignore
├── requirements.txt
├── .gitignore
├── skills/
│   └── line-summary/
│       └── SKILL.md
├── output/                 # gitignore
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

### Phase 0（Spike — 必須先通過）

| 步驟 | 驗證目標 |
|------|---------|
| 偵測 LINE PID | `EnumProcesses` 找到 LINE.exe |
| 讀取 memory region | `VirtualQueryEx` + `ReadProcessMemory` 無 access denied |
| 找候選金鑰 | 正則找到至少一個 32-char hex 字串 |
| 解開 .edb | wxSQLite3 PRAGMA 設定正確，可開啟資料庫 |
| 列出聊天室 | 至少回傳 1 筆真實聊天室記錄 |

**Phase 0 通過後才進入 Phase 1。**

### Phase 1（元件測試）

| 層次 | 內容 |
|------|------|
| `key_extractor` | Mock ReadProcessMemory，驗證 hex pattern 辨識邏輯 |
| `db_reader` | 用已知明文測試 `.db` 驗證查詢 SQL 正確 |
| MCP 工具 | MCP test client，驗證回傳格式符合 Skill 預期 |
| 時區轉換 | Skill 層自然語言 → ISO 8601 邊界測試 |

---

## 已知限制與未來擴充

- **LINE 版本依賴：** wxSQLite3 金鑰格式若改變需重新驗證 `db_reader.py`
- **Windows 限制：** key_extractor 僅支援 Windows（wx-cli 同樣限制）
- **URL 後續：** 網站內容擷取與搜尋整合留待下一輪設計
- **媒體後續：** 影片描述、音訊轉文字留待下一輪設計
- **pysqlcipher3 依賴：** Windows 安裝可能需要額外步驟，Phase 0 確認後 pin 版本
