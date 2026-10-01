"use strict";
/* voice.js — голосовой канал браузера: WS-мост к VoiceMixer хоста.
   Работает только по https (микрофон браузер отдаёт лишь в secure
   context). PCM int16 mono 48kHz (20мс-кадры) — те же фреймы, что и в Qt-клиенте (v2.0: fullband).

   v1.9.6 — настройки звука как в приложении:
   - выбор микрофона (deviceId) и динамика (AudioContext.setSinkId,
     Chrome 110+; в остальных браузерах селектор честно помечен);
   - громкость собеседников — мастер-гейн, применяется на живом звуке;
   - вкл/выкл обработки браузера (шумодав/эхо/AGC) с живым
     переподключением микрофона;
   - метр уровня микрофона (живой отклик «тебя слышно»);
   - выбор сохраняется в localStorage (wr_mic/wr_spk/wr_vol/wr_dsp).

   Качество звука (v1.8.0):
   - AudioWorklet (отдельный аудио-поток): захват и воспроизведение не
     лагают от перерисовок/GC главного потока. ScriptProcessor остался
     как fallback для старых браузеров.
   - Адаптивный джиттер-буфер: старт 80мс, после каждого недобора +20мс
     (максимум 360мс — v3.6.4 под ZeroTier/VPN), при стабильном звуке
     медленно сжимается до 60мс.
   - Фейды на стыках (2мс): звук→тишина→звук без щелчков. */

var VC = {
  ws: null, ctx: null, stream: null, on: false, muted: false, busy: false,
  hostVersion: "",     // v3.6.4: версия приложения хоста (spk_all «v»)
  pend: [],            // накопитель входа до батча 960 сэмплов (20мс)
  playQ: [],           // jitter-буфер (SPA-fallback): Float32Array-куски
  playPos: 0, playStarted: false, playGap: true,
  resPos: 0,           // дробный остаток ресемплера (если контекст ≠ 48к)
  capNode: null, outNode: null, playPort: null, worklet: false,
  srcNode: null,       // v1.9.6: MediaStreamSource (для живой смены мики)
  masterGain: null,    // v1.9.6: громкость собеседников
  jitterTarget: 4,     // пре-буфер в 20мс-фреймах (80мс)
  lastUnderAt: 0,
  speak: {},           // кто говорит: {имя: bool} — control-фреймы моста
  mySpeak: false, mySil: 0,
  wsPath: "", httpsPort: 0, statTimer: null,
  devicesGranted: false, // метки устройств доступны после разрешения
  // v3.4.1 FIX: авто-реконнект + heartbeat
  reconnectAttempts: 0,  // счётчик для экспоненциального backoff
  reconnectTimer: null,  // таймер следующей попытки переподключения
  // v3.5.5: лестница задержек — RECONNECT_LADDER_MS (см. scheduleReconnect)
  hbTimer: null,         // таймер heartbeat {t:hb} 15 сек (v3.5.5)
  intentionalDisconnect: false, // true = пользователь сам кликнул «Отключиться»
  wsGoneAt: 0,           // (v3.5.5) когда пропала связь — для мягких сообщений
  lastInboundAt: 0,      // (v3.5.5) последний вход с моста — liveness-часы
  /* v3.6.6 (R4): КАЧЕСТВО СВЯЗИ — как в Qt-движке (v3.5.7). Раз в 2с шлём
     {t:ping,id:N} — мост проксирует его микшеру, pong (текст JSON с
     счётчиками in/out/odd) возвращается сюда. RTT — время ping→pong;
     потери ↑ — дельта (послали мы − принял микшер)/послали, потери ↓ —
     дельта (отправил микшер − приняли мы)/отправил. Пороги зеркалят
     voice/config.py: good ≤80мс/≤2%, bad >200мс/>8%. */
  pingTimer: null,       // таймер {t:ping} раз в 2с
  pingId: 0, pingAt: {}, // id → performance.now() отправки (ждём pong)
  rttMs: 0, upPct: 0, downPct: 0,  // EMA — как у Qt-движка (0.7/0.3)
  oddOut: 0,             // кадры, отвергнутые микшером по размеру
  sentFrames: 0, recvFrames: 0,    // наши счётчики 20мс-кадров
  lastIn: null, lastOut: null, lastSent: null, lastRecv: null, // снапшоты
  /* v3.6.8 (проверки веба): сторож тишины микрофона. Если с момента
     подключения микрофон НЕ издал ни звука (метр на нуле) — один раз
     подскажем, что собеседник тебя точно не слышит. */
  micVoiceSeen: false,   // был ли хоть один живой кадр с микрофона
  micHintShown: false,   // подсказку «микрофон молчит» уже показали
  /* v3.7.0 (чистота звука): телеметрия плейаута и аплинка.
     unders — недоборы джиттер-буфера (в ворклет и здесь, для статуса);
     upDrops — кадры микрофона, срезанные сторожем ws.bufferedAmount:
     при заторе аплинка лучше потерять кусок НА ЛЕТУ, чем копить
     устаревший звук — иначе после затора собеседнику проигрывается
     «стена прошлого» с растущей задержкой (эффект старого скайпа). */
  unders: 0, upDrops: 0, playLevel: 0,
  UPDROP_LIMIT_BYTES: 16384,   // ~8 кадров (160мс) очереди — дальше срезаем
};

function voiceWarn(msg){ $("voiceWarn").textContent = msg || ""; }
async function voiceRefresh(){
  voiceWarn("");
  try {
    const {json} = await apiGet("/voice/info");
    VC.wsPath = (json && json.voice_ws_path) || "";
    VC.httpsPort = (json && json.https_port) || 0;
  } catch(e){ VC.wsPath = ""; VC.httpsPort = 0; }
  $("voiceHttpsHint").textContent = (location.protocol !== "https:" && VC.httpsPort)
    ? "https://" + location.host + " (тот же порт, что и сейчас)"
    : "эту страницу (уже https)";
  $("voiceStat").textContent = VC.on ? "подключено"
    : (VC.wsPath ? "мост доступен" : "мост недоступен (нет websockets на хосте)");
  /* v1.9.5: кнопку НЕ дизейблим навсегда — разовый сбой сети при заходе на
     вкладку раньше оставлял «мост недоступен» с мёртвой кнопкой, хотя мост
     жив (ретрая внутри вкладки не было). connectVoice сам переспрашивает. */
  refreshVoiceDevices();   /* v1.9.6: заодно свежий список устройств */
}

/* v3.2: единый способ перерисовать кнопку с иконкой + подписью.
   Раньше код делал btn.textContent = "🎙 Подключиться" — и это стирало
   статичную SVG-иконку, вбитую icons.js. Теперь рисуем иконку из
   спрайта сами (frIcon), подпись рядом — как в дизайн-системе. */
function voiceBtn(btnId, icon, text){
  const b = $(btnId);
  if (!b) return;
  b.textContent = "";
  b.appendChild(frIcon(icon));
  b.appendChild(document.createTextNode(" " + text));
}

/* ───────────────── v1.9.6: устройства и звук ───────────────── */

function voiceSetting(key, val){
  try {
    if (val === undefined) return localStorage.getItem(key) || "";
    localStorage.setItem(key, String(val));
  } catch(e){}
  return String(val);
}

function micConstraints(deviceId){
  const dsp = $("dspCheck") ? $("dspCheck").checked : true;
  const audio = {
    echoCancellation: dsp, noiseSuppression: dsp, autoGainControl: dsp,
    channelCount: 1,
  };
  if (deviceId) audio.deviceId = {exact: deviceId};
  return {audio};
}

