# 平台接入矩陣：共用核心，逐一驗證 host

查核日期：2026-10-08。這裡的 `adapters/` 目前是接入指南與少量有正式 schema 的本機 mock 設定範例，**不是六套已完成的平台連接器**。所有路徑都以同一個 `../plugin_adapter.py` 與同一套資料邏輯為基礎；不因模型名稱複製 reader 或降低邊界。

「官方有路徑」只表示官方文件有這項能力。「本機合成測試」只驗證共用 MCP schema／stdio／synthetic 行為，不能證明某個 app 已安裝。本次全部實際 host、帳號、手機操作及 API 端到端測試均未執行。最終本機測試命令與結果見 [驗證紀錄](../docs/PLUGIN_PREPARATION.md#測試與安裝的證據分開記錄)。

| 供應商／產品 | 明確區分的使用面 | 官方能力／限制 | 本次狀態 |
| --- | --- | --- | --- |
| [ChatGPT](chatgpt/README.md) | Web chat | Custom MCP 的 URL／官方 Tunnel；安裝後 `@`；受 workspace 權限限制 | 共用 stdio mock 已備；帳號與 tunnel 未接線 |
| ChatGPT | Desktop chat／Android／iOS | 本次未核實這個自訂插件在各客戶端的實際支援與方案 | actual-host-unverified；不能以 Web 成功推定 |
| ChatGPT／OpenAI | CLI／API | 官方 tunnel 文件另列受支援產品；不是 Web chat 安裝的同義詞 | API client 未製作，未呼叫模型 |
| [Gemini App](gemini/README.md) | Web chat／Mobile | 官方有 URL 設定及 `@`；限美國、英文、18+、個人帳號、Keep Activity 開啟；台灣資格未保證 | actual-host-unverified；本版無遠端 URL／官方本機 tunnel 保證 |
| Gemini | Desktop 獨立 app／CLI／API | 各是獨立產品面，不能用 CLI 或 API 能力證明 Gemini App 接受本機 stdio | 未驗證／本版未實作這些 client |
| [Grok](grok/README.md) | grok.com Web chat | 官方 custom MCP 要可公開連達的 server URL；不是使用者 PC 的 localhost | 本版僅 stdio，Web 帳號未接線 |
| Grok | Mobile／Desktop chat | 本次未確認 custom MCP 的手機 `@` 或獨立 Desktop 支援 | unverified，沒有安裝包 |
| Grok Build | CLI | 官方另有 stdio／HTTP MCP；與 Grok chat 分開 | host 未測；需防止相容設定自動載入原始 reader |
| xAI | API | 官方有 Remote MCP 工具；API 整合與 Grok chat 分開驗證 | API-client-only 路徑；本版無 API client |
| [Claude](claude/README.md) | Desktop chat | 官方本機 MCP 設定可用 stdio；另有桌面 extension 機制 | 提供 mock JSON，未寫入使用者設定或實際安裝 |
| Claude | Web chat／Desktop chat／Mobile 的 plugin ZIP | Skill 可用，ZIP 內的 local command MCP 在 Chat 被忽略；remote connector 需雲端可連 URL | 沒有 remote endpoint；不上傳假裝本機接通 |
| Claude | 本機 Cowork／Claude Code CLI | 官方支援本機 command MCP；CLI 有 stdio 設定流程 | 共用 mock 入口及手動命令已備，host 未測 |
| Claude | API | 與 consumer chat、桌面設定不同 | 本次未實作／驗證 API client |
| [DeepSeek](deepseek/README.md) | 原版 Web／Mobile／Desktop chat | 未確認一般 consumer chat 可自行安裝此 MCP／Skill | unverified；不宣稱 `@` 可用 |
| DeepSeek | 官方 Harness CLI | 獨立 agent 工具，官方支援 MCP／plugin skills；不是原版 Chat | 可評估 stdio 共用入口；未安裝或驗證 |
| DeepSeek | API | 官方 tool calls，由 client 執行工具及回傳結果 | API-client-only；本版未製作 client |
| [Qwen](qwen/README.md) | 原版 Web／Mobile／Desktop chat | 未確認一般 consumer chat 可自行安裝此 MCP／Skill | unverified；不提供虛構安裝包 |
| Qwen Code | CLI | 官方 MCP 支援 stdio／HTTP／SSE | 共用入口可供後續測試；未安裝或驗證 |
| 阿里雲 Model Studio | API | 已查官方 MCP 路徑使用 SSE，與 Qwen Chat 不同 | API-client-only；本版沒有 SSE endpoint 或 API client |

各列的「可供後續測試」不保證相容。stdio 共用測試完成後，仍需逐個 host 實測工具發現、參數 schema、structured output、拒絕、限流與連線生命週期。API function calling 要另外做工具呼叫迴圈，不能把它稱為現成 MCP／Skill 安裝功能。

## 隔離 mock 設定

專案根目錄原 `.mcp.json` 仍指向 `line_mcp_server.py` raw 核心，不是本原型。所有 consumer／CLI 接入應使用獨立 mock profile／設定，只啟動 `plugin_adapter.py --mode mock`。首次啟動前檢查 host 是否自動匯入專案、家目錄或其他 client 設定；不得讓相容性探索意外啟動原核心。

## 共用指令及安全門檻

[唯一的 provider-neutral Skill](../plugin/skills/line-summary-mock/SKILL.md) 描述 synthetic-only 工作流程。它不建立連線、不附憑證、不授權真實 LINE，也不使不支援 Skills 的平台突然支援安裝。不同 host 是否載入這種指令格式，要由其正式格式與實測決定。

- 只有 `disabled`／`mock`；沒有真實資料開關。
- 沒有 HTTP／SSE listener、公開服務、tunnel 啟動腳本或跨平台一鍵 ZIP。
- 不自動安裝、註冊、改設定、建立憑證或呼叫任何供應商模型。
- 一般客戶端例子見 [plugin/](../plugin/README.md)，正式本機 Claude schema 例子見 [Claude 指南](claude/README.md)。未核實者僅列文件路徑，不製造假 manifest。
- 任何未來真實資料接入都先通過 [隱私門檻](../docs/PRIVACY_READINESS.md)，再確認特定 host、資料、接收者及必要權限。

## 其他可評估的 host（僅文件查核，均未實測）

若目標是 Windows 上直接使用本機 stdio、多模型聊天而不先架遠端服務，可優先評估 Cherry Studio、Jan、AnythingLLM Desktop。這是接入形態的建議，不代表三者已通過此 LINE 原型測試；選雲模型時工具結果仍會送往供應商。以下均沒有在本次安裝或接線，也沒有額外實作包。

| Host／使用面 | 已核實或待核實能力 | 對此原型的限制／來源 |
| --- | --- | --- |
| Cherry Studio，Windows desktop | 本機 stdio；SSE／Streamable HTTP；官方說明 Skill ZIP／資料夾導入 | 可評估共同 mock；MCP／Skills 的官方搜尋索引可見，但全文工具未成功開啟，需按實際版本再核對。[MCP](https://www.cherryai.com/docs/en/advanced-basic/extensions/mcp/)／[Skills](https://www.cherryai.com/docs/en/advanced-basic/extensions/skills/) |
| Jan，Windows desktop | 本機 stdio、HTTP、SSE；有工具確認；未核實一般 Skill ZIP | 可評估共同 mock；不要開全域略過工具確認，不將 CLI Skills 算成 Desktop 功能。[官方 MCP](https://www.jan.ai/docs/desktop/integrations/mcp-servers)／[確認與模型限制](https://www.jan.ai/docs/desktop/mcp) |
| AnythingLLM Desktop，Windows | 本機 stdio、SSE／Streamable；設定用 anythingllm_mcp_servers.json | 自有 Agent Skill 是 plugin.json＋handler.js，不是共用 SKILL.md ZIP；未確認每次 MCP 都強制確認。[官方 MCP](https://docs.anythingllm.com/mcp-compatibility/overview)／[Desktop 限制](https://docs.anythingllm.com/mcp-compatibility/desktop) |
| Msty Studio Desktop | 本機 stdio、Streamable HTTP；舊 SSE 不原生支持 | 與舊 Msty App 分開；任意 Skill ZIP／逐次確認未核實。[官方 Toolbox](https://docs.msty.ai/studio/toolbox/tools) |
| LibreChat，自架 Web | stdio／SSE／Streamable HTTP／WebSocket | stdio 在 server／容器執行；Linux Docker 不等於能讀 Windows LINE。[官方設定](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_servers) |
| Open WebUI，自架 Web | 主產品原生 MCP 為 Streamable HTTP；stdio 可另經 mcpo bridge | 本版沒有建立 bridge；別把另立 Computer 產品的 stdio 算成主 UI。[官方 MCP](https://docs.openwebui.com/features/extensibility/mcp/)／[mcpo 路徑](https://docs.openwebui.com/features/extensibility/plugin/tools/openapi-servers/mcp/) |
| TypingMind Personal／Team，Web | Personal 有 Skill ZIP／GitHub；Team 尚不支持 Skills；Plugin JSON／URL 是另一格式 | MCP 可另設同裝置 Node.js bridge，本次未建；雲模型資料仍離機。[Skills](https://docs.typingmind.com/skills)／[Plugin JSON](https://docs.typingmind.com/plugins/share-import-plugins)／[本機 bridge](https://docs.typingmind.com/model-context-protocol-(mcp)-in-typingmind/use-mcp-with-private-mcp-connector) |
| Perplexity Computer，雲端 | 支持 .md／含 SKILL.md 的 ZIP；remote connector 使用 HTTPS | Skill 不安裝本機 MCP；本版只有 stdio。[官方 Skills](https://www.perplexity.ai/help-center/en/articles/13914413-how-to-use-computer-skills)／[Remote connectors](https://www.perplexity.ai/help-center/en/articles/13915507-adding-custom-remote-connectors) |
| Perplexity Portable Computer，Windows | 另一個產品的本機 MCP 路徑；Pro／Max、Windows 10／11、NVIDIA 24 GB VRAM 等前提 | 不能把此特殊本機產品能力套到普通雲端 chat；硬體與帳號未核。[官方 Portable Computer](https://www.perplexity.ai/help-center/en/articles/20260915-what-is-portable-computer) |
| Mistral Vibe chat | 官方有 Skill editor 與 HTTPS MCP connectors；任意 ZIP 匯入未核 | 與 Vibe CLI 分開；動態工具發現／resources／prompts 不在已支持範圍。本版無 HTTPS endpoint。[官方 Skills](https://docs.mistral.ai/vibe/work/skills)／[MCP connectors](https://docs.mistral.ai/vibe/work/connectors/mcp-connectors) |
| Microsoft Copilot Studio，企業 agent | 可接 Streamable HTTP MCP／相關 OpenAPI 路徑 | 企業 agent 平台，不是 consumer Copilot 的通用插件安裝。[官方文件](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent) |
| Poe，server／script bot 開發 | 可開發 bot／adapter | 不是已確認的任意 MCP／Skill ZIP 安裝功能；本次未製作 bot。[Server bots](https://creator.poe.com/docs/server-bots/quick-start)／[Script bots](https://creator.poe.com/docs/script-bots/quick-start) |

每一項都要另外確認版本、方案、地區、工具授權、模型是否支援工具、資料保留，以及 server 是否會自動讀取其他 MCP 設定。任何 host 沒有連線時都只能回報未連線，不能藉共用 Skill 杜撰資料或切換到 raw reader。

手機外出連家中 PC 還需要另經授權的安全連線、在線且存活的服務，以及實際手機 host 驗證；手機 localhost 不能直達家中 PC。鎖屏／休眠與斷線處理、重啟後 cursor／配額等操作界線見 [準備說明](../docs/PLUGIN_PREPARATION.md#5-外出手機使用下一階段的操作概念)。目前只有本機 mock，沒有這條手機連線。
