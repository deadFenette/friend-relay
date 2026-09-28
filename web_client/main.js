"use strict";
/* main.js — подключение, цикл поллинга, вкладки, обработчики UI.
   Загружается ПОСЛЕДНИМ (после core/chat/files/profile/chess/voice).

   Багфиксы v1.8.2:
   - повторный клик «Подключиться» во время подключения больше не
     создаёт вторую сессию (кнопка блокируется + реентерабельность);
   - при возврате в общий чат селектор каналов синхронизируется;
   - ошибки API (ключ не принят / сессия) больше не глотаются молча —
     видно «нет связи: …» вместо вечного «подключено»;
   - Enter в поле ключа шифрования тоже подключает.

   v2.0.2:
   - темы оформления (Aurora / Lukewarm Ocean) — data-theme на <html>,
     выбор в localStorage «wr_theme» (см. style.css);
   - вкладка «ЛС» — личные сообщения в браузере (dm.js);
   - звук-уведомление о новых сообщениях, когда вкладка не активна.

   v2.0.3:
   - третья тема Cherry Grove (светлая) — цикл кнопки 🌌→🌊→🌸→🌌;
   - управление каналами (＋ / 🗑) и панель пинов (📌) — обработчики
     в chat.js, здесь только проводка кликов;
   - бейдж непрочитанных ЛС на вкладке «✉ ЛС» (dm.js, dmBadgeTick).

   v2.0.5:
   - вкладка «🎮 Игры» — лаунчер + окно игры в iframe (games.js);
     закрытие окна по ✕/Esc, Esc гасит только открытое окно игры. */

/* ───────────────────────── темы (v2.0.2, v2.0.3, Studio v3) ─────────────────────────
   Тема — атрибут data-theme на <html>: CSS в style.css читает его в
   :root[data-theme=…]. «Aurora» — отсутствие атрибута (дефолт, тёмная).
   Кнопка показывает, НА ЧТО переключит (следующую по кругу тему):
   Aurora → 🌊 Lukewarm Ocean → ❄ Snow (светлая, белая) → 🌸 Sakura
   → 🍒 Cherry Grove → 🍮 Crème Brûlée → 🌌 Aurora.
   Выбор живёт в localStorage. color-scheme тоже переключаем: в светлых
   темах браузер иначе рисует нативные контролы (скроллы, select). */
const THEME_KEY = "wr_theme";
/* v3.2: иконка темы ставится напрямую из спрайта (frIcon) — раньше
   main.js писал в кнопку эмодзи и полагался на MutationObserver в
   icons.js, но guard data-iconDone блокировал повторную подмену —
   эмодзи оставался рядом с иконкой. Теперь детерминированно. */
/* v3.2.1: иконки тем выровнены по смыслу — aurora (тёмная) → луна,
   ocean → волна, snow → снежинка, sakura → цветок, cherry → ягода
   (вишня всё-таки ягода, а не цветок), creme-brulee → креман с
   треснувшей карамелью. icons.js подменяет их на SVG (EMOJI_MAP). */
const THEME_ICON = {
  "aurora": "moon", "lukewarm-ocean": "wave",
  "snow": "snow", "cherry-grove": "cherry",
  "sakura": "flower", "creme-brulee": "creme",
};
const THEMES = [
  {name: "aurora",         icon: "🌌", label: "Aurora · тёмная"},
  {name: "lukewarm-ocean", icon: "🌊",  label: "Lukewarm Ocean · океан"},
  {name: "snow",           icon: "❄",  label: "Snow · белая"},
  {name: "sakura",         icon: "🌸", label: "Sakura · сакура"},
  {name: "cherry-grove",   icon: "🍒", label: "Cherry Grove · вишня"},
  {name: "creme-brulee",   icon: "🍮", label: "Crème Brûlée · крем-брюле"},
];
function themeIndex(name){
  const i = THEMES.findIndex(t => t.name === name);
  return i < 0 ? 0 : i;
}
function applyTheme(name, save){
  const root = document.documentElement;
  if (!name || name === "aurora") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", name);
  /* светлые темы меняют и нативные контролы браузера (color-scheme) */
  const isLight = (name === "cherry-grove" || name === "snow" ||
                   name === "sakura" || name === "creme-brulee");
  root.style.colorScheme = isLight ? "light" : "dark";
  if (save){
    try { localStorage.setItem(THEME_KEY, name); } catch(e){}
  }
  const b = $("btnTheme");
  if (b){
    b.textContent = "";
    b.appendChild(frIcon(THEME_ICON[THEMES[themeIndex(name)].name] || "theme"));
  }
}
(function initTheme(){
  let t = "";
  try { t = localStorage.getItem(THEME_KEY) || ""; } catch(e){}
  applyTheme(t, false);
})();
$("btnTheme").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme") || "aurora";
  const next = THEMES[(themeIndex(cur) + 1) % THEMES.length];
  applyTheme(next.name, true);
  toast("Тема: " + next.label);
};