/* Список микрофонов/динамиков. ДО разрешения на микрофон браузер не
   отдаёт метки (label="") — показываем «Микрофон N»; после первого
   подключения (или любого getUserMedia) метки появляются, список
   обновится через devicechange/voiceRefresh. */
async function refreshVoiceDevices(){
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices)
    return;
  let devices = [];
  try { devices = await navigator.mediaDevices.enumerateDevices(); }
  catch(e){ return; }

  const micSel = $("micSel"), spkSel = $("spkSel");
  if (!micSel || !spkSel) return;
  const micId = voiceSetting("wr_mic");
  const spkId = voiceSetting("wr_spk");

  micSel.innerHTML = "";
  let mi = 0, micFound = false;
  const defOpt = document.createElement("option");
  defOpt.value = ""; defOpt.textContent = "По умолчанию (системное)";
  micSel.appendChild(defOpt);
  for (const d of devices){
    if (d.kind !== "audioinput") continue;
    mi++;
    const o = document.createElement("option");
    o.value = d.deviceId;
    o.textContent = d.label || ("Микрофон " + mi);
    if (d.deviceId && d.deviceId === micId){ o.selected = true; micFound = true; }
    micSel.appendChild(o);
  }
  if (!micFound) micSel.value = "";

  spkSel.innerHTML = "";
  let si = 0, spkFound = false;
  const defSpk = document.createElement("option");
  defSpk.value = ""; defSpk.textContent = "По умолчанию (системное)";
  spkSel.appendChild(defSpk);
  const supportsSink = VC.ctx
    ? typeof VC.ctx.setSinkId === "function"
    : ("setSinkId" in (window.AudioContext || window.webkitAudioContext || {}).prototype);
  for (const d of devices){
    if (d.kind !== "audiooutput") continue;
    si++;
    const o = document.createElement("option");
    o.value = d.deviceId;
    o.textContent = d.label || ("Динамик " + si);
    if (d.deviceId && d.deviceId === spkId){ o.selected = true; spkFound = true; }
    spkSel.appendChild(o);
  }
  if (!spkFound) spkSel.value = "";
  if (!supportsSink){
    spkSel.disabled = true;
    spkSel.title = "Этот браузер не умеет менять устройство вывода " +
      "(нужно Chrome/Edge 110+)";
    spkSel.value = "";
    const note = spkSel.parentElement.querySelector(".opt");
    if (note) note.textContent = "— браузер не поддерживает смену, звук " +
      "пойдёт в системное устройство";
  } else {
    spkSel.disabled = false;
    spkSel.title = "";
  }
}

/* Смена микрофона: сохраняем и (если уже говорим) перехватываем поток
   на живом соединении — собеседники услышат короткую паузу, дисконнекта
   и смены имени в канале не будет. */
async function switchMic(deviceId, silent){
  voiceSetting("wr_mic", deviceId);
  if (!VC.on || !VC.ctx || !VC.capNode){
    if (!silent) voiceWarn("");
    return true;
  }
  try {
    const fresh = await navigator.mediaDevices.getUserMedia(
      micConstraints(deviceId));
    if (VC.srcNode){ try { VC.srcNode.disconnect(); } catch(e){} }
    if (VC.stream) VC.stream.getTracks().forEach(t => t.stop());
    VC.stream = fresh;
    VC.srcNode = VC.ctx.createMediaStreamSource(fresh);
    VC.srcNode.connect(VC.capNode);
    VC.devicesGranted = true;
    return true;
  } catch(e){
    voiceWarn("не удалось переключить микрофон (" + (e.name || e) + ")");
    return false;
  }
}

/* Смена динамика: AudioContext.setSinkId (Chromium 110+). Работает
   на живом звуке без пересборки графа. */
async function switchSpeaker(deviceId){
  voiceSetting("wr_spk", deviceId);
  if (!VC.ctx || typeof VC.ctx.setSinkId !== "function") return;
  try { await VC.ctx.setSinkId(deviceId || ""); }
  catch(e){
    voiceWarn("динамик не переключился (" + (e.name || e) + ")");
    refreshVoiceDevices();
  }
}

/* Громкость собеседников: мастер-гейн, применяется мгновенно. */
function applyOutputVolume(){
  const v = (+voiceSetting("wr_vol") || 100) / 100;
  if ($("volVal")) $("volVal").textContent = Math.round(v * 100) + "%";
  if (VC.masterGain){
    const t = VC.ctx.currentTime;
    try {
      VC.masterGain.gain.setTargetAtTime(v, t, 0.05);  // без щелчка
    } catch(e){ VC.masterGain.gain.value = v; }
  }
}

/* Метр уровня: полоска в вкладке, обновляется из pushSamples. */
function setMicMeter(level){
  const m = $("micMeter");
  if (m) m.style.width = Math.round(Math.min(1, level) * 100) + "%";
}

/* devicechange: воткнули/вытащили наушники — список устройств меняется */
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener){
  navigator.mediaDevices.addEventListener("devicechange", () => {
    refreshVoiceDevices();
  });
}

/* ───────────────── подключение ───────────────── */
/* v3.4.1 FIX: Запускает app-level heartbeat раз в 15 секунд (v3.5.5:
   было 25 — интервал чаще watchdog-порога). Не доверяем WS ping/pong:
   при свернутой вкладке браузер throttling-ит всё, и мост может закрыть
   соединение по своему таймауту.

   v3.5.5 FIX: watchdog полуподвешенных сокетов. Если с моста НЕТ НИКАКОГО
   входа дольше 45с — сокет мёртв (VPN сменился, NAT тихо снёс мэппинг):
   в норме микшер шлёт кадры тишины каждые 20мс, так что 45с тишины = обрыв.
   Раньше такой сокет обнаруживался только по TCP-ретрансмиту (минуты!),
   и человек видел «разорвано» без видимой причины. Сами закрываем мёртвый
   сокет → onclose → мгновенный реконнект. */
function startHeartbeat(){
  stopHeartbeat();
  VC.hbTimer = setInterval(() => {
    if (!VC.ws || VC.ws.readyState !== 1) return;
    if (VC.on && VC.lastInboundAt && Date.now() - VC.lastInboundAt > 45000){
      try { VC.ws.close(4000, "stale"); } catch(e){}
      return;
    }
    try {
      VC.ws.send(JSON.stringify({t:"hb", ts: Date.now()}));
    } catch(e){}
  }, 15000);
}
function stopHeartbeat(){
  if (VC.hbTimer){ clearInterval(VC.hbTimer); VC.hbTimer = null; }
}

/* ───────── v3.6.6 (R4): качество связи — пинг и потери ↑↓ ───────── */
/* Пороги зеркалят voice/config.py (QUALITY_RTT_GOOD_MS=80, BAD=200;
   QUALITY_LOSS_GOOD_PCT=2, BAD=8) — вердикт красит строку статуса. */
const QUALITY_RTT_GOOD_MS = 80, QUALITY_RTT_BAD_MS = 200;
const QUALITY_LOSS_GOOD_PCT = 2, QUALITY_LOSS_BAD_PCT = 8;

