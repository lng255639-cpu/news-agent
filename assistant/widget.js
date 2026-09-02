/*
 * 新聞AI小助手 —— 前端
 *
 * 用法（放在網站任何一頁的 </body> 前面）：
 *   <script src="https://你的後端網址/widget.js"
 *           data-name="AI小助手"
 *           data-starters="今天有什麼重要新聞？|那排小格子是什麼意思？|時間標籤怎麼看？"></script>
 *
 * 預設配色沿用 about 頁面的 --signal (#0b6e6e) 與 --rule (#d6dce4)，
 * 圓角也壓到 3px 跟新聞卡片一致，所以看起來會像網站本來就有的東西。
 *
 * 整個介面關在 Shadow DOM 裡，不會被網站的 CSS 影響，也不會影響它。
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var cfg = {
    api: (script.dataset.api || new URL(script.src).origin).replace(/\/$/, ""),
    name: script.dataset.name || "AI小助手",
    greeting: script.dataset.greeting ||
      "我讀得到今天的簡報，也可以說明這個網站怎麼看——\n那排小格子、時間標籤、新聞怎麼排序。",
    starters: (script.dataset.starters || "").split("|").map(function (s) {
      return s.trim();
    }).filter(Boolean),
    accent: script.dataset.accent || "#0b6e6e"
  };

  var host = document.createElement("div");
  host.style.cssText = "position:fixed;z-index:2147483000;inset:auto 0 0 auto;";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });

  // 免費方案的後端閒置 15 分鐘會休眠，冷啟動要 30–60 秒。
  // 頁面一載入就先敲一下 /health 把它叫醒，訪客點開視窗、讀完招呼語、
  // 打完字的這段時間剛好夠它起來，所以感覺不到延遲。
  // 失敗完全忽略——這只是熱機，不影響後續對話。
  try {
    fetch(cfg.api + "/health", { method: "GET" }).catch(function () {});
  } catch (e) {}

  root.innerHTML = [
    "<style>",
    ":host{--signal:" + cfg.accent + ";--ink:#16202e;--muted:#5c6b7f;",
    "--paper:#f2f4f7;--card:#fff;--rule:#d6dce4;",
    "--ui:'Segoe UI',system-ui,'Noto Sans TC','PingFang TC','Microsoft JhengHei',sans-serif}",
    "*{box-sizing:border-box;margin:0;font-family:var(--ui)}",
    ".wrap{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;",
    "align-items:flex-end;gap:10px}",

    /* 啟動鈕：方角膠囊，跟網站的新聞卡片同一個圓角 */
    ".launch{display:flex;align-items:center;gap:9px;border:1px solid var(--signal);",
    "cursor:pointer;background:var(--signal);color:#fff;font-size:.9rem;",
    "letter-spacing:.02em;padding:11px 18px;border-radius:3px;",
    "box-shadow:0 2px 10px rgba(22,32,46,.16);transition:opacity .15s}",
    ".launch:hover{opacity:.9}",
    ".launch:focus-visible{outline:2px solid var(--signal);outline-offset:3px}",

    /* 三格記號，呼應網站的佐證強度標記 */
    ".bars{display:flex;gap:2px;flex:none}",
    ".bar{width:3px;height:11px;background:#fff;border-radius:1px}",
    ".bar.off{background:rgba(255,255,255,.4)}",

    ".panel{width:370px;height:520px;max-height:78vh;background:var(--card);",
    "border:1px solid var(--rule);border-radius:3px;overflow:hidden;display:none;",
    "flex-direction:column;box-shadow:0 8px 30px rgba(22,32,46,.14);",
    "transform-origin:bottom right;animation:pop .16s ease-out}",
    ".panel.on{display:flex}",
    "@keyframes pop{from{opacity:0;transform:translateY(6px)}}",
    "@media(prefers-reduced-motion:reduce){.panel{animation:none}.launch{transition:none}}",

    ".head{display:flex;align-items:flex-start;justify-content:space-between;",
    "padding:13px 15px;border-bottom:1px solid var(--rule);background:var(--paper)}",
    ".eyebrow{font-size:.68rem;font-weight:600;letter-spacing:.14em;",
    "text-transform:uppercase;color:var(--signal);margin-bottom:2px}",
    ".title{font-size:.95rem;font-weight:600;color:var(--ink)}",
    ".x{border:0;background:none;cursor:pointer;color:var(--muted);font-size:20px;",
    "line-height:1;padding:2px 5px;border-radius:3px}",
    ".x:hover{background:var(--rule)}",

    ".log{flex:1;overflow-y:auto;padding:15px;display:flex;flex-direction:column;gap:11px}",
    ".msg{max-width:85%;font-size:.9rem;line-height:1.65;padding:9px 12px;",
    "border-radius:3px;white-space:pre-wrap;word-break:break-word}",
    ".bot{align-self:flex-start;background:var(--paper);color:var(--ink);",
    "border-left:2px solid var(--signal)}",
    ".me{align-self:flex-end;background:var(--signal);color:#fff}",
    ".err{align-self:flex-start;font-size:.85rem;line-height:1.6;color:#8a3a2e;",
    "padding:2px 0}",

    ".chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 15px 13px}",
    ".chip{border:1px solid var(--rule);background:var(--card);color:var(--ink);",
    "cursor:pointer;font-size:.8rem;padding:6px 11px;border-radius:3px;text-align:left}",
    ".chip:hover{border-color:var(--signal);color:var(--signal)}",

    ".bar-in{display:flex;gap:8px;align-items:flex-end;padding:11px;",
    "border-top:1px solid var(--rule)}",
    "textarea{flex:1;resize:none;border:1px solid var(--rule);border-radius:3px;",
    "padding:8px 10px;font-size:.9rem;line-height:1.5;max-height:92px;color:var(--ink)}",
    "textarea:focus{outline:none;border-color:var(--signal)}",
    ".send{border:0;background:var(--signal);color:#fff;cursor:pointer;",
    "border-radius:3px;padding:9px 14px;font-size:.85rem}",
    ".send:disabled{opacity:.4;cursor:default}",

    ".think{display:flex;gap:3px;align-self:flex-start;padding:11px 12px}",
    ".think i{width:5px;height:5px;border-radius:50%;background:var(--muted);",
    "opacity:.4;animation:blink 1.2s infinite}",
    ".think i:nth-child(2){animation-delay:.2s}.think i:nth-child(3){animation-delay:.4s}",
    "@keyframes blink{30%{opacity:1}}",

    "@media(max-width:30rem){.wrap{right:12px;left:12px;bottom:12px;align-items:stretch}",
    ".panel{width:100%;height:72vh}.launch{justify-content:center}}",
    "</style>",

    '<div class="wrap">',
    '  <div class="panel" role="dialog" aria-label="' + cfg.name + '">',
    '    <div class="head"><div>',
    '      <div class="eyebrow">簡報</div>',
    '      <div class="title">' + cfg.name + "</div></div>",
    '      <button class="x" aria-label="關閉">&times;</button></div>',
    '    <div class="log"></div>',
    '    <div class="chips"></div>',
    '    <div class="bar-in"><textarea rows="1" placeholder="問今天的新聞或網站用法…"></textarea>',
    '      <button class="send">送出</button></div>',
    "  </div>",
    '  <button class="launch"><span class="bars">',
    '    <span class="bar"></span><span class="bar"></span><span class="bar off"></span>',
    "  </span>問AI小助手</button>",
    "</div>"
  ].join("");

  var $ = function (s) { return root.querySelector(s); };
  var panel = $(".panel"), log = $(".log"), chips = $(".chips");
  var box = $("textarea"), sendBtn = $(".send"), launch = $(".launch");
  var history = [], busy = false;

  function bubble(cls, text) {
    var el = document.createElement("div");
    el.className = cls === "err" ? "err" : "msg " + cls;
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function drawChips() {
    chips.innerHTML = "";
    if (history.length || !cfg.starters.length) return;
    cfg.starters.forEach(function (q) {
      var b = document.createElement("button");
      b.className = "chip";
      b.textContent = q;
      b.onclick = function () { send(q); };
      chips.appendChild(b);
    });
  }

  function toggle(open) {
    panel.classList.toggle("on", open);
    launch.style.display = open ? "none" : "flex";
    if (open) {
      if (!log.childElementCount) { bubble("bot", cfg.greeting); drawChips(); }
      box.focus();
    }
  }

  launch.onclick = function () { toggle(true); };
  $(".x").onclick = function () { toggle(false); };
  box.oninput = function () {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 92) + "px";
  };
  box.onkeydown = function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(box.value); }
  };
  sendBtn.onclick = function () { send(box.value); };

  async function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;

    busy = true;
    sendBtn.disabled = true;
    box.value = "";
    box.style.height = "auto";
    bubble("me", text);
    history.push({ role: "user", content: text });
    chips.innerHTML = "";

    var wait = document.createElement("div");
    wait.className = "think";
    wait.innerHTML = "<i></i><i></i><i></i>";
    log.appendChild(wait);
    log.scrollTop = log.scrollHeight;

    var out = null, answer = "";
    try {
      var res = await fetch(cfg.api + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history })
      });
      wait.remove();

      if (!res.ok) {
        var detail = await res.json().catch(function () { return {}; });
        bubble("err", detail.detail || "連線失敗（" + res.status + "），請稍後再試。");
        history.pop();
        return;
      }

      var reader = res.body.getReader(), decoder = new TextDecoder(), buf = "";
      while (true) {
        var r = await reader.read();
        if (r.done) break;
        buf += decoder.decode(r.value, { stream: true });
        var parts = buf.split("\n\n");
        buf = parts.pop();
        parts.forEach(function (block) {
          var line = block.split("\n").find(function (l) { return l.indexOf("data: ") === 0; });
          if (!line) return;
          var ev = JSON.parse(line.slice(6));
          if (ev.type === "delta") {
            if (!out) out = bubble("bot", "");
            answer += ev.text;
            out.textContent = answer;
            log.scrollTop = log.scrollHeight;
          } else if (ev.type === "error") {
            bubble("err", ev.text);
          }
        });
      }
      if (answer) history.push({ role: "assistant", content: answer });
      else history.pop();
    } catch (err) {
      wait.remove();
      bubble("err", "連不上小助手，請確認網路後再試一次。");
      history.pop();
    } finally {
      busy = false;
      sendBtn.disabled = false;
      box.focus();
    }
  }
})();
