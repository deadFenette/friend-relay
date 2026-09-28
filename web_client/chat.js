"use strict";
/* chat.js — лента общего чата и каналов, события, правки/удаления,
   реакции, пины, индикатор «печатает».

   Багфиксы v1.8.2 (по сравнению с одностраничным index.html):
   - событие edit читает new_text (раньше ev.text — правка СТИРАЛА текст
     сообщения, оставляя «(изменено)»);
   - реакции и пины теперь рисуются (раньше молча выбрасывались), свой
     реактив ставится кликом; свои сообщения можно править/удалять;
   - канал больше не перерисовывается целиком каждые 1.5с — инкрементальный
     дифф (скролл и позиция чтения истории не сбрасываются);
   - лента ограничена FEED_CAP бабблами — длинные сессии не течёт память;
   - веб-клиент теперь шлёт POST /typing (раньше индикатор «печатает»
     работал только в одну сторону: Qt видел, веб — нет).

   v2.0.3:
   - управление каналами прямо из веба: создать (＋) и удалить (🗑 —
     только создатель, сервер вернёт 403 not_creator);
   - панель закреплённых сообщений (GET /pinned) с прыжком к сообщению;
   - кнопка 📌 в действиях бабла — закрепить/открепить (POST /pin_message);
   - текст баблов рисуется мини-маркдауном (renderMsgMarkdown из core.js:
     **bold** · *italic* · `code` · ```блоки``` · > цитата · ссылки). */

/* быстрый набор реакций (совпадает с палитрой Qt-клиента по духу) */
const REACT_SET = ["\uD83D\uDC4D", "\u2764\uFE0F", "\uD83D\uDE02",
  "\uD83D\uDD25", "\uD83C\uDF89", "\uD83D\uDEAE"];

function nearBottom(el){
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}
/* ── v2.0.2: пилюля «вниз ↓» ──
   Видна, когда лента прокручена выше низа больше чем на 300px (чтение
   истории/канала). В скобках — счётчик непрочитанных, как на вкладке.
   Вызывается из addBubble/capFeed и scroll-хендлера в main.js. */
function updateJumpPill(){
  const feed = $("feed"), pill = $("jumpDown");
  if (!feed || !pill) return;
  const far = feed.scrollHeight - feed.scrollTop - feed.clientHeight > 300;
  pill.classList.toggle("hidden", !far);
  const cnt = $("jumpCnt");
  if (cnt) cnt.textContent = S.unread > 0 ? "+" + S.unread : "";
}
/* ── v2.0.2: поиск по загруженной ленте ──
   Фильтр НА КЛИЕНТЕ по баблам, которые уже в ленте (серверного полнотексто-
   го поиска нет — см. docs/PERFORMANCE.md, SQLite FTS5 в планах). Скрывает
   классом .hidden-by-search, ничего не удаляет: очистил поле — лента вернулась
   целиком, включая события, приехавшие во время поиска. Работает и в каналах. */
function applyFeedFilter(){
  const feed = $("feed");
  if (!feed) return;
  const q = ($("searchBox").value || "").trim().toLowerCase();
  if (!q){
    if (S.filterActive){
      /* сняли фильтр — вернуть всё разом */
      for (const el of feed.children)
        el.classList.remove("hidden-by-search");
      $("searchStat").textContent = "";
      S.filterActive = false;
    }
    return;
  }
  S.filterActive = true;
  let hits = 0;
  for (const el of feed.children){
    const txt = el.querySelector(".txt");
    const who = el.querySelector(".who .nm");
    const hay = ((txt ? txt.textContent : "") + " " +
                 (who ? who.textContent : "")).toLowerCase();
    const match = hay.includes(q);
    el.classList.toggle("hidden-by-search", !match);
    if (match) hits++;
  }
  $("searchStat").textContent = "найдено: " + hits;
}
function resetFeed(){
  const feed = $("feed");
  feed.innerHTML = "";
  S.msgs.clear(); S.meta.clear();
  S.chanSnapshot = [];
  S.oldestSeq = Infinity;
  S.filterActive = false;
  if ($("searchBox")) $("searchBox").value = "";
  if ($("searchStat")) $("searchStat").textContent = "";
  const loader = feed.querySelector(".loadmore");
  if (loader) loader.remove();
  updateJumpPill();
}

/* ── реакции: идемпотентные дельты (v1.9.7) ──
   Серверные text-события приходят с УЖЕ вшитым полем reactions (текущее
   состояние), а reaction-дельты — поверх. Дельта без флага «added» —
   слепой переключатель: повторное применение отматывает реакцию.
   Поэтому: (1) новые дельты несут явный флаг added — применяем его
   идемпотентно; (2) при полном пересбросе ленты (since=0, переподключение
   или перезагрузка страницы) дельты реакций ПРОПУСКАЮТСЯ — текст-события
   уже содержат итоговое состояние. Это чинит «поставил реакцию →
   перезагрузил страницу → реакция пропала». */

