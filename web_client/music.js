/* music.js — синхронный музыкальный плеер веб-клиента (v3.3.0).

Модель «хост стримит — все слушают — каждый может выбрать»:
- Библиотека: GET /music/library (файлы из папки relay_data/music хоста).
- Состояние:  GET /music/sync каждые ~2.5с (тик встроен в общий поллинг,
  main.js). Сервер отдаёт position от СВОИХ часов + server_time, клиент
  корректирует дрейф локального audio-элемента.
- Управление: POST /music/play|pause|skip|seek|queue/* — права решает
  сервер (open_dj: управляют все, иначе хост/админы; очередь открыта всем).
- Конец трека: браузер честно сообщает POST /music/ended — сервер
  переходит к следующему, даже если длительность файла неизвестна.
- Внешние API: POST /music/api/search|import (задел на большую
  библиотеку — lib/music_library.py: iTunes без ключа, Jamendo с ключом).

Автоплей: браузеры блокируют play() без жеста пользователя — при первом
блоке показываем оверлей «Слушать со всеми» (один клик — и дальше всё
синхронно навсегда в этой сессии).
*/

"use strict";

/* ── v3.4.2: константы синхронизации ─────────────────────────
   MUSIC_SYNC_MS = обычный тик после прогрева (1 раз в секунду)
   MUSIC_SYNC_FAST_MS = первые N секунд нового трека (400 мс)
     (v3.4.2 fix: было 200 мс на 12 с — каждый клиент за первые секунды
     трека посылал 5 запросов/с, а сервер на каждый /music/sync брал
     общий лок бота, который в это же время держали дисковые записи
     play/pause. На слабом хосте это складывалось в очередь и подвешивало
     и сервер, и GUI-кнопки хоста. 400 мс × 8 с — тот же результат
     выравнивания без шторма; мягкая докрутка playbackRate всё равно
     продолжает подгонять всех после окна быстрого синка.)
   DRIFT_SOFT_SEC = 80..500 мс — плавно догоняем playbackRate 0.95/1.05
   DRIFT_HARD_SEC = >500 мс — жёсткий seek, иначе никогда не догоним
   ───────────────────────────────────────────────────────── */
const MUSIC_SYNC_MS = 1000;        /* обычный тик: раз в 1 сек */
const MUSIC_SYNC_FAST_MS = 400;     /* первые 8 с трека: раз в 400 мс */
const MUSIC_SYNC_FAST_WINDOW = 8;   /* сколько секунд держать ускоренный синк */
const MUSIC_DRIFT_SOFT_SEC = 0.08;  /* 80 мс: порог мягкой коррекции */
const MUSIC_DRIFT_HARD_SEC = 0.5;   /* 500 мс: порог жёсткого перескака */
const MUSIC_LIB_CAP = 300;         /* максимум строк библиотеки на экране */

const MC = {
  audio: null,
  lib: [],                /* библиотека хоста */
  libLoaded: false,
  filter: "",             /* текущий фильтр поиска */
  state: null,            /* последний /music/sync */
  version: -1,            /* state_version, чтобы не перерисовывать зря */
  currentId: "",
  previewing: false,      /* слушаем превью из внешнего API (не синхронно) */
  gesture: false,         /* был пользовательский жест (autoplay unlocked) */
  joinShown: false,
  progressTimer: null,
  syncing: false,
  /* (NEW v3.4.2) — поля синхронизации */
  clockSkew: 0,           /* смещение часов клиента vs сервера: clientNow - serverNow */
  _wantSeek: 0,           /* позиция, которую надо применить ПОСЛЕ canplay */
  _rateCorrectionTimer: null, /* таймер сброса playbackRate на 1.0 */
  _fastSyncUntil: 0,      /* до какого серверного времени держать ускоренный синк */
};

/* ───────────────────────── утилиты ───────────────────────── */

function fmtDur(s){
  if (s == null || isNaN(s) || s <= 0) return "–:––";
  s = Math.round(s);
  const m = Math.floor(s / 60), ss = s % 60;
  if (m >= 60) return Math.floor(m/60) + ":" + String(m%60).padStart(2,"0") + ":" + String(ss).padStart(2,"0");
  return m + ":" + String(ss).padStart(2,"0");
}

