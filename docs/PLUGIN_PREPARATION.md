# LINE MCP 插件準備：本機合成原型

狀態日期：2026-10-08。這份文件區分可測試的本機程式、官方連線路徑與尚未完成的實際安裝。**目前沒有連上任何模型平台或真實 LINE。** ChatGPT、Gemini、Grok、Claude、DeepSeek、Qwen 的各產品面與待接狀態見 [平台矩陣](../adapters/README.md)。

## 1. 交付範圍

- `plugin_adapter.py` 是獨立的 stdio MCP 入口；只接受預設 `disabled` 與明確選用的 `mock`。
- `disabled` 不建立資料來源；`mock` 只使用程式建立的暫存 SQLite 合成資料，不接收帳號資料庫路徑。
- 六個工具有 MCP 輸入 schema、結構化輸出與唯讀提示：`line_status`、`line_list_chats`、`line_get_history`、`line_search_messages`、`line_get_unread`、`line_get_contacts`。提示不等於安全授權或平台審核。
- `plugin_runtime.py` 提供本程序流量控制與相同進行中請求的合併，沒有跨請求持久結果快取。
- `plugin_settings.example.json` 預設停用；未知設定欄位、真實資料模式、`db_path` 與宣告隱私已就緒的布林開關都被拒絕。沒有靠改設定即可啟用的 live 模式。
- `plugin/mock-client.example.json` 只是通用 MCP 測試客戶端參考；不是 ChatGPT 或 Gemini 的安裝套件。

原 repo 的 `line_mcp_server.py`、`.mcp.json` 與 Claude Code 操作說明屬於另一個本機核心入口，這些入口不會放進隔離示範包。所有 consumer／CLI 測試應使用隔離的 mock profile 或設定；啟動前核對 host 是否會自動探索專案、家目錄或其他客戶端設定，避免載入原 `.mcp.json`。原核心的真實讀取依賴 Windows LINE；本次沒有新增 macOS 讀取器、重建 LINE 回覆討論串，或驗證真實帳號。新 adapter 沒有匯入原 server 或其金鑰擷取器。**不要用原核心替換 adapter 作為遠端接入入口；這會跳過此原型禁止真實資料的邊界。**

本次原型沒有建置 HTTP server、開放 port、建立 tunnel、產生憑證、登入平台、建立平台插件或部署。將本地程式修正發布至 repo 不代表接通上述服務。

### 交付布局與隔離使用

- 主示範 ZIP 是固定 allowlist 建置的本機 source/test bundle，不是平台 installer。根目錄 README 取自 `plugin/DEMO_README.md`，只引導合成測試。
- ZIP 不含原 `.mcp.json`、`.claude`、raw server、金鑰擷取器、帳號設定、live 測試或 repo 原始 autoload 入口。包含 adapter、共用 reader 的合成路徑、必要測試、指南及範例。
- `BUNDLE-MANIFEST.json` 記錄檔案清單、hash 與來源基準 commit，用來核對包內內容；不是已通過平台認證或簽章的證明。
- 另附的 `repo.patch` 是供維護者在指定基準 repository 上審阅及套用的變更，不是平台 plugin／Skill 安裝檔。不要將 patch、整個原 repo 或主 source ZIP 上傳當作平台插件。
- 將示範包解壓到原 repo 與其父層設定之外的新資料夾，使用隔離 mock profile。包內沒有 autoload 檔，不代表能停用使用者已存在的全域真實 LINE 註冊；測試前仍要檢查 host 載入來源。
- 本機測試與平台安裝分開：示範包只證明可啟動同一個 synthetic stdio 原型，未建立任何帳號連線。共用 Skill 也只含工作流指令。

## 2. 只在本機做合成測試

先使用已依專案鎖定依賴配置好的 Python 環境。在隔離示範包根目錄或已隔離設定的開發目錄執行：

```sh
# 預設停用；stdio 等待 MCP client 輸入，沒有網頁畫面。
python plugin_adapter.py

# 只使用合成資料；結束測試時由 client 關閉子程序。
python plugin_adapter.py --mode mock

# 離線測試，仍不啟用任何 live LINE 測試。
python -m pytest -m "not integration"
```

Windows 可將 `python` 換成專案虛擬環境的 `.\.venv\Scripts\python.exe`；其他作業系統使用自己的虛擬環境路徑。mock 能在合適的 Python 環境測試，不表示真實 LINE 讀取已跨平台。

使用支援 stdio 的本機 MCP 測試客戶端，將執行檔、專案絕對路徑與工作目錄設定好。參考 [測試客戶端說明](../plugin/README.md)。應先完成 MCP `initialize`，再 `tools/list` 確認六個工具，最後 `tools/call`。不要把 JSON 範例上傳到任何平台當作已完成安裝。