/* ── декор бабла: реакции, пин, признак правки ── */
function msgDecor(el, meta){
  if (!meta) return;
  const who = el.querySelector(".who");
  if (who){
    let pin = who.querySelector(".pin");
    if (meta.pinned && !pin){
      pin = document.createElement("span");
      pin.className = "pin";
      pin.title = "Закреплено";
      pin.appendChild(frIcon("pin"));
      who.insertBefore(pin, who.firstChild);
    } else if (!meta.pinned && pin){ pin.remove(); }
  }
  let rx = el.querySelector(".rx");
  if (!rx){
    rx = document.createElement("div");
    rx.className = "rx";
    el.appendChild(rx);
  }
  rx.innerHTML = "";
  const reactions = meta.reactions || {};
  for (const emoji of Object.keys(reactions)){
    const users = reactions[emoji] || [];
    if (!users.length) continue;
    const b = document.createElement("span");
    b.className = "rxn" + (users.includes(S.name) ? " mine" : "");
    b.textContent = emoji + " ";
    const cnt = document.createElement("span");
    cnt.className = "cnt"; cnt.textContent = users.length;
    b.appendChild(cnt);
    b.title = users.join(", ");
    if (!S.channel) b.onclick = () => reactTo(meta.seq, emoji);
    rx.appendChild(b);
  }
}
/* v1.9.7: оптимистичный отклик как в Qt — реакция видна мгновенно,
   серверная дельта с флагом added её только подтвердит. Сеть упала —
   откатываем тоггл и показываем предупреждение. */
function toggleLocalReaction(seq, emoji){
  const meta = S.meta.get(seq);
  const el = S.msgs.get(seq);
  if (!meta || !el) return false;
  const rx = meta.reactions || (meta.reactions = {});
  const users = rx[emoji] || (rx[emoji] = []);
  const i = users.indexOf(S.name);
  if (i >= 0) users.splice(i, 1); else users.push(S.name);
  if (!users.length) delete rx[emoji];
  msgDecor(el, meta);
  return true;
}
async function reactTo(seq, emoji){
  const toggled = toggleLocalReaction(seq, emoji);
  let ok = true, err = "";
  try {
    const {json} = await apiPost("/add_reaction", {seq, emoji});
    if (json && json.ok === false){ ok = false; err = json.error || "сервер отказал"; }
  } catch(e){ ok = false; err = String(e); }
  if (!ok){
    if (toggled) toggleLocalReaction(seq, emoji);   /* откат */
    warn("chatWarn", "реакция не ушла: " + err);
  }
}

/* ── кнопки-действия при наведении (только общий чат: правки/реакции —
      события сервера, в каналах их нет) ──
   v3.2: эмодзи-надписи заменены на монолайн-иконки (frIcon, спрайт
   icons.js) — принцип «никаких эмодзи в хроме» доведён до конца.
   Подпись живёт в title/aria-label — наведи и увидишь. */
function addMsgActions(el, meta){
  const acts = document.createElement("span");
  acts.className = "acts";
  const mkBtn = (icon, title) => {
    const b = document.createElement("button");
    b.type = "button";
    b.title = title;
    b.setAttribute("aria-label", title);
    b.appendChild(frIcon(icon));
    acts.appendChild(b);
    return b;
  };
  mkBtn("emoji", "Реакция").onclick = (e) => {
    e.stopPropagation();
    openReactPicker(e.currentTarget, meta.seq);
  };
  /* v2.0.2: копировать можно ЛЮБОЕ сообщение (своё и чужое) — расшифрованный
     текст уже лежит в meta.plain, файловые баблы копируют имя файла. */
  mkBtn("copy", "Скопировать текст").onclick = () => copyMsgText(meta.seq);
  /* v2.0.3: закрепить/открепить сообщение (судья — сервер, /pin_message) */
  mkBtn("pin", meta.pinned ? "Открепить" : "Закрепить")
    .onclick = () => pinMsg(meta.seq);
  if (meta.me && !meta.file){
    mkBtn("edit", "Исправить").onclick = () => editOwnMsg(meta.seq);
  }
  if (meta.me){
    mkBtn("trash", "Удалить").onclick = () => deleteOwnMsg(meta.seq);
  }
  el.appendChild(acts);
}
function closeReactPicker(){
  const p = document.querySelector(".rpicker");
  if (p) p.remove();
}
function openReactPicker(anchor, seq){
  closeReactPicker();
  const p = document.createElement("div");
  p.className = "rpicker";
  p.setAttribute("role", "menu");
  /* v3.5.5: вид пилюли живёт в style.css (.rpicker) — раньше стили были
     инлайном, и hover на эмодзи был невозможен в принципе */
  p.style.cssText = "position:fixed;z-index:60;";
  for (const emoji of REACT_SET){
    const b = document.createElement("span");
    b.textContent = emoji;
    b.setAttribute("role", "button");
    b.setAttribute("tabindex", "0");
    b.onclick = () => { closeReactPicker(); reactTo(seq, emoji); };
    /* v1.9.5: с клавиатуры (Tab + Enter/Space) — раньше эмодзи были
       нефокусируемыми спэнами, пикер не открывался без мыши. */
    b.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " "){
        e.preventDefault();
        closeReactPicker(); reactTo(seq, emoji);
      }
    };
    p.appendChild(b);
  }
  document.body.appendChild(p);
  const r = anchor.getBoundingClientRect();
  p.style.left = Math.min(r.left, innerWidth - 180) + "px";
  p.style.top = Math.max(0, r.top - 40) + "px";
  const esc = (e) => {
    if (e.key === "Escape"){ closeReactPicker();
      document.removeEventListener("keydown", esc); }
  };
  document.addEventListener("keydown", esc);
  setTimeout(() => {
    const away = (e) => {
      if (!p.contains(e.target)){ closeReactPicker();
        document.removeEventListener("mousedown", away); }
    };
    document.addEventListener("mousedown", away);
  }, 0);
  const first = p.querySelector("span");
  if (first) first.focus();
}
async function editOwnMsg(seq){
  const meta = S.meta.get(seq);
  /* v3.2: своя модалка вместо системного prompt() — тема и акцент
     наследуются, Enter подтверждает, Esc отменяет (см. modal.js) */
  const nv = await frPrompt({
    title: "Исправить сообщение",
    value: meta ? (meta.plain || "") : "",
    maxlength: 4000,
    ok: "Исправить",
  });
  if (nv === null) return;
  const cleaned = nv.trim().slice(0, 4000);
  if (!cleaned){ warn("chatWarn", "Пустое сообщение нельзя"); return; }
  try {
    const {json} = await apiPost("/edit_text", {seq, text: cleaned});
    if (!json.ok) warn("chatWarn", json.error || "не поправилось");
  } catch(e){ warn("chatWarn", "не поправилось: " + e); }
}
async function deleteOwnMsg(seq){
  const ok = await frConfirm({
    title: "Удалить сообщение?",
    body: "Оно пропадёт для всех участников чата.",
    ok: "Удалить", danger: true,
  });
  if (!ok) return;
  try {
    const {json} = await apiPost("/delete_event", {seq});
    if (!json.ok) warn("chatWarn", json.error || "не удалилось");
  } catch(e){ warn("chatWarn", "не удалилось: " + e); }
}
/* v2.0.3: закрепить/открепить сообщение. Сервер сам тогглит и шлёт
   событие pin — но мы обновляем meta сразу, не дожидаясь поллинга. */
