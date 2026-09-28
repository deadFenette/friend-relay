"use strict";
/* screen.js — демонстрация экрана СО ЗВУКОМ (v3.6.0, вкладка «Экран»).
 *
 * КАК УСТРОЕНО (почему без библиотек):
 *   Захват и кодирование делает сам браузер — getDisplayMedia (экран/
 *   окно/вкладка + галочка «системный звук» в Chrome/Edge на Windows)
 *   и встроенный WebRTC (RTCPeerConnection). Это самый открытый и
 *   бесплатный стек: Chromium (BSD-подобные лицензии) уже в браузере
 *   у друзей, никаких установок и Node-серверов. Готовые библиотеки
 *   (PeerJS, mediasoup, LiveKit) потянули бы Node.js/отдельный сервис —
 *   не наш путь: сервер Friend Relay только РЕЛЕИТ служебный сигналинг
 *   (пара JSON на хендшейк), а картинка и звук идут P2P напрямую от
 *   ведущего к зрителям — как в голосовом чате, но медиа без хоста.
 *
 * РОЛИ:
 *   sharer — у него RTCPeerConnection на КАЖДОГО зрителя (mesh);
 *   viewer — одна RTCPeerConnection, удалённый поток в <video>.
 *
 * ХЕНДШЕЙК (non-trickle ICE: кандидаты едут внутри SDP — меньше ходов,
 * и всё работает через поллинг-сигналинг без держимого сокета):
 *   зритель → {type:"hello"}
 *   ведущий → {type:"offer", sdp}
 *   зритель → {type:"answer", sdp}
 *   любой   → {type:"bye"}  (вежливый выход)
 *   После установления связи зритель POST /screen/watch — счётчик.
 *
 * ЛИВНОСТЬ: ведущий шлёт heartbeat /screen/publish раз в 8с; тишина
 * дольше STREAM_LIVE_S (сервер) — показ сам исчезает из списка.
 *
 * БЕЗОПАСНЫЙ КОНТЕКСТ (v3.6.1): захват (getDisplayMedia) браузеры дают
 * только на https:// или localhost — на http://IP нужен баннер-подсказка
 * с готовой https-ссылкой (см. screenShowInsecure). ЗРИТЕЛЮ захват не
 * нужен: просмотр по http работает (RTCPeerConnection не ограничен).
 *
 * КАЧЕСТВО/СТАБИЛЬНОСТЬ (v3.6.2):
 *   - профиль «максимум» для LAN/ZeroTier (60 кадров/5 Мбит) наряду
 *     с «движение» и «чтение» (SCREEN_MODES);
 *   - адаптив по getStats: потери >4% или RTT >350мс — шаг вниз по
 *     лестнице (меньше битрейт, крупнее пиксели), 20с чисто — шаг вверх
 *     (SCREEN_LADDER, screenAdaptTick); звук не трогаем;
 *   - зритель сам переподключается после обрыва (2 попытки, экран
 *     пишет «переподключаюсь») — не надо жать «Смотреть» заново;
 *   - Firefox/Safari не отдают системный звук — панель предлагает
 *     догрузить «Стерео микшер»/VB-Cable как микрофон (дорожка
 *     дописывается в показ, уже смотрящим — свежий offer); */

var SC = {
  role: "",          // "" | "sharer" | "viewer"
  stream: null,      // MediaStream захвата (у ведущего)
  pcs: {},           // ведущий: имя зрителя -> RTCPeerConnection
  viewPc: null,      // зритель: его единственное соединение
  watching: "",      // зритель: за кем смотрит
  since: 0,          // курсор сигналинг-почты
  streams: [],       // копия /screen/info (кто показывает)
  beatTimer: null,   // heartbeat ведущего
  busy: false,       // защита от двойного клика по кнопкам
  restartAt: 0,      // антидребезг рестартов просмотра
  watchBeat: 0,      // v3.6.3: когда последний раз отправляли watch(on=true)
  // v3.6.2: стабильность
  adapt: {},         // ведущий: зритель -> {step, prev, cleanSince}
  adaptTimer: null,  // периодический getStats-замер
  retries: 0,        // зритель: авто-попытки переподключения
  retryTimer: null,  // таймер авто-rehello
};

