# LINE MCP：隔離的合成資料示範包

這是本機 prototype source/test bundle，並非任何平台已安裝的插件。只可回傳程式產生的合成資料；`real`、帳號 DB 路徑與「privacy ready」開關均不支援。沒有連線到 ChatGPT、Gemini 或其他供應商。

## 先保持隔離

- 解壓到新的獨立資料夾，放在原 LINE repo 及其父層設定之外。
- 本包刻意不含原 `.mcp.json`、`.claude` 技能、`settings.json`、`line_mcp_server.py` 或 `key_extractor.py`。沒有任何自動載入設定。
- 使用獨立的本機 MCP 測試 profile。不要讓 host 自動載入其他專案或全域已註冊的真實 LINE 工具；本包不能停用你原有的 host 設定。
- 只執行本包的 `plugin_adapter.py`；不要改用原 raw reader、手動補入帳號檔案或把整個原 repo 當作 plugin 上傳。

## 本機準備

在此獨立資料夾建立 Python 3.11/3.12 虛擬環境，依鎖檔安裝。Windows 使用相應 `.venv\Scripts\python.exe`，其他系統使用 `.venv/bin/python`。

```sh
python -m venv .venv
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-test.txt
python -m pytest -q
```

應將上述 `python` 換成新虛擬環境的執行檔。mock 使用標準 SQLite；沿用的鎖檔還包含原核心依賴，但沒有呼叫其真實帳號路徑。

stdio server 由 MCP client 啟動，不會開網頁或網路 port：

```sh
python plugin_adapter.py
python plugin_adapter.py --mode mock
```

第一行預設 disabled；第二行只讀合成 fixture。沒有 local server URL、tunnel、API key 或平台身分。停止 client 即結束本次測試；不要重啟來逃避輸出預算。

- 本機設計預設：10 個受理呼叫／分鐘、2 秒冷卻、1 個 backend 讀取並行。
- 六工具：狀態、合成聊天室、歷史、literal 搜尋、近似未讀、合成聯絡人。
- 每次相關讀取前先詢問具名接收方、範圍及去識別／原文；缺選或改範圍只回 requires_confirmation。兩種選擇都只是合成示範，human_confirmation_verified 永遠 false，去識別處理器尚未實作。
- 狀態工具會給固定 fixture 日期。`fetched_at` 是 UTC 回覆時間，`source_sync_at` 固定 null，並非即時 LINE 同步。
- 所有交付資料有 `synthetic: true`。不應把範例報價、交期或人名當成使用者資料。

詳見 [準備與使用步驟](docs/PLUGIN_PREPARATION.md)、[平台矩陣](adapters/README.md)、[隱私 gate](docs/PRIVACY_READINESS.md) 與 [共用 mock Skill](plugin/skills/line-summary-mock/SKILL.md)。Skill 是指令層，不建立連線或授權。

本包可跑合成測試，不代表某個 host／手機已可用，也不包含 macOS 真實讀取器或 OpenChat 討論串修復。真實資料入口仍未實作。

## 可直接跑的合成 CLI

```sh
python tools/mock_cli.py status
python tools/mock_cli.py self-test
python tools/mock_cli.py chats --interactive
```

這個 helper 透過真正 stdio MCP 呼叫六工具，不連供應商或真實 LINE。每次 CLI 都是新子程序；跨頁請用同一 MCP client session，不重用別次 CLI 的 cursor。詳細逐平台流程、驗收、費用與撤銷見 [完整使用手冊](docs/DETAILED_USAGE_GUIDE.md)。