function musicTrackUrl(id){ return "/music/stream/" + encodeURIComponent(id); }

function mIcon(btn, name){
  if (!btn) return;
  btn.textContent = "";
  btn.appendChild(frIcon(name));
}

/* ───────────────────────── инициализация ───────────────────────── */

function initMusicPlayer(){
  if (MC.audio) { fetchMusicLibrary(); return; }
  MC.audio = new Audio();
  MC.audio.preload = "auto";
  try { MC.audio.volume = (+localStorage.getItem("wr_music_vol") || 70) / 100; }
  catch(e){ MC.audio.volume = 0.7; }

  MC.audio.addEventListener("ended", () => {
    /* честный отчёт серверу: он переключит ВСЕХ на следующий трек */
    if (MC.previewing) return;
    apiPost("/music/ended", {}).catch(()=>{});
  });
  MC.audio.addEventListener("error", () => {
    if (MC.previewing) return;
    musicStat("Не удалось загрузить трек", true);
  });

  /* первый жест на странице снимает блок автоплея */
  const unlock = () => {
    MC.gesture = true;
    document.removeEventListener("pointerdown", unlock);
    document.removeEventListener("keydown", unlock);
    if (MC.joinShown) hideMusicJoin();
    /* если сервер уже играет, а нас заглушили — присоединяемся */
    if (MC.state && MC.state.playing && !MC.previewing &&
        MC.audio.paused && MC.currentId){
      MC.audio.play().catch(()=>{});
    }
  };
  document.addEventListener("pointerdown", unlock);
  document.addEventListener("keydown", unlock);

  const vs = $("musicVol");
  if (vs){
    try { vs.value = Math.round(MC.audio.volume * 100); } catch(e){}
    $("musicVolVal").textContent = vs.value + "%";
    vs.oninput = () => {
      const v = +vs.value;
      MC.audio.volume = v / 100;
      $("musicVolVal").textContent = v + "%";
      try { localStorage.setItem("wr_music_vol", String(v)); } catch(e){}
    };
  }

  setupMusicControls();
  fetchMusicLibrary();
  musicTick();           /* сразу, не дожидаясь тика поллинга */
}

function setupMusicControls(){
  const on = (id, fn) => { const b = $(id); if (b) b.onclick = fn; };
  on("btnMPlay", () => {
    if (MC.previewing){ MC.previewing = false; }
    const st = MC.state;
    if (!st || !st.track){
      toast("Сначала выбери трек из библиотеки");
      return;
    }
    if (st.playing){ apiPost("/music/pause", {}).then(musicToastRes); }
    else {
      apiPost("/music/play", {}).then((r) => {
        musicToastRes(r);
        if (r.json && r.json.ok) tryLocalPlay();
      });
    }
  });
  on("btnMNext", () => apiPost("/music/skip", {}).then(musicToastRes));
  on("btnMPrev", () => {
    /* «назад» — перемотка текущего в начало (очередь не ломаем) */
    apiPost("/music/seek", {position: 0}).then(musicToastRes);
  });
  on("btnMRescan", async () => {
    musicStat("Обновляю…");
    const r = await apiPost("/music/rescan", {});
    await fetchMusicLibrary();
    if (r.json && r.json.ok)
      musicStat("Библиотека: " + (r.json.count ?? MC.lib.length) + " треков");
    else musicStat((r.json && r.json.error) || "не удалось", true);
  });
  on("btnMJoin", () => { hideMusicJoin(); tryLocalPlay(); });

  const bar = $("musicBar");
  if (bar){
    bar.onclick = (e) => {
      const st = MC.state;
      if (!st || !st.track) return;
      const r = bar.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
      const dur = st.track.duration || MC.audio.duration;
      if (!dur || dur <= 0){ toast("Длительность неизвестна — перемотка недоступна"); return; }
      apiPost("/music/seek", {position: ratio * dur}).then(musicToastRes);
    };
  }

  const search = $("musicSearch");
  if (search){
    search.addEventListener("input", () => {
      MC.filter = search.value.trim().toLowerCase();
      renderMusicList();
      $("musicApiBox").classList.add("hidden");
    });
    search.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      musicApiSearch(search.value.trim());
    });
  }
}