async function pinMsg(seq){
  try {
    const {json} = await apiPost("/pin_message", {seq});
    if (!json.ok){ warn("chatWarn", json.error || "не закрепилось"); return; }
    const meta = S.meta.get(seq);
    const el = S.msgs.get(seq);
    if (meta){
      meta.pinned = !!json.pinned;
      if (el) msgDecor(el, meta);
      /* пересобрать надписи кнопок действий (Закрепить↔Открепить) */
      if (el){ const acts = el.querySelector(".acts");
        if (acts) acts.remove();
        if (!S.channel) addMsgActions(el, meta); }
    }
    toast(meta && meta.pinned ? "Сообщение закреплено"
      : "Сообщение откреплено");
  } catch(e){ warn("chatWarn", "не закрепилось: " + e); }
}
/* v2.0.2: копирование текста бабла в буфер (с fallback'ом на execCommand —
   clipboard.writeText живёт только в secure context, а чат бывает и на http) */
function copyMsgText(seq){
  const meta = S.meta.get(seq);
  const text = (meta && meta.plain) || "";
  if (!text) return;
  const done = () => toast("Скопировано");
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done).catch(() => copyFallback(text, done));
  } else copyFallback(text, done);
}
function copyFallback(text, done){
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;left:-9999px";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    done();
  } catch(e){ toast("Не удалось скопировать", "err"); }
}

/* ── v3: клики по in-app ссылкам friendrelay:// в сообщениях ──
   Ставим ОДИН делегированный обработчик на document (а не по каждой
   ссылке отдельно) — баблы добавляются и удаляются динамически, проще
   ловить через всплытие.
   Поддерживается:
     friendrelay://chess/join/<CODE>  — открыть подвид шахмат и
                                        присоединиться к матчу. */
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href^='friendrelay://']");
  if (!a) return;
  e.preventDefault();
  const url = a.getAttribute("href");
  handleFriendRelayLink(url);
});

function handleFriendRelayLink(url){
  if (typeof url !== "string") return;
  url = url.trim();
  if (url.startsWith("friendrelay://chess/join/")){
    let code = url.slice("friendrelay://chess/join/".length);
    code = code.split("/")[0].split("?")[0].split("#")[0].toUpperCase();
    if (!code){ toast("В ссылке нет кода матча", "err"); return; }
    joinChessByCode(code);
    return;
  }
  if (url.startsWith("friendrelay://go/join/")){
    let code = url.slice("friendrelay://go/join/".length);
    code = code.split("/")[0].split("?")[0].split("#")[0].toUpperCase();
    if (!code){ toast("В ссылке нет кода матча", "err"); return; }
    joinGoByCode(code);
    return;
  }
  toast("Неизвестная ссылка: " + url, "err");
}

/* v3: присоединиться к шахматному матчу по коду из ссылки-приглашения.
   - Если мы ещё не на вкладке «Игры» — переключаемся.
   - Открываем подвид шахмат (если ещё не открыт).
   - Вставляем код в поле и дёргаем join.
   - chessCmd использует /bot_command, ответ silent=True → в общий чат
     ничего не идёт (нет спама). */
function joinChessByCode(code){
  if (!$("app") || $("app").classList.contains("hidden")){
    toast("Сначала подключись к серверу", "err");
    return;
  }
  /* активируем вкладку Games (this переключит и подгрузит лаунчер) */
  if (typeof activateTab === "function"){
    activateTab("games");
  }
  /* открываем подвид шахмат */
  const chessCard = $("chessCard");
  if (chessCard) chessCard.click();
  /* вставляем код в поле и джойним */
  setTimeout(() => {
    const codeInput = $("chessCode");
    if (codeInput) codeInput.value = code;
    if (typeof doChessJoin === "function"){
      doChessJoin();
    } else if (typeof chessCmd === "function"){
      chessCmd("join", [code]);
    }
  }, 250);
}