建議依序檢查：

1. 預設入口列得出工具，但工具呼叫回覆 `status: "disabled"`，且沒有讀取真實資料。
2. mock 的 `line_status` 顯示 `real_data_enabled: false`、`privacy_filter_implemented: false`、`automatic_sync: false`。以其中的 `sample_window` 為準；固定資料不是「今天」的資料。
3. 查 `line_list_chats`，僅取得允許的合成聊天室；向 `demo-denied` 查歷史或搜尋應遭拒絕。
4. 用下列合成查詢確認中文與特殊字元搜尋，再以 `limit: 1` 測試同一查詢的 cursor 分頁。
5. 查未讀與聯絡人，確認皆為合成範例；測試無時區、反向日期、超過 31 天、變造／跨範圍 cursor 等拒絕情況。
6. 驗證流量、byte 與訊息預算不足時的拒絕，不靠重啟程序或切換工具繞過限制。

以下是已初始化 client 的 `tools/call` 參數，不是獨立可執行檔：

```json
{
  "name": "line_search_messages",
  "arguments": {
    "chat_id": "demo-project",
    "query": "報價",
    "destination": "local-test",
    "since": "2026-10-01T00:00:00+08:00",
    "until": "2026-10-02T00:00:00+08:00",
    "limit": 1
  }
}
```

同一日期範圍也可查 `50%_test`。`%`、`_` 必須照字面比對。每個測試間保留至少設定的冷卻時間；遇到 `retry_after_seconds` 時等候，不要快速輪詢。mock 固定日期若日後更改，先用 `line_status` 重新確認。

### 測試與安裝的證據分開記錄

- 已驗程式 snapshot `2ccc29e3…`：Linux、Python 3.12.14，472 passed、5 live skipped，runtime coverage 92.52%（branch 計量，85% 門檻保留）；另有獨立 QA 13 passed，分開計數。
- 較早 snapshot 的隔離示範包曾有 332 passed；本輪最終隔離包全套為 `NOT_RUN`，不能沿用舊數字。
- 遠端 synthetic CI 須核對同一提交；它不啟用 live LINE，也不等於真實 Windows reader 或模型 host 驗收。實際平台、手機、tunnel、登入與真實 LINE 本輪仍為 `NOT_RUN`。
- 仍有上游 FastMCP／Pydantic lifespan warning，可顯示套件安裝路徑。合成協商不是可信真人授權，去識別與跨程序總配額仍未實作。
- `.coveragerc` 計入 `plugin_adapter`、`plugin_runtime` 與 `mock_privacy`；隔離示範包不帶原 repo coverage 設定，兩種測試數不可混用。

## 呼叫前的 mock 隱私選擇

除狀態檢查外，每次資料讀取先說清楚接收方與本次聊天室、日期、query，再詢問去識別／原文。未知接收方、缺選擇或改 scope 時回覆 `requires_confirmation`，`items` 必為空。示範接收方為 `local-test`、`chatgpt`、`claude`、`gemini`、`grok`、`deepseek`、`qwen`；這只是標籤，不會建立模型連線。

先取得本輪 `mock_scope_id`，明確選擇後原樣帶回；它是新的非持久示範 ID，另用 `scope_fingerprint` 綁定參數，120 秒後不接受新執行／加入，不能反覆重播或跨程序使用。模型傳入 `privacy_choice:original` 不代表真人批准。兩種選擇均只回合成資料，永遠 `human_confirmation_verified:false`、`privacy_processing:not_implemented`，不是匿名化完成或 real 啟用機制。憑證類不設一般放行選項。

安全的本機示範可用：

```sh
python tools/mock_cli.py status
python tools/mock_cli.py chats
python tools/mock_cli.py chats --interactive
python tools/mock_cli.py self-test
```

未選的 chats 只印空資料預覽；interactive 在本機詢問。self-test 自動示範選原文但只有合成 fixtures，並標 host/live 為 NOT_RUN。每次 CLI 都是新子程序；分頁要用保留同一 session 的 MCP client，不沿用另一個 CLI 的 cursor，也不透過重開 CLI 逃配額。

## 3. 工具契約與資料限制

### 歷史與關鍵字搜尋

