# LINE Summary 多平台操作手冊

本機合成測試與後續接線指南

版本日期：2026-10-08

這份手冊讓你先在獨立資料夾驗證 LINE MCP 合成原型，再按指定 host 的正式流程決定下一步。現在能驗證的是本機程式與合成資料協定；任何平台帳號、手機、tunnel 與真實 LINE 讀取，都不能因本機測試通過而視為已接通。

「host」指真正載入工具、執行 MCP 呼叫並把結果交給模型的程式。例如 Claude Desktop、Claude Code、ChatGPT Web 是不同 host。選了相同模型，不代表它們有相同安裝方式或權限。

建議順序：先讀第 1 至 5 節，完成本機 mock；再選第 7 至 13 節中的一條平台路徑。只想保留原 Windows 使用方式，讀第 6 節。外出手機使用先看第 16 節，停止與撤銷看第 17 節。

### 閱讀索引

- 第 1 節：已完成與待啟用的部分
- 第 2 至 3 節：獨立資料夾、mock CLI 與真實 stdio 測試
- 第 4 至 5 節：每次隱私選擇、六工具、日期、分頁與停止
- 第 6 節：原 Windows 核心的既有流程與限制
- 第 7 至 8 節：Claude Desktop、Claude Code 與 Skill 匯入
- 第 9 至 11 節：ChatGPT、Gemini、Grok
- 第 12 至 13 節：DeepSeek Harness 與 Qwen Code
- 第 14 至 15 節：其他桌面及 Web host
- 第 16 至 17 節：手機、電腦狀態、停止及撤銷
- 第 18 節：官方來源與版本核對

## 1 先確認現在完成到哪裡

### 已備妥的本機部分

- 共用入口 `plugin_adapter.py`，透過 stdio 與 MCP client 通訊。只有 `disabled` 和 `mock` 兩種模式。
- `mock` 建立程式內的合成 SQLite 資料；不讀真實 LINE、不提取金鑰、不接收帳號資料庫路徑。
- 六個工具、輸入 schema、結構化輸出、範圍檢查、分頁、字面搜尋、程序內限流與輸出預算。新版另有只限 mock 的逐次隱私選擇協商。
- 共用 `SKILL.md`，說明如何使用已存在的工具，遇到未連線、拒絕或不完整結果時如何停止。

### 尚未完成的啟用部分

本輪真實 Windows LINE、macOS LINE、Claude Desktop、Claude Code、ChatGPT Web、Android、iOS、Gemini、Grok、DeepSeek Harness、Qwen Code，以及其他 host 的端到端測試，均為 `NOT_RUN`。沒有建立或啟用 tunnel、public endpoint、API key、平台插件身分或帳號授權。

每次涉及資料的呼叫前，host 都應先問你要「去識別化」或「原文」，不替你預選去識別化。`line_status` 僅回傳服務狀態，無須選擇。新版 mock 協商只是流程示範，不能驗證真人批准。

真實 adapter 尚未實作。去識別化引擎、敏感分類、可信本機預覽、可信限時範圍核准及多 host 共用授權，也都不是本版功能。將 `privacy_ready` 改成 `true`，或加入 `db_path`、`real` 模式，都不會解鎖真實資料。

### 怎麼看測試證據

本輪確認的本機測試為 Linux、Python 3.12.14：已驗程式 snapshot `2ccc29e3…` 的完整 repo 472 passed、5 live skipped，runtime coverage 92.52%（branch 計量，85% 門檻保留）；mock_privacy 模組為 100%。仍有 1 項既有 Pydantic warning，可顯示套件安裝路徑。

同輪包含原入口四工具的合成 stdio list／call 相容性、24 項 host template／Skill 結構測試，以及 4 項 mock CLI 子程序測試。這些證據不代表 Windows 真實 LINE 或模型 host 已驗收。跨平台 synthetic CI 以本次 PR 的 exact-head checks 為準，需核對同一提交；本手冊不預先宣稱 CI 已通過。

上一版基線為 288 passed、5 live skipped、coverage 91.12%，隔離 mock ZIP 164 passed；較早新版 snapshot 的隔離包曾有 332 passed；本輪最終隔離包全套為 `NOT_RUN`，不沿用舊數字聲稱新版通過。

共用 MCP 加中立 Skill，可以重用工具與操作原則；每個 host 仍需自己的連線、包裝及驗收。沒有「同一個 ZIP 上傳所有平台就接通」的承諾。

## 2 準備獨立的 mock 測試環境

### 先選對檔案

主示範 ZIP 是 source/test bundle，解壓後執行本機測試。`repo.patch` 是原 repo 的修訂內容，供審閱與套用。兩者都不是 ChatGPT、Gemini 或 Claude 的通用 plugin 安裝包。

共用 Skill 位於 `plugin/skills/line-summary-mock/SKILL.md`，另附兩種 ZIP 結構，內容相同：

- `line-summary-mock-skill.zip`：ZIP 內為 `line-summary-mock/SKILL.md`，供 Claude 的資料夾式格式；也可解壓後按 Claude Code／Qwen Code 的位置手動放置。
- `line-summary-mock-skill-root.zip`：`SKILL.md` 直接在 ZIP 根目錄，供 Perplexity Computer 文件要求的格式。

兩者都只有指令，不包含 MCP 設定、可執行程式或憑證，不會接通本機。實際 host 匯入仍 `NOT_RUN`，詳見第 8、13、15 節。PDF、DOCX 和 TXT 則只是閱讀指南。

### Windows PowerShell

1. 把示範 ZIP 解壓到新資料夾，例如 `C:\line-summary-mock`。不要放在原 repo 或會繼承原 repo 設定的父層下。
2. 檢查此資料夾沒有原 `.mcp.json`、`.claude`、`line_mcp_server.py`、`key_extractor.py` 或真實 `settings.json`。
3. 用一般使用者 PowerShell 進入該資料夾，建立 Python 3.11 或 3.12 虛擬環境。以下以 Python 3.12 示範；若使用 3.11，改為對應版本執行檔。每行依序執行。

```powershell
Set-Location C:\line-summary-mock
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-test.txt
.\.venv\Scripts\python.exe -m pytest -q
```

### macOS 或 Linux 的合成測試

```sh
cd /absolute/path/to/line-summary-mock
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-test.txt
.venv/bin/python -m pytest -q
```

這裡只測 mock，不代表 macOS 可讀真實 LINE。示範包沿用的依賴鎖檔仍含原核心依賴；若目前系統或 Python 沒有對應 wheel，安裝會停止。先查支援組合，不關閉 hash 驗證、不自行換成未審閱來源。

### 成功與停止條件

