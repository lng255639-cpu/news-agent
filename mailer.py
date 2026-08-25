"""
寄信模組
========
設定放在 .env：

    # Gmail（推薦）
    SMTP_PRESET=gmail
    SMTP_USER=你的帳號@gmail.com
    SMTP_PASS=應用程式密碼（16 碼，不是你的登入密碼）
    MAIL_TO=收件人@example.com

    # 或手動指定任意 SMTP 伺服器
    SMTP_HOST=smtp.example.com
    SMTP_PORT=587
    SMTP_USER=...
    SMTP_PASS=...
    MAIL_TO=...

測試：
    python mailer.py            寄一封測試信
    python mailer.py check      只檢查設定，不寄信
"""

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate

# llm.py 已經會載入 .env，借用它避免重複實作
try:
    from llm import _load_dotenv
    _load_dotenv()
except Exception:
    pass


# 常見服務商的 SMTP 設定。填 SMTP_PRESET 就不用自己查主機和埠號。
PRESETS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "mode": "starttls"},
    "outlook": {"host": "smtp-mail.outlook.com", "port": 587, "mode": "starttls"},
    "yahoo": {"host": "smtp.mail.yahoo.com", "port": 587, "mode": "starttls"},
    "icloud": {"host": "smtp.mail.me.com", "port": 587, "mode": "starttls"},
    "zoho": {"host": "smtp.zoho.com", "port": 587, "mode": "starttls"},
}


class MailConfigError(RuntimeError):
    pass


def config():
    preset_name = os.environ.get("SMTP_PRESET", "").strip().lower()
    preset = PRESETS.get(preset_name, {})

    host = os.environ.get("SMTP_HOST") or preset.get("host")
    port = os.environ.get("SMTP_PORT") or preset.get("port")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO") or user
    sender = os.environ.get("MAIL_FROM") or user
    name = os.environ.get("MAIL_FROM_NAME", "News Agent")

    missing = [label for label, value in
               (("SMTP_HOST 或 SMTP_PRESET", host), ("SMTP_USER", user),
                ("SMTP_PASS", password)) if not value]
    if missing:
        raise MailConfigError(
            "缺少設定：" + "、".join(missing) +
            "\n  在 .env 補上，或參考 .env.example 的寄信段落"
        )

    if preset_name and preset_name not in PRESETS:
        raise MailConfigError(
            f"不認得的 SMTP_PRESET {preset_name!r}，"
            f"可選：{'、'.join(PRESETS)}（或直接設 SMTP_HOST）")

    port = int(port or 587)
    mode = os.environ.get("SMTP_MODE") or preset.get("mode")
    if not mode:
        mode = "ssl" if port == 465 else "starttls"

    # 收件人可以用逗號分隔多個
    recipients = [x.strip() for x in str(to).replace(";", ",").split(",")
                  if x.strip()]

    return {
        "host": host, "port": port, "mode": mode,
        "user": user, "password": password,
        "sender": sender, "name": name, "recipients": recipients,
    }


def send_html(subject, html, text=None, cfg=None):
    """寄出一封 HTML 信件。成功回傳收件人清單，失敗拋出 RuntimeError。"""
    cfg = cfg or config()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["name"], cfg["sender"]))
    msg["To"] = ", ".join(cfg["recipients"])
    msg["Date"] = formatdate(localtime=True)

    # 純文字版是給不顯示 HTML 的環境看的，也能降低被當成垃圾信的機率
    msg.set_content(text or "這封信需要支援 HTML 的郵件軟體才能正常顯示。")
    msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    try:
        if cfg["mode"] == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"],
                                      context=context, timeout=30)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)

        with server:
            if cfg["mode"] == "starttls":
                server.starttls(context=context)
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)

    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            f"登入失敗（{e.smtp_code}）。常見原因：\n"
            "  · Gmail / Yahoo / iCloud 必須用「應用程式密碼」，不能用登入密碼\n"
            "  · 應用程式密碼要先在帳號安全設定裡開啟兩步驟驗證才能產生\n"
            "  · 密碼貼上時把空格一起貼進去了（16 碼中間的空格要刪掉）")
    except smtplib.SMTPRecipientsRefused:
        raise RuntimeError(f"收件人被拒絕：{cfg['recipients']}")
    except (smtplib.SMTPConnectError, OSError) as e:
        raise RuntimeError(
            f"連不上 {cfg['host']}:{cfg['port']}\n"
            f"  · 檢查主機和埠號是否正確（587 用 starttls，465 用 ssl）\n"
            f"  · 有些網路會擋 SMTP 連出\n"
            f"  · 原始錯誤：{str(e)[:150]}")
    except smtplib.SMTPException as e:
        raise RuntimeError(f"寄信失敗：{str(e)[:200]}")

    return cfg["recipients"]


def _mask(value):
    if not value:
        return "(未設定)"
    return value[:2] + "*" * max(0, len(value) - 4) + value[-2:]


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "send"

    try:
        cfg = config()
    except MailConfigError as e:
        sys.exit(f"設定有問題：{e}")

    print(f"伺服器　：{cfg['host']}:{cfg['port']}（{cfg['mode']}）")
    print(f"寄件帳號：{cfg['user']}")
    print(f"密碼　　：{_mask(cfg['password'])}")
    print(f"收件人　：{', '.join(cfg['recipients'])}")

    if " " in (cfg["password"] or ""):
        print("\n注意：密碼裡有空格。Gmail 顯示的應用程式密碼會分成四組，"
              "貼進 .env 時要把空格刪掉。")

    if command == "check":
        print("\n（只檢查設定，沒有寄信）")
        sys.exit(0)

    print("\n寄出測試信…")
    html = """<div style="font-family:system-ui,sans-serif;padding:1rem">
      <h2 style="color:#0b6e6e;margin:0 0 .5rem">設定成功</h2>
      <p>如果你看到這封信，代表 news_agent 的寄信設定沒問題了。</p>
    </div>"""
    try:
        sent = send_html("News Agent 測試信", html,
                         "設定成功：如果你看到這封信，代表寄信設定沒問題了。", cfg)
    except RuntimeError as e:
        sys.exit(f"\n{e}")
    print(f"已寄給：{', '.join(sent)}")
    print("去收件匣看看（也檢查一下垃圾信匣）。")