const SCREEN_ICE = [
  // STUN нужен: Chrome маскирует локальные адреса mDNS-именами (.local),
  // которые не резолвятся между машинами. srflx-кандидаты от STUN дают
  // рабочие адреса; через ZeroTier это обычно прямой P2P-путь.
  { urls: "stun:stun.l.google.com:19302" },
  { urls: "stun:stun1.l.google.com:19302" },
  { urls: "stun:stun.cloudflare.com:3478" },
];
const SCREEN_BEAT_MS = 8000;    // heartbeat ведущего
const SCREEN_INFO_MS = 4000;    // как часто спрашивать «кто показывает»
const SCREEN_ICE_WAIT_MS = 1500; // максимум ждём сбора ICE-кандидатов
// v3.6.2: профили качества, адаптив и авто-реконнект
const SCREEN_ADAPT_MS = 3000;     // период getStats-замера у ведущего
const SCREEN_RETRY_MS = 2000;     // пауза перед авто-rehello зрителя
const SCREEN_RETRY_MAX = 2;       // авто-попыток, потом «жми Смотреть»
const SCREEN_DISC_GRACE_MS = 5000; // «disconnected» может сам подняться
// v3.6.3: пульс счётчика зрителей — повторяем watch(on=true) раз в 12с,
// иначе вкладка без «bye» вечно висит в «смотрят» у всех остальных.
const SCREEN_WATCH_BEAT_MS = 12000;
/* Лестница деградации: при потерях/высоком RTT ведущий снижает битрейт
   и укрупняет картинку (scaleResolutionDownBy) — кадр идёт дальше,
   просто мягче. Только видео: звук (Opus) на таких сетях не страдает. */
const SCREEN_LADDER = [
  { k: 1.0,  scale: 1 },          // шаг 0 — как задумано
  { k: 0.6,  scale: 1.5 },        // шаг 1 — сеть поплохела
  { k: 0.35, scale: 2 },          // шаг 2 — дожимаем стабильность
];
/* Профили качества: к прежним двум добавлен «максимум» для LAN/ZeroTier. */
const SCREEN_MODES = {
  motion: { fps: 30, bitrate: 2500000, hint: "motion", title: "движение" },
  detail: { fps: 15, bitrate: 1500000, hint: "detail", title: "чтение" },
  max:    { fps: 60, bitrate: 5000000, hint: "motion", title: "максимум" },
};
function screenMode(){
  const el = $("screenQuality");
  return SCREEN_MODES[el && el.value] || SCREEN_MODES.motion;
}

function screenWarn(msg){ $("screenWarn").textContent = msg || ""; }
function screenStat(msg){ $("screenStat").textContent = msg || ""; }

/* ───────────────────── https-подсказка (v3.6.1) ─────────────────────
 * «Не работает в Edge/Brave» почти всегда означает: клиент открыт по
 * http://IP. На таком адресе ЛЮБОЙ браузер (Chromium, Firefox, Safari)
 * прячет navigator.mediaDevices — захват экрана/микрофона разрешён
 * только в безопасном контексте (https или localhost). Это не «нет
 * поддержки браузером». Сервер Friend Relay слушает HTTPS на ТОМ ЖЕ
 * порту (tls_mux), поэтому честный фикс — показать готовую ссылку
 * https://тот-же-адрес:тот-же-порт и попросить принять сертификат.
 * Смотреть ЧУЖОЙ показ можно и по http: RTCPeerConnection не запрещён
 * на небезопасном контексте — захват нужен только ведущему. */
function screenIsInsecure(){
  try { if (window.isSecureContext) return false; } catch(e){}
  return true;
}
function screenHttpsHref(){
  try {
    if (location.protocol === "https:") return "";
    const host = location.hostname || "";
    if (!host || host === "localhost" || host === "127.0.0.1") return "";
    return "https://" + host + (location.port ? ":" + location.port : "")
      + location.pathname;
  } catch(e){ return ""; }
}
/* Постоянный баннер сверху панели (обоих скинов — панель одна). */
function screenShowInsecure(){
  const panel = $("tab-screen");
  if (!panel) return;
  let b = $("screenInsecure");
  if (!b){
    b = document.createElement("div");
    b.id = "screenInsecure";
    b.className = "warn";
    panel.insertBefore(b, panel.firstChild);
  }
  b.textContent = "";
  const p1 = document.createElement("div");
  p1.textContent = "Страница открыта по http://" + (location.hostname || "…")
    + " — на таком адресе браузер (любой: Edge, Brave, Chrome, Opera, "
    + "Яндекс) отключает захват экрана и микрофона. Это требование "
    + "безопасности, а не отсутствие поддержки.";
  b.appendChild(p1);
  const p2 = document.createElement("div");
  const href = screenHttpsHref();
  if (href){
    p2.textContent = "Ведущему: открой клиент по тому же адресу, но по https — ";
    const link = document.createElement("a");
    link.href = href; link.textContent = href;
    p2.appendChild(link);
    const p3 = document.createElement("div");
    p3.textContent = "…и прими самоподписанный сертификат сервера. Смотреть чужой показ можно и отсюда, по http.";
    b.appendChild(p2); b.appendChild(p3);
  } else {
    p2.textContent = "Ведущему: открой клиент по https://<адрес-хоста>:8420 и прими сертификат — или прямо на машине-хосте по http://localhost:8420 (localhost браузеры считают безопасным). Смотреть чужой показ можно и по http.";
    b.appendChild(p2);
  }
}
function screenHideInsecure(){
  const b = $("screenInsecure");
  if (b) b.remove();
}

