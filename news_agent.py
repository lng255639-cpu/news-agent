"""
新聞代理程式 — 用自然語言問，回傳今天的重要新聞
================================================
搭配 llm.py 使用，可切換任意 LLM 供應商。

安裝：
    python -m pip install -r requirements.txt

用法：
    python news_agent.py                        互動模式（開啟後問你要找什麼）
    python news_agent.py "今天 AI 有什麼新聞？"   直接查詢
    python news_agent.py "台股新聞" --hours 48
    python news_agent.py "AI 新聞" --json > today.json
"""

import argparse
import hashlib
import json
import math
import re
import sys
import urllib.parse
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

from llm import llm_json, _config

USER_AGENT = "NewsAgent/1.0"
CACHE_DIR = Path(".cache")
CACHE_TTL = 900          # RSS 快取 15 分鐘，避免反覆重問時重撈
SIM_THRESHOLD = 0.25     # 事件聚合門檻（IDF 加權後；實測邊界很窄，
                         # 寧可少併也不要多併 —— 誤併會產生把好幾件事
                         # 混在一起的摘要，比漏併難看得多）


# Windows 主控台預設可能是 cp950，遇到不在 Big5 裡的字（日文、表情符號）
# 會直接爆 UnicodeEncodeError。改成 UTF-8，無法顯示的字元用 ? 代替。
def _fix_console_encoding():
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", "") or ""
        if enc.lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_fix_console_encoding()


# ============ 第 1 段：問題理解 ============

def plan_queries(question):
    plan = llm_json(f"""你是新聞檢索規劃器。把使用者的問題轉成搜尋計畫。

使用者問題：{question}

請只輸出 JSON：
{{
  "topic": "主題的簡短描述",
  "hours": 時間範圍小時數（「今天」=24，「這週」=168，沒說=24）,
  "keywords": ["中文關鍵字", "English keyword", ...]
}}

keywords 給 5-8 組，涵蓋主題的不同面向：公司名、技術名詞、產業動詞。
中英文都要有，英文能撈到國際媒體。
例如「AI 新聞」可展開成：
["AI", "人工智慧", "OpenAI", "生成式AI", "AI chip", "LLM", "AI regulation"]""")

    plan["hours"] = max(1, min(int(plan.get("hours", 24)), 720))
    plan["keywords"] = [k for k in plan.get("keywords", []) if k.strip()][:10]
    if not plan["keywords"]:
        plan["keywords"] = [question]
    plan.setdefault("topic", question)
    return plan


# ============ 第 2 段：廣泛撈取 ============

