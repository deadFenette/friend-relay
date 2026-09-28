"use strict";
/* dm.js — личные сообщения в браузере (v2.0.2, расширено в v2.0.3).
   Вкладка «ЛС»: выбор собеседника (друзья + существующие диалоги),
   лента диалога, отправка. Серверный API существовал давно (Qt-клиент
   пользуется): GET /dm/history, POST /dm/send, GET /dm/conversations —
   веб-клиент только добрался до него. ЛС — приватная переписка: она не
   попадает в общий журнал history.jsonl и в ленту других участников.

   E2E-шифрование — то же, что в общем чате (encryptEventText из core.js):
   при готовом ключе текст уходит как v2:…, у получателя расшифровывается.

   Обновление ленты — без своего таймера: pollTick (main.js) дергает
   pollDm() в общем такте каждые 1.5с, пока вкладка открыта и диалог
   выбран. Лента обновляется инкрементально (как каналы в chat.js) —
   скролл и позиция чтения не сбрасываются.

   v2.0.3:
   - файлы в ЛС: 📎 → POST /send_dm_file (X-Relay-To = собеседник;
     физически отдельная папка dm_files/, в общей витрине /files НЕ
     показывается), скачивание GET /dm_download/<file_id> — сервер
     отдаёт только отправителю и получателю, остальным 403;
   - бейдж непрочитанных на вкладке «ЛС»: dmBadgeTick опрашивает
     /dm/conversations раз в DM_BADGE_MS (легкий список — только
     последние сообщения диалогов) и сверяет с отметками «прочитано»
     в localStorage «wr_dm_seen». Точность — «диалог обновился»:
     сервер не сообщает, кто был последним, поэтому своё же сообщение,
     отправленное из другого клиента, тоже поднимет бейдж — терпимо,
     зато ноль лишних запросов истории. */

const DM = {
  peer: "",        // выбранный собеседник (логин)
  peers: [],       // кому вообще можно писать: друзья + старые диалоги
  snapshot: [],    // последнее состояние истории (для диффа)
  req: 0,          // счётчик запросов: устаревший ответ не трогает ленту
  sending: false,  // guard двойной отправки (как в chat.js)
  uploading: false, // guard двойной заливки файла
};
/* как часто обновлять бейдж ЛС (вызывает main.js в общем такте) */
const DM_BADGE_MS = 6000;
const DM_SEEN_KEY = "wr_dm_seen";   // {peer: ts последнего прочитанного}

function dmWarn(msg){ warn("dmWarn", msg || ""); }

/* ── отметки «прочитано» (localStorage не критичен — try/catch) ── */
function dmSeenMap(){
  try { return JSON.parse(localStorage.getItem(DM_SEEN_KEY) || "{}"); }
  catch(e){ return {}; }
}
function dmMarkSeen(peer, ts){
  if (!peer) return;
  try {
    const seen = dmSeenMap();
    if ((seen[peer] || 0) >= ts) return;   // старее — не пишем
    seen[peer] = ts;
    localStorage.setItem(DM_SEEN_KEY, JSON.stringify(seen));
  } catch(e){}
}

/* ── бейдж непрочитанных ЛС на вкладке «✉ ЛС» ──
   Первый запуск инициализирует seen текущим состоянием диалогов —
   иначе после обновления клиент засчитал бы ВСЮ старую переписку. */
let DM_SEEN_INIT = false;
async function dmBadgeTick(){
  if (!S.token) return;
  try {
    const {json} = await apiGet("/dm/conversations");
    const convs = (json && json.conversations) || [];
    const seen = dmSeenMap();
    if (!DM_SEEN_INIT && !seen._init){
      for (const c of convs) seen[c.user] = c.last_time || 0;
      seen._init = 1;
      try { localStorage.setItem(DM_SEEN_KEY, JSON.stringify(seen)); }
      catch(e){}
      DM_SEEN_INIT = true;
      updateDmBadge(0);
      return;
    }
    DM_SEEN_INIT = true;
    let unread = 0;
    for (const c of convs){
      if ((c.last_time || 0) > (seen[c.user] || 0)) unread++;
    }
    /* открытый диалог с открытой вкладкой считается прочитанным:
       pollDm уже обновил ленту и поставил отметку (см. loadDmMessages) */
    updateDmBadge(unread);
  } catch(e){ /* тишина: бейдж не критичен */ }
}
function updateDmBadge(n){
  const tab = document.querySelector('.tabs button[data-tab="dm"]');
  if (!tab) return;
  let b = tab.querySelector(".badge");
  if (n > 0){
    if (!b){
      b = document.createElement("span");
      b.className = "badge";
      tab.appendChild(b);
    }
    b.textContent = n > 99 ? "99+" : String(n);
  } else if (b) b.remove();
}

