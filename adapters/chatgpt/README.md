# ChatGPT：官方路徑已確認，帳號未接線

查核日期：2026-10-08。共用入口為 `../../plugin_adapter.py`，只提供 stdio disabled/mock；本目錄沒有獨立 reader、平台 manifest 或憑證。

- Web：官方提供 Plugins → Add custom MCP server → Server URL／Tunnel，安裝後可 `@` 選取。[官方自訂 MCP](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- Desktop／Android／iOS：本次沒有用實際帳號驗證此自訂插件的安裝及呼叫，不能由 Web 文件推定全部客戶端皆可。
- API／CLI：是另一種 host 整合面；本次沒有製作或執行 API client。官方 tunnel 可供受支援 OpenAI 產品使用，不等於本插件已在其上測試。

未來可評估官方 Secure MCP Tunnel 的 stdio 路徑；它需 Platform tunnel、runtime key、tunnel 權限及正確 organization／workspace 關聯。資料回覆仍會送往 OpenAI；沒有 public listener 不等於沒有雲端資料傳輸。[官方 tunnel 文件](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

本次只備妥本機合成協定。實際方案與 workspace 資格、認證、安全確認及首次接線都尚未完成；沒有自動設定或可直接執行的 tunnel 指令。先閱讀 [準備說明](../../docs/PLUGIN_PREPARATION.md) 與 [隱私門檻](../../docs/PRIVACY_READINESS.md)，再由使用者選擇是否進行下一階段。

## 每次資料呼叫先選擇隱私方式

AI 必須先說明具名接收方與本次聊天室／日期／查詢，詢問「先去識別化」或「保留原文」；未選、取消、未知接收方或 scope 改變就不回資料。這是 [共用 mock 協商](../../docs/PLUGIN_PREPARATION.md#呼叫前的-mock-隱私選擇)，不是此平台已驗證真人批准的機制。模型傳 original 參數不能解鎖 real，去識別選項也不表示處理器已實作。憑證類仍不一般放行；真實敏感原文預覽只能先在本機可信介面處理。
