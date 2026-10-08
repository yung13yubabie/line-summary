# line-summary

用 Claude Code 讀取本機 LINE 電腦版已儲存的聊天記錄，整理指定聊天室與時間範圍的摘要。適用於 Windows；不是完整聊天備份，也不能證明 LINE 雲端訊息已全部同步。

## 先了解資料會去哪裡

```text
LINE 程序記憶體 → 本機金鑰提取 → 以唯讀模式解密本機 LINE 資料庫
                                      ↓
                                MCP 工具結果
                                      ↓
                         Claude Code → 所選模型供應商 → 摘要
```

- MCP server 不主動把金鑰寫入檔案、log 或工具回傳值；程式會在存活期間把它保留在記憶體。這不等於「金鑰絕不落地」：作業系統分頁檔、休眠檔、程序或系統傾印仍可能包含記憶體資料，也不保證 Python 記憶體被安全抹除。
- 本機資料庫唯讀，只描述 MCP 的資料存取方式，**不代表整個摘要流程離線**。聊天室名稱、發言者、訊息、連結及其他工具結果會進入 Claude Code 上下文，依你的模型與服務設定，可能傳送至模型供應商並保留在其紀錄或工作階段中。使用前確認供應商的資料使用與保留政策。
- MCP 的讀取不操作 LINE 畫面、不把對話標為已讀，也不透過 LINE API 送出已讀回條。這項說明不適用於自行打開 LINE 對話或下方的選用捲動工具。
- 只處理自己有權存取及使用的資料。群組裡其他人的訊息不因你能讀取就適合公開、轉寄或上傳。

## 需要什麼

- Windows，以及正在執行、已登入的 LINE 電腦版。讀取 LINE 程序記憶體使用 Windows API。
- Python 3.11 以上與 Claude Code。
- 對本機資料庫的讀取權限，以及可用的 SQLite3MultipleCiphers 引擎。

原專案曾在 LINE 電腦版 26.3（wxSQLite3 aes128cbc）上測試。LINE 更新後可能需要重新驗證；本次安全修補沒有執行 Windows／真實帳號整合測試。

## 安裝

建議使用獨立虛擬環境，在一般使用者權限的 PowerShell 執行：

```powershell
git clone https://github.com/yung13yubabie/line-summary.git
cd line-summary
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt
```

Runtime 相依套件含版本與 hash 鎖定；若目前 Python／Windows 組合沒有對應 wheel，安裝會停止，請先確認支援情況，不要直接關閉 hash 檢查或改從未審閱來源建置。這次修補未驗證 Windows binary 相容性。

將專案 `.mcp.json` 的 `command` 改為這個虛擬環境 Python 的絕對路徑；全域註冊時，Python 與 server 都使用絕對路徑。不要因讀取失敗就直接改用系統管理員權限。

## 先設定最小資料範圍

Server 的本機權限設定在 `line_mcp_server.py` 同目錄的 `settings.json`，不是 Claude Code 的 `.claude/settings.json`。由使用者複製 `settings.example.json` 並自行編輯；`settings.json` 已被 Git 忽略，不要加入版本控制或公開分享。

預設 `enabled: false`、`allowed_chat_ids: []`、`allow_chat_discovery: false`、`allow_contacts: false`，不讀取資料。啟用前：

1. 填入自己帳號資料庫的 `db_path`，建議使用絕對路徑。Server 不再從多個 `.edb` 中自動挑選最大檔案，以免讀錯帳號。
2. 將 `enabled` 改為 `true`，只在 `allowed_chat_ids` 放入需要的明確 ID。允許清單最多 100 個 ID，沒有「全部聊天室」萬用值。
3. 若不知道 ID，可暫時開啟 `allow_chat_discovery`，查找聊天室中繼資料，再加入選定 ID 並關閉探索。探索會把回傳的名稱、ID、類型與時間交給模型；它不授權讀取清單以外聊天室的歷史或未讀內容。
4. 聯絡人查詢是另外的通訊錄權限，只有需要時才開啟 `allow_contacts`。即使此工具停用，獲准訊息仍會包含本機已解析的發言者名稱。

設定在程序第一次工具呼叫時固定。使用者更改設定後需重新啟動 MCP server 才會生效；不要讓聊天訊息、網頁或模型自行修改權限、提高額度，或重啟程序來繞過限制。這是 server 的資料輸出防線，並不是作業系統沙箱；請另行限制 Claude Code 對無關檔案與 shell 的存取。

### 預設輸出上限