成功：依賴完成、pytest 正常結束，且所有執行內容來自獨立示範資料夾。若套件安裝失敗、測試失敗、執行檔路徑不明或 host 自動發現原 LINE server，停止設定。不要提權、停用安全軟體或改用原 reader 來讓測試「過關」。

## 3 用真正的 MCP client 驗證 stdio

### 先理解 server 的行為

`python plugin_adapter.py --mode mock` 啟動的是 stdio server。它等待 MCP client 的初始化及工具呼叫；不會顯示登入頁、不會開 HTTP port，也沒有可貼入瀏覽器的 localhost URL。

不要把任意 JSON 直接接到 shell，或把 `tools/call` 參數當成 server 的命令列選項。正式順序為 `initialize`、`tools/list`、`tools/call`。專案測試使用實際 MCP SDK client 與子程序管線驗證這個順序。

在 Windows 的獨立示範資料夾，可先執行只針對真實 stdio 的離線測試：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_plugin_protocol.py
```

macOS／Linux 將前面的 Python 路徑改成 `.venv/bin/python`。這是協定驗證，不會呼叫任何模型供應商，也不會登入 LINE。

### 可直接執行的本機 mock CLI

本版 `tools/mock_cli.py` 是真正的 MCP SDK client，會啟動子程序、初始化、列工具並呼叫；它不連任何模型供應商。Windows 在獨立資料夾依序執行：

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py tools/mock_cli.py self-test
& $py tools/mock_cli.py status
& $py tools/mock_cli.py chats
& $py tools/mock_cli.py chats --interactive
```

self-test 應發現六工具、各項 `passed: true`，並保留 host／real LINE 的 `NOT_RUN`。它自動示範 synthetic 的 original 選項，只測程式；沒有真人核准或真實資料傳輸。

`chats` 不選隱私方式時應回 `requires_confirmation`、空 items。`--interactive` 會在本機詢問 `deidentify / original / stop`，沒有默認接受。選 stop 或不選就沒有資料。

單次字面搜尋示範如下；`--privacy original` 只描述 synthetic 演示，不能代替未來 host 的真人確認：

```powershell
$since = "2026-10-01T00:00:00+08:00"
$until = "2026-10-02T00:00:00+08:00"
& $py tools/mock_cli.py search --chat-id demo-project `
  --query 報價 --since $since --until $until --privacy original
