# 通用 MCP 本機測試參考

這個資料夾目前不是 ChatGPT 或 Gemini 的插件安裝包。`mock-client.example.json` 僅展示常見 stdio MCP client 的 `command`／`args`／`cwd` 設定概念；各 client 的實際設定位置、頂層 key 及支援欄位應查其文件。

## 使用前

1. 先閱讀 [完整準備說明](../docs/PLUGIN_PREPARATION.md) 與 [隱私門檻](../docs/PRIVACY_READINESS.md)。
2. 先將合成示範包解壓到原始 repo 及其父層設定之外的新資料夾，使用已配置鎖定依賴的 Python 環境與獨立 mock profile／設定。不要在原始 repo 啟動 host；避免自動載入原 `.mcp.json` 的 raw reader。
3. 把範例裡的兩種絕對路徑 placeholder 換成自己的 Python 執行檔與獨立解壓的 mock 根目錄，依 client 的 schema 手動套用。不要把範例當作能直接匯入平台的 manifest。
4. 本地測試只啟動 `plugin_adapter.py --mode mock`；若要驗證預設停用，移除 `--mode` 與 `mock`，或明確改用 `--mode disabled`。
5. 客戶端完成 MCP 初始化後列出工具並呼叫 `line_status`，確認合成模式和範例日期，再測試歷史、搜尋、未讀與聯絡人。

不要在設定中放 `db_path`、真實 LINE 路徑、API key、密碼、token、tunnel ID 或遠端 URL。不要把入口換成原始 `line_mcp_server.py`。本次沒有平台安裝、tunnel、網路 listener 或實際手機 `@` 驗證。

需手動啟動時，只在獨立解壓的 mock 資料夾執行，絕不在原 repo 執行：

```sh
python plugin_adapter.py --mode mock
```

這是 stdio server，會等待 MCP client；不是 HTTP endpoint，也不會在瀏覽器產生登入或設定頁。用戶端若將輸出接給模型，仍可能傳輸合成內容；此範例沒有授權任何真實資料傳輸。

正式平台接入要分別遵循各自官方流程，重新確認資格、資料授權及安全要求。無共同 ZIP 安裝保證，無自動 setup 或憑證建立腳本。

## 共用 Skill

[skills/line-summary-mock/SKILL.md](skills/line-summary-mock/SKILL.md) 有 name／description frontmatter，只描述如何使用已連線的 synthetic 工具，不會啟動或安裝 server。工具缺少或 disabled 時須明說無法查詢，不杜撰真實結果。其格式能否在目標 host 載入需另外驗證；本資料夾仍不是平台通用插件 ZIP。各 host 路徑見 [平台矩陣](../adapters/README.md)。

## 指令型 Skill ZIP 的封裝與匯入邊界

`tools/build_skill_bundle.py` 依 [skill-package-spec.json](skill-package-spec.json) 對同一份指令產出兩種有正式文件依據的 ZIP 目錄結構；每包都只含一個檔案。預設目錄型：

```text
line-summary-mock-skill.zip
└── line-summary-mock/
    └── SKILL.md
```

另一份 `line-summary-mock-skill-root.zip` 直接以 `SKILL.md` 為 ZIP 根層檔案，對應 Perplexity Computer 文件的格式。兩包指令位元組完全相同，不是兩套資料邏輯。

兩份 ZIP 都不含 Python、MCP 設定、plugin manifest、hook、API key 或資料庫。它不能啟動服務；上傳後若沒有另外連好工具，只能讀到操作指令，必須回答未連線。

官方格式查核：2026-10-08；下列實際匯入與使用均為 `NOT_RUN`。

- Claude Skills：官方說明採 skill 資料夾為 ZIP root，可在其 Skills UI 另行匯入並啟用；帳號資格與功能仍以當下產品為準。[官方 Skill 封裝](https://support.claude.com/en/articles/12512198-how-to-create-custom-skills)
- Claude Code：將解壓的資料夾另行放到隔離專案的 `.claude/skills/`，使位置成為 `.claude/skills/line-summary-mock/SKILL.md`。這與 MCP 設定是兩個步驟。[官方位置](https://code.claude.com/docs/en/skills)
- Qwen Code：相同內容需放到隔離專案 `.qwen/skills/line-summary-mock/SKILL.md`，不是上傳到 Qwen consumer chat。[官方位置](https://qwenlm.github.io/qwen-code-docs/en/users/features/skills/)
- Perplexity Computer：其文件要求 ZIP 根層直接有 `SKILL.md`，或直接上傳 `.md`。使用本次獨立的 `line-summary-mock-skill-root.zip`，或解壓後的 `SKILL.md`；不要使用目錄型 ZIP，不宣稱同一 ZIP 通用。即使載入指令，也沒有替它建立本機 stdio 連線。[官方要求](https://www.perplexity.ai/help-center/en/articles/13914413-how-to-use-computer-skills)
- 其餘 consumer ChatGPT／Gemini／Grok／DeepSeek／Qwen：沒有根據此檔宣稱可安裝插件或自動接入。Harness 的 MCP overlay 也不證明它接受這個 Skill ZIP。

封裝本身只用 Python 標準函式庫；範例不執行安裝或上傳：

```sh
python tools/build_skill_bundle.py --output ../line-summary-mock-skill.zip
python tools/build_skill_bundle.py --layout root --output ../line-summary-mock-skill-root.zip
```

工具會拒絕覆蓋既有檔案，檢查 allowlist、此 Skill 的兩欄單行 YAML frontmatter、ZIP 回讀與 SHA-256；測試不代替實際 host 驗收。一般 [Agent Skills 規格](https://agentskills.io/specification) 有更多選用欄位，本封裝刻意只接受已審查的最小格式。
