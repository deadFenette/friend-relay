/* Дымовой тест appearance.js без браузера: минимальная заглушка DOM,
   затем — инициализация, клик по свотчу/сегменту/пресету, применение
   CSS-переменных и сохранение. Ловит runtime-ошибки, которые не видит
   node --check. Запуск: node scripts/smoke_appearance.js */
"use strict";
const fs = require("fs");
const path = require("path");

const CODE = fs.readFileSync(
  path.join(__dirname, "..", "web_client", "appearance.js"), "utf8");

/* ── заглушка DOM ── */
const props = new Map();               /* применение CSS-переменных */
let saved = null;
function makeEl(tag){
  const el = {
    tag, children: [], style: {
      setProperty(k, v){ props.set(k, String(v)); },
      removeProperty(k){ props.delete(k); },
    },
    dataset: {}, value: "", textContent: "", checked: false, type: "",
    disabled: false, spellcheck: false,
    _cls: new Set(),
    classList: {
      add(c){ el._cls.add(c); }, remove(c){ el._cls.delete(c); },
      toggle(c, f){ (f === undefined ? !el._cls.has(c) : f)
        ? el._cls.add(c) : el._cls.delete(c); },
      contains(c){ return el._cls.has(c); },
    },
    addEventListener(ev, fn){ (el._h[ev] = el._h[ev] || []).push(fn); },
    _h: {},
    appendChild(c){ el.children.push(c); return c; },
    querySelector(){ return null; },
    querySelectorAll(){ return []; },
    setAttribute(){}, removeAttribute(){},
    remove(){},
    /* onclick= и addEventListener("click") — оба пути реального кода */
    click(){
      if (typeof el.onclick === "function") el.onclick({});
      for (const f of el._h.click || []) f({});
    },
  };
  return el;
}
const byId = {};
for (const id of ["uxBody", "uxDrawer", "uxOverlay", "btnUx", "btnUxClose",
  "uxPresetRich", "uxPresetCalm", "uxPresetReset", "uxExport", "uxImport"])
  byId[id] = makeEl("div");
/* в index.html панель и оверлей стартуют с классом hidden */
byId.uxDrawer.classList.add("hidden");
byId.uxOverlay.classList.add("hidden");

const docListener = [];
const rootStyle = {
  setProperty(k, v){ props.set(k, String(v)); },
  removeProperty(k){ props.delete(k); },
};
global.document = {
  createElement: makeEl,
  head: makeEl("head"),
  documentElement: {
    style: rootStyle, dataset: {},
    setAttribute(){}, getAttribute(){ return null; },
    removeAttribute(){},
  },
  getElementById: (id) => byId[id] || null,
  querySelector(){ return null; },
  querySelectorAll(){ return []; },
  addEventListener(ev, fn){ docListener.push({ev, fn}); },
};
global.window = {};
try { global.navigator = {}; } catch(e){ /* node >= 21: только геттер */ }
global.localStorage = {
  getItem(){ return null; },
  setItem(k, v){ saved = {k, v}; },
};
let toasts = 0;
global.toast = () => { toasts++; };
/* $ — глобальный const из core.js (в браузере общий lexical scope всех
   классических скриптов); в node даём его глобально */
global.$ = (id) => byId[id] || null;

