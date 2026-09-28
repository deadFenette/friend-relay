"use strict";
/* core.js — общее состояние, форматирование, крипто-помощники и HTTP-слой.
   Первый из модулей веб-клиента (см. index.html, порядок загрузки важен:
   core → chat/files/profile/chess/voice → main). */

/* ───────────────────────── состояние ───────────────────────── */
const S = {
  name: "", key: "", token: "", since: 0,
  adminKey: "",         // (v3.5.1) ключ админа: с именем хоста -> права админа
  channel: "", channelOpen: false,
  online: [], typing: [], busy: false, timer: null, connecting: false,
  msgs: new Map(),      // seq -> элемент бабла (лента)
  meta: new Map(),      // seq -> {encrypted, iv, file, reactions, pinned, edited}
  chanSnapshot: [],     // последний ответ /channel/messages (для диффа)
  typingSentAt: 0,      // троттлинг POST /typing
  salt: "", encKey: "", encPass: "", encReady: false, // шифрование сообщений
  sending: false,       // v1.9.5: guard двойной отправки
  chanReq: 0,           // v1.9.5: счётчик запросов канала (гонка устаревших ответов)
  unread: 0,            // v1.9.5: непрочитанные (вкладка не активна / scrolled up)
  oldestSeq: Infinity,  // v1.9.5: минимальный seq в ленте (подгрузка вверх)
  chanCreator: {},      // v2.0.3: имя канала -> создатель (кнопка 🗑 только автору)
  dmBadgeAt: 0,         // v2.0.3: момент последнего dmBadgeTick (раз в DM_BADGE_MS)
};

const FEED_CAP = 400;   // максимум баблов в ленте (длинные сессии не жрут память)

const $ = id => document.getElementById(id);
const esc = s => (s == null ? "" : String(s)).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtSize = n => {
  if (n == null) return "";
  const u = ["Б","КБ","МБ","ГБ"]; let i = 0, v = n;
  while (v >= 1024 && i < u.length-1){ v /= 1024; i++; }
  return (i ? v.toFixed(1) : v) + " " + u[i];
};
const fmtTime = ts => {
  try { return new Date(ts*1000).toLocaleTimeString("ru-RU",
    {hour:"2-digit",minute:"2-digit"}); } catch(e){ return ""; }
};

/* header_encode из lib/util.py: UTF-8 байты как latin-1 строка */
function headerEncode(str){
  const bytes = new TextEncoder().encode(str);
  let out = "";
  for (const b of bytes) out += String.fromCharCode(b);
  return out;
}

/* v3.2: SVG-иконка по имени (спрайт icons.js) — лениво, без зависимостей
   на порядок загрузки: спрайт инжектится на DOMContentLoaded, а все
   вызовы frIcon случаются позже (реакции на пользователя). Если icons.js
   вдруг не загрузился — возвращаем безвредную точку-заглушку. */
function frIcon(name){
  if (window.FRIcon && typeof window.FRIcon.make === "function"){
    try {
      const el = window.FRIcon.make(name);
      if (el) return el;
    } catch(e){}
  }
  const s = document.createElement("span");
  s.className = "ico-fallback";
  s.setAttribute("aria-hidden", "true");
  s.textContent = "•";
  return s;
}

/* ───────────────────────── крипто-помощники ─────────────────────────
   Основной путь — node-forge (MIT, /static/forge.min.js): работает и на
   http://IP, где crypto.subtle недоступен (secure context). Все строки
   перед хешированием — UTF-8, ровно как .encode("utf-8") в lib/auth.py
   и lib/crypto.py. */