/* ─────────── костыль звука для Firefox/Safari (v3.6.2) ───────────
 * Системный звук экрана отдают только Chromium на Windows. Firefox и
 * Safari молча вернут картинку без аудиодорожки. Обход: Windows умеет
 * выводить ВЕСЬ звук системы в устройство ЗАПИСИ — «Стерео микшер»
 * (mmsys.cpl, включается галочкой, обычно есть у Realtek) или
 * виртуальный кабель VB-Audio/CABLE. Такой «микрофон» догружаем
 * getUserMedia-дорожкой прямо в идущий показ; тем, кто уже смотрит,
 * уходит свежий offer (пересборка pc без нажатия «Смотреть»). */
function screenOfferAudioFix(){
  const panel = $("tab-screen");
  if (!panel || !SC.stream || SC.stream.getAudioTracks().length) return;
  let box = $("screenAudioFix");
  if (!box){
    box = document.createElement("div");
    box.id = "screenAudioFix";
    box.className = "warn";
    const host = $("screenHost");
    if (host) panel.insertBefore(box, host); else panel.appendChild(box);
  }
  box.textContent = "";
  const p = document.createElement("div");
  p.textContent = "Системного звука нет (этот браузер его не отдаёт — "
    + "умеют Chrome/Edge на Windows). Включи в Windows «Стерео микшер» "
    + "(mmsys.cpl → Запись) или поставь VB-Cable — и поймай его как "
    + "микрофон:";
  const btn = document.createElement("button");
  btn.className = "sec";
  btn.textContent = " Догрузить звук (микрофон/кабель)";
  btn.onclick = async () => {
    try {
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false,   // кабель — уже чистый микс:
                 noiseSuppression: false,   // любая обработка мнёт музыку
                 autoGainControl: false },
      });
      const at = mic.getAudioTracks()[0];
      if (!at) throw new Error("аудиодорожка пуста");
      SC.stream.addTrack(at);
      at.addEventListener("ended", () => {
        if (SC.role === "sharer")
          screenWarn("Звук-кабель пропал — показ продолжается без звука.");
      });
      box.textContent = "";
      const ok = document.createElement("div");
      ok.textContent = "Звук пошёл через микрофон-кабель. Тем, кто уже "
        + "смотрит, уйдёт свежий offer — картинка на секунду мигнёт.";
      box.appendChild(ok);
      for (const name of Object.keys(SC.pcs)) screenSharerHello(name);
    } catch(e){
      screenWarn("Микрофон/кабель не поймался: " + (e && e.message || e)
        + ". Проверь: mmsys.cpl → вкладка «Запись» → «Стерео микшер» "
        + "(правый клик → включить) или устройство CABLE Output.");
    }
  };
  box.appendChild(p); box.appendChild(btn);
}
function screenHideAudioFix(){
  const box = $("screenAudioFix");
  if (box) box.remove();
}

/* Публичный хук для скинов (nochnik.js renderNow): карточка «Экран». */
function screenState(){
  let tag = "", live = false;
  if (SC.role === "sharer"){
    const n = Object.keys(SC.pcs).length;
    live = true; tag = "показ идёт";
    if (n) tag += " · " + n;
  } else if (SC.role === "viewer"){
    live = true; tag = "смотрю: " + SC.watching;
  } else {
    const other = (SC.streams || []).filter(s => s.from !== S.name);
    if (other.length){ live = true; tag = other[0].from; }
  }
  return { live, tag };
}

/* ───────────────────────── сигналинг ───────────────────────── */
async function screenSend(to, data){
  try {
    const {json} = await apiPost("/screen/signal", {to, data});
    if (!json || !json.ok) screenWarn(json && json.error || "сигнал не прошёл");
  } catch(e){ /* сеть мигнула — poll доложит остальное */ }
}