function resetQuality(){
  /* Новое TCP-подключение к мосту = новые счётчики микшера и моста:
     снапшоты дельт обнуляем, иначе первые дельты были бы отрицательными
     и потери рисовались бы мусором до второго pong. */
  VC.rttMs = 0; VC.upPct = 0; VC.downPct = 0; VC.oddOut = 0;
  VC.pingAt = {};
  VC.lastIn = null; VC.lastOut = null;
  VC.lastSent = null; VC.lastRecv = null;
}
function startPing(){
  stopPing();
  VC.pingTimer = setInterval(() => {
    if (!VC.ws || VC.ws.readyState !== 1) return;
    const id = ++VC.pingId;
    VC.pingAt[id] = performance.now();
    try { VC.ws.send(JSON.stringify({t:"ping", id:id})); } catch(e){}
    /* Зависшие пинги (pong не пришёл за 15с — сеть мертва) чистим,
       чтобы pingAt не рос бесконечно за часами тишины. */
    const now = performance.now();
    for (const k in VC.pingAt){
      if (now - VC.pingAt[k] > 15000) delete VC.pingAt[k];
    }
  }, 2000);
}
function stopPing(){
  if (VC.pingTimer){ clearInterval(VC.pingTimer); VC.pingTimer = null; }
}
function onPong(j){
  const sentAt = VC.pingAt[j.id];
  if (sentAt === undefined) return;   // чужой/устаревший/зачищённый
  delete VC.pingAt[j.id];
  const rtt = Math.max(0, performance.now() - sentAt);
  VC.rttMs = VC.rttMs <= 0 ? rtt : VC.rttMs * 0.7 + rtt * 0.3;
  const mIn = Math.max(0, j.in | 0), mOut = Math.max(0, j.out | 0);
  VC.oddOut = Math.max(0, j.odd | 0);
  if (VC.lastIn !== null){
    const dIn = mIn - VC.lastIn, dOut = mOut - VC.lastOut;
    const dSent = VC.sentFrames - VC.lastSent;
    const dRecv = VC.recvFrames - VC.lastRecv;
    /* Потери ВВЕРХ: послали dSent 20мс-кадров — микшер принял dIn. */
    if (dSent > 0){
      const up = Math.max(0, Math.min(100,
        (dSent - Math.max(0, dIn)) * 100 / dSent));
      VC.upPct = VC.upPct <= 0 ? up : VC.upPct * 0.7 + up * 0.3;
    }
    /* Потери ВНИЗ: микшер отправил dOut — приняли dRecv. */
    if (dOut > 0){
      const down = Math.max(0, Math.min(100,
        (dOut - Math.max(0, dRecv)) * 100 / dOut));
      VC.downPct = VC.downPct <= 0 ? down : VC.downPct * 0.7 + down * 0.3;
    }
  }
  VC.lastIn = mIn; VC.lastOut = mOut;
  VC.lastSent = VC.sentFrames; VC.lastRecv = VC.recvFrames;
}
function qualityVerdict(){
  if (VC.rttMs <= 0 && VC.lastIn === null) return "measuring";
  if (VC.rttMs <= QUALITY_RTT_GOOD_MS
      && VC.upPct <= QUALITY_LOSS_GOOD_PCT
      && VC.downPct <= QUALITY_LOSS_GOOD_PCT) return "good";
  if (VC.rttMs > QUALITY_RTT_BAD_MS
      || VC.upPct > QUALITY_LOSS_BAD_PCT
      || VC.downPct > QUALITY_LOSS_BAD_PCT) return "bad";
  return "ok";
}

/* v3.5.5 FIX: авто-реконнект, который НЕ замечен глазами.
   Лестница стала быстрой: 0.3с → 0.7с → 1.5с → 3с → 5с → 10с — при
   одиночном чихе сети зазор 0.3-0.5с, услышать его невозможно (раньше
   первая попытка была через 1с, и каждое микроторождение красило
   вкладку тревожной надписью «разорвано»).

   Тревожную надпись показываем ТОЛЬКО если реально не получается
   восстановиться (3-я попытка или связь отсутствует дольше 5с).
   Успех гасит всё (voiceStarted → voiceWarn("")).
   UI состояние (микрофон/ctx/mute) сохраняется — пользователь не видит ничего. */
const RECONNECT_LADDER_MS = [300, 700, 1500, 3000, 5000, 10000];
function scheduleReconnect(){
  if (VC.intentionalDisconnect) return;
  if (VC.reconnectTimer) return; // уже запланирована
  if (VC.reconnectAttempts >= 999) return; // стоп-лист зацикливания
  if (!VC.wsGoneAt) VC.wsGoneAt = Date.now();
  VC.reconnectAttempts++;
  const a = VC.reconnectAttempts;
  const delay = RECONNECT_LADDER_MS[Math.min(a - 1, RECONNECT_LADDER_MS.length - 1)];
  const goneS = Math.round((Date.now() - VC.wsGoneAt) / 1000);
  $("voiceStat").textContent = "связь дрогнула — переподключаюсь (попытка " + a + ")";
  if (a >= 3 || goneS >= 5){
    voiceWarn("связь с голосовым мостом рвётся — переподключаюсь сам (попытка " +
      a + ", без связи " + goneS + " с)…");
  }
  VC.reconnectTimer = setTimeout(() => {
    VC.reconnectTimer = null;
    if (VC.intentionalDisconnect) return;
    reconnectVoice();
  }, delay);
}

/* Внутренняя процедура переподключения: без пересоздания AudioContext
   и без повторного запроса getUserMedia (мы уже имеем поток, ctx, gain).
   Заново только: WebSocket → auth → voiceStarted().

   v3.5.5: на первых двух попытках НЕ ходим за /voice/info — восстанавливаемся
   по кешу wsPath мгновенно (HTTP-круг в момент обрыва только затягивал яму;
   свежая /voice/info — с 3-й попытки). UI (muted/speak/jitterTarget) живёт. */
async function reconnectVoice(){
  if (VC.busy) return;
  if (VC.intentionalDisconnect) return;
  VC.busy = true;
  try {
    if (VC.reconnectAttempts >= 3 || !VC.wsPath) await voiceRefresh();
    if (!VC.wsPath){
      VC.busy = false;
      scheduleReconnect();
      return;
    }
    const ws = new WebSocket("wss://" + location.host + VC.wsPath);
    ws.binaryType = "arraybuffer";
    VC.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({name:S.name, access_key:S.key}));
    ws.onmessage = onVoiceMsg;
    ws.onerror = () => {};
    ws.onclose = () => {
      if (VC.on) {
        scheduleReconnect();
      } else if (!VC.everConnected) {
        voiceWarn("нет связи с мостом — проверь хост/ключ");
        scheduleReconnect();
      } else {
        scheduleReconnect();
      }
    };
    VC.everConnected = false;
    $("voiceStat").textContent = "переподключение…";
  } finally { VC.busy = false; }
}