def _cache_path(keyword):
    key = hashlib.sha256(keyword.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{key}.json"


def _fetch_keyword(keyword, use_cache=True):
    path = _cache_path(keyword)

    if use_cache and path.exists():
        age = datetime.now().timestamp() - path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(path.read_text(encoding="utf-8"))

    q = urllib.parse.quote(keyword)
    url = (f"https://news.google.com/rss/search?q={q}"
           "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    feed = feedparser.parse(url, agent=USER_AGENT)

    entries = []
    for e in feed.entries[:30]:
        published = e.get("published_parsed")
        if not published:
            continue
        entries.append({
            "title": e.title,
            "link": e.link,
            "source": e.get("source", {}).get("title", "未知來源"),
            "published": datetime(*published[:6], tzinfo=timezone.utc).isoformat(),
        })

    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return entries


def collect(keywords, hours, use_cache=True, log=print):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items, seen_links = [], set()

    for kw in keywords:
        try:
            entries = _fetch_keyword(kw, use_cache)
        except Exception as e:
            log(f"      「{kw}」撈取失敗：{e}")
            continue

        added = 0
        for e in entries:
            if e["link"] in seen_links:
                continue
            dt = datetime.fromisoformat(e["published"])
            if dt < cutoff:
                continue
            seen_links.add(e["link"])
            items.append({**e, "published": dt})
            added += 1

        log(f"      「{kw}」+{added} 則（累計 {len(items)}）")

    return items


# ============ 第 3 段：事件聚合 ============

def strip_source_suffix(title):
    """Google News 的標題結尾都帶「 - 媒體名」，比對前要拿掉。

    留著的話，兩則同一事件但來自不同媒體的標題，會因為結尾不同
    而被判定為不相似 —— 這正是我們最不想要的。
    """
    return re.sub(r"\s*[-–—|]\s*[^-–—|]{1,20}$", "", title).strip()


def bigrams(text):
    text = re.sub(r"[^\w]", "", text.lower())
    return {text[i:i + 2] for i in range(len(text) - 1)}


def build_idf(items):
    """依這批標題算出每個 bigram 的稀有度權重。

    關鍵：「Anthropic」在 116 則裡出現十幾次，兩則不相干的
    Anthropic 新聞光靠公司名就有 8 個共同 bigram，會被誤判成同一事件。
    IDF 讓常見詞近乎不計分，稀有詞（「法說會」「TPU」）才有份量。
    """
    df = Counter()
    for it in items:
        for b in bigrams(strip_source_suffix(it["title"])):
            df[b] += 1
    n = len(items)
    idf = {b: math.log((n + 1) / (c + 1)) + 0.15 for b, c in df.items()}
    return idf, math.log(n + 1) + 0.15


def similarity(a, b, idf=None, default=1.0):
    """IDF 加權的 overlap 係數（交集權重 / 較短者的權重）。

    用 overlap 而非 Jaccard，是因為中文標題長短差異大，
    Jaccard 會被長標題稀釋。不傳 idf 就退化成未加權版本。
    """
    A = bigrams(strip_source_suffix(a))
    B = bigrams(strip_source_suffix(b))
    if not A or not B:
        return 0.0

    if idf is None:
        return len(A & B) / min(len(A), len(B))

    weight = lambda s: sum(idf.get(x, default) for x in s)
    denom = min(weight(A), weight(B))
    return weight(A & B) / denom if denom else 0.0


def cluster(items, threshold=SIM_THRESHOLD):
    idf, idf_default = build_idf(items)
    clusters = []
    for item in items:
        # 跟每一群的代表標題比，挑「最像的那一群」而不是「第一個夠像的」。
        #
        # 用「任一成員夠像就併」會出事：A 和 B 都提到 Anthropic、
        # B 和 C 都在講服務中斷，結果 A 和 C 這兩件不相干的事
        # 被鏈在一起。只跟代表比就切斷了這種傳遞。
        best, best_score = None, threshold
        for c in clusters:
            score = similarity(item["title"], c["items"][0]["title"],
                               idf, idf_default)
            if score >= best_score:
                best, best_score = c, score

        if best is not None:
            best["items"].append(item)
        else:
            clusters.append({"items": [item]})

    for c in clusters:
        sources = {i["source"] for i in c["items"]}
        c["source_count"] = len(sources)
        c["sources"] = sorted(sources)
        c["earliest"] = min(i["published"] for i in c["items"])
        c["latest"] = max(i["published"] for i in c["items"])
        c["rep"] = max(c["items"], key=lambda x: x["published"])

    clusters.sort(key=lambda c: (c["source_count"], c["latest"]), reverse=True)
    return clusters


# ============ 第 4、5 段：排序 + 摘要 ============

def rank_and_summarize(question, clusters, top_n=8, candidates=25):
    payload = [
        {
            "id": i,
            "titles": [x["title"] for x in c["items"][:3]],
            "source_count": c["source_count"],
            "sources": c["sources"][:5],
        }
        for i, c in enumerate(clusters[:candidates])
    ]

    result = llm_json(f"""使用者問：{question}

以下是這段時間撈到的新聞事件（重複報導已合併）。
source_count 是有幾家媒體報同一件事，數字高通常代表重要，但不是絕對。

{json.dumps(payload, ensure_ascii=False, indent=1)}

挑出對這個問題最重要的 {top_n} 則，依重要性排序。

注意：titles 是用標題相似度自動分群的，可能出錯。
如果某一組的 titles 其實在講「不只一件事」（例如同一家公司的
財報、當機、併購被混在一起），請只挑其中最重要的那一件來寫，
不要把不同的事硬湊成一段摘要。這種情況下 source_count 也不可信，
請依你自己的判斷評估重要性。

排序原則：
- source_count >= 2 的事件優先。多家媒體同時報導是重要性的可靠訊號。
- source_count == 1 且內容聳動或宣稱重大突破的，要保守看待 —— 可能是
  未經查證的單一報導。除非確實重要，否則不要排在前面。
- 排除：業配、股票喊盤、內容農場、個別股價漲跌、和問題無關的同名誤撈。
- 社會案件除非牽涉該領域的制度或技術本身，否則不算該領域的重要新聞。
- 如果相關的不到 {top_n} 則，就只給相關的，不要硬湊。

只輸出 JSON：
{{
  "items": [
    {{
      "id": 對應上面的 id,
      "headline": "改寫成清楚的一句話標題",
      "summary": "2-3 句話說明發生什麼事",
      "why": "一句話說明為什麼重要"
    }}
  ]
}}""")

    valid = range(len(payload))
    ranked = _normalize_ranked(result, clusters, valid)

    # 模型完全沒照格式回 → 退回純演算法排序，至少還有東西可看
    if not ranked:
        return _fallback_ranking(clusters, top_n)
    return ranked


# --- 模型輸出的容錯處理 ---
#
# 不同模型（尤其小模型）常常自作主張換鍵名或漏欄位。
# 與其相信它會乖乖照做，不如把各種寫法都接住。

_ALIASES = {
    "id": ("id", "index", "idx", "cluster_id", "編號"),
    "headline": ("headline", "title", "head", "標題"),
    "summary": ("summary", "description", "desc", "content", "摘要"),
    "why": ("why", "reason", "importance", "why_important",
            "significance", "why_it_matters", "為什麼重要"),
}


def _pick(obj, names):
    for n in names:
        value = obj.get(n)
        if value not in (None, "", [], {}):
            return value
    return None


def _coerce_list(result):
    """模型可能回 {"items":[...]}、直接回 [...]、或包在別的鍵底下。"""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("items", "news", "results", "data", "articles", "新聞"):
            if isinstance(result.get(key), list):
                return result[key]
        if any(k in result for k in _ALIASES["id"]):   # 只回了單一則
            return [result]
        # 只有一個鍵而值是 list 的話就用它
        values = [v for v in result.values() if isinstance(v, list)]
        if len(values) == 1:
            return values[0]
    return []


def _normalize_ranked(result, clusters, valid):
    out, used = [], set()

    for raw in _coerce_list(result):
        if not isinstance(raw, dict):
            continue

        idx = _pick(raw, _ALIASES["id"])
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        if idx not in valid or idx in used:      # 過濾幻想的 id 和重複
            continue
        used.add(idx)

        # 缺欄位就退回用原始標題，不要整個報錯
        headline = _pick(raw, _ALIASES["headline"]) \
            or clusters[idx]["rep"]["title"]
        summary = _pick(raw, _ALIASES["summary"]) or ""
        why = _pick(raw, _ALIASES["why"]) or ""

        out.append({
            "id": idx,
            "headline": str(headline).strip(),
            "summary": str(summary).strip(),
            "why": str(why).strip(),
        })

    return out


def _fallback_ranking(clusters, top_n):
    """模型不配合時的保底：直接用報導家數排序，標題用原文。"""
    print("      （模型輸出格式異常，改用報導家數排序）", file=sys.stderr)
    return [
        {
            "id": i,
            "headline": c["rep"]["title"],
            "summary": "",
            "why": "",
        }
        for i, c in enumerate(clusters[:top_n])
    ]


# ============ 時間顯示 ============
# RSS 給的是 UTC，要轉成使用者本地時區（台灣是 UTC+8）才有意義。
# astimezone() 不帶參數就會用系統時區，Windows / Mac / Linux 都適用。

def to_local(dt):
    return dt.astimezone()


def relative_time(dt, now=None):
    """回傳「3 小時前」這種相對時間。人比較容易感覺到新舊。"""
    now = now or datetime.now(timezone.utc)
    seconds = (now - dt).total_seconds()

    if seconds < 0:                       # 少數來源時間會超前一點點
        return "剛剛"
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} 分鐘前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小時前"
    days = int(seconds // 86400)
    if days == 1:
        return "1 天前"
    return f"{days} 天前"


def clock_time(dt, now=None):
    """回傳「今天 14:30」或「8/22 16:40」這種絕對時間。"""
    now = now or datetime.now(timezone.utc)
    local = to_local(dt)
    today = to_local(now).date()
    delta_days = (today - local.date()).days

    if delta_days == 0:
        return f"今天 {local:%H:%M}"
    if delta_days == 1:
        return f"昨天 {local:%H:%M}"
    if delta_days == 2:
        return f"前天 {local:%H:%M}"
    return f"{local.month}/{local.day} {local:%H:%M}"


def time_label(cluster_, now=None):
    """事件的時間標籤。以最早報導為準——那才是事情發生的時間點。"""
    now = now or datetime.now(timezone.utc)
    first = cluster_["earliest"]
    label = f"{relative_time(first, now)}（{clock_time(first, now)}）"

    # 跨度大代表事情還在延燒，值得標出來
    span_hours = (cluster_["latest"] - first).total_seconds() / 3600
    if span_hours >= 3:
        label += f"，持續報導至 {relative_time(cluster_['latest'], now)}"
    return label


# ============ 連結還原 ============
#
# Google News 的 RSS 連結長這樣：
#   news.google.com/rss/articles/CBMiWEFVX3lxTE92...
# 那串是加密的 protobuf，離線解不出原始網址（試過了，裡面沒有明文 URL），
# 只能實際發請求跟著轉址走。
#
# 只對「要顯示的那幾則」做，用多執行緒並行，任何一步失敗都退回原連結。

LINK_CACHE = CACHE_DIR / "links.json"
RESOLVE_TIMEOUT = 6
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/122.0 Safari/537.36")


def _load_link_cache():
    try:
        return json.loads(LINK_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_link_cache(cache):
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        LINK_CACHE.write_text(json.dumps(cache, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        pass


BLOCKED_HOST_PARTS = (
    "google.com", "googleusercontent.com", "gstatic.com",
    "googleapis.com", "googlesyndication.com", "googletagmanager.com",
    "google.co", "youtube.com", "ggpht.com", "doubleclick.net",
    "schema.org", "w3.org",
)

ASSET_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".woff", ".woff2", ".mp4", ".pdf",
)


def _looks_like_article(url):
    """判斷這個網址像不像一篇文章，而不是圖片、資源檔或首頁。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.netloc.lower()
    if not host or any(b in host for b in BLOCKED_HOST_PARTS):
        return False

    path = parsed.path.lower()
    if any(path.endswith(ext) for ext in ASSET_EXTENSIONS):
        return False

    # 圖片 CDN 常見的尺寸參數，例如 =w16 或 =s200
    if re.search(r"=[ws]\d+", url):
        return False

    # 只有網域沒有路徑，多半是首頁或看板，不是文章
    if len(path.strip("/")) < 3:
        return False

    return True


def _extract_target(html):
    """從 Google 的中介頁面裡挖出真正的文章網址。

    Google 的頁面結構會變，所以準備了三種策略，由準到糙排列。
    每個候選都要通過 _looks_like_article 檢查 —— 少了這道驗證，
    第三個策略會抓到頁面上的圖片網址（親身踩過）。
    """
    # 1) Google 自己標註目標網址的屬性
    for m in re.finditer(r'data-n-au="(https?://[^"]+)"', html):
        if _looks_like_article(m.group(1)):
            return m.group(1)

    # 2) meta refresh 轉址
    m = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+'
                  r'content=["\'][^"\']*url=(https?://[^"\'>\s]+)',
                  html, re.I)
    if m and _looks_like_article(m.group(1)):
        return m.group(1)

    # 3) 頁面裡第一個看起來像文章的外部連結
    for candidate in re.findall(r'href="(https?://[^"]+)"', html):
        if _looks_like_article(candidate):
            return candidate

    return None


def _clean_url(url):
    """拿掉追蹤參數，讓網址短一點也乾淨一點。"""
    try:
        parsed = urllib.parse.urlparse(url)
        keep = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query)
                if not k.lower().startswith(("utm_", "fbclid", "gclid"))
                and k.lower() not in ("oc", "hl", "gl", "ceid")]
        return urllib.parse.urlunparse(parsed._replace(
            query=urllib.parse.urlencode(keep), fragment=""))
    except Exception:
        return url


def resolve_link(url, session=None):
    """回傳真實網址；任何失敗都回傳原網址，絕不拋錯。"""
    if "news.google.com" not in url:
        return _clean_url(url)

    getter = session or requests
    try:
        resp = getter.get(url, timeout=RESOLVE_TIMEOUT,
                          headers={"User-Agent": BROWSER_UA},
                          allow_redirects=True)
    except Exception:
        return url

    # 轉址已經把我們送到出版社網站
    if "news.google.com" not in resp.url:
        return _clean_url(resp.url)

    # 還在 Google，那就從頁面內容裡挖
    try:
        target = _extract_target(resp.text)
    except Exception:
        target = None
    return _clean_url(target) if target else url


def resolve_links(clusters, ranked, log=print):
    """把要顯示的那幾則的連結換成真實網址（並行處理）。"""
    if not ranked:
        return

    cache = _load_link_cache()
    todo = []
    for r in ranked:
        original = clusters[r["id"]]["rep"]["link"]
        if original in cache:
            clusters[r["id"]]["rep"]["link"] = cache[original]
        else:
            todo.append((r["id"], original))

    if not todo:
        return

    log(f"      還原 {len(todo)} 個連結…")
    session = requests.Session()
    outcome = {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(resolve_link, url, session): (cid, url)
                   for cid, url in todo}
        for future in as_completed(futures):
            cid, original = futures[future]
            try:
                final = future.result()
            except Exception:
                final = original
            outcome[cid] = (original, final)

    # 健全性檢查：多筆還原成同一個網址，代表擷取邏輯抓錯東西了
    # （曾經全部抓到頁面上的同一張圖）。寧可全部退回原連結。
    changed = [(o, f) for o, f in outcome.values() if f != o]
    distinct = {f for _, f in changed}
    if len(changed) >= 3 and len(distinct) == 1:
        log("      還原結果全部相同，判定為擷取錯誤，退回原連結")
        return

    resolved = 0
    for cid, (original, final) in outcome.items():
        cache[original] = final
        clusters[cid]["rep"]["link"] = final
        if final != original:
            resolved += 1

    _save_link_cache(cache)
    log(f"      成功還原 {resolved}/{len(todo)} 個")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --paper: #f2f4f7;
    --card: #ffffff;
    --ink: #16202e;
    --muted: #5c6b7f;
    --rule: #d6dce4;
    --signal: #0b6e6e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 2.5rem 1.25rem 4rem;
    background: var(--paper);
    color: var(--ink);
    font-family: Georgia, "Songti TC", "宋體", serif;
    line-height: 1.65;
  }}
  .wrap {{ max-width: 44rem; margin: 0 auto; }}

  header {{ margin-bottom: 2.5rem; }}
  .eyebrow {{
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--signal);
    margin: 0 0 0.5rem;
  }}
  h1 {{
    font-family: "Segoe UI Semibold", "Segoe UI", system-ui, sans-serif;
    font-size: 1.7rem;
    font-weight: 600;
    letter-spacing: -0.015em;
    margin: 0 0 0.6rem;
  }}
  .meta {{
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: 0.82rem;
    color: var(--muted);
    margin: 0;
  }}

  article {{
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 1.4rem 1.5rem;
    margin-bottom: 1rem;
  }}

  /* 佐證強度：一格代表一家媒體。這是本頁唯一的視覺記號，
     而且編碼的是真實資訊 —— 有幾家媒體獨立報導同一件事。 */
  .corro {{
    display: flex;
    align-items: center;
    gap: 0.55rem;
    margin-bottom: 0.7rem;
  }}
  .bars {{ display: flex; gap: 2px; }}
  .bar {{
    width: 4px; height: 13px;
    background: var(--signal);
    border-radius: 1px;
  }}
  .bar.off {{ background: var(--rule); }}
  .corro-label {{
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: 0.72rem;
    letter-spacing: 0.04em;
    color: var(--muted);
  }}

  h2 {{
    font-family: "Segoe UI Semibold", "Segoe UI", system-ui, sans-serif;
    font-size: 1.14rem;
    font-weight: 600;
    line-height: 1.4;
    letter-spacing: -0.01em;
    margin: 0 0 0.5rem;
  }}
  h2 a {{ color: var(--ink); text-decoration: none; }}
  h2 a:hover {{ color: var(--signal); text-decoration: underline; }}
  h2 a:focus-visible {{ outline: 2px solid var(--signal); outline-offset: 3px; }}

  .summary {{ margin: 0 0 0.7rem; font-size: 0.97rem; }}
  .why {{
    margin: 0 0 0.9rem;
    padding-left: 0.8rem;
    border-left: 2px solid var(--signal);
    font-size: 0.92rem;
    color: var(--muted);
  }}
  .foot {{
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: 0.78rem;
    color: var(--muted);
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 0.9rem;
  }}
  .empty {{
    background: var(--card); border: 1px solid var(--rule);
    padding: 2rem; text-align: center; color: var(--muted);
  }}
  footer {{
    margin-top: 2.5rem;
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: 0.75rem;
    color: var(--muted);
    border-top: 1px solid var(--rule);
    padding-top: 1rem;
  }}
  @media (max-width: 30rem) {{
    body {{ padding: 1.5rem 0.9rem 3rem; }}
    article {{ padding: 1.1rem 1.1rem; }}
  }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <p class="eyebrow">新聞簡報</p>
  <h1>{question}</h1>
  <p class="meta">{stamp}　·　過去 {hours} 小時　·　{count} 則</p>
</header>
{items}
<footer>由 news_agent 自動整理　·　佐證格數代表有幾家媒體報導同一事件</footer>
</div>
<script src="https://news-agent-assistant.onrender.com/widget.js"
        data-name="AI小助手"
        data-accent="#0b6e6e"
        data-starters="這頁在講什麼？|那排小格子是什麼意思？|其他主題今天有什麼？"></script>
</body>
</html>
"""

ITEM_TEMPLATE = """<article>
  <div class="corro">
    <span class="bars">{bars}</span>
    <span class="corro-label">{count} 家媒體</span>
  </div>
  <h2><a href="{link}" target="_blank" rel="noopener">{headline}</a></h2>
  {summary}
  {why}
  <div class="foot"><span>{time}</span><span>{sources}</span></div>
</article>
"""


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_html(question, ranked, clusters, plan):
    now = datetime.now(timezone.utc)
    blocks = []

    for r in ranked:
        c = clusters[r["id"]]
        n = c["source_count"]
        bars = "".join(
            f'<span class="bar{"" if i < n else " off"}"></span>'
            for i in range(max(5, n))
        )
        blocks.append(ITEM_TEMPLATE.format(
            bars=bars,
            count=n,
            link=_esc(c["rep"]["link"]),
            headline=_esc(r.get("headline") or c["rep"]["title"]),
            summary=f'<p class="summary">{_esc(r["summary"])}</p>'
                    if r.get("summary") else "",
            why=f'<p class="why">{_esc(r["why"])}</p>' if r.get("why") else "",
            time=_esc(time_label(c, now)),
            sources=_esc("、".join(c["sources"][:4])),
        ))

    if not blocks:
        blocks = ['<div class="empty">這段時間內沒找到相關的重要新聞。</div>']

    return HTML_TEMPLATE.format(
        title=_esc(question),
        question=_esc(question),
        stamp=to_local(now).strftime("%Y-%m-%d %H:%M"),
        hours=plan["hours"],
        count=len(ranked),
        items="\n".join(blocks),
    )


def render_html(question, ranked, clusters, plan, path):
    path = Path(path)
    path.write_text(build_html(question, ranked, clusters, plan),
                    encoding="utf-8")
    return path.resolve()


def build_text(question, ranked, clusters, plan):
    """純文字版，給郵件的 fallback 用。"""
    now = datetime.now(timezone.utc)
    lines = [question, to_local(now).strftime("%Y-%m-%d %H:%M"), ""]
    for n, r in enumerate(ranked, 1):
        c = clusters[r["id"]]
        lines.append(f"{n}. {r.get('headline') or c['rep']['title']}")
        lines.append(f"   [{time_label(c, now)}] {c['source_count']} 家媒體")
        if r.get("summary"):
            lines.append(f"   {r['summary']}")
        if r.get("why"):
            lines.append(f"   -> {r['why']}")
        lines.append(f"   {c['rep']['link']}")
        lines.append("")
    return "\n".join(lines)


# ============ 輸出 ============

def render_text(question, ranked, clusters, plan):
    print(f"\n{'=' * 64}")
    print(f"  {question}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}"
          f"  過去 {plan['hours']} 小時")
    print(f"{'=' * 64}\n")

    if not ranked:
        print("  這段時間內沒找到相關的重要新聞。")
        print("  試試換個問法，或加大時間範圍。\n")
        return

    now = datetime.now(timezone.utc)
    for n, r in enumerate(ranked, 1):
        c = clusters[r["id"]]
        print(f"{n}. {r.get('headline') or c['rep']['title']}")
        print(f"   [{time_label(c, now)}]")
        if r.get("summary"):
            print(f"   {r['summary']}")
        if r.get("why"):
            print(f"   -> {r['why']}")
        print(f"   {c['source_count']} 家媒體報導："
              f"{'、'.join(c['sources'][:3])}")
        print(f"   {c['rep']['link']}\n")


def render_json(question, ranked, clusters, plan):
    now = datetime.now(timezone.utc)
    print(json.dumps({
        "question": question,
        "topic": plan["topic"],
        "hours": plan["hours"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": str(datetime.now().astimezone().tzinfo),
        "items": [
            {
                "headline": r.get("headline") or clusters[r["id"]]["rep"]["title"],
                "summary": r.get("summary", ""),
                "why": r.get("why", ""),
                "time_label": time_label(clusters[r["id"]], now),
                "first_published": to_local(clusters[r["id"]]["earliest"]).isoformat(),
                "last_published": to_local(clusters[r["id"]]["latest"]).isoformat(),
                "source_count": clusters[r["id"]]["source_count"],
                "sources": clusters[r["id"]]["sources"],
                "link": clusters[r["id"]]["rep"]["link"],
            }
            for r in ranked
        ],
    }, ensure_ascii=False, indent=2))


# ============ 單次查詢 ============

def analyze(question, top=8, hours=None, use_cache=True, resolve=True,
            log=lambda m: None):
    """跑完整分析流程，回傳 (ranked, clusters, plan)。

    沒有任何輸出副作用，方便被批次產生、網站建置等情境重複使用。
    撈不到新聞時回傳 (None, None, plan)。
    """
    log("[1/4] 解析問題…（呼叫模型，可能要等幾十秒）")
    plan = plan_queries(question)
    if hours:
        plan["hours"] = hours
    log(f"      主題：{plan['topic']}  範圍：過去 {plan['hours']} 小時")
    log(f"      關鍵字：{'、'.join(plan['keywords'])}")

    log("[2/4] 撈取新聞…")
    items = collect(plan["keywords"], plan["hours"], use_cache, log)
    if not items:
        log("      這段時間內沒撈到新聞。")
        return None, None, plan

    log("[3/4] 聚合事件…")
    clusters = cluster(items)
    log(f"      {len(items)} 則報導 -> {len(clusters)} 個事件")

    log(f"[4/4] 排序與摘要…（送出 {min(len(clusters), 25)} 個事件給模型）")
    ranked = rank_and_summarize(question, clusters, top_n=top)

    if resolve:
        resolve_links(clusters, ranked, log)

    return ranked, clusters, plan


def run_query(question, top=8, hours=None, use_cache=True, as_json=False,
              resolve=True, html_path=None, open_browser=False,
              email=False):
    """跑完整流程並輸出。回傳 True 表示成功。"""
    def log(msg):
        if not as_json:
            print(msg, file=sys.stderr)

    ranked, clusters, plan = analyze(question, top, hours, use_cache,
                                     resolve, log)
    if ranked is None:
        log("      試試放寬時間範圍（--hours）。")
        return False

    if email:
        from mailer import send_html
        subject = f"{plan['topic']}｜{to_local(datetime.now(timezone.utc)):%m/%d}"
        try:
            sent = send_html(subject,
                             build_html(question, ranked, clusters, plan),
                             build_text(question, ranked, clusters, plan))
            log(f"      已寄給 {', '.join(sent)}")
        except Exception as e:
            log(f"      寄信失敗：{e}")

    if html_path:
        out = render_html(question, ranked, clusters, plan, html_path)
        log(f"      已寫入 {out}")
        if open_browser:
            webbrowser.open(out.as_uri())
        if not as_json:
            print(f"\n已產生簡報：{out}")
    elif as_json:
        render_json(question, ranked, clusters, plan)
    else:
        render_text(question, ranked, clusters, plan)
    return True


# ============ 互動模式 ============

EXAMPLES = [
    "今天 AI 有什麼重要新聞？",
    "這週半導體產業有什麼動靜？",
    "最近台灣的能源政策新聞",
    "過去兩天國際財經大事",
]

QUIT_WORDS = {"q", "quit", "exit", "離開", "結束", "bye"}


def interactive(args):
    try:
        cfg = _config()
    except Exception as e:
        print(f"LLM 設定有問題：{e}")
        print("請先跑 python doctor.py 檢查設定。")
        return

    print("=" * 64)
    print("  新聞代理程式")
    print("=" * 64)
    print("\n可以這樣問：")
    for ex in EXAMPLES:
        print(f"    {ex}")
    print("\n指令：/top 12 改筆數　/hours 48 改範圍"
          "　/html on 改用網頁顯示　/settings 看設定")
    print("      直接按 Enter 或輸入 q 離開")

    top, hours = args.top, args.hours
    html_mode = bool(args.html)

    while True:
        try:
            question = input("\n要找什麼相關的新聞？\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再見。")
            return

        if not question or question.lower() in QUIT_WORDS:
            print("再見。")
            return

        # --- 指令 ---
        if question.startswith("/"):
            parts = question[1:].split()
            cmd = parts[0].lower() if parts else ""
            value = parts[1] if len(parts) > 1 else None

            if cmd == "top" and value and value.isdigit():
                top = max(1, min(int(value), 30))
                print(f"  已設定：每次顯示 {top} 則")
            elif cmd == "hours" and value and value.isdigit():
                hours = max(1, min(int(value), 720))
                print(f"  已設定：搜尋過去 {hours} 小時")
            elif cmd == "html":
                html_mode = value not in ("off", "0", "false")
                print(f"  HTML 簡報：{'開啟（會自動用瀏覽器顯示）' if html_mode else '關閉'}")
            elif cmd == "settings":
                print(f"  供應商：{cfg['name']} / {cfg['model']}")
                print(f"  顯示筆數：{top}")
                print(f"  時間範圍：{hours or '交給 AI 判斷'}")
                print(f"  HTML 簡報：{'開' if html_mode else '關'}")
            else:
                print("  不認得的指令。可用：/top 數字　/hours 數字"
                      "　/html on|off　/settings")
            continue

        # --- 查詢 ---
        try:
            run_query(question, top=top, hours=hours,
                      use_cache=not args.no_cache,
                      resolve=not args.no_resolve,
                      html_path="news.html" if html_mode else None,
                      open_browser=html_mode)
        except KeyboardInterrupt:
            print("\n  已中斷這次查詢。")
        except Exception as e:
            print(f"\n  查詢出錯：{e}")
            print("  可以換個問法再試一次。")


# ============ 進入點 ============

def main():
    ap = argparse.ArgumentParser(
        description="用自然語言查今天的重要新聞（不給問題就進入互動模式）")
    ap.add_argument("question", nargs="*", help="你的問題")
    ap.add_argument("--top", type=int, default=8, help="輸出幾則（預設 8）")
    ap.add_argument("--hours", type=int, help="時間範圍（小時）")
    ap.add_argument("--no-cache", action="store_true", help="不使用 RSS 快取")
    ap.add_argument("--no-resolve", action="store_true",
                    help="不還原 Google 轉址連結（比較快）")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--html", nargs="?", const="news.html", metavar="檔名",
                    help="輸出成 HTML 簡報（預設 news.html）")
    ap.add_argument("--open", action="store_true",
                    help="產生 HTML 後直接用瀏覽器開啟")
    ap.add_argument("--email", action="store_true",
                    help="把簡報寄到 .env 設定的信箱")
    args = ap.parse_args()

    if args.question:
        question = " ".join(args.question)
        try:
            _config()
        except Exception as e:
            sys.exit(f"LLM 設定有問題：{e}\n請先跑 python doctor.py 檢查。")
        run_query(question, top=args.top, hours=args.hours,
                  use_cache=not args.no_cache, as_json=args.json,
                  resolve=not args.no_resolve,
                  html_path=args.html, open_browser=args.open,
                  email=args.email)
    else:
        interactive(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        import sys as _sys
        print("\n已中斷。", file=_sys.stderr)
        raise SystemExit(130)