/* Ждём завершения сбора ICE (non-trickle): кандидаты уже в SDP. */
function screenWaitIce(pc){
  return new Promise(res => {
    if (pc.iceGatheringState === "complete") return res();
    const done = () => { clearTimeout(t); res(); };
    const t = setTimeout(done, SCREEN_ICE_WAIT_MS); // кандидатов может не быть
    pc.addEventListener("icegatheringstatechange", () => {
      if (pc.iceGatheringState === "complete") done();
    });
  });
}

/* ───────────────────────── ВЕДУЩИЙ ───────────────────────── */
async function startScreenShare(){
  if (SC.busy || SC.role) return;
  SC.busy = true; screenWarn("");
  try {
    // v3.6.1: честный диагноз вместо «браузер не умеет».
    // 1) http://IP — небезопасный контекст, mediaDevices скрыт во ВСЕХ
    //    браузерах; показываем баннер с готовой https-ссылкой.
    if (screenIsInsecure()){
      screenShowInsecure();
      screenWarn("Захват не стартовал: открыт http:// — смотри подсказку выше (нужен https:// или localhost).");
      return;
    }
    // 2) Сам браузер без getDisplayMedia (очень старый / экзотика).
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia){
      screenWarn("Этот браузер без захвата экрана (getDisplayMedia). Обнови его или возьми Edge/Brave/Chrome/Firefox/Safari — все поддерживают.");
      return;
    }
    const mode = screenMode();
    let stream;
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: mode.fps },
                 width: { max: 1920 } },
        audio: true,                      // системный звук — галочка в диалоге Chrome
        systemAudio: "include",           // Chrome 137+: подсказка диалогу
        selfBrowserSurface: "exclude",    // не предлагаем шарить саму эту вкладку
        surfaceSwitching: "include",      // зритель видит смену экрана/окна
      });
    } catch(e){
      screenWarn(e && e.name === "NotAllowedError"
        ? "Захват отменён — показ не начат."
        : "Захват не удался: " + (e && e.message || e));
      return;
    }
    const vt = stream.getVideoTracks()[0];
    if (vt){
      try { vt.contentHint = mode.hint; } catch(e){}
      // пользователь нажал «Прекратить показ» в панели браузера
      vt.addEventListener("ended", () => { if (SC.role === "sharer") stopScreenShare(); });
    }
    SC.stream = stream; SC.role = "sharer"; SC.pcs = {}; SC.since = 0;
    SC.adapt = {}; SC.retries = 0;
    screenHideInsecure();               // добрались до захвата — баннер не нужен
    $("screenPreview").srcObject = stream;
    $("screenHost").classList.remove("hidden");
    $("btnScreenShare").classList.add("hidden");
    $("btnScreenStop").classList.remove("hidden");
    screenStat("показ идёт");
    // Firefox/Safari вернут видео без аудио — честно предупреждаем и
    // предлагаем костыль (стерео-микшер/VB-Cable), v3.6.2
    if (!stream.getAudioTracks().length)
      screenWarn("Звука системы этот браузер не отдаёт (умеют Chrome/Edge на Windows — там поставь галочку «Также передавать звук системы»). Ниже есть обходной путь.");
    screenOfferAudioFix();
    // голосовой чат активен? его звук уйдёт в показ — предупреждаем сразу
    try {
      if (typeof VC !== "undefined" && VC.on)
        screenWarn("Голосовой чат активен — его звук тоже попадёт в показ (захват системы). Собеседникам-зрителям лучше наушники, иначе услышат голос дважды.");
    } catch(e){}
    try {
      await apiPost("/screen/publish", {on: true, title: mode.title});
    } catch(e){}
    SC.beatTimer = setInterval(() => {
      if (SC.role !== "sharer") return;
      apiPost("/screen/publish", {on: true}).catch(()=>{});
    }, SCREEN_BEAT_MS);
    // v3.6.2: адаптив качества по getStats
    clearInterval(SC.adaptTimer);
    SC.adaptTimer = setInterval(() => { screenAdaptTick().catch(()=>{}); },
                               SCREEN_ADAPT_MS);
  } finally { SC.busy = false; }
}

async function stopScreenShare(){
  if (SC.role !== "sharer") return;
  if (SC.beatTimer){ clearInterval(SC.beatTimer); SC.beatTimer = null; }
  if (SC.adaptTimer){ clearInterval(SC.adaptTimer); SC.adaptTimer = null; }
  SC.adapt = {}; screenHideAudioFix();
  for (const name of Object.keys(SC.pcs)) screenCloseSharerPc(name);
  SC.pcs = {};
  if (SC.stream){ for (const t of SC.stream.getTracks()) try { t.stop(); } catch(e){} }
  SC.stream = null; SC.role = "";
  $("screenPreview").srcObject = null;
  $("screenHost").classList.add("hidden");
  $("btnScreenShare").classList.remove("hidden");
  $("btnScreenStop").classList.add("hidden");
  screenStat(""); screenWarn("");
  try { await apiPost("/screen/publish", {on: false}); } catch(e){}
}

