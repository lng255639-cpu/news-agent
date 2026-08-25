"""
連結還原診斷工具
================
用法：
    python debug_link.py                    # 自動從快取抓一個網址來測
    python debug_link.py "https://news.google.com/rss/articles/CBMi..."

會把 Google 中介頁面裡的線索列出來，用來判斷該用什麼策略擷取真實網址。
不會顯示完整頁面（太長），只列出關鍵特徵。
"""

import json
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def pick_sample_url():
    """從 RSS 快取裡隨便挑一個 Google 連結來測。"""
    cache = Path(".cache")
    if not cache.exists():
        return None
    for f in cache.glob("*.json"):
        if f.name == "links.json":
            continue
        try:
            entries = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in entries:
            if isinstance(e, dict) and "news.google.com" in e.get("link", ""):
                return e["link"]
    return None


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else pick_sample_url()
    if not url:
        sys.exit("找不到可測試的網址。先跑一次 news_agent.py，或直接把網址當參數傳進來。")

    print("=" * 64)
    print(f"測試網址：{url[:80]}…")
    print("=" * 64)

    try:
        resp = requests.get(url, headers={"User-Agent": UA},
                            timeout=15, allow_redirects=True)
    except Exception as e:
        sys.exit(f"請求失敗：{e}")

    html = resp.text
    print(f"\nHTTP 狀態　：{resp.status_code}")
    print(f"最終網址　：{resp.url[:100]}")
    print(f"轉址次數　：{len(resp.history)}")
    if resp.history:
        for h in resp.history:
            print(f"    {h.status_code} -> {h.headers.get('Location', '')[:80]}")
    print(f"頁面大小　：{len(html):,} 字元")
    print(f"Content-Type：{resp.headers.get('Content-Type', '')}")

    # --- 關鍵屬性 ---
    print("\n--- Google 用來標示目標的屬性 ---")
    markers = {
        "data-n-au": r'data-n-au="([^"]{10,200})"',
        "data-n-a-id": r'data-n-a-id="([^"]{5,100})"',
        "data-n-a-sg": r'data-n-a-sg="([^"]{5,100})"',
        "jsdata": r'jsdata="([^"]{10,120})"',
        "meta refresh": r'http-equiv=["\']refresh["\'][^>]{0,200}',
        "canonical": r'<link[^>]+rel=["\']canonical["\'][^>]+href="([^"]+)"',
    }
    for name, pattern in markers.items():
        found = re.findall(pattern, html, re.I)
        if found:
            print(f"  {name:14} 找到 {len(found)} 個 -> {str(found[0])[:70]}")
        else:
            print(f"  {name:14} 無")

    # --- 頁面上的外部網域 ---
    print("\n--- 頁面上出現的外部網域（前 12 名）---")
    hosts = Counter()
    samples = {}
    for link in re.findall(r'https?://[^\s"\'<>\\]{10,300}', html):
        host = urllib.parse.urlparse(link).netloc.lower()
        if not host:
            continue
        hosts[host] += 1
        samples.setdefault(host, link)

    google_like = ("google", "gstatic", "ggpht", "youtube", "doubleclick",
                   "schema.org", "w3.org")
    for host, count in hosts.most_common(12):
        tag = "  (Google 自家)" if any(g in host for g in google_like) else ""
        print(f"  {count:4}x  {host}{tag}")
        if not tag:
            print(f"          範例：{samples[host][:90]}")

    external = [h for h in hosts if not any(g in h for g in google_like)]
    print("\n" + "=" * 64)
    if external:
        print(f"  頁面裡有 {len(external)} 個外部網域，代表真實網址就在頁面上，")
        print("  只是我的擷取規則沒抓到。把上面的「範例」那幾行貼給我。")
    else:
        print("  頁面上沒有任何外部網域 —— 真實網址是靠 JavaScript 動態取得的，")
        print("  純 HTTP 請求拿不到。這種情況下 Google 轉址連結是唯一選擇。")
    print("=" * 64)

    # --- 是否為 JS 導向的頁面 ---
    if len(html) < 3000:
        print(f"\n注意：頁面只有 {len(html)} 字元，很可能是 JS 跳轉的骨架頁。")
        print("完整內容如下：\n")
        print(html[:2000])


if __name__ == "__main__":
    main()