```

macOS／Linux 用 `.venv/bin/python tools/mock_cli.py ...`。預設 destination 是 `local-test`；改成六平台標籤也只是本機 mock label，絕不建立平台連線。

每次 CLI 都有獨立子程序，cursor 和程序配額不能跨次延續。不要把上次 cursor 帶到下次 CLI，也不要多開／重啟以規避限流。分頁驗收請用下面可保持同一 session 的 MCP client。

### 手動 MCP client 的連線設定

1. 使用支援本機 stdio 的 MCP client，另建 mock profile。先核對它是否自動讀取全域、專案、家目錄或其他 client 設定。
2. `command` 指向剛建立的 Python 絕對路徑。
3. `args` 依序是 adapter 的絕對路徑、`--mode`、`mock`。如果 client 支援 `cwd`，指向獨立示範資料夾。
4. 不填真實 DB 路徑、API key、token、tunnel ID 或遠端 URL。`plugin/mock-client.example.json` 只供參考，實際頂層欄位依 client schema。
5. 連線後列出六個工具，再呼叫 `line_status`。等待至少本機設定的冷卻時間，再依第 4 節詢問隱私選擇並執行資料查詢。

### 必須看到的結果

工具清單應包含 `line_status`、`line_list_chats`、`line_get_history`、`line_search_messages`、`line_get_unread`、`line_get_contacts`。狀態 envelope 必須是 `status: "ok"`、`mode: "mock"`、`synthetic: true`；`items[0]` 內的 `real_data_enabled`、`privacy_filter_implemented`、`automatic_sync` 都應為 `false`。

`sample_window` 應提供固定範例日期。移除 `--mode mock` 或改成 `--mode disabled` 的獨立停用測試，應仍能發現工具，但資料呼叫回覆 disabled、空 items，不能讀到任何真實內容。

成功標準是工具真的回傳合成 envelope。僅有「Added」「設定已儲存」或模型口頭說已連線，都不算通過。工具不足、schema 不符、出現真實資料或 disabled 時停止，保留不含敏感內容的錯誤代碼。

### 第一個合成資料協商範例

以下 JSON 是已初始化 MCP client 的 `tools/call` 參數，不是 shell 指令，也不是平台安裝檔。

```json
{
  "name": "line_search_messages",
  "arguments": {
    "chat_id": "demo-project",
    "query": "報價",
    "since": "2026-10-01T00:00:00+08:00",
    "until": "2026-10-02T00:00:00+08:00",
    "limit": 1,
    "destination": "local-test"
  }
}
```

上例是本機 client 的第一次協商請求，預期先得到 `requires_confirmation`，不是立即收到訊息。模型 host 請填實際 `destination`，如 `claude` 或 `chatgpt`，不可把雲端資料流標成 `local-test`。用戶選擇後的第二次呼叫須加上原樣回傳的 `mock_scope_id` 與所選 `privacy_choice`。

## 4 六個工具的實際操作

### 每次資料呼叫前先選擇

聊天室、歷史、搜尋、未讀、聯絡人及每一頁資料，都先列接收平台、工具、聊天室／查詢、日期與筆數，問你：「這次要去識別化，還是原文？」不預選、不沿用舊批准；兩選項都不是已實作的隱私保證。

mock 第一次請求帶接收者及完整查詢，不帶批准；應得到 `requires_confirmation`、空 `items` 和 `mock_scope_id`。你選擇後，host 才原樣帶回 ID 和 `privacy_choice: "deidentify"` 或 `"original"`。接收者未知、scope 改變、下一頁或到期，重新詢問。

`mock_scope_id` 每輪產生新的 generation ID，非秘密、非持久，也不證明真人批准；`scope_fingerprint` 另列參數範圍指紋，不是批准 ID。120 秒後禁止新呼叫或加入，已開始的 synthetic 讀取可能完成；重啟後失效。所有結果仍應有 `human_confirmation_verified: false` 和 `privacy_processing: "not_implemented"`。選去識別化沒有真正執行去識別化，選原文也只回傳固定 synthetic fixture；絕不能據此解鎖真實資料。

### 先確認來源和聊天室

1. `line_status` 使用空 arguments。先讀 `sample_window`，不要把固定 fixture 當今天。
2. 只有需要辨識聊天室時，才呼叫 `line_list_chats`，例如 `query: ""`、`limit: 20`。合成示範使用 `demo-project`。
3. 需要聊天室歷史，用 `line_get_history`；只找某段文字，用 `line_search_messages`。不要為了回答一題就讀聯絡人或所有聊天室。

沿用第 3 節的日期與聊天室，可把 query 改成 `50%_test` 驗證特殊字元。`%` 和 `_` 要照字面比對。搜尋區分大小寫，沒有 Unicode 正規化、regex、斷詞或語意搜尋；也不會自動找前後各 N 則或重建回覆討論串。

### 其他常用呼叫

- 歷史：`line_get_history`；arguments 為上述 `chat_id`、`since`、`until`、`limit`，去掉 `query`。
- 近似未讀：`line_get_unread`；可用 `limit_chats: 20`、`per_chat_limit: 50`。結果只是本機計數與最近記錄的近似取樣。
- 合成聯絡人：`line_get_contacts`；可用 `query: ""`、`limit: 20`。只有需求涉及聯絡人時才使用。
- 範圍拒絕測試：在合成測試中把聊天室改成 `demo-denied`，應得到拒絕且沒有資料。拒絕屬於預期安全結果，不能改用其他入口查它。

### 對模型下指令的例子

「先查 line_status，確認只用合成資料。用範例日期查 demo-project 的『報價』，一次一頁；每次資料呼叫前先問我要去識別化或原文，不要預選。列出訊息 ID、時間及摘要。遇到限制就停止並說明不完整，不讀其他聊天室。」

## 5 判讀日期分頁新鮮度與錯誤

### 日期和下一頁

所有歷史／搜尋都使用帶時區的半開區間 `[since, until)`。查台北時間一天，填當天 `00:00:00+08:00` 到隔天 `00:00:00+08:00`；不要用 `23:59:59`。單次最多 31 天。

只有 `has_more: true`、有 `next_cursor`，且仍需要該資料時才繼續。下一頁原樣沿用聊天室、query、日期和 cursor；cursor 不能修改、跨查詢使用或當永久書籤。資料修改、刪除和 rowid 重用仍可能影響分頁。

### 三種時間不要混淆

- `fetched_at`：這次產生回覆的 UTC 時間。
- `source_sync_at: null`：沒有可驗證的 LINE 同步時間。
- `source_latest_at`：指定來源範圍最新的本機記錄，未必符合搜尋字詞。`latest_returned_at` 才是本頁實際回傳的最新訊息時間。

`content_complete` 只描述本頁交付項目；`coverage: "local_rows_only"` 只描述本機列。兩者都不證明 LINE 雲端歷史完整。查無結果不等於沒有聊過。未讀的 `unread_count` 和 `returned_count` 不能相減後稱為「未同步 N 則」，回傳的近似樣本可能全都已讀。

### 等待或停止

- `requires_confirmation`：沒有資料可摘要。明示本次接收者和範圍，等待使用者選擇；模型不能替使用者填選項假裝同意。
- `rate_limited`、`busy`：照 `retry_after_seconds` 等候，再重試同一必要請求；不要密集輪詢或平行轟炸。
- `scope_denied`、無時區、反向日期、變造 cursor、schema 不符：停止該查詢，檢查原始需求與參數，不擴大權限。
- byte／訊息預算耗盡、`item_exceeds_byte_budget`、`content_complete: false`，或 `has_more: true` 卻無 cursor：停止並交付標示不完整的結果，不能跳過受阻項目。
- transport 中斷、server 停止或 source error：回報目前無法取得資料，不能拿舊結果冒充新查詢成功。

預設每程序每分鐘 10 個受理呼叫、2 秒冷卻、1 個並行 backend 讀取，是本機設計值，不是任何平台官方配額。預設每次 100 則及 256 KiB payload，整個程序 5,000 則及 2 MiB；實際仍受較低設定與剩餘預算限制。

成功資料／狀態與合併中的每份結果各計程序預算；確認、拒絕與 disabled 回覆不計累積 session 配額，但每一份仍受單次 response cap。限制不涵蓋全部 MCP wire bytes，也不是多程序全域配額。重啟會使 cursor 失效、程序配額重算，但不得用重啟、改 query、切日期、換工具或多開程序逃避限制。

## 6 保留原 Windows 核心使用方式

### 這條路徑的適用範圍

原 `line_mcp_server.py` 是 Windows 真實讀取器，與六工具 mock adapter 分開。它原本有四個工具；沒有新增的字面訊息搜尋，也沒有已接通的遠端插件。原核心保留在 repo，不放入隔離示範 ZIP。

只有使用者自行決定處理其有權使用的資料、理解資料將交給所選模型供應商，且完成既有範圍 opt-in 時，才考慮這條本機流程。本輪沒有執行，狀態為 `NOT_RUN`；以下是現有流程的操作對照，不代表已替你啟用。原核心沒有新增的逐次隱私選擇或可信核准 gate，不能當作符合新版每次詢問要求的完成品，也不能拿來繞過 adapter 的拒絕。

### 使用者的準備步驟

1. 使用 Windows、Python 3.11 以上，以及正在執行且已登入的 LINE 電腦版。原測試版本曾為 LINE 26.3；更新後要重新驗證相容性。
2. 按原 README 在獨立虛擬環境安裝鎖定依賴。不要把 mock 資料夾的入口改成原 reader。
3. 在原 server 同目錄，由使用者建立私人 `settings.json`，先維持 `enabled: false`。明確核對自己的 `db_path`，不以最大 `.edb` 猜帳號。
4. 使用者確認接收平台、聊天室、日期及用途後，才自行設定 `enabled: true` 與最小 `allowed_chat_ids`。沒有「全部聊天室」萬用值。
5. 若需要找 ID，可暫開 `allow_chat_discovery`；結果中的名稱、ID 和時間也會進模型。找到後關閉探索。`allow_contacts` 是另外的通訊錄權限，只在需要時開啟。
6. 設定於首次工具呼叫固定；使用者完成必要設定變更後，以正常維護方式重啟。這不允許模型為繞過限制自行改設定或重啟。
7. 按原 README 審閱原 `.mcp.json` 的 Python 與 server 絕對路徑，在原專案的 Claude Code 用 `/mcp` 核對；先選一個明確聊天室及短時間範圍驗收。

### 成功與失敗界線

真正成功需在 Windows 確認唯讀範圍、錯誤、分頁及資料來源，不能只看 Claude Code 顯示 Connected。第一次取得金鑰可能較慢。金鑰由程序記憶體快取，不保證安全抹除；OS swap、休眠檔或 dump 仍可能含記憶體資料。

不要承諾取到 key 後 LINE 可以關閉，或登出、切換帳號後仍安全可用。每次帳號、LINE 版本或程序狀態改變都應停止並重新確認。權限、cipher 或讀取失敗時，不自動提權、不停用安全保護、不把金鑰或資料庫貼給人排查。

停止時先停 MCP，再由使用者將 `enabled` 設為 `false`、清除不再需要的允許範圍，並移除相應 host 註冊。不要將原核心接上 tunnel、remote bridge 或 API 來略過未實作的隱私 gate。選用捲動補歷史工具會操作 LINE 畫面，不屬於這條唯讀流程，本手冊不啟動它。

## 7 Claude Desktop 本機 mock

### 前提

先完成第 2 至 5 節。Desktop 本機 stdio 設定是一條獨立路徑；它與 Web／Mobile 上傳 Skill 或 plugin ZIP 的效果不同。Windows 設定檔是 `%APPDATA%\Claude\claude_desktop_config.json`；macOS 是 `~/Library/Application Support/Claude/claude_desktop_config.json`。[R1]

### 操作步驟

1. 完整退出 Claude Desktop。備份現有設定，確認其他已註冊 server 是否會讀真實 LINE。
2. 開啟 `adapters/claude/desktop.mock.example.json`。把所有 `REPLACE_WITH_ISOLATED_MOCK_DIR` 換成獨立解壓資料夾，Python 指向該資料夾的虛擬環境。
3. 只合併新增 `mcpServers` entry，不覆蓋整份現有設定。JSON 的 Windows 反斜線需跳脫。

```json
{
  "mcpServers": {
    "line-summary-mock": {
      "command": "C:\\line-summary-mock\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\line-summary-mock\\plugin_adapter.py",
        "--mode", "mock"
      ]
    }
  }
}
```

4. 重開 Desktop，使用當前版本提供的 MCP／工具狀態檢查連線。實際選單名稱若和官方不同，以當前官方說明為準，不猜未出現的按鈕。
5. 列出六工具，呼叫 `line_status`，確認合成標記及 `real_data_enabled: false`。
6. 以第 4 節查一次短範圍搜尋及下一頁，再驗證 `demo-denied` 拒絕。最後停用這個 server，確認不能再取得新結果。

### 成功失敗與撤銷

成功：Desktop 實際 Connected、工具 schema 正確、合成查詢有來源、拒絕與停用有效。此項目前 `NOT_RUN`。

若顯示找不到程式，檢查絕對路徑及虛擬環境依賴；若 server 關閉，檢查不含敏感值的錯誤代碼；若只看見 Skill 而沒工具，先完成 MCP 連線。不要改接原 `.mcp.json`。

撤銷：完整退出 Desktop，僅移除 `line-summary-mock` entry 或恢復備份，重開後確認該 server 不再出現且子程序已結束。若另外加過 Skill，分別移除。移除本機 server 不會自動刪除供應商既有聊天紀錄。

## 8 Claude Code 與 Claude 的 Skill 匯入

### Claude Code 的本機步驟

1. 在獨立 mock 資料夾操作，不在原 repo 下啟動。先檢查專案和全域 MCP 設定，避免自動載入原 raw reader。
2. 優先使用單次明確設定，先替換 `adapters/claude/desktop.mock.example.json` 的路徑，再從獨立資料夾啟動：[R2]

```powershell
Set-Location C:\line-summary-mock
claude --strict-mcp-config --mcp-config "C:\line-summary-mock\adapters\claude\desktop.mock.example.json"
```

3. `--strict-mcp-config` 一般只載入明確指定的 MCP 設定；組織管理政策可能覆蓋，啟動前仍須核對。這個參數不是整個 host 的沙箱。
4. 在 Claude Code 用 `/mcp` 查看實際連線與六工具，先 `line_status`，再依第 4 節逐次詢問隱私選擇並查合成資料。
5. 若使用者另外決定儲存專案綁定 local scope，官方形狀如下；這會修改設定，本次未執行：

```powershell
claude mcp add --transport stdio --scope local line-summary-mock -- "C:\line-summary-mock\.venv\Scripts\python.exe" "C:\line-summary-mock\plugin_adapter.py" --mode mock
claude mcp get line-summary-mock
```

6. Skill ZIP 解壓後，把 `line-summary-mock` 資料夾放到隔離專案的 `.claude/skills/`，得到 `.claude/skills/line-summary-mock/SKILL.md`，保留 YAML frontmatter。是否另外安裝全域副本由使用者決定。[R3]

Skill 的成功條件是 host 能讀到操作指令；MCP 的成功條件另按第 3 節驗證。全域 MCP 註冊不會自動安裝全域 Skill。

撤銷時，按 `claude mcp --help` 與官方文件移除本次確切 server 名稱及其 scope；再用 `claude mcp get line-summary-mock`／`/mcp` 確認不再可用。不要整批刪除其他設定。若另外放了 Skill，再移除那份副本。CLI 實際註冊與驗收目前 `NOT_RUN`。

### Claude Chat 的三種檔案要分開

- Claude 自訂 Skill：官方 ZIP 內需有資料夾 `line-summary-mock/SKILL.md`。本次 `line-summary-mock-skill.zip` 採這種結構，實際匯入仍 `NOT_RUN`；它只載入指令和資源，不能讓雲端服務讀到你電腦的 stdio。[R32]
- Claude plugin ZIP：需按正式 plugin 結構製作。官方說明 Chat 會忽略 ZIP 內的本機 command MCP 元件；本機 Cowork／Claude Code 的支援不能直接套用到 Chat。[R4]
- Desktop extension：另有正式桌面封裝機制；本次沒有製作 `.mcpb` 或可驗收的 extension，不能把示範 source ZIP 改副檔名冒充。

Claude Web／Mobile 的 remote connector 由雲端連往 server，不能直接連家中 PC 的 `localhost`。[R5] 本版沒有 remote endpoint，也未驗證手機 `@`。如果選用 remote connector，先停在資格、資料接收者及安全連線規劃；不要上傳整個 repo 試圖代替接線。

## 9 ChatGPT Web 與官方 Tunnel

### 先做資格檢查

官方文件列出 Web 的 Plugins → Add custom MCP server，可用 Server URL 或 Tunnel，安裝後可用 `@` 選取。[R6] 實際帳號方案、workspace 權限與顯示選項仍需確認。找不到入口時先核對官方說明和管理員政策，不用換區、改帳號或其他未核准方式繞過。

### 為何本版選 Tunnel 也還不能直接啟用

Secure MCP Tunnel 可在私有環境接 stdio 或 HTTP MCP，由私有端向外建立 HTTPS 連線；不要求把本機 MCP 直接開成 public listener。[R7] 本版只有協定部分，沒有預建 tunnel、憑證、授權或平台身分。

### 後續經核准才進行的步驟

1. 確認要使用的 ChatGPT workspace 及 Platform organization，兩者關聯正確，帳號有自訂 MCP 與 tunnel 所需權限。
2. 確认使用者同意以合成資料接線；審閱服務條款、工具輸出接收者及保留規則。
3. 按官方文件建立 Platform tunnel、runtime key 及必要權限。建立持續存取和憑證須由使用者另行確認，經安全機制處理；key 不放命令列、repo、日誌或聊天。
4. 在核准的 PC 環境依官方 runtime 流程連接獨立 mock adapter。只指向 `plugin_adapter.py --mode mock`，不指向原核心。此手冊不提供未經 host 驗證的 tunnel 啟動指令。
5. 回 Web 的 Add custom MCP server，選擇正確 Tunnel，完成當前認證及風險確認。Server URL 是另一條需已存在、可連且有適當認證 endpoint 的路徑；不能填 stdio 檔案路徑。
6. 在新對話選取該插件，先要求 `line_status`，再完成六工具發現、合成搜尋、拒絕、停用及斷線測試。工具出現本身不足以證明成功。

### 成功失敗與撤銷

實際 Web 安裝、tunnel 認證及 Android／iOS 呼叫目前均 `NOT_RUN`。前提缺一時就停止，不能聲稱「已準備好直接用手機」。沒有 public port 不等於資料留在 PC；工具結果仍會傳往 OpenAI。

撤銷需分層確認：在 ChatGPT 移除或停用此次自訂 MCP；在 Platform 撤銷相應 tunnel 權限／runtime key；停止 PC runtime 和 adapter；重新查詢應明確失敗。各層控制的確切名稱依官方當時 UI 核對，不能只關聊天視窗就視為撤銷。

source ZIP、通用 MCP JSON、Skill ZIP 都不等同官方 ChatGPT plugin package。若日後要做正式包裝，另依官方 Package your plugin 規格製作與驗收。[R8]

## 10 Gemini App 的 Web 與手機路徑

### 第一個檢查點是帳號資格

依 2026-10-08 已查官方資料，自訂 tools 流程列有美國、英文、年滿 18 歲、個人 Google 帳號、Keep Activity 開啟等條件。台灣帳號能否使用尚無保證；接線當天仍要重新核對。[R9]

### 操作順序

1. 打開官方自訂 tools 說明，對照實際 Gemini App 帳號。若沒有該入口或資格不符，停止，記錄「資格尚未確認」，不要自動改地區、語言或活動設定。
2. 審閱 Keep Activity 與 Gemini Apps 資料使用／保留方式。若需要變更設定，先由使用者決定。[R10]
3. 官方流程需要 MCP server URL；先確認真的有經授權、可由 host 連達且認證適當的服務。本版只有 stdio，這一步目前沒有可填的 URL。
4. 後續若另行完成安全遠端 adapter，依官方網頁流程新增，處理當前認證，先只測合成資料。
5. Web 確認能找到工具並正確呼叫後，再按官方行動裝置 `@` 流程在手機實測。Web 文件描述手機使用，仍不能代替你帳號上的驗收。
6. 按第 3 至 5 節驗證狀態、日期、結果、拒絕及斷線；手機回覆要有新的工具呼叫證據，不能只重述先前聊天。

### 本版停在哪裡

Gemini 帳號、Web、自訂工具、手機、認證與 endpoint 均 `NOT_RUN`。沒有核實 Gemini App 可用官方本機 tunnel 直接連此 stdio 原型；不能填 `localhost`、上傳 Python 或 Skill ZIP 就認為已完成。

Gemini CLI／API 是不同產品面，本次沒有做其 client。不能因 CLI 具工具能力，就宣稱 Gemini App 或獨立 Desktop app 支援相同功能。

### 撤銷和常見失敗

如果日後新增 connector，先在 Gemini 的工具管理介面移除該自訂工具，再撤銷對應服務認證、停遠端服務或 bridge。使用新查詢確認不能存取。Keep Activity 的調整與聊天紀錄刪除是另外的設定及決策，不因 connector 刪除而自動完成。

入口缺少通常先查資格；URL 失敗先查 host 可達性及正式 transport；認證失敗先查授權與到期。不可將未知錯誤武斷判成「LINE 沒同步」，也不可架無認證 public URL 求快。

## 11 Grok Web 與 Grok Build

### Grok Web 的自訂 connector

官方路徑為 Connectors → New Connector → Custom → server URL。[R11] server 必須從網際網路可連達，`localhost` 或私有 IP 會被拒絕。[R12] 本版沒有符合此條件的 endpoint。

1. 先核對 grok.com 帳號是否有 Custom connector，以及當前方案、認證選項、資料處理規則。
2. 若要繼續，先另行設計並核准安全遠端服務、認證與資料出口。只測 mock，也要防止意外接到原 reader。
3. URL 準備完成後依官方流程新增，核對工具清單與 schema。
4. 依序驗證 `line_status`、合成歷史／搜尋、範圍拒絕、停用、憑證撤銷和服務中斷。

本次 Web 安裝 `NOT_RUN`。手機、獨立 Desktop chat 及自訂 MCP 的 `@` 體驗未核實，不能從 Web 或 CLI 推論已支援。

撤銷：在 Connectors 移除該 connector，撤銷 endpoint 的認證，停止 bridge／服務，再確認新呼叫失敗。公開 URL 需求不等於允許公開真實資料，更不等於可使用無認證服務。

### Grok Build CLI

官方另支援本機 stdio 與遠端 HTTP MCP，但會讀取其他 client 的相容設定，可能包含專案 `.mcp.json`。[R13]

1. 使用獨立解壓資料夾及 mock profile，先逐一核對它實際發現的設定來源。
2. 若它發現原 `line_mcp_server.py`，停止此次 mock 安裝，調整隔離範圍後再檢查。
3. 按當前官方 Build 設定格式，command 指向隔離 Python，args 指向 mock adapter。未核實的 CLI 子命令與檔案位置不要猜。
4. 連線後完整執行第 3 至 5 節。撤銷時移除精確 mock entry，關閉對應子程序，並確認沒有被相容性自動匯入重新啟動。

Build host 驗收 `NOT_RUN`。xAI API 的 Remote MCP 另需 client、URL 和認證；官方當前說明其 `require_approval` 相容參數不支援，不能把它當安全 gate。[R14] 本版沒有 API client，不提供 raw reader 遠端化指令。

## 12 DeepSeek Harness 與一般 DeepSeek Chat

### 先確認你要的是哪個產品

一般 DeepSeek Web／Mobile／Desktop Chat 可否安裝此自訂 MCP 或共用 Skill，目前未核實。不要在 chat.deepseek.com 或 Android 看到聊天框，就假定可用 `@` 啟動此工具。

官方 DeepSeek Harness 是另一個開發者 agent 工具，有 plugin／skill 系統，以及本機 stdio、Streamable HTTP MCP client。[R15][R16] 這條路徑目前沒有安裝或跑過，狀態 `NOT_RUN`。

### Harness 的本機 mock 準備流程

1. 先按官方 Harness 文件核對系統要求、正式發行版與當前設定結構；安裝、帳號登入或 API 認證由使用者另行決定。
2. 使用第 2 節獨立資料夾。檢查 Harness 的專案、使用者、plugin 與相容 MCP 設定來源，確保不會啟動原 LINE 核心。
3. 使用 `adapters/deepseek/harness.mock.example.cordis.yml`，替換 Python、adapter 和 cwd 的全部絕對路徑。它是 JSON 相容 YAML，以 Cordis `insert` patch 載入官方 `@deepseek-ai/dsh-mcp-client`，不是一般 `mcpServers` JSON。保留 `reconnect.enabled: false`，避免示範自動重啟來重置程序內預算。
4. 審閱其他 profile／patch 後，由使用者依官方命令啟動：[R16]

```powershell
Set-Location C:\line-summary-mock
dsh web --patch "C:\line-summary-mock\adapters\deepseek\harness.mock.example.cordis.yml"
```

5. 這會開 Harness 本機 Web UI，不會把 LINE adapter 變成 HTTP endpoint。工具名稱可能帶 `mcp__line-summary-mock__` 名稱空間。先真正呼叫狀態工具，再按第 4 節逐次選擇和查詢。
6. 審閱 session／工具結果日誌的位置與保留方式。獨立 Skill ZIP 在 Harness 的完整匯入流程尚未核實，不提供猜測的上傳命令；先依正式 host 格式另行驗收。

### 失敗和撤銷

若當前版本沒有可確認的 stdio 設定，停在本機協定已驗證，不宣稱 Harness 相容。若設定成功卻沒有工具，先查 host 日誌與 transport，不切換 raw reader。

撤銷時移除這個精確 mock server／plugin 設定，停止其子程序，重開新 session 確認不可呼叫。若曾另建 API 憑證，憑證撤銷與聊天／session 清理分開處理，不能只刪 Skill。

DeepSeek API 的 tool calls 是開發者 client 接收模型呼叫、執行工具、再回傳結果的迴圈。[R17] API 模型不會直接啟動你 PC 的 MCP。本次沒有這個 client，也沒有使用 API key。

## 13 Qwen Code 與一般 Qwen Chat

### Code 路徑的範圍

Qwen Code 的官方 MCP 設定支援 stdio／HTTP／SSE；本機入口用 `command` 和 `args`，可選 `cwd`／`env`。[R18] 原版 Qwen Web／Mobile／Desktop Chat 可否自行匯入這個 MCP 或 Skill 尚未核實，不能把 Code 的能力套用到一般 Chat。

### 本機 mock 操作

1. 依官方文件核對 Qwen Code 的安裝要求。專案設定位置為 `.qwen/settings.json`，使用者層為 `~/.qwen/settings.json`；此例使用隔離專案。本輪沒有代為安裝、登入或改設定。
2. 在獨立 mock 資料夾使用隔離設定；先備份原設定並檢查其他 MCP 的自動載入來源。
3. 參照 `adapters/qwen/settings.mock.example.json`，把下列設定合併到隔離專案；保留 `trust: false`，不放憑證。`mcp.allowed` 不是整個 host 的沙箱，仍需核對其他設定來源。

```json
{
  "mcp": {"allowed": ["line-summary-mock"]},
  "mcpServers": {
    "line-summary-mock": {
      "command": "C:\\line-summary-mock\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\line-summary-mock\\plugin_adapter.py",
        "--mode", "mock"
      ],
      "cwd": "C:\\line-summary-mock",
      "trust": false
    }
  }
}
```

4. 在 Qwen Code 使用 `/mcp` 查看狀態。若只是儲存設定而未 Connected，不能繼續假設工具存在。
5. 跑 `line_status`，再跑一個合成查詢、分頁、範圍拒絕、停用測試。所有結果都要標示合成資料。
6. Skill ZIP 解壓後可放入隔離專案的 `.qwen/skills/line-summary-mock/SKILL.md`。這只是指令載入，仍須 MCP 接線；實際載入驗收 `NOT_RUN`。[R33]

目前 Code 端到端驗收 `NOT_RUN`。`@server:uri` 是資源引用語法，不是 Qwen consumer Chat 有 plugin 選擇器的證據。

撤銷：從對應 scope 的設定中移除 `line-summary-mock`，停止子程序並重新檢查 `/mcp`。若仍出現，查其他載入來源，不整份重置使用者設定。

Model Studio API 是另外的整合。本次查到的 Responses API MCP 路徑接受 SSE server，區域與 workspace endpoint 需分別確認。[R19] 本版沒有 SSE endpoint 或 API client；這不代表所有阿里雲產品只有 SSE。

## 14 其他本機桌面 host 的選法

以下每個 host 都只有官方文件路徑，實際安裝與合成驗收均 `NOT_RUN`。先完成第 2 至 5 節，保留工具確認，再按各 host 的正式 schema 填入隔離 Python、mock adapter 與 `--mode mock`。選用雲端模型時，工具結果仍可能離開電腦。

### Cherry Studio

可評估本機 stdio、SSE、Streamable HTTP；官方亦有 Skill ZIP／資料夾導入說明。[R20] 現有查核只有官方搜尋索引與來源連結，全文未成功開啟，因此實際設定頁名稱、ZIP 結構及確認行為仍需按當前版本核對。

操作：先確認 MCP 的 stdio 設定，再新增獨立 mock server，檢查工具和 `line_status`；之後才測 Skill 匯入。若只有 Skill 文字、沒有工具，停止。撤銷時分別移除 MCP entry 與 Skill，再確認子程序退出。

### Jan Desktop

官方列出本機 stdio、HTTP、SSE 和工具確認，另有模型工具能力限制。[R21] 操作：新增 mock MCP，保留逐次確認，用支援工具的模型執行狀態與拒絕測試。模型不支援工具時，不用文字回答冒充呼叫成功。一般 Skill ZIP 未核實；不要把 Jan CLI 的 Skill 功能算成 Desktop 功能。撤銷時移除 server，重開對話確認工具消失。

### AnythingLLM Desktop

官方支援本機 stdio、SSE／Streamable HTTP，MCP 設定使用 `anythingllm_mcp_servers.json`。[R22] 操作：先查該版本設定檔位置，備份後合併 mock entry；在實際 agent 對話跑狀態、搜尋及停用測試。每次 MCP 是否強制確認未核實，需實測。

其 Agent Skill 使用 `plugin.json` 加 `handler.js`，不是共用 `SKILL.md` ZIP。不要把原型改包成任意 JavaScript 就視為已有功能。撤銷時移除精確 MCP entry，停止子程序；若另建 Agent Skill，單獨停用。

### Msty Studio Desktop

官方 Toolbox 支援本機 stdio、Streamable HTTP，舊 SSE 不原生支援。[R23] 操作：確認使用的是 Studio 而非舊 Msty App，依 Toolbox 正式格式新增 mock 工具，再驗證模型實際呼叫。任意 Skill ZIP 和逐次確認尚未核實。撤銷時移除該工具配置、停止子程序，並測試新對話無法再讀取。

這些替代 host 不會補足尚未實作的真實 LINE adapter。某個桌面程式能啟動 Python，也不代表已通過資料出口與多 host 授權要求。

## 15 其他 Web 平台與 Skill 上傳邊界

### LibreChat 與 Open WebUI

LibreChat 自架 Web 支援多種 MCP transport，但 stdio 在 server／容器裡執行；Linux Docker 不會因此讀到 Windows LINE。[R24] 操作：先確認實際執行位置，再把隔離 mock 放在該位置，依官方設定新增 server，驗證工具與資料。撤銷：移除 server 設定、停止相應程序或容器，確認新 session 無工具。

Open WebUI 主產品原生 MCP 路徑為 Streamable HTTP；stdio 可另經 `mcpo` bridge。[R25] 本版沒有 bridge 或 endpoint，因此停在接線未備妥。另建 bridge 需要核准並配置認證；不能拿另一個 Computer 產品的 stdio 能力代替主 UI。撤銷時移除 connector，停止 bridge 並撤銷認證。

### TypingMind

Personal 支援 Skill ZIP／GitHub，Team 尚不支援 Skills；Plugin JSON／URL 是不同格式。[R26] MCP 可另用同裝置 Node.js private connector，但本次沒有建立。操作：先選 Personal 或 Team，再確認要裝 Skill、Plugin 還是 MCP bridge；只有第三者提供對本機工具的連線。成功要有真實工具呼叫；撤銷需同時檢查 host entry、bridge 和其憑證。

### Perplexity

Computer 接受直接 `.md` 或在 ZIP 根目錄放 `SKILL.md`，remote connector 要 HTTPS。[R27] 此平台請選 `line-summary-mock-skill-root.zip`，或直接上傳解出的 `.md`；資料夾式 `line-summary-mock-skill.zip` 不符合它文件列出的根目錄結構。兩種 ZIP 只共用指令，實際 Perplexity 匯入仍 `NOT_RUN`。 操作：可先核對 Skill 格式，但本版沒有 HTTPS endpoint，不可宣稱能讀家中 PC。移除 Skill 不等於撤銷 connector，需分別處理。

Portable Computer 是另一個本機產品；官方列出 Pro／Max、Windows 10／11、NVIDIA 24 GB VRAM 等前提，實際資格及硬體未確認。[R28] 若評估，先核對所有前提再考慮本機 mock，不能把它的能力套用到普通雲端 chat。

### Mistral 及其他 bot 平台

Mistral Vibe chat 有 Skill editor 與 HTTPS MCP connectors；任意 ZIP 匯入未核實，動態工具發現、resources、prompts 也不在已查支援範圍。[R29] 本版沒有 HTTPS endpoint；先停在接線前，勿混用 Vibe CLI 能力。日後撤銷需分別移除 Skill 和 connector。

Copilot Studio 是企業 agent 平台，官方可加 Streamable HTTP MCP；這不代表 consumer Copilot 有通用插件匯入。[R30] 需先確認企業環境、權限與 endpoint，撤銷時移除 agent 的 server 及對應認證。

Poe 的 server／script bot 需要另寫 bot 或 adapter；沒有已確認的任意 MCP／Skill ZIP 安裝路徑。[R31] 本版未做 bot。沒有 client／adapter 的平台，就停在未接通，不能靠貼提示詞取得本機資料。

本節所有 host 和 API 路徑均 `NOT_RUN`。任何要求你上傳整個 repo、資料庫或憑證的捷徑，都不在本版使用方式內。

## 16 外出用手機前的驗收

### 先知道連線走哪裡

手機的 `localhost` 是手機自己；雲端 host 的 `localhost` 也不是家中 PC。要用手機查家中資料，需 host 支援、使用者核准的安全連線，或 host 可達且經適當認證的服務。本版沒有這條連線，Android／iOS 均 `NOT_RUN`。

家中 PC 必須開機、網路可用、未休眠，adapter 與所需 tunnel／bridge 必須存活。只關螢幕、鎖屏、休眠、關機是不同狀態；鎖屏是否影響服務要在實際系統驗證，不能為求便利停用鎖屏或系統保護。

### 只用 mock 的實際驗收順序

1. 先在同一帳號的 Web host 完成連線、合成查詢、拒絕與撤銷測試。未完成前不轉手機。
2. 在 Android 或 iOS 開啟正式 app，確認是否能選到同一個已核准插件／connector。每一種手機 host 分開記錄版本、帳號和結果。
3. 第一次呼叫必須是新的 `line_status`，驗證 mock、synthetic 和固定日期；再跑短範圍查詢。看見先前對話不代表本次手機成功呼叫。
4. 保持 PC 服務運行，鎖定螢幕後再測一次。失敗就記錄鎖屏狀態與錯誤，不關閉安全控制。
5. 讓服務停止或網路斷開後測試。期望結果是明確無法取得新資料，不能用舊內容偽裝即時成功。
6. 在使用者同意的測試條件下驗證休眠及恢復。恢復後先檢查連線與狀態；若程序重啟，丟棄舊 cursor，不自動把未完查詢跨程序接續。
7. 撤銷 connector 或憑證後再測。應不可存取；如果仍有新工具資料，停止使用並檢查還有哪些有效連線。

### 記錄什麼才算通過

每次記錄 host／版本、平台帳號或 workspace 的非敏感識別、PC 狀態、測試時間、實際工具名稱、合成標記、成功／拒絕／斷線結果。不要保存 key、token、真實聊天或資料庫路徑。

本機沒有完成結果快取，也不自動同步 LINE。LINE 尚未同步或來源不可達時，要明說資料新鮮度未知。原 Windows 取 key 流程需要正在執行且已登入的 LINE；不能把 mock 的通過結果當成關閉 LINE、登出或切帳號後也能用的承諾。

## 17 停止撤銷與真實資料前的決定

### 今天結束 mock 測試

1. 停止 MCP client，確認相應 adapter 子程序結束。若手動在終端啟動，正常中止即可；不要透過重啟接續已耗盡的資料額度。
2. 若添加過 host 設定，只移除這次的 mock entry／Skill／plugin，不影響其他服務。
3. 若只是本機 stdio，沒有 remote listener 可關；若日後另建了 tunnel 或 bridge，還需分別停止服務及撤銷權限／認證。
4. 用新的對話或 client 驗證工具不存在、disabled 或連線失敗。舊對話仍保有文字，不等於連線仍開著。
5. 依你選擇的保留方式處理測試輸出及 host session。停服務、刪設定和刪雲端對話是不同操作。

### 真實資料需要另做的工作

每次相關資料呼叫前都讓使用者選擇「去識別化」或「原文」，不強制去識別化為預設。可信核准需確認是使用者本人、本次範圍和指定接收者；不能只因模型填了 `original` 就放行。本版只有合成協商，沒有真實資料傳輸的可用開關。

必須先完成共用輸出檢查，涵蓋訊息、聊天室／人名／ID、搜尋詞、來源參照、錯誤、metadata、stdout／stderr、trace 及 host 二次使用。只有遮蔽訊息本文或電話號碼不足以保障隱私。

敏感分類需考慮醫療、財務、未成年人及跨訊息語境；低信心內容採拒絕或可信本機審閱。可信預覽必須發生在原文送給模型之前，不能先傳出去再問可否傳送。

使用者核准要綁定具名平台／帳號／workspace、聊天室、日期、內容類別、用途、有效期限與預算。模型、聊天內容或網頁不能替使用者批准。密碼、OTP、API key、token 不屬於一般「原文」放行選項，不能回傳或寫入 repo、shell args、範例、日誌及聊天；持續存取與安全設定變更另行確認。

### 資料流與可能成本

本機讀取器／合成來源 → MCP 工具結果 → host／其 session → 所選模型供應商 → 摘要。唯讀、沒有 public port 或不持久快取，都不等於資料不離機；供應商及 host 可能保留對話。真實 LINE 的金鑰程序記憶體也受 OS swap／dump 等風險影響。

可能的費用來自 host 訂閱、模型 API、雲端運算、tunnel／hosting 或其他連線服務。免費本機測試不表示各平台使用免費；以啟用當天的正式方案及帳單為準，本手冊不提供未核實價格。

任何關鍵資格、範圍、接收者、認證或隱私機制尚未確認，就停在合成測試。不要用原 reader、其他工具、shell 或檔案讀取繞過 adapter 的拒絕。

## 18 官方參考與版本核對

以下來源由專案 adapters 與準備文件整理，查核日期為 2026-10-08。產品 UI、資格與格式可能改變；正式安裝前重新開啟相關官方頁面，按實際版本核對。來源描述官方路徑，不等於本專案已在該 host 通過驗收。

- R1 MCP Python SDK 的實際 host 指南：https://py.sdk.modelcontextprotocol.io/get-started/real-host/
- R2 Claude Code MCP：https://code.claude.com/docs/en/mcp
- R3 Claude Code Skills：https://code.claude.com/docs/en/skills
- R4 Claude plugin 結構與各 app 差異：https://claude.com/docs/plugins/build
- R5 Claude remote MCP connectors：https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- R6 OpenAI 自訂 MCP：https://developers.openai.com/api/docs/guides/custom-mcp-server
- R7 OpenAI Secure MCP Tunnel：https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- R8 OpenAI plugin package：https://developers.openai.com/plugins/build/plugins
- R9 Gemini 自訂 tools：https://support.google.com/gemini/answer/17209137?hl=en
- R10 Gemini Apps 隱私中心：https://support.google.com/gemini/answer/13594961?hl=en
- R11 Grok Connectors：https://docs.x.ai/grok/connectors
- R12 Grok 網路要求：https://docs.x.ai/grok/connectors/custom-mcp-tunneling
- R13 Grok Build MCP：https://docs.x.ai/build/features/mcp-servers
- R14 xAI Remote MCP：https://docs.x.ai/developers/tools/remote-mcp
- R15 DeepSeek Harness：https://www.deepseek.com/harness/
- R16 Harness MCP：https://deepseek-harness.github.io/deepseek-harness/en/guide/mcp-memory
- R17 DeepSeek tool calls：https://api-docs.deepseek.com/guides/tool_calls/
- R18 Qwen Code MCP：https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/
- R19 Alibaba Model Studio MCP：https://www.alibabacloud.com/help/en/model-studio/mcp
- R20 Cherry Studio MCP：https://www.cherryai.com/docs/en/advanced-basic/extensions/mcp/；Skills：https://www.cherryai.com/docs/en/advanced-basic/extensions/skills/
- R21 Jan MCP：https://www.jan.ai/docs/desktop/integrations/mcp-servers；模型和確認：https://www.jan.ai/docs/desktop/mcp
- R22 AnythingLLM MCP：https://docs.anythingllm.com/mcp-compatibility/overview；Desktop：https://docs.anythingllm.com/mcp-compatibility/desktop
- R23 Msty Studio Toolbox：https://docs.msty.ai/studio/toolbox/tools
- R24 LibreChat MCP：https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_servers
- R25 Open WebUI MCP：https://docs.openwebui.com/features/extensibility/mcp/；mcpo：https://docs.openwebui.com/features/extensibility/plugin/tools/openapi-servers/mcp/
- R26 TypingMind Skills：https://docs.typingmind.com/skills；Plugin：https://docs.typingmind.com/plugins/share-import-plugins；private connector：https://docs.typingmind.com/model-context-protocol-(mcp)-in-typingmind/use-mcp-with-private-mcp-connector
- R27 Perplexity Computer Skills：https://www.perplexity.ai/help-center/en/articles/13914413-how-to-use-computer-skills；connectors：https://www.perplexity.ai/help-center/en/articles/13915507-adding-custom-remote-connectors
- R28 Perplexity Portable Computer：https://www.perplexity.ai/help-center/en/articles/20260915-what-is-portable-computer
- R29 Mistral Vibe Skills：https://docs.mistral.ai/vibe/work/skills；MCP：https://docs.mistral.ai/vibe/work/connectors/mcp-connectors
- R30 Copilot Studio MCP：https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent
- R31 Poe server bots：https://creator.poe.com/docs/server-bots/quick-start；script bots：https://creator.poe.com/docs/script-bots/quick-start
- R32 Claude 自訂 Skills ZIP：https://support.claude.com/en/articles/12512198-how-to-create-custom-skills
- R33 Qwen Code Skills：https://qwenlm.github.io/qwen-code-docs/en/users/features/skills/

原始 repo 的細節以 README、MIGRATION、docs/PLUGIN_PREPARATION、docs/PRIVACY_READINESS、各 adapters README 及共用 Skill 為準。遇到範例與當前工具 schema 不符，先停止並核對，不讓模型自行猜欄位或改權限。