/* ───────────────────────── подключение ───────────────────────── */
async function connect(){
  if (S.connecting) return;          /* ФИКС: без guard двойной клик */
  S.connecting = true;               /* давал две сессии и два пинга */
  const btn = $("btnConnect");
  btn.disabled = true;
  try {
    await connectInner();
  } finally {
    S.connecting = false;
    btn.disabled = false;
  }
}
async function connectInner(){
  warn("connWarn", "");
  S.name = $("name").value.trim();
  S.key = $("key").value.trim();
  /* (v3.5.1) ключ админа: если заполнен — уйдёт в X-Relay-Admin на /ping.
     Работает ТОЛЬКО вместе с именем хоста: админ — это личность хоста. */
  S.adminKey = $("adminkey").value.trim();
  if (!S.name){ warn("connWarn", "Введи имя"); return; }
  try {
    /* (v3.5.1) X-Relay-Admin шлём только на /ping (больше нигде не нужен);
       headerEncode — та же схема, что для X-Relay-From (UTF-8 байтами). */
    const pingHeaders = baseHeaders();
    if (S.adminKey) pingHeaders["X-Relay-Admin"] = headerEncode(S.adminKey);
    const r = await fetch("/ping", {headers: pingHeaders, ...fetchTimeout(8000)});
    const j = await safeJson(r);
    if (!j || !j.ok){
      warn("connWarn", "Хост отказал: " + ((j && j.error) || ("HTTP " + r.status)));
      return;
    }
    /* (v3.3.2) Дискриминаторы как в Discord: сервер мог выдать «Жуж#2»,
       если «Жуж» занят хостом. Принимаем выданное имя — дальше все
       запросы (X-Relay-From) подписываются именно от него. */
    if (j.name_assigned && j.name_assigned !== S.name){
      warn("connWarn", "Имя «" + j.requested_name + "» занято — ты вошёл как «" +
           j.name_assigned + "»");
      S.name = j.name_assigned;
      $("name").value = j.name_assigned;
    }
    /* (v3.5.1) Ключ админа: если сервер пометил admin_key_rejected — ключ
       ввели, но он неверный. Говорим прямо: раньше это выглядело как
       молчаливое «Жуж#2», и никто не понимал, почему админства нет. */
    if (j.admin_key_rejected){
      warn("connWarn", "Ключ админа НЕ ПРИНЯТ — ты вошёл гостем «" + S.name +
           "». Проверь ключ и что имя совпадает с именем хоста.");
    } else if (!j.name_assigned && S.name === j.name){
      /* имя хоста удержано за собой (по ключу или с машины хоста) —
         значит это админский вход */
      toast("👑 Ты вошёл как админ-хост «" + S.name + "»");
    }
    S.token = j.session_token || "";
    S.since = 0; S.channel = ""; S.channelOpen = false;
    resetFeed();
    $("channelSel").value = "";      /* ФИКС: селектор не врал после
                                        переподключения из канала */
    $("hostInfo").textContent = "хост: " + location.host +
      (j.name ? " · раздаёт: " + j.name : "");
    /* v1.9.5: ключ доступа и пароль шифрования — в sessionStorage, не в
       localStorage: localStorage живёт вечно и читается любым скриптом
       origin (XSS) и любым, у кого есть профиль браузера. Имя — не секрет,
       остаётся в localStorage для автозаполнения. */
    try { localStorage.setItem("wr_name", S.name);
          sessionStorage.setItem("wr_key", S.key);
          sessionStorage.setItem("wr_adminkey", S.adminKey);
          sessionStorage.setItem("wr_enckey", $("enckey").value); } catch(e){}
    S.unread = 0; updateUnreadBadge();
    /* шифрование сообщений: соль от хоста + секретный ключ пользователя */
    S.salt = ""; S.encKey = ""; S.encPass = ""; S.encReady = false;
    const encPass = $("enckey").value;
    try {
      const cr = await fetch("/crypto/info", {headers: baseHeaders()});
      const cj = await safeJson(cr);
      if (cj && cj.ok) S.salt = cj.salt || "";
    } catch(e){}
    if (encPass && S.salt){
      S.encPass = encPass;
      setConn(true, "вывожу ключ шифрования…");
      try {
        S.encKey = await deriveKeyHex(encPass, S.salt);
        S.encReady = true;
      } catch(e){
        warn("connWarn", "Шифрование недоступно: " + e.message);
      }
    } else if (encPass && !S.salt){
      warn("connWarn", "Хост не отдал соль шифрования — сообщения будут " +
        "видны как зашифрованные");
    }
    $("connectPanel").classList.add("hidden");
    $("app").classList.remove("hidden");
    /* v3.2: класс на body — CSS разворачивает app-shell: рейл-остров,
       внутренний скролл панелей (см. style.css). Событие fr-connected
       будит социальный рейл (rail.js). */
    document.body.classList.add("app-on");
    try { document.dispatchEvent(new Event("fr-connected")); } catch(e){}
    setConn(true, S.encReady ? "подключено · расшифровка вкл" : "подключено");
    await loadChannels();
    S.chanRefreshAt = Date.now();    /* только что загрузили — не дублируем */
    await loadChannelMessages();
    await loadFiles();
    await refreshProfile();
    if (!S.timer) S.timer = setInterval(pollTick, 1500);
  } catch(e){
    warn("connWarn", "Хост не ответил: " + e);
    setConn(false);
  }
}

