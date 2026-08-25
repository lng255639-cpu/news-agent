# News Agent

用自然語言查詢新聞，自動整理成簡報。可以在終端機互動使用、寄到 email、
或產生靜態網站每天自動更新。

運作方式是四段管線：**問題理解 → 廣泛撈取 → 事件聚合 → 排序摘要**。
LLM 只負責頭尾兩段，中間的撈取和聚合由程式處理，所以成本低、結果穩定。

判斷「重要」的核心訊號是**有幾家媒體獨立報導同一件事**——這個指標免費、
不需要 LLM，而且比標題聳動程度可靠得多。

---

## 安裝

需要 Python 3.10 以上。

```bash
pip install -r requirements.txt
```

複製設定檔並填入你的 API 金鑰：

```bash
cp .env.example .env
```

`.env` 最少要有這幾行：

```
LLM_PROVIDER=custom
LLM_BASE_URL=https://你的中轉站/v1
LLM_API_KEY=你的金鑰
LLM_MODEL=claude-sonnet-5
LLM_TIMEOUT=300
```

直連原廠的話更簡單，例如 Groq：

```
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
```

支援的供應商：`anthropic` `openai` `gemini` `deepseek` `groq`
`openrouter` `litellm` `ollama` `custom`。詳見 `.env.example`。

## 確認設定正確

```bash
python doctor.py          # 檢查 .env 有沒有問題
python llm.py             # 測試能不能呼叫模型
python llm.py models      # 列出你的帳號可用的模型
python llm.py probe       # 測試中轉站吃哪種請求格式
```

---

## 三種用法

### 1. 互動查詢

```bash
python news_agent.py                        # 開啟後問你要找什麼
python news_agent.py "今天 AI 有什麼新聞？"   # 直接查
python news_agent.py --html --open          # 結果用瀏覽器顯示
```

互動模式的指令：

| 指令 | 作用 |
|---|---|
| `/top 12` | 改成顯示 12 則 |
| `/hours 48` | 搜尋範圍改成過去 48 小時 |
| `/html on` | 改用網頁顯示 |
| `/settings` | 看目前設定 |

其他參數：`--json`（輸出 JSON）、`--no-cache`、`--no-resolve`、`--email`。

### 2. 寄到 email

在 `.env` 加上寄信設定（Gmail 需要**應用程式密碼**，不是登入密碼）：

```
SMTP_PRESET=gmail
SMTP_USER=你的帳號@gmail.com
SMTP_PASS=十六碼應用程式密碼（把空格刪掉）
MAIL_TO=收件人@example.com
```

```bash
python mailer.py check    # 只檢查設定
python mailer.py          # 寄測試信
python news_agent.py "今天 AI 新聞" --email
```

### 3. 產生靜態網站

編輯 `topics.json` 設定要追蹤的主題，然後：

```bash
python build_site.py --dry-run              # 看會產生什麼，不花錢
python build_site.py --no-resolve           # 產生到 public/
```

輸出：每個主題一個 `.html` 和 `.json`，加上一個 `index.html` 索引頁。
`public/` 可以直接丟到 GitHub Pages、Cloudflare Pages、Netlify。

單一主題失敗不會中斷整個建置——那個主題會沿用上次的內容，
索引頁會標示為過期。

---

## 每天自動更新（GitHub Actions）

`.github/workflows/build.yml` 設定成每天台北時間早上 7 點跑一次，
在 GitHub 的機器上執行，**你的電腦不用開機**。公開 repo 免費。

設定步驟：

1. 把這個資料夾推到 GitHub（`.env` 已在 `.gitignore` 裡，不會外洩）
2. Settings → Secrets and variables → Actions，加入：
   `LLM_PROVIDER` `LLM_BASE_URL` `LLM_API_KEY` `LLM_MODEL` `LLM_TIMEOUT`
3. Settings → Pages → Source 選 **GitHub Actions**
4. Actions 分頁手動觸發一次測試

改排程時間就編輯那行 cron，**注意用的是 UTC**（台北時間減 8）：

```yaml
- cron: "0 23 * * *"    # UTC 23:00 = 台北隔天 07:00
```

GitHub 的排程不保證準時，免費方案常延遲 5 到 30 分鐘。
把分鐘改成非整點（例如 `17 23 * * *`）可以避開壅塞。

**推上去之前務必確認金鑰沒有跟著走：**

```bash
git init && git add . && git status
```

清單裡不可以出現 `.env`。出現的話先 `git rm --cached .env` 再繼續。

---

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `news_agent.py` | 主程式：分析管線、互動介面、HTML 輸出 |
| `llm.py` | LLM 抽象層，一份程式碼切換任意供應商 |
| `build_site.py` | 批次產生靜態網站 |
| `mailer.py` | 寄信 |
| `topics.json` | 網站要追蹤哪些主題 |
| `doctor.py` | 設定診斷 |
| `debug_link.py` | 連結還原的診斷工具 |

---

## 調校

**新聞太雜或漏掉重要的** — 調 `news_agent.py` 裡的 `SIM_THRESHOLD`
（目前 0.25）。調高會少併，調低會多併。誤併會產生把好幾件事混在一起的
摘要，比漏併難看，所以寧可設高一點。

**跑太慢** — 時間幾乎全花在生成輸出。降低則數（`--top 5`）效果最直接。
換更快的模型也有幫助，`python llm.py models` 看有哪些可選。

**想加主題** — 編輯 `topics.json`。`slug` 會變成檔名和網址，只用英文和
連字號。避免主題之間關鍵字高度重疊，否則會撈到大量重複的新聞。

---

## 已知限制

**Google News 的連結還原不了。** 那串 `CBMi...` 是加密的 protobuf，
真實網址由 JavaScript 在瀏覽器端算出，純 HTTP 請求拿不到。
HTML 輸出把連結藏在標題底下，所以不影響閱讀。建置時加 `--no-resolve`
可以省下白費的請求。

**中英文的同一則新聞不會合併。** 相似度比對用字元 bigram，
「激增40%」和「大增四成」在字元層面幾乎沒交集。要解決得改用 embedding
語意比對，那是明顯的複雜度躍升。

---

## 使用上的分寸

輸出只包含標題、摘要、來源、時間、連結，流量導回原網站。
不要為了「內容更豐富」去抓全文放上去——新聞內文有著作權，
那樣就從整理工具變成內容農場了。