/* ───────────────────────── библиотека ───────────────────────── */

async function fetchMusicLibrary(){
  try {
    const {json} = await apiGet("/music/library");
    if (json && json.ok){
      MC.lib = json.tracks || [];
      MC.libLoaded = true;
      renderMusicList();
    }
  } catch(e){ /* вкладка музыки доступна и без сети — просто пусто */ }
}

/* v3.3.0: вызывается из main.js при активации вкладки */
function musicRefresh(){
  initMusicPlayer();
  fetchMusicLibrary();
}

function musicStat(txt, isErr){
  const el = $("musicStat");
  if (!el) return;
  el.textContent = txt || "";
  el.style.color = isErr ? "var(--danger,#ff6b6b)" : "";
}

function musicToastRes(res){
  if (res && res.json && !res.json.ok)
    toast(res.json.error || "Ошибка музыки");
}

/* ───────────────────────── синхронизация ───────────────────────── */

/* ── (NEW v3.4.2) musicTick с АДАПТИВНОЙ ЧАСТОТОЙ ────────────
   Обычно: раз в MUSIC_SYNC_MS (1000 мс).
   Первые 12 секунд после смены трека: MUSIC_SYNC_FAST_MS (200 мс),
   потому что именно в первые секунды все клиенты выравниваются и
   старый 2.5с-тик был СЛИШКОМ медленным — первые 10 секунд у всех
   играло вразнобой. */
async function musicTick(){
  /* Встроен в общий поллинг (main.js, throttle MUSIC_SYNC_MS).
     Не дергаем, пока не открыто соединение с сервером. */
  if (!S.name || MC.syncing) return;
  MC.syncing = true;
  try {
    const {json} = await apiGet("/music/sync");
    if (json && json.ok) {
      applyMusicState(json);
      // (NEW) Ещё в окне быстрого синка — запустим одиночный таймер
      // на следующий синк через 200 мс, не ждя общий pollTick.
      const tClientNow = Date.now() / 1000;
      const serverNow = tClientNow - MC.clockSkew;
      if (serverNow < MC._fastSyncUntil && !MC.previewing) {
        setTimeout(musicTick, MUSIC_SYNC_FAST_MS);
      }
    }
  } catch(e){ /* сервер недоступен — тикнем позже */ }
  finally { MC.syncing = false; }
}

