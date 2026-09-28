"use strict";
/* palette.js — v3.2 «Studio Social»: command palette (Ctrl+K / ⌘K).

   ФИЛОСОФИЯ (лучше дискорда): любая точка приложения достигается за
   одно сочетание клавиш — вкладка, канал, личка с другом, тема,
   оформление. Discord прячет всё в клики по серверам и шестерёнкам;
   здесь — Cmd/Ctrl+K, пару букв, Enter. Плюс Ctrl+1..6 — прямые
   прыжки по вкладкам (как в DESIGN_STYLE.md для Qt-клиента).

   КАК РАБОТАЕТ:
   - источники: вкладки (main.js TAB_NAMES), каналы (живые опции
     #channelSel), люди (FRIENDS + DM.peers), темы (THEMES из
     main.js), действия (оформление, звук, создать канал);
   - фильтр: сначала подстрока, потом субпоследовательность (fuzzy);
   - клавиатура: ↑/↓ выбор, Enter перейти, Esc закрыть;
     мышь: hover выделяет, клик выполняет;
   - z-index 280: над UX-студией, под тостами (см. карту в README).

   Совместимость: ничего не меняет в существующих модулях — только
   читает их глобальное состояние и дёргает публичные функции
   (activateTab, openChannel, openDmWith, applyTheme, createChannel). */

