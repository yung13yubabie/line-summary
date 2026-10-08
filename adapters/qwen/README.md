# Qwen：Code CLI 與 Model Studio API 不等於 Qwen Chat

查核日期：2026-10-08。本版共用 `../../plugin_adapter.py` 只有 stdio disabled/mock，沒有 API client、SSE server 或平台帳號設定。

- 原版 Qwen Web／Mobile／Desktop chat：未確認可匯入此自訂 MCP 或 Skill。不能宣稱某個 CLI 的 `@` 語法就是一般 Chat 的插件選擇器。
- Qwen Code CLI：官方 `settings.json` 的 `mcpServers` 支援本機 stdio、HTTP、SSE，stdio 用 command／args，可選 cwd／env。`@server:uri` 是資源引用，不證明 Qwen consumer chat 具同一功能。[官方 MCP 文件](https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/)
- Alibaba Cloud Model Studio API：此次所查 Responses API MCP 路徑接受 SSE server，區域／workspace endpoint 需分別配置。這只描述該 API 路徑，不能推廣為所有阿里雲產品的 transport 上限。[官方 Model Studio MCP](https://www.alibabacloud.com/help/en/model-studio/mcp)

本次沒有設定 `trust: true`、存憑證或呼叫模型，也沒有把 stdio 原型變成遠端 endpoint。後續若採 Code，可先在獨立 mock 設定中手動配置共同入口，再檢查 `/mcp` 狀態與 `line_status`；實際 host 相容性仍待驗證。[通用測試參考](../../plugin/README.md)

先確認要用的明確產品面、帳號及資料接收者，並依 [隱私門檻](../../docs/PRIVACY_READINESS.md) 辦理；不要因 API 支援工具就製作假的 Qwen Chat 安裝包。

## 可檢查的本機設定範例

`settings.mock.example.json` 是合法 JSON、採官方 Qwen Code schema，並明確保留 `trust: false`；`mcp.allowed` 僅允許本例的 server 名稱。這不是 Qwen Chat 安裝包。所有實際 host 測試狀態為 `NOT_RUN`。

1. 先將合成示範 ZIP 解壓至原始 repo 及其父層設定之外的新資料夾。以下的 `C:\REPLACE_WITH_ISOLATED_MOCK_DIR` 只代表該新資料夾，不可指回 raw source。
2. 替換範例的 Python、adapter 和 cwd 三個絕對路徑。先備份既有設定，由使用者自行將內容合併到該隔離資料夾的 `.qwen/settings.json`。本次未建立任何自動載入檔或修改使用者設定。
3. 啟動前審查目前 host 的 user／system／extension 設定是否另有 LINE 來源；同名設定可能覆寫 entry。若無法確認隔離，停止，不用 raw reader 補救。`mcp.allowed` 不是停用其他 host 功能或憑證的沙箱。
4. 使用者之後若決定實測，從此隔離資料夾啟動 Qwen Code，在 `/mcp` 確認實際 server 與六工具，再呼叫 `line_status`。只在 mock、synthetic、real_data_enabled:false 全部符合時繼續。

CLI 的正式形狀為 `qwen mcp add [options] <name> <commandOrUrl> [args...]`，scope 預設是 user。此交付優先提供明確的 project JSON，避免不小心寫到全域範圍；沒有執行 add／login 或工具模型呼叫。[官方 schema、scope 與 CLI](https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/)（查核：2026-10-08）。

共用 Skill 解壓後，可由使用者另行放到隔離專案的 `.qwen/skills/line-summary-mock/SKILL.md`；這只載入指令，工具接線仍須分開完成。[Qwen Code Skills](https://qwenlm.github.io/qwen-code-docs/en/users/features/skills/)（查核：2026-10-08）。

資料查詢前，仍須依 [詳細操作手冊](../../docs/DETAILED_USAGE_GUIDE.md) 當次確認接收者、範圍及「去識別化／原文」選擇，並完成 mock scope 協商。這只是 synthetic 流程示範，不能證明真人批准或已實作去識別化，也不會啟用真實資料。

## 每次資料呼叫先選擇隱私方式

AI 必須先說明具名接收方與本次聊天室／日期／查詢，詢問「先去識別化」或「保留原文」；未選、取消、未知接收方或 scope 改變就不回資料。這是 [共用 mock 協商](../../docs/PLUGIN_PREPARATION.md#呼叫前的-mock-隱私選擇)，不是此平台已驗證真人批准的機制。模型傳 original 參數不能解鎖 real，去識別選項也不表示處理器已實作。憑證類仍不一般放行；真實敏感原文預覽只能先在本機可信介面處理。
