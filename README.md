# line-summary

用 Claude Code 經由本機 stdio MCP 讀取 LINE 電腦版已儲存的聊天記錄，整理獲准聊天室與時間範圍的摘要。核心預設停用，需由使用者設定資料庫與聊天室允許清單。資料範圍限本機記錄，不能據此證明 LINE 雲端歷史已全部同步。

本輪修復保留四個 CLI MCP 工具，加入程序身分檢查、候選金鑰的實際解密驗證，以及 Windows active WAL 唯讀 worker。已驗範圍是鎖定 runtime 的合成測試；**真實資料整合尚未完成，效能與生命週期仍待驗證**。本修復維持 draft，尚不可宣稱 live 可用或合併；ChatGPT／手機連線及乾淨機器安裝也尚未完成驗收。

## Windows 加密資料庫唯讀邊界

加密查詢在一次查詢一次啟動的子程序執行。主程序透過本機 pipe 傳入來源路徑與金鑰；兩者不放入命令列或新增環境變數。子程序要求既有 DB、WAL、SHM 都可讀，固定三個檔案身分，使用原生 SQLite WAL 交易與 OS 鎖讀取；缺失或無法讀取 SHM 時直接回報錯誤。它不建立來源 SHM、磁碟副本，也不使用 `immutable=1`。

本版限制的二進位組合是 Windows x86-64、CPython 3.11、`apsw-sqlite3mc 3.53.3.1`（SQLite 3.53.3），來源須位於本機磁碟；網路磁碟的 mapped-view 一致性不受支持。runtime 鎖定 `mcp 1.29.0`；APSW lock 收錄既有 Windows／Linux、CPython 3.11／3.12 CI matrix 的四個 x86-64 wheel hash，均核對 PyPI metadata 與實際下載檔案。wheel 安裝驗證不擴大 Windows native reader 的支援範圍，其他平台或版本須另行驗證。四個 MCP 查詢入口仍使用相同的 `DbReader` 方法。

建立真正的 SQLite 讀交易後，程式短暫持有共享 CKPT／RECOVER／read-slot 鎖以阻止 checkpoint、recovery 與 WAL reset，同時允許 writer append。透過唯讀 SHM mapping 和已開啟的 SQLite WAL handle，核對有效雙 header、nBackfill、已宣告提交 frames 的長度、salt 與 checksum。檢查不在開交易前執行，不製作 DB/WAL snapshot；已提交 WAL 損壞會明確失敗，正常 checkpoint 的空 WAL 與已提交界線後的未完成尾框仍可讀。無效 SHM header 明確失敗，不宣稱能從已遺失或同時損壞的 WAL/SHM 還原歷史提交。

每個 session 共用 25 秒預算，從 spawn 前計時，預留 2 秒回收；stdin、stdout、驗證與正常 wait 均使用此預算。父端以 Win32 `PIPE_NOWAIT` 處理 bounded pipes，沒有 I/O thread／reply queue。`cancel()` 透過只傳給本次 child 的 unnamed event 及 stdin EOF，讓 child interrupt／progress predicate 中止 SQLite，關閉 connection、validation view/locks、pins，join 有界 watcher 與 watchdog 後退出。只有確認正常 child 退出才承諾來源已釋放；若須強制終止原生 reader，會回固定 `read worker cleanup incomplete`，不把 kill/wait 當成已證明能立即替換來源。仍存活的 child 或未完成清理會保留 owned worker 供重試，不終止其他 LINE/app，也不加 sleep 掩蓋釋放問題。

Windows `CreateProcess` 本身無法中斷；若返回時預算已耗盡，主程序不傳來源／金鑰，並作最多 2 秒清理。讀交易與檢查可延後 checkpoint／WAL reset，長 WAL 校驗可能達到有限 deadline 而明確失敗。未驗證真 LINE 或乾淨機器安裝。