/* v3.6.2: применяем ступень лестницы к видео-сендеру зрителя. */
function screenApplyStep(viewer){
  const a = SC.adapt[viewer], pc = SC.pcs[viewer];
  if (!a || !pc) return;
  const sender = pc.getSenders().find(s => s.track && s.track.kind === "video");
  if (!sender || !sender.setParameters) return;
  const m = screenMode(), st = SCREEN_LADDER[a.step] || SCREEN_LADDER[0];
  sender.getParameters().then(p => {
    if (!p.encodings || !p.encodings.length) p.encodings = [{}];
    p.encodings[0].maxBitrate = Math.round(m.bitrate * st.k);
    try { p.encodings[0].scaleResolutionDownBy = st.scale; } catch(e){}
    return sender.setParameters(p);
  }).catch(()=>{});
}
/* v3.6.2: каждые 3с меряем потери/RTT по каждому зрителю и ходим по
 * лестнице: плохо — вниз (кадр остаётся, качество мягче), 20с чисто —
 * осторожно вверх. Метрики: outbound-rtp (пакеты), remote-inbound-rtp
 * (fractionLost), candidate-pair (RTT); чего-то может не быть — не страшно. */
async function screenAdaptTick(){
  if (SC.role !== "sharer") return;
  for (const viewer of Object.keys(SC.pcs)){
    const pc = SC.pcs[viewer];
    let stats;
    try { stats = await pc.getStats(); } catch(e){ continue; }
    let out = null, rin = null, pair = null;
    stats.forEach(r => {
      if (r.type === "outbound-rtp" && r.kind === "video") out = r;
      else if (r.type === "remote-inbound-rtp" && r.kind === "video") rin = r;
      else if (r.type === "candidate-pair" && r.state === "succeeded"
               && (r.nominated || r.selected || !pair)) pair = r;
    });
    if (!out) continue;
    let a = SC.adapt[viewer];
    if (!a) a = SC.adapt[viewer] = { step: 0, prev: null, cleanSince: 0 };
    const now = Date.now();
    const sent = out.packetsSent || 0, lost = out.packetsLost || 0;
    const rtt = (pair && pair.currentRoundTripTime)
             || (rin && rin.roundTripTime) || null;
    const fLost = (rin && typeof rin.fractionLost === "number")
      ? rin.fractionLost : null;
    const prev = a.prev; a.prev = { sent, lost };
    if (!prev) continue;                        // первый замер — база
    const dSent = sent - prev.sent, dLost = lost - prev.lost;
    const lossRate = (dSent + dLost) > 0 ? dLost / (dSent + dLost) : 0;
    const bad = lossRate > 0.04 || (fLost !== null && fLost > 0.04)
                || (rtt !== null && rtt > 0.35);
    const good = lossRate < 0.005 && (fLost === null || fLost < 0.005)
                 && (rtt === null || rtt < 0.15);
    if (bad && a.step < SCREEN_LADDER.length - 1){
      a.step++; a.cleanSince = 0;
      screenApplyStep(viewer);
    } else if (good){
      if (!a.cleanSince) a.cleanSince = now;
      if (a.step > 0 && now - a.cleanSince > 20000){
        a.step--; a.cleanSince = now;          // аккуратно поднимаем
        screenApplyStep(viewer);
      }
    } else a.cleanSince = 0;
  }
}

