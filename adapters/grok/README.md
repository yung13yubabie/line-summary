# Grok：Web、Build CLI 與 API 是不同接入面

查核日期：2026-10-08。本版共用 `../../plugin_adapter.py`，只有 stdio disabled/mock，沒有 HTTP／SSE endpoint 或實際 Grok 連線。

- grok.com Web：官方 Connectors 頁提供 New Connector → Custom → server URL。server 必須能由網際網路連達；localhost／私有 IP 會被拒絕。尚未確認使用者帳號與手機／Desktop chat 的自訂 MCP、`@` 體驗。[官方 Connectors](https://docs.x.ai/grok/connectors) · [官方網路要求](https://docs.x.ai/grok/connectors/custom-mcp-tunneling)
- Grok Build CLI：官方另有本機 stdio／遠端 HTTP MCP 設定；這不是原版手機或 Web chat。官方也會讀取其他客戶端的相容設定，包含專案 `.mcp.json`；本專案原 `.mcp.json` 指向 raw reader，後續測試前必須核對載入來源並隔離它，不能意外啟動。[官方 Build MCP](https://docs.x.ai/build/features/mcp-servers)
- xAI API：官方 Remote MCP 能透過 URL 使用 MCP；API 路徑需獨立 client／認證與資料授權。文件指出相容 Responses API 的 `require_approval` 參數目前不支援，不能把它當作已生效的安全 gate。[官方 Remote MCP Tools](https://docs.x.ai/developers/tools/remote-mcp)

本次沒有啟用任何以上路徑或建立憑證。沒有為了 Web 公網需求建 bridge 或 tunnel；不提供能讓 raw LINE reader 上網的替代指令。未來接線先以合成資料驗證，真實資料依 [隱私門檻](../../docs/PRIVACY_READINESS.md) 另行設計與授權。

## 每次資料呼叫先選擇隱私方式

AI 必須先說明具名接收方與本次聊天室／日期／查詢，詢問「先去識別化」或「保留原文」；未選、取消、未知接收方或 scope 改變就不回資料。這是 [共用 mock 協商](../../docs/PLUGIN_PREPARATION.md#呼叫前的-mock-隱私選擇)，不是此平台已驗證真人批准的機制。模型傳 original 參數不能解鎖 real，去識別選項也不表示處理器已實作。憑證類仍不一般放行；真實敏感原文預覽只能先在本機可信介面處理。