/* v3.1: присоединиться к матчу Го по коду из ссылки-приглашения.
   Го в веб-клиенте живёт в games/go.html (iframe-игра). Открываем
   окно игры с автоджойном: games.js пошлёт go_auto_join в fr_ctx,
   go.html переключится в NET-режим и вызовет netJoin(code). */
function joinGoByCode(code){
  if (!$("app") || $("app").classList.contains("hidden")){
    toast("Сначала подключись к серверу", "err");
    return;
  }
  /* игра Го лежит на /games/go (см. lib/server/http_api.py:_GAMES). */
  const goGame = {
    id: "go",
    title: "Го",
    url: "/games/go",
    mode: "multi",
    available: true,
  };
  /* пытаемся найти игру в уже загруженном GM.list (точно знает url) */
  if (typeof GM !== "undefined" && GM.list){
    const found = GM.list.find((g) => g.id === "go" || g.id === "go");
    if (found) Object.assign(goGame, found);
  }
  /* открываем окно игры с автоджойном */
  if (typeof openGameWithAutoJoin === "function"){
    openGameWithAutoJoin(goGame, "go", code);
  } else if (typeof openGame === "function"){
    openGame(goGame);
    toast("Вставь код " + code + " в поле и нажми «Присоединиться»");
  } else {
    toast("Не удалось открыть игру Го", "err");
  }
}

/* ── вставка бабла ── */
function addBubble(o){
  const feed = $("feed");
  const stick = nearBottom(feed);
  let el;
  const meta = {
    seq: o.seq || 0, encrypted: !!(o.ev && o.ev.encrypted),
    iv: o.ev ? o.ev.iv : undefined, file: !!o.file,
    me: !!o.me, reactions: (o.ev && o.ev.reactions) || {},
    pinned: !!(o.ev && o.ev.pinned), edited: !!(o.ev && o.ev.edited),
    plain: (typeof o.text === "string" && !o.file) ? o.text : "",
  };
  if (o.system){
    el = document.createElement("div");
    el.className = "sysline";
    el.textContent = o.text;
  } else {
    el = document.createElement("div");
    el.className = "msg" + (o.me ? " me" : "") + (o.file ? " file" : "");
    /* v1.9.6: цветной кружок с инициалами (как в приложении) — лента
       читается быстрее, чем «имя + время + текст» */
    if (typeof avatarColor === "function" && !o.me){
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
    /* v1.9.7: имя и время — раздельные спэны: имя окрашивается в цвет
       автора (как аватарка), время приглушается */
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = o.from;
    if (typeof avatarColor === "function" && !o.me)
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
      const a = document.createElement("a");
      a.href = "#";
      /* v3.2: иконка-скрепка из спрайта вместо эмодзи — единый хром */
      a.appendChild(frIcon("attach"));
      a.appendChild(document.createTextNode(" " + o.text.name));
      a.onclick = (e) => { e.preventDefault(); downloadFile(o.text); };
      txt.appendChild(a);
      const meta2 = document.createElement("div");
      meta2.className = "hint";
      meta2.textContent = fmtSize(o.text.size) +
        (o.text.sha256 ? " · sha256 ok" : "");
      txt.appendChild(meta2);
    } else {
      /* v2.0.3: мини-маркдаун вместо голого textContent — тот же
         набор разметки, что в Qt (renderMsgMarkdown безопасен: esc ДО разметки) */
      renderMsgMarkdown(txt, o.text + (meta.edited ? " (изменено)" : ""));
    }
    body.appendChild(who); body.appendChild(txt);
    el.appendChild(body);
    if (o.seq){
      S.meta.set(o.seq, meta);
      if (!S.channel) addMsgActions(el, meta);
    }
    msgDecor(el, meta);
  }
  /* v3.5.9: дата бабла в data-атрибуте — скину «Ночник» этого достаточно,
     чтобы рисовать дневные метки (см. nochnik.js reconcileDays) */
  if (o.ts) el.dataset.ts = String(o.ts);
  if (o.seq){
    el.dataset.seq = o.seq;
    S.msgs.set(o.seq, el);
    if (o.seq < S.oldestSeq) S.oldestSeq = o.seq;
  }
  const me = !!o.me;
  feed.appendChild(el);
  capFeed();
  if (stick) feed.scrollTop = feed.scrollHeight;
  else if (me) feed.scrollTop = feed.scrollHeight;
  /* v1.9.5: непрочитанные. Вкладка неактивна (document.hidden) или юзер
     читает историю выше — считаем и показываем бейдж на вкладке «Чат» и в
     document.title. Сбрасывается при возврате на вкладку + скролле вниз. */
  if (!me && !o.system && !feedLoadPending &&
      (document.hidden || !stick) && !S.channelOpen){
    S.unread++;
    updateUnreadBadge();
    /* v2.0.2: звук «динь» о новом сообщении, когда вкладка НЕ активна
       (двухтональный, как в Qt v1.9.6). Пока читаешь ленту — молчим. */
    if (document.hidden && typeof notifyEnabled === "function" && notifyEnabled())
      notifyBeep();
  }
  updateJumpPill();
  if (S.filterActive) applyFeedFilter();   /* новый бабл проходит через активный фильтр */
}
/* v2.0.2: короткий двухтональный «динь» (частоты как в qt_app/sound.py):
   AudioContext создаётся на лету; при заблокированном автоплее просто молчим. */
