# line-summary

用 Claude Code 讀本機 LINE 電腦版的聊天記錄，整理成每日摘要。解密與金鑰處理全程在你自己的電腦本機、你自己登入的 LINE 上進行，金鑰不落地。要留意：整理摘要那一步是 Claude Code 把聊天內容交給它的模型處理，內容是否離開本機取決於你的 Claude Code 模型供應商與資料政策——那部分不在本工具的控制範圍。

## 需要什麼

- Windows。金鑰提取靠 Windows 的 ReadProcessMemory，Mac 和 Linux 用不了。
- LINE 電腦版，開著而且已登入。金鑰只存在 LINE 的記憶體裡，關掉就讀不到了。
- Python 3.11 以上。
- Claude Code。

## 安裝

```
git clone https://github.com/yung13yubabie/line-summary.git
cd line-summary
pip install -r requirements.txt
```

## 註冊到 Claude Code

專案裡已經有一份 `.mcp.json`。在這個資料夾裡開 Claude Code，它就會載入名叫 `line` 的 MCP server，第一次會問你要不要信任，選同意。

想在任何資料夾都叫得到，改成全域註冊，把路徑換成 clone 下來的實際位置：

```
claude mcp add line --scope user -- python C:\path\to\line-summary\line_mcp_server.py
```

全域註冊要寫絕對路徑，因為啟動時的工作目錄不一定在專案裡。專案內附的 `.mcp.json` 用相對路徑，不需要改。

## 怎麼用

在專案資料夾開 Claude Code，直接用中文講就行：

- 總結「XXX 群組」今天的對話
- 幫我看「XXX」最近三天聊了什麼

Claude 會呼叫三個工具把訊息撈出來，照 `skills/line-summary/SKILL.md` 的格式整理成摘要。

第一次呼叫要等大概 80 秒，它在掃 LINE 的記憶體找解密金鑰。找到之後會記住，同一個 session 後面都很快。

## 換一個新 session

不用特別做什麼。LINE 開著、在專案資料夾重開 Claude Code，`line` server 會再載入一次，照上面講一句話就好。

金鑰不存檔，所以每個新 session 的第一次呼叫會再花那 80 秒重找一次。這是刻意的，換來的是金鑰不會留在硬碟上。

## 摘要存在哪

存在 `output/` 底下，這個資料夾已經寫進 `.gitignore`。裡面是真實對話內容，包含別人的發言，別放到公開的地方。

## 安全與分寸

- 只讀你自己電腦、你自己登入的 LINE。金鑰在記憶體裡用完就算了，不寫檔、不寫 log、不送上任何網路。聊天內容則會作為摘要素材交給 Claude Code 的模型，那一步是否離開本機取決於你的模型供應商。
- 群組和開放聊天室裡有別人的訊息。自己整理來看沒問題，要公開之前先想一下。
- 目前只在 LINE 電腦版 26.3 上試過（它用 wxSQLite3 的 aes128cbc 加密）。LINE 改版可能就要重新確認加密方式。

## 動不了的時候

- 「LINE is not running」：LINE 沒開或沒登入。
- 掃不到金鑰、或每個候選都解不開：多半是 LINE 更新過，加密方式變了，需要重新確認解密方式。
- 用系統管理員開 terminal 有時能讀到更多記憶體區段。
