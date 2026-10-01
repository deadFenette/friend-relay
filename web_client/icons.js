"use strict";
/* icons.js — «Studio» icon swap: replaces emoji chrome with monoline SVG.
 *
 * ИДЕЯ: эмодзи в UI-хроме (кнопки, табы, заголовки) — главный «ИИ-тell».
 * Этот модуль подменяет известные эмодзи на минимал-лайн SVG-иконки
 * (Lucide-стиль: 24×24, stroke=1.75, currentColor). Подмена идёт по
 * селекторам — содержимое сообщений и пользовательский текст НЕ трогаем.
 *
 * Подмена идёт в два захода:
 *  1) статические emoji в index.html заменяются инлайн-SVG при парсинге
 *     (там они уже написаны как <span class="ico" data-i="chat"></span>);
 *  2) динамические emoji (которые JS вставляет в innerHTML кнопок при
 *     работе — 🗑, 📌, 🌊, 🎤 и т.д.) подменяются MutationObserver-ом.
 *
 * Совместимость: ничего не ломает. Если иконки нет — emoji остаётся.
 * Если MutationObserver не поддерживается — статика всё равно работает. */

(function () {

/* ── SVG-спрайт: один <symbol> на иконку, зовётся через <use href="#i-…"> ──
   Все иконки — 24×24 viewBox, stroke=currentColor, fill=none,
   stroke-width=1.75, round caps/joins. Цвет наследует кнопку. */
const SPRITE = `<svg aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden" xmlns="http://www.w3.org/2000/svg">
<symbol id="i-relay" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M4.5 16.5a9 9 0 0 1 15 0"/>
  <path d="M7.5 13.5a5 5 0 0 1 9 0"/>
  <circle cx="12" cy="19" r="1.4" fill="currentColor" stroke="none"/>
  <path d="M3 7.5a14 14 0 0 1 18 0"/>
</symbol>
<symbol id="i-chat" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M21 11.5a8.4 8.4 0 0 1-9.8 8.3 8.5 8.5 0 0 1-3.1-.6L3 21l1.8-4.5A8.4 8.4 0 0 1 12 3.1a8.4 8.4 0 0 1 9 8.4z"/>
</symbol>
<symbol id="i-dm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <rect x="3" y="5" width="18" height="14" rx="2.5"/>
  <path d="M3.5 7.5l8.5 5.5 8.5-5.5"/>
</symbol>
<symbol id="i-files" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M4 6.5a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.5.7l1.4 1.6H18a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>
</symbol>
<symbol id="i-profile" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="8.5" r="4"/>
  <path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>
</symbol>
<symbol id="i-chess" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M9 21h6"/>
  <path d="M10 21v-3.5c-2 0-3-1.6-3-3.5 0-1.4.8-2.6 1.5-3.5L9 9c-.5-.5-.5-1.4 0-2 .7-.7 1.4-.7 2 0l1 1V6c0-1.5 1-3 3-3 .5 0 1 .5 1 1.5V12c1 .8 1.5 1.6 1.5 3 0 2-1 3.5-3 3.5V21"/>
</symbol>
<symbol id="i-games" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <line x1="6" y1="11" x2="10" y2="11"/>
  <line x1="8" y1="9" x2="8" y2="13"/>
  <line x1="15" y1="12" x2="15.01" y2="12"/>
  <line x1="18" y1="10" x2="18.01" y2="10"/>
  <rect x="2" y="6" width="20" height="12" rx="4"/>
</symbol>
<symbol id="i-voice" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <rect x="9" y="3" width="6" height="11" rx="3"/>
  <path d="M5 11a7 7 0 0 0 14 0"/>
  <line x1="12" y1="18" x2="12" y2="22"/>
</symbol>
<symbol id="i-music" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="7" cy="18" r="2.6"/>
  <circle cx="17.5" cy="15.5" r="2.6"/>
  <path d="M9.6 18V7.5l10.5-2.6V15.5"/>
  <path d="M9.6 10.5l10.5-2.6"/>
</symbol>
<symbol id="i-screen" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <rect x="2.5" y="4" width="19" height="13" rx="2.2"/>
  <path d="M8.5 21h7"/>
  <path d="M12 17.5V21"/>
  <path d="M8.8 13V9.2M12 13V7.6M15.2 13v-2.6"/>
</symbol>
<symbol id="i-play" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M8 5.5v13l10-6.5z" fill="currentColor" stroke="none"/>
</symbol>
<symbol id="i-pause" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <rect x="7" y="5.5" width="3.4" height="13" rx="1.2" fill="currentColor" stroke="none"/>
  <rect x="13.6" y="5.5" width="3.4" height="13" rx="1.2" fill="currentColor" stroke="none"/>
</symbol>
<symbol id="i-skip-fwd" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M6 6v12l8-6z" fill="currentColor" stroke="none"/>
  <line x1="17.5" y1="6" x2="17.5" y2="18"/>
</symbol>
<symbol id="i-skip-back" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M18 6v12l-8-6z" fill="currentColor" stroke="none"/>
  <line x1="6.5" y1="6" x2="6.5" y2="18"/>
</symbol>
<symbol id="i-theme" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
  <path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor" stroke="none"/>
</symbol>
<symbol id="i-ux" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <line x1="4" y1="6" x2="20" y2="6"/>
  <line x1="4" y1="12" x2="20" y2="12"/>
  <line x1="4" y1="18" x2="20" y2="18"/>
  <circle cx="9" cy="6" r="2.2" fill="var(--surface, #14161b)" stroke="currentColor"/>
  <circle cx="15" cy="12" r="2.2" fill="var(--surface, #14161b)" stroke="currentColor"/>
  <circle cx="8" cy="18" r="2.2" fill="var(--surface, #14161b)" stroke="currentColor"/>
</symbol>
<symbol id="i-send" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 19V5"/>
  <path d="M5 12l7-7 7 7"/>
</symbol>
<symbol id="i-emoji" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
  <line x1="9" y1="10" x2="9.01" y2="10"/>
  <line x1="15" y1="10" x2="15.01" y2="10"/>
  <path d="M8.5 14.5a4.5 4.5 0 0 0 7 0"/>
</symbol>
<symbol id="i-pin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 17v5"/>
  <path d="M9 3h6l-1 7 3 2v3H7v-3l3-2-1-7z"/>
</symbol>
<symbol id="i-search" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="11" cy="11" r="7"/>
  <line x1="16.5" y1="16.5" x2="21" y2="21"/>
</symbol>
<symbol id="i-bell" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6z"/>
  <path d="M10 19a2 2 0 0 0 4 0"/>
</symbol>
<symbol id="i-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <line x1="5" y1="12" x2="19" y2="12"/>
  <line x1="12" y1="5" x2="12" y2="19"/>
</symbol>
<symbol id="i-trash" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="3,6 5,6 21,6"/>
  <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
  <path d="M10 11v6M14 11v6"/>
  <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
</symbol>
<symbol id="i-x" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <line x1="6" y1="6" x2="18" y2="18"/>
  <line x1="18" y1="6" x2="6" y2="18"/>
</symbol>
<!-- v3.6.8: зум лайтбокса скриншотов (images.js) -->
<symbol id="i-zoom-in" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="11" cy="11" r="7"/>
  <line x1="16.5" y1="16.5" x2="21" y2="21"/>
  <line x1="8" y1="11" x2="14" y2="11"/>
  <line x1="11" y1="8" x2="11" y2="14"/>
</symbol>
<symbol id="i-zoom-out" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="11" cy="11" r="7"/>
  <line x1="16.5" y1="16.5" x2="21" y2="21"/>
  <line x1="8" y1="11" x2="14" y2="11"/>
</symbol>
<symbol id="i-refresh" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="21,4 21,10 15,10"/>
  <path d="M3.5 12a8 8 0 0 1 14-5.3L21 10"/>
  <polyline points="3,20 3,14 9,14"/>
  <path d="M20.5 12a8 8 0 0 1-14 5.3L3 14"/>
</symbol>
<symbol id="i-mic-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <line x1="3" y1="3" x2="21" y2="21"/>
  <path d="M9 9v2a3 3 0 0 0 5.1 2.1"/>
  <path d="M15 11V6a3 3 0 0 0-5.7-1.3"/>
  <path d="M5 11a7 7 0 0 0 9.5 6.5"/>
  <line x1="12" y1="18" x2="12" y2="22"/>
</symbol>
<symbol id="i-attach" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M21 11.5l-8.5 8.5a5 5 0 0 1-7-7l8-8a3.5 3.5 0 0 1 5 5l-7.5 7.5a2 2 0 0 1-3-3l7-7"/>
</symbol>
<symbol id="i-image" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <rect x="3" y="3" width="18" height="18" rx="2.5"/>
  <circle cx="8.5" cy="8.5" r="1.5"/>
  <path d="M21 15l-5-5L5 21"/>
</symbol>
<symbol id="i-save" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M5 3h11l3 3v15a0 0 0 0 1 0 0H5a0 0 0 0 1 0 0z"/>
  <path d="M8 3v6h7V3"/>
  <path d="M8 14h8v7H8z"/>
</symbol>
<symbol id="i-chevron-down" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="6,9 12,15 18,9"/>
</symbol>
<symbol id="i-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="4,12 10,18 20,6"/>
</symbol>
<symbol id="i-flag" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M4 21V4"/>
  <path d="M4 4h11l-1.5 4L15 12H4"/>
</symbol>
<symbol id="i-volume" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <polygon points="11,5 6,9 3,9 3,15 6,15 11,19" fill="currentColor" stroke="none"/>
  <path d="M16 8.5a4 4 0 0 1 0 7"/>
  <path d="M19 5.5a8 8 0 0 1 0 13"/>
</symbol>
<symbol id="i-sparkles" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 4l1.5 4.5L18 10l-4.5 1.5L12 16l-1.5-4.5L6 10l4.5-1.5z" fill="currentColor" stroke="none"/>
  <path d="M18 4l.6 1.8L20.5 6.5 18.6 7.2 18 9l-.6-1.8L15.5 6.5 17.4 5.8z" fill="currentColor" stroke="none"/>
</symbol>
<symbol id="i-rocket" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M5 15c-1 1-1.5 4-1.5 4s3-.5 4-1.5"/>
  <path d="M9 15l-3-3c1-5 4-9 11-9 0 7-4 10-9 11z"/>
  <path d="M9 15l-3 3"/>
  <circle cx="14.5" cy="9.5" r="1.5"/>
</symbol>
<symbol id="i-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <rect x="9" y="9" width="11" height="11" rx="2"/>
  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
</symbol>
<symbol id="i-download" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 4v12"/>
  <path d="M7 11l5 5 5-5"/>
  <path d="M5 20h14"/>
</symbol>
<symbol id="i-upload" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 20V8"/>
  <path d="M7 13l5-5 5 5"/>
  <path d="M5 4h14"/>
</symbol>
<symbol id="i-power" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 4v8"/>
  <path d="M7 6a8 8 0 1 0 10 0"/>
</symbol>
<symbol id="i-key" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="8" cy="15" r="4"/>
  <path d="M11 12l9-9"/>
  <path d="M17 5l2 2"/>
  <path d="M14 8l2 2"/>
</symbol>
<symbol id="i-eye" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z"/>
  <circle cx="12" cy="12" r="3"/>
</symbol>
<symbol id="i-info" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
  <line x1="12" y1="11" x2="12" y2="16"/>
  <circle cx="12" cy="8" r="0.5" fill="currentColor"/>
</symbol>
<symbol id="i-circle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
</symbol>
/* ── НОВЫЕ ИКОНКИ: шахматные фигуры (для карты Chess в играх) ── */
<symbol id="i-chess-king" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <line x1="12" y1="2" x2="12" y2="5"/>
  <line x1="10" y1="3.5" x2="14" y2="3.5"/>
  <path d="M8 7h8l-1 4a3 3 0 0 1-6 0z"/>
  <path d="M9 13l-1 5h8l-1-5"/>
  <path d="M7 21h10"/>
</symbol>
<symbol id="i-chess-queen" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="5" cy="7" r="1.3"/>
  <circle cx="9.5" cy="4.5" r="1.3"/>
  <circle cx="14.5" cy="4.5" r="1.3"/>
  <circle cx="19" cy="7" r="1.3"/>
  <path d="M5 7l2 8h10l2-8"/>
  <path d="M7 15h10"/>
  <path d="M6 18h12v3H6z"/>
</symbol>
<symbol id="i-chess-rook" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M6 4h3v2h2V4h2v2h2V4h3v5l-2 1v6l2 1v3H6v-3l2-1v-6L6 9z"/>
  <line x1="9" y1="13" x2="15" y2="13"/>
</symbol>
<symbol id="i-chess-bishop" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="4" r="1.3"/>
  <path d="M9 7c0-2 1.5-3 3-3s3 1 3 3c0 3-3 5-3 7 0-2-3-4-3-7z"/>
  <path d="M8 14h8l1 4H7z"/>
  <path d="M6 21h12"/>
</symbol>
<symbol id="i-chess-knight" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M7 21h10v-3c0-3-2-5-2-7 0-3 2-5 2-5l-3-3-2 3-3 1c-2 1-3 3-3 6 0 1 1 2 2 2 1 0 2-1 2-2"/>
  <line x1="7" y1="21" x2="17" y2="21"/>
</symbol>
<symbol id="i-chess-pawn" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="6" r="2"/>
  <path d="M9 11h6l-1 4h-4z"/>
  <path d="M8 15h8l1 4H7z"/>
</symbol>
/* ── Иконки для игр (карточки в лаунчере) ── */
<symbol id="i-game-dice" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <rect x="3" y="3" width="18" height="18" rx="3"/>
  <circle cx="8" cy="8" r="1.2" fill="currentColor"/>
  <circle cx="16" cy="16" r="1.2" fill="currentColor"/>
  <circle cx="12" cy="12" r="1.2" fill="currentColor"/>
  <circle cx="16" cy="8" r="1.2" fill="currentColor"/>
  <circle cx="8" cy="16" r="1.2" fill="currentColor"/>
</symbol>
<symbol id="i-game-puzzle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M8 4h2v2a2 2 0 0 0 4 0V4h2a2 2 0 0 1 2 2v2h-2a2 2 0 0 0 0 4h2v2a2 2 0 0 1-2 2h-2v-2a2 2 0 0 0-4 0v2H8a2 2 0 0 1-2-2v-2H4a2 2 0 0 1 0-4h2V6a2 2 0 0 1 2-2z"/>
</symbol>
<symbol id="i-game-target" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
  <circle cx="12" cy="12" r="5"/>
  <circle cx="12" cy="12" r="1.3" fill="currentColor"/>
</symbol>
<symbol id="i-game-bomb" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="11" cy="14" r="7"/>
  <line x1="14" y1="9" x2="18" y2="5"/>
  <line x1="18" y1="5" x2="20" y2="3"/>
  <line x1="18" y1="5" x2="20" y2="6"/>
  <line x1="18" y1="5" x2="17" y2="3"/>
  <circle cx="9" cy="12" r="1" fill="currentColor"/>
</symbol>
<symbol id="i-game-grid" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <rect x="3" y="3" width="8" height="8" rx="1.5"/>
  <rect x="13" y="3" width="8" height="8" rx="1.5"/>
  <rect x="3" y="13" width="8" height="8" rx="1.5"/>
  <rect x="13" y="13" width="8" height="8" rx="1.5"/>
</symbol>
<symbol id="i-game-circle-stone" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
  <circle cx="12" cy="12" r="4.5" fill="currentColor" stroke="none" opacity="0.18"/>
  <circle cx="12" cy="12" r="4.5"/>
</symbol>
<symbol id="i-game-rocket" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M5 15c-1 1-1.5 4-1.5 4s3-.5 4-1.5"/>
  <path d="M9 15l-3-3c1-5 4-9 11-9 0 7-4 10-9 11z"/>
  <path d="M9 15l-3 3"/>
  <circle cx="14.5" cy="9.5" r="1.3"/>
</symbol>
<symbol id="i-game-voxel" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 3l9 5v8l-9 5-9-5V8z"/>
  <path d="M12 3v9l9 4"/>
  <path d="M12 12L3 8"/>
  <path d="M12 12v9"/>
</symbol>
<symbol id="i-game-snake" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M4 8h6a3 3 0 0 1 0 6H8a3 3 0 0 0 0 6h12"/>
  <circle cx="4" cy="8" r="1.3" fill="currentColor"/>
  <circle cx="20" cy="20" r="1.3" fill="currentColor"/>
</symbol>
/* ── Больше UI-иконок ── */
<symbol id="i-settings" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="3"/>
  <path d="M19.4 14.5l1.4 1.1-2 3.5-1.8-.7a7.5 7.5 0 0 1-1.7 1l-.3 1.9h-4l-.3-1.9a7.5 7.5 0 0 1-1.7-1l-1.8.7-2-3.5 1.4-1.1a7.6 7.6 0 0 1 0-2L2.2 11.4l2-3.5 1.8.7a7.5 7.5 0 0 1 1.7-1l.3-1.9h4l.3 1.9a7.5 7.5 0 0 1 1.7 1l1.8-.7 2 3.5-1.4 1.1a7.6 7.6 0 0 1 0 2z"/>
</symbol>
<symbol id="i-user-circle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
  <circle cx="12" cy="10" r="3"/>
  <path d="M5.5 19a7 7 0 0 1 13 0"/>
</symbol>
<symbol id="i-users" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="9" cy="8" r="3.2"/>
  <path d="M3 20a6 6 0 0 1 12 0"/>
  <path d="M16 5.5a3.2 3.2 0 0 1 0 5"/>
  <path d="M17 14.5A6 6 0 0 1 21 20"/>
</symbol>
<symbol id="i-lock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <rect x="5" y="11" width="14" height="9" rx="2"/>
  <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
  <circle cx="12" cy="15.5" r="1" fill="currentColor"/>
</symbol>
<symbol id="i-unlock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <rect x="5" y="11" width="14" height="9" rx="2"/>
  <path d="M8 11V7a4 4 0 0 1 8 0"/>
  <circle cx="12" cy="15.5" r="1" fill="currentColor"/>
</symbol>
<symbol id="i-headphones" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M4 14v-2a8 8 0 0 1 16 0v2"/>
  <rect x="3" y="13" width="4" height="6" rx="1.5"/>
  <rect x="17" y="13" width="4" height="6" rx="1.5"/>
</symbol>
<symbol id="i-mic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <rect x="9" y="3" width="6" height="11" rx="3"/>
  <path d="M5 11a7 7 0 0 0 14 0"/>
  <line x1="12" y1="18" x2="12" y2="22"/>
  <line x1="9" y1="22" x2="15" y2="22"/>
</symbol>
<symbol id="i-terminal" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <rect x="3" y="4" width="18" height="16" rx="2"/>
  <polyline points="7,9 10,12 7,15"/>
  <line x1="13" y1="15" x2="17" y2="15"/>
</symbol>
<symbol id="i-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M10 14a4 4 0 0 0 5.5.4l3-3a4 4 0 0 0-5.7-5.7l-1.4 1.4"/>
  <path d="M14 10a4 4 0 0 0-5.5-.4l-3 3A4 4 0 0 0 11.2 18l1.4-1.4"/>
</symbol>
<symbol id="i-external" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14 5h5v5"/>
  <line x1="19" y1="5" x2="11" y2="13"/>
  <path d="M19 13v6a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6"/>
</symbol>
<symbol id="i-edit" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14 4l6 6L9 21H3v-6z"/>
  <line x1="13" y1="5" x2="19" y2="11"/>
</symbol>
<symbol id="i-hash" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <line x1="9" y1="4" x2="7" y2="20"/>
  <line x1="17" y1="4" x2="15" y2="20"/>
  <line x1="4" y1="9" x2="20" y2="9"/>
  <line x1="3" y1="15" x2="19" y2="15"/>
</symbol>
<symbol id="i-at" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="4"/>
  <path d="M16 12v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-3.5 7.1"/>
</symbol>
<symbol id="i-clock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
  <polyline points="12,7 12,12 16,14"/>
</symbol>
<symbol id="i-calendar" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <rect x="3" y="5" width="18" height="16" rx="2"/>
  <line x1="3" y1="10" x2="21" y2="10"/>
  <line x1="8" y1="3" x2="8" y2="7"/>
  <line x1="16" y1="3" x2="16" y2="7"/>
</symbol>
<symbol id="i-bookmark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M6 3h12v18l-6-4-6 4z"/>
</symbol>
<symbol id="i-star" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <polygon points="12,3 14.7,9.1 21,9.8 16.4,14 17.8,20.3 12,17 6.2,20.3 7.6,14 3,9.8 9.3,9.1"/>
</symbol>
<symbol id="i-heart" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 20s-7-4.3-9-8.5C1.5 7 4 4 7 4c2 0 3.5 1.2 5 3 1.5-1.8 3-3 5-3 3 0 5.5 3 4 7.5-2 4.2-9 8.5-9 8.5z"/>
</symbol>
<symbol id="i-share" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="6" cy="12" r="2.5"/>
  <circle cx="18" cy="6" r="2.5"/>
  <circle cx="18" cy="18" r="2.5"/>
  <line x1="8.2" y1="10.8" x2="15.8" y2="7.2"/>
  <line x1="8.2" y1="13.2" x2="15.8" y2="16.8"/>
</symbol>
<symbol id="i-more" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="6" cy="12" r="1.2" fill="currentColor"/>
  <circle cx="12" cy="12" r="1.2" fill="currentColor"/>
  <circle cx="18" cy="12" r="1.2" fill="currentColor"/>
</symbol>
<symbol id="i-arrow-left" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <line x1="20" y1="12" x2="4" y2="12"/>
  <polyline points="10,6 4,12 10,18"/>
</symbol>
<symbol id="i-arrow-up" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <line x1="12" y1="20" x2="12" y2="4"/>
  <polyline points="6,10 12,4 18,10"/>
</symbol>
<symbol id="i-snow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <line x1="12" y1="3" x2="12" y2="21"/>
  <line x1="3" y1="12" x2="21" y2="12"/>
  <line x1="5.6" y1="5.6" x2="18.4" y2="18.4"/>
  <line x1="18.4" y1="5.6" x2="5.6" y2="18.4"/>
  <polyline points="9,4 12,7 15,4"/>
  <polyline points="9,20 12,17 15,20"/>
  <polyline points="4,9 7,12 4,15"/>
  <polyline points="20,9 17,12 20,15"/>
</symbol>
<symbol id="i-wave" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M2 14c2 0 2-3 4-3s2 6 4 6 2-9 4-9 2 6 4 6 2-3 4-3"/>
  <path d="M2 19c2 0 2-1.5 4-1.5s2 3 4 3 2-4.5 4-4.5 2 3 4 3 2-1.5 4-1.5" opacity="0.5"/>
</symbol>
<symbol id="i-flower" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="2.2"/>
  <path d="M12 9.8c0-3-1-5.8-2-5.8S8 5.5 8 7.5s2 2.3 4 2.3z"/>
  <path d="M14.2 12c3 0 5.8-1 5.8-2s-1.5-2-3.5-2-2.3 2-2.3 4z"/>
  <path d="M12 14.2c0 3 1 5.8 2 5.8s2-1.5 2-3.5-2-2.3-4-2.3z"/>
  <path d="M9.8 12c-3 0-5.8 1-5.8 2s1.5 2 3.5 2 2.3-2 2.3-4z"/>
</symbol>
<symbol id="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>
</symbol>
<symbol id="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="4.5"/>
  <line x1="12" y1="2" x2="12" y2="5"/>
  <line x1="12" y1="19" x2="12" y2="22"/>
  <line x1="2" y1="12" x2="5" y2="12"/>
  <line x1="19" y1="12" x2="22" y2="12"/>
  <line x1="5" y1="5" x2="7" y2="7"/>
  <line x1="17" y1="17" x2="19" y2="19"/>
  <line x1="19" y1="5" x2="17" y2="7"/>
  <line x1="7" y1="17" x2="5" y2="19"/>
</symbol>
<symbol id="i-cloud" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M7 18h11a4 4 0 0 0 .5-8A6 6 0 0 0 7 9a4 4 0 0 0 0 9z"/>
</symbol>
<symbol id="i-palette" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 3a9 9 0 0 0 0 18c1 0 1.5-.8 1.5-1.5 0-.4-.2-.7-.4-1-.3-.3-.4-.6-.4-1 0-.8.7-1.5 1.5-1.5H16a5 5 0 0 0 5-5c0-4.4-4-8-9-8z"/>
  <circle cx="7.5" cy="11.5" r="1" fill="currentColor"/>
  <circle cx="10.5" cy="7.5" r="1" fill="currentColor"/>
  <circle cx="15" cy="8" r="1" fill="currentColor"/>
  <circle cx="17.5" cy="12" r="1" fill="currentColor"/>
</symbol>
<symbol id="i-sliders" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <line x1="4" y1="6" x2="20" y2="6"/>
  <line x1="4" y1="12" x2="20" y2="12"/>
  <line x1="4" y1="18" x2="20" y2="18"/>
  <circle cx="9" cy="6" r="2.2" fill="var(--surface, #14161b)" stroke="currentColor"/>
  <circle cx="15" cy="12" r="2.2" fill="var(--surface, #14161b)" stroke="currentColor"/>
  <circle cx="8" cy="18" r="2.2" fill="var(--surface, #14161b)" stroke="currentColor"/>
</symbol>
<symbol id="i-trophy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M7 4h10v5a5 5 0 0 1-10 0z"/>
  <path d="M7 5H4v2a3 3 0 0 0 3 3"/>
  <path d="M17 5h3v2a3 3 0 0 1-3 3"/>
  <path d="M12 14v4"/>
  <path d="M9 21h6"/>
  <path d="M10 18h4l1 3H9z"/>
</symbol>
<symbol id="i-zap" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <polygon points="13,3 4,14 11,14 10,21 19,10 12,10" fill="currentColor" stroke="none"/>
</symbol>
<symbol id="i-shield" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>
  <polyline points="9,12 11,14 15,10"/>
</symbol>
<symbol id="i-eye-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M3 3l18 18"/>
  <path d="M10.6 10.6a3 3 0 0 0 4.2 4.2"/>
  <path d="M9.4 5.2A9.6 9.6 0 0 1 12 5c5 0 9 4 10 7a13 13 0 0 1-2 3.2"/>
  <path d="M5.2 9.4A13 13 0 0 0 2 12c1 3 5 7 10 7a9.6 9.6 0 0 0 2.6-.4"/>
</symbol>
<symbol id="i-compass" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
  <polygon points="15,9 13,13 9,15 11,11"/>
</symbol>
<symbol id="i-feather" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M20 4c-3 0-9 1-12 6l-3 5 3-1c3-5 8-6 11-6"/>
  <path d="M16 8c-3 1-6 3-7 7"/>
  <path d="M5 19l4-4"/>
</symbol>
<symbol id="i-wifi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M2 8a16 16 0 0 1 20 0"/>
  <path d="M5 12a11 11 0 0 1 14 0"/>
  <path d="M8.5 15.5a6 6 0 0 1 7 0"/>
  <circle cx="12" cy="19" r="1" fill="currentColor"/>
</symbol>
<symbol id="i-globe" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="9"/>
  <ellipse cx="12" cy="12" rx="4" ry="9"/>
  <line x1="3" y1="12" x2="21" y2="12"/>
</symbol>
<symbol id="i-bell-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M13.7 21a2 2 0 0 1-3.4 0"/>
  <path d="M18.4 15.8c-.7-1.1-1.9-1.7-1.9-6.3A4.5 4.5 0 0 0 12 5a4.4 4.4 0 0 0-3 1.2"/>
  <path d="M6.3 7.5c-.3.6-.4 1.3-.4 2 0 4.6-1.2 5.2-1.9 6.3-.4.7 0 1.7.9 1.7h13.9"/>
  <line x1="3" y1="3" x2="21" y2="21"/>
</symbol>
<symbol id="i-stop" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <rect x="6.5" y="6.5" width="11" height="11" rx="2"/>
</symbol>
<symbol id="i-medal" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M8.2 8.6 6.4 3.5h3.9L12 6.4l1.7-2.9h3.9l-1.8 5.1"/>
  <circle cx="12" cy="15" r="4.6"/>
  <path d="M12 12.7l.7 1.4 1.6.2-1.2 1.1.3 1.6-1.4-.8-1.4.8.3-1.6-1.2-1.1 1.6-.2z" fill="currentColor" stroke="none"/>
</symbol>
<symbol id="i-file-image" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z"/>
  <path d="M14.5 3v4h4"/>
  <circle cx="9.7" cy="11.2" r="1" fill="currentColor" stroke="none"/>
  <path d="M7.8 17l2.7-2.9 2 2.1 1.6-1.6 2.1 2.4"/>
</symbol>
<symbol id="i-file-video" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z"/>
  <path d="M14.5 3v4h4"/>
  <path d="M10 11.7v4.6l4-2.3z" fill="currentColor" stroke-linejoin="round"/>
</symbol>
<symbol id="i-file-audio" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z"/>
  <path d="M14.5 3v4h4"/>
  <path d="M10 17.2v-5.4l4-1v5.3"/>
  <circle cx="8.9" cy="17.6" r="1.2" fill="currentColor" stroke="none"/>
  <circle cx="12.9" cy="16.5" r="1.2" fill="currentColor" stroke="none"/>
</symbol>
<symbol id="i-file-archive" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z"/>
  <path d="M14.5 3v4h4"/>
  <path d="M12 9.5v1.6"/>
  <path d="M10.4 12.6h3.2v4.9h-3.2z"/>
</symbol>
<symbol id="i-file-pdf" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z"/>
  <path d="M14.5 3v4h4"/>
  <path d="M9.5 18v-5.5l2.5 1.7 2.5-1.7V18" stroke-width="1.5"/>
</symbol>
<symbol id="i-file-doc" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z"/>
  <path d="M14.5 3v4h4"/>
  <path d="M9 12h6M9 15h6M9 18h3.5"/>
</symbol>
<symbol id="i-file-exe" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z"/>
  <path d="M14.5 3v4h4"/>
  <rect x="9" y="10.5" width="6" height="6" rx="1"/>
  <path d="M9.5 12.2h1.6l.9 2.4 1-3.4 1 2.6.6-1.6h1.4" stroke-width="1.4"/>
</symbol>
<symbol id="i-file-code" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z"/>
  <path d="M14.5 3v4h4"/>
  <path d="M9.7 11.5 8 13.3l1.7 1.7M14.3 11.5l1.7 1.8-1.7 1.7M12.9 10.8l-1.6 5" stroke-width="1.5"/>
</symbol>
<symbol id="i-file-package" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z"/>
  <path d="M14.5 3v4h4"/>
  <path d="M9 17.5v-5l3-1.7 3 1.7v5"/>
  <path d="M9 12.5l3 1.7 3-1.7M12 14.2v3.3"/>
</symbol>
<symbol id="i-cherry" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M11 3.5c3.2.4 4.7 3.5 4.8 8.9"/>
  <path d="M11 3.5C10.3 6.7 8.5 9.2 5.6 11.8"/>
  <circle cx="15.7" cy="16.6" r="3.9"/>
  <circle cx="5.6" cy="15.2" r="3.4"/>
</symbol>
<symbol id="i-creme" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <ellipse cx="12" cy="10.2" rx="7" ry="2.6"/>
  <path d="M5 10.2v3.4c0 1.9 3.1 3.4 7 3.4s7-1.5 7-3.4v-3.4"/>
  <path d="m7.2 9.9 1.9-1 2.4 1.9 2.5-2 2.1 1.6 1.9-1" stroke-width="1.3"/>
</symbol>
</svg>`;

/* ── Карта эмодзи → ID иконки ──
   Используется MutationObserver-ом и ручным swapEmoji(). */
const EMOJI_MAP = {
  "📡": "relay",
  "💬": "chat",
  "✉": "dm", "✉️": "dm",
  "📁": "files",
  "👤": "profile",
  "♟": "chess", "♟️": "chess",
  "🎮": "games",
  "🎙": "voice", "🎙️": "voice",
  /* тема: иконка зависит от следующей темы — main.js ставит эмодзи,
     мы их подменяем на осмысленные SVG */
  "🌊": "wave",       /* → Lukewarm Ocean */
  "🌸": "flower",     /* → Sakura */
  "🍒": "cherry",     /* → Cherry Grove (ягода точнее цветка) */
  "🍮": "creme",      /* → Crème Brûlée */
  "❄": "snow", "❄️": "snow", "🌨": "snow", "🌨️": "snow",  /* → Snow */
  "🌌": "moon",       /* → Aurora (тёмная) */
  "🎨": "palette",
  "🚀": "rocket",
  "😊": "emoji",
  "➤": "send",
  "📌": "pin",
  "🔍": "search",
  "🔔": "bell",
  "🔕": "bell-off",
  "＋": "plus",
  "🗑": "trash", "🗑️": "trash",
  "✕": "x",
  "↻": "refresh",
  "🎤": "mic",
  "🎧": "headphones",
  "🔇": "mic-off",
  "🔊": "volume",
  "📎": "attach",
  "🖼": "image", "🖼️": "image",   /* кнопка «Сменить аватар» (статика); файловая витрина рисует i-file-image напрямую */
  "💾": "save",
  "🏳": "flag", "🏳️": "flag",
  "⬆": "upload", "⬇": "download",
  "▸": "chevron-down",
  "⚙": "settings", "⚙️": "settings",
  "✨": "sparkles",
  "🔤": "sliders",
  "📐": "sliders",
  "🧊": "cloud",
  "🖌": "palette",
  /* v3.2: дочистка хрома — кнопки действий сообщений, файлы, голос */
  "😀": "emoji",
  "📋": "copy",
  "✏": "edit", "✏️": "edit",
  "🎬": "file-video",
  "🎵": "file-audio",
  "🗜": "file-archive", "🗜️": "file-archive",
  "📕": "file-pdf",
  "📄": "file-doc",
  "📦": "file-package",
  "🔌": "power",
  "⏹": "stop",
  "🟢": "circle",
  "🥇": "medal", "🥈": "medal", "🥉": "medal",
  /* v3.3.0: музыкальная вкладка и плеер */
  "🎵": "music", "🎶": "music",
  /* v3.6.0: демонстрация экрана */
  "🖥": "screen", "🖥️": "screen", "📺": "screen",
};

/* Элементы, в которых эмодзи ТОЧНО надо подменять (UI-хром).
   Сообщения и пользовательский текст — никогда. */
const CHROME_SELECTORS = [
  "header button",
  "header .brandlogo",
  "header .h-brand b",
  ".tabs button",
  "#connectPanel .brandhero .logo",
  "#btnConnect",
  ".chatTop button",
  ".chatTop .filebtn",
  "#composer button",
  "#composer .filebtn",
  "#dmComposer button",
  "#dmComposer .filebtn",
  "#tab-files .row button",
  "#tab-files .filebtn",
  "#tab-profile .row button",
  "#tab-profile .filebtn",
  "#tab-chess .row button",
  "#tab-chess .chess-back",
  "#tab-chess .subhead",
  "#tab-voice .row button",
  "#tab-voice .voiceset label",
  "#tab-voice .subhead",
  "#tab-games .gameicon",
  "#tab-games .games-section-title",
  "#tab-games .hint",
  /* v3.3.0: музыкальная вкладка */
  "#tab-music button",
  "#tab-music .subhead",
  "#tab-music .music-cover",
  "#tab-music .music-api-head",
  /* v3.6.0: демонстрация экрана */
  "#tab-screen button",
  "#tab-screen .subhead",
  "#tab-screen .field",
  ".uxcard-ico",
  ".ux-io button",
  ".ux-foot button",
  ".ux-head button",
  ".howto summary",
  /* v3.7.1: спойлер ключей на дружелюбном экране входа — иконка замка */
  ".advkeys summary",
  ".gamewin-bar button",
  ".subhead",
  /* статичные .gameicon из featured-секции тоже подхватываем (data-i) */
  ".gameicon[data-i]",
  ".games-section-title[data-i]",
  ".chess-back",
  /* v3.2: дочистка — кнопки действий баблов, файловая витрина,
     модалки и палитра (свой UI рисует иконки сам, но на всякий случай) */
  ".msg .acts button",
  ".filerow button",
  ".frmodal button",
  ".frpal-item",
];

/* ── Утилиты ── */
function injectSprite(){
  if (document.getElementById("i-sprite")) return;
  const holder = document.createElement("div");
  holder.id = "i-sprite";
  holder.style.cssText = "position:absolute;width:0;height:0;overflow:hidden";
  holder.innerHTML = SPRITE;
  document.body.insertBefore(holder, document.body.firstChild);
}

function makeIcon(name){
  /* Возвращает <svg class="ico"><use href="#i-…"/></svg> */
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "ico");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttributeNS("http://www.w3.org/1999/xlink", "href", "#i-" + name);
  use.setAttribute("href", "#i-" + name);
  svg.appendChild(use);
  return svg;
}

function swapEmojisIn(root){
  /* Проходим по CHROME_SELECTORS и подменяем эмодзи внутри них.
     Текстовые узлы разбиваем: до/после эмодзи остаётся текстом,
     эмодзи → <svg class="ico">. Если иконка не найдена —
     эмодзи остаётся как есть. */
  if (!root || !root.querySelectorAll) return;
  const targets = [];
  for (const sel of CHROME_SELECTORS){
    try {
      if (root.matches && root.matches(sel)) targets.push(root);
      targets.push(...root.querySelectorAll(sel));
    } catch(e){}
  }
  for (const el of targets){
    /* data-iconDone защищает только статичную разметку data-i.
       Для эмодзи в тексте НЕ ставим флаг: если JS потом поменяет
       textContent (как main.js крутит иконку темы 🌊→🌸→🌌), нужно
       уметь пересвопнуть. */
    if (el.dataset.i && el.dataset.iconDone === "1") continue;
    /* поддержка data-i="chat" — статичная разметка в index.html.
       data-i-prepend="0" — вставить в конец (для редких случаев). */
    if (el.dataset.i && !el.querySelector(".ico")){
      el.classList.add("ico-host");
      const ic = makeIcon(el.dataset.i);
      if (el.dataset.iPrepend === "0") el.appendChild(ic);
      else el.insertBefore(ic, el.firstChild);
      el.dataset.iconDone = "1";
      continue;
    }
    /* подмена эмодзи в текстовых узлах */
    let changed = false;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
      acceptNode(node){
        if (!node.nodeValue) return NodeFilter.FILTER_REJECT;
        for (const k of Object.keys(EMOJI_MAP)){
          if (node.nodeValue.includes(k)) return NodeFilter.FILTER_ACCEPT;
        }
        return NodeFilter.FILTER_REJECT;
      }
    });
    const targets = [];
    while (walker.nextNode()) targets.push(walker.currentNode);
    for (const textNode of targets){
      const text = textNode.nodeValue;
      /* строим регексп из всех эмодзи в карте */
      const keys = Object.keys(EMOJI_MAP).sort((a,b) => b.length - a.length);
      const re = new RegExp(keys.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "gu");
      const frag = document.createDocumentFragment();
      let last = 0, m;
      while ((m = re.exec(text))){
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        const iconName = EMOJI_MAP[m[0]];
        if (iconName) frag.appendChild(makeIcon(iconName));
        else frag.appendChild(document.createTextNode(m[0]));
        last = m.index + m[0].length;
        changed = true;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      if (changed) textNode.parentNode.replaceChild(frag, textNode);
    }
  }
}

