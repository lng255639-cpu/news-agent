"""
LLM 抽象層 — 一份程式碼，任意切換供應商 / 中轉站
===============================================
用法：
    from llm import llm, llm_json
    text = llm("把這段話翻成英文：你好")

指令：
    python llm.py            測試目前設定能不能通
    python llm.py models     列出可用模型

設定放在 .env 或環境變數：
    LLM_PROVIDER   供應商代號（見下方 PROVIDERS）
    LLM_MODEL      覆寫預設模型
    LLM_BASE_URL   覆寫 API 網址（自架 / 中轉站用）
    LLM_API_KEY    通用金鑰（custom 供應商用）
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests


# ---------------------------------------------------------------
# 自動載入同目錄下的 .env（不需要額外套件）
# ---------------------------------------------------------------

def _load_dotenv():
    for path in (Path(".env"), Path(__file__).parent / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        break


_load_dotenv()


# ---------------------------------------------------------------
# 供應商設定
#
# base 是「到 /v1 為止」的網址，端點由程式組裝：
#   OpenAI 相容 → {base}/chat/completions 和 {base}/models
#   Anthropic   → {base}/messages         和 {base}/models
#
# 除了 anthropic 之外全部走 OpenAI 相容格式。
# model 名稱各家改版都很快，跑 `python llm.py models` 查最準。
# ---------------------------------------------------------------

PROVIDERS = {
    # ---- 直連原廠 ----
    "anthropic": {
        "base": "https://api.anthropic.com/v1",
        "key_env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-5",
        "format": "anthropic",
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
        "format": "openai",
    },
    "gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "model": "gemini-2.0-flash",
        "format": "openai",
    },
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
        "format": "openai",
    },
    "groq": {
        "base": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
        "format": "openai",
    },

    # ---- 中轉 / 聚合 ----
    "openrouter": {
        # 一把 key 通吃數百個模型。model 名稱是 "廠商/模型" 格式。
        "base": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "model": "google/gemini-2.0-flash-001",
        "format": "openai",
    },
    "litellm": {
        # 自架的 LiteLLM Proxy。網址和模型別名都是你自己定的，
        # 所以務必設 LLM_BASE_URL 和 LLM_MODEL。
        "base": "http://localhost:4000/v1",
        "key_env": "LITELLM_API_KEY",
        "model": None,
        "format": "openai",
    },
    "custom": {
        # 任何 OpenAI 相容端點。完全靠環境變數驅動，不用改程式碼。
        "base": None,
        "key_env": "LLM_API_KEY",
        "model": None,
        "format": "openai",
    },

    # ---- 本地 ----
    "ollama": {
        "base": "http://localhost:11434/v1",
        "key_env": None,
        "model": "qwen2.5:14b",
        "format": "openai",
    },
}


def _config():
    name = os.environ.get("LLM_PROVIDER", "anthropic").lower().strip()
    if name not in PROVIDERS:
        raise ValueError(
            f"不認得的供應商 {name!r}，可選：{', '.join(PROVIDERS)}"
        )

    cfg = dict(PROVIDERS[name])
    cfg["name"] = name

    # base URL：環境變數優先，方便指向自架或中轉站
    base = os.environ.get("LLM_BASE_URL") or cfg["base"]
    if not base:
        raise RuntimeError(
            f"供應商 {name!r} 沒有預設網址，請設定 LLM_BASE_URL\n"
            f"  例如：LLM_BASE_URL=https://你的中轉站.com/v1"
        )
    cfg["base"] = base.rstrip("/")

    # 模型
    cfg["model"] = os.environ.get("LLM_MODEL") or cfg["model"]
    if not cfg["model"]:
        raise RuntimeError(
            f"供應商 {name!r} 沒有預設模型，請設定 LLM_MODEL\n"
            f"  先跑 `python llm.py models` 看有哪些可選"
        )

    # 金鑰：優先用供應商專屬的，退而求其次用通用的 LLM_API_KEY
    if cfg["key_env"] is None:
        cfg["key"] = None
    else:
        key = os.environ.get(cfg["key_env"]) or os.environ.get("LLM_API_KEY")
        if not key:
            raise RuntimeError(
                f"請設定環境變數 {cfg['key_env']}（或通用的 LLM_API_KEY）"
            )
        cfg["key"] = key

    return cfg


def _endpoint(cfg, kind):
    if kind == "models":
        return f"{cfg['base']}/models"
    if cfg["format"] == "anthropic":
        return f"{cfg['base']}/messages"
    return f"{cfg['base']}/chat/completions"


# ---------------------------------------------------------------
# 主要介面
# ---------------------------------------------------------------

def _timeout():
    try:
        return float(os.environ.get("LLM_TIMEOUT", "120"))
    except ValueError:
        return 120.0


def _debug_on():
    return os.environ.get("LLM_DEBUG", "").strip() not in ("", "0", "false")


def _debug(msg):
    if _debug_on():
        print(f"  [debug] {msg}", file=sys.stderr, flush=True)


def llm(prompt, system=None, max_tokens=4000, json_mode=False):
    """送一段 prompt，回傳純文字。各家的格式差異都藏在這裡面。"""
    cfg = _config()
    started = time.time()

    _debug(f"POST {_endpoint(cfg, 'chat')}")
    _debug(f"model={cfg['model']} json_mode={json_mode} "
           f"prompt={len(prompt)} 字 timeout={_timeout()}s")

    def call(stream, timeout=None):
        if cfg["format"] == "anthropic":
            return _call_anthropic(cfg, prompt, system, max_tokens, stream,
                                   timeout)
        return _call_openai(cfg, prompt, system, max_tokens, json_mode,
                            stream, timeout)

    pref = _stream_pref()
    use_stream = pref if pref is not None else False
    result = None
    last_error = None

    # 中轉站偶爾會卡住或斷線。重試幾次比讓整個任務失敗划算。
    # 未指定串流模式時，第一次逾時就改用串流，
    # 成功後記住供這個執行階段的後續呼叫使用。
    for attempt in range(3):
        try:
            # 沒指定模式時，第一次非串流只是探測，短逾時就好 ——
            # 等滿 120 秒才發現這站不吃非串流，太浪費了
            probing = pref is None and not use_stream
            result = call(use_stream,
                          timeout=min(_timeout(), 25) if probing else None)

            # 有些中轉站對大 prompt 的串流請求會回 200 但一個字都不吐。
            # 這種要當成失敗處理，改走非串流通常就通了。
            if use_stream and not (result or "").strip():
                raise RuntimeError("串流回傳空白內容")

            if pref is None and use_stream:
                os.environ["LLM_STREAM"] = "1"
            break
        except RuntimeError as e:
            message = str(e)
            transient = ("沒有回應" in message or "中斷了" in message
                         or "回傳空白" in message or "429" in message)
            if not transient or attempt == 2:
                raise
            last_error = e

            if "回傳空白" in message and use_stream:
                # 這個請求串流不通，退回非串流（即使使用者指定了串流）
                _debug("串流回傳空白，改用非串流重試")
                use_stream = False
            elif pref is None and not use_stream:
                _debug("非串流逾時，改用串流重試")
                use_stream = True
            else:
                wait = 3 * (attempt + 1)
                _debug(f"第 {attempt + 1} 次失敗，{wait} 秒後重試")
                time.sleep(wait)

    if result is None:
        raise last_error

    _debug(f"耗時 {time.time() - started:.1f}s，回應 {len(result)} 字")
    if _debug_on():
        preview = result[:400].replace("\n", "\\n")
        _debug(f"回應開頭：{preview}")

    return result


def _call_anthropic(cfg, prompt, system, max_tokens, stream=False, timeout=None):
    body = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system          # Anthropic 的 system 是頂層參數
    if stream:
        body["stream"] = True

    headers = {
        "x-api-key": cfg["key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    resp = _post(_endpoint(cfg, "chat"), headers=headers, body=body,
                 stream=stream, timeout=timeout)
    _raise_for_status(resp)

    if stream:
        return _read_sse(resp, _read_sse_anthropic)
    return "".join(b.get("text", "") for b in resp.json()["content"])


def _call_openai(cfg, prompt, system, max_tokens, json_mode, stream=False, timeout=None):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if stream:
        body["stream"] = True

    resp = _post(_endpoint(cfg, "chat"), headers=_headers(cfg), body=body,
                 stream=stream, timeout=timeout)
    _raise_for_status(resp)

    if stream:
        return _read_sse(resp, _read_sse_openai)

    data = resp.json()
    if "choices" not in data:
        # 中轉站出錯時常常回 200 但內容是錯誤訊息
        raise RuntimeError(f"回應格式不對：{json.dumps(data, ensure_ascii=False)[:400]}")
    return data["choices"][0]["message"]["content"]


def _headers(cfg):
    headers = {"content-type": "application/json"}
    if cfg["key"]:
        headers["Authorization"] = f"Bearer {cfg['key']}"

    if cfg["name"] == "openrouter":
        # 選填。填了會出現在 OpenRouter 的公開排行榜上。
        site = os.environ.get("OPENROUTER_SITE_URL")
        app = os.environ.get("OPENROUTER_APP_NAME")
        if site:
            headers["HTTP-Referer"] = site
        if app:
            headers["X-Title"] = app

    return headers


def _stream_pref():
    """LLM_STREAM: 1/on 強制串流，0/off 強制非串流，未設定則自動判斷。"""
    v = os.environ.get("LLM_STREAM", "").strip().lower()
    if v in ("1", "true", "on", "yes"):
        return True
    if v in ("0", "false", "off", "no"):
        return False
    return None


class _Progress:
    """串流期間在同一行顯示收到多少字。

    這是用來分辨兩種「卡住」的：一種是慢但持續在吐字，
    另一種是完全沒有東西進來。看起來一樣，處理方式完全不同。
    """

    def __init__(self):
        self.start = time.time()
        self.last_draw = 0.0
        self.chars = 0

    def __call__(self, chars):
        self.chars = chars
        now = time.time()
        if now - self.last_draw < 0.5:
            return
        self.last_draw = now
        print(f"\r      串流中… {chars} 字 / {now - self.start:.0f}s",
              end="", file=sys.stderr, flush=True)

    def finish(self, total):
        elapsed = time.time() - self.start
        if self.chars == 0 and total is None:
            print(f"\r      串流中斷，完全沒收到內容（{elapsed:.0f}s）    ",
                  file=sys.stderr, flush=True)
        elif total is None:
            print(f"\r      串流中斷，已收到 {self.chars} 字（{elapsed:.0f}s）    ",
                  file=sys.stderr, flush=True)
        elif total == 0:
            print(f"\r      串流結束但沒有內容（{elapsed:.0f}s）        ",
                  file=sys.stderr, flush=True)
        else:
            rate = total / elapsed if elapsed else 0
            print(f"\r      收到 {total} 字，耗時 {elapsed:.0f}s"
                  f"（{rate:.0f} 字/秒）        ", file=sys.stderr, flush=True)


def _progress_reporter():
    if os.environ.get("LLM_PROGRESS", "").strip().lower() in ("0", "off", "false"):
        return None
    return _Progress()


def _read_sse(resp, parser):
    """包住串流讀取。

    連線讀到一半斷掉時，例外是在 _post 回傳之後才發生的，
    不在 _post 的 try 範圍內 —— 沒接住就會整包 traceback 噴出來。

    另外必須強制 UTF-8：SSE 規格本來就規定 UTF-8，但回應標頭常常
    沒寫 charset，requests 便依 RFC 對 text/* 預設 ISO-8859-1，
    中文會整片變成亂碼。
    """
    resp.encoding = "utf-8"
    progress = _progress_reporter()
    try:
        result = parser(resp, progress) if progress else parser(resp)
        if progress:
            progress.finish(len(result))
        return result
    except (requests.exceptions.RequestException, OSError) as e:
        if progress:
            progress.finish(None)
        raise RuntimeError(
            f"串流讀到一半中斷了：{str(e)[:150]}"
            f"\n  → 中轉站不穩，稍後再試或換個模型")


def _read_sse_openai(resp, on_progress=None):
    parts = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        done = False
        for choice in obj.get("choices", []):
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
            # 有些中轉站不送 [DONE]，收完內容還把連線掛著。
            # 看到 finish_reason 就自己收工，別傻等到逾時。
            if choice.get("finish_reason"):
                done = True
        if on_progress:
            on_progress(sum(len(x) for x in parts))
        if done:
            break
    return "".join(parts)


def _read_sse_anthropic(resp, on_progress=None):
    parts = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        kind = obj.get("type")
        if kind == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("text"):
                parts.append(delta["text"])
                if on_progress:
                    on_progress(sum(len(x) for x in parts))
        elif kind == "message_stop":
            break        # 收完了就走，別等中轉站關連線
    return "".join(parts)


def _post(url, headers, body, stream=False, timeout=None):
    """統一送出請求，把連線類錯誤翻成看得懂的訊息。"""
    try:
        return requests.post(url, headers=headers, json=body,
                             timeout=timeout or _timeout(), stream=stream)
    except requests.exceptions.ReadTimeout:
        raise RuntimeError(
            f"等了 {timeout or _timeout():.0f} 秒，中轉站沒有回應。"
            f"\n  → 可能是中轉站太慢、模型排隊中，或這個模型在該站不可用"
            f"\n  → 很多中轉站只支援串流，試試 LLM_STREAM=1"
            f"\n  → 或設 LLM_TIMEOUT=30 早點失敗、LLM_DEBUG=1 看細節")
    except requests.exceptions.ConnectTimeout:
        raise RuntimeError(
            f"連不上 {url}（逾時）"
            f"\n  → 檢查 LLM_BASE_URL 是否正確、網路是否通")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"連不上 {url}"
            f"\n  → 檢查網址、防火牆、或中轉站是否還活著"
            f"\n  → 原始錯誤：{str(e)[:200]}")


def _friendly_timeout_hint():
    return (f"\n  → 等了 {_timeout():.0f} 秒沒回應。可能是中轉站太慢或沒回。"
            f"\n  → 設 LLM_TIMEOUT=30 讓它早點失敗，"
            f"或設 LLM_DEBUG=1 看送出去的內容")


def _raise_for_status(resp):
    if resp.status_code >= 400:
        hint = ""
        if resp.status_code == 401:
            hint = "\n  → API key 不對，或中轉站要求的 key 格式不同"
        elif resp.status_code == 404:
            hint = ("\n  → 網址或模型名稱不對。"
                    "檢查 LLM_BASE_URL 有沒有包含 /v1，"
                    "並跑 `python llm.py models` 確認模型名稱")
        elif resp.status_code == 402:
            hint = "\n  → 餘額不足"
        elif resp.status_code == 429:
            hint = "\n  → 被限流，稍後再試"
        raise RuntimeError(f"API 錯誤 {resp.status_code}: {resp.text[:400]}{hint}")


# ---------------------------------------------------------------
# JSON 解析
#
# 便宜的小模型很愛在 JSON 前後多講話或包 markdown 圍欄，
# 所以不能直接 json.loads，要先清乾淨。
# ---------------------------------------------------------------

def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError(f"無法從回應中解析 JSON：\n{text[:300]}")


JSON_SYSTEM = (
    "你是一個 JSON API。無論前面有什麼指示，你的輸出必須是且只能是"
    "一個合法的 JSON 物件，完全符合使用者指定的欄位名稱。"
    "不要輸出任何說明文字、前言、結語或 markdown 圍欄。"
)


def llm_json(prompt, system=None, max_tokens=4000, retries=2):
    """要 JSON 就用這個。

    會先嘗試用 response_format 強制模型輸出合法 JSON；
    供應商不支援就自動退回一般模式，再靠 parse_json 清乾淨。

    預設帶一段 system 指令，用來壓過中轉站可能注入的 system prompt。
    """
    cfg = _config()
    use_json_mode = cfg["format"] == "openai"
    if os.environ.get("LLM_JSON_MODE", "").strip() in ("0", "false", "off"):
        use_json_mode = False

    system = system or JSON_SYSTEM
    last_error = None
    current = prompt

    for _ in range(retries + 1):
        try:
            raw = llm(current, system=system, max_tokens=max_tokens,
                      json_mode=use_json_mode)
        except RuntimeError as e:
            # 不支援 response_format 的供應商會回 400，關掉重試一次
            if use_json_mode and ("response_format" in str(e) or "400" in str(e)):
                use_json_mode = False
                raw = llm(current, system=system, max_tokens=max_tokens)
            else:
                raise

        try:
            return parse_json(raw)
        except ValueError as e:
            last_error = e
            current = (
                f"{prompt}\n\n"
                f"（上次你的回應無法解析成 JSON。請只輸出純 JSON，"
                f"不要有任何說明文字或 markdown 圍欄。）"
            )

    raise last_error


# ---------------------------------------------------------------
# 查詢可用模型
# 直接問供應商最準，比查文件或憑記憶填可靠得多。
# ---------------------------------------------------------------

def list_models():
    cfg = _config()

    if cfg["format"] == "anthropic":
        headers = {"x-api-key": cfg["key"], "anthropic-version": "2023-06-01"}
    else:
        headers = _headers(cfg)

    resp = requests.get(_endpoint(cfg, "models"), headers=headers, timeout=30)
    _raise_for_status(resp)

    payload = resp.json()
    rows = payload.get("data") or payload.get("models") or []

    names = []
    for m in rows:
        if isinstance(m, dict):
            names.append(m.get("id") or m.get("name") or str(m))
        else:
            names.append(str(m))
    return sorted(set(names))


# ---------------------------------------------------------------
# 探測中轉站到底吃哪一種請求格式
#
# 症狀：GET /v1/models 成功但 POST 卡住 → 多半是格式選錯，
# 打到一個沒人接的路徑上。
# ---------------------------------------------------------------

def probe():
    cfg = _config()
    base = cfg["base"]
    timeout = min(_timeout(), 20)
    tiny = {"model": cfg["model"], "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}]}

    anthropic_headers = {
        "x-api-key": cfg["key"] or "",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    openai_headers = {
        "Authorization": f"Bearer {cfg['key'] or ''}",
        "content-type": "application/json",
    }

    print(f"  基準測試 GET {base}/models …", end=" ", flush=True)
    try:
        r = requests.get(base + "/models", headers=openai_headers,
                         timeout=timeout)
        print(f"HTTP {r.status_code}")
    except Exception as e:
        print(f"失敗（{str(e)[:80]}）")

    attempts = []
    for fmt, url, headers in (
        ("anthropic", f"{base}/messages", anthropic_headers),
        ("openai", f"{base}/chat/completions", openai_headers),
    ):
        for stream in (False, True):
            attempts.append((fmt, stream, url, headers))

    results = []
    for fmt, stream, url, headers in attempts:
        tag = f"{fmt} {'串流' if stream else '非串流'}"
        print(f"\n  測試 {tag} → POST {url}")
        body = dict(tiny)
        if stream:
            body["stream"] = True

        started = time.time()
        try:
            resp = requests.post(url, headers=headers, json=body,
                                 timeout=timeout, stream=stream)
            elapsed = time.time() - started
            if stream and resp.status_code == 200:
                text = _read_sse_anthropic(resp) if fmt == "anthropic" \
                    else _read_sse_openai(resp)
                preview = (text or "(串流空白)")[:120]
            else:
                preview = resp.text[:160].replace("\n", " ")
            print(f"    HTTP {resp.status_code}（{elapsed:.1f}s）")
            print(f"    {preview}")
            results.append((fmt, stream, resp.status_code))
        except requests.exceptions.Timeout:
            print(f"    逾時（{timeout:.0f}s）")
            results.append((fmt, stream, "timeout"))
        except Exception as e:
            print(f"    失敗：{str(e)[:120]}")
            results.append((fmt, stream, "error"))

    print("\n" + "-" * 60)
    ok = [r for r in results if r[2] == 200]
    alive = [r for r in results
             if isinstance(r[2], int) and r[2] in (400, 401, 403, 429)]

    if ok:
        fmt, stream, _ = ok[0]
        print(f"  可用組合：{fmt} 格式 + {'串流' if stream else '非串流'}")
        print(f"\n  請在 .env 設定：")
        print(f"    LLM_PROVIDER={'anthropic' if fmt == 'anthropic' else 'custom'}")
        print(f"    LLM_BASE_URL={base}")
        print(f"    LLM_MODEL={cfg['model']}")
        if stream:
            print(f"    LLM_STREAM=1")
        if any(r[0] == fmt and not r[1] and r[2] == "timeout" for r in results):
            print(f"\n  （這個站只吃串流，非串流會掛住 —— LLM_STREAM=1 一定要設）")
    elif alive:
        fmt, stream, status = alive[0]
        print(f"  {fmt} 格式的路徑有回應（HTTP {status}），但被拒絕：")
        if status in (401, 403):
            print("    · 金鑰不對，或沒有這個模型的權限")
        elif status == 400:
            print("    · 參數有問題，試試換模型或 LLM_JSON_MODE=0")
        elif status == 429:
            print("    · 被限流，稍後再試")
    else:
        print("  四種組合都不通。可能原因：")
        print("    · 網址少了或多了 /v1（GET models 通不代表 POST 路徑相同）")
        print("    · 中轉站要求特定 header（有些要 User-Agent 或自訂欄位）")
        print("    · 金鑰沒有這個模型的權限，換個模型再測一次")
        for fmt, stream, status in results:
            print(f"    {fmt} {'串流' if stream else '非串流'}: {status}")
    print("-" * 60)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "test"

    try:
        cfg = _config()
    except Exception as e:
        sys.exit(f"設定有問題：{e}")

    print(f"供應商：{cfg['name']}")
    print(f"網址　：{_endpoint(cfg, 'chat')}")
    print(f"模型　：{cfg['model']}\n")

    if command == "probe":
        probe()
    elif command == "models":
        try:
            names = list_models()
        except Exception as e:
            sys.exit(f"查詢模型清單失敗：{e}")

        print(f"可用模型（{len(names)} 個）：")
        for n in names:
            mark = "  ← 目前使用" if n == cfg["model"] else ""
            print(f"  {n}{mark}")
        print("\n挑一個填進 .env：  LLM_MODEL=模型名稱")
    else:
        try:
            print(llm("用一句話自我介紹，並說出你是哪個模型。"))
        except Exception as e:
            sys.exit(f"呼叫失敗：{e}")
