"use strict";
/* notify.js — ОС-уведомления браузера (v3.8.0).

   ЗАЧЕМ: звук о новом сообщении был (v2.0.2), но пока вкладка спрятана,
   пользователь не знает, КТО написал и что именно. Notification API
   показывает системный попап с именем автора и текстом; клик по
   уведомлению возвращает в окно чата.

   КАК ВКЛЮЧАЕТСЯ:
   - Разрешение (permission) запрашивается ТОЛЬКО по жесту пользователя:
     при включении звука кнопкой-колокольчиком (btnNotify в main.js) —
     если браузер ещё не спрашивал. Никаких попапов «по собственному
     желанию» при загрузке страницы.
   - Тумблер ОС-уведомлений раздельный со звуком и живёт в localStorage
     «wr_osnotify» (по умолчанию ВЫКЛ, включается вместе с разрешением;
     повторное включение звука permission не переспрашивает).

   КОГДА ШЛЁМ: только когда вкладка НЕ активна (document.hidden) — пока
   человек читает чат, попапы не нужны. Общий чат: из addBubble (chat.js,
   рядом с notifyBeep). ЛС: из dmBadgeTick (dm.js) — по каждому
   собеседнику не чаще одного уведомления на новое сообщение.

   Тестовый хук: успешная отправка пишется в window.__frLastNotify —
   E2E проверяет факт уведомления без подглядывания в ОС. */

const OSNOTIFY_KEY = "wr_osnotify";

function osNotifySupported(){
  return typeof window.Notification !== "undefined";
}

function osNotifyEnabled(){
  try {
    return localStorage.getItem(OSNOTIFY_KEY) === "1" &&
      osNotifySupported() &&
      Notification.permission === "granted";
  } catch(e){ return false; }
}

/* Показать уведомление. Возвращает true, если реально показали.
   silent: true — звук уже делает notifyBeep, двойной «динь» не нужен. */
function osNotify(title, body, tag){
  if (!document.hidden || !osNotifyEnabled()) return false;
  try {
    const n = new Notification(String(title || "Friend Relay"), {
      body: String(body || "").slice(0, 160),
      tag: tag || "fr-chat",
      silent: true,
    });
    n.onclick = () => {
      try { window.focus(); n.close(); } catch(e){}
    };
    window.__frLastNotify = {
      title: String(title || ""), body: String(body || ""),
      tag: tag || "fr-chat",
    };
    return true;
  } catch(e){
    return false;
  }
}

/* Вызывается из main.js при включении звука: если браузер ещё не
   спрашивал разрешение — спрашиваем (это единственный попап-запрос).
   Пользователь может отказать — звук продолжит работать, ОС-попапов
   просто не будет. */
async function osNotifyMaybeAsk(){
  if (!osNotifySupported()) return "unsupported";
  try {
    if (Notification.permission === "granted"){
      try { localStorage.setItem(OSNOTIFY_KEY, "1"); } catch(e){}
      return "granted";
    }
    if (Notification.permission === "denied") return "denied";
    const p = await Notification.requestPermission();
    if (p === "granted"){
      try { localStorage.setItem(OSNOTIFY_KEY, "1"); } catch(e){}
      return "granted";
    }
    return p || "default";
  } catch(e){
    return "error";
  }
}