/* ── Инициализация ──
   Спрайт вставляем сразу (синхронно), чтобы <use href="#i-…"> уже
   работал к моменту первого swap. Свапаем после DOMContentLoaded и
   ставим MutationObserver на добавленные узлы. */
function boot(){
  injectSprite();
  swapEmojisIn(document);
  /* Наблюдатель: ловит кнопки, которые JS создаёт в runtime —
     например «＋ создать канал» или «🗑 удалить» при активации канала,
     или смену иконки темы 🌊→🌸→🌌. */
  const mo = new MutationObserver((mutations) => {
    for (const m of mutations){
      for (const node of m.addedNodes){
        if (node.nodeType === 1){
          /* новый элемент — проверим его и его потомков */
          swapEmojisIn(node);
        } else if (node.nodeType === 3 && node.parentNode){
          /* текстовый узел — проверим его родителя (напр. textContent
             кнопки темы сменился на эмодзи) */
          swapEmojisIn(node.parentNode);
        }
      }
    }
  });
  mo.observe(document.body, {childList: true, subtree: true});
}

if (document.readyState === "loading"){
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

/* Публичный API (для ручных обновлений, если понадобится) */
window.FRIcon = {
  swap: swapEmojisIn,
  make: makeIcon,
  map: EMOJI_MAP,
};

})();
