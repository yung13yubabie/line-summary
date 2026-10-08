# Claude：本機 mock 設定參考，尚未安裝

查核日期：2026-10-08。本目錄只提供使用相同 `plugin_adapter.py` 的手動範例。沒有替各模型複製資料邏輯，沒有變更使用者電腦設定。

## Claude Desktop 本機 stdio

`desktop.mock.example.json` 依官方 MCP client 設定形狀，僅指向 mock adapter。Windows 的設定位置為 `%APPDATA%\Claude\claude_desktop_config.json`；macOS 為 `~/Library/Application Support/Claude/claude_desktop_config.json`。[MCP SDK 官方 host 指南](https://py.sdk.modelcontextprotocol.io/get-started/real-host/)

由使用者決定手動配置時：

1. 先將合成示範包解壓到原始 repo 及其父層設定之外的新資料夾，並配置鎖定依賴。JSON 的 Python／adapter 路徑必須指向該隔離 mock 資料夾；macOS 改為相應 POSIX 路徑，不能指回原始 repo。
2. 備份既有設定，只合併新的 `mcpServers` entry，不覆蓋其他 server 或設定。
3. 完整退出再重開 Claude Desktop，檢查 server 的實際連線狀態與六個工具。
4. 先呼叫 `line_status`。必須確認 mock／synthetic、`real_data_enabled: false`，再使用它提供的範例日期測試。
5. 本地載入工具不表示結果留在本機；交給雲端模型的工具輸出仍進入其對話。本版只含合成資料。

本版沒有實際安裝或 Desktop 端驗收。mock 可測試不表示原 Windows LINE reader 能在 macOS 讀取真實帳號。

## Claude Code CLI

以下是使用者之後自行核對版本、帳號與設定的手動流程，全部尚未執行。優先用單次明確設定，並從隔離 mock 根目錄啟動：

```powershell
Set-Location 'C:\REPLACE_WITH_ISOLATED_MOCK_DIR'
claude --strict-mcp-config --mcp-config 'C:\REPLACE_WITH_ISOLATED_MOCK_DIR\adapters\claude\desktop.mock.example.json'
```

先替換 JSON 內的絕對路徑。`--strict-mcp-config` 使一般情況下只載入明確給定的 MCP 設定；組織管理的 MCP policy 可能覆蓋它，必須先核對，不能把這個參數當作整個 host 的沙箱。`/mcp` 必須顯示正確 adapter 與工具，接著驗證 `line_status`。[官方 MCP 隔離設定](https://code.claude.com/docs/en/mcp)（查核：2026-10-08）。

若使用者另外決定儲存專案綁定的 local scope，官方 CLI 形狀如下；它會修改 Claude Code 設定，不是本次已執行的動作：

```powershell
Set-Location 'C:\REPLACE_WITH_ISOLATED_MOCK_DIR'
claude mcp add --transport stdio --scope local line-summary-mock -- "C:\REPLACE_WITH_ISOLATED_MOCK_DIR\.venv\Scripts\python.exe" "C:\REPLACE_WITH_ISOLATED_MOCK_DIR\plugin_adapter.py" --mode mock
```

`Added` 只表示寫入設定，不證明 Connected。專案原有 `.mcp.json` 是原始 reader 的另一個入口；不得用它作為被拒絕資料的替代來源，更不能在 raw checkout 中啟動 host。實際 Claude Desktop／Code 驗收均為 `NOT_RUN`。

## Web／Desktop Chat／Mobile：區分三種東西

- Skill 是工作流指令，不安裝本機 server，也不賦予帳號資料讀取權限。
- Claude Chat 可使用 plugin，但 plugin ZIP 內的本機 command MCP 元件在 Chat 被忽略；本機 Cowork／Claude Code 才是該元件的支援環境。Desktop Chat 的本機 server 需另外配置或採正式桌面 extension，不能靠上傳 ZIP 代替。[官方 plugin 結構與各 app 差異](https://claude.com/docs/plugins/build)
- Web／Mobile 的 remote connector 由 Anthropic 雲端連向 server，不能連使用者電腦的 `localhost`。本版沒有遠端 endpoint，沒有驗證手機 `@`、remote connector 或 API。[官方 remote MCP connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)

這裡沒有製作 `.mcpb`、完整 Claude plugin ZIP 或遠端服務。若另備獨立 Skill，應先檢查工具存在，再呼叫 `line_status`；若 disabled、工具缺少或 schema 不符，停止並說明，不得透過 shell、檔案或其他連接器偷讀原始 LINE。輸出只依實際工具結果，mock 必須標為合成資料。

[回平台矩陣](../README.md) · [隱私門檻](../../docs/PRIVACY_READINESS.md)

資料查詢前，仍須依 [詳細操作手冊](../../docs/DETAILED_USAGE_GUIDE.md) 當次確認接收者、範圍及「去識別化／原文」選擇，並完成 mock scope 協商。這只是 synthetic 流程示範，不能證明真人批准或已實作去識別化，也不會啟用真實資料。

## 每次資料呼叫先選擇隱私方式

AI 必須先說明具名接收方與本次聊天室／日期／查詢，詢問「先去識別化」或「保留原文」；未選、取消、未知接收方或 scope 改變就不回資料。這是 [共用 mock 協商](../../docs/PLUGIN_PREPARATION.md#呼叫前的-mock-隱私選擇)，不是此平台已驗證真人批准的機制。模型傳 original 參數不能解鎖 real，去識別選項也不表示處理器已實作。憑證類仍不一般放行；真實敏感原文預覽只能先在本機可信介面處理。