程序身分通過檢查後，記憶體中的候選仍需以實際資料庫解密／查詢驗證；符合 hex 格式不代表有效金鑰。reader 或金鑰快取存在也不證明 LINE 目前仍登入、帳號未切換或資料已同步。讀取時間與最新訊息時間是不同資訊；沒有同步證據時，同步狀態維持 unknown。

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
- Windows x86-64 的 CPython 3.11 與支援本機 stdio MCP 的 host；以下示範 Claude Code。
- 對本機資料庫的讀取權限，以及可用的 SQLite3MultipleCiphers 引擎。

本輪合成驗證使用 Python **3.11.9**、MCP **1.29.0**、`apsw-sqlite3mc` **3.53.3.1**／SQLite **3.53.3**、pytest **8.4.2**。Python 版本需另行選定；套件精確版本與下載 hash 由 requirements 檔鎖定。

原專案曾在 LINE 電腦版 26.3（wxSQLite3 aes128cbc）上測試。LINE 更新後需要重新驗證；該歷史紀錄不代表本輪修復已通過真實帳號驗收。社群討論串、多媒體、其他 Windows／SQLite ABI 及網路磁碟均未驗證。

## 安裝

建議使用獨立虛擬環境，在一般使用者權限的 PowerShell 執行：

```powershell
git clone https://github.com/yung13yubabie/line-summary.git
cd line-summary
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt
```

Runtime 相依套件含版本與 hash 鎖定；若目前 Python／Windows 組合沒有對應 wheel，安裝會停止，請先確認支援情況，不要直接關閉 hash 檢查或改從未審閱來源建置。本輪只驗證上述 Windows binary 組合的合成讀取，未驗證乾淨機器安裝。

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

原始入口維持 stdio，由 MCP host 啟動以下命令並管理 stdin／stdout：

```powershell
.\.venv\Scripts\python.exe .\line_mcp_server.py
```

單獨執行時會等待 JSON-RPC 請求；程序存在不代表已連到 ChatGPT，也不會自行啟用資料存取。

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

同名聊天室要先確認，不能把多個結果直接合併讀取。第一次獲准的資料讀取需要檢查程序身分、掃描候選並驗證資料庫，可能較慢；本輪沒有真 LINE 耗時承諾。金鑰只由這個 MCP 程序快取，重啟後需重新取得。

摘要會列出確切時區、半開區間 `[since, until)`、已回傳訊息數，以及任何分頁、截斷或額度造成的未完成範圍。查到空結果只代表指定範圍內沒有回傳的本機記錄，不能宣稱該聊天室沒有聊過。

## 工具契約

四工具名稱、說明與輸入 schema 維持既有 stdio 契約：

| 工具 | 用途 |
| --- | --- |
| `line_list_chats` | 依名稱／類型列出允許範圍內的聊天室中繼資料；額外探索需另開權限 |
| `line_get_history` | 讀取指定聊天室與 `[since, until)` 範圍的有界本機歷史 |
| `line_get_unread` | 回傳本機未讀計數及最近訊息近似樣本，不能驗證真實未讀邊界 |
| `line_get_contacts` | 依獨立聯絡人權限分頁查詢通訊錄 |

目前沒有訊息內文關鍵字搜尋工具。聊天名稱查找不等於全文搜尋；目前只能讀取獲准的日期歷史或近似未讀，再由 host 整理。

四個工具都回傳分頁 envelope，不再直接回傳陣列：

- `items`：本頁資料；歷史訊息含 `message_id`。
- `has_more`、`next_cursor`：是否有下一頁，以及要原樣傳回的 cursor。
- `content_complete`：本頁已交付項目是否完整；**不表示整個範圍、未讀或雲端歷史已全部取得**。
- 若首項放不進 byte 上限，回傳空 `items`、`has_more: true`、`next_cursor: null`、`content_complete: false` 和 `blocked_reason: "item_exceeds_byte_budget"`。應停止並回報，不能無限重試或跳過它假裝完整。

`line_get_history` 預設每頁 100 則，最多 500 則，仍受使用者較低的上限及剩餘程序額度限制。輸入為帶時區 ISO 8601，接受正、負 UTC offset 或 `Z`；以 UTC 毫秒處理 `[since, until)`。整天用當天 00:00 到隔天 00:00，不使用 `23:59:59`，避免漏掉最後一秒內的訊息。跨頁沿用相同聊天室、時間範圍及回傳 cursor。