/* ───────────────────────── поллинг ───────────────────────── */
const CHANNEL_REFRESH_MS = 5000;   /* новые каналы видны без reconnect */
async function pollTick(){
  if (S.busy) return;
  S.busy = true;
  try {
    /* v1.9.5: при первом подключении (since=0) просим только хвост
       (tail=200) — раньше сервер выдавал ВЕСЬ in-memory лог (тысячи
       событий, мегабайты JSON) и вкладка подвисала на рендере. */
    let qs = "since=" + S.since;
    if (S.since === 0) qs += "&tail=200";
    /* v1.9.7: wasReplay — полный пересброс ленты. В этом режиме text-события
       приходят с УЖЕ вшитыми реакциями, а reaction-дельты — поверх них;
       применить дельту повторно = отмотать реакцию (см. chat.js). Поэтому
       при replay дельты реакций пропускаем — это чинит «поставил реакцию →
       перезагрузил страницу → пропала». Новые дельты приедут
       инкрементально (since > 0) и применятся как положено. */
    const wasReplay = S.since === 0;
    const {status, json} = await apiGet("/events?" + qs);
    /* ФИКС: раньше ok:false (403 неверный ключ, ошибка прокси) не
       обрабатывался — статус висел «подключено», хотя всё умерло */
    if (json && json.ok === false){
      setConn(false, "нет связи: " + (json.error || ("HTTP " + status)));
      return;
    }
    if (json && json.events){
      /* v1.9.5: открыт КАНАЛ — события ОБЩЕГО чата в его ленту не
         вклеиваем (раньше applyEvent дописывал чужие баблы прямо в
         канал, и дифф канала их не удалял — ленты смешивались до
         переключения). S.since всё равно двигаем: при возврате в общий
         чат делается since=0 + resetFeed. */
      if (!S.channelOpen){
        for (const ev of json.events){
          if (wasReplay && ev.kind === "reaction") continue;  /* см. выше */
          applyEvent(ev);
        }
      }
      if (typeof json.next_since === "number") S.since = json.next_since;
      S.online = json.online || [];
      S.typing = json.typing || [];
      renderOnline(); renderTyping();
      setConn(true);
      if (!document.hidden && nearBottom($("feed"))) clearUnread();
    }
    /* список каналов подгружается и после подключения: друг мог
       создать канал, пока мы онлайн (раньше — только reconnect) */
    if (Date.now() - (S.chanRefreshAt || 0) > CHANNEL_REFRESH_MS){
      S.chanRefreshAt = Date.now();
      await loadChannels();
    }
    if (S.channelOpen) await loadChannelMessages(); // активный канал — догоняем
    /* шахматы: подтягиваем ходы соперника, пока матч активен */
    if (typeof pollChess === "function" && CH && CH.match &&
        CH.match.status !== "finished") await pollChess();
    /* ЛС (v2.0.2): обновляем открытый диалог в том же такте поллинга —
       свой таймер не нужен, экономим запросы (см. dm.js) */
    if (typeof pollDm === "function" && DM.peer) await pollDm();
    /* v2.0.3: бейдж непрочитанных ЛС — легкий опрос /dm/conversations
       раз в DM_BADGE_MS, независимо от открытой вкладки (см. dm.js) */
    if (typeof dmBadgeTick === "function" &&
        Date.now() - (S.dmBadgeAt || 0) > DM_BADGE_MS){
      S.dmBadgeAt = Date.now();
      dmBadgeTick();   /* без await — бейдж не тормозит поллинг */
    }
    /* v3.3.0: музыка — легкий /music/sync раз в MUSIC_SYNC_MS (music.js).
       Синхронизация нужна ВСЕГДА, пока играет: слушают из любой вкладки */
    if (typeof musicTick === "function" &&
        Date.now() - (S.musicAt || 0) > MUSIC_SYNC_MS){
      S.musicAt = Date.now();
      musicTick();     /* без await — музыка не тормозит поллинг */
    }
    /* v3.6.0: экран — почта сигналинга (при активной роли) и реестр
       показов раз в SCREEN_INFO_MS. Без await — по образцу музыки. */
    if (typeof screenTick === "function") screenTick();
  } catch(e){
    setConn(false, "нет связи, ретраю…");
  } finally { S.busy = false; }
}