function notifyBeep(){
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const gain = ctx.createGain();
    gain.gain.value = 0.08;
    gain.connect(ctx.destination);
    const tone = (freq, at, dur) => {
      const osc = ctx.createOscillator();
      osc.type = "sine";
      osc.frequency.value = freq;
      osc.connect(gain);
      osc.start(ctx.currentTime + at);
      osc.stop(ctx.currentTime + at + dur);
    };
    tone(880, 0, 0.12);
    tone(1174.7, 0.12, 0.18);   /* ре-диез — мягкое завершение */
    setTimeout(() => { try { ctx.close(); } catch(e){} }, 600);
  } catch(e){ /* звук не критичен */ }
}
let feedLoadPending = false;
function updateUnreadBadge(){
  const tab = document.querySelector('.tabs button[data-tab="chat"]');
  if (tab){
    let b = tab.querySelector(".badge");
    if (S.unread > 0){
      if (!b){
        b = document.createElement("span");
        b.className = "badge";
        tab.appendChild(b);
      }
      b.textContent = S.unread > 99 ? "99+" : String(S.unread);
    } else if (b) b.remove();
  }
  const base = S.name ? S.name + " · Friend Relay" : "Friend Relay";
  document.title = S.unread > 0 ? `(${S.unread}) ` + base : base;
}
function clearUnread(){
  S.unread = 0;
  updateUnreadBadge();
  updateJumpPill();
}
function capFeed(){
  const feed = $("feed");
  while (feed.children.length > FEED_CAP){
    const first = feed.firstChild;
    if (!first) break;
    const sq = first.dataset ? first.dataset.seq : "";
    if (sq){ S.msgs.delete(+sq); S.meta.delete(+sq); }
    feed.removeChild(first);
  }
}

/* ── применение событий общего чата (поллинг /events) ── */
function applyEvent(ev){
  const kind = ev.kind;
  if (kind === "text"){
    const body = displayText(ev);
    addBubble({seq: ev.seq, from: ev.from, text: body, ts: ev.ts,
      me: ev.from === S.name, ev});
  } else if (kind === "file"){
    addBubble({seq: ev.seq, from: ev.from, ts: ev.ts, file: true,
      me: ev.from === S.name, ev,
      text: {file_id: ev.file_id, name: ev.name, size: ev.size,
        sha256: ev.sha256}});
  } else if (kind === "system"){
    addBubble({text: ev.text || "(system)", system: true});
  } else if (kind === "edit"){
    /* ФИКС: сервер шлёт new_text (lib/domain/event_store.py), раньше
       читалось несуществующее ev.text — текст стирался */
    const el = S.msgs.get(ev.target_seq);
    const meta = S.meta.get(ev.target_seq);
    if (el){
      let nt = (ev.new_text != null) ? ev.new_text : (ev.text || "");
      let body = nt;
      if (meta && meta.encrypted && typeof nt === "string" &&
          nt.startsWith("v2:")){
        const pt = decryptEventText({encrypted: true, text: nt});
        body = (pt === null || pt === "")
          ? "\uD83D\uDD12 [не удалось расшифровать]" : pt;
      }
      const txt = el.querySelector(".txt");
      if (txt) renderMsgMarkdown(txt, body + " (изменено)");
      if (meta){ meta.edited = true; meta.plain = body; }
    }
  } else if (kind === "delete"){
    const el = S.msgs.get(ev.target_seq);
    if (el){
      const txt = el.querySelector(".txt");
      if (txt) txt.textContent = "… удалено";
      const acts = el.querySelector(".acts");
      if (acts) acts.remove();
      S.meta.delete(ev.target_seq);
    }
  } else if (kind === "reaction"){
    /* v1.9.7: дельта несёт явный флаг added — применение идемпотентно
       (оптимистичный тоггл в reactTo + подтверждение сервером больше
       не отматывают друг друга). Легаси-дельты без флага — переключение. */
    const el = S.msgs.get(ev.target_seq);
    const meta = S.meta.get(ev.target_seq);
    if (el && meta && ev.emoji){
      const rx = meta.reactions || (meta.reactions = {});
      const users = rx[ev.emoji] || (rx[ev.emoji] = []);
      const have = users.indexOf(ev.from) >= 0;
      const want = (typeof ev.added === "boolean") ? ev.added : !have;
      if (want && !have) users.push(ev.from);
      else if (!want && have) users.splice(users.indexOf(ev.from), 1);
      if (!users.length) delete rx[ev.emoji];
      msgDecor(el, meta);
    }
  } else if (kind === "pin"){
    const el = S.msgs.get(ev.target_seq);
    const meta = S.meta.get(ev.target_seq);
    if (el && meta){ meta.pinned = !!ev.pinned; msgDecor(el, meta); }
  } else if (kind === "link_preview"){
    /* v1.9.5: превью ссылки подъехало (Qt уже рисует его) — раньше
       веб-клиент молча выбрасывал эти события. */
    const el = S.msgs.get(ev.target_seq);
    if (el && ev.link_preview && !el.querySelector(".lprev")){
      const pv = ev.link_preview;
      const box = document.createElement("a");
      box.className = "lprev";
      box.href = pv.url || "#";
      box.target = "_blank"; box.rel = "noopener noreferrer";
      const t = document.createElement("div");
      t.className = "lprev-t";
      t.textContent = pv.title || pv.url || "";
      box.appendChild(t);
      if (pv.description){
        const d = document.createElement("div");
        d.className = "lprev-d";
        d.textContent = pv.description.slice(0, 200);
        box.appendChild(d);
      }
      if (pv.image){
        const img = document.createElement("img");
        img.className = "lprev-i"; img.src = pv.image; img.alt = "";
        img.loading = "lazy"; img.referrerPolicy = "no-referrer";
        box.prepend(img);
      }
      el.appendChild(box);
    }
  }
}

