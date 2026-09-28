"use strict";
/* profile.js — профиль (v1.9.6): карточка с аватаром, редактирование
   отображаемого имени / статуса / bio, загрузка и удаление аватара,
   список друзей с онлайн-статусом и добавление.

   Раньше вкладка была таблицей read-only + prompt() на смену имени —
   теперь инлайн-редактирование как в приложении, всё то же API
   (POST /profile/update, POST /avatar, GET /friends). */

let PROFILE = null;
let FRIENDS = [];
let AV_URL = null;        // blob-URL текущей аватарки (пересоздаётся)

const AV_MAX_BYTES = 2 * 1024 * 1024;

/* цвет по имени — тот же алгоритм, что в Qt online_user_chip */
function nameHue(name){
  let h = 0;
  const s = String(name || "");
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}
function avatarColor(name){
  return "hsl(" + nameHue(name) + ", 62%, 52%)";
}
function initialsOf(name){
  const s = String(name || "").trim();
  if (!s) return "?";
  const parts = s.split(/\s+/);
  if (parts.length > 1) return (parts[0][0] + parts[1][0]).toUpperCase();
  return s.slice(0, 2).toUpperCase();
}

/* ── тосты: дружелюбные неблокирующие уведомления ──
   v3.2: слева — монолайн-иконка (✓ зелёная / ✕ красная), текст без
   эмодзи — тост выглядит частью дизайн-системы, а не заметкой на полях. */
function toast(msg, kind){
  const box = $("toasts");
  if (!box) return;
  const t = document.createElement("div");
  t.className = "toast " + (kind || "ok");
  t.appendChild(frIcon(kind === "err" ? "x" : "check"));
  t.appendChild(document.createTextNode(msg));
  box.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => {
    t.classList.remove("show");
    setTimeout(() => t.remove(), 350);
  }, 3200);
}

/* ── загрузка профиля ── */
async function refreshProfile(){
  try {
    const {json} = await apiGet("/profile/" + encodeURIComponent(S.name));
    PROFILE = (json && json.profile) || null;
    if (!PROFILE){
      warn("profWarn", "профиль недоступен");
      return;
    }
    renderProfileCard();
    renderProfileTable();
    await loadAvatarThumb();
    await refreshFriends();
  } catch(e){ warn("profWarn", "не удалось загрузить профиль: " + e); }
}

function renderProfileCard(){
  const dn = (PROFILE.display_name || "").trim();
  $("profName").textContent = dn || PROFILE.name || S.name;
  $("profLogin").textContent = (dn && dn !== PROFILE.name)
    ? "логин: " + (PROFILE.name || S.name) : "";
  $("profNameInput").value = dn || "";
  $("profStatusInput").value = PROFILE.status || "";
  $("profBioInput").value = PROFILE.bio || "";
  updCnt("profStatusCnt", PROFILE.status || "", 128);
  updCnt("profBioCnt", PROFILE.bio || "", 2000);
  /* v3.2: статус с иконкой-баблом вместо эмодзи */
  const stMsg = $("profStatusMsg");
  stMsg.textContent = "";
  if (PROFILE.status){
    stMsg.appendChild(frIcon("chat"));
    stMsg.appendChild(document.createTextNode(" " + PROFILE.status));
  }
}

function renderProfileTable(){
  const t = $("profileTable");
  const row = (k, v) => "<tr><td>" + k + "</td><td>" + esc(v) + "</td></tr>";
  t.innerHTML =
    row("Имя входа (аккаунт)", PROFILE.name || S.name) +
    row("В друзьях", (PROFILE.friends || []).length + " чел.") +
    row("На сервере с", (PROFILE.created_at
      ? new Date(PROFILE.created_at * 1000).toLocaleDateString("ru-RU") : "—"));
}

function updCnt(id, text, max){
  const el = $(id);
  if (el) el.textContent = text.length + "/" + max;
}

/* ── аватар ── */
async function loadAvatarThumb(){
  if (AV_URL){ URL.revokeObjectURL(AV_URL); AV_URL = null; }
  const el = $("profAvatar");
  el.style.background = avatarColor(PROFILE && PROFILE.name || S.name);
  el.textContent = initialsOf(PROFILE && PROFILE.display_name || S.name);
  if (!PROFILE || !PROFILE.has_avatar) return;
  try {
    const r = await fetch("/avatar/" + encodeURIComponent(S.name),
      {headers: baseHeaders(), ...fetchTimeout(10000)});
    if (!r.ok) return;
    const blob = await r.blob();
    if (!blob.size) return;
    AV_URL = URL.createObjectURL(blob);
    el.textContent = "";
    el.style.backgroundImage = 'url("' + AV_URL + '")';
    el.style.backgroundSize = "cover";
  } catch(e){ /* аватарка не критична */ }
}