/* ── список собеседников: друзья + существующие диалоги, без дублей ── */
async function refreshDm(){
  if (!S.token) return;
  const names = [];
  const push = (n) => {
    if (n && n !== S.name && !names.includes(n)) names.push(n);
  };
  try {
    const {json} = await apiGet("/dm/conversations");
    for (const c of (json && json.conversations) || []) push(c.user);
  } catch(e){ /* список диалогов не критичен */ }
  try {
    const {json} = await apiGet("/friends?name=" + encodeURIComponent(S.name));
    for (const f of (json && json.friends) || []) push(f.name);
  } catch(e){}
  /* друзья, пришедшие из профиля в этой сессии */
  for (const f of (typeof FRIENDS !== "undefined" ? FRIENDS : [])) push(f.name);

  const sel = $("dmPeer");
  if (!sel) return;
  const sig = names.join("\n");
  if (sig === DM.peers.join("\n")) return;   /* без изменений — не трогаем */
  DM.peers = names;
  sel.innerHTML = "";
  const def = document.createElement("option");
  def.value = ""; def.textContent = "— выбери, кому писать —";
  sel.appendChild(def);
  for (const n of names){
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  }
  if (DM.peer && names.includes(DM.peer)) sel.value = DM.peer;
  else { DM.peer = ""; }
}

/* ── открыть диалог с человеком (клик по другу, выбор в селекте) ── */
function dmSetPeer(name){
  DM.peer = name || "";
  const sel = $("dmPeer");
  if (sel && sel.value !== DM.peer) sel.value = DM.peer;
  DM.snapshot = [];
  $("dmFeed").innerHTML = "";
  dmWarn("");
  $("dmStat").textContent = DM.peer
    ? "диалог с " + DM.peer + " — видите только вы двое" : "";
  if (DM.peer){
    loadDmMessages(true);
    /* v2.0.3: диалог открыли — бейдж с него можно снять сразу */
    const convs = dmSeenMap();
    if (convs[DM.peer]) updateDmBadge(0);
  }
}

/* ── рендер бабла ЛС (классы общие с чатом: .msg/.me/.sysline) ── */
function dmAddBubble(o){
  const feed = $("dmFeed");
  const stick = nearBottom(feed);
  let el;
  if (o.system){
    el = document.createElement("div");
    el.className = "sysline";
    el.textContent = o.text;
  } else {
    el = document.createElement("div");
    el.className = "msg" + (o.me ? " me" : "") + (o.file ? " file" : "");
    if (!o.me && typeof avatarColor === "function"){
      const av = document.createElement("span");
      av.className = "bava";
      av.style.background = avatarColor(o.from);
      av.textContent = (typeof initialsOf === "function")
        ? initialsOf(o.from) : "?";
      el.appendChild(av);
    }
    const body = document.createElement("div");
    body.className = "mbody";
    const who = document.createElement("div");
    who.className = "who";
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = o.me ? "ты" : o.from;
    if (!o.me && typeof avatarColor === "function")
      nm.style.color = avatarColor(o.from);
    who.appendChild(nm);
    if (o.ts){
      const tm = document.createElement("span");
      tm.className = "tm";
      tm.textContent = fmtTime(o.ts);
      who.appendChild(tm);
    }
    const txt = document.createElement("div");
    txt.className = "txt";
    if (o.file){
      /* v2.0.3: файл ЛС — ссылка-скачивание (POST-событие dm_file).
         v3.2: скрепка — иконкой из спрайта, как в общем чате. */
      const a = document.createElement("a");
      a.href = "#";
      a.appendChild(frIcon("attach"));
      a.appendChild(document.createTextNode(" " + o.text.name));
      a.onclick = (e) => { e.preventDefault(); dmDownloadFile(o.text); };
      txt.appendChild(a);
      const meta2 = document.createElement("div");
      meta2.className = "hint";
      meta2.textContent = fmtSize(o.text.size) +
        (o.text.sha256 ? " · sha256 ok" : "");
      txt.appendChild(meta2);
    } else {
      /* v2.0.3: тот же мини-маркдаун, что в общем чате */
      renderMsgMarkdown(txt, o.text);
    }
    body.appendChild(who); body.appendChild(txt);
    el.appendChild(body);
  }
  if (o.seq) el.dataset.seq = o.seq;
  feed.appendChild(el);
  if (stick) feed.scrollTop = feed.scrollHeight;
}