async function connectVoice(){
  if (VC.busy || VC.on) return;
  VC.busy = true;
  VC.intentionalDisconnect = false;
  VC.reconnectAttempts = 0;
  if (VC.reconnectTimer){ clearTimeout(VC.reconnectTimer); VC.reconnectTimer = null; }
  voiceWarn("");
  try {
    /* v2.0.2: если шла проверка микрофона — останавливаем её, чтобы не
       держать два getUserMedia на одном устройстве */
    micTestStop(false);
    if (location.protocol !== "https:"){
      voiceWarn("микрофон доступен только на https — открой " +
        (VC.httpsPort ? "https://" + location.host + " (тот же порт)"
                      : "https-адрес хоста (тот же порт, что и сейчас)"));
      return;
    }
    /* v1.9.5: всегда свежий /voice/info — устаревший VC.wsPath после сбоя
       мог наврать «мост недоступен» и похоронить кнопку. */
    await voiceRefresh();
    if (!VC.wsPath){ voiceWarn("голосовой мост недоступен"); return; }
    try {
      VC.stream = await navigator.mediaDevices.getUserMedia(
        micConstraints(voiceSetting("wr_mic")));
      VC.devicesGranted = true;
    } catch(e){
      /* v3.6.7: сохранённый deviceId мог протухнуть (устройство вынули,
         USB-гарнитура переподключилась) — раньше это НАВСЕГДА ронило
         подключение с «микрофон недоступен», хотя системный микрофон
         жив. Сначала пробуем ДЕФОЛТНЫЙ микрофон, и только если и он
         недоступен — сдаёмся с объяснением. */
      const saved = voiceSetting("wr_mic");
      if (saved){
        try {
          VC.stream = await navigator.mediaDevices.getUserMedia(
            micConstraints(""));
          VC.devicesGranted = true;
          voiceSetting("wr_mic", "");   // сбрасываем протухший выбор
          if ($("micSel")) $("micSel").value = "";
          voiceWarn("выбранный микрофон недоступен — включён системный " +
            "по умолчанию");
        } catch(e2){
          voiceWarn("микрофон недоступен (" + (e2.name || e2) +
            ") — разреши доступ к микрофону для этой страницы");
          return;
        }
      } else {
        voiceWarn("микрофон недоступен (" + (e.name || e) +
          ") — разреши доступ к микрофону для этой страницы");
        return;
      }
    }
    try { VC.ctx = new AudioContext({sampleRate:48000}); }
    catch(e){ VC.ctx = new AudioContext(); }
    if (VC.ctx.state === "suspended"){ try { await VC.ctx.resume(); } catch(e){} }
    /* v1.9.6: сохранённый динамик + громкость применяются к новому
       контексту сразу */
    const spkId = voiceSetting("wr_spk");
    if (spkId && typeof VC.ctx.setSinkId === "function"){
      try { await VC.ctx.setSinkId(spkId); } catch(e){}
    }
    applyOutputVolume();
    refreshVoiceDevices();   /* метки устройств теперь доступны */
    /* wss на ТОТ ЖЕ адрес/порт, что и страница: сертификат уже принят
       браузером для этого origin — отдельного подтверждения не нужно */
    const ws = new WebSocket("wss://" + location.host + VC.wsPath);
    ws.binaryType = "arraybuffer";
    VC.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({name:S.name, access_key:S.key}));
    ws.onmessage = onVoiceMsg;
    /* ФИКС v1.8.2: если мост не поднялся/отвалился ДО первого ok,
       onclose раньше проходил молча (VC.on ещё false) — кнопка просто
       «отщёлкивалась» назад без объяснения.
       v3.4.1: ВСЁ теперь идёт через scheduleReconnect, кроме intentional. */
    ws.onerror = () => {};
    ws.onclose = () => {
      if (VC.intentionalDisconnect){
        // Юзер сам кликнул «Отключиться»
        if (VC.on) voiceWarn("");
        disconnectVoice();
        return;
      }
      if (VC.on){
        scheduleReconnect();
      } else if (!VC.everConnected){
        voiceWarn("не удалось подключиться — проверь хост/ключ");
        scheduleReconnect();
      } else {
        scheduleReconnect();
      }
    };
    VC.everConnected = false;
    $("voiceStat").textContent = "подключение\u2026";
  } finally { VC.busy = false; }
}
function onVoiceMsg(ev){
  /* v3.5.5: любой вход (control/бинарь/hb-ack) — мост жив. Часы для
     watchdog'а в startHeartbeat: 45с тишины = сокет полумёртв. */
  VC.lastInboundAt = Date.now();
  if (typeof ev.data === "string"){
    let j = null;
    try { j = JSON.parse(ev.data); } catch(e){ return; }
    if (j.error){ voiceWarn("мост: " + j.error); disconnectVoice(); return; }
    if (j.ok){
      VC.everConnected = true;
      /* v3.6.6 (R4): новое подключение (и первое, и реконнект) — качество
         заново: счётчики микшера на новом TCP начинаются с нуля. */
      resetQuality();
      startPing();          /* идемпотентно: сам гасит старый таймер */
      /* v3.5.5: mute синхронизируем СРАЗУ после ok — раньше был таймер
         1.5с после onopen, за который микрофон успевал озвучиться.
         v3.5.6 КРИТИЧНЫЙ ФИКС: то же самое при РЕКОННЕКТЕ. Ветка раньше
         срабатывала только при первом подключении (!VC.on), а при тихом
         восстановлении связи VC.on уже true — и mute НЕ отправлялся
         заново: мост после обрыва поднимает НОВОЕ подключение с
         state.muted=false, и микрофон «включался» на сервере, хотя у
         тебя горит «микрофон выкл» (утечка голоса). Заодно при
         реконнекте сбрасываются счётчики — раньше wsGoneAt оставался
         ненулевым навсегда, статус залипал на «переподключение…», а
         следующее предупреждение рисовало «без связи N с» со старым
         числом. */
      if (VC.muted && VC.ws && VC.ws.readyState === 1){
        try { VC.ws.send(JSON.stringify({t:"mute", on:true})); } catch(e){}
      }
      if (!VC.on){
        voiceStarted();          /* первый вход: строим аудио-граф */
      } else {
        voiceReconnected();      /* реконнект: аудио-граф жив, чиним статус */
      }
      return;
    }
    if (j.t === "pong"){ onPong(j); return; }   // v3.6.6 (R4) качество
    if (j.t === "spk" && j.name){
      VC.speak[j.name] = !!j.on; renderVoicePeople();
    } else if (j.t === "spk_all" && j.states){
      VC.speak = j.states; renderVoicePeople();
      /* v3.6.4: версия приложения хоста — показываем в статусе, чтобы
         «бурундук»/тишина между разными версиями диагностировались
         сразу (аудио-формат менялся между релизами). */
      if (j.v && typeof j.v === "string") VC.hostVersion = j.v;
    } else if (j.t === "roster" && Array.isArray(j.names)){
      /* состав канала изменился (кто-то вошёл/вышел): молчащие
         новички теперь появляются сразу, ушедшие — исчезают.
         Состояния «говорит» у оставшихся сохраняем. */
      const st = {};
      for (const n of j.names) st[n] = !!VC.speak[n];
      VC.speak = st; renderVoicePeople();
    }
    return;
  }
  /* binary: смешанный PCM int16 mono 48k от микшера */
  if (!VC.ctx) return;
  const i16 = new Int16Array(ev.data);
  /* v3.6.6 (R4): считаем принятые 20мс-кадры для честных потерь ↓
     (дельта против счётчика «out» микшера в pong). */
  VC.recvFrames += Math.floor(i16.length / 960);
  for (let off = 0; off + 960 <= i16.length; off += 960){
    const f = floatFrame(i16.subarray(off, off + 960));
    const out = resampleOut(f);
    if (VC.worklet && VC.playPort){
      VC.playPort.postMessage(out.buffer, [out.buffer]);
    } else {
      VC.playQ.push(out);
      const cap = Math.round(600 * VC.ctx.sampleRate / 1000);
      let tot = 0; for (const fr of VC.playQ) tot += fr.length;
      while (tot > cap && VC.playQ.length > 1){
        tot -= VC.playQ[0].length; VC.playQ.shift(); VC.playPos = 0;
      }
    }
  }
}
function floatFrame(i16){
  const f = new Float32Array(960);
  for (let i = 0; i < 960; i++) f[i] = i16[i] / 32768;
  return f;
}
/* Микшер отдаёт 48к; если контекст браузера другой (редкая железка
   44.1к) — ресемпл к его частоте по 20мс-фреймам, без накопления
   ошибки между фреймами. На контексте 48к путь отключён полностью. */