function utf8(s){ return window.forge ? forge.util.encodeUtf8(s) : s; }
function sha256hex(s){
  if (window.forge){
    const md = forge.md.sha256.create();
    md.update(utf8(s));
    return md.digest().toHex();
  }
  throw new Error("forge не загружен");
}
/* ArrayBuffer → бинарная строка (для sha256 файла) */
function binStrOfBuffer(buf){
  const u8 = new Uint8Array(buf);
  const parts = [];
  for (let i = 0; i < u8.length; i += 8192)
    parts.push(String.fromCharCode.apply(null, u8.subarray(i, i + 8192)));
  return parts.join("");
}
/* SHA-256 файла: forge на http, crypto.subtle на https — раньше был
   только crypto.subtle, и на http://IP заливка файлов валилась с
   «crypto.subtle is undefined» (баг v1.8.1 и ниже).
   v1.9.5: без склейки в одну строку — md.update кусок за куском. Раньше
   parts.join("") давал пик 2× размера файла в RAM (массив кусков + строка
   целиком) + сами куски = ~3×: файл 500 МБ съедал ~1.5 ГБ и вкладка
   умирала. */
async function sha256OfBuffer(buf){
  if (window.forge){
    const md = forge.md.sha256.create();
    const u8 = new Uint8Array(buf);
    for (let i = 0; i < u8.length; i += 8192){
      const chunk = u8.subarray(i, i + 8192);
      md.update(String.fromCharCode.apply(null, chunk));
    }
    return md.digest().toHex();
  }
  if (window.crypto && crypto.subtle){
    const digest = await crypto.subtle.digest("SHA-256", buf);
    return [...new Uint8Array(digest)]
      .map(b => b.toString(16).padStart(2, "0")).join("");
  }
  throw new Error("нет ни forge, ни crypto.subtle");
}
/* HMAC по БИНАРНОЙ строке (байты файла, charCode 0-255) — без
   encodeUtf8: forge.util.encodeUtf8 на бинарной строке пере-кодирует байты
   старше 127 в многобайтовый UTF-8 и подпись расходится с сервером,
   который считает HMAC по сырым байтам. */
async function hmacHexBin(token, binStr){
  if (window.forge){
    const mac = forge.hmac.create();
    mac.start("sha256", token);
    mac.update(binStr);
    return mac.digest().toHex();
  }
  if (window.crypto && crypto.subtle){
    const enc = new TextEncoder();
    const key = await crypto.subtle.importKey("raw", enc.encode(token),
      {name:"HMAC", hash:"SHA-256"}, false, ["sign"]);
    const bytes = new Uint8Array(binStr.length);
    for (let i = 0; i < binStr.length; i++) bytes[i] = binStr.charCodeAt(i);
    const sig = await crypto.subtle.sign("HMAC", key, bytes);
    return [...new Uint8Array(sig)].map(b=>b.toString(16).padStart(2,"0")).join("");
  }
  throw new Error("нет ни forge, ни crypto.subtle");
}
/* X-Relay-Auth: "<token>:<HMAC-SHA256(token, body)>" — lib/auth.py */
async function hmacHex(token, text){
  if (window.forge){
    const mac = forge.hmac.create();
    mac.start("sha256", token);   // token — ascii base64
    mac.update(utf8(text));       // тело — UTF-8, как sign_request
    return mac.digest().toHex();
  }
  if (window.crypto && crypto.subtle){
    const enc = new TextEncoder();
    const key = await crypto.subtle.importKey("raw", enc.encode(token),
      {name:"HMAC", hash:"SHA-256"}, false, ["sign"]);
    const sig = await crypto.subtle.sign("HMAC", key, enc.encode(text));
    return [...new Uint8Array(sig)].map(b=>b.toString(16).padStart(2,"0")).join("");
  }
  throw new Error("нет ни forge, ни crypto.subtle");
}
/* PBKDF2-HMAC-SHA256(pass, salt, 600000, 32 байта) — как _derive_key в
   lib/crypto.py. forge-путь синхронный и на http занимает несколько секунд,
   поэтому результат кешируется в sessionStorage. Возвращает hex. */