/* ── история диалога: инкрементальный дифф по seq (см. chat.js) ── */
async function loadDmMessages(force){
  if (!DM.peer) return;
  const my = ++DM.req;
  try {
    const {json} = await apiGet("/dm/history?user="
      + encodeURIComponent(DM.peer) + "&limit=200");
    if (my !== DM.req) return;      /* приехал более свежий ответ */
    const msgs = (json && json.messages) || [];
    applyDmSnapshot(msgs, force);
  } catch(e){
    /* тишина: следующий тик поллинга повторит */
  }
}

function applyDmSnapshot(msgs, force){
  const feed = $("dmFeed");
  const prev = force ? [] : DM.snapshot;
  const prevBySeq = new Map(prev.map(m => [m.seq, m]));
  const newSeqs = new Set(msgs.map(m => m.seq));
  /* удалить баблы, которых больше нет в истории (сервер этого сейчас не
     делает для ЛС, но дифф дешевле оставить честным) */
  for (const m of prev){
    if (!newSeqs.has(m.seq)){
      const el = feed.querySelector('[data-seq="' + m.seq + '"]');
      if (el) el.remove();
    }
  }
  for (const m of msgs){
    const old = prevBySeq.get(m.seq);
    if (old && JSON.stringify(old) === JSON.stringify(m)) continue;
    const el = feed.querySelector('[data-seq="' + m.seq + '"]');
    if (el) el.remove();   /* изменилось (шифро-метка и т.п.) — перерисуем */
    if (m.kind === "dm_file"){
      /* v2.0.3: файл в ЛС — бабл со ссылкой на /dm_download/<id> */
      dmAddBubble({
        seq: m.seq, from: m.from, ts: m.ts, file: true,
        me: m.from === S.name,
        text: {file_id: m.file_id, name: m.name, size: m.size,
          sha256: m.sha256},
      });
    } else {
      dmAddBubble({
        seq: m.seq, from: m.from, ts: m.ts,
        me: m.from === S.name,
        text: displayText(m),   /* расшифровка как в общем чате */
      });
    }
  }
  if (!msgs.length){
    if (!feed.querySelector(".sysline"))
      dmAddBubble({system: true,
        text: "Пока пусто — напиши первым. Диалог видите только вы двое."});
  } else {
    const sys = feed.querySelector(".sysline");
    if (sys) sys.remove();
    /* v2.0.3: лента диалога актуальна — помечаем прочитанным.
       dmSetPeer не ставит отметку до первого ответа сервера: ts берём
       из истории, а не из Date.now(), чтобы не «съесть» сообщения,
       которые приедут с сервера с ts больше локальных часов. */
    const last = msgs[msgs.length - 1];
    if (last && (last.ts || 0) > 0) dmMarkSeen(DM.peer, last.ts);
  }
  DM.snapshot = msgs;
}

/* ── отправка (зеркало sendMsg из chat.js) ── */
async function sendDm(){
  dmWarn("");
  if (!DM.peer){ dmWarn("Сначала выбери собеседника"); return; }
  if (DM.sending) return;
  const text = $("dmText").value.trim();
  if (!text) return;
  DM.sending = true;
  try {
    let payload = {to: DM.peer, text};
    if (S.encReady){
      const enc = encryptEventText(text);
      if (enc) payload = {to: DM.peer, text: enc.text,
        encrypted: true, iv: enc.iv};
    }
    const {json} = await apiPost("/dm/send", payload);
    if (json.ok){
      $("dmText").value = "";
      await loadDmMessages();       /* подтянем своё сообщение с сервера */
    } else {
      dmWarn(json.error || "не отправилось");
    }
  } catch(e){
    dmWarn("не отправилось: " + e);
    $("dmText").value = text;       /* текст вернули — как в общем чате */
  } finally { DM.sending = false; }
}