function applyMusicState(st){
  MC.state = st;
  const track = st.track || null;
  const verChanged = (st.state_version ?? -1) !== MC.version;
  const trackChanged = (track ? track.id : "") !== MC.currentId;
  MC.version = st.state_version ?? MC.version;

  /* ── (NEW v3.4.2) КОРРЕКЦИЯ СМЕЩЕНИЯ ЧАСОВ (clock skew) ──
     Date.now() на клиенте ВСЕГДА ошибается (Windows-часы спешат/отстают
     на 2-5 секунд у 40% пользователей — это ГЛАВНАЯ причина дрейфа).
     Первый же sync сохраняем skew = clientTime - serverTime, дальше
     везде считаем serverNow = clientNow - skew. */
  const tClientNow = Date.now() / 1000;
  const tServerNow = st.server_time ? (tClientNow - (tClientNow - st.server_time)) : tClientNow;
  // Усредняем по последним 3 синкам (плавнее, чем один замер)
  const thisSkew = tClientNow - (st.server_time || tClientNow);
  MC.clockSkew = MC.clockSkew ? (MC.clockSkew * 0.6 + thisSkew * 0.4) : thisSkew;
  const serverNow = tClientNow - MC.clockSkew;

  /* превью внешнего API гасим, когда сервер переключил всех на новое */
  if (trackChanged && st.playing && MC.previewing){
    MC.previewing = false;
  }

  if (trackChanged){
    MC.currentId = track ? track.id : "";
    if (track){
      /* ── (NEW v3.4.2) СНАЧАЛА canplay, ПОТОМ seek! ───────────
         До этого: сразу писали currentTime → браузер МОЛЧА игнорировал,
         пока не скачал заголовки. В итоге все клиенты начинали с 0:00
         и расходились на 30+ секунд в первые минуты. */
      MC.audio.src = musicTrackUrl(track.id);
      MC._wantSeek = st.position || 0;
      // Ставим «ускоренный синк» на 12 секунд после смены трека
      MC._fastSyncUntil = serverNow + MUSIC_SYNC_FAST_WINDOW;
      const _onReady = () => {
        try { MC.audio.currentTime = Math.max(0, MC._wantSeek); } catch(e){}
        MC.audio.removeEventListener("canplay", _onReady);
      };
      MC.audio.addEventListener("canplay", _onReady);
      renderMusicList();          /* подсветить играющий */
    } else {
      MC.audio.removeAttribute("src");
      MC.audio.load();
      MC._fastSyncUntil = 0;
    }
  } else if (track && st.playing && !MC.previewing && !MC.audio.paused){
    /* ── (NEW v3.4.2) МЯГКАЯ СИНХРОНИЗАЦИЯ: playbackRate вместо seek ──
       ▸ drift < 80мс   → ничего не делаем (человек не слышит)
       ▸ 80..500 мс     → плавно догоняем: скорость 0.95/1.05
       ▸ >500 мс        → жёсткий seek (иначе никогда не догоним)
       В итоге пользователь ВООБЩЕ НЕ СЛЫШИТ синхронизации, никаких щелчков!
       Именно так делают Spotify / YouTube / Discord. */
    const age_s = Math.max(0, serverNow - (st.server_time || serverNow));
    const est = (st.position || 0) + age_s;  // какая должна быть позиция СЕЙЧАС
    const drift = MC.audio.currentTime - est;  // >0 = мы ОПЕРЕЖАЕМ сервер

    if (Math.abs(drift) > MUSIC_DRIFT_HARD_SEC){
      // Больше 0.5 сек — жёсткая коррекция, иначе не догоним никогда
      try { MC.audio.currentTime = Math.max(0, est); } catch(e){}
      MC.audio.playbackRate = 1;
    } else if (Math.abs(drift) > MUSIC_DRIFT_SOFT_SEC){
      // 80..500 мс: мягкая коррекция скоростью (±5%)
      const targetRate = drift > 0 ? 0.95 : 1.05;
      if (Math.abs(MC.audio.playbackRate - targetRate) > 0.01){
        MC.audio.playbackRate = targetRate;
        // Через сколько вернём playbackRate = 1.0? — время чтобы догнать drift
        const fix_ms = Math.min(8000, Math.abs(drift) * 25000);
        if (MC._rateCorrectionTimer) clearTimeout(MC._rateCorrectionTimer);
        MC._rateCorrectionTimer = setTimeout(() => {
          try { MC.audio.playbackRate = 1; } catch(e){}
        }, fix_ms);
      }
    }
  } else if (track && !st.playing){
    // Пауза: всегда возвращаем скорость на 1.0
    MC.audio.playbackRate = 1;
  }

  /* громкость сервера — только дефолт для НОВОГО слушателя;
     свой ползунок пользователь крутит локально (localStorage) */

  if (st.playing && track){
    if (MC.audio.paused && !MC.previewing) tryLocalPlay();
  } else if (!st.playing && !MC.previewing){
    if (!MC.audio.paused) MC.audio.pause();
  }

  if (verChanged || trackChanged){
    renderMusicNow(st);
    renderMusicQueue(st);
    mIcon($("btnMPlay"), st.playing ? "pause" : "play");
  }
  /* прогресс-бар рисуем всегда — он течёт локально */
}

function tryLocalPlay(){
  if (!MC.audio || !MC.audio.src) return;
  const p = MC.audio.play();
  if (p && p.catch){
    p.catch((e) => {
      if (e && e.name === "NotAllowedError"){
        showMusicJoin();          /* нужен жест пользователя */
      }
    });
  }
}

