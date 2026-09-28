"use strict";
/* rail.js — v3.2 «Studio Social»: социальный рейл (левый «остров»).

   ФИЛОСОФИЯ (лучше дискорда): у Discord слева — «поезд серверов»,
   мёртвые иконки сообществ, в которых ты не общаешься. У Friend Relay
   слева — ЖИВЫЕ ЛЮДИ: аватары друзей с онлайн-точками. Клик по аватару
   сразу открывает личку. Никакой вложенности «сервер → канал → чат»:
   один рейл = вся твоя соцсеть.

   КАК РАБОТАЕТ:
   - рейл встраивается в существующий контейнер .tabs (он уже
     превращён CSS в вертикальный остров): сверху секция «В сети»,
     посередине — навигация (табы как иконки), снизу — твой «док»
     (аватар → профиль);
   - данные: S.online из поллинга (каждые 1.5с обновляется main.js) +
     список друзей (свой лёгкий опрос /friends раз в 20с — тот же
     эндпоинт, что и вкладка «Профиль»);
   - ререндер только при реальном изменении (подпись-строка) —
     не дёргает DOM на каждый тик;
   - клик по аватару — openDmWith(имя) из dm.js (единый путь).

   Совместимость: ни один существующий id/класс не тронут — только
   добавляются .rail-people / .rail-dock внутрь .tabs. На мобильных
   CSS разворачивает остров в нижнюю панель (см. style.css). */