/* ───────────────────────── вкладки/события UI ───────────────────────── */
/* v2.0.5: + вкладка games — лаунчер игр (games.js)
   v3 Studio: chess убран из панели вкладок и переехал в Games как
   «избранная» встроенная игра (см. index.html — #tab-chess теперь
   живёт внутри #tab-games как подвид). activateTab("chess") всё
   ещё работает (используется из games.js при клике на карту Chess). */
const TAB_NAMES = ["chat", "dm", "files", "profile", "games", "music", "voice", "screen"];
/* v2.0.2: активация вкладки выделена в функцию — из ЛС-фичи (клик по
   другу в профиле открывает переписку) и горячих путей поллинга. */
function activateTab(name){
  document.querySelectorAll(".tabs button").forEach(x => {
    x.classList.toggle("act", x.dataset.tab === name);
    /* v3.2: честный ARIA для скринридеров */
    if (x.dataset.tab)
      x.setAttribute("aria-selected", x.dataset.tab === name ? "true" : "false");
  });
  for (const t of TAB_NAMES)
    $("tab-" + t).classList.toggle("hidden", t !== name);
  if (name === "files") loadFiles();
  if (name === "profile") refreshProfile();
  if (name === "dm" && typeof refreshDm === "function") refreshDm();
  if (name === "chess"){
    if (typeof pollChess === "function") pollChess();
    /* v2.0.3: рейтинг ELO подтягиваем при открытии вкладки
       (и по кнопкам ↻ внутри вкладки — см. chess.js) */
    if (typeof loadLeaderboard === "function") loadLeaderboard();
  }
  if (name === "voice" && typeof voiceRefresh === "function")
    voiceRefresh();
  /* v3.6.0: вкладка «Экран» — сразу свежий реестр показов и почта */
  if (name === "screen" && typeof screenTick === "function") screenTick();
  /* v2.0.5: лаунчер игр подтягивается при каждом открытии вкладки —
    список маленький, а хост мог добавить игру без рестарта клиента */
  if (name === "games" && typeof loadGames === "function") loadGames();
  /* v3.3.0: вкладка «Музыка» — библиотека + первый тик синхронизации */
  if (name === "music" && typeof musicRefresh === "function") musicRefresh();
}
document.querySelectorAll(".tabs button").forEach(b => {
  b.onclick = () => activateTab(b.dataset.tab);
});

/* ── v3 Studio: Шахматы как подвид Games ──
   Клик по карте «Шахматы» в лаунчере игр прячет #tab-games и
   показывает #tab-chess (со всеми его ID, на которые опирается chess.js),
   активирует chess-логику (pollChess + loadLeaderboard) и подсвечивает
   Games-вкладку как активную. Кнопка «← к играм» — обратный путь. */