function resampleOut(f){
  const sr = VC.ctx ? VC.ctx.sampleRate : 48000;
  if (sr === 48000) return f;
  const ratio = sr / 48000;
  const n = Math.round(960 * ratio);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++){
    const p = i / ratio;
    const j = Math.floor(p), frac = p - j;
    /* v3.6.7: на краю кадра интерполировали В НОЛЬ (вместо соседнего
       сэмпла) — крошечный провал волны на каждом 20мс-стыке на
       контекстах 44.1к. Теперь край дублирует последний сэмпл. */
    out[i] = f[j] * (1 - frac) + f[Math.min(j + 1, 959)] * frac;
  }
  return out;
}
async function voiceStarted(){
  VC.on = true;
  const ctx = VC.ctx;
  /* v1.9.6: мастер-грейн громкости между плейаутом и destination.
     Создаётся здесь (не в connectVoice) — контекст живёт с сессией. */
  if (!VC.masterGain){
    VC.masterGain = ctx.createGain();
    VC.masterGain.gain.value = (+voiceSetting("wr_vol") || 100) / 100;
    VC.masterGain.connect(ctx.destination);
  }
  /* Инициализация музыкального плеера при подключении к голосу */
  if (typeof initMusicPlayer === "function"){
    initMusicPlayer();
  }
  let ok = false;
  try {
    /* ворклет — отдельный файл (раньше Blob из строки) */
    await ctx.audioWorklet.addModule("/static/voice-worklet.js");
    const src = ctx.createMediaStreamSource(VC.stream);
    const cap = new AudioWorkletNode(ctx, "vc-cap",
      {numberOfInputs:1, numberOfOutputs:1, outputChannelCount:[1]});
    const sink = ctx.createGain(); sink.gain.value = 0;
    src.connect(cap); cap.connect(sink); sink.connect(ctx.destination);
    cap.port.onmessage = (e) => { if (VC.on) pushSamples(e.data); };
    const play = new AudioWorkletNode(ctx, "vc-play",
      {numberOfInputs:0, numberOfOutputs:1, outputChannelCount:[1]});
    play.port.onmessage = (e) => {
      if (e.data && e.data.under){
        VC.lastUnderAt = Date.now();
        VC.unders++;
        /* v3.7.0: рост цели +2 кадра (40мс) за недобор — как у Qt-движка
           (v3.6.4): ОДИН TCP-затык ZeroTier — это 100-300мс ретрансмита,
           рост по +20мс означал 5-15 повторных недоборов, прежде чем
           буфер дорастал до затыка — вся это время голос «прожёвывался».
           Потолок 18 кадров (360мс) — v3.6.4. */
        VC.jitterTarget = Math.min(18, VC.jitterTarget + 2);
        try { play.port.postMessage({targetMs: VC.jitterTarget * 20}); } catch(e2){}
      } else if (e.data && e.data.stat){
        /* v3.7.0: телеметрия плейаута (уровень буфера, недоборы) */
        VC.playLevel = e.data.stat.lvl || 0;
        VC.unders = e.data.stat.und | 0;
      }
    };
    play.connect(VC.masterGain);
    VC.srcNode = src; VC.capNode = cap; VC.outNode = play; VC.playPort = play.port;
    VC.worklet = true; ok = true;
  } catch(e){ ok = false; }
  if (!ok) startLegacyAudio();  // старый браузер — ScriptProcessor
  VC.lastUnderAt = Date.now();  /* не даём сжать буфер сразу после старта */
  VC.startedAt = Date.now();    /* v3.6.8: точка отсчёта сторожа тишины */
  VC.micVoiceSeen = false; VC.micHintShown = false;   /* новая сессия */
  // v3.4.1: сбрасываем reconnect counter (успешно зашли!) и стартуем HB.
  VC.reconnectAttempts = 0;
  if (VC.reconnectTimer){ clearTimeout(VC.reconnectTimer); VC.reconnectTimer = null; }
  /* v3.5.5: связь восстановлена — гасим счётчик «сколько висим без связи»
     и заводим liveness-часы для watchdog'а полуподвешенных сокетов. */
  VC.wsGoneAt = 0;
  VC.lastInboundAt = Date.now();
  startHeartbeat();
  VC.statTimer = setInterval(voiceTick, 1000);
  voiceBtn("btnVoice", "power", "Отключиться");
  voiceBtn("btnVoiceMute", VC.muted ? "mic-off" : "mic", "Микрофон: " + (VC.muted ? "выкл" : "вкл"));
  $("btnVoiceMute").classList.remove("hidden");
  voiceWarn("");  // очищаем предупреждение авто-переподключения
  voiceTick();
  renderVoicePeople();
}
/* v3.5.6: тихий реконнект удался. Аудио-граф (ctx, worklet, микрофон,
   громкость) жив с первого подключения — пересоздавать не нужно, иначе
   был бы слышен щелчок и мигание колец. Чиним только состояние:
   счётчики лестницы, «время без связи», heartbeat и честный статус. */
