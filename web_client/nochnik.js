"use strict";
/* ═══════════════════════════════════════════════════════════════════════
   nochnik.js — v3.5.9 скины «Ночник» и «Крем-брюле» (по nochnik2.html)

   ЧТО ДЕЛАЕТ
   Собирает ВТОРУЮ ПАРАДИГМУ интерфейса и переносит туда СУЩЕСТВУЮЩИЕ
   панели через appendChild — ни один id, класс или обработчик не
   теряется; chat.js/voice.js/music.js/games.js продолжают работать.

       header (классика)      — становится планкой: лампа, сигнал, ⌘K
       .nk-rooms              — где говорим / чем заняты / кто в сети + me-dock
       .nk-stream             — #tab-chat, #tab-dm, #tab-profile, #tab-chess
       .nk-now                — карточки #tab-voice, #tab-music,
                                #tab-games, #tab-files («Сейчас»)

   ПОВЕДЕНИЕ (как в макете nochnik2.html):
     • клик по человеку — СРАЗУ в переписку (openDmWith);
     • клик по своему имени внизу — профиль в потоке;
     • Голос/Музыка/Игры/Файлы — не вкладки, а процессы справа:
       клик раскрывает карточку, поток не покидается;
     • строка поиска открывает командную палитру (palette.js);
      подпись честная по платформе: Ctrl+K / ⌘K;
    • v3.5.12 — user-friendly боковых колонок: секции сворачиваются
      кликом по заголовку (помнится), ширина колонок регулируется
      ручками у края (двойной клик — как было), карточки «Сейчас»
      реально сворачиваются назад (фикс .nk-cbody), шторки закрываются
      Esc и кликом мимо;
     • потеря хоста — лампа гаснет, «рёбра» сигнала падают, после 6с
       тишины — честный экран «Свет погас» (не блокирует: есть «ждать
       в фоне»);
     • в ленте — дневные метки (даты из dataset.ts, chat.js v3.5.9);
     • волна у говорящих в голосе (по .spk от voice.js).

   ВЫКЛЮЧАЕТСЯ ПОЛНОСТЬЮ
   Без html[data-skin="nochnik"|"creme"] скрипт DOM не трогает; при
   выключении панели возвращаются ровно на свои места (карта home).

   ЗАГРУЖАТЬ ПОСЛЕДНИМ (нужны activateTab, S, FRIENDS, openChannel,
   openDmWith, avatarColor, initialsOf, frIcon, setConn — все уже готовы).
   ═══════════════════════════════════════════════════════════════════════ */