/* ── отправка ──
   v1.9.5:
   - E2E-шифрование исходящих (зеркало Qt): при готовом ключе текст
     уходит как {text: "v2:…", encrypted: true, iv};
   - in-flight guard: двойной Enter не даёт два одинаковых сообщения;
   - при неудаче текст ВОЗВРАЩАЕТСЯ в инпут (раньше стирался до запроса —
     сеть моргнула и набранное пропадало безвозвратно). */
async function sendMsg(){
  warn("chatWarn", "");
  if (S.sending) return;
  const text = $("msgText").value.trim();
  if (!text) return;
  S.sending = true;
  try {
    let payload;
    if (S.channel){
      payload = {channel: S.channel, text};
      if (S.encReady){
        const enc = encryptEventText(text);
        if (enc) payload = {channel: S.channel, text: enc.text,
          encrypted: true, iv: enc.iv};
      }
      const {json} = await apiPost("/channel/send", payload);
      if (!json.ok){
        warn("chatWarn", json.error || "ошибка отправки");
        restoreInput(text);
      } else {
        $("msgText").value = "";
        await loadChannelMessages();
      }
    } else {
      payload = {text};
      if (S.encReady){
        const enc = encryptEventText(text);
        if (enc) payload = {text: enc.text, encrypted: true, iv: enc.iv};
      }
      const {json} = await apiPost("/send_text", payload);
      if (json.ok) $("msgText").value = "";
      else { warn("chatWarn", json.error || "ошибка отправки"); restoreInput(text); }
      await pollTick();
    }
  } catch(e){
    warn("chatWarn", "не отправилось: " + e);
    restoreInput(text);
  } finally { S.sending = false; }
}
function restoreInput(text){
  const inp = $("msgText");
  if (inp && !inp.value) inp.value = text;
}
/* «печатает…»: троттлинг 2с, как TYPING_SEND_EVERY в Qt-чате */
function sendTypingSoon(){
  if (!S.token) return;
  const now = Date.now();
  if (now - S.typingSentAt < 2000) return;
  S.typingSentAt = now;
  apiPost("/typing", {}).catch(() => {});
}

/* ── каналы ──
   v1.9.5: селектор перестраивается ТОЛЬКО при реальном изменении списка
   имён — раньше каждые 5с innerHTML="" рвал открытый dropdown и
   сбрасывал выбор.
   v2.0.3: попутно запоминаем создателя канала (S.chanCreator) — от этого
   зависит видимость кнопки 🗑 (удалять может только создатель, сервер
   вернёт 403 not_creator, но кнопку лучше вообще не показывать). */
async function loadChannels(){
  try {
    const {json} = await apiGet("/channels");
    const sel = $("channelSel");
    const chans = (json.channels || [])
      .map(c => (typeof c === "string" ? {name: c} : c))
      .filter(c => c.name);
    const names = chans.map(c => c.name);
    if (names.join("\n") === (S.chanNames || "")) return;
    S.chanNames = names.join("\n");
    S.chanCreator = {};
    for (const c of chans) S.chanCreator[c.name] = c.creator || "";
    sel.innerHTML = '<option value=""># общий чат</option>';
    for (const nm of names){
      const o = document.createElement("option");
      o.value = nm; o.textContent = "# " + nm;
      /* создатель — в тултип опции: сразу видно, чей это канал */
      const cr = S.chanCreator[nm];
      if (cr) o.title = "создал: " + cr;
      sel.appendChild(o);
    }
    /* ребилд списка не должен сбрасывать текущий выбор
       (v1.8.2: список теперь обновляется и ПОСЛЕ подключения) */
    if (S.channel && names.includes(S.channel)) sel.value = S.channel;
    else if (S.channel && !names.includes(S.channel)){
      /* канал удалили на сервере — возвращаемся в общий чат честно */
      sel.value = "";
    }
    updateChanDelBtn();
  } catch(e){}
}
/* v2.0.3: кнопка 🗑 видна только в канале, который создал ты.
   В общем чате удалять нечего — сервер тоже бы отказал. */
function updateChanDelBtn(){
  const del = $("btnChanDel");
  if (!del) return;
  const mine = S.channel &&
    (S.chanCreator || {})[S.channel] === S.name;
  del.classList.toggle("hidden", !mine);
}
/* v2.0.3: создать канал (＋ рядом с селектором) и сразу открыть его.
   v3.2: своя модалка вместо prompt(). */
async function createChannel(){
  warn("chatWarn", "");
  const name = (await frPrompt({
    title: "Создать канал",
    body: "Имя увидят все участники — латиницей или по-русски.",
    placeholder: "напр. meme-dump",
    maxlength: 32,
    ok: "Создать",
  }) || "").trim();
  if (!name) return;
  try {
    const {json} = await apiPost("/channel/create", {name});
    if (!json.ok){
      warn("chatWarn", json.error || "не создалось");
      return;
    }
    toast("Канал создан: #" + name);
    await loadChannels();
    /* сервер не обязан вернуть имя — открываем то, что просили */
    openChannel(name);
  } catch(e){ warn("chatWarn", "не создалось: " + e); }
}
/* v2.0.3: удалить текущий канал (только создатель — проверит сервер).
   v3.2: confirm → своя модалка с красной кнопкой (danger). */
