"""
靜態網站產生器
==============
依 topics.json 逐一產生各主題的簡報頁，外加一個索引頁。
輸出全部是靜態 HTML，可直接丟到 GitHub Pages / Cloudflare Pages / Netlify。

用法：
    python build_site.py                    產生到 public/
    python build_site.py --out docs         改輸出目錄
    python build_site.py --only ai chip     只重建指定主題
    python build_site.py --dry-run          只列出要做什麼，不呼叫 API

單一主題失敗不會中斷整個建置 —— 那個主題會沿用上次的頁面。
"""

import argparse
import json
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from news_agent import (
    analyze, build_html, time_label, to_local, _esc,
)

CONFIG = Path("topics.json")


# ---------------------------------------------------------------
# 索引頁
# 沿用簡報頁的設計語彙：冷灰底、深藍墨、單一青色點綴。
# 每張卡片顯示該主題最新一則的標題，讓索引本身就有資訊量。
# ---------------------------------------------------------------

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{site_title}</title>
<style>
  :root {{
    --paper: #f2f4f7; --card: #ffffff; --ink: #16202e;
    --muted: #5c6b7f; --rule: #d6dce4; --signal: #0b6e6e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 3rem 1.25rem 4rem;
    background: var(--paper); color: var(--ink);
    font-family: Georgia, "Songti TC", serif; line-height: 1.65;
  }}
  .wrap {{ max-width: 44rem; margin: 0 auto; }}
  .eyebrow {{
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: .72rem; font-weight: 600; letter-spacing: .14em;
    text-transform: uppercase; color: var(--signal); margin: 0 0 .5rem;
  }}
  h1 {{
    font-family: "Segoe UI Semibold", "Segoe UI", system-ui, sans-serif;
    font-size: 1.9rem; font-weight: 600; letter-spacing: -.015em;
    margin: 0 0 .5rem;
  }}
  .meta {{
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: .82rem; color: var(--muted); margin: 0 0 2.5rem;
  }}
  a.card {{
    display: block; background: var(--card); border: 1px solid var(--rule);
    border-radius: 3px; padding: 1.3rem 1.5rem; margin-bottom: .9rem;
    text-decoration: none; color: inherit;
  }}
  a.card:hover {{ border-color: var(--signal); }}
  .card.dead {{
    display: block; background: var(--card); border: 1px dashed var(--rule);
    border-radius: 3px; padding: 1.3rem 1.5rem; margin-bottom: .9rem;
    opacity: .65;
  }}
  a.card:focus-visible {{ outline: 2px solid var(--signal); outline-offset: 3px; }}
  .label {{
    font-family: "Segoe UI Semibold", "Segoe UI", system-ui, sans-serif;
    font-size: 1.08rem; font-weight: 600; margin: 0 0 .35rem;
  }}
  .lede {{ margin: 0 0 .6rem; font-size: .95rem; color: var(--ink); }}
  .stamp {{
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: .76rem; color: var(--muted);
    display: flex; flex-wrap: wrap; gap: .3rem .9rem;
  }}
  .stale {{ color: #9a6b1f; }}
  footer {{
    margin-top: 2.5rem; border-top: 1px solid var(--rule); padding-top: 1rem;
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: .75rem; color: var(--muted);
  }}
  footer a {{ color: var(--signal); }}
  @media (max-width: 30rem) {{
    body {{ padding: 2rem .9rem 3rem; }}
    a.card {{ padding: 1.1rem; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <p class="eyebrow">新聞簡報</p>
  <h1>{site_title}</h1>
  <p class="meta">更新於 {stamp}</p>
{cards}
  <footer>{site_note}<br><a href="about.html">關於這個網站</a></footer>
</div>
<script src="https://news-agent-assistant.onrender.com/widget.js"
        data-name="AI小助手"
        data-accent="#0b6e6e"
        data-starters="今天有什麼重要新聞？|那排小格子是什麼意思？|時間標籤怎麼看？"></script>
</body>
</html>
"""

CARD_TEMPLATE = """  <a class="card" href="{slug}.html">
    <p class="label">{label}</p>
    <p class="lede">{lede}</p>
    <div class="stamp"><span class="{stale_class}">{updated}</span><span>{count} 則</span></div>
  </a>
"""

# 頁面根本不存在時用這個 —— 連過去只會 404，不如不要做成連結。
# CI 環境每次都是全新 checkout，沒有舊頁面可以沿用。
DEAD_CARD_TEMPLATE = """  <div class="card dead">
    <p class="label">{label}</p>
    <p class="lede">{lede}</p>
    <div class="stamp"><span class="stale">本次未能產生</span></div>
  </div>
"""


def render_index(config, states, out_dir):
    now = datetime.now(timezone.utc)
    cards = []

    for topic in config["topics"]:
        slug = topic["slug"]
        state = states.get(slug, {})
        page_exists = (out_dir / f"{slug}.html").exists()

        if not page_exists:
            # 連過去會 404，做成不可點的卡片
            cards.append(DEAD_CARD_TEMPLATE.format(
                label=_esc(topic["label"]),
                lede=_esc("這次建置沒有產生內容，下次更新會再試。"),
            ))
            continue

        if not state.get("ok"):
            lede = "（本次更新失敗，顯示的是上一版內容）"
            updated = state.get("updated_label") or "時間不明"
            stale = "stale"
        else:
            lede = state.get("lede", "")
            updated = state.get("updated_label", "")
            stale = ""

        cards.append(CARD_TEMPLATE.format(
            slug=_esc(slug),
            label=_esc(topic["label"]),
            lede=_esc(lede),
            updated=_esc(updated),
            stale_class=stale,
            count=state.get("count", 0),
        ))

    html = INDEX_TEMPLATE.format(
        site_title=_esc(config.get("site_title", "每日新聞簡報")),
        site_note=_esc(config.get("site_note", "")),
        stamp=to_local(now).strftime("%Y-%m-%d %H:%M"),
        cards="".join(cards),
    )
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------
# 建置
# ---------------------------------------------------------------

def build_topic(topic, out_dir, use_cache, resolve=True, retries=1):
    """產生單一主題。回傳狀態 dict。

    中轉站偶爾會逾時，所以失敗會重試 —— 為了一次網路抖動就讓
    整個主題沿用昨天的內容並不划算。
    """
    slug = topic["slug"]
    question = topic["question"]

    def log(msg):
        print(f"  {msg}", file=sys.stderr)

    last_error = None
    for attempt in range(retries + 1):
        try:
            ranked, clusters, plan = analyze(
                question,
                top=topic.get("top", 8),
                hours=topic.get("hours"),
                use_cache=use_cache,
                resolve=topic.get("resolve", resolve),
                log=log,
            )
            if not ranked:
                raise RuntimeError("沒有可用的新聞")
            break
        except Exception as e:
            last_error = e
            if attempt < retries:
                log(f"失敗（{str(e)[:80]}），10 秒後重試…")
                time.sleep(10)
            else:
                raise last_error

    html = build_html(question, ranked, clusters, plan)
    (out_dir / f"{slug}.html").write_text(html, encoding="utf-8")

    # 索引頁要用的摘要資訊
    now = datetime.now(timezone.utc)
    top_item = ranked[0]
    lede = top_item.get("headline") or clusters[top_item["id"]]["rep"]["title"]

    # 同時輸出 JSON，方便之後接別的前端
    data = {
        "slug": slug,
        "label": topic["label"],
        "question": question,
        "generated_at": now.isoformat(),
        "items": [
            {
                "headline": r.get("headline") or clusters[r["id"]]["rep"]["title"],
                "summary": r.get("summary", ""),
                "why": r.get("why", ""),
                "time_label": time_label(clusters[r["id"]], now),
                "source_count": clusters[r["id"]]["source_count"],
                "sources": clusters[r["id"]]["sources"],
                "link": clusters[r["id"]]["rep"]["link"],
            }
            for r in ranked
        ],
    }
    (out_dir / f"{slug}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "lede": lede,
        "count": len(ranked),
        "updated_label": to_local(now).strftime("%m/%d %H:%M"),
    }


def main():
    ap = argparse.ArgumentParser(description="產生靜態新聞簡報網站")
    ap.add_argument("--out", default="public", help="輸出目錄（預設 public）")
    ap.add_argument("--config", default=str(CONFIG), help="主題設定檔")
    ap.add_argument("--only", nargs="+", metavar="SLUG", help="只重建指定主題")
    ap.add_argument("--no-cache", action="store_true", help="不使用 RSS 快取")
    ap.add_argument("--no-resolve", action="store_true",
                    help="不還原 Google 轉址連結（省時間，反正目前還原不了）")
    ap.add_argument("--retries", type=int, default=1,
                    help="單一主題失敗時的重試次數（預設 1）")
    ap.add_argument("--dry-run", action="store_true", help="只列出計畫，不呼叫 API")
    args = ap.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        sys.exit(f"找不到設定檔 {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))

    topics = config["topics"]
    if args.only:
        wanted = set(args.only)
        topics = [t for t in topics if t["slug"] in wanted]
        unknown = wanted - {t["slug"] for t in config["topics"]}
        if unknown:
            sys.exit(f"設定檔裡沒有這些主題：{'、'.join(sorted(unknown))}")

    out_dir = Path(args.out)

    if args.dry_run:
        print(f"輸出目錄：{out_dir.resolve()}")
        print(f"要產生 {len(topics)} 個主題：")
        for t in topics:
            print(f"  {t['slug']}.html  <- {t['question']}")
        print("  index.html")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    # 讀回上次的狀態，讓失敗的主題在索引頁保留舊資訊
    state_file = out_dir / ".build-state.json"
    try:
        states = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        states = {}

    failures = []
    for n, topic in enumerate(topics, 1):
        slug = topic["slug"]
        print(f"\n[{n}/{len(topics)}] {topic['label']}（{slug}）", file=sys.stderr)
        try:
            states[slug] = build_topic(topic, out_dir, not args.no_cache,
                                       resolve=not args.no_resolve,
                                       retries=args.retries)
            print(f"  完成：{states[slug]['count']} 則", file=sys.stderr)
        except Exception as e:
            failures.append((slug, e))
            print(f"  失敗：{e}", file=sys.stderr)
            traceback.print_exc(limit=2, file=sys.stderr)
            # 保留舊狀態但標記為失敗，索引頁會顯示提示
            previous = states.get(slug, {})
            previous["ok"] = False
            states[slug] = previous

    # static/ 裡的檔案原樣複製過去（例如 about.html）。
    # public/ 每次都是重新產生的，手寫的頁面放這裡才不會被洗掉。
    static_dir = Path("static")
    if static_dir.is_dir():
        copied = 0
        for f in static_dir.iterdir():
            if f.is_file():
                shutil.copy2(f, out_dir / f.name)
                copied += 1
        if copied:
            print(f"\n複製 {copied} 個靜態檔案", file=sys.stderr)

    # 自訂網域：GitHub Pages 靠發布內容裡的 CNAME 檔記住網域。
    # public/ 每次都是重新產生的，不寫這個檔的話每次部署都會掉回
    # 預設的 xxx.github.io 網址。
    domain = config.get("custom_domain", "").strip()
    if domain:
        (out_dir / "CNAME").write_text(domain + "\n", encoding="utf-8")
        print(f"\n自訂網域：{domain}", file=sys.stderr)

    index = render_index(config, states, out_dir)
    state_file.write_text(json.dumps(states, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    print(f"\n索引頁：{index.resolve()}", file=sys.stderr)
    ok = len(topics) - len(failures)
    print(f"成功 {ok}/{len(topics)} 個主題", file=sys.stderr)

    if failures:
        print("失敗的主題：", file=sys.stderr)
        for slug, e in failures:
            print(f"  {slug}: {e}", file=sys.stderr)
        # 全部失敗才視為建置失敗；部分失敗仍產出可用的網站
        if len(failures) == len(topics):
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        import sys as _sys
        print("\n已中斷。", file=_sys.stderr)
        raise SystemExit(130)