(function(){

const RAIL = {
  people: [],       /* [{name, online, friend, display}] */
  sig: "",          /* подпись последнего рендера */
  friendsAt: 0,     /* когда последний раз опрашивали /friends */
  myAvatarUrl: null,/* blob-URL своей аватарки */
  myAvatarTried: false,
};

/* ── собрать список людей: друзья + все онлайн, без дублей ──
   Друзья идут первыми, потом остальные онлайн-участники (им тоже
   можно написать — это приватная сеть друзей). Себя не показываем. */
function collectPeople(){
  const map = new Map();
  const add = (name, online, friend, display) => {
    if (!name || name === S.name) return;
    const prev = map.get(name) || {name, online: false,
      friend: false, display: name};
    if (online) prev.online = true;
    if (friend) prev.friend = true;
    if (display && display !== name) prev.display = display;
    map.set(name, prev);
  };
  /* друзья из профиля (FRIENDS обновляет вкладка «Профиль») */
  if (typeof FRIENDS !== "undefined" && Array.isArray(FRIENDS)){
    for (const f of FRIENDS)
      add(f.name, !!f.online, true, f.display_name || f.name);
  }
  /* все, кто сейчас в сети (из pollTick → S.online) */
  if (Array.isArray(S.online)){
    for (const n of S.online) add(n, true, false, n);
  }
  const list = [...map.values()];
  /* сортировка: онлайн выше, друзья выше, по алфавиту */
  list.sort((a, b) => (b.online - a.online) || (b.friend - a.friend)
    || a.name.localeCompare(b.name, "ru"));
  return list;
}

/* ── лёгкий опрос друзей (тот же API, что профиль; раз в 20с) ──
   Нужен, чтобы рейл знал presence друзей ещё до первого захода
   во вкладку «Профиль». */
async function railRefreshFriends(){
  if (!S.token) return;
  if (Date.now() - RAIL.friendsAt < 20000) return;
  RAIL.friendsAt = Date.now();
  try {
    const {json} = await apiGet("/friends?name=" + encodeURIComponent(S.name));
    if (json && Array.isArray(json.friends)){
      /* обновляем глобальный FRIENDS только если он пуст — иначе
         перетрём display-имена, которые уже собрал профиль */
      FRIENDS = json.friends;
      render();
    }
  } catch(e){ /* тишина: рейл не критичен */ }
}

/* ── своя аватарка для дока (однократный fetch, как в профиле) ── */
async function loadMyAvatar(){
  if (RAIL.myAvatarUrl || RAIL.myAvatarTried || !S.name) return;
  RAIL.myAvatarTried = true;
  try {
    const r = await fetch("/avatar/" + encodeURIComponent(S.name),
      {headers: baseHeaders(), ...fetchTimeout(8000)});
    if (!r.ok) return;
    const blob = await r.blob();
    if (!blob.size) return;
    RAIL.myAvatarUrl = URL.createObjectURL(blob);
    /* если док уже нарисован — обновим картинку на месте */
    const me = document.querySelector(".railme-face");
    if (me){
      me.textContent = "";
      me.style.backgroundImage = 'url("' + RAIL.myAvatarUrl + '")';
      me.style.backgroundSize = "cover";
    }
  } catch(e){ /* инициалы останутся */ }
}

/* ── рендер ──
   Секция людей пересобирается только при изменении подписи —
   клики и ховеры не сбрасываются без причины. */
function render(){
  const tabs = document.querySelector("#app .tabs");
  if (!tabs) return;
  const people = collectPeople();
  const sig = people.map(p => p.name + (p.online ? "+" : "-")
    + (p.friend ? "f" : "") + "|" + p.display).join(",")
    + "#" + (S.name || "");
  if (sig === RAIL.sig) return;
  RAIL.sig = sig;

  /* ── секция «люди» ── */
  let box = tabs.querySelector(".rail-people");
  if (!box){
    box = document.createElement("div");
    box.className = "rail-people";
    tabs.insertBefore(box, tabs.firstChild);
  }
  box.innerHTML = "";

  const onlineCount = people.filter(p => p.online).length;
  const label = document.createElement("div");
  label.className = "rail-plabel";
  label.textContent = onlineCount ? "в сети " + onlineCount : "друзья";
  box.appendChild(label);

  const list = document.createElement("div");
  list.className = "rail-list";
  if (!people.length){
    const hint = document.createElement("div");
    hint.className = "rail-empty";
    hint.textContent = "пока никого";
    hint.title = "Добавь друзей во вкладке «Профиль» — они появятся здесь";
    list.appendChild(hint);
  } else {
    for (const p of people){
      const b = document.createElement("button");
      b.type = "button";
      b.className = "railava" + (p.online ? " on" : "");
      b.title = p.display + (p.online ? " — в сети" : " — не в сети")
        + "\nЛичное сообщение";
      b.setAttribute("aria-label", "Написать " + p.display);
      const face = document.createElement("span");
      face.className = "railava-face";
      if (typeof avatarColor === "function")
        face.style.background = avatarColor(p.name);
      face.textContent = (typeof initialsOf === "function")
        ? initialsOf(p.display || p.name) : "?";
      const dot = document.createElement("span");
      dot.className = "railava-dot";
      b.appendChild(face);
      b.appendChild(dot);
      b.onclick = () => {
        if (typeof openDmWith === "function") openDmWith(p.name);
      };
      list.appendChild(b);
    }
  }
  box.appendChild(list);

  /* ── свой «док» внизу рейла ── */
  let dock = tabs.querySelector(".rail-dock");
  if (!dock){
    dock = document.createElement("div");
    dock.className = "rail-dock";
    tabs.appendChild(dock);
  }
  dock.innerHTML = "";
  const me = document.createElement("button");
  me.type = "button";
  me.className = "railme";
  me.title = "Мой профиль — имя, статус, аватар";
  me.setAttribute("aria-label", "Мой профиль");
  const face = document.createElement("span");
  face.className = "railme-face";
  if (typeof avatarColor === "function")
    face.style.background = avatarColor(S.name || "?");
  face.textContent = (typeof initialsOf === "function")
    ? initialsOf(S.name || "?") : "?";
  if (RAIL.myAvatarUrl){
    face.textContent = "";
    face.style.backgroundImage = 'url("' + RAIL.myAvatarUrl + '")';
    face.style.backgroundSize = "cover";
  }
  me.appendChild(face);
  me.onclick = () => {
    if (typeof activateTab === "function") activateTab("profile");
  };
  dock.appendChild(me);
}

/* ── жизненный цикл ──
   Тик каждые 2с: сверяем подпись (S.online обновляется поллингом),
   изредка обновляем список друзей и подтягиваем свою аватарку.
   Всё лениво: пока #app скрыт (до подключения) — рендер пропускаем. */
function tick(){
  const app = document.getElementById("app");
  if (!app || app.classList.contains("hidden")) return;
  render();
  railRefreshFriends();
  loadMyAvatar();
}
if (!window.__railTimer){
  window.__railTimer = setInterval(tick, 2000);
  tick();
}

/* при подключении рейл рисуется сразу (main.js покажет #app;
   следующий тик интервала подхватит данные) */
document.addEventListener("fr-connected", tick);

})();