(function setupChessSubView(){
  function openChessSubView(){
    /* визуально остаёмся в Games-вкладке */
    document.querySelectorAll(".tabs button").forEach(x =>
      x.classList.toggle("act", x.dataset.tab === "games"));
    $("tab-games").classList.add("hidden");
    $("tab-chess").classList.remove("hidden");
    /* активируем chess-логику (как раньше при активации вкладки) */
    if (typeof pollChess === "function") pollChess();
    if (typeof loadLeaderboard === "function") loadLeaderboard();
  }
  function closeChessSubView(){
    $("tab-chess").classList.add("hidden");
    $("tab-games").classList.remove("hidden");
    /* при возврате в лаунчер — обновим список игр (могло что-то поменяться) */
    if (typeof loadGames === "function") loadGames();
  }
  /* клик по карте «Шахматы» */
  const chessCard = $("chessCard");
  if (chessCard) chessCard.onclick = openChessSubView;
  /* кнопка «← к играм» в #tab-chess */
  const chessBack = $("btnChessBack");
  if (chessBack) chessBack.onclick = closeChessSubView;
  /* Esc в подвиде шахмат = назад к играм */
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const chess = $("tab-chess");
    if (chess && !chess.classList.contains("hidden")
        && !document.querySelector(".gamewin:not(.hidden)")){
      closeChessSubView();
    }
  });
})();

$("btnConnect").onclick = connect;
$("name").addEventListener("keydown", e => { if (e.key === "Enter") connect(); });
$("key").addEventListener("keydown", e => { if (e.key === "Enter") connect(); });
$("enckey").addEventListener("keydown", e => { if (e.key === "Enter") connect(); });
$("btnSend").onclick = sendMsg;
$("msgText").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); sendMsg(); }
});

/* v1.9.7: быстрый эмодзи-пикер в композере — сетка частых эмодзи,
   вставка в позицию курсора, закрытие по клику мимо/Esc */
const EMOJI_SET = ["😀","😂","🥰","😎","🤔","😴","😭","🤯",
  "👍","👎","❤️","🔥","🎉","👀","🙏","💪",
  "🐱","🐶","🦊","🍕","☕","🎮","⚽","🌙"];
function closeEmojiPicker(){
  const p = document.querySelector(".epick");
  if (p) p.remove();
}
$("btnEmoji").onclick = (e) => {
  if (document.querySelector(".epick")){ closeEmojiPicker(); return; }
  const p = document.createElement("div");
  p.className = "epick";
  for (const em of EMOJI_SET){
    const s = document.createElement("span");
    s.textContent = em;
    s.onclick = () => {
      const inp = $("msgText");
      const start = inp.selectionStart ?? inp.value.length;
      const end = inp.selectionEnd ?? start;
      inp.value = inp.value.slice(0, start) + em + inp.value.slice(end);
      inp.focus();
      try { inp.selectionStart = inp.selectionEnd = start + em.length; } catch(err){}
      closeEmojiPicker();
    };
    p.appendChild(s);
  }
  document.body.appendChild(p);
  const r = e.currentTarget.getBoundingClientRect();
  let top = r.top - p.offsetHeight - 10;
  if (top < 8) top = r.bottom + 10;
  p.style.left = Math.max(8, Math.min(r.left - 30,
    innerWidth - p.offsetWidth - 8)) + "px";
  p.style.top = top + "px";
  const esc = (ev) => {
    if (ev.key === "Escape"){ closeEmojiPicker();
      document.removeEventListener("keydown", esc); }
  };
  document.addEventListener("keydown", esc);
  setTimeout(() => {
    const away = (ev) => {
      if (!p.contains(ev.target) && ev.currentTarget !== $("btnEmoji")){
        closeEmojiPicker(); document.removeEventListener("mousedown", away); }
    };
    document.addEventListener("mousedown", away);
  }, 0);
};
/* «печатает…» теперь уходит и из браузера (см. chat.js) */
$("msgText").addEventListener("input", sendTypingSoon);
/* v2.0.2: поиск по загруженной ленте (фильтр баблов, см. chat.js) */
$("searchBox").addEventListener("input", applyFeedFilter);
$("searchBox").addEventListener("keydown", e => {
  if (e.key === "Escape"){ $("searchBox").value = ""; applyFeedFilter(); }
});
$("channelSel").onchange = () => {
  /* v2.0.3: единый путь открытия канала (используют и ／🗑 кнопки) */
  openChannel($("channelSel").value);
};
/* v2.0.3: каналы — создать/удалить; пины — открыть/закрыть панель.
   Логика — в chat.js, здесь только клики. */