/* Ведущий: новое соединение под зрителя. */
async function screenSharerHello(viewer){
  if (SC.role !== "sharer" || !SC.stream) return;
  screenCloseSharerPc(viewer);          // рестарт от того же зрителя
  const pc = new RTCPeerConnection({ iceServers: SCREEN_ICE });
  SC.pcs[viewer] = pc;
  if (!SC.adapt[viewer]) SC.adapt[viewer] = { step: 0, prev: null, cleanSince: 0 };
  for (const track of SC.stream.getTracks()) pc.addTrack(track, SC.stream);
  // капаем битрейт/частоту под профиль и текущую ступень лестницы
  // (Chrome; остальным — как выйдет)
  try {
    const sender = pc.getSenders().find(s => s.track && s.track.kind === "video");
    const m = screenMode();
    const st = SCREEN_LADDER[SC.adapt[viewer].step] || SCREEN_LADDER[0];
    if (sender && sender.setParameters){
      const p = sender.getParameters();
      if (!p.encodings || !p.encodings.length) p.encodings = [{}];
      p.encodings[0].maxBitrate = Math.round(m.bitrate * st.k);
      p.encodings[0].maxFramerate = m.fps;
      try { p.encodings[0].scaleResolutionDownBy = st.scale; } catch(e){}
      await sender.setParameters(p);
    }
  } catch(e){}
  pc.onconnectionstatechange = () => {
    const st = pc.connectionState;
    if (st === "failed" || st === "closed"){
      if (SC.pcs[viewer] === pc) screenCloseSharerPc(viewer);
    } else if (st === "disconnected"){
      // «disconnected» часто сам поднимается (моргнула сеть) — даём
      // 5с на восстановление, только потом рвём (новый hello соберёт заново)
      setTimeout(() => {
        if (SC.pcs[viewer] === pc
            && (pc.connectionState === "disconnected"
                || pc.connectionState === "failed"))
          screenCloseSharerPc(viewer);
      }, SCREEN_DISC_GRACE_MS);
    }
  };
  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await screenWaitIce(pc);
    await screenSend(viewer, {type: "offer", sdp: pc.localDescription.toJSON()});
    screenStat("показ идёт · соединяюсь с " + viewer);
  } catch(e){
    screenCloseSharerPc(viewer);
  }
}

function screenCloseSharerPc(viewer){
  const pc = SC.pcs[viewer];
  if (!pc) return;
  delete SC.pcs[viewer];
  if (SC.adapt) delete SC.adapt[viewer];   // метрики ступени — вместе с pc
  try { pc.close(); } catch(e){}
  const n = Object.keys(SC.pcs).length;
  screenStat(SC.role === "sharer" ? ("показ идёт" + (n ? " · смотрят: " + n : "")) : "");
}

/* ───────────────────────── ЗРИТЕЛЬ ───────────────────────── */
/* v3.6.2: связь моргнула — сами повторяем hello, не заставляя жать
 * «Смотреть». Ограниченно (SCREEN_RETRY_MAX), с антидребезгом через
 * SC.restartAt — как у рестартов просмотра. */
function screenViewerRetry(reason){
  if (SC.role !== "viewer" || !SC.watching) return;
  if (Date.now() < SC.restartAt) return;
  SC.retries = (SC.retries || 0) + 1;
  const idle = $("screenViewIdle");
  if (SC.retries > SCREEN_RETRY_MAX){
    if (idle){ idle.classList.remove("hidden");
      idle.textContent = "связь не держится — нажми «Смотреть» заново"; }
    apiPost("/screen/watch", {to: SC.watching, on: false}).catch(()=>{});
    return;
  }
  SC.restartAt = Date.now() + SCREEN_RETRY_MS + 1500;
  if (idle){ idle.classList.remove("hidden");
    idle.textContent = reason + " — переподключаюсь ("
      + SC.retries + "/" + SCREEN_RETRY_MAX + ")…"; }
  if (SC.viewPc){ try { SC.viewPc.close(); } catch(e){} SC.viewPc = null; }
  clearTimeout(SC.retryTimer);
  SC.retryTimer = setTimeout(() => {
    if (SC.role === "viewer" && SC.watching)
      screenSend(SC.watching, {type: "hello"});
  }, SCREEN_RETRY_MS);
}

async function startScreenWatch(sharer){
  if (SC.busy || SC.role) return;
  SC.busy = true;
  try {
    if (SC.role === "sharer") return;   // сначала закончи свой показ
    stopScreenWatchQuiet();             // на всякий случай прежний просмотр
    SC.role = "viewer"; SC.watching = sharer; SC.since = 0;
    SC.retries = 0; clearTimeout(SC.retryTimer);   // новая попытка — чистый счёт
    $("screenView").classList.remove("hidden");
    $("screenViewIdle").classList.remove("hidden");
    $("screenViewIdle").textContent = "соединяюсь с " + sharer + "…";
    $("screenViewWho").textContent = "показ: " + sharer;
    screenStat("смотрю: " + sharer);
    await screenSend(sharer, {type: "hello"});
  } finally { SC.busy = false; }
}

function stopScreenWatchQuiet(){
  if (SC.viewPc){ try { SC.viewPc.close(); } catch(e){} SC.viewPc = null; }
  const v = $("screenVideo");
  if (v){ v.srcObject = null; }
  $("screenView").classList.add("hidden");
  $("screenViewIdle").classList.add("hidden");
  $("screenViewWho").textContent = "";
}