歷史分頁按時間與唯一訊息 ID 排序，在資料庫不變時可穩定遍歷。首次呼叫的 rowid 上界可減少一般新增資料混入，但 SQLite 刪除後可能重用 rowid，修改／刪除也會影響分頁；因此 `consistency: "live_keyset_scan"`、`database_changes_may_affect_pagination: true`、`coverage: "local_rows_only"` 不代表完整快照、完全排除新訊息或同步完成。工具的時間字串以 `+08:00` 顯示，摘要應換算成使用者指定時區。

`line_get_unread` 預設每頁 20 個對話、每個對話最多 50 則最近本機訊息。`unread_count` 是本機未讀計數，`returned_count` 是實際回傳數，`selection: "latest_local_approximation"`、`sync_status: "unknown"`、`unread_boundary_verified: false`。選出的訊息可能全部已讀，不能推斷真正未讀邊界，也不能用兩種筆數相減宣稱「尚未同步 N 則」。`messages_limited` 表示採樣受上限限制；`more_local_messages` 只表示還有本機記錄，不代表還有未讀。未讀 cursor 只翻到其他對話，不補同一對話較早的訊息。

達上限就停止並交付明確標示不完整的摘要。不要拆分日期、切換工具、重試或重啟來規避額度。更多 API 差異見 [MIGRATION.md](MIGRATION.md)。

## ChatGPT 接入狀態