const PBKDF2_ITER = 600000;
/* PBKDF2 в Web Worker (v1.9.5): 600k итераций синхронно замораживали
   главный поток на секунды (на слабых телефонах — до 10с, вкладка не
   отвечала). Worker гонит тот же forge по тем же параметрам. */
function pbkdf2InWorker(pass, salt){
  return new Promise((resolve, reject) => {
    let w;
    try { w = new Worker("/static/pbkdf2-worker.js"); }
    catch(e){ reject(e); return; }
    const timer = setTimeout(() => {
      w.terminate(); reject(new Error("вывод ключа затянулся"));
    }, 120000);
    w.onmessage = (ev) => {
      clearTimeout(timer);
      w.terminate();
      if (ev.data && ev.data.ok) resolve(ev.data.hex);
      else reject(new Error((ev.data && ev.data.error) || "worker fail"));
    };
    w.onerror = (e) => {
      clearTimeout(timer);
      w.terminate();
      reject(new Error("pbkdf2 worker: " + (e.message || "ошибка")));
    };
    w.postMessage({pass, salt, iter: PBKDF2_ITER});
  });
}
async function deriveKeyHex(pass, salt){
  const cacheId = "wr_k_" + sha256hex(salt + ":" + pass);
  try {
    const c = sessionStorage.getItem(cacheId);
    if (c) return c;
  } catch(e){}
  let hex = "";
  if (window.Worker && window.forge){
    /* основной путь: фоновой поток; UI остаётся отзывчивым */
    try {
      hex = await pbkdf2InWorker(pass, salt);
    } catch(e){
      hex = "";   /* фолбэк ниже пересчитает синхронно */
    }
  }
  if (hex){
    /* Worker уже вывел ключ */
  } else if (window.forge){
    await new Promise(r => setTimeout(r, 30)); // дать UI дорисоваться
    hex = forge.util.bytesToHex(
      forge.pkcs5.pbkdf2(utf8(pass), utf8(salt), PBKDF2_ITER, 32, "sha256"));
  } else if (window.crypto && crypto.subtle){
    const enc = new TextEncoder();
    const key = await crypto.subtle.importKey("raw", enc.encode(pass),
      "PBKDF2", false, ["deriveBits"]);
    const bits = await crypto.subtle.deriveBits(
      {name:"PBKDF2", hash:"SHA-256", salt: enc.encode(salt),
       iterations: PBKDF2_ITER}, key, 256);
    hex = [...new Uint8Array(bits)].map(b => b.toString(16).padStart(2, "0")).join("");
  } else {
    throw new Error("нет ни forge, ни crypto.subtle");
  }
  try { sessionStorage.setItem(cacheId, hex); } catch(e){}
  return hex;
}

/* ───────────────────────── расшифровка событий ─────────────────────────
   Возвращает:
   null — ключа нет (показать подсказку),
   ""   — ключ есть, но не подошло (подмена/чужой ключ),
   строку — открытый текст. Форматы как в lib/crypto.py:
   "v2:" + base64(nonce[12] + ciphertext + tag[16]) и легаси XOR (hex + iv). */