async function stopScreenWatch(notify){
  if (SC.role !== "viewer") return;
  const sharer = SC.watching;
  clearTimeout(SC.retryTimer); SC.retries = 0;
  stopScreenWatchQuiet();
  SC.role = ""; SC.watching = ""; screenStat("");
  if (sharer){
    try { apiPost("/screen/watch", {to: sharer, on: false}).catch(()=>{}); } catch(e){}
    if (notify) await screenSend(sharer, {type: "bye"});
  }
}

function screenViewerAnswer(frm, sdp){
  // offer должен быть от того, за кем смотрим
  if (frm !== SC.watching || SC.role !== "viewer") return;
  try {
    // v3.6.3: закрываем прежнее соединение ПЕРЕД созданием нового.
    // Повторный offer (рестарт показа/гонка hello) раньше оставлял
    // старый pc жить: дорожки, ICE и getStats — всё вхолостую, у
    // экрана же показывался только новый поток.
    if (SC.viewPc){ try { SC.viewPc.close(); } catch(e){} SC.viewPc = null; }
    const pc = new RTCPeerConnection({ iceServers: SCREEN_ICE });
    SC.viewPc = pc;
    const idle = $("screenViewIdle");
    pc.ontrack = (ev) => {
      const v = $("screenVideo");
      if (v.srcObject !== ev.streams[0]) v.srcObject = ev.streams[0];
      v.play().catch(() => {          // автоплей-политика: гасим и просим клик
        v.muted = true;
        v.play().catch(()=>{});
      });
      v.volume = ($("screenVol").value || 100) / 100;
      v.onclick = () => { v.play().catch(()=>{}); };
      if (idle) idle.classList.add("hidden");
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "connected"){
        SC.retries = 0; clearTimeout(SC.retryTimer);   // доехали — счётчик в ноль
        if (idle) idle.classList.add("hidden");
        screenStat("смотрю: " + SC.watching);
        SC.watchBeat = Date.now();   // подключились — пульс уже отправлен
        apiPost("/screen/watch", {to: SC.watching, on: true}).catch(()=>{});
      } else if (pc.connectionState === "failed"){
        screenViewerRetry("связь не установилась");
      }
    };
    (async () => {
      try {
        await pc.setRemoteDescription(new RTCSessionDescription(sdp));
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        await screenWaitIce(pc);
        await screenSend(frm, {type: "answer", sdp: pc.localDescription.toJSON()});
      } catch(e){
        screenViewerRetry("хендшейк не удался");
      }
    })();
  } catch(e){}
}

/* ───────────────────────── общий тик (из pollTick) ───────────────────────── */
async function screenTick(){
  if (typeof S === "undefined" || !S.name) return;
  const now = Date.now();
  // 0) v3.6.3: пульс счётчика зрителей. watch(on=true) — не только
  //    «я подключился», но и «я ещё здесь»: вкладка, умершая без «bye»,
  //    перестаёт пульсировать и через WATCHER_STALE_S исчезает из
  //    «смотрят» у всех остальных. Медиа не трогаем (идёт P2P).
  if (SC.role === "viewer" && SC.watching
      && now - (SC.watchBeat || 0) > SCREEN_WATCH_BEAT_MS){
    SC.watchBeat = now;
    apiPost("/screen/watch", {to: SC.watching, on: true}).catch(()=>{});
  }
  // 1) почта сигналинга — только пока есть роль (иначе зачем)
  if (SC.role){
    try {
      const {json} = await apiGet("/screen/poll?since=" + SC.since);
      if (json && json.ok){
        SC.since = json.since || SC.since;
        for (const m of (json.signals || [])) screenHandleSignal(m.frm, m.data);
      }
    } catch(e){}
  }
  // 2) реестр показов — всем и всегда (лёгкий GET)
  if (now - (S.screenAt || 0) < SCREEN_INFO_MS) { screenRenderStreams(); return; }
  S.screenAt = now;
  try {
    const {json} = await apiGet("/screen/info");
    if (json && json.ok){ SC.streams = json.streams || []; screenRenderStreams(); }
  } catch(e){}
}

function screenHandleSignal(frm, data){
  if (!data || typeof data !== "object" || frm === S.name) return;
  if (SC.role === "sharer"){
    if (data.type === "hello") screenSharerHello(frm);
    else if (data.type === "answer" && SC.pcs[frm] && data.sdp)
      SC.pcs[frm].setRemoteDescription(new RTCSessionDescription(data.sdp)).catch(()=>{});
    else if (data.type === "bye") screenCloseSharerPc(frm);
  } else if (SC.role === "viewer"){
    if (data.type === "offer" && data.sdp) screenViewerAnswer(frm, data.sdp);
    else if (data.type === "bye" && frm === SC.watching){
      screenWarn(frm + " закончил показ.");
      stopScreenWatch(false);
    }
  }
}

