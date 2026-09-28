"use strict";
/* appearance.js — v2.0.8 «Appearance Studio»: настраиваемые плагины
   оформления веб-клиента.

   ИДЕЯ: интерфейс собирается из ПЛАГИНОВ — каждый плагин отвечает за
   одну область внешнего вида (акцент, шрифт, фон, стекло, баблы…)
   и настраивается человеком прямо на странице: подвинул ползунок /
   ткнул свотч — всё применилось сразу и сохранилось в браузере.

   КАК РАБОТАЕТ:
   - состояние — плоский объект UX, живёт в localStorage «wr_ux»
     (JSON, объединяется с DEFAULTS — добавление нового плагина
     не ломает старые сохранения);
   - применение — без перезагрузки: атрибуты data-* на <html>
     (их читают селекторы style.css) + инлайновые CSS-переменные на
     documentElement (они сильнее любой темы, поэтому акцент
     перекрашивает и Aurora, и Cherry Grove);
   - панель рисуется из реестра PLUGINS декларативно: новый плагин =
     один объект в массиве, разметка и проводка появляются сами;
   - «Своя CSS» — power-user плагин: произвольный CSS в <style#uxCustom>
     (это личная косметика пользователя в его браузере, на сервер и
     других клиентов не влияет).

   Совместимость: НЕ трогает ни один существующий id/класс/скрипт —
   только добавляет кнопку 🎨, панель и переменные. PySide6-клиент
   этим файлом не пользуется вовсе. */