async function uploadAvatar(){
  warn("profWarn", "");
  const f = $("avatarInput").files[0];
  if (!f){ toast("Сначала выбери PNG-файл", "err"); return; }
  if (f.size > AV_MAX_BYTES){
    warn("profWarn", "Файл больше 2 МБ — выбери поменьше или сожми PNG");
    toast("Аватар слишком большой (макс 2 МБ)", "err");
    return;
  }
  $("profSaveStat").textContent = "загружаю аватар…";
  try {
    const buf = await f.arrayBuffer();
    const upHeaders = baseHeaders({"Content-Type": "application/octet-stream"});
    if (S.token){
      try {
        upHeaders["X-Relay-Auth"] = S.token + ":"
          + await hmacHexBin(S.token, binStrOfBuffer(buf));
      } catch(e){}
    }
    const r = await fetch("/avatar", {method: "POST", headers: upHeaders,
      body: buf, ...fetchTimeout(30000)});
    const json = await safeJson(r) || {ok: false, error: "HTTP " + r.status};
    if (json.ok){
      toast("Аватар обновлён ✓");
      $("avatarInput").value = "";
      $("profSaveStat").textContent = "";
      if (PROFILE) PROFILE.has_avatar = true;   /* без этого карточка
         не знала, что картинка уже есть */
      await loadAvatarThumb();
    } else {
      warn("profWarn", json.error || "не сохранилось");
      $("profSaveStat").textContent = "";
    }
  } catch(e){
    warn("profWarn", "ошибка: " + e);
    $("profSaveStat").textContent = "";
  }
}

async function removeAvatar(){
  warn("profWarn", "");
  /* v3.2: системный confirm → своя модалка (danger) */
  const ok = await frConfirm({
    title: "Убрать аватарку?",
    body: "Вместо неё снова будет цветная плитка с инициалами.",
    ok: "Убрать", danger: true,
  });
  if (!ok) return;
  try {
    const upHeaders = baseHeaders({"Content-Type": "application/octet-stream"});
    if (S.token){
      try {
        upHeaders["X-Relay-Auth"] = S.token + ":"
          + await hmacHexBin(S.token, "");
      } catch(e){}
    }
    const r = await fetch("/avatar", {method: "POST", headers: upHeaders,
      body: new Uint8Array(0), ...fetchTimeout(15000)});
    const json = await safeJson(r) || {ok: false, error: "HTTP " + r.status};
    if (json.ok){
      toast("Аватар убран");
      if (PROFILE) PROFILE.has_avatar = false;
      await loadAvatarThumb();
    } else warn("profWarn", json.error || "не получилось");
  } catch(e){ warn("profWarn", "ошибка: " + e); }
}

/* ── сохранение имени/статуса/bio ── */
async function saveProfile(){
  warn("profWarn", "");
  $("profSaveStat").textContent = "сохраняю…";
  const fields = {
    display_name: $("profNameInput").value.trim().slice(0, 32),
    status: $("profStatusInput").value.trim().slice(0, 128),
    bio: $("profBioInput").value.trim().slice(0, 2000),
  };
  if (!fields.display_name){ fields.display_name = S.name; }
  try {
    const {json} = await apiPost("/profile/update", fields);
    if (json.ok){
      PROFILE = json.profile || PROFILE;
      renderProfileCard();
      renderProfileTable();
      $("profSaveStat").textContent = "";
      toast("Профиль сохранён ✓");
      await pollTick();
    } else {
      warn("profWarn", json.error || "не сохранилось");
      $("profSaveStat").textContent = "";
    }
  } catch(e){
    warn("profWarn", "ошибка: " + e);
    $("profSaveStat").textContent = "";
  }
}

/* ── друзья ── */
async function refreshFriends(){
  try {
    const {json} = await apiGet("/friends?name=" + encodeURIComponent(S.name));
    FRIENDS = (json && json.friends) || [];
    renderFriends();
    /* таблица «Данные аккаунта» показывает счётчик друзей — держим её
     в курсе, не перегружая весь профиль */
    if (PROFILE){
      PROFILE.friends = FRIENDS.map(f => f.name);
      if ($("profileTable")) renderProfileTable();
    }
  } catch(e){ /* молча: вкладка может быть закрыта */ }
}

function renderFriends(){
  const box = $("friendsBox");
  if (!box) return;
  box.innerHTML = "";
  if (!FRIENDS.length){
    box.innerHTML = '<div class="hint" style="padding:8px 4px">Пока никого — ' +
      "введи логин друга ниже и нажми «＋ Добавить». Он появится здесь, " +
      "а его онлайн-статус будет виден в реальном времени.</div>";
    return;
  }
  for (const f of FRIENDS){
    const row = document.createElement("div");
    row.className = "friendrow" + (f.online ? "" : " off");
    /* v2.0.2: клик по строке друга открывает с ним ЛС (крестик удаления
       внутри глушит всплытие). Курсор-рука уже в CSS (.friendrow). */
    row.title = "Открыть личные сообщения с " + (f.display_name || f.name);
    row.onclick = () => {
      if (typeof openDmWith === "function") openDmWith(f.name);
    };
    const av = document.createElement("span");
    av.className = "friendava";
    av.style.background = avatarColor(f.name);
    av.textContent = initialsOf(f.display_name || f.name);
    const info = document.createElement("span");
    info.className = "friendinfo";
    const nm = document.createElement("span");
    nm.className = "friendname";
    nm.textContent = f.display_name || f.name;
    info.appendChild(nm);
    if (f.status){
      const st = document.createElement("span");
      st.className = "friendstatus";
      st.textContent = f.status;
      info.appendChild(st);
    }
    const dot = document.createElement("span");
    dot.className = "frienddot " + (f.online ? "on" : "");
    dot.title = f.online ? "в сети" : "не в сети";
    const rm = document.createElement("button");
    rm.className = "sec frdel";
    rm.title = "Удалить из друзей";
    rm.setAttribute("aria-label", "Удалить " + (f.display_name || f.name) + " из друзей");
    rm.appendChild(frIcon("x"));
    rm.onclick = (e) => {
      e.stopPropagation();   /* клик по ✕ не должен открывать ЛС */
      removeFriend(f.name);
    };
    row.appendChild(av); row.appendChild(info);
    row.appendChild(dot); row.appendChild(rm);
    box.appendChild(row);
  }
}