function decryptEventText(ev){
  if (!ev || !ev.encrypted || !S.encReady) return null;
  const t = ev.text || "";
  if (t.startsWith("v2:")){
    try {
      const blob = atob(t.slice(3));
      if (blob.length < 28) return "";
      /* python AESGCM.encrypt() отдаёт ciphertext+tag[16] склейкой;
         forge хочет тег отдельно */
      const ct = blob.slice(12, blob.length - 16);
      const tag = blob.slice(blob.length - 16);
      const key = forge.util.createBuffer(forge.util.hexToBytes(S.encKey));
      const d = forge.cipher.createDecipher("AES-GCM", key);
      d.start({iv: forge.util.createBuffer(blob.slice(0, 12)),
               tag: forge.util.createBuffer(tag)});
      d.update(forge.util.createBuffer(ct));
      return d.finish() ? d.output.toString("utf8") : "";
    } catch(e){ return ""; }
  }
  try {
    if (!ev.iv) return "";
    const data = forge.util.hexToBytes(t);
    const kb = utf8(S.encPass + ev.iv);
    const bytes = new Uint8Array(data.length);
    for (let i = 0; i < data.length; i++)
      bytes[i] = data.charCodeAt(i) ^ kb.charCodeAt(i % kb.length);
    return new TextDecoder().decode(bytes);
  } catch(e){ return ""; }
}
/* ───────────── шифрование исходящих (v1.9.5) ─────────────
   Зеркало decryptEventText: "v2:" + base64(nonce[12] + ct + tag[16]),
   формат 1:1 с lib/crypto.py AESGCM.encrypt(). Раньше веб-клиент
   ВООБЩЕ не шифровал исходящие (даже с введённым ключом): его сообщения
   лежали на сервере открытым текстом, хотя Qt-получатели с ключом
   считали канал E2E. iv обязателен непустой — иначе Qt пометит
   сообщение _decrypt_failed (client.py:328). */
function encryptEventText(text){
  if (!S.encReady || !window.forge) return null;
  try {
    const nonce = forge.random.getBytesSync(12);
    const key = forge.util.createBuffer(forge.util.hexToBytes(S.encKey));
    const c = forge.cipher.createCipher("AES-GCM", key);
    c.start({iv: forge.util.createBuffer(nonce)});
    c.update(forge.util.createBuffer(forge.util.encodeUtf8(text)));
    if (!c.finish()) return null;
    const blob = nonce + c.output.getBytes() + c.mode.tag.getBytes();
    return {
      text: "v2:" + btoa(blob),
      iv: forge.util.bytesToHex(nonce),
    };
  } catch(e){ return null; }
}

/* Текст бабла для события/сообщения канала */
function displayText(ev){
  if (!ev.encrypted) return ev.text || "";
  const pt = decryptEventText(ev);
  if (pt === null)
    return "\uD83D\uDD12 [зашифровано — введи ключ шифрования при подключении]";
  if (pt === "") return "\uD83D\uDD12 [не удалось расшифровать]";
  return pt;
}

/* ───────── мини-маркдаун (v2.0.3) ─────────
   Порт lib/markdown_renderer.py: то же подмножество и ТОТ ЖЕ порядок
   правил, чтобы веб и Qt показывали сообщение одинаково:
     **bold** · *italic* · `code` · ```блоки``` · > цитата
     [текст](url) · автоссылки https://…
   Безопасность — как в Qt: СНАЧАЛА esc() экранирует & < > " во всём
   тексте (XSS закрыт), и только ЗАТЕМ разметка превращается в теги.
   Внутри оказываются только: <b><i><code><pre><blockquote><a><br><div>.
   href у <a> — лишь http(s):// или friendrelay://, javascript: отсекается
   на этапе проверки ссылки (как _safe_url в markdown_renderer.py). */
/* v3: в URL могут быть http(s):// и friendrelay:// (deep-link для игр) */
const MD_LINK_RE = /\[([^\]]+)\]\(((?:https?:|friendrelay:)[^)\s]+)\)/g;
/* v3: bare-URL — НЕ матчат то, что уже внутри href="..." (иначе ломает
   ссылки, созданные на шаге 4 из [text](url)). Негативный lookbehind
   как в lib/markdown_renderer.py. Также не матчат URL внутри одинарных
   кавычек (на случай, если кто-то использует href='...'). */
