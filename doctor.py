"""
設定診斷工具
============
執行：python doctor.py

會逐項檢查為什麼 .env 沒被讀到，並指出該怎麼修。
不會顯示完整的 API key（只顯示前後幾碼）。
"""

import os
import sys
from pathlib import Path

OK, BAD, WARN = "[ OK ]", "[ 問題 ]", "[ 注意 ]"
problems = []


def mask(value):
    if not value:
        return "(空的)"
    if len(value) <= 10:
        return value[:2] + "*" * (len(value) - 2)
    return f"{value[:6]}...{value[-4:]}（長度 {len(value)}）"


print("=" * 60)
print("  設定診斷")
print("=" * 60)

# --- 1. 位置 ---
cwd = Path.cwd()
script_dir = Path(__file__).parent.resolve()
print(f"\n目前工作目錄：{cwd}")
print(f"程式所在目錄：{script_dir}")
if cwd != script_dir:
    print(f"{WARN} 你不是在程式的目錄下執行。")
    print("       .env 要放在「工作目錄」或「程式目錄」其中之一。")

# --- 2. llm.py 在不在、是不是新版 ---
print("\n--- 檢查 llm.py ---")
llm_path = script_dir / "llm.py"
if not llm_path.exists():
    print(f"{BAD} 找不到 llm.py（應該在 {script_dir}）")
    problems.append("llm.py 不存在")
else:
    source = llm_path.read_text(encoding="utf-8")
    if "_load_dotenv" in source:
        print(f"{OK} llm.py 是支援 .env 的版本")
    else:
        print(f"{BAD} 你的 llm.py 是舊版，不會自動讀 .env")
        print("       → 請重新下載最新版的 llm.py")
        problems.append("llm.py 是舊版")

# --- 3. .env 找得到嗎 ---
print("\n--- 檢查 .env ---")
candidates = [cwd / ".env", script_dir / ".env"]
found = next((p for p in candidates if p.exists()), None)

if not found:
    print(f"{BAD} 找不到 .env")
    for p in candidates:
        print(f"       找過：{p}")
    problems.append(".env 不存在")

    # Windows 記事本最愛偷加 .txt
    strays = []
    for folder in {cwd, script_dir}:
        strays += list(folder.glob(".env.*")) + list(folder.glob("env*"))
    strays = [p for p in strays if p.name != ".env.example"]
    if strays:
        print(f"\n{WARN} 但我發現這些長得很像的檔案：")
        for p in strays:
            print(f"       {p.name}")
        print("       → 檔名必須正好是 .env（沒有 .txt、沒有其他前後綴）")
        print("       → Windows 記事本存檔時，「存檔類型」要選「所有檔案」")
else:
    print(f"{OK} 找到：{found}")

    raw = found.read_text(encoding="utf-8-sig")  # 順便吃掉 BOM
    parsed, bad_lines = {}, []

    for n, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            bad_lines.append((n, line))
            continue
        key, _, value = stripped.partition("=")
        parsed[key.strip()] = value.strip().strip("'\"")

    print(f"\n     內容（{len(parsed)} 組有效設定）：")
    for k, v in parsed.items():
        shown = mask(v) if "KEY" in k.upper() or "TOKEN" in k.upper() else v
        print(f"       {k} = {shown}")

    if bad_lines:
        print(f"\n{WARN} 這些行沒有 '='，會被忽略：")
        for n, line in bad_lines:
            print(f"       第 {n} 行：{line!r}")

    # --- 關鍵檢查：有沒有 LLM_PROVIDER ---
    provider = parsed.get("LLM_PROVIDER")
    if not provider:
        print(f"\n{BAD} .env 裡沒有 LLM_PROVIDER 這一行")
        print("       這就是為什麼它退回預設的 anthropic。")
        similar = [k for k in parsed if "PROVIDER" in k.upper()]
        if similar:
            print(f"       → 你寫的是 {similar[0]}，拼字要正好是 LLM_PROVIDER")
        else:
            print("       → 在 .env 最上面加一行，例如：LLM_PROVIDER=groq")
        problems.append("缺少 LLM_PROVIDER")
    else:
        print(f"\n{OK} LLM_PROVIDER = {provider}")

        try:
            sys.path.insert(0, str(script_dir))
            from llm import PROVIDERS
        except Exception as e:
            print(f"{BAD} 無法載入 llm.py：{e}")
            PROVIDERS = {}
            problems.append("llm.py 匯入失敗")

        if PROVIDERS and provider.lower() not in PROVIDERS:
            print(f"{BAD} 「{provider}」不是認得的供應商")
            print(f"       可選：{', '.join(PROVIDERS)}")
            problems.append("供應商名稱錯誤")
        elif PROVIDERS:
            need = PROVIDERS[provider.lower()]["key_env"]
            if need is None:
                print(f"{OK} {provider} 不需要 API key")
            elif parsed.get(need):
                print(f"{OK} {need} 有填 = {mask(parsed[need])}")
            else:
                print(f"{BAD} 缺少 {need}")
                wrong = [k for k in parsed if k.endswith("_API_KEY")]
                if wrong:
                    print(f"       → 你填的是 {wrong[0]}，"
                          f"但 {provider} 需要的是 {need}")
                    print("       → 兩者要對得上：供應商和 key 是一組的")
                problems.append(f"缺少 {need}")

# --- 4. 真正的環境變數會蓋掉 .env ---
print("\n--- 檢查系統環境變數 ---")
shell_provider = os.environ.get("LLM_PROVIDER")
if shell_provider:
    print(f"{WARN} 系統環境變數已有 LLM_PROVIDER = {shell_provider}")
    print("       系統環境變數優先，會蓋掉 .env 裡的設定。")
    if found and parsed.get("LLM_PROVIDER") not in (None, shell_provider):
        print(f"       你的 .env 寫的是 {parsed['LLM_PROVIDER']}，但不會生效。")
        print("       → 重開一個終端機視窗，或 unset LLM_PROVIDER")
        problems.append("系統環境變數蓋掉 .env")
else:
    print(f"{OK} 系統環境變數沒有 LLM_PROVIDER，會用 .env 的值")

# --- 5. 最終結果 ---
print("\n--- 實際載入結果 ---")
try:
    sys.path.insert(0, str(script_dir))
    import llm
    llm._load_dotenv()
    cfg = llm._config()
    print(f"{OK} 供應商：{cfg['name']}")
    print(f"{OK} 模型　：{cfg['model']}")
    print(f"{OK} API key：{mask(cfg['key']) if cfg['key'] else '(不需要)'}")
except Exception as e:
    print(f"{BAD} {e}")

print("\n" + "=" * 60)
if problems:
    print(f"  找到 {len(problems)} 個問題：")
    for p in problems:
        print(f"    · {p}")
else:
    print("  設定看起來沒問題。接著跑：python llm.py")
print("=" * 60)