/* ── тик поллинга (вызывается из main.js в общем такте) ── */
async function pollDm(){
  /* обновляем только ОТКРЫТЫЙ диалог: фоном обходить все диалоги —
     лишние запросы; список собеседников подхватит refreshDm при открытии */
  await refreshDm();
  if (DM.peer) await loadDmMessages();
}

/* ── файлы в ЛС (v2.0.3) ──
   POST /send_dm_file: тело — сырые байты, X-Relay-To = получатель,
   X-Relay-Filename / X-Relay-SHA256 — как в общей витрине.
   Подпись X-Relay-Auth — HMAC по байтам файла (стриминговая проверка
   на сервере). Файл НЕ попадает в /files — только в диалог. */
async function uploadDmFile(){
  const file = $("dmFileInput").files[0];
  $("dmFileInput").value = "";   /* выбор того же файла заново должен работать */
  if (!file){ return; }
  if (!DM.peer){ dmWarn("Сначала выбери собеседника"); return; }
  if (DM.uploading){ dmWarn("уже заливаю предыдущий файл"); return; }
  DM.uploading = true;
  dmWarn("");
  $("dmStat").textContent = "заливаю " + file.name + " (" +
    fmtSize(file.size) + ")…";
  try {
    const buf = await file.arrayBuffer();
    const sha = await sha256OfBuffer(buf);   /* forge на http, subtle на https */
    const upHeaders = baseHeaders({
      "X-Relay-Filename": headerEncode(file.name || "файл"),
      "X-Relay-To": headerEncode(DM.peer),
      "X-Relay-SHA256": sha,
      "Content-Type": "application/octet-stream",
    });
    if (S.token){
      try {
        upHeaders["X-Relay-Auth"] = S.token + ":"
          + await hmacHexBin(S.token, binStrOfBuffer(buf));
      } catch(e){}
    }
    const r = await fetch("/send_dm_file", {
      method: "POST",
      headers: upHeaders,
      body: buf,
      ...fetchTimeout(120000),   /* большие файлы — дольше 10с */
    });
    const json = await safeJson(r) || {ok: false, error: "HTTP " + r.status};
    if (json.ok){
      $("dmStat").textContent = "файл отправлен: " + file.name;
      await loadDmMessages();       /* подтянем событие dm_file */
      setTimeout(() => {
        if ($("dmStat").textContent.startsWith("файл отправлен"))
          $("dmStat").textContent = "диалог с " + DM.peer +
            " — видите только вы двое";
      }, 4000);
    } else {
      dmWarn(json.error || "не залито");
      $("dmStat").textContent = "диалог с " + DM.peer + " — видите только вы двое";
    }
  } catch(e){
    dmWarn("файл не залит: " + e);
  } finally { DM.uploading = false; }
}
/* Скачивание приватного файла: GET /dm_download/<file_id>. Сервер
   отдаёт только отправителю и получателю (X-Relay-From), остальным 403. */
async function dmDownloadFile(f){
  if (!f || !f.file_id) return;
  try {
    const r = await fetch("/dm_download/" + encodeURIComponent(f.file_id),
      {headers: baseHeaders(), ...fetchTimeout(120000)});
    if (!r.ok){
      const j = await safeJson(r);
      dmWarn((j && j.error) || ("ошибка " + r.status));
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = f.name || f.file_id;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch(e){ dmWarn("не скачалось: " + e); }
}

/* ── открыть ЛС с другом из профиля (profile.js вызывает эту) ── */
function openDmWith(name){
  if (typeof activateTab === "function") activateTab("dm");
  dmSetPeer(name);
}

/* ── события UI ── */
$("dmPeer").onchange = () => dmSetPeer($("dmPeer").value);
$("btnDmSend").onclick = sendDm;
$("dmText").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); sendDm(); }
});
/* v2.0.3: файл в ЛС — выбор файла сразу запускает отправку */
$("dmFileInput").onchange = uploadDmFile;