function voiceReconnected(){
  VC.reconnectAttempts = 0;
  if (VC.reconnectTimer){ clearTimeout(VC.reconnectTimer); VC.reconnectTimer = null; }
  VC.wsGoneAt = 0;
  VC.lastInboundAt = Date.now();
  startHeartbeat();          /* идемпотентно: сам гасит старый таймер */
  voiceWarn("");
  $("voiceStat").textContent = "подключено";
}
function startLegacyAudio(){
  const ctx = VC.ctx;
  if (!VC.masterGain){
    VC.masterGain = ctx.createGain();
    VC.masterGain.gain.value = (+voiceSetting("wr_vol") || 100) / 100;
    VC.masterGain.connect(ctx.destination);
  }
  const src = ctx.createMediaStreamSource(VC.stream);
  const cap = ctx.createScriptProcessor(512, 1, 1);
  const sink = ctx.createGain(); sink.gain.value = 0;
  src.connect(cap); cap.connect(sink); sink.connect(ctx.destination);
  cap.onaudioprocess = (ev) => { if (VC.on) pushSamples(ev.inputBuffer.getChannelData(0)); };
  const out = ctx.createScriptProcessor(512, 1, 1);
  out.onaudioprocess = onPlayback;
  out.connect(VC.masterGain);
  VC.srcNode = src; VC.capNode = cap; VC.outNode = out; VC.playPort = null;
  VC.worklet = false;
}
function voiceTick(){
  if (!VC.on) return;
  const stat = $("voiceStat");
  /* v3.6.8 ПРОВЕРКА 1 — автоплей-политика. Контекст может остаться
     suspended: подключение без клика (автореконнект после пробуждения
     вкладки, возврат из фоновой вкладки по восстановлению сети).
     Плейаут при этом МОЛЧИТ, хотя кадры от собеседника идут, и
     человек видит «подключено» без звука. Пробуем разбудить; не
     удалось — честно говорим, что нужен клик по странице. */
  if (VC.ctx && VC.ctx.state === "suspended"){
    try { VC.ctx.resume(); } catch(e){}
    if (VC.ctx.state === "suspended"){
      stat.textContent = "звук заблокирован браузером — кликни по странице";
      stat.style.color = "var(--warn)";
      return;
    }
  }
  /* стабильно >10с — медленно сжимаем буфер (минимум 80мс: v3.7.0 —
     было 60мс, но TCP-транспорт (WS поверх ZeroTier) сжимает поток
     всплесками, и на 60мс каждый чих уже выжигал буфер) */
  if (Date.now() - VC.lastUnderAt > 10000 && VC.jitterTarget > 4){
    VC.jitterTarget--;
    if (VC.playPort) try { VC.playPort.postMessage({targetMs: VC.jitterTarget * 20}); } catch(e){}
  }
  /* v3.5.5: пока идёт реконнект — НЕ рисуем «подключено»: статусом ведёт
     scheduleReconnect (звука в этот момент реально нет, и раньше строка
     статуса каждую секунду врала «подключено», а потом мигала «разорвано»). */
  if (VC.wsGoneAt || VC.reconnectTimer) return;
  /* v3.6.6 (R4): строка качества — пинг/потери ↑↓ с цветом вердикта
     (те же пороги, что в Qt-клиенте). Пока первый pong не пришёл —
     без цифр, не пугаем «0 мс» в первые секунды. */
  let text = "подключено · " + (VC.ctx ? VC.ctx.sampleRate : "?") + " Гц";
  const verdict = qualityVerdict();
  if (verdict !== "measuring"){
    text += " · пинг " + Math.round(VC.rttMs) + " мс" +
      " · потери ↑" + Math.round(VC.upPct) + "% ↓" + Math.round(VC.downPct) + "%";
  }
  text += " · джиттер " + (VC.jitterTarget * 20) + " мс" +
    (VC.unders > 0 ? " · провалов " + VC.unders : "") +
    (VC.upDrops > 0 ? " · срезано отправлено " + VC.upDrops : "") +
    (VC.hostVersion ? " · хост v" + VC.hostVersion : "") +
    (VC.muted ? " · микрофон выкл" : "");
  if (VC.oddOut > 0){
    text += " · ⚠ твои кадры не принимаются — обнови программу";
  }
  stat.textContent = text;
  /* цвет всей строки по вердикту качества: зелёный/жёлтый/красный
     (палитра скина через CSS-переменные — работает во всех темах). */
  stat.style.color = verdict === "good" ? "var(--ok)"
    : verdict === "bad" ? "var(--err)" : "var(--warn)";
  /* v3.6.8 ПРОВЕРКА 2 — «микрофон молчит». Ровно ОДИН раз за сессию,
     если за 30 секунд подключения метр ни разу не шевельнулся: при
     мёртвом/выбранном-не-том устройстве человек говорит, а собеседник
     не слышит НИЧЕГО — из веба это никак не видно. Формулировка
     спокойная: паузы в разговоре — норма. */
  if (!VC.micHintShown && !VC.muted && Date.now() - VC.startedAt > 30000
      && !VC.micVoiceSeen){
    VC.micHintShown = true;
    voiceWarn("микрофон пока молчал — если ты говорил, а друг не слышит, " +
      "проверь выбор устройства выше (кнопка «Проверить микрофон» " +
      "запишет и проиграет твой голос)");
  }
}
function pushSamples(samples){
  /* своя индикация «говорит» — гистерезис ~250мс, как в Qt-клиенте */
  let acc = 0;
  for (let i = 0; i < samples.length; i++) acc += samples[i] * samples[i];
  const rms = Math.sqrt(acc / samples.length) * 32768;
  if (!VC.muted && rms > 300){
    VC.mySil = 0;
    if (!VC.mySpeak){ VC.mySpeak = true; renderVoicePeople(); }
    /* v3.6.8: живой голос с микрофона виден — сторож тишины отбой */
    VC.micVoiceSeen = true;
    if (VC.micHintShown){ VC.micHintShown = false; voiceWarn(""); }
  } else if (++VC.mySil >= 15 && VC.mySpeak){
    VC.mySpeak = false; renderVoicePeople();
  }
  /* v1.9.6: метр уровня (в масштабе Qt: порог VAD 300, максимум ~1800) */
  setMicMeter(rms / 1800);
  /* контекст не 48к — линейный ресемпл к 48к (обычно браузер сам 48к) */
  let s = samples;
  const sr = VC.ctx ? VC.ctx.sampleRate : 48000;
  if (sr !== 48000){
    const ratio = sr / 48000;
    const n = Math.floor(samples.length / ratio);
    const out = new Array(n);
    let pos = VC.resPos;
    for (let i = 0; i < n; i++){
      const j = Math.floor(pos), frac = pos - j;
      /* v3.6.7: край батча дублирует последний сэмпл вместо
         интерполяции в ноль (провал волны на каждом стыке) */
      out[i] = samples[j] * (1 - frac) +
        samples[Math.min(j + 1, samples.length - 1)] * frac;
      pos += ratio;
    }
    VC.resPos = Math.max(0, pos - samples.length);
    s = out;
  }
  /* режем на батчи 960 сэмплов (20мс) → int16 → в мост */
  for (let i = 0; i < s.length; i++) VC.pend.push(s[i]);
  while (VC.pend.length >= 960){
    const fr = new Int16Array(960);
    for (let i = 0; i < 960; i++){
      const v = Math.round(VC.pend[i] * 32768);
      fr[i] = v < -32768 ? -32768 : (v > 32767 ? 32767 : v);
    }
    VC.pend.splice(0, 960);
    /* v3.7.0 СТОРОЖ АПЛИНКА: если в WS-сокете уже застряло >160мс звука
       (затор сети) — живой кадр СРЕЗАЕМ, а не ставим в очередь.
       Иначе после затора отправляется «стена прошлого»: собеседник
       дослушивает устаревшую речь с растущим отставанием. Реалтайм
       дороже полноты: PLC на той стороне закроет дыру тишины. */
    if (VC.ws && VC.ws.readyState === 1
        && VC.ws.bufferedAmount < VC.UPDROP_LIMIT_BYTES){
      VC.ws.send(fr.buffer);
      VC.sentFrames++;   /* v3.6.6 (R4): для честных потерь ↑ */
    } else {
      VC.upDrops++;
    }
  }
  if (VC.pend.length > 19200) VC.pend.splice(0, VC.pend.length - 960);
}
function onPlayback(ev){
  /* SPA-fallback: плейаут из jitter-очереди с фейдами.
     v3.7.0: тот же набор, что у ворклета — рестарт на половину цели
     (вместо полного) + PLC-достройка хвоста (без дрена/статистики —
     путь только для древних браузеров без AudioWorklet). */
  const out = ev.outputBuffer.getChannelData(0);
  const sr = VC.ctx ? VC.ctx.sampleRate : 48000;
  const pre = Math.max(1, VC.jitterTarget >> 1) * 960 * (sr / 48000);
  let total = 0;
  for (const fr of VC.playQ) total += fr.length;
  let i = 0;
  if (!VC.playStarted && total >= pre){ VC.playStarted = true; VC.playGap = true; }
  if (VC.playStarted){
    while (i < out.length && VC.playQ.length){
      const fr = VC.playQ[0];
      const need = Math.min(out.length - i, fr.length - VC.playPos);
      for (let k = 0; k < need; k++){
        const v = fr[VC.playPos + k];
        out[i + k] = v;
        VC.lastReal = v;   /* хвост для PLC */
      }
      VC.playPos += need; i += need;
      if (VC.playPos >= fr.length){ VC.playQ.shift(); VC.playPos = 0; }
    }
    if (i > 0){ VC.plcRun = 0; }
    if (VC.playGap && i > 0){        /* фейд-ин после паузы */
      const n = Math.min(32, i);
      for (let k = 0; k < n; k++) out[k] *= k / n;
      VC.playGap = false;
    }
  }
  if (i < out.length){
    const budget = Math.max(0, Math.round(60 * sr / 1000) - (VC.plcRun || 0));
    const fill = (VC.playStarted || VC.plcRun > 0)
      ? Math.min(out.length - i, budget) : 0;
    if (i > 0 && fill === 0){          /* фейд-аут хвоста — без щелчка */
      const n = Math.min(32, i);
      for (let k = 0; k < n; k++) out[i - 1 - k] *= k / n;
    }
    if (fill > 0){
      const PLC_MAX = Math.round(60 * sr / 1000);
      for (let k = 0; k < fill; k++){
        const t = (VC.plcRun || 0) + k;
        const env = 1 - t / PLC_MAX;
        out[i + k] = (VC.lastReal || 0) * env;
      }
      VC.plcRun = (VC.plcRun || 0) + fill; i += fill;
    }
    for (let k = i; k < out.length; k++) out[k] = 0;
    VC.playStarted = false; VC.playGap = true;
    VC.lastUnderAt = Date.now();
    VC.unders++;
    /* v3.6.4/v3.7.0: потолок 18 (360мс), рост +2 кадра — как у ворклета */
    VC.jitterTarget = Math.min(18, VC.jitterTarget + 2);
  }
}
function renderVoicePeople(){
  const box = $("voicePeople");
  if (!box) return;
  const names = Object.keys(VC.speak);
  if (!names.length){
    box.innerHTML = VC.on
      ? '<div class="hint" style="padding:6px 2px">Пока никого — друг должен ' +
        "подключиться к голосовому каналу (в приложении или тут, во вкладке " +
        "«Голос»)</div>"
      : "";
    return;
  }
  box.innerHTML = "";
  for (const n of names){
    const on = !!VC.speak[n] || (n === S.name && VC.mySpeak);
    const row = document.createElement("div");
    row.className = "vperson" + (on ? " spk" : "");
    const av = document.createElement("span");
    av.className = "vava";
    av.style.background = (typeof avatarColor === "function")
      ? avatarColor(n) : "#5b8cff";
    av.textContent = (typeof initialsOf === "function")
      ? initialsOf(n) : n.slice(0, 2).toUpperCase();
    const nm = document.createElement("span");
    nm.className = "vname";
    nm.textContent = n + (n === S.name ? " (ты)" : "");
    const mic = document.createElement("span");
    mic.className = "vmic " + (on ? "on" : (n === S.name && VC.muted ? "mut" : "idle"));
    mic.title = on ? "говорит" : (n === S.name && VC.muted ? "микрофон выключен" : "в канале");
    /* v3.2: эмодзи 🎙/🔇/🟢 → монолайн-иконки из спрайта */
    mic.appendChild(frIcon(on ? "mic" : ((n === S.name && VC.muted) ? "mic-off" : "circle")));
    row.appendChild(av); row.appendChild(nm); row.appendChild(mic);
    box.appendChild(row);
  }
}
function disconnectVoice(){
  VC.intentionalDisconnect = true;  // запрещаем авто-реконнект
  stopHeartbeat();
  if (VC.reconnectTimer){
    clearTimeout(VC.reconnectTimer);
    VC.reconnectTimer = null;
  }
  VC.reconnectAttempts = 0;
  VC.on = false;
  if (VC.statTimer){ clearInterval(VC.statTimer); VC.statTimer = null; }
  stopPing();   /* v3.6.6 (R4): пинги качества тоже гасим */
  resetQuality();
  if (VC.srcNode){ try{VC.srcNode.disconnect();}catch(e){} VC.srcNode = null; }
  if (VC.capNode){ try{VC.capNode.disconnect();}catch(e){} VC.capNode = null; }
  if (VC.outNode){ try{VC.outNode.disconnect();}catch(e){} VC.outNode = null; }
  if (VC.masterGain){ try{VC.masterGain.disconnect();}catch(e){} VC.masterGain = null; }
  VC.playPort = null;
  if (VC.stream){ VC.stream.getTracks().forEach(t => t.stop()); VC.stream = null; }
  if (VC.ctx){ try{VC.ctx.close();}catch(e){} VC.ctx = null; }
  if (VC.ws){
    const w = VC.ws; VC.ws = null;
    w.onclose = null; try{w.close(1000, "user disconnect");}catch(e){}
  }
  VC.pend = []; VC.playQ = []; VC.playPos = 0;
  VC.playStarted = false; VC.playGap = true;
  VC.resPos = 0; VC.worklet = false;
  VC.unders = 0; VC.upDrops = 0; VC.playLevel = 0;   /* v3.7.0 */
  VC.plcRun = 0; VC.lastReal = 0;
  /* v3.5.6: джиттер-буфер сбрасываем к стартовым 80мс (было 8=160мс —
     после переподключения канал навсегда начинал с двойной задержки). */
  VC.jitterTarget = 4; VC.speak = {}; VC.mySpeak = false; VC.mySil = 0;
  VC.wsGoneAt = 0; VC.lastInboundAt = 0;   /* v3.5.5: liveness-часы тоже */
  /* v3.6.8: сторож тишины микрофона — на новую сессию заново */
  VC.micVoiceSeen = false; VC.micHintShown = false;
  setMicMeter(0);
  voiceBtn("btnVoice", "voice", "Подключиться");
  $("btnVoiceMute").classList.add("hidden");
  $("voiceStat").textContent = "отключено";
  $("voiceStat").style.color = "";   /* v3.6.6: сброс цвета вердикта */
  renderVoicePeople();
  voiceWarn("");
}
$("btnVoice").onclick = () => { if (VC.on) disconnectVoice(); else connectVoice(); };
$("btnVoiceMute").onclick = () => {
  if (!VC.on || !VC.ws) return;
  VC.muted = !VC.muted;
  voiceBtn("btnVoiceMute", VC.muted ? "mic-off" : "mic", "Микрофон: " + (VC.muted ? "выкл" : "вкл"));
  try { VC.ws.send(JSON.stringify({t:"mute", on:VC.muted})); } catch(e){}
  if (VC.muted && VC.mySpeak){ VC.mySpeak = false; renderVoicePeople(); }
  voiceTick();
};
/* ───────────────── v2.0.2: проверка микрофона (послушать себя) ─────────────────
   Локальная запись 5 секунд БЕЗ сервера: пользователь говорит, потом слушает
   себя и получает вердикт «тишина / норма / перегруз» — та же тройка, что в
   Qt («Проверить микрофон», v1.9.6). Зачем: до подключения к каналу хочется
   понять, что вообще выбран не тот микрофон/динамик, а полоска уровня этого
   не показывает — её видно только ПОСЛЕ подключения.

   Как устроено: getUserMedia → ScriptProcessorNode (для разовой проверки
   достаточно; голосовой канал живёт на AudioWorklet) копит Float32-кадры и
   считает пик → по остановке кадры собираются в WAV 16 бит и отдаются
   <audio> через ObjectURL. Никакие байты не покидают браузер. */
