# Gemini：App 資格與遠端接線仍待確認

查核日期：2026-10-08。共用入口 `../../plugin_adapter.py` 只有 stdio disabled/mock，沒有 Gemini 專用資料邏輯或可連的遠端 URL。

Google 官方自訂 tools 流程有網頁新增 MCP server URL、網頁及行動裝置 `@` 使用說明；資格包含美國、英文、18 歲以上、個人 Google 帳號與 Keep Activity 開啟。台灣帳號目前能否使用不能保證；必須重新核對實際帳號與當時條件。[官方自訂 tools](https://support.google.com/gemini/answer/17209137?hl=en)

- Web／Mobile：有上述官方流程；本次均未以真實帳號安裝或呼叫。
- Desktop 獨立客戶端：未確認此路徑的額外支援。
- Gemini CLI／API：與 Gemini App 是不同入口；本次沒有以其能力代替 App 驗證，也沒有製作 API client。
- 本機 stdio／tunnel：沒有查到足以保證 Gemini App 可直接採用此原型本機接線的官方證據；不能填 `localhost` 就宣稱可用。

本版不會開啟 Activity、改地區或語言、建立帳號、建置 HTTP endpoint 或 tunnel。若使用者未來決定評估，先處理資格與 [隱私門檻](../../docs/PRIVACY_READINESS.md)，並審閱 [Gemini Apps 隱私中心](https://support.google.com/gemini/answer/13594961?hl=en)。

## 每次資料呼叫先選擇隱私方式

AI 必須先說明具名接收方與本次聊天室／日期／查詢，詢問「先去識別化」或「保留原文」；未選、取消、未知接收方或 scope 改變就不回資料。這是 [共用 mock 協商](../../docs/PLUGIN_PREPARATION.md#呼叫前的-mock-隱私選擇)，不是此平台已驗證真人批准的機制。模型傳 original 參數不能解鎖 real，去識別選項也不表示處理器已實作。憑證類仍不一般放行；真實敏感原文預覽只能先在本機可信介面處理。