(function(){

/* ───────────────────────── состояние ───────────────────────── */
const UX_KEY = "wr_ux";
const DEFAULTS = {
  skin: "classic",       /* v3.5.9: classic | nochnik | creme — каркас.
                            nochnik/creme — вторая парадигма целиком
                            (nochnik.css/js); creme — её светлая пара */
  accent: "",            /* "" = как в теме (не перекрашивать) */
  font: "system",        /* system | rounded | serif | mono */
  fscale: 15,            /* базовый размер текста, px */
  density: "normal",     /* compact | normal | roomy */
  radius: 10,            /* базовые скругления, px (Studio default) */
  bgfx: "aurora",        /* aurora | grid | dots | plain */
  bgop: 0,               /* интенсивность фонового эффекта 0..1 (выкл по умолчанию) */
  glass: "off",          /* стекло (размытие шапки/попапов) — выкл по умолчанию */
  glassBlur: 14,         /* сила размытия, px */
  anim: "on",            /* анимации и микровзаимодействия */
  bubbles: "soft",       /* soft | flat | outline | ink */
  avatars: "on",         /* аватарки в ленте */
  css: "",               /* своя CSS (power-user) */
};

let UX = loadState();
function loadState(){
  try {
    const raw = localStorage.getItem(UX_KEY);
    if (raw){
      const ux = Object.assign({}, DEFAULTS, JSON.parse(raw));
      /* v3.5.9: скин «Studio 4» (натяжка CSS на классику) заменён
         полноценной парадигмой из nochnik2.html — старые сохранения
         переезжают в «Ночник», а не в молча сломанный вариант */
      if (ux.skin === "studio4") ux.skin = "nochnik";
      return ux;
    }
  } catch(e){}
  return Object.assign({}, DEFAULTS);
}
let saveT = 0;
function save(){
  clearTimeout(saveT);            /* дебаунс: слайдеры дёргаются часто */
  saveT = setTimeout(() => {
    try { localStorage.setItem(UX_KEY, JSON.stringify(UX)); } catch(e){}
  }, 150);
}

/* ───────────────────────── цветовая математика ─────────────────────
   Из одного hex-акцента выводится ВСЯ палитра темы (градиент бабла,
   мягкие подложки, кольца фокуса, тени кнопок) — как это делают
   дизайн-системы. h = тон 0..360, s/l = насыщенность/светлота 0..1. */
function hexToRgb(hex){
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || "").trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return {r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255};
}
function rgbToHsl(r, g, b){
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return {h: 0, s: 0, l};           /* серый */
  const d = max - min;
  const s = l > .5 ? d / (2 - max - min) : d / (max + min);
  let h;
  if (max === r) h = ((g - b) / d + (g < b ? 6 : 0));
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  return {h: (h * 60) % 360, s, l};
}
function hslToHex(h, s, l){
  const f = (n) => {
    const k = (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const v = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(255 * v).toString(16).padStart(2, "0");
  };
  return "#" + f(0) + f(8) + f(4);
}
const clamp01 = (v) => Math.max(0, Math.min(1, v));

/* какие переменные перекрашивает акцент (и снимает при сбросе) */
const ACCENT_VARS = ["--accent", "--accent2", "--me-bubble", "--accent-soft",
  "--accent-soft-strong", "--accent-border", "--glow", "--btn-shadow",
  "--btn-shadow-h", "--logo-shadow", "--thumb-shadow"];

function accentDerived(hex){
  const rgb = hexToRgb(hex);
  if (!rgb) return null;
  const {h, s, l} = rgbToHsl(rgb.r, rgb.g, rgb.b);
  /* второй цвет градиента: тон +16°, чуть насыщеннее и светлее —
     мягкий «дорогой» переход без кислотности */
  const a2 = hslToHex((h + 16) % 360, clamp01(Math.max(s, .45)),
                      Math.min(.72, Math.max(.38, l + .07)));
  const c = rgb.r + "," + rgb.g + "," + rgb.b;
  return {
    "--accent": hex,
    "--accent2": a2,
    "--me-bubble": "linear-gradient(135deg," + hex + " 0%," + a2 + " 100%)",
    "--accent-soft": "rgba(" + c + ",.16)",
    "--accent-soft-strong": "rgba(" + c + ",.55)",
    "--accent-border": "rgba(" + c + ",.7)",
    "--glow": "0 0 0 3px rgba(" + c + ",.22)",
    "--btn-shadow": "0 4px 14px rgba(" + c + ",.30)",
    "--btn-shadow-h": "0 6px 18px rgba(" + c + ",.38)",
    "--logo-shadow": "0 10px 30px rgba(" + c + ",.45)",
    "--thumb-shadow": "0 2px 8px rgba(" + c + ",.5)",
  };
}

/* ───────────────────────── применение ─────────────────────────
   Атрибуты data-* читают селекторы style.css; переменные ставим
   инлайново на <html> — они побеждают темы без перезагрузки. */
const uxStyle = document.createElement("style");
uxStyle.id = "uxCustom";
document.head.appendChild(uxStyle);

function apply(){
  const h = document.documentElement, st = h.style;
  const d = UX.accent ? accentDerived(UX.accent) : null;
  for (const k of ACCENT_VARS){
    if (d) st.setProperty(k, d[k]);
    else st.removeProperty(k);          /* вернуть цвета темы */
  }
  st.setProperty("--font-size-base", (+UX.fscale || 15) + "px");
  st.setProperty("--radius", (+UX.radius || 16) + "px");
  st.setProperty("--radius-sm",
    Math.max(6, Math.round(UX.radius * .62)) + "px");
  st.setProperty("--glass-blur", (+UX.glassBlur || 0) + "px");
  st.setProperty("--bgfx-op", String(clamp01(+UX.bgop || 0)));
  /* v3.5.9: скин — читают nochnik.css (html[data-skin="nochnik"] и
     "creme") и nochnik.js (MutationObserver: собирает/разбирает каркас
     без перезагрузки страницы) */
  h.dataset.skin = UX.skin || "classic";
  h.dataset.font = UX.font;
  h.dataset.density = UX.density;
  h.dataset.bgfx = UX.bgfx;
  h.dataset.glass = UX.glass;
  h.dataset.anim = UX.anim;
  h.dataset.bubbles = UX.bubbles;
  h.dataset.avatars = UX.avatars;
  uxStyle.textContent = UX.css || "";
}

/* ───────────────────────── реестр плагинов ─────────────────────────
   Каждый плагин = карточка в панели. controls описываются декларативно:
   swatches | color | segment | range | toggle | css. */
const SWATCHES = [
  {v: "",        l: "Как в теме"},
  {v: "#ff5b3a", l: "Коралл"},
  {v: "#6366f1", l: "Индиго"},
  {v: "#2fc4cc", l: "Бирюза"},
  {v: "#d6336c", l: "Вишня"},
  {v: "#f59e0b", l: "Янтарь"},
  {v: "#10b981", l: "Изумруд"},
  {v: "#e5484d", l: "Кармин"},
  {v: "#0ea5e9", l: "Небо"},
  {v: "#a855f7", l: "Аметист"},
  {v: "#84cc16", l: "Лайм"},
  {v: "#ec4899", l: "Фламинго"},
  {v: "#14b8a6", l: "Мята"},
  {v: "#f97316", l: "Мандарин"},
  {v: "#8b5cf6", l: "Лаванда"},
];
const PLUGINS = [
  /* v3.5.9: скин — вторая парадигма (nochnik2.html): тёмный «Ночник»
     и светлая пара «Крем-брюле»; обе живут рядом с классикой */
  {id: "skin", icon: "🧩", name: "Скин",
   desc: "Классика · Ночник · Крем-брюле — переключай и сравнивай",
   controls: [
     {type: "segment", key: "skin", options: [
       {v: "classic", l: "Классика"},
       {v: "nochnik", l: "Ночник"},
       {v: "creme", l: "Крем-брюле"}]},
   ]},
  {id: "accent", icon: "🎨", name: "Акцентный цвет",
   desc: "Кнопки, ссылки, баблы «себя», кольца фокуса",
   controls: [
     {type: "swatches", key: "accent"},
     {type: "color", key: "accent", label: "Свой цвет"},
   ]},
  {id: "type", icon: "🔤", name: "Типографика",
   desc: "Шрифт интерфейса и размер текста",
   controls: [
     {type: "segment", key: "font", options: [
       {v: "system", l: "Системный"}, {v: "rounded", l: "Округлый"},
       {v: "serif", l: "Журнальный"}, {v: "mono", l: "Моно"}]},
     {type: "range", key: "fscale", min: 13, max: 18, step: .5,
      label: "Размер текста", unit: "px"},
   ]},
  {id: "shape", icon: "📐", name: "Форма и плотность",
   desc: "Скругления и «воздух» в ленте и панелях",
   controls: [
     {type: "segment", key: "density", options: [
       {v: "compact", l: "Компактно"}, {v: "normal", l: "Обычно"},
       {v: "roomy", l: "Просторно"}]},
     {type: "range", key: "radius", min: 4, max: 26, step: 1,
      label: "Скругления", unit: "px"},
   ]},
  {id: "bg", icon: "🌌", name: "Фон",
   desc: "Атмосфера за панелями — от авроры до чистого листа",
   controls: [
     {type: "segment", key: "bgfx", options: [
       {v: "aurora", l: "Аврора"}, {v: "grid", l: "Сетка"},
       {v: "dots", l: "Точки"}, {v: "plain", l: "Чистый"}]},
     {type: "range", key: "bgop", min: 0, max: 1, step: .05,
      label: "Интенсивность", unit: "%"},
   ]},
  {id: "glass", icon: "🧊", name: "Стекло",
   desc: "Размытие шапки и всплывающих панелей",
   toggle: true, key: "glass",
   controls: [
     {type: "range", key: "glassBlur", min: 0, max: 24, step: 1,
      label: "Сила размытия", unit: "px"},
   ]},
  {id: "anim", icon: "✨", name: "Анимации",
   desc: "Появление сообщений и микровзаимодействия",
   toggle: true, key: "anim",
   controls: []},
  {id: "bubbles", icon: "💬", name: "Сообщения",
   desc: "Стиль баблов в чате и ЛС, аватарки",
   controls: [
     {type: "segment", key: "bubbles", options: [
       {v: "soft", l: "Мягкие"}, {v: "flat", l: "Плоские"},
       {v: "outline", l: "Контур"}, {v: "ink", l: "Чернила"}]},
     {type: "toggle", key: "avatars", label: "Показывать аватарки"},
   ]},
  {id: "css", icon: "🖌", name: "Своя CSS",
   desc: "Правило применяется сразу и живёт только у тебя",
   controls: [
     {type: "css", key: "css"},
   ]},
];

/* пресеты (в футере панели): «Дорого» — атмосфера и стекло,
   «Спокойно» — минимум эффектов, «Сброс» — как из коробки (Studio) */
const PRESET_RICH = {accent: "", font: "system", fscale: 15,
  density: "normal", radius: 14, bgfx: "aurora", bgop: .55,
  glass: "on", glassBlur: 14, anim: "on", bubbles: "ink",
  avatars: "on", css: ""};
const PRESET_CALM = {accent: "", font: "system", fscale: 15,
  density: "normal", radius: 8, bgfx: "plain", bgop: 0,
  glass: "off", glassBlur: 0, anim: "on", bubbles: "outline",
  avatars: "on", css: ""};

/* ───────────────────────── отрисовка панели ─────────────────────────
   Панель строится из реестра один раз; контролы обновляют UX сами. */
function el(tag, cls, html){
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}

function fmtRangeVal(c, v){
  if (c.unit === "%") return Math.round(v * 100) + "%";
  const num = (c.step < 1)
    ? (Number.isInteger(+v) ? String(v) : v.toFixed(1))
    : String(Math.round(v));
  return num + (c.unit ? " " + c.unit : "");
}

function makeSwitch(getter, setter){
  const lab = el("label", "uxswitch");
  const inp = document.createElement("input");
  inp.type = "checkbox"; inp.checked = getter() === "on";
  const knob = el("i");
  lab.appendChild(inp); lab.appendChild(knob);
  inp.addEventListener("change", () => setter(inp.checked));
  return lab;
}

function renderControl(c){
  if (c.type === "swatches"){
    const box = el("div", "uxswatches");
    for (const s of SWATCHES){
      const b = el("button", "uxswatch");
      b.type = "button";
      b.title = s.l;
      b.setAttribute("aria-label", "Акцент: " + s.l);
      if (s.v){
        const d = accentDerived(s.v);
        b.style.background = d
          ? "linear-gradient(135deg," + s.v + " 0%," + d["--accent2"] + " 100%)"
          : s.v;
      } else {
        /* «Как в теме» — радужный свотч: вернуть цвета текущей темы */
        b.style.background =
          "linear-gradient(135deg,#6366f1,#a855f7,#2fc4cc,#d6336c)";
      }
      if ((UX[c.key] || "") === s.v) b.classList.add("act");
      b.addEventListener("click", () => {
        UX[c.key] = s.v; save(); apply(); syncSwatches(c.key);
      });
      box.appendChild(b);
    }
    return box;
  }
  if (c.type === "color"){
    const row = el("div", "uxcolor");
    row.appendChild(el("span", "lbl", c.label || "Свой цвет"));
    const inp = document.createElement("input");
    inp.type = "color";
    inp.value = UX[c.key] || "#6366f1";
    inp.addEventListener("input", () => {
      UX[c.key] = inp.value; save(); apply(); syncSwatches(c.key);
    });
    row.appendChild(inp);
    return row;
  }
  if (c.type === "segment"){
    const seg = el("div", "uxseg");
    for (const o of c.options){
      const b = el("button", UX[c.key] === o.v ? "act" : "");
      b.type = "button";
      b.textContent = o.l;
      b.addEventListener("click", () => {
        UX[c.key] = o.v; save(); apply();
        seg.querySelectorAll("button").forEach(x =>
          x.classList.toggle("act", x === b));
      });
      seg.appendChild(b);
    }
    return seg;
  }
  if (c.type === "range"){
    const wrap = el("div", "uxrange");
    const row = el("div", "uxrow");
    row.appendChild(el("span", "lbl", c.label || ""));
    const out = el("output", "", fmtRangeVal(c, +UX[c.key]));
    row.appendChild(out);
    const inp = document.createElement("input");
    inp.type = "range"; inp.className = "uxr";
    inp.min = c.min; inp.max = c.max; inp.step = c.step;
    inp.value = String(+UX[c.key]);
    inp.addEventListener("input", () => {
      const v = parseFloat(inp.value);
      UX[c.key] = v; out.textContent = fmtRangeVal(c, v);
      apply(); save();
    });
    wrap.appendChild(row); wrap.appendChild(inp);
    return wrap;
  }
  if (c.type === "toggle"){
    const onV = c.on || "on", offV = c.off || "off";
    const row = el("div", "uxrow");
    row.appendChild(el("span", "lbl", c.label || ""));
    row.appendChild(makeSwitch(
      () => UX[c.key] === onV, (on) => {
        UX[c.key] = on ? onV : offV; save(); apply();
      }));
    return row;
  }
  if (c.type === "css"){
    const ta = document.createElement("textarea");
    ta.className = "uxcss";
    ta.spellcheck = false;
    ta.placeholder = "/* например:\n.msg{font-style:italic}\n.tabs{letter-spacing:2px} */";
    ta.value = UX[c.key] || "";
    ta.addEventListener("input", () => {
      UX[c.key] = ta.value; apply(); save();
    });
    return ta;
  }
  return el("div");
}

/* подсветка выбранного свотча (акцент мог прийти и из color-пикера;
   кнопки рисуются в порядке SWATCHES — сверяемся по индексу) */
function syncSwatches(key){
  document.querySelectorAll(".uxswatches").forEach((box) => {
    const btns = box.querySelectorAll(".uxswatch");
    SWATCHES.forEach((s, i) => {
      if (btns[i]) btns[i].classList.toggle("act", (UX[key] || "") === s.v);
    });
  });
}

function buildDrawer(){
  const body = $("uxBody");
  body.innerHTML = "";
  for (const p of PLUGINS){
    const card = el("section", "uxcard");
    card.dataset.plugin = p.id;
    const head = el("div", "uxcard-head");
    head.appendChild(el("span", "uxcard-ico", p.icon));
    head.appendChild(el("span", "uxcard-tt",
      "<b>" + p.name + "</b><small>" + p.desc + "</small>"));
    if (p.toggle){
      head.appendChild(makeSwitch(
        () => UX[p.key], (on) => {
          UX[p.key] = on ? "on" : "off"; save(); apply(); syncCard(p.id);
        }));
    }
    card.appendChild(head);
    if (p.controls && p.controls.length){
      const bd = el("div", "uxcard-body");
      for (const c of p.controls) bd.appendChild(renderControl(c));
      card.appendChild(bd);
      if (p.toggle) syncCard(p.id);   /* пригасить, если выключен */
    }
    body.appendChild(card);
  }
}
/* карточка выключенного плагина становится полупрозрачной */
function syncCard(id){
  const card = document.querySelector('.uxcard[data-plugin="' + id + '"]');
  if (!card) return;
  const p = PLUGINS.find((x) => x.id === id);
  if (!p || !p.toggle) return;
  const bd = card.querySelector(".uxcard-body");
  if (bd) bd.classList.toggle("uxoff", UX[p.key] !== "on");
}

/* ───────────────────────── пресеты / экспорт ───────────────────────── */
function setPreset(vals, msg){
  /* v3.5.8: скин — каркас, а не косметика: «Дорого»/«Спокойно»/«Сброс»
     перекрашивают и настраивают ТЕКУЩИЙ скин, но не выключают его
     (иначе пресет внезапно уводил бы из Studio 4 в классику) */
  UX = Object.assign({}, DEFAULTS, vals, {skin: UX.skin || "classic"});
  save(); apply(); buildDrawer();
  toast(msg);
}
function exportSettings(){
  const json = JSON.stringify(UX, null, 2);
  const done = () => toast("Настройки скопированы — вставь их на другом устройстве");
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(json).then(done, () => showImport(json));
  } else showImport(json);
}
function showImport(pre){
  /* v3.2: свой многострочный диалог вместо системного window.prompt —
     перенос настроек между устройствами теперь в теме приложения.
     Fallback на prompt остался для окружений без DOM-модалок
     (scripts/smoke_appearance.js гоняет этот файл в Node-стабе). */
  const doApply = (v) => {
    if (v == null || !v.trim()) return;
    try {
      UX = Object.assign({}, DEFAULTS, JSON.parse(v));
      save(); apply(); buildDrawer();
      toast("Настройки применены");
    } catch(e){ toast("Не понял JSON — проверь текст", "err"); }
  };
  if (typeof frPrompt === "function"){
    frPrompt({
      title: pre ? "Твои настройки (JSON)" : "Вставь настройки (JSON)",
      value: pre || "",
      placeholder: '{ "accent": "#ff5b3a", "radius": 10 }',
      multiline: true, rows: 7,
      ok: "Применить",
    }).then(doApply);
    return;
  }
  const v = window.prompt("Настройки оформления (JSON).\n" +
    (pre ? "Скопируй текст ниже:\n" : "Вставь JSON и нажми ОК:") || "", pre || "");
  doApply(v);
}

/* ───────────────────────── панель: открытие/закрытие ─────────────── */
function openUx(){
  $("uxDrawer").classList.remove("hidden");
  $("uxOverlay").classList.remove("hidden");
}
function closeUx(){
  $("uxDrawer").classList.add("hidden");
  $("uxOverlay").classList.add("hidden");
}

/* ───────────────────────── инициализация ───────────────────────── */
apply();
buildDrawer();

$("btnUx").onclick = openUx;
$("btnUxClose").onclick = closeUx;
$("uxOverlay").onclick = closeUx;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("uxDrawer").classList.contains("hidden"))
    closeUx();
});
$("uxPresetRich").onclick = () => setPreset(PRESET_RICH,
  "Пресет «Дорого»: аврора, стекло, мягкие баблы");
$("uxPresetCalm").onclick = () => setPreset(PRESET_CALM,
  "Пресет «Спокойно»: минимум эффектов");
$("uxPresetReset").onclick = () => setPreset({}, "Оформление сброшено");
$("uxExport").onclick = exportSettings;
$("uxImport").onclick = () => showImport("");

})();