本次核心修復未包含 ChatGPT facade 或遠端橋接；獨立 facade 仍為 disabled，既有 [mock adapter draft PR](https://github.com/yung13yubabie/line-summary/pull/2) 也未併入本次核心變更。本機 stdio 可運行不等於已能在 ChatGPT `@` 呼叫。

後續接入需要 PC 在線、LINE 已登入且本機資料可用、服務持續運行，以及另行設定並驗證可信 owner／session context 與 tunnel 權限。官方 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 支援本機 stdio 目標；這只提供接入方向，不代表本專案帳號／tunnel／ChatGPT UI／手機已驗證。互動選擇卡另參考官方 [ChatGPT UI](https://developers.openai.com/plugins/build/chatgpt-ui) 與 [Plugin reference](https://developers.openai.com/plugins/reference)。

每次讀取的「去識別／原文／取消」選擇及可信授權仍屬獨立 facade 工作；本核心不提供去識別保證，也不會因工具參數寫入 `approved=true` 而取得額外權限。

## 摘要存在哪

**預設只在 Claude Code 對話中回覆，不建立摘要檔或 metadata 檔。** 這不會刪除或阻止 Claude Code／模型供應商自己的工作階段紀錄。

只有使用者明確要求另存時才寫檔。先確定檔案目的地、是否含直接引文／人名，以及保留策略，例如「存於私人資料夾，月底由我手動刪除」。不要默認 `~/line-summary/output/`，也不要默認會自動清理。此工具沒有保留期限或自動刪除排程。

`output/` 被 `.gitignore` 排除，但 `.gitignore` 不是存取控制，也不會阻止備份、雲端同步或已追蹤檔案被提交。已用舊版產生的摘要／metadata 不會被這次升級清除，請自行檢查分享範圍及保留期限。

## 選用：捲動補歷史

`tools/scroll_backfill.py` 不屬於唯讀 MCP 流程，預設不執行。它會啟用 LINE 視窗、移動真實滑鼠、送出鍵盤／滾輪輸入，可能讓 LINE 從網路下載並儲存更早的訊息，也可能改變已讀狀態。

**它不能驗證畫面上目前開啟的是指定聊天室。** 名稱參數只用來查詢資料庫統計，不會選取或核對畫面上的對話。此腳本直接讀取資料庫，不受 MCP server 的聊天室允許清單或輸出額度保護。請先閱讀 [選用工具說明](tools/README.md)，由使用者獨立決定是否執行；摘要 skill 不會自動啟動它。

## 排查與測試

MCP 的無效參數、未知工具／提示／資源及未預期 backend 錯誤改用固定診斷，SDK 日誌不回顯原始輸入或例外內容；原四工具的成功 JSON 與本機權限設定維持原契約。既有 Pydantic 啟動 warning 仍可能顯示套件安裝路徑。

- 存取遭拒：核對使用者設定的 `enabled`、`db_path` 與明確聊天室 ID；不要自動開放所有聊天室。
- 額度已用完：摘要必須標示不完整；讓使用者決定後續範圍，不自動重啟或提高額度。
- LINE 未執行：確認它在同一使用者帳號下開啟且已登入。
- 找不到資料庫／無法開啟：檢查 `db_path`、一般檔案讀取權限、鎖定狀態與 cipher 相依套件。不要把所有錯誤都解釋成金鑰錯誤或 LINE 改版。
- 缺少 SHM、active WAL 的提交證據不一致或無法建立安全讀交易：明確拒絕讀取。不要建立空 SHM、複製零散 sidefile 或忽略錯誤後改讀可能過時的 base DB。
- 逾時／取消：reader 會嘗試中止查詢並回收本次 child；Windows OS spawn 不能由 Python timer 硬中斷。`read worker cleanup incomplete` 表示尚未證明完整釋放，不應立即替換來源；取消也不能收回已交付 host 的內容。
- 權限或程序記憶體讀取受阻：停止並檢查原因，不預設提權、不停用系統保護。不要貼出金鑰、記憶體傾印或真實聊天內容求助。

日常測試使用人工資料與 mock，避免碰觸真實 LINE：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-test.txt
.\.venv\Scripts\python.exe -m pytest -m "not integration" --cov --cov-report=term-missing
```

普通 pytest（甚至只加 `-m integration`）都會在偵測 LINE 前跳過 live 測試。真實 Windows 整合測試會讀取帳號的程序記憶體與聊天資料，必須另行明確決定並加上 `--run-live-line` 才會啟用；不要放進日常測試或 CI。沒有執行就不能宣稱通過。升級前後的行為差異見 [MIGRATION.md](MIGRATION.md)。

## 本輪核心修復驗證（待合併）

鎖定 Windows runtime 的獨立審查：完整 source **519 passed、0 failed、5 live deselected**，四個工具的真 stdio name／description／inputSchema 與原契約相同。另以有效 32／64 字元合成 key，先成功讀取真正加密 DB 的已提交 WAL，再分別清空／截斷另一份 WAL 且保留 SHM；四個反例均拒絕過時讀取，來源 bytes 不變且 child 正常退出。這些是合成資料上的真 native reader 驗證，沒有讀取真 LINE。

固定歷史反例未被刪除或改寫為通過：舊 identity 模型仍為 44 passed／2 failed，舊 strict 候選模型仍為 632 passed／32 failed。它們保留 native 模型與「候選 enumeration 後須經真 DB 驗證」的契約差異，未計入完整 source 的 519 個通過案例；各審查組有重疊，不能相加成總案例數。

本機合成結果不代表 CI 通過；請查看該 PR commit 的 checks。既有 CI 含其他 OS／Python matrix，但本輪 Windows reader 的支援範圍仍以上述鎖定組合為限。真實資料整合、效能與生命週期仍待驗證；tunnel／ChatGPT、手機及乾淨機器驗收也尚未完成。

## 歷史修復紀錄（2026-10-08）

本次核心修復加入穩定分頁、正確日期邊界、保守未讀標示、server 範圍及資料預算，並修正 skill 位置、離線測試預設值及依賴鎖定。這是會改變工具 schema 的更新，升級請先讀 [MIGRATION.md](MIGRATION.md)；逐項紀錄見 [CHANGELOG.md](CHANGELOG.md)。

本機 Linux Python 3.12 合成測試：122 passed、5 項真實 LINE 測試 skipped；runtime coverage 88.81%（啟用 branch measurement，門檻 85%）。已對原始基線套用 patch 並重跑。CI 設定增加 Linux／Windows、Python 3.11／3.12 的合成測試；實際遠端結果以 PR checks 為準。這些測試不會存取真實 LINE，也不能視為 Windows LINE 相容性驗證。
