"use strict";
/* games.js — вкладка «🎮 Игры» (v2.0.5): лаунчер + окно игры в iframe.

   Архитектура — как «активити» в Discord:
   - игры лежат на сервере (GET /games — список, GET /games/<id> — HTML
     самой игры), это самостоятельные веб-приложения;
   - клиент встраивает игру в <iframe sandbox="allow-scripts
     allow-same-origin">: same-origin нужен игре для localStorage
     (рекорды), allow-scripts — для жизни, а ВСЁ остальное (нет форм,
     нет топ-навигации, нет попапов) отрезано песочницей;
   - игра ↔ клиент общаются ТОЛЬКО через postMessage (мини-SDK):
       игра → fr_hello : игра загрузилась
       клиент → fr_ctx : {type, name, token} — контекст пользователя
                          (token нужен мультиплеерным играм для
                          подписанных запросов к API, как в core.js)
       игра → fr_toast : {text} — показать всплывашку в клиенте
       игра → fr_close : попросить закрыть окно (кнопка «Выйти в чат»)
       игра → fr_api   : {id, method, path, body} — API-запрос через хоста
                         (v2.0.7): хост делает apiGet/apiPost СО СВОЕЙ
                         подписью X-Relay-Auth и авторекавери сессии
                         (core.js) и возвращает {id, ok, status, json}.
                         Так мультиплеерные игры (Го) ходят в /bot_command,
                         не дублируя HMAC-подпись у себя внутри.
   Одиночные игры (2048/Сапёр) используют только fr_close/fr_toast. */

const GM = {list: [], current: null, busy: false};

async function loadGames(){
  const grid = $("gamesGrid");
  if (!grid || GM.busy) return;
  GM.busy = true;
  try {
    const {json} = await apiGet("/games");
    GM.list = (json && json.games) || [];
    renderGames(grid);
  } catch(e){
    grid.innerHTML = "";
    const d = document.createElement("div");
    d.className = "hint";
    d.textContent = "Лаунчер недоступен: " + e;
    grid.appendChild(d);
  } finally { GM.busy = false; }
}

function renderGames(grid){
  grid.innerHTML = "";
  if (!GM.list.length){
    const d = document.createElement("div");
    d.className = "hint";
    d.textContent = "Серверных игр пока нет — на хосте пустой реестр. Встроенные игры (Шахматы) доступны выше.";
    grid.appendChild(d);
    return;
  }
  /* карта известных эмодзи → SVG-иконок (icons.js).
     Если игра прислала незнакомый эмодзи — используем дефолтную i-game-grid. */
  const EMOJI_TO_ICON = {
    "♟": "chess-knight", "♟️": "chess-knight",
    "⚪": "game-circle-stone", "⚫": "game-circle-stone",
    "🧩": "game-puzzle",
    "💣": "game-bomb",
    "2048": "game-grid",
    "🟫": "game-voxel", "🟦": "game-voxel",
    "🚀": "game-rocket",
    "🐍": "game-snake",
    "🎲": "game-dice",
    "🎯": "game-target",
  };
  for (const g of GM.list){
    const card = document.createElement("div");
    card.className = "gamecard" + (g.available ? "" : " off");
    /* ищем SVG-иконку по эмодзи или id игры; fallback — i-game-grid */
    const iconName = EMOJI_TO_ICON[g.emoji] ||
                     EMOJI_TO_ICON[g.id] ||
                     "game-grid";
    card.innerHTML =
      '<div class="gameicon" data-i="' + iconName + '"></div>' +
      '<div class="gametitle"></div>' +
      '<div class="gamedesc"></div>' +
      '<div class="gamemode"></div>';
    card.querySelector(".gametitle").textContent = g.title || g.id;
    card.querySelector(".gamedesc").textContent = g.desc || "";
    /* режим: multi — матч судит сервер, single — вся игра в браузере.
       Текст без эмодзи — иконка режима будет подставлена ниже. */
    const mode = card.querySelector(".gamemode");
    mode.textContent = g.mode === "multi" ? "мультиплеер" : "одиночная";
    if (g.available) card.onclick = () => openGame(g);
    else {
      card.title = "Файл игры отсутствует на хосте";
      mode.textContent = "файл не найден";
    }
    grid.appendChild(card);
  }
}

