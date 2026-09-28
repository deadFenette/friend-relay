"use strict";
/* pbkdf2-worker.js — вывод AES-ключа в фоновом потоке (v1.9.5).
   PBKDF2-HMAC-SHA256 600k итераций синхронно занимает секунды; раньше это
   считалось в ГЛАВНОМ потоке и вкладка «умирала» (никакого отклика, ни
   спиннера, ни прогресса — на слабом телефоне до 10с). Теперь тяжёлая
   работа здесь, UI живой. forge подтягиваем тем же /static, что и страница. */
/* Внутри Worker нет window — а forge.min.js при загрузке обращается к
   window.* (define'ы окружения). Подкладываем self вместо window ДО
   importScripts, иначе ReferenceError и весь PBKDF2 молча падал обратно
   в синхронный путь главного потока (заморозка страницы, которую
   Worker и должен был убрать). */
if (typeof window === "undefined") { self.window = self; }
importScripts("/static/forge.min.js");

self.onmessage = function(e){
  const d = e.data || {};
  try {
    const hex = forge.util.bytesToHex(forge.pkcs5.pbkdf2(
      forge.util.encodeUtf8(d.pass),
      forge.util.encodeUtf8(d.salt),
      d.iter, 32, "sha256"));
    self.postMessage({ok: true, hex: hex});
  } catch (err) {
    self.postMessage({ok: false, error: String(err)});
  }
};