async function addFriend(){
  warn("friendsWarn", "");
  const name = $("friendName").value.trim();
  if (!name){ warn("friendsWarn", "Введи имя друга"); return; }
  try {
    const {json} = await apiPost("/friends/add", {name});
    if (json.ok){
      $("friendName").value = "";
      toast("Друг добавлен: " + name);
      await refreshFriends();
    } else warn("friendsWarn", json.error || "не добавилось");
  } catch(e){ warn("friendsWarn", "ошибка: " + e); }
}

async function removeFriend(name){
  /* v3.2: подтверждение своей модалкой — с иконкой и в теме */
  const ok = await frConfirm({
    title: "Удалить " + name + " из друзей?",
    body: "Личная переписка останется — друг просто исчезнет из списка.",
    ok: "Удалить", danger: true,
  });
  if (!ok) return;
  try {
    const {json} = await apiPost("/friends/remove", {name});
    if (json.ok){
      toast("Удалён: " + name);
      await refreshFriends();
    } else warn("friendsWarn", json.error || "не удалилось");
  } catch(e){ warn("friendsWarn", "ошибка: " + e); }
}

/* счётчики при вводе */
$("profStatusInput").addEventListener("input", e =>
  updCnt("profStatusCnt", e.target.value, 128));
$("profBioInput").addEventListener("input", e =>
  updCnt("profBioCnt", e.target.value, 2000));
$("btnSaveProfile").onclick = saveProfile;
$("avatarInput").onchange = uploadAvatar;
$("btnRemoveAvatar").onclick = removeAvatar;
$("btnAddFriend").onclick = addFriend;
$("friendName").addEventListener("keydown", e => {
  if (e.key === "Enter") addFriend();
});

/* ── сводка хоста (v2.0.3) ──
   GET /server/stats (появился в v2.0.2): uptime, счётчики событий,
   сессий, файлов/каналов, голос/voxel, бэкенд JSON. Только счётчики,
   никакой лички — за тем же ключом доступа, что и весь API.
   Подгружается ТОЛЬКО по кнопке ↻: вкладка профиля и так обновляется,
   а служебная сводка не должна плодить лишний трафик на каждый тик. */
function fmtUptime(s){
  if (!(s > 0)) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return d + " д " + h + " ч";
  if (h) return h + " ч " + m + " мин";
  if (m) return m + " мин";
  return s + " с";
}
async function loadHostStats(){
  const t = $("hostTable");
  if (!t) return;
  t.innerHTML = '<tr><td colspan="2" class="hint">спрашиваю сервер…</td></tr>';
  try {
    const {json} = await apiGet("/server/stats");
    if (!json || !json.ok){
      t.innerHTML = '<tr><td colspan="2" class="warn">сервер не отдал сводку' +
        (json && json.error ? ": " + esc(json.error) : "") + "</td></tr>";
      return;
    }
    const rows = [
      ["Хост", json.name || "—"],
      ["Работает", json.running ? "да" : "нет"],
      ["Аптайм", fmtUptime(json.uptime_s)],
      ["В сети", String(json.online || 0) + " чел."],
      ["Активных сессий", String(json.sessions || 0)],
      ["Событий всего (seq)", String(json.next_seq ?? "—")],
      ["Событий в кеше RAM", String(json.events_cached ?? "—")],
      ["Файлов в витрине", String(json.files_tracked ?? "—")],
      ["Файлов в ЛС", String(json.dm_files_tracked ?? "—")],
      ["Каналов", String(json.channels || 0)],
      ["Голосовой сервер", json.voice ? "включён" : "выключен"],
      ["Voxel-сервер", json.voxel ? "включён" : "выключен"],
      ["JSON-бэкенд", json.json_backend || "—"],
    ];
    t.innerHTML = rows.map(([k, v]) =>
      "<tr><td>" + esc(k) + "</td><td>" + esc(v) + "</td></tr>").join("");
  } catch(e){
    t.innerHTML = '<tr><td colspan="2" class="warn">нет связи: ' +
      esc(String(e)) + "</td></tr>";
  }
}
$("btnHostStats").onclick = loadHostStats;