const MICTEST = {
  running: false, stream: null, ctx: null, node: null, sink: null,
  frames: [], total: 0, peak: 0, clipped: 0,
  url: null, timer: null, t0: 0,
};
const MICTEST_SECONDS = 5;
/* Пороги в шкале int16 (как в Qt-метре: VAD-порог 300, максимум ~1800):
   пик ниже 150 — микрофон молчит; выше 26000 (≈80% шкалы) — перегруз. */
const MICTEST_SILENCE = 150;
const MICTEST_OVERLOAD = 26000;

async function micTestToggle(){
  if (MICTEST.running){ micTestStop(true); return; }
  micTestStart();
}

async function micTestStart(){
  voiceWarn("");
  const btn = $("btnMicTest"), stat = $("micTestStat");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    warn("voiceWarn", "браузер не отдаёт микрофон (нужен https или localhost)");
    return;
  }
  try {
    /* Тот же deviceId, что выбран для голоса — проверяем реальное устройство */
    MICTEST.stream = await navigator.mediaDevices.getUserMedia(
      micConstraints(voiceSetting("wr_mic")));
  } catch(e){
    voiceWarn("микрофон недоступен (" + (e.name || e) +
      ") — разреши доступ к микрофону для этой страницы");
    return;
  }
  try { MICTEST.ctx = new AudioContext(); }
  catch(e){ micTestCleanup(); return; }
  if (MICTEST.ctx.state === "suspended"){ try { await MICTEST.ctx.resume(); } catch(e){} }
  MICTEST.frames = []; MICTEST.total = 0; MICTEST.peak = 0; MICTEST.clipped = 0;
  MICTEST.running = true;
  MICTEST.t0 = Date.now();
  const src = MICTEST.ctx.createMediaStreamSource(MICTEST.stream);
  const node = MICTEST.ctx.createScriptProcessor(2048, 1, 1);
  const sink = MICTEST.ctx.createGain(); sink.gain.value = 0; /* без вывода на колонки */
  node.onaudioprocess = (ev) => {
    if (!MICTEST.running) return;
    const data = ev.inputBuffer.getChannelData(0);
    MICTEST.frames.push(new Float32Array(data));
    MICTEST.total += data.length;
    let peak = 0;
    for (let i = 0; i < data.length; i++){
      const a = Math.abs(data[i]);
      if (a > peak) peak = a;
      if (a >= 0.998) MICTEST.clipped++;   /* срезанные вершины = перегруз */
    }
    const i16peak = peak * 32768;
    if (i16peak > MICTEST.peak) MICTEST.peak = i16peak;
    setMicMeter(peak * 3.2);              /* живая полоска уровня */
    const left = Math.max(0, MICTEST_SECONDS - (Date.now() - MICTEST.t0) / 1000);
    if (stat) stat.textContent = "записываю… осталось " + left.toFixed(0) + " с";
  };
  src.connect(node); node.connect(sink); sink.connect(MICTEST.ctx.destination);
  MICTEST.node = node; MICTEST.sink = sink;
  voiceBtn("btnMicTest", "stop", "Остановить");
  if ($("micTestBox")) $("micTestBox").classList.add("hidden");
  MICTEST.timer = setTimeout(() => micTestStop(true), MICTEST_SECONDS * 1000 + 100);
}