- `line_get_history`／`line_search_messages` 限定一個允許的聊天室及帶時區的半開時間範圍 `[since, until)`，範圍最多 31 天。
- 訊息搜尋使用 SQLite `instr` 與參數綁定，做**區分大小寫的 Unicode 字面子字串比對**。query 為 1–256 字元，不能全是空白；其餘空白保留。沒有大小寫折疊、Unicode 正規化、正規表示式、萬用字元、斷詞或語意搜尋。
- 沒有「前後各 N 則」、自動上下文擴張或回覆討論串重建。若未來需要，必須另外設計範圍授權、預算與測試。
- cursor 綁定聊天室、原樣 query 與日期範圍。下一頁沿用這些值及原 cursor；不可自行解碼、修改、挪給另一查詢。cursor 不是永久書籤。
- 搜尋來源參照包含本機聊天室 ID、訊息 ID 與時間，不是假造可點擊的 LINE 網址，也不能證明其他訊息已同步。
- 分頁只涵蓋本機符合條件的列。資料修改、刪除與 rowid 重用可影響遍歷；`live_keyset_scan` 不是完整快照。

### 新鮮度與未讀

- `fetched_at`：此次產生回覆的 UTC 時間，不是 LINE 同步時間。
- `source_sync_at`：固定 `null`，沒有可驗證的 LINE 同步證據。
- `source_latest_at`：來源若提供，只是允許範圍內最新本機記錄的時間。搜尋限定該聊天室及請求日期範圍，未必是符合關鍵字的最新結果；未知時為 `null`。
- `latest_returned_at`：本頁實際回傳訊息的最新時間；未知或沒有訊息時為 `null`。訊息顯示時區為 `+08:00`，host 可依使用者指定時區換算。
- 未讀是本機計數搭配最近記錄的近似取樣；`selection: "latest_local_approximation"`、`sync_status: "unknown"`、`unread_boundary_verified: false`。回傳訊息可能都已讀，不能保證真正未讀邊界。
- 沒有自動同步、背景監聽、補抓雲端歷史或捲動 LINE。空結果不能推導為「沒有聊過」；本機最新不能改寫成「剛同步完成」。

### 流量與輸出預算

預設每程序每 60 秒最多 10 個受理呼叫、新 backend 讀取間隔至少 2 秒、同時最多 1 個 backend 讀取。這些是可在 schema 範圍內調整的**本機設計值，不是 OpenAI、Google 或 LINE 官方上限**。平台自己的限制、錯誤與資格需另外驗證。

完全相同且仍在進行中的請求可共用一次 backend 工作；每個受理呼叫仍耗用自己的 rate slot，每份送出的結果仍各自計入訊息及 byte 預算。不同的並行查詢回覆 `busy`。沒有完成結果快取、背景結果資料庫或跨程序去重。

預設每次最多 100 則訊息、256 KiB JSON payload；整個程序最多 5,000 則及 2 MiB。累計預算計入成功交付的資料／狀態 JSON payload，每份 duplicate 仍獨立計入。固定、空資料的拒絕／disabled error envelope 不累加到 session 預算，但每份仍受單次硬上限約束。預算不包含 MCP 外層封裝，不能宣稱所有傳輸 raw bytes 或網路 wire bytes 有總上限。byte、訊息與 rate 控制目的不同，不能互相替代。`has_more`／`content_complete` 只描述交付狀況，不宣告整個 LINE 歷史完整。

到達預算、首項過大或缺少可用 cursor 時停止並說明不完整；不得透過改 query、切日期、換工具、自動提高額度或重啟避開限制。這些 process-local 限制也不是多程序或多 host 的全域配額。

## 4. 官方接入路徑：文件存在，尚未實裝

以下依 2026-10-08 查閱的官方文件。產品功能可能改變；真正接線前重新查證帳號畫面、方案、地區及管理員政策。

### ChatGPT