$("btnChanNew").onclick = createChannel;
$("btnChanDel").onclick = deleteChannel;
$("btnPins").onclick = () => togglePinPanel();
$("btnPinClose").onclick = () => togglePinPanel(false);

/* v2.0.5: окно игры — закрыть по ✕ или Esc (клик мимо НЕ закрывает:
   случайный клик не должен ронять партию) */
$("btnGameClose").onclick = () => closeGame();
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("gameWin").classList.contains("hidden"))
    closeGame();
});

/* v1.9.5: скролл к верху ленты общего чата = подгрузить старые сообщения;
   скролл вниз/видимость = прочитать всё (сброс unread). */
$("feed").addEventListener("scroll", () => {
  const feed = $("feed");
  if (!S.channelOpen && feed.scrollTop < 40) loadOlderMessages();
  if (nearBottom(feed)) clearUnread();
  updateJumpPill();
}, {passive: true});
/* пилюля «вниз ↓» (v2.0.2): клик — к низу и сброс непрочитанных */
$("jumpDown").onclick = () => {
  const feed = $("feed");
  feed.scrollTop = feed.scrollHeight;
  clearUnread();
  updateJumpPill();
};
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && nearBottom($("feed"))) clearUnread();
});
$("btnRefreshFiles").onclick = loadFiles;
$("fileInput").onchange = uploadFile;

/* v1.9.6: показать/спрятать ключи (пароли по умолчанию — не подсматривают
   через плечо) */
$("showKeys").onchange = () => {
  const t = $("showKeys").checked ? "text" : "password";
  $("key").type = t;
  $("adminkey").type = t;   /* (v3.5.1) ключ админа — тоже ключ */
  $("enckey").type = t;
};

/* v2.0.2: звук-уведомление о новых сообщениях (кнопка 🔔/🔕 в шапке чата;
   состояние — в localStorage «wr_notify», по умолчанию ВКЛ, как в Qt) */
function notifyEnabled(){
  try { return (localStorage.getItem("wr_notify") || "1") === "1"; }
  catch(e){ return true; }
}
(function initNotifyBtn(){
  const b = $("btnNotify");
  if (!b) return;
  const draw = () => {
    b.textContent = "";
    b.appendChild(frIcon(notifyEnabled() ? "bell" : "bell-off"));
  };
  draw();
  b.onclick = () => {
    const now = !notifyEnabled();
    try { localStorage.setItem("wr_notify", now ? "1" : "0"); } catch(e){}
    draw();
    toast(now ? "Звук новых сообщений включён" : "Звук новых сообщений выключен");
  };
})();

/* v1.9.6: восстановить сохранённую громкость голоса в слайдер */
(function(){
  const v = +localStorage.getItem("wr_vol") || 100;
  $("volSlider").value = v;
  const vv = $("volVal");
  if (vv) vv.textContent = v + "%";
})();

/* автозаполнение с прошлого раза */
try {
  $("name").value = localStorage.getItem("wr_name") || "";
  /* v1.9.5: ключи из sessionStorage; одноразовая миграция из localStorage
     (после чтения — затираем там, чтобы не лежали вечно). */
  let k = sessionStorage.getItem("wr_key");
  if (k == null){
    k = localStorage.getItem("wr_key") || "";
    if (k) { localStorage.removeItem("wr_key"); sessionStorage.setItem("wr_key", k); }
  }
  let ek = sessionStorage.getItem("wr_enckey");
  if (ek == null){
    ek = localStorage.getItem("wr_enckey") || "";
    if (ek) { localStorage.removeItem("wr_enckey"); sessionStorage.setItem("wr_enckey", ek); }
  }
  $("key").value = k;
  $("enckey").value = ek;
  /* (v3.5.1) ключ админа — тоже секрет: живёт в sessionStorage вкладки */
  $("adminkey").value = sessionStorage.getItem("wr_adminkey") || "";
} catch(e){}