const MD_URL_RE = /(?<!href=")(?<!href=')(?:https?:\/\/[^\s<>"']+|friendrelay:\/\/[^\s<>"']+)/g;
/* _safe_url из lib/markdown_renderer.py: только http(s), без кавычек */
function mdSafeUrl(u){
  u = String(u || "");
  /* v3: разрешаем http(s) и наш собственный deep-link friendrelay://
     (кликабельные приглашения в шахматы — см. lib/bots/chess.py).
     javascript: и прочее — отбрасываем (защита от XSS). */
  if (!/^(https?:|friendrelay:)/i.test(u)) return "";
  if (/["'<>\\]/.test(u)) return "";   /* подстраховка от кривых URL */
  return u;
}
function renderMsgMarkdown(el, text){
  if (text == null) text = "";
  text = String(text);
  /* быстрый выход: нет ни одного маркера — рисуем plain text (дешево).
     v3: + friendrelay: для deep-link приглашений */
  if (!/[*`>\[]|https?:\/\/|friendrelay:|```/.test(text)){
    el.textContent = text;
    return;
  }
  /* 1. экранируем HTML (защита от XSS — как html.escape в Qt) */
  let t = esc(text);
  /* 2. code-блоки ```…``` прячем под плейсхолдеры — внутри них
        markdown НЕ применяется (как в _save_code_block) */
  const spans = [];   /* и блоки, и инлайн-код — общий карман */
  const stash = (s) => "\x00CB" + (spans.push(s) - 1) + "\x00";
  t = t.replace(/```(?:[^\n]*)?\n?([\s\S]*?)```/g,
    (m, code) => stash(code.replace(/\n$/, "")));
  /* 3. инлайн-код `…` — тоже прячем: внутри кода не должны работать
        ни ссылки, ни bold/italic (как в Qt) */
  t = t.replace(/`([^`\n]+)`/g, (m, code) => stash(code));
  /* 4. [текст](url) — до автоссылок, чтобы ссылка не съела скобки */
  t = t.replace(MD_LINK_RE, (m, label, url) => {
    const safe = mdSafeUrl(url);
    return safe
      ? '<a href="' + safe + '" target="_blank" rel="noopener noreferrer">'
        + label + "</a>"
      : label;
  });
  /* 5. автоссылки https://… (одиночные URL вне скобок) */
  t = t.replace(MD_URL_RE, (u) => {
    const safe = mdSafeUrl(u);
    if (!safe) return u;
    const cut = u.replace(/[.,;:!)\]]+$/, "");   /* хвостовая пунктуация — не в URL */
    const tail = u.slice(cut.length);
    return '<a href="' + cut + '" target="_blank" rel="noopener noreferrer">'
      + cut + "</a>" + tail;
  });
  /* 6. bold/italic — неразрушающие к уже вставленным тегам: **…** без < > */
  t = t.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  t = t.replace(/\*([^*\n]+)\*/g, "<i>$1</i>");
  /* 7. строки: «> цитата» собираем в blockquote, переносы → <br> */
  const lines = t.split("\n");
  let out = "", inQuote = false;
  for (const line of lines){
    if (/^&gt;\s?/.test(line)){
      if (!inQuote){ out += "<blockquote>"; inQuote = true; }
      else out += "<br>";
      out += line.replace(/^&gt;\s?/, "");
      continue;
    }
    if (inQuote){ out += "</blockquote>"; inQuote = false; }
    out += line + "<br>";
  }
  if (inQuote) out += "</blockquote>";
  /* 8. возвращаем код на место (уже после всего остального — внутри кода
        гарантированно нет разметки). Блоку переносы строк даём <br>:
        white-space:pre-wrap у .mdcode показал бы \n и <br> дважды */
  out = out.replace(/\x00CB(\d+)\x00/g, (m, i) => {
    const code = spans[+i] || "";
    /* блок это плейсхолдер, съевший \n (многострочный) — рендерим
       как mdcode; одиночный без \n — компактным <code> */
    return code.includes("\n")
      ? '<div class="mdcode">' + code.replace(/\n/g, "<br>") + "</div>"
      : "<code>" + code + "</code>";
  });
  el.innerHTML = out;
}

/* ───────────────────────── HTTP-слой ───────────────────────── */
function baseHeaders(extra){
  const h = Object.assign({}, extra);
  h["X-Relay-From"] = headerEncode(S.name);
  if (S.key) h["X-Relay-Key"] = S.key;
  return h;
}
/* Раньше r.json() падал с «Unexpected token» на любом не-JSON ответе
   (страница ошибки прокси, пустое тело) — теперь безопасный разбор:
   не-JSON превращается в {ok:false, error}, вызывающие продолжают
   видеть понятные сообщения. */
async function safeJson(r){
  try { return await r.json(); }
  catch(e){ return null; }
}
function synthError(r, j){
  if (j) return j;
  const hint = r.status === 403 ? " (ключ доступа не принят)"
    : (r.status === 401 ? " (сессия истекла)" : "");
  return {ok: false, error: "HTTP " + r.status + hint};
}
/* Таймаут на fetch (v1.9.5): полутупое TCP-соединение (хост-процесс убран
   за NAT без RST, смена сети) НЕ отвергается и НЕ резолвится минутами —
   fetch висел вечно, S.busy=true застревал, pollTick молча пропускал все
   тики: лента замирала «подключено» до перезагрузки страницы. 10 секунд
   хватает всем живым запросам; AbortSignal.timeout есть во всех
   поддерживаемых браузерах (2022+), при отсутствии — без сигнала, как
   раньше. */
function fetchTimeout(ms){
  try { return {signal: AbortSignal.timeout(ms)}; }
  catch(e){ return {}; }
}

async function apiGet(path){
  /* GET тоже подписываем (v1.9.5): приватные маршруты (/dm/*) сервер
     теперь проверяет; остальным подпись не мешает. */
  const headers = baseHeaders();
  if (S.token){
    try {
      headers["X-Relay-Auth"] = S.token + ":" + await hmacHex(S.token, path);
    } catch(e){}
  }
  const r = await fetch(path, {headers, ...fetchTimeout(10000)});
  return {status: r.status, json: synthError(r, await safeJson(r))};
}
async function apiPost(path, bodyObj, opts){
  /* opts (v1.9.5): {timeoutMs} — для файлов/медленных операций. */
  const text = JSON.stringify(bodyObj);
  let headers = baseHeaders({"Content-Type":"application/json; charset=utf-8"});
  if (S.token){
    headers["X-Relay-Auth"] = S.token + ":" + await hmacHex(S.token, text);
  }
  let r = await fetch(path, {method:"POST", headers, body: text,
    ...fetchTimeout((opts && opts.timeoutMs) || 10000)});
  let json = synthError(r, await safeJson(r));
  /* Авторекавери сессии (v1.9.5): сервер хранит сессии в RAM — после
     рестарта хоста token протухает, каждый POST получал 403 «неверная
     сессия» навсегда (до F5). Пере-ping'аемся тем же именем и повторяем
     запрос один раз с новой подписью. */
  if (r.status === 403 && S.token && json &&
      /сесси/i.test(json.error || "")){
    const ok = await relogin();
    if (ok){
      headers = baseHeaders({"Content-Type":"application/json; charset=utf-8"});
      headers["X-Relay-Auth"] = S.token + ":" + await hmacHex(S.token, text);
      r = await fetch(path, {method:"POST", headers, body: text,
        ...fetchTimeout((opts && opts.timeoutMs) || 10000)});
      json = synthError(r, await safeJson(r));
    }
  }
  return {status: r.status, json};
}
/* Пере-ping с сохранённым именем/ключом (см. apiPost) */
async function relogin(){
  try {
    const r = await fetch("/ping", {headers: baseHeaders(), ...fetchTimeout(8000)});
    const j = await safeJson(r);
    if (j && j.ok && j.session_token){
      S.token = j.session_token;
      return true;
    }
  } catch(e){}
  return false;
}
function setConn(on, txt){
  $("connDot").classList.toggle("on", !!on);
  $("connTxt").textContent = txt || (on ? "подключено" : "нет связи");
}
function warn(id, msg){ $(id).textContent = msg || ""; }