function micTestStop(showResult){
  if (!MICTEST.running) return;
  MICTEST.running = false;
  if (MICTEST.timer){ clearTimeout(MICTEST.timer); MICTEST.timer = null; }
  const btn = $("btnMicTest"), stat = $("micTestStat");
  voiceBtn("btnMicTest", "headphones", "Проверить микрофон");
  setMicMeter(0);
  const frames = MICTEST.frames, peak = MICTEST.peak, clipped = MICTEST.clipped;
  const total = MICTEST.total;
  micTestCleanup();
  if (!showResult || !total){
    if (stat) stat.textContent = "Запись 5 секунд прямо в браузере — " +
      "на сервер ничего не уходит. Потом послушай себя.";
    return;
  }
  /* Вердикт: те же три буквы, что у Qt-проверки, пороги — в шкале int16 */
  let verdict;
  if (peak < MICTEST_SILENCE)
    verdict = "тишина — микрофон молчит или выбрано не то устройство. " +
      "Проверь выбор микрофона выше и системные настройки";
  else if (peak > MICTEST_OVERLOAD || clipped > total * 0.003)
    verdict = "перегруз — звук срезается сверху. Отодвинься от микрофона " +
      "или убавь усиление в системных настройках";
  else
    verdict = "норма — тебя слышно. Нажми ▶ и послушай себя";
  if (stat) stat.textContent = "Пик: " + Math.round(peak) +
    " / 32767 · запись готова ниже";
  const box = $("micTestBox"), audio = $("micTestAudio"), vEl = $("micTestVerdict");
  if (!box || !audio) return;
  if (MICTEST.url){ URL.revokeObjectURL(MICTEST.url); MICTEST.url = null; }
  MICTEST.url = URL.createObjectURL(micTestWav(frames));
  audio.src = MICTEST.url;
  if (vEl) vEl.textContent = "Вердикт: " + verdict;
  box.classList.remove("hidden");
}

function micTestCleanup(){
  if (MICTEST.node){ try{MICTEST.node.disconnect();}catch(e){} MICTEST.node = null; }
  if (MICTEST.sink){ try{MICTEST.sink.disconnect();}catch(e){} MICTEST.sink = null; }
  if (MICTEST.stream){ MICTEST.stream.getTracks().forEach(t => t.stop()); MICTEST.stream = null; }
  if (MICTEST.ctx){ try{MICTEST.ctx.close();}catch(e){} MICTEST.ctx = null; }
  MICTEST.frames = []; MICTEST.total = 0;
}

/* Float32-кадры → WAV (PCM 16 бит, mono, sampleRate контекста).
   WAV выбран вместо MediaRecorder (webm/ogg): играет ГДЕ УГОДНО, а его
   длительность мы и так знаем — байтов 44 + total*2. */
function micTestWav(frames){
  let total = 0;
  for (const f of frames) total += f.length;
  const sr = MICTEST.ctx ? MICTEST.ctx.sampleRate : 48000;
  const buf = new ArrayBuffer(44 + total * 2);
  const view = new DataView(buf);
  const wrStr = (off, s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  wrStr(0, "RIFF"); view.setUint32(4, 36 + total * 2, true);
  wrStr(8, "WAVE"); wrStr(12, "fmt ");
  view.setUint32(16, 16, true);          /* размер fmt-чанка */
  view.setUint16(20, 1, true);           /* PCM */
  view.setUint16(22, 1, true);           /* mono */
  view.setUint32(24, sr, true);
  view.setUint32(28, sr * 2, true);      /* байт/сек */
  view.setUint16(32, 2, true);           /* блок */
  view.setUint16(34, 16, true);          /* бит/сэмпл */
  wrStr(36, "data"); view.setUint32(40, total * 2, true);
  let off = 44;
  for (const f of frames){
    for (let i = 0; i < f.length; i++){
      let v = Math.round(f[i] * 32768);
      v = v < -32768 ? -32768 : (v > 32767 ? 32767 : v);
      view.setInt16(off, v, true); off += 2;
    }
  }
  return new Blob([buf], {type: "audio/wav"});
}

$("btnMicTest").onclick = micTestToggle;
window.addEventListener("pagehide", () => { micTestStop(false); });

/* v1.9.6: контролы настроек звука */
$("micSel").onchange = () => switchMic($("micSel").value);
$("spkSel").onchange = () => switchSpeaker($("spkSel").value);
$("volSlider").oninput = () => {
  voiceSetting("wr_vol", $("volSlider").value);
  applyOutputVolume();
};
$("dspCheck").onchange = () => {
  /* живое переподключение микрофона с новыми ограничениями */
  switchMic($("micSel").value, true);
};
window.addEventListener("pagehide", () => { if (VC.on) disconnectVoice(); });