/* ───────────────────────── список «Сейчас показывают» ───────────────────────── */
function screenRenderStreams(){
  const box = $("screenStreams");
  if (!box) return;
  const others = (SC.streams || []).filter(s => s.from !== S.name);
  // (v3.6.0) ведущий умер «вкладкой» (без bye): показ исчез из реестра
  // (ливность/heartbeat сервера) — зритель выходит сам, не висит вечно.
  if (SC.role === "viewer" && SC.watching
      && !SC.streams.some(s => s.from === SC.watching)){
    screenWarn(SC.watching + " закончил показ (связь оборвалась).");
    stopScreenWatch(false);
  }
  // сигнатура, чтобы не перерисовывать DOM каждые 4с (моргание/фокус)
  const sig = others.map(s => s.from + "|" + (s.viewers || []).length).join(";");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.textContent = "";
  if (!others.length){
    const p = document.createElement("div");
    p.className = "hint";
    p.textContent = "— сейчас никто не показывает. Стань ведущим: кнопка выше —";
    box.appendChild(p);
    return;
  }
  for (const s of others){
    const row = document.createElement("div");
    row.className = "screenrow";
    const dot = document.createElement("span");
    dot.className = "spk-dot";
    const name = document.createElement("span");
    name.className = "screenrow-name";
    name.textContent = s.from;
    const sub = document.createElement("span");
    sub.className = "screenrow-sub hint";
    sub.style.flex = "1";
    sub.textContent = (s.viewers && s.viewers.length)
      ? "смотрят: " + s.viewers.length : "ждёт зрителей";
    const btn = document.createElement("button");
    btn.className = SC.role === "viewer" && SC.watching === s.from ? "" : "sec";
    if (SC.role === "sharer") btn.disabled = true;
    btn.appendChild(frIcon(SC.role === "viewer" && SC.watching === s.from ? "eye" : "screen"));
    btn.appendChild(document.createTextNode(
      SC.role === "viewer" && SC.watching === s.from ? " Смотрю" : " Смотреть"));
    btn.onclick = () => {
      if (SC.role === "viewer" && SC.watching === s.from) return;
      if (SC.role === "sharer"){ screenWarn("Сначала закончи свой показ."); return; }
      startScreenWatch(s.from);
    };
    row.appendChild(dot); row.appendChild(name); row.appendChild(sub); row.appendChild(btn);
    box.appendChild(row);
  }
}

/* ───────────────────────── инициализация ───────────────────────── */
(function screenInit(){
  document.addEventListener("DOMContentLoaded", () => {
    const bShare = $("btnScreenShare");
    const bStop = $("btnScreenStop");
    const bWStop = $("btnScreenWatchStop");
    if (bShare) bShare.onclick = startScreenShare;
    if (bStop) bStop.onclick = stopScreenShare;
    if (bWStop) bWStop.onclick = () => { screenWarn(""); stopScreenWatch(true); };
    // v3.6.1: открыто по http://IP — объясняем сразу, а не после клика.
    if (screenIsInsecure()) screenShowInsecure();
    const vol = $("screenVol");
    if (vol) vol.oninput = () => {
      const v = $("screenVideo");
      if (v) v.volume = (vol.value || 100) / 100;
    };
    const bFull = $("btnScreenFull");
    if (bFull) bFull.onclick = () => {
      const fr = document.querySelector("#screenView .screenframe");
      if (!fr) return;
      if (document.fullscreenElement){ document.exitFullscreen().catch(()=>{}); }
      else if (fr.requestFullscreen) fr.requestFullscreen().catch(()=>{});
    };
    const q = $("screenQuality");
    if (q) q.onchange = () => {          // живая смена contentHint у трека
      if (SC.role === "sharer" && SC.stream){
        const vt = SC.stream.getVideoTracks()[0];
        if (vt) try { vt.contentHint = screenMode().hint; } catch(e){}
      }
    };
    /* Скин «Ночник»/«Крем-брюле» ПЕРЕНОСИТ панель в свою карточку —
       <video> в некоторых браузерах встает на паузу при переносе.
       Дешёвая страховка: после смены data-skin мягко пинаем play(). */
    try {
      new MutationObserver(() => {
        setTimeout(() => {
          const v = $("screenVideo");
          if (v && v.srcObject) v.play().catch(()=>{});
          const p = $("screenPreview");
          if (p && p.srcObject) p.play().catch(()=>{});
        }, 350);
      }).observe(document.documentElement, {attributes: true, attributeFilter: ["data-skin"]});
    } catch(e){}
  });
})();