/* ── загрузка файла (как <script>) ── */
let failed = 0, total = 0;
function check(name, cond){
  total++;
  console.log((cond ? "  [OK] " : "  [FAIL] ") + name);
  if (!cond) failed++;
}
new Function(CODE)();

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async function main(){
/* ── проверки инициализации ── */
check("инициализация без ошибок, карточки-плагины отрисованы",
  byId.uxBody.children.length === 9);   /* 8 + скин (v3.5.8) */
check("скин по умолчанию — classic (data-skin на <html>)",
  global.document.documentElement.dataset.skin === "classic");
check("панель не открыта при старте",
  byId.uxDrawer.classList.contains("hidden"));
check("дефолтный размер текста применён",
  props.get("--font-size-base") === "15px");
check("дефолтные скругления применены",
  props.get("--radius") === "10px" && props.get("--radius-sm") === "6px");
check("акцент по умолчанию НЕ перекрашивает тему (переменные не затыканы)",
  !props.has("--accent"));

/* ── клик по свотчу «Бирюза» (#2fc4cc) ── */
let accentCard = null;
for (const c of byId.uxBody.children)
  if (c.dataset.plugin === "accent") accentCard = c;
const swBox = accentCard.children[1].children[0];   /* body > .uxswatches */
const teal = swBox.children[3];                     /* четвёртый свотч:
  «Как в теме» сдвинула палитру — Бирюза #2fc4cc теперь под индексом 3 */
teal.click();
check("клик по свотчу применил --accent", props.get("--accent") === "#2fc4cc");
check("выведен градиент бабла из акцента",
  (props.get("--me-bubble") || "").startsWith("linear-gradient(135deg,#2fc4cc"));
await wait(220);   /* save() дебаунсится на 150мс */
check("настройки ушли в localStorage", saved && saved.k === "wr_ux" &&
  JSON.parse(saved.v).accent === "#2fc4cc");

/* ── сегмент «Журнальный» шрифт ── */
let typeCard = null;
for (const c of byId.uxBody.children)
  if (c.dataset.plugin === "type") typeCard = c;
const fontSeg = typeCard.children[1].children[0];   /* body > .uxseg */
fontSeg.children[2].click();                        /* serif */
check("сегмент сменил шрифт на serif",
  global.document.documentElement.dataset.font === "serif");

/* ── сегмент скина: Классика ↔ Ночник ↔ Крем-брюле (v3.5.9) ── */
let skinCard = null;
for (const c of byId.uxBody.children)
  if (c.dataset.plugin === "skin") skinCard = c;
check("карточка «Скин» первая в панели",
  byId.uxBody.children[0] === skinCard);
const skinSeg = skinCard.children[1].children[0];   /* body > .uxseg */
skinSeg.children[1].click();                        /* Ночник */
check("сегмент переключил скин на nochnik",
  global.document.documentElement.dataset.skin === "nochnik");
skinSeg.children[2].click();                        /* Крем-брюле */
check("сегмент переключил скин на creme",
  global.document.documentElement.dataset.skin === "creme");
skinSeg.children[0].click();                        /* обратно */
check("сегмент вернул классику",
  global.document.documentElement.dataset.skin === "classic");
skinSeg.children[1].click();                        /* снова Ночник — для пресетов */

/* ── пресеты ── */
byId.uxPresetReset.click();
check("пресет «Сброс» вернул шрифт и снял акцент",
  global.document.documentElement.dataset.font === "system" &&
  !props.has("--accent") && toasts === 1);
byId.uxPresetRich.click();
check("пресет «Дорого» поднял радиус до 14px",
  props.get("--radius") === "14px");
check("пресет «Дорого» НЕ тронул скин (остался nochnik)",
  global.document.documentElement.dataset.skin === "nochnik");
byId.uxPresetCalm.click();
check("пресет «Спокойно» выключил стекло и фон",
  global.document.documentElement.dataset.glass === "off" &&
  global.document.documentElement.dataset.bgfx === "plain");
check("пресет «Спокойно» НЕ тронул скин",
  global.document.documentElement.dataset.skin === "nochnik");
check("тосты пресетов показаны (3 подряд)", toasts === 3);

/* ── открытие/закрытие панели ── */
byId.btnUx.click();
check("🎨 открывает панель и оверлей",
  !byId.uxDrawer.classList.contains("hidden") &&
  !byId.uxOverlay.classList.contains("hidden"));
byId.btnUxClose.click();
check("✕ закрывает панель",
  byId.uxDrawer.classList.contains("hidden"));

/* ── Esc-обработчик повешен на document ── */
check("Esc-обработчик повешен на document", docListener.length >= 1);

console.log(`\nИтого: ${total - failed} OK / ${failed} FAIL`);
process.exit(failed ? 1 : 0);
})();