(function(){

const AMBIENT = ["voice", "music", "games", "files", "screen"];  /* процессы → now */
const PLACES = ["chat", "dm", "profile", "chess"];      /* места → stream */
const CARD_META = {
  voice: {icon: "voice", title: "Голос"},
  music: {icon: "music", title: "Музыка"},
  games: {icon: "games", title: "Игры"},
  files: {icon: "files", title: "Файлы"},
  screen: {icon: "screen", title: "Экран"},   /* v3.6.0 */
};
const LOST_OVERLAY_AFTER_MS = 6000;   /* «Свет погас» только при затяжной потере */

let built = false;
let home = new Map();        /* панель → {parent, next} для возврата */
let shell = null, roomsEl = null, streamEl = null, nowEl = null;
let medock = null, micBtn = null, medockName = null, medockAv = null;
let asleepEl = null;
let lostAt = 0, asleepDismissed = false;
let sideSig = "", nowSig = "", headSig = "";
let feedObs = null, dayPending = false;

/* ── мелкие помощники ──────────────────────────────────────────────── */
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const icon = (name) => (typeof frIcon === "function")
  ? frIcon(name) : el("span", "ico");
const colorOf = (name) => (typeof avatarColor === "function")
  ? avatarColor(name) : "#888";
const initials = (name) => (typeof initialsOf === "function")
  ? initialsOf(name) : (name || "?").slice(0, 2).toUpperCase();
const isOn = () => {
  const s = document.documentElement.dataset.skin;
  return s === "nochnik" || s === "creme";
};
/* подпись строки поиска по платформе: на Windows/Linux палитру открывает
   Ctrl+K; глиф ⌘ там не нажимается и в моно-шрифте может рисоваться
   квадратиком — тот самый «артефакт в строке поиска» */
const KBD = /Mac|iPhone|iPad|iPod/i.test(navigator.platform
  || navigator.userAgent || "") ? "⌘K" : "Ctrl+K";

/* ── секции колонки комнат: сворачивание кликом + запоминание ─────── */
const FOLD_KEY = "friendrelay.nkFold";
function foldState(){
  try { return JSON.parse(localStorage.getItem(FOLD_KEY) || "{}") || {}; }
  catch(e){ return {}; }
}
function applyFold(){
  if (!roomsEl) return;
  const st = foldState();
  roomsEl.querySelectorAll(".nk-sec").forEach(sec => {
    sec.classList.toggle("folded", !!st[sec.dataset.fold]);
  });
}
function saveFold(){
  const st = {};
  if (roomsEl) roomsEl.querySelectorAll(".nk-sec").forEach(sec => {
    if (sec.classList.contains("folded")) st[sec.dataset.fold] = 1;
  });
  try { localStorage.setItem(FOLD_KEY, JSON.stringify(st)); } catch(e){}
}

/* ── ширина боковых колонок: ручки у края + запоминание ───────────── */
const COL_W = {
  rooms: {prop: "--nk-rooms-w", key: "friendrelay.nkRoomsW",
          def: 264, min: 224, max: 420},
  now:   {prop: "--nk-now-w", key: "friendrelay.nkNowW",
          def: 316, min: 272, max: 480},
};
function setColW(which, px, persist){
  const c = COL_W[which];
  const w = Math.max(c.min, Math.min(c.max, Math.round(px)));
  if (shell) shell.style.setProperty(c.prop, w + "px");
  if (persist){ try { localStorage.setItem(c.key, String(w)); } catch(e){} }
  return w;
}
function restoreColW(){
  for (const which of Object.keys(COL_W)){
    const c = COL_W[which];
    let v = 0;
    try { v = parseInt(localStorage.getItem(c.key) || "", 10) || 0; }
    catch(e){}
    if (v && shell) shell.style.setProperty(c.prop,
      Math.max(c.min, Math.min(c.max, v)) + "px");
  }
}

/* ── аватарки: один fetch на человека, потом из кеша (как в rail.js) ── */
const AV_CACHE = new Map();
function avatarUrl(name, node){
  if (AV_CACHE.has(name)){
    const u = AV_CACHE.get(name);
    if (u) paint(node, u);
    return;
  }
  AV_CACHE.set(name, null);
  fetch("/avatar/" + encodeURIComponent(name),
      {headers: baseHeaders(), ...fetchTimeout(8000)})
    .then(r => r.ok ? r.blob() : null)
    .then(b => {
      if (!b || !b.size) return;
      const u = URL.createObjectURL(b);
      AV_CACHE.set(name, u);
      paint(node, u);
    })
    .catch(() => {});
}
function paint(node, url){
  if (!node || !node.isConnected) return;
  node.textContent = "";
  node.style.backgroundImage = 'url("' + url + '")';
}
function avatar(name, online, cls){
  const a = el("span", "nk-av " + (online ? "lit" : "off") +
    (cls ? " " + cls : ""));
  a.style.background = colorOf(name);
  a.textContent = initials(name);
  avatarUrl(name, a);
  return a;
}
function face(name){
  const f = el("i", "f");
  f.style.background = colorOf(name);
  f.textContent = initials(name).slice(0, 1);
  f.title = name;
  avatarUrl(name, f);
  return f;
}
function faces(names){
  const box = el("span", "nk-faces");
  names.slice(0, 3).forEach(n => box.appendChild(face(n)));
  return box;
}

/* ── сборка каркаса ────────────────────────────────────────────────── */
function build(){
  if (built) return;
  const app = document.getElementById("app");
  if (!app) return;

  /* запомнить, где панели лежали, чтобы вернуть при выключении */
  home = new Map();
  for (const t of PLACES.concat(AMBIENT)){
    const p = document.getElementById("tab-" + t);
    if (p) home.set(t, {parent: p.parentNode, next: p.nextSibling});
  }

  shell = el("div", "nk");
  roomsEl = el("nav", "nk-rooms");
  roomsEl.setAttribute("aria-label", "Комнаты и люди");
  streamEl = el("section", "nk-stream");
  nowEl = el("aside", "nk-now");
  nowEl.setAttribute("aria-label", "Сейчас");

  /* секции колонки комнат: заголовок = сворачивание/разворачивание.
     Идентификаторы групп (nkChans/nkDoing/nkPeople) прежние —
     renderSide() продолжает работать без изменений */
  const mkSec = (lbl, gid, key, withAdd) => {
    const sec = el("div", "nk-sec");
    sec.dataset.fold = key;
    const p = el("p", "nk-lbl");
    p.appendChild(el("span", "", lbl));
    p.title = "Свернуть или развернуть секцию";
    const caret = el("span", "nk-caret");
    caret.appendChild(icon("chevron-down"));
    p.appendChild(caret);
    p.onclick = () => { sec.classList.toggle("folded"); saveFold(); };
    if (withAdd){
      /* «+» — та же кнопка #btnChanNew; клик по ней секцию не сворачивает */
      const addBtn = el("button");
      addBtn.type = "button";
      addBtn.title = "Создать канал";
      addBtn.appendChild(icon("plus"));
      addBtn.onclick = (e) => {
        e.stopPropagation();
        const b = document.getElementById("btnChanNew");
        if (b) b.click();
      };
      p.appendChild(addBtn);
    }
    const grp = el("div", "nk-group");
    grp.id = gid;
    sec.appendChild(p);
    sec.appendChild(grp);
    roomsEl.appendChild(sec);
  };
  mkSec("Где говорим", "nkChans", "chans", true);
  mkSec("Чем заняты", "nkDoing", "doing", false);
  mkSec("Кто в сети", "nkPeople", "people", false);
  applyFold();

  /* me-dock — своё имя + микрофон */
  medock = el("div", "nk-medock");
  medockAv = avatar(S.name || "я", true);
  medockName = el("div", "nk-ptxt");
  medockName.appendChild(el("span", "nk-pnm", S.name || "я"));
  medockName.appendChild(el("span", "nk-pdo", "профиль и настройки"));
  const main = el("button", "nk-medock-main");
  main.type = "button";
  main.title = "Профиль и настройки";
  main.appendChild(medockAv);
  main.appendChild(medockName);
  main.onclick = () => window.activateTab("profile");
  micBtn = el("button", "nk-mic");
  micBtn.type = "button";
  micBtn.setAttribute("aria-label", "Микрофон");
  micBtn.appendChild(icon("mic"));
  micBtn.onclick = () => {
    if (typeof VC !== "undefined" && VC.on){
      const b = document.getElementById("btnVoiceMute");
      if (b) b.click();
      return;
    }
    openCard("voice");
    if (typeof toast === "function")
      toast("Чтобы говорить — подключись к голосу");
  };
  medock.appendChild(main);
  medock.appendChild(micBtn);
  roomsEl.appendChild(medock);

  shell.appendChild(roomsEl);
  shell.appendChild(streamEl);
  shell.appendChild(nowEl);
  app.appendChild(shell);

  /* ручки ширины колонок: потянул — подстроил под себя;
     двойной клик — вернуть как было. На шторках их прячёт CSS */
  const mkHandle = (which, cls) => {
    const c = COL_W[which];
    const h = el("div", "nk-h " + cls);
    h.title = "Потянуть — ширина колонки; двойной клик — вернуть";
    h.addEventListener("pointerdown", (e) => {
      /* синтетический/чужой pointerId бросает NotFoundError —
         захват не обязан ломать весь drag */
      try { h.setPointerCapture(e.pointerId); } catch(err){}
      h.dataset.x0 = String(e.clientX);
      const cur = parseFloat(getComputedStyle(shell)
        .getPropertyValue(c.prop));
      h.dataset.w0 = String(cur || c.def);
      h.classList.add("drag");
      document.body.classList.add("nk-drag");
      e.preventDefault();
    });
    h.addEventListener("pointermove", (e) => {
      if (!h.classList.contains("drag")) return;
      const x0 = parseFloat(h.dataset.x0);
      const w0 = parseFloat(h.dataset.w0);
      const dx = (which === "rooms") ? e.clientX - x0 : x0 - e.clientX;
      setColW(which, w0 + dx, false);
    });
    const done = () => {
      if (!h.classList.contains("drag")) return;
      h.classList.remove("drag");
      document.body.classList.remove("nk-drag");
      const cur = shell
        ? parseFloat(shell.style.getPropertyValue(c.prop)) : 0;
      if (cur) setColW(which, cur, true);
    };
    h.addEventListener("pointerup", done);
    h.addEventListener("pointercancel", done);
    h.addEventListener("dblclick", () => {
      if (shell) shell.style.removeProperty(c.prop);
      try { localStorage.removeItem(c.key); } catch(e){}
    });
    shell.appendChild(h);
  };
  mkHandle("rooms", "nk-h-rooms");
  mkHandle("now", "nk-h-now");
  restoreColW();

  /* места — в поток, процессы — в карточки справа */
  for (const t of PLACES){
    const p = document.getElementById("tab-" + t);
    if (p) streamEl.appendChild(p);
  }
  for (const t of AMBIENT){
    const p = document.getElementById("tab-" + t);
    if (!p) continue;
    const meta = CARD_META[t];
    const card = el("section", "nk-card closed");
    card.dataset.card = t;
    const head = el("button", "nk-ch");
    head.type = "button";
    head.appendChild(icon(meta.icon));
    head.appendChild(el("span", "", meta.title));
    head.appendChild(el("span", "nk-tag"));
    const caret = el("span", "nk-caret");
    caret.appendChild(icon("chevron-down"));
    head.appendChild(caret);
    head.onclick = () => toggleCard(t);
    const body = el("div", "nk-cbody");
    body.appendChild(p);
    card.appendChild(head);
    card.appendChild(body);
    nowEl.appendChild(card);
  }

  /* планка: лампа вместо лого, рёбра сигнала, текст ⌘K, шторки */
  const brand = document.querySelector("header .h-brand");
  if (brand && !document.getElementById("nkLamp")){
    const lamp = el("span", "nk-lamp");
    lamp.id = "nkLamp";
    brand.insertBefore(lamp, brand.firstChild);
  }
  const pill = document.querySelector("header .connpill");
  if (pill){
    if (!pill.querySelector(".nk-bars")){
      const bars = el("span", "nk-bars");
      for (let i = 0; i < 4; i++) bars.appendChild(el("i"));
      pill.insertBefore(bars, pill.firstChild);
    }
    pill.title = "Быстрый переход (" + KBD + ")";
    pill.style.cursor = "pointer";
    pill.onclick = () => {
      const b = document.getElementById("btnPalette");
      if (b) b.click();
    };
  }
  const pal = document.getElementById("btnPalette");
  if (pal && !pal.querySelector(".nk-kbar-t")){
    pal.appendChild(el("span", "nk-kbar-t",
      "Куда угодно: вкладка, канал, друг, тема…"));
    pal.appendChild(el("kbd", "nk-kbd", KBD));
    pal.title = "Быстрый переход (" + KBD + ")";
  }
  const tools = document.querySelector("header .h-tools");
  if (tools && !document.getElementById("nkTNow")){
    const tNow = el("button", "sec nk-toggle nk-t-now");
    tNow.id = "nkTNow";
    tNow.title = "Что сейчас";
    tNow.appendChild(icon("music"));
    tNow.onclick = () => nowEl.classList.toggle("peek");
    const tRooms = el("button", "sec nk-toggle nk-t-rooms");
    tRooms.id = "nkTRooms";
    tRooms.title = "Каналы и люди";
    tRooms.appendChild(icon("chat"));
    tRooms.onclick = () => roomsEl.classList.toggle("peek");
    tools.insertBefore(tNow, tools.firstChild);
    tools.insertBefore(tRooms, tools.firstChild);
  }

  buildAsleep();
  loadFonts();
  observeFeed();

  built = true;
  syncSignal(typeof S !== "undefined" && document
    .querySelector("#connDot.on") !== null);
  renderSide();
  renderNow();
  renderHead();
}

/* разобрать каркас — панели возвращаются ровно туда, где лежали */
function teardown(){
  if (!built) return;
  for (const [t, place] of home){
    const p = document.getElementById("tab-" + t);
    if (p && place.parent) place.parent.insertBefore(p, place.next);
  }
  if (shell && shell.parentNode) shell.remove();
  document.querySelectorAll(".nk-head").forEach(n => n.remove());
  const lamp = document.getElementById("nkLamp");
  if (lamp) lamp.remove();
  document.querySelectorAll("header .nk-bars").forEach(n => n.remove());
  document.querySelectorAll(".nk-kbar-t, .nk-kbd").forEach(n => n.remove());
  document.querySelectorAll(".nk-toggle").forEach(n => n.remove());
  document.body.classList.remove("nk-drag");
  const pill = document.querySelector("header .connpill");
  if (pill){ pill.onclick = null; pill.style.cursor = ""; pill.title = ""; }
  document.querySelectorAll(".nk-day").forEach(n => n.remove());
  document.querySelectorAll("#voicePeople .nk-wave").forEach(n => n.remove());
  if (asleepEl){ asleepEl.classList.remove("show"); }
  if (feedObs){ feedObs.disconnect(); feedObs = null; }
  shell = roomsEl = streamEl = nowEl = null;
  medock = micBtn = medockName = medockAv = null;
  built = false;
  sideSig = nowSig = headSig = "";
  lostAt = 0; asleepDismissed = false;
  document.body.dataset.signal = "ok";
  if (typeof activateTab === "function") activateTab("chat");
}

/* ── «хост уснул»: честный экран при затяжной потере ─────────────────── */
function buildAsleep(){
  if (asleepEl) return;
  asleepEl = el("div", "nk-asleep");
  asleepEl.id = "nkAsleep";
  const box = el("div", "nk-asleep-box");
  box.appendChild(el("span", "nk-lamp"));
  box.appendChild(el("h2", "", "Свет погас"));
  box.appendChild(el("p",
    "", "Хост перестал отвечать. Написанное сохранено и уйдёт, " +
    "как только связь вернётся — стучусь автоматически."));
  const tryLine = el("p", "nk-try");
  tryLine.appendChild(document.createTextNode("Стучусь снова — попытка "));
  tryLine.appendChild(el("span", "", "1"));
  box.appendChild(tryLine);
  const row = el("div");
  const retry = el("button", "", "Проверить сейчас");
  retry.type = "button";
  retry.onclick = () => {
    if (typeof pollTick === "function") pollTick();
  };
  const quiet = el("button", "nk-quiet", "Ждать в фоне");
  quiet.type = "button";
  quiet.onclick = () => {
    asleepDismissed = true;
    asleepEl.classList.remove("show");
  };
  row.appendChild(retry);
  row.appendChild(quiet);
  box.appendChild(row);
  asleepEl.appendChild(box);
  document.body.appendChild(asleepEl);
}

/* ── шрифты макета: грузим ПОСЛЕ старта страницы — офлайн не мешают ── */
function loadFonts(){
  if (document.getElementById("nkFonts")) return;
  const l = document.createElement("link");
  l.id = "nkFonts";
  l.rel = "stylesheet";
  l.href = "https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:" +
    "opsz,wght@12..96,400;12..96,600;12..96,800&family=Inter+Tight:" +
    "ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap";
  document.head.appendChild(l);
}

/* ── сигнал хоста: рёбра + honest-статус + «уснул» ──────────────────── */
function syncSignal(on){
  document.body.dataset.signal = on ? "ok" : "lost";
  if (!on){
    document.querySelectorAll("header .nk-bars i")
      .forEach(b => b.classList.remove("lit"));
  }
  if (on){
    lostAt = 0;
    asleepDismissed = false;
    if (asleepEl) asleepEl.classList.remove("show");
  } else if (!lostAt){
    lostAt = Date.now();
  }
  if (on){
    const online = (typeof S !== "undefined" && Array.isArray(S.online))
      ? S.online.length : 0;
    const txt = document.getElementById("connTxt");
    const bars = document.querySelectorAll("header .nk-bars i");
    const lit = online >= 8 ? 4 : online >= 4 ? 3 : online >= 2 ? 2
      : online >= 1 ? 1 : 1;
    bars.forEach((b, i) => b.classList.toggle("lit", i < lit));
    if (txt && on && /^подключено$/.test(txt.textContent) && online)
      txt.textContent = "в сети " + online;
  }
}
/* обёртка setConn (одна на страницу): следим за связью, как ядро */
if (!window.__nkConnWrap && typeof window.setConn === "function"){
  window.__nkConnWrap = true;
  const origSetConn = window.setConn;
  window.setConn = function(on, txt){
    const r = origSetConn.apply(this, arguments);
    if (typeof on === "boolean") syncSignal(on);
    return r;
  };
}

/* ── перехват навигации: процессы больше не уводят из потока ─────────── */
if (!window.__nkTabWrap && typeof window.activateTab === "function"){
  window.__nkTabWrap = true;
  const origActivate = window.activateTab;
  window.activateTab = function(name){
    if (!isOn() || !built){ origActivate(name); return; }
    if (AMBIENT.indexOf(name) >= 0){
      openCard(name);
      const anyPlace = PLACES.some(t => {
        const p = document.getElementById("tab-" + t);
        return p && !p.classList.contains("hidden");
      });
      if (!anyPlace) origActivate("chat");
      return;
    }
    if (roomsEl) roomsEl.classList.remove("peek");
    origActivate(name);
    renderHead();
  };
}

/* ── карточки процессов ────────────────────────────────────────────── */
function toggleCard(t){
  const c = document.querySelector('.nk-card[data-card="' + t + '"]');
  if (!c) return;
  const willOpen = c.classList.contains("closed");
  c.classList.toggle("closed");
  if (willOpen){
    const p = document.getElementById("tab-" + t);
    if (p) p.classList.remove("hidden");   /* процесс обязан быть виден */
    refreshAmbient(t);
  } else if (window.innerWidth <= 1180 && nowEl
      && !document.querySelector(".nk-card:not(.closed)")) {
    nowEl.classList.remove("peek");       /* свернули последнюю — шторка вниз */
  }
}
function openCard(t){
  const c = document.querySelector('.nk-card[data-card="' + t + '"]');
  if (!c) return;
  c.classList.remove("closed");
  const p = document.getElementById("tab-" + t);
  if (p) p.classList.remove("hidden");     /* см. toggleCard */
  refreshAmbient(t);
  if (window.innerWidth <= 1180 && nowEl) nowEl.classList.add("peek");
  c.scrollIntoView({block: "nearest", behavior: "smooth"});
}
function refreshAmbient(t){
  if (t === "voice" && typeof voiceRefresh === "function") voiceRefresh();
  if (t === "music" && typeof musicRefresh === "function") musicRefresh();
  if (t === "games" && typeof loadGames === "function") loadGames();
  if (t === "files" && typeof loadFiles === "function") loadFiles();
}

/* ── источники данных (те же, что у skin4 — проверено) ─────────────── */
function collectPeople(){
  const map = new Map();
  const add = (name, online, friend, display) => {
    if (!name || name === S.name) return;
    const prev = map.get(name) ||
      {name, online: false, friend: false, display: name};
    if (online) prev.online = true;
    if (friend) prev.friend = true;
    if (display && display !== name) prev.display = display;
    map.set(name, prev);
  };
  if (typeof FRIENDS !== "undefined" && Array.isArray(FRIENDS))
    for (const f of FRIENDS) add(f.name, !!f.online, true,
      f.display_name || f.name);
  if (Array.isArray(S.online))
    for (const n of S.online) add(n, true, false, n);
  const list = [...map.values()];
  list.sort((a, b) => (b.online - a.online) || (b.friend - a.friend)
    || a.name.localeCompare(b.name, "ru"));
  return list;
}
function voiceNames(){
  const box = document.getElementById("voicePeople");
  if (!box) return [];
  const nm = box.querySelectorAll(".vname");
  const src = nm.length ? [...nm] : [...box.children];
  return src
    .map(n => n.textContent.replace(/\s*\(\s*ты\s*\)\s*$/, "").trim())
    .filter(Boolean).slice(0, 6);
}
function musicState(){
  const t = document.getElementById("musicTitle");
  const title = t ? t.textContent.trim() : "";
  return {title, live: !!title && title !== "Ничего не играет"};
}
function dmUnread(){
  const b = document.querySelector('.tabs button[data-tab="dm"] .badge');
  return b ? b.textContent : "";
}

/* ── колонка комнат ────────────────────────────────────────────────── */
function renderSide(){
  if (!isOn() || !built) return;
  const people = collectPeople();
  const sel = document.getElementById("channelSel");
  const chans = sel ? [...sel.options].map(o => ({v: o.value, t: o.textContent}))
    : [];
  const vn = voiceNames();
  const music = musicState();
  const files = document.getElementById("filesList");
  const dmBadge = dmUnread();
  const micOff = (typeof VC !== "undefined" && VC.muted);
  const sig = chans.map(c => c.v).join(",") + "|" + (S.channel || "") + "|"
    + people.map(p => p.name + (p.online ? "+" : "-")).join(",")
    + "|" + vn.join(",") + "|" + music.title + "|" + dmBadge
    + "|" + (typeof DM !== "undefined" ? (DM.peer || "") : "")
    + "|" + String(micOff) + "|" + (S.name || "");
  if (sig === sideSig) return;
  sideSig = sig;

  /* где говорим: каналы + явный вход в ЛС */
  const cb = document.getElementById("nkChans");
  cb.innerHTML = "";
  for (const c of chans){
    const b = el("button", "nk-room");
    b.type = "button";
    /* активен: общий чат (v="") когда S.channel пуст, или совпадение */
    const active = c.v ? (S.channel === c.v) : !S.channel;
    if (active) b.classList.add("act");
    b.appendChild(el("span", "hash", "#"));
    b.appendChild(el("span", "nm", c.t.replace(/^#\s*/, "")));
    b.onclick = () => {
      window.activateTab("chat");
      if (typeof openChannel === "function") openChannel(c.v);
      if (roomsEl && window.innerWidth <= 820)
        roomsEl.classList.remove("peek");
    };
    cb.appendChild(b);
  }
  const dmRow = el("button", "nk-room");
  dmRow.type = "button";
  dmRow.appendChild(icon("dm"));
  dmRow.appendChild(el("span", "nm", "Личные сообщения"));
  if (dmBadge) dmRow.appendChild(el("span", "nk-unread", dmBadge));
  dmRow.onclick = () => {
    window.activateTab("dm");
    if (roomsEl && window.innerWidth <= 820)
      roomsEl.classList.remove("peek");
  };
  cb.appendChild(dmRow);

  /* чем заняты: процессы с лицами тех, кто внутри */
  const db = document.getElementById("nkDoing");
  db.innerHTML = "";
  const doing = [
    {id: "voice", n: "Голос", inside: vn, live: vn.length > 0,
     tag: vn.length ? vn.length + " в канале" : ""},
    {id: "music", n: "Музыка", inside: [], live: music.live,
     tag: music.live ? "играет" : ""},
    {id: "games", n: "Игры", inside: [], live: false, tag: ""},
    {id: "files", n: "Файлы", inside: [], live: false,
     tag: files && files.children.length
       ? files.children.length + " шт." : ""},
  ];
  for (const d of doing){
    const b = el("button", "nk-room" + (d.live ? " live" : ""));
    b.type = "button";
    b.appendChild(icon(CARD_META[d.id].icon));
    b.appendChild(el("span", "nm", d.n));
    if (d.inside.length) b.appendChild(faces(d.inside));
    else if (d.tag) b.appendChild(el("span", "nk-pdo", d.tag));
    b.onclick = () => openCard(d.id);
    db.appendChild(b);
  }

  /* кто в сети: клик — СРАЗУ в переписку (как в макете) */
  const pb = document.getElementById("nkPeople");
  pb.innerHTML = "";
  if (!people.length){
    pb.appendChild(el("div", "nk-empty",
      "Пока никого. Добавь друга в профиле — он появится здесь."));
  }
  for (const p of people){
    const inVoice = vn.indexOf(p.name) >= 0;
    const dmActive = typeof DM !== "undefined" && DM.peer === p.name
      && !document.getElementById("tab-dm").classList.contains("hidden");
    const b = el("button", "nk-person" + (p.online ? "" : " offline")
      + (dmActive ? " act" : ""));
    b.type = "button";
    b.title = "Написать " + p.display;
    b.appendChild(avatar(p.name, p.online));
    const txt = el("span", "nk-ptxt");
    txt.appendChild(el("span", "nk-pnm", p.display));
    const what = p.online
      ? (inVoice ? "<em>в голосе</em>" : "<em>в сети</em>")
      : "не в сети";
    txt.appendChild(el("span", "nk-pdo", what));
    b.appendChild(txt);
    b.onclick = () => {
      if (typeof openDmWith === "function") openDmWith(p.name);
      if (roomsEl && window.innerWidth <= 820)
        roomsEl.classList.remove("peek");
    };
    pb.appendChild(b);
  }

  /* me-dock: имя/аватар/микрофон */
  if (medockName){
    medockName.querySelector(".nk-pnm").textContent = S.name || "я";
    if (micBtn){
      micBtn.classList.toggle("mut", !!micOff);
      micBtn.title = micOff ? "Микрофон выключен" : "Микрофон";
    }
    if (typeof VC !== "undefined" && VC.on)
      avatarUrl(S.name, medockAv);
  }
}

/* ── подписи карточек справа ───────────────────────────────────────── */
function renderNow(){
  if (!isOn() || !built) return;
  const vn = voiceNames();
  const music = musicState();
  const files = document.getElementById("filesList");
  const tags = {
    voice: vn.length ? vn.length + " в канале" : "никого",
    music: music.live ? music.title : "тишина",
    games: "",
    files: files && files.children.length
      ? files.children.length + " шт." : "",
    /* v3.6.0: экран — из screen.js (роль/чужой показ) */
    screen: (typeof screenState === "function") ? (screenState().tag || "") : "",
  };
  const live = {voice: vn.length > 0, music: music.live, games: false,
    files: false,
    screen: (typeof screenState === "function") ? screenState().live : false};
  const sig = JSON.stringify(tags);
  /* видимость раскрытых карточек чиним КАЖДЫЙ тик (до sig-выхода):
     классический activateTab прячет все вкладки мимо активной */
  for (const t of AMBIENT){
    const card = document.querySelector('.nk-card[data-card="' + t + '"]');
    if (!card || card.classList.contains("closed")) continue;
    const p = document.getElementById("tab-" + t);
    if (p && p.classList.contains("hidden")) p.classList.remove("hidden");
  }
  if (sig === nowSig) return;
  nowSig = sig;
  for (const t of AMBIENT){
    const card = document.querySelector('.nk-card[data-card="' + t + '"]');
    if (!card) continue;
    card.classList.toggle("live", !!live[t]);
    const tag = card.querySelector(".nk-tag");
    if (tag) tag.textContent = tags[t] || "";
  }
}

/* ── заголовок потока: где я и что тут ─────────────────────────────── */
function renderHead(){
  if (!isOn() || !built) return;
  let place = "chat";
  for (const t of PLACES){
    const p = document.getElementById("tab-" + t);
    if (p && !p.classList.contains("hidden")){ place = t; break; }
  }
  let title = "", hash = "", sub = "";
  if (place === "chat"){
    const sel = document.getElementById("channelSel");
    const opt = sel && sel.selectedOptions[0];
    const nm = opt ? opt.textContent.replace(/^#\s*/, "") : "общий чат";
    hash = "#"; title = S.channelOpen ? nm : "общий";
    const typing = (S.typing || []).filter(n => n !== S.name);
    sub = S.channelOpen
      ? (typing.length ? "печатает: " + typing.join(", ") : "канал")
      : (typing.length ? "печатает: " + typing.join(", ") : "общий чат хоста");
  } else if (place === "dm"){
    const sel = document.getElementById("dmPeer");
    const who = sel && sel.value ? sel.value : "Личные сообщения";
    hash = "→"; title = who;
    const st = document.getElementById("dmStat");
    sub = st ? st.textContent.trim() : "только вы двое";
  } else if (place === "profile"){
    title = "Профиль";
    sub = S.name ? "это ты — " + S.name : "профиль и настройки";
  } else if (place === "chess"){
    title = "Шахматы";
    const st = document.getElementById("chessStatus");
    sub = st ? st.textContent.trim().slice(0, 80) : "партия с судьёй-сервером";
  }
  const sig = place + "|" + hash + title + "|" + sub;
  if (sig === headSig) return;
  headSig = sig;
  let head = streamEl.querySelector(".nk-head");
  if (!head){
    head = el("div", "nk-head");
    const tt = el("div", "nk-head-tt");
    tt.appendChild(el("div", "nk-title"));
    tt.appendChild(el("div", "nk-sub"));
    head.appendChild(tt);
    /* вставить заголовок В открытую панель места (первым ребёнком) */
    const active = document.getElementById("tab-" + place);
    if (active) active.insertBefore(head, active.firstChild);
    head.dataset.place = place;
  }
  if (head.dataset.place !== place){
    /* место сменилось — переставить заголовок в новую панель */
    const active = document.getElementById("tab-" + place);
    if (active) active.insertBefore(head, active.firstChild);
    head.dataset.place = place;
  }
  const tEl = head.querySelector(".nk-title");
  tEl.innerHTML = "";
  if (hash) tEl.appendChild(el("span", "hash", hash));
  tEl.appendChild(document.createTextNode(title));
  head.querySelector(".nk-sub").textContent = sub;
}

/* ── волны говорящих + аватарки в голосе ───────────────────────────── */
function decorateVoice(){
  const box = document.getElementById("voicePeople");
  if (!box) return;
  for (const row of box.querySelectorAll(".vperson")){
    if (!row.querySelector(".nk-wave")){
      const w = el("span", "nk-wave");
      for (let i = 0; i < 4; i++) w.appendChild(el("i"));
      row.appendChild(w);
    }
    const av = row.querySelector(".vava");
    const nm = row.querySelector(".vname");
    if (av && nm)
      avatarUrl(nm.textContent.replace(/\s*\(\s*ты\s*\)\s*$/, ""), av);
  }
}

/* ── дневные метки ленты (по датам баблов) ─────────────────────────── */
function dayKey(ts){
  const d = new Date(ts * 1000);
  if (isNaN(d)) return "";
  const now = new Date();
  const same = (a, b) => a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  const yest = new Date(now.getTime() - 86400000);
  if (same(d, now)) return "сегодня";
  if (same(d, yest)) return "вчера";
  return d.toLocaleDateString("ru-RU",
    {day: "numeric", month: "long"});
}
function reconcileDays(){
  if (dayPending) return;
  dayPending = true;
  requestAnimationFrame(() => {
    dayPending = false;
    if (!isOn() || !built) return;
    const feed = document.getElementById("feed");
    if (!feed) return;
    feed.querySelectorAll(".nk-day").forEach(n => n.remove());
    let prev = "";
    for (const k of [...feed.children]){
      if (!k.dataset || !k.dataset.ts) continue;
      const key = dayKey(+k.dataset.ts);
      if (!key || key === prev) continue;
      prev = key;
      const div = el("div", "nk-day", key);
      feed.insertBefore(div, k);
    }
  });
}
function observeFeed(){
  const feed = document.getElementById("feed");
  if (!feed || feedObs) return;
  feedObs = new MutationObserver(reconcileDays);
  feedObs.observe(feed, {childList: true});
}

/* ── текст поверх акцента: тёмный на светлом, белый на тёмном ──────── */
function onAccentTick(){
  const h = document.documentElement;
  const raw = (getComputedStyle(h).getPropertyValue("--accent") || "")
    .trim();
  const m = /^#?([0-9a-f]{6})$/i.exec(raw)
    || /^rgb\((\d+)[,\s]+(\d+)[,\s]+(\d+)\)$/i.exec(raw);
  if (!m) return;
  let r, g, b;
  if (raw.startsWith("rgb")){ r = +m[1]; g = +m[2]; b = +m[3]; }
  else {
    const n = parseInt(m[1], 16);
    r = (n >> 16) & 255; g = (n >> 8) & 255; b = n & 255;
  }
  const lum = r * .299 + g * .587 + b * .114;
  h.style.setProperty("--on-accent", lum > 150 ? "#12222a" : "#fff6ea");
}

/* ── Esc: сначала закрываем шторки, потом старая логика шахмат ─────── */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !isOn() || !built) return;
  if (nowEl && nowEl.classList.contains("peek")){
    nowEl.classList.remove("peek");
    return;
  }
  if (roomsEl && roomsEl.classList.contains("peek")){
    roomsEl.classList.remove("peek");
    return;
  }
  const chess = document.getElementById("tab-chess");
  if (!chess || chess.classList.contains("hidden")) return;
  if (document.querySelector(".gamewin:not(.hidden)")) return;
  setTimeout(() => {
    if (!isOn() || !built) return;
    const chat = document.getElementById("tab-chat");
    if (chat) chat.classList.remove("hidden");
    openCard("games");
    renderHead();
  }, 0);
});
/* кнопка «К играм» — тот же возврат (guard isOn: живёт и после выключения) */
document.addEventListener("click", (e) => {
  if (!isOn() || !built) return;
  const back = e.target && e.target.closest
    && e.target.closest("#btnChessBack");
  if (!back) return;
  setTimeout(() => {
    if (!isOn() || !built) return;
    const chat = document.getElementById("tab-chat");
    if (chat) chat.classList.remove("hidden");
    openCard("games");
    renderHead();
  }, 0);
});

/* ── клик мимо раскрытой шторки закрывает её (телефон/планшет) ─────── */
document.addEventListener("pointerdown", (e) => {
  if (!isOn() || !built) return;
  const t = e.target;
  if (!t || !t.closest) return;
  /* открыватели (.nk-room/.nk-ch) и сами шторки не считаем «мимо»,
     иначе клик-открыватель закрывал бы шторку в тот же миг */
  if (nowEl && nowEl.classList.contains("peek")
      && !t.closest(".nk-now, .nk-toggle, .nk-room, .nk-ch"))
    nowEl.classList.remove("peek");
  if (roomsEl && roomsEl.classList.contains("peek")
      && !t.closest(".nk-rooms, .nk-toggle"))
    roomsEl.classList.remove("peek");
}, true);

/* ── тик: 1.2с — между тиками поллинга; ререндер только при изменении ── */
setInterval(() => {
  if (!isOn() || !built) return;
  renderSide();
  renderNow();
  renderHead();
  decorateVoice();
  onAccentTick();
  /* «Свет погас» — только при затяжной потере, не при чихе */
  if (asleepEl){
    const lost = document.body.dataset.signal === "lost";
    if (lost && lostAt && !asleepDismissed
        && Date.now() - lostAt > LOST_OVERLAY_AFTER_MS){
      asleepEl.classList.add("show");
      const n = asleepEl.querySelector(".nk-try span");
      if (n) n.textContent =
        String(Math.max(1, Math.round((Date.now() - lostAt) / 1500)));
    } else if (!lost && asleepEl.classList.contains("show")){
      asleepEl.classList.remove("show");
    }
  }
}, 1200);

/* ── включение/выключение скина ────────────────────────────────────── */
function sync(){
  if (isOn()){
    const app = document.getElementById("app");
    if (app && !app.classList.contains("hidden")) build();
  } else if (built){
    teardown();
  }
}
new MutationObserver(sync).observe(document.documentElement,
  {attributes: true, attributeFilter: ["data-skin"]});
const appNode = document.getElementById("app");
if (appNode) new MutationObserver(sync).observe(appNode,
  {attributes: true, attributeFilter: ["class"]});
sync();

})();