function showMusicJoin(){
  if (MC.joinShown) return;
  MC.joinShown = true;
  const j = $("musicJoin");
  if (j) j.classList.remove("hidden");
}
function hideMusicJoin(){
  MC.joinShown = false;
  const j = $("musicJoin");
  if (j) j.classList.add("hidden");
}

/* локальный ход прогресс-бара между тиками синхронизации */
setInterval(() => {
  const st = MC.state;
  if (!st || !st.track) return;
  const playing = st.playing && !MC.audio.paused && !MC.previewing;
  const pos = playing ? MC.audio.currentTime : (st.position || 0);
  const dur = st.track.duration || MC.audio.duration || 0;
  const pct = dur > 0 ? Math.min(100, (pos / dur) * 100) : 0;
  const fill = $("musicBarFill");
  if (fill) fill.style.width = pct + "%";
  $("musicPos").textContent = fmtDur(pos);
  $("musicDur").textContent = fmtDur(dur);
}, 500);

/* ───────────────────────── отрисовка ───────────────────────── */

function renderMusicNow(st){
  const t = st.track;
  $("musicTitle").textContent = t ? (t.title || t.filename) : "Ничего не играет";
  const parts = [];
  if (t && t.artist) parts.push(t.artist);
  if (t && t.folder) parts.push(t.folder);
  $("musicSub").textContent = !t
    ? "Выбери трек из библиотеки — он зазвучит у всех"
    : (st.playing ? "Играет синхронно у всех" : "Пауза")
      + (parts.length ? " · " + parts.join(" · ") : "");
  $("musicCover").classList.toggle("playing", !!st.playing);
}

function renderMusicQueue(st){
  const box = $("musicQueue");
  if (!box) return;
  const q = st.queue || [];
  box.innerHTML = "";
  if (!q.length){
    box.innerHTML = '<div class="hint">Очередь пуста — жми «+» у любого трека.</div>';
    return;
  }
  q.forEach((item, i) => {
    const t = item.track || {};
    const row = document.createElement("div");
    row.className = "mqrow";
    row.innerHTML =
      '<span class="mq-idx">' + (i+1) + '</span>' +
      '<div class="mrow-main"><div class="mrow-title">' + esc(t.title || t.filename || "?") + '</div>' +
      '<div class="mrow-sub hint">' + esc(t.artist || "") + '</div></div>' +
      '<span class="mq-by hint">от ' + esc(item.by || "?") + '</span>';
    const del = document.createElement("button");
    del.className = "sec";
    del.title = "Убрать из очереди";
    del.appendChild(frIcon("trash"));
    del.onclick = () => apiPost("/music/queue/remove", {index: i+1}).then(musicToastRes);
    row.appendChild(del);
    box.appendChild(row);
  });
}