(function(){

let open = false;
let root = null;         /* .frpal-wrap */
let items = [];          /* [{icon, title, hint, run, section, key}] */
let filtered = [];
let cursor = 0;

/* ── источники ── */
const TABS = [
  {tab: "chat",   title: "Чат",        icon: "chat",        kbd: "1"},
  {tab: "dm",     title: "Личные сообщения", icon: "dm",    kbd: "2"},
  {tab: "files",  title: "Файлы",      icon: "files",       kbd: "3"},
  {tab: "profile",title: "Профиль",    icon: "profile",    kbd: "4"},
  {tab: "games",  title: "Игры",       icon: "games",       kbd: "5"},
  {tab: "voice",  title: "Голос",      icon: "voice",       kbd: "6"},
  {tab: "music",  title: "Музыка",     icon: "music",       kbd: "7"},
  {tab: "screen", title: "Экран (показ)", icon: "screen",   kbd: "8"},  /* v3.6.0 */
];

function collectItems(){
  items = [];
  /* 1. вкладки */
  for (const t of TABS){
    items.push({icon: t.icon, title: t.title, hint: "Ctrl+" + t.kbd,
      section: "Переход",
      run: () => activateTab(t.tab)});
  }
  /* 2. каналы (опции селектора — живые, с сервера) */
  const sel = document.getElementById("channelSel");
  if (sel){
    for (const o of sel.options){
      if (!o.value) continue;
      items.push({icon: "hash", title: "#" + o.value, hint: "канал",
        section: "Каналы",
        run: () => { activateTab("chat"); openChannel(o.value); }});
    }
  }
  /* 3. люди: друзья + недавние диалоги (без дублей, себя не предлагаем) */
  const names = [];
  const push = (n, dn) => {
    if (!n || n === S.name || names.some(x => x.name === n)) return;
    names.push({name: n, display: dn || n});
  };
  if (typeof FRIENDS !== "undefined" && Array.isArray(FRIENDS))
    for (const f of FRIENDS) push(f.name, f.display_name || f.name);
  if (typeof DM !== "undefined" && Array.isArray(DM.peers))
    for (const n of DM.peers) push(n, n);
  for (const p of names){
    items.push({icon: "at", title: p.display, hint: "личные сообщения",
      section: "Люди",
      run: () => openDmWith(p.name)});
  }
  /* 4. темы оформления (v3.2.1: у каждой — своя иконка из THEME_ICON) */
  if (typeof THEMES !== "undefined" && Array.isArray(THEMES)){
    for (const t of THEMES){
      const ic = (typeof THEME_ICON !== "undefined" && THEME_ICON[t.name]) || "theme";
      items.push({icon: ic, title: "Тема: " + t.label, hint: "оформление",
        section: "Темы",
        run: () => {
          if (typeof applyTheme === "function"){
            applyTheme(t.name, true);
            toast("Тема: " + t.label);
          }
        }});
    }
  }
  /* 5. действия */
  items.push({icon: "palette", title: "Оформление и плагины",
    hint: "Appearance Studio", section: "Действия",
    run: () => { const b = document.getElementById("btnUx"); if (b) b.click(); }});
  items.push({icon: "plus", title: "Создать канал", hint: "в чате",
    section: "Действия",
    run: () => { activateTab("chat");
      if (typeof createChannel === "function") createChannel(); }});
  items.push({icon: "bell", title: "Звук уведомлений: вкл/выкл",
    hint: "новые сообщения", section: "Действия",
    run: () => { const b = document.getElementById("btnNotify"); if (b) b.click(); }});
  items.push({icon: "sliders", title: "Проверить микрофон",
    hint: "5 секунд, локально", section: "Действия",
    run: () => { activateTab("voice");
      const b = document.getElementById("btnMicTest"); if (b) b.click(); }});
}

/* ── fuzzy-поиск: подстрока > субпоследовательность ──
   Пустой запрос — всё показываем (вкладки первыми). */
function score(query, text){
  if (!query) return 1;
  const q = query.toLowerCase(), t = text.toLowerCase();
  const idx = t.indexOf(q);
  if (idx >= 0) return 1000 - idx * 2;
  let ti = 0;
  for (const ch of q){
    const found = t.indexOf(ch, ti);
    if (found < 0) return 0;
    ti = found + 1;
  }
  return 100 - (ti - q.length);   /* чем плотнее, тем выше */
}

function applyFilter(){
  const q = root.querySelector(".frpal-in input").value.trim();
  /* порядок секций — фиксированный: навигация → каналы → люди → темы →
     действия. Скор работает ВНУТРИ секции, чтобы список не превращался
     в винегрет из повторяющихся заголовков (баг первого рендера v3.2). */
  const SECT = {"Переход": 1, "Каналы": 2, "Люди": 3, "Темы": 4, "Действия": 5};
  filtered = [];
  for (const it of items){
    const s = score(q, it.title + " " + (it.section || ""));
    if (s > 0) filtered.push({...it, s});
  }
  filtered.sort((a, b) =>
    ((SECT[a.section] || 9) - (SECT[b.section] || 9))
    || (b.s - a.s)
    || a.title.localeCompare(b.title, "ru"));
  cursor = 0;
  renderList();
}

/* ── рендер ── */
function renderList(){
  const list = root.querySelector(".frpal-list");
  list.innerHTML = "";
  if (!filtered.length){
    const empty = document.createElement("div");
    empty.className = "frpal-empty";
    empty.textContent = "Ничего не нашлось — попробуй иначе";
    list.appendChild(empty);
    return;
  }
  let lastSection = "";
  const max = 24;   /* длинные списки режем — палитра не скроллится вечно */
  filtered.slice(0, max).forEach((it, i) => {
    if (it.section && it.section !== lastSection){
      lastSection = it.section;
      const sec = document.createElement("div");
      sec.className = "frpal-sec";
      sec.textContent = it.section;
      list.appendChild(sec);
    }
    const b = document.createElement("button");
    b.type = "button";
    b.className = "frpal-item" + (i === cursor ? " act" : "");
    b.dataset.idx = String(i);
    if (typeof frIcon === "function"){
      b.appendChild(frIcon(it.icon));
    }
    const tt = document.createElement("span");
    tt.className = "frpal-tt";
    tt.textContent = it.title;
    b.appendChild(tt);
    if (it.hint){
      const h = document.createElement("span");
      h.className = "frpal-hint";
      h.textContent = it.hint;
      b.appendChild(h);
    }
    b.onclick = () => run(i);
    b.onmouseenter = () => { cursor = i; syncCursor(); };
    list.appendChild(b);
  });
}
function syncCursor(){
  root.querySelectorAll(".frpal-item").forEach((b) =>
    b.classList.toggle("act", +b.dataset.idx === cursor));
}
function moveCursor(d){
  const list = root.querySelectorAll(".frpal-item");
  if (!list.length) return;
  cursor = (cursor + d + list.length) % list.length;
  syncCursor();
  const el = list[cursor];
  if (el && el.scrollIntoView)
    el.scrollIntoView({block: "nearest"});
}
function run(i){
  const it = filtered[i];
  close();
  if (it && typeof it.run === "function") it.run();
}

/* ── открытие/закрытие ── */
function ensureDom(){
  if (root) return;
  root = document.createElement("div");
  root.className = "frpal-wrap";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-label", "Быстрый переход");
  const card = document.createElement("div");
  card.className = "frpal";
  const inWrap = document.createElement("div");
  inWrap.className = "frpal-in";
  if (typeof frIcon === "function") inWrap.appendChild(frIcon("search"));
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "Куда угодно: вкладка, канал, друг, тема…";
  input.spellcheck = false;
  inWrap.appendChild(input);
  card.appendChild(inWrap);
  const list = document.createElement("div");
  list.className = "frpal-list";
  card.appendChild(list);
  const foot = document.createElement("div");
  foot.className = "frpal-foot";
  foot.innerHTML = "<span>↑↓ выбор</span><span>Enter перейти</span>"
    + "<span>Esc закрыть</span><span>Ctrl+K снова</span>";
  card.appendChild(foot);
  root.appendChild(card);
  document.body.appendChild(root);

  input.addEventListener("input", applyFilter);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown"){ e.preventDefault(); moveCursor(1); }
    else if (e.key === "ArrowUp"){ e.preventDefault(); moveCursor(-1); }
    else if (e.key === "Enter"){ e.preventDefault(); run(cursor); }
    else if (e.key === "Escape"){ e.preventDefault(); close(); }
  });
  root.addEventListener("mousedown", (e) => {
    if (e.target === root) close();
  });
}
function openPalette(){
  if (open) return;
  ensureDom();
  collectItems();
  open = true;
  root.classList.remove("hidden");
  requestAnimationFrame(() => root.classList.add("show"));
  const input = root.querySelector("input");
  input.value = "";
  applyFilter();
  setTimeout(() => input.focus(), 20);
}
function close(){
  if (!open) return;
  open = false;
  root.classList.remove("show");
  root.classList.add("hidden");
}

/* ── горячие клавиши ──
   Ctrl+K / Cmd+K — палитра; Ctrl+1..6 — прямые вкладки.
   Не перехватываем, когда фокус в поле ввода сообщения? Наоборот —
   Ctrl+K должен работать ОТОВСЮДУ (в этом смысл). Но не мешаем
   печати: событие слушаем только с Ctrl/Cmd. */
document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (!mod) return;
  const k = (e.key || "").toLowerCase();
  if (k === "k"){
    e.preventDefault();
    if (open) close(); else openPalette();
    return;
  }
  /* Ctrl+1..6 — вкладки (только когда приложение активно) */
  const app = document.getElementById("app");
  if (app && app.classList.contains("hidden")) return;
  const n = parseInt(e.key, 10);
  if (n >= 1 && n <= TABS.length){
    e.preventDefault();
    activateTab(TABS[n - 1].tab);
  }
});

/* кнопка в шапке (index.html: #btnPalette) */
const btnPal = document.getElementById("btnPalette");
if (btnPal) btnPal.onclick = openPalette;

})();