官方目前列出網頁版 Plugins → Add custom MCP server，以 Server URL 或 Tunnel 連線，再設定認證、審閱風險並安裝；已安裝插件可在對話以 `@` 選取。URL 路徑支援 SSE／streaming HTTP。[官方自訂 MCP 說明](https://developers.openai.com/api/docs/guides/custom-mcp-server)

Secure MCP Tunnel 可在私有環境內接 stdio 或 HTTP MCP，透過對外 HTTPS 工作，不要求私有 MCP 直接開 public listener。它仍會把工具結果傳往 OpenAI。前提包括 Platform tunnel、runtime API key、相應 tunnel 權限，並把正確 Platform organization／ChatGPT workspace 關聯好；ChatGPT workspace 的自訂 MCP 權限是另一道要求。[官方 Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

因此，本機 stdio 原型是未來可用的協定部分，但本資料夾沒有 tunnel 或平台插件身分。使用者實際方案、帳號可見選項及 Android 端使用情況尚未驗證。一般插件頁面不能代替此帳號的實測。[ChatGPT Plugins 說明](https://learn.chatgpt.com/docs/plugins)

### Gemini

Google 官方提供自訂 MCP tools 的網頁設定與網頁／行動裝置 `@` 使用流程，但目前列有美國、英文、年滿 18 歲、個人 Google 帳號與 Keep Activity 開啟等資格限制。**不能承諾台灣帳號現在能使用。**應先查看實際帳號及最新資格，不為此自動變更地區、語言、活動紀錄或帳號設定。[官方自訂 tools 說明](https://support.google.com/gemini/answer/17209137?hl=en)

目前這條官方流程接受 server 網址；不能把 Gemini CLI、一般 Extensions 或 stdio 客戶端的能力當成 Gemini App 的本機接線證明。未確認 Gemini App 官方保證支援此本機 tunnel；本次沒有建立可供它接入的遠端 URL，也沒有驗證與此 adapter 的端到端相容性。Keep Activity 的資料處理應單獨審閱。[Gemini Apps 隱私中心](https://support.google.com/gemini/answer/13594961?hl=en)

### 安裝包邊界

ChatGPT 的 plugin 包裝格式與測試／發布是獨立流程；應按官方 package 規格另行製作與驗證。[官方 Package your plugin](https://developers.openai.com/plugins/build/plugins)

本目錄沒有聲稱符合該規格的 manifest 或 ZIP，也沒有 Gemini App 可匯入的共同安裝格式。通用 MCP JSON、README、ZIP 裡放一個 Python 檔，都不等於「一包裝好兩個平台」。

### 其他平台與共用 Skill

Claude Desktop／Code、Grok Web、DeepSeek Harness／API、Qwen Code／Model Studio 的路徑與不確定處分別放在 [adapters/ 平台指南](../adapters/README.md)，不得將 CLI／API 支援推論成一般手機 Chat 可安裝。只有一套 stdio 核心與 [共用 synthetic-only Skill](../plugin/skills/line-summary-mock/SKILL.md)，沒有六份重複資料邏輯。Skill 是指令，不是連線、權限或平台通用安裝包。

## 5. 外出手機使用：下一階段的操作概念

共用 MCP 加 provider-neutral Skill，不是同一個 ZIP 可在所有平台直接安裝。手機雲端 host 若要使用家中 PC 的資料，仍需一條另經使用者授權、符合 host 支援方式的安全連線；手機的 `localhost` 不能連到家中 PC。**目前 mock 原型沒有這條連線，也未驗證實際手機 `@`。**聊天室／日期等 scope、資料接收者、憑證與 persistent access 的授權均屬下一階段。

PC 需在線、服務存活且未休眠。鎖屏不等於服務必然停止，也不保證仍可用；要在選定作業系統與 host 下用 mock 分別驗證，不建議關閉鎖屏或其他安全保護。斷線、休眠或 LINE 尚未同步時，清楚回報無法取得所需的新資料；不能把先前結果或本機舊記錄冒充最新內容，也不能強制補抓 LINE。原型沒有完成結果快取或背景同步。

server 重啟後既有 cursor 失效，process-local 配額也重新開始；這是程序生命週期限制，不是允許 host 自動重啟逃限流／預算。沿用 `retry_after_seconds` 的最短等待時間，從手機或其他外端呼叫也不可密集輪詢。只在明確的新範圍與授權下開始新工作，不為追求即時結果自動擴權。

## 6. 未來接真實資料前的順序

1. 先完成 [隱私就緒門檻](PRIVACY_READINESS.md)：全輸出路徑隔離、敏感資訊分類、可信本機預覽與範圍核准等。本版本不具備這些機制。
2. 由使用者選擇具名接收平台、帳號／workspace、資料類別、聊天室、日期、用途與保留方式。資料在本機不等於模型看不到回傳內容。
3. 驗證 Windows 真實 reader 的現行相容性與授權邊界；不得由 mock 自動降級到原核心或任意檔案路徑。
4. 重新查明目標平台的帳號資格、host 協定與管理員權限。先以合成資料進行平台測試。
5. 建立 persistent access、憑證、tunnel 或相關安全設定前逐項取得相應確認；憑證由使用者透過安全方式處理，不寫進 repo、shell 範例、日誌或聊天。
6. 經核准後才製作相應平台包裝／認證與接線，逐項驗證工具發現、呼叫、錯誤、停用與撤銷。不能把「工具出現」當成「傳輸隱私正確」。
7. 只有通過全流程測試且使用者明確授權具體真實資料傳輸後，才能考慮首次有限讀取。沒有授權、範圍不明或分類不確定時拒絕輸出。

本文件只備妥後續步驟，不會觸發任何安裝、憑證建立或傳輸。
