# line-summary

透過 MCP 讀取本機 LINE 記錄的工具，以及獨立的 **experimental／mock-only** 插件原型。原 Windows 入口保留；新 adapter 目前只能回傳合成資料，尚未接通真實 LINE 或任何模型平台。

## 兩個入口

- `line_mcp_server.py`：原 Windows reader，依本機 `settings.json` 的聊天室 allowlist、日期與輸出預算唯讀查詢。`.mcp.json` 及原 Claude Code Skill 仍指向這個入口，四工具與參數維持原樣。
- `plugin_adapter.py`：獨立 stdio MCP adapter，預設 `disabled`；只有明確選用 `mock` 才建立暫存合成 SQLite。`real`、`db_path`、未知設定與「privacy ready」開關都會遭拒絕，不會回退到原 reader。

兩者共用 `DbReader`。新增 adapter 經合成範圍檢查、mock 選擇協商及程序內流量／輸出預算後回覆；共用 reader 追加字面搜尋，沒有替原四工具增加搜尋入口或隱私選擇 gate。

MCP host 會把工具結果交給其模型。唯讀只描述資料庫存取方式，不代表結果留在本機；名稱、訊息及 metadata 可能送往模型供應商並被保留。原 raw CLI 仍由原 allowlist 約束，不受新 adapter 的 mock 協商保護。不要將它接到遠端 bridge 來繞過新入口限制。

## 先試合成原型

請使用獨立 mock 資料夾與 client profile，避免 host 自動載入原 repo 的 `.mcp.json`、原 Skill 或全域真實 LINE 註冊。可先在 repo 以標準 Python 建立固定 allowlist 的 source/test 包，再解壓到原 repo 與其父層設定之外的新資料夾：

```sh
python tools/build_mock_bundle.py --output ../line-summary-mock-source.zip
```

這是 source/test bundle，不是平台插件安裝包。它不含原 raw server、金鑰擷取器、autoload 設定或 live 測試。已交付過的早期預覽 ZIP／PDF 不代表目前 source；本 repo 不要求下載或上傳這些舊附件。

在隔離資料夾建立 Python 3.11／3.12 虛擬環境，依鎖檔安裝依賴。以下 `python` 須指向該環境；Windows 可用 `.venv\Scripts\python.exe`，其他系統用 `.venv/bin/python`：

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-test.txt
python tools/mock_cli.py status
python tools/mock_cli.py self-test
python tools/mock_cli.py chats --interactive
```

`mock_cli.py` 是真正的 stdio MCP client，但只啟動合成 adapter，不呼叫模型供應商。每次 CLI 是新程序；跨頁請保持同一 MCP client session，不沿用另一程序的 cursor，也不重啟以逃避配額。

由 MCP client 啟動 server 時使用 `plugin_adapter.py --mode mock`；移除該模式則預設停用。它等待 MCP 初始化，不開網頁、HTTP port 或 tunnel。

## 合成工具與限制

新入口有六工具：`line_status`、`line_list_chats`、`line_get_history`、`line_search_messages`、`line_get_unread`、`line_get_contacts`。先讀 status 的固定 `sample_window`；所有樣本必須標示「合成資料示範」，不能把範例人名、報價、交期或未讀數當成使用者查詢結果。

- 歷史與搜尋限定聊天室及帶時區的半開區間 `[since, until)`，最多 31 天。搜尋是區分大小寫、無 Unicode 正規化的字面子字串；`%`、`_` 是普通字元，沒有語意搜尋、附件內容分析或討論串重建。
- 每次資料呼叫與新分頁先協商 destination、scope 及 `deidentify`／`original`。缺選、改範圍、到期、重播或重啟後，舊示範 ID 不能直接讀資料。
- 這只展示選擇流程：`human_confirmation_verified` 永遠 false，`privacy_processing` 永遠 `not_implemented`。模型填參數不等於真人批准，選去識別也沒有真的匿名化。
- 預設每程序每分鐘 10 個受理呼叫、2 秒冷卻、1 個 backend 讀取；每次最多 100 則／256 KiB JSON，累計最多 5,000 則／2 MiB。相同進行中請求共用 backend，但每份結果各自計帳。這些是本機設計值，沒有跨程序或重啟後持續的全域配額。
- `fetched_at` 是回覆時間，`source_sync_at` 固定 null。未讀是最近本機列的近似取樣；分頁也不是完整快照。空結果、`content_complete` 或最新本機時間都不能證明 LINE 同步或歷史完整。

遇到拒絕、預算不足、首項過大或無法前進的 cursor 就停止，標明未完成範圍，不換入口、拆日期、提高額度或重啟繞過。

## 保留原 Windows 使用方式

原入口需要 Windows、已登入的 LINE 電腦版、Python 3.11 以上及相容依賴；本輪沒有真實 LINE 相容性驗收。依鎖檔配置獨立環境，將 `.mcp.json` 的 Python 路徑設為該環境的絕對路徑。

由使用者自行複製 `settings.example.json` 為 `settings.json`，指定正確帳號的 `db_path` 與明確 `allowed_chat_ids`，再決定是否啟用。預設 `enabled: false`，聊天室探索與 contacts 也各自停用。不要上傳帳號設定、資料庫、金鑰或真實聊天；設定變更需重新啟動，不能自動擴權或重啟規避額度。

原工具只有聊天室查找、日期歷史、近似未讀與聯絡人，沒有訊息全文搜尋。原工具契約、日期／分頁限制及升級差異見 [MIGRATION.md](MIGRATION.md)；原 Skill 位於 `.claude/skills/line-summary/SKILL.md`。摘要預設只在 host 對話回覆，另存或分享需另外確認目的地與授權。

## 正式資料接入仍待完成

真實 adapter、可信本機預覽與本人核准、具接收者及範圍的限時授權／撤銷、敏感分類、去識別及全出口保護都尚未實作。還需選定 host 的實際帳號、安裝、傳輸與保留政策驗收。私有 tunnel 也不保證資料不離開本機。合成測試通過不能替代這些條件。

- [本機準備與協定操作](docs/PLUGIN_PREPARATION.md)
- [完整文字使用手冊](docs/DETAILED_USAGE_GUIDE.md)
- [真實資料的隱私就緒門檻](docs/PRIVACY_READINESS.md)
- [各 host 路徑與未驗證事項](adapters/README.md)
- [合成 Skill 與封裝邊界](plugin/README.md)

## 測試

在配置好的 repo 測試環境執行，live LINE 測試預設跳過；CI 也只跑 synthetic，不能加入 live opt-in：

```sh
python -m pytest --cov --cov-report=term-missing -q
```

已驗程式 snapshot `2ccc29e3…` 在 Linux／Python 3.12.14 為 472 passed、5 live skipped，branch coverage 92.52%（門檻 85%）；另有獨立 QA 13 passed，分開計數。遠端跨平台結果須核對 [Actions](https://github.com/yung13yubabie/line-summary/actions) 的同一提交。仍有上游 Pydantic lifespan warning，可能顯示套件安裝路徑。實際模型 host、手機、tunnel 與真實 LINE 均未驗收。