| 設定 | 預設值，也是允許的最高值 | 說明 |
| --- | ---: | --- |
| `max_messages_per_call` | 500 | 歷史訊息／未讀訊息每次回傳的總筆數 |
| `max_response_bytes` | 262144（256 KiB） | 每次回傳 JSON payload；不含 MCP 傳輸封裝 |
| `max_session_messages` | 5000 | 整個 server 程序累計回傳訊息數 |
| `max_session_bytes` | 2097152（2 MiB） | 整個程序所有工具累計回傳 JSON bytes |
| `max_range_days` | 31 | 單次歷史查詢區間長度 |

使用者可降低上限；byte 設定最低 2048，其餘數值最低 1。重複回傳同一則訊息仍會重複計入額度。可選的 `allowed_since`、`allowed_until` 限制歷史查詢的固定日期範圍；任一日期限制啟用時，未讀工具會直接拒絕呼叫，應改用符合日期範圍的歷史查詢。詳見 [遷移說明](MIGRATION.md)。

## 註冊到 Claude Code

在專案根目錄開啟 Claude Code，先檢查 `.mcp.json` 的啟動命令，再依提示審閱是否信任這個 MCP server。用 `/mcp` 檢查連線。專案 MCP 設定位置與授權流程見 [Claude Code 官方 MCP 文件](https://code.claude.com/docs/en/mcp#project-scope)。

需要跨專案使用時，可自行用絕對路徑註冊：

```powershell
claude mcp add line --scope user -- C:\path\to\line-summary\.venv\Scripts\python.exe C:\path\to\line-summary\line_mcp_server.py
```

MCP 註冊與 skill 安裝是兩件事。專案 skill 位於 `.claude/skills/line-summary/SKILL.md`，包含 `name`、`description` YAML frontmatter；在專案內可用 `/line-summary`。全域 MCP 註冊不會一併安裝全域 skill。如確實需要跨專案 skill，可自行將此 skill 資料夾放入 `~/.claude/skills/`，並管理更新，避免舊副本遮蓋新版本。參見 [Claude Code 官方 Skills 文件](https://code.claude.com/docs/en/skills#choose-where-skills-load)。

## 怎麼用

完成允許清單後，在專案內要求：

- 「總結 XXX 群組今天的對話，使用台北時間」
- 「幫我看 XXX 從 10 月 1 日到 10 月 3 日的對話」
- 「整理允許的聊天室最近的未讀重點」

同名聊天室要先確認，不能把多個結果直接合併讀取。第一次獲准的資料讀取需要掃描 LINE 記憶體，可能較慢；原實測約 80 秒，不是固定耗時。金鑰只由這個 MCP 程序快取，重啟後需重新取得。

摘要會列出確切時區、半開區間 `[since, until)`、已回傳訊息數，以及任何分頁、截斷或額度造成的未完成範圍。查到空結果只代表指定範圍內沒有回傳的本機記錄，不能宣稱該聊天室沒有聊過。

## 工具契約

目前沒有訊息內文關鍵字搜尋工具，也沒有 ChatGPT `@` 插件／遠端橋接。聊天名稱查找不等於全文搜尋；目前只能讀取獲准的日期歷史或近似未讀，再由 host 整理。這些擴充需另行設計及授權。

四個工具都回傳分頁 envelope，不再直接回傳陣列：

- `items`：本頁資料；歷史訊息含 `message_id`。
- `has_more`、`next_cursor`：是否有下一頁，以及要原樣傳回的 cursor。
- `content_complete`：本頁已交付項目是否完整；**不表示整個範圍、未讀或雲端歷史已全部取得**。
- 若首項放不進 byte 上限，回傳空 `items`、`has_more: true`、`next_cursor: null`、`content_complete: false` 和 `blocked_reason: "item_exceeds_byte_budget"`。應停止並回報，不能無限重試或跳過它假裝完整。

`line_get_history` 預設每頁 100 則，最多 500 則，仍受使用者較低的上限及剩餘程序額度限制。輸入為帶時區 ISO 8601，接受正、負 UTC offset 或 `Z`；以 UTC 毫秒處理 `[since, until)`。整天用當天 00:00 到隔天 00:00，不使用 `23:59:59`，避免漏掉最後一秒內的訊息。跨頁沿用相同聊天室、時間範圍及回傳 cursor。

歷史分頁按時間與唯一訊息 ID 排序，在資料庫不變時可穩定遍歷。首次呼叫的 rowid 上界可減少一般新增資料混入，但 SQLite 刪除後可能重用 rowid，修改／刪除也會影響分頁；因此 `consistency: "live_keyset_scan"`、`database_changes_may_affect_pagination: true`、`coverage: "local_rows_only"` 不代表完整快照、完全排除新訊息或同步完成。工具的時間字串以 `+08:00` 顯示，摘要應換算成使用者指定時區。

`line_get_unread` 預設每頁 20 個對話、每個對話最多 50 則最近本機訊息。`unread_count` 是本機未讀計數，`returned_count` 是實際回傳數，`selection: "latest_local_approximation"`、`sync_status: "unknown"`、`unread_boundary_verified: false`。選出的訊息可能全部已讀，不能推斷真正未讀邊界，也不能用兩種筆數相減宣稱「尚未同步 N 則」。`messages_limited` 表示採樣受上限限制；`more_local_messages` 只表示還有本機記錄，不代表還有未讀。未讀 cursor 只翻到其他對話，不補同一對話較早的訊息。

達上限就停止並交付明確標示不完整的摘要。不要拆分日期、切換工具、重試或重啟來規避額度。更多 API 差異見 [MIGRATION.md](MIGRATION.md)。

## 摘要存在哪

**預設只在 Claude Code 對話中回覆，不建立摘要檔或 metadata 檔。** 這不會刪除或阻止 Claude Code／模型供應商自己的工作階段紀錄。

只有使用者明確要求另存時才寫檔。先確定檔案目的地、是否含直接引文／人名，以及保留策略，例如「存於私人資料夾，月底由我手動刪除」。不要默認 `~/line-summary/output/`，也不要默認會自動清理。此工具沒有保留期限或自動刪除排程。

`output/` 被 `.gitignore` 排除，但 `.gitignore` 不是存取控制，也不會阻止備份、雲端同步或已追蹤檔案被提交。已用舊版產生的摘要／metadata 不會被這次升級清除，請自行檢查分享範圍及保留期限。

## 選用：捲動補歷史

`tools/scroll_backfill.py` 不屬於唯讀 MCP 流程，預設不執行。它會啟用 LINE 視窗、移動真實滑鼠、送出鍵盤／滾輪輸入，可能讓 LINE 從網路下載並儲存更早的訊息，也可能改變已讀狀態。

**它不能驗證畫面上目前開啟的是指定聊天室。** 名稱參數只用來查詢資料庫統計，不會選取或核對畫面上的對話。此腳本直接讀取資料庫，不受 MCP server 的聊天室允許清單或輸出額度保護。請先閱讀 [選用工具說明](tools/README.md)，由使用者獨立決定是否執行；摘要 skill 不會自動啟動它。

## 排查與測試

- 存取遭拒：核對使用者設定的 `enabled`、`db_path` 與明確聊天室 ID；不要自動開放所有聊天室。
- 額度已用完：摘要必須標示不完整；讓使用者決定後續範圍，不自動重啟或提高額度。
- LINE 未執行：確認它在同一使用者帳號下開啟且已登入。
- 找不到資料庫／無法開啟：檢查 `db_path`、一般檔案讀取權限、鎖定狀態與 cipher 相依套件。不要把所有錯誤都解釋成金鑰錯誤或 LINE 改版。
- 權限或程序記憶體讀取受阻：停止並檢查原因，不預設提權、不停用系統保護。不要貼出金鑰、記憶體傾印或真實聊天內容求助。

日常測試使用人工資料與 mock，避免碰觸真實 LINE：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-test.txt
.\.venv\Scripts\python.exe -m pytest -m "not integration" --cov --cov-report=term-missing
```

普通 pytest（甚至只加 `-m integration`）都會在偵測 LINE 前跳過 live 測試。真實 Windows 整合測試會讀取帳號的程序記憶體與聊天資料，必須另行明確決定並加上 `--run-live-line` 才會啟用；不要放進日常測試或 CI。沒有執行就不能宣稱通過。升級前後的行為差異見 [MIGRATION.md](MIGRATION.md)。

## 修復紀錄（2026-10-08）

本次核心修復加入穩定分頁、正確日期邊界、保守未讀標示、server 範圍及資料預算，並修正 skill 位置、離線測試預設值及依賴鎖定。這是會改變工具 schema 的更新，升級請先讀 [MIGRATION.md](MIGRATION.md)；逐項紀錄見 [CHANGELOG.md](CHANGELOG.md)。

本機 Linux Python 3.12 合成測試：122 passed、5 項真實 LINE 測試 skipped；runtime coverage 88.81%（啟用 branch measurement，門檻 85%）。已對原始基線套用 patch 並重跑。CI 設定增加 Linux／Windows、Python 3.11／3.12 的合成測試；實際遠端結果以 PR checks 為準。這些測試不會存取真實 LINE，也不能視為 Windows LINE 相容性驗證。