async function deleteChannel(){
  warn("chatWarn", "");
  if (!S.channel) return;
  const ok = await frConfirm({
    title: "Удалить канал #" + S.channel + "?",
    body: "Сообщения канала пропадут для всех участников — отмены не будет.",
    ok: "Удалить канал", danger: true,
  });
  if (!ok) return;
  try {
    const {json} = await apiPost("/channel/delete", {channel: S.channel});
    if (!json.ok){
      warn("chatWarn", json.error || "не удалилось");
      return;
    }
    toast("Канал удалён: #" + json.name);
    S.channel = ""; S.channelOpen = false;
    resetFeed();
    S.since = 0;
    await loadChannels();
    await pollTick();
  } catch(e){ warn("chatWarn", "не удалилось: " + e); }
}
/* v2.0.3: открыть канал по имени — единый путь для селектора,
   создания канала и будущих вызовов. Вынесено из channelSel.onchange. */
function openChannel(name){
  S.channel = name || "";
  S.channelOpen = !!S.channel;
  $("channelSel").value = S.channel;
  resetFeed();
  clearUnread();
  updateChanDelBtn();
  if (!S.channel){
    S.since = 0;
    pollTick();
  } else {
    loadChannelMessages(true);
  }
}
/* Канал: раньше каждые 1.5с лента СТРОИЛАСЬ ЗАНОВО (innerHTML="") —
   скролл сбрасывало на низ, историю читать было невозможно. Теперь
   инкрементальный дифф: новые сообщения дописываются, изменившиеся
   обновляются на месте, пропавшие удаляются.
   v1.9.5:
   - S.chanReq: счётчик запросов — устаревший ответ /channel/messages
     (медленная сеть, гонка с только что отправленным сообщением) больше
     не удаляет свежие баблы из ленты;
   - сообщения, выпавшие из 50-сообщечного окна сервера (история
     подросла), больше НЕ удаляются из ленты — удаляются только
     реально удалённые на сервере. */
async function loadChannelMessages(force){
  if (!S.channel) return;
  const my = ++S.chanReq;
  try {
    const {json} = await apiGet("/channel/messages?channel="
      + encodeURIComponent(S.channel) + "&limit=50");
    if (my !== S.chanReq) return;   /* приехал более свежий ответ — этот устарел */
    applyChannelSnapshot((json && json.messages) || [], force);
  } catch(e){}
}
function applyChannelSnapshot(msgs, force){
  const feed = $("feed");
  const prev = force ? [] : (S.chanSnapshot || []);
  const newSeqs = new Set(msgs.map(m => m.seq));
  const oldest = msgs.length ? msgs[0].seq : Infinity;
  for (const m of prev){
    /* Удаляем только те, что ЖИЛИ в окне сервера и пропали из него
       (реально удалённые). Выпавшие из окна (seq < oldest) — история,
       которая просто уехала за лимит 50: оставляем в ленте. */
    if (!newSeqs.has(m.seq) && m.seq >= oldest){
      const el = S.msgs.get(m.seq);
      if (el){ el.remove(); S.msgs.delete(m.seq); S.meta.delete(m.seq); }
    }
  }
  const prevBySeq = new Map(prev.map(m => [m.seq, m]));
  for (const m of msgs){
    const old = prevBySeq.get(m.seq);
    const same = old && JSON.stringify(old) === JSON.stringify(m);
    const el = S.msgs.get(m.seq);
    if (el && same) continue;
    if (el){
      const meta = S.meta.get(m.seq) || {seq: m.seq};
      meta.reactions = m.reactions || {};
      meta.pinned = !!m.pinned;
      meta.encrypted = !!m.encrypted;
      meta.iv = m.iv;
      meta.edited = !!m.edited;
      meta.plain = displayText(m);
      const txt = el.querySelector(".txt");
      if (txt) renderMsgMarkdown(txt, meta.plain + (meta.edited ? " (изменено)" : ""));
      S.meta.set(m.seq, meta);
      msgDecor(el, meta);
    } else {
      addBubble({seq: m.seq, from: m.from, text: displayText(m), ts: m.ts,
        me: m.from === S.name, ev: m});
    }
  }
  if (msgs.length) S.oldestSeq = Math.min(S.oldestSeq, msgs[0].seq);
  if (!msgs.length){
    if (!feed.querySelector(".sysline"))
      addBubble({text: "в канале пока пусто", system: true});
  } else {
    const sys = feed.querySelector(".sysline");
    if (sys) sys.remove();
  }
  S.chanSnapshot = msgs;
}

/* ── шапка: онлайн/печатает ── */
function renderOnline(){
  $("online").textContent = S.online.length
    ? "в сети: " + S.online.length + " (" + S.online.join(", ") + ")" : "";
}
/* v1.9.7: индикатор «печатает» — пилюля с прыгающими точками
   (раньше — сухая текстовая строка, которую никто не замечал) */
function renderTyping(){
  const el = $("typing");
  if (!S.typing.length){ el.innerHTML = ""; el.classList.remove("typing-pill"); return; }
  el.classList.add("typing-pill");
  el.innerHTML = "";
  const dots = document.createElement("span");
  dots.className = "tdots";
  for (let i = 0; i < 3; i++){
    const d = document.createElement("i");
    d.style.setProperty("--i", String(i));
    dots.appendChild(d);
  }
  el.appendChild(dots);
  el.appendChild(document.createTextNode(" " + S.typing.join(", ") +
    (S.typing.length > 1 ? " печатают…" : " печатает…")));
}

