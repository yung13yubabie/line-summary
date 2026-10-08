# DeepSeek：原版 Chat 未確認；Harness／API 分開評估

查核日期：2026-10-08。本版只有共用 stdio disabled/mock adapter，沒有 DeepSeek 特有 reader、API client 或安裝包。

- 原版 Web／Mobile／Desktop chat：沒有核實到可安裝此自訂 MCP／Skill 的官方路徑。不能宣稱在 chat.deepseek.com 或 Android 輸入 `@` 即可使用。
- DeepSeek Harness：官方獨立開發者工具，具 plugin／skill 系統，MCP client 支援本機 stdio 及 Streamable HTTP。它與一般 DeepSeek Chat 不同；本次未安裝或跑 host 整合測試。[官方 Harness](https://www.deepseek.com/harness/) · [官方 MCP 文件](https://deepseek-harness.github.io/deepseek-harness/en/guide/mcp-memory) · [官方 MCP client 原始文件](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)
- DeepSeek API：tool calls 要由開發者的 client 執行工具並回傳結果，並非模型直接啟動使用者 PC 上的 MCP。本版沒有建立此呼叫迴圈或使用 API key。[官方 tool calls](https://api-docs.deepseek.com/guides/tool_calls/)

未來若使用 Harness 或自建 client，可沿用同一份六工具 schema 與 [synthetic Skill](../../plugin/skills/line-summary-mock/SKILL.md)，但必須按 host 的正式格式整合與實測。載入 Skill 不建立連線或資料權限。還要審查 host 的 session／tool-result 日誌及其模型資料流；「本機執行」不能代替 [隱私門檻](../../docs/PRIVACY_READINESS.md)。

## Harness 專用 Cordis overlay：不是 consumer Chat JSON

`harness.mock.example.cordis.yml` 是 **JSON 相容 YAML**，以 Cordis `insert` patch 載入官方 `@deepseek-ai/dsh-mcp-client`。`id` 是這份本機設定的 entry 名稱，不是平台 plugin ID；沒有建立供應商帳號或註冊插件。`reconnect.enabled: false` 避免此示範自動重啟 adapter 而重置程序內預算。

先將合成示範包解壓到原 repo／父層設定之外的全新資料夾，替換所有 `C:\REPLACE_WITH_ISOLATED_MOCK_DIR`。審查既有 Harness profile／patch 是否另有 MCP，不能把單一新增 overlay 誤當完整隔離。以下只是在使用者日後自行核對版本、profile 與帳號後的官方命令形狀，**本次沒有執行**：

```powershell
Set-Location 'C:\REPLACE_WITH_ISOLATED_MOCK_DIR'
dsh web --patch 'C:\REPLACE_WITH_ISOLATED_MOCK_DIR\adapters\deepseek\harness.mock.example.cordis.yml'
```

這會啟動 Harness 自己的本機 Web UI；不是 LINE adapter 的 HTTP endpoint，也不是 DeepSeek consumer 網頁。LINE adapter 仍是 stdio。工具由 host 加上 `mcp__line-summary-mock__` 名稱空間；先驗證狀態工具真正回傳 mock／synthetic／real_data_enabled:false，不以 UI 已開啟當作成功。

官方依據（查核：2026-10-08）：[Cordis overlay 與 --patch 用法](https://deepseek-harness.github.io/deepseek-harness/en/guide/mcp-memory)、[MCP client 欄位／名稱空間／重連設定](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)。本次只有設定格式與本機合成協定驗證；Harness 真正載入、工具發現及模型呼叫一律 `NOT_RUN`。未核實此 Harness 對獨立 Skill ZIP 的完整匯入流程，因此不提供虛構的 Skill 上傳命令。

資料查詢前，仍須依 [詳細操作手冊](../../docs/DETAILED_USAGE_GUIDE.md) 當次確認接收者、範圍及「去識別化／原文」選擇，並完成 mock scope 協商。這只是 synthetic 流程示範，不能證明真人批准或已實作去識別化，也不會啟用真實資料。

## 每次資料呼叫先選擇隱私方式

AI 必須先說明具名接收方與本次聊天室／日期／查詢，詢問「先去識別化」或「保留原文」；未選、取消、未知接收方或 scope 改變就不回資料。這是 [共用 mock 協商](../../docs/PLUGIN_PREPARATION.md#呼叫前的-mock-隱私選擇)，不是此平台已驗證真人批准的機制。模型傳 original 參數不能解鎖 real，去識別選項也不表示處理器已實作。憑證類仍不一般放行；真實敏感原文預覽只能先在本機可信介面處理。