function renderMusicList(){
  const box = $("musicList");
  if (!box) return;
  if (!MC.libLoaded){
    box.innerHTML = '<div class="hint">Загружаю библиотеку…</div>';
    return;
  }
  const f = MC.filter;
  const all = MC.lib;
  const list = f
    ? all.filter(t =>
        (t.title || "").toLowerCase().includes(f) ||
        (t.artist || "").toLowerCase().includes(f) ||
        (t.filename || "").toLowerCase().includes(f) ||
        (t.folder || "").toLowerCase().includes(f))
    : all;
  $("musicCnt").textContent = all.length
    ? (list.length === all.length ? all.length + " треков"
       : list.length + " из " + all.length)
    : "";
  box.innerHTML = "";
  if (!all.length){
    box.innerHTML = '<div class="hint">Библиотека пуста. Хост: закинь файлы в ' +
      'папку <b>relay_data/music</b> рядом с приложением и нажми ' +
      '«Обновить библиотеку».</div>';
    return;
  }
  if (!list.length){
    box.innerHTML = '<div class="hint">Ничего не найдено. Enter — поиск во внешних API.</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  list.slice(0, MUSIC_LIB_CAP).forEach((t) => {
    const cur = t.id === MC.currentId;
    const row = document.createElement("div");
    row.className = "mrow" + (cur ? " cur" : "");
    const sub = [t.artist, t.folder, fmtDur(t.duration)]
      .filter(Boolean).join(" · ");
    row.innerHTML =
      '<div class="mrow-main"><div class="mrow-title">' + esc(t.title || t.filename) + '</div>' +
      '<div class="mrow-sub hint">' + esc(sub) + '</div></div>';
    const add = document.createElement("button");
    add.className = "sec";
    add.title = "В очередь (играть после текущего)";
    add.appendChild(frIcon("plus"));
    add.onclick = (e) => {
      e.stopPropagation();
      apiPost("/music/queue/add", {track_id: t.id}).then((r) => {
        musicToastRes(r);
        if (r.json && r.json.ok)
          toast(r.json.started ? "Играет: " + (t.title || t.filename)
                : "В очереди: " + (t.title || t.filename));
      });
    };
    const main = row.querySelector(".mrow-main");
    main.onclick = () => {
      /* play прямо сейчас (если open_dj выключен — сервер подскажет) */
      apiPost("/music/play", {track_id: t.id}).then((r) => {
        musicToastRes(r);
        if (r.json && r.json.ok){
          MC.gesture = true;
          hideMusicJoin();
          tryLocalPlay();
        }
      });
    };
    row.appendChild(add);
    frag.appendChild(row);
  });
  box.appendChild(frag);
  if (list.length > MUSIC_LIB_CAP){
    const more = document.createElement("div");
    more.className = "hint";
    more.textContent = "… и ещё " + (list.length - MUSIC_LIB_CAP) +
      " — уточни фильтр";
    box.appendChild(more);
  }
}

/* ───────────────────────── внешние API (задел) ───────────────────────── */

async function musicApiSearch(query){
  if (!query){ return; }
  const box = $("musicApiBox");
  box.classList.remove("hidden");
  box.innerHTML = '<div class="hint">Ищу «' + esc(query) + '» во внешних библиотеках…</div>';
  const r = await apiPost("/music/api/search", {query});
  const j = r.json;
  if (!j || !j.ok){
    box.innerHTML = '<div class="warn">' + esc((j && j.error) ||
      "Поиск недоступен") + "</div>";
    return;
  }
  const items = j.results || [];
  if (!items.length){
    box.innerHTML = '<div class="hint">Во внешних библиотеках ничего нет.' +
      (j.errors && j.errors.length ? " " + esc(j.errors.join("; ")) : "") + '</div>';
    return;
  }
  box.innerHTML = '<div class="music-api-head hint">Внешние библиотеки — ' +
    items.length + ' совпадений (превью можно слушать сразу, «Импорт» скачивает файл в библиотеку хоста)</div>';
  items.slice(0, 12).forEach((t) => {
    const row = document.createElement("div");
    row.className = "mrow api";
    row.innerHTML =
      '<div class="mrow-main"><div class="mrow-title">' + esc(t.title) + '</div>' +
      '<div class="mrow-sub hint">' + esc([t.artist, t.album, fmtDur(t.duration), "[" + t.provider + "]"]
        .filter(Boolean).join(" · ")) + '</div></div>';
    if (t.stream_url){
      const prev = document.createElement("button");
      prev.className = "sec";
      prev.title = "Слушать превью (только у себя)";
      prev.appendChild(frIcon("headphones"));
      prev.onclick = () => {
        MC.previewing = true;
        MC.audio.pause();
        MC.audio.src = t.stream_url;
        MC.audio.currentTime = 0;
        tryLocalPlay();
        toast("Превью (30с) — только у тебя, синхронизация приостановлена");
      };
      row.appendChild(prev);
    }
    const imp = document.createElement("button");
    imp.className = "sec";
    imp.title = "Скачать в библиотеку хоста";
    imp.appendChild(frIcon("download"));
    imp.onclick = async () => {
      toast("Импорт запущен на хосте…");
      const rr = await apiPost("/music/api/import",
        {provider: t.provider, track_id: t.id});
      musicToastRes(rr);
      if (rr.json && rr.json.ok){
        setTimeout(fetchMusicLibrary, 6000);
        setTimeout(fetchMusicLibrary, 15000);
      }
    };
    row.appendChild(imp);
    box.appendChild(row);
  });
}