/* ── окно игры (аналог окна активити) ─────────────────────────────────── */
/* v3.1: GM.pendingAutoJoin — {bot_id, code} для отложенного автоджойна.
   Когда игра пришлёт fr_hello, мы отправим fr_ctx с доп. полем
   <bot_id>_auto_join (например go_auto_join), и игра сама откроет
   онлайн-матч и присоединится к коду. Так работает клик по ссылке-
   приглашению friendrelay://go/join/CODE в чате. */
GM.pendingAutoJoin = null;

function openGame(g){
  GM.current = g;
  const win = $("gameWin");
  /* v3 Studio: в заголовке окна — текст без эмодзи (иконка и так есть на карте) */
  $("gameWinTitle").textContent = g.title || g.id;
  /* ?v=<мс> — честный cache-buster: сервер отдаёт игру с no-cache, но
     промежуточные прокси могут кешировать; перезапуск игры всегда свежий */
  $("gameFrame").src = g.url + "?v=" + Date.now();
  win.classList.remove("hidden");
}
/* v3.1: открыть игру с автоприсоединением к матчу по коду.
   Используется кликом по ссылке-приглашению friendrelay://go/join/CODE.
   Ждёт fr_hello от игры, затем шлёт fr_ctx с <bot_id>_auto_join=code. */
function openGameWithAutoJoin(g, botId, code){
  GM.pendingAutoJoin = {bot_id: botId, code: code};
  openGame(g);
}
function closeGame(){
  $("gameFrame").src = "about:blank";   /* игра умирает — таймеры стоп */
  $("gameWin").classList.add("hidden");
  GM.current = null;
  GM.pendingAutoJoin = null;   /* v3.1: сброс — игра закрыта, автоджойн не нужен */
  loadGames();   /* лаунчер мог устареть (в будущем — очки мультиплеера) */
}

/* ── приём сообщений от игры (сервер мини-SDK) ────────────────────────── */
window.addEventListener("message", (ev) => {
  /* same-origin: источник — только наш же сервер (iframe с /games/…) */
  if (ev.origin !== location.origin) return;
  const d = ev.data || {};
  if (d.type === "fr_hello"){
    /* игра загрузилась — отдаём контекст (как SDK в Discord).
       token — сессия веб-клиента: с ним игра сама ходит в API
       (/bot_command и т.п.), не спрашивая у пользователя логин снова. */
    const ctx = {
      type: "fr_ctx",
      name: S.name,
      token: S.token,
    };
    /* v3.1: если ждём автоджойн — добавляем поле <bot_id>_auto_join.
       Игра (go.html) при получении переключится в NET-режим и
       присоединится к матчу по коду. */
    if (GM.pendingAutoJoin && GM.pendingAutoJoin.bot_id){
      const key = GM.pendingAutoJoin.bot_id + "_auto_join";
      ctx[key] = GM.pendingAutoJoin.code;
      GM.pendingAutoJoin = null;   /* одноразовое — после отправки чистим */
    }
    ev.source.postMessage(ctx, "*");
    return;
  }
  if (d.type === "fr_toast" && d.text) toast(String(d.text).slice(0, 120));
  if (d.type === "fr_close") closeGame();
  if (d.type === "fr_api" && d.id != null && typeof d.path === "string")
    handleFrApi(ev.source, d);
});

/* ── fr_api: прокси API-запросов игры через хоста (v2.0.7) ─────────────
   Игра внутри sandbox-iframe НЕ знает access_key и не умеет подписывать
   X-Relay-Auth. Хост умеет (core.js: подпись + авторекавери сессии).
   Поэтому игра шлёт {type:"fr_api", id, method, path, body}, хост
   выполняет запрос и отвечает {type:"fr_api_res", id, ok, status, json}. */
async function handleFrApi(source, d){
  let ok = true, status = 0, json = null, error = null;
  try {
    if (d.method === "GET"){
      ({status, json} = await apiGet(d.path));
    } else {
      ({status, json} = await apiPost(d.path, d.body || {}));
    }
  } catch(e){ ok = false; error = String(e); }
  try {
    source.postMessage({type:"fr_api_res", id: d.id, ok, status, json, error}, "*");
  } catch(e){ /* iframe уже закрыт — не важно */ }
}