/* ── подгрузка старых сообщений (v1.9.5) ──
   Скролл к самому верху ленты общего чата дергает GET /events?before=…
   (Qt-клиент так делает с самого ввода этой фичи на сервере). Вставляем
   батч НАД лентой и удерживаем позику чтения: сколько добавили сверху —
   столько и докручиваем вниз. В каналах сервер не поддерживает before —
   там кнопка не показывается. */
async function loadOlderMessages(){
  if (S.channelOpen || feedLoadPending) return;
  if (S.oldestSeq === Infinity || S.oldestSeq <= 1) return;
  feedLoadPending = true;
  try {
    const {json} = await apiGet("/events?before=" + S.oldestSeq + "&count=50");
    if (!json || !json.ok || !json.events) return;
    const events = json.events;
    if (!events.length) return;
    const feed = $("feed");
    const scrollBefore = feed.scrollHeight;
    /* addBubble дописывает элементы в конец ленты: собираем их и
       переставляем НАД текущим верхом, сохраняя хронологию батча
       (сервер отдаёт ascending — от старых к новым). */
    const anchor = feed.firstChild;   /* текущий верх ленты (может быть null) */
    const batchEls = [];
    for (const ev of events){
      if (ev.kind !== "text" && ev.kind !== "file") continue;
      const wasNew = !S.msgs.has(ev.seq);
      if (ev.kind === "text"){
        addBubble({seq: ev.seq, from: ev.from, text: displayText(ev),
          ts: ev.ts, me: ev.from === S.name, ev});
      } else {
        addBubble({seq: ev.seq, from: ev.from, ts: ev.ts, file: true,
          me: ev.from === S.name, ev,
          text: {file_id: ev.file_id, name: ev.name, size: ev.size,
            sha256: ev.sha256}});
      }
      const el = S.msgs.get(ev.seq);
      if (el && (wasNew || !batchEls.includes(el))) batchEls.push(el);
    }
    for (const el of batchEls) feed.insertBefore(el, anchor);
    /* позиция чтения не прыгает: докручиваем ровно на добавленную высоту */
    feed.scrollTop = feed.scrollHeight - scrollBefore;
  } finally { feedLoadPending = false; }
}

/* ── панель закреплённых сообщений (v2.0.3) ──
   GET /pinned отдаёт ВСЕ закрепы сервера — включая те, что давно выпали
   из ленты (лента ограничена FEED_CAP и хвостом /events). Клик по строке
   прыгает к баблу, если он сейчас загружен; иначе честно говорим, что
   сообщение в глубине истории (подгрузка идёт скроллом вверх). */
function togglePinPanel(show){
  const panel = $("pinPanel");
  if (!panel) return;
  const want = (typeof show === "boolean") ? show : panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !want);
  if (want) loadPins();
}
async function loadPins(){
  const list = $("pinList");
  if (!list) return;
  list.innerHTML = '<div class="hint" style="padding:4px 8px">загружаю…</div>';
  try {
    const {json} = await apiGet("/pinned");
    const pins = (json && json.pinned) || [];
    list.innerHTML = "";
    if (!pins.length){
      list.innerHTML = '<div class="hint" style="padding:6px 8px">' +
        "Пока ничего не закреплено. Наведи курсор на сообщение в чате " +
        "и нажми кнопку с булавкой.</div>";
      return;
    }
    for (const p of pins){
      const row = document.createElement("div");
      row.className = "pinrow";
      const inFeed = S.msgs.has(p.seq);
      if (!inFeed) row.classList.add("off");
      const who = document.createElement("span");
      who.className = "pwho";
      who.textContent = p.from || "?";
      who.style.color = (typeof avatarColor === "function")
        ? avatarColor(p.from) : "";
      const tm = document.createElement("span");
      tm.className = "ptm";
      tm.textContent = p.ts ? fmtTime(p.ts) : "";
      const txt = document.createElement("span");
      txt.className = "ptxt";
      txt.textContent = (typeof p.text === "string" && p.text)
        ? p.text : "(файл)";
      /* зашифрованные пины расшифровываем тем же путём, что и ленту */
      if (p.encrypted) txt.textContent = displayText(p) || "(файл)";
      const jump = document.createElement("span");
      jump.className = "pjump";
      jump.textContent = inFeed ? "→ к сообщению" : "не в ленте";
      if (inFeed) row.onclick = () => {
        togglePinPanel(false);
        jumpToSeq(p.seq);
      };
      else row.title = "Сообщение глубже в истории — подгрузи её скроллом вверх";
      row.appendChild(who); row.appendChild(tm);
      row.appendChild(txt); row.appendChild(jump);
      list.appendChild(row);
    }
  } catch(e){
    list.innerHTML = '<div class="warn" style="padding:6px 8px">' +
      "не удалось получить закрепы: " + esc(String(e)) + "</div>";
  }
}
/* Прыжок к сообщению в ленте: центр ленты + разовая подсветка рамкой.
   scrollTop считается в координатах ленты (offsetTop обоих элементов
   относительно общего offsetParent — #tab-chat с position:relative). */
function jumpToSeq(seq){
  const feed = $("feed");
  const el = S.msgs.get(seq);
  if (!feed || !el) return;
  feed.scrollTop = el.offsetTop - feed.offsetTop
    - feed.clientHeight / 2 + el.clientHeight / 2;
  el.classList.remove("flash");
  /* перезапуск CSS-анимации: без reflow повторный клик не мигнёт */
  void el.offsetWidth;
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1700);
}
