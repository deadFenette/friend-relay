"use strict";
/* modal.js — v3.2 «Studio Social»: нативные prompt()/confirm() → свои модалки.

   ЗАЧЕМ: браузерные диалоги — главный «дешёвый» tell интерфейса: они
   рвут визуальный контекст, не темизируются и выглядят тревожно.
   Теперь: аккуратная карточка по центру, тема/акцент/скругления
   наследуются, Esc/Enter работают как в нативных, фокус сразу в поле.

   API (глобальные, как toast()):
     frConfirm({title, body, ok, cancel, danger}) -> Promise<boolean>
     frPrompt ({title, body, value, placeholder, multiline,
                ok, cancel, maxlength})        -> Promise<string|null>
     frCloseModal()                               — закрыть принудительно

   Совместимость: ноль зависимостей от других модулей (только DOM),
   рисуется лениво при первом вызове. Если модалка уже открыта —
   новый вызов заменяет старую (старый promise резолвится отменой). */

(function(){

let activeResolve = null;   /* резолвер текущего диалога */
let active = null;         /* корневой элемент */

function close(result){
  if (active){
    const el = active;
    active = null;
    /* мягкий уход: анимация 120мс, потом remove */
    el.classList.add("closing");
    setTimeout(() => el.remove(), 130);
  }
  if (activeResolve){
    const r = activeResolve;
    activeResolve = null;
    r(result);
  }
}
function frCloseModal(){ close(null); }

/* ── сборка DOM ──
   Разметка минимальна, всё в style.css (.frmodal-*).
   role="dialog" + aria-modal для скринридеров, autofocus на поле. */
function build(opts, kind){
  const o = opts || {};
  const wrap = document.createElement("div");
  wrap.className = "frmodal-wrap";
  wrap.setAttribute("role", "dialog");
  wrap.setAttribute("aria-modal", "true");
  wrap.setAttribute("aria-label", o.title || "");

  const card = document.createElement("div");
  card.className = "frmodal" + (o.danger ? " danger" : "");
  wrap.appendChild(card);

  const tt = document.createElement("div");
  tt.className = "frmodal-tt";
  tt.textContent = o.title || "";
  card.appendChild(tt);

  let field = null;
  if (kind === "prompt"){
    if (o.body){
      const b = document.createElement("div");
      b.className = "frmodal-body";
      b.textContent = o.body;
      card.appendChild(b);
    }
    field = document.createElement(o.multiline ? "textarea" : "input");
    field.className = "frmodal-field";
    if (!o.multiline){
      field.type = "text";
      field.spellcheck = false;
    } else {
      field.rows = o.rows || 5;
      field.spellcheck = false;
    }
    if (o.placeholder) field.placeholder = o.placeholder;
    if (o.value != null) field.value = o.value;
    if (o.maxlength) field.maxLength = o.maxlength;
    card.appendChild(field);
  } else if (o.body){
    const b = document.createElement("div");
    b.className = "frmodal-body";
    b.textContent = o.body;
    card.appendChild(b);
  }

  const row = document.createElement("div");
  row.className = "frmodal-row";
  const bc = document.createElement("button");
  bc.type = "button";
  bc.className = "sec";
  bc.textContent = o.cancel || "Отмена";
  const bo = document.createElement("button");
  bo.type = "button";
  bo.className = "frmodal-ok";
  bo.textContent = o.ok || (kind === "prompt" ? "ОК" : "Да");
  row.appendChild(bc);
  row.appendChild(bo);
  card.appendChild(row);

  /* ── поведение ──
     Клик по фону = отмена (как в Figma/Linear), Esc = отмена,
     Enter в поле = подтвердить (в textarea — Ctrl+Enter, Enter — перенос). */
  wrap.addEventListener("mousedown", (e) => {
    if (e.target === wrap) close(kind === "prompt" ? null : false);
  });
  bc.onclick = () => close(kind === "prompt" ? null : false);
  bo.onclick = () => {
    if (kind === "prompt"){
      close(field.value);
    } else close(true);
  };
  card.addEventListener("keydown", (e) => {
    if (e.key === "Escape"){
      e.preventDefault(); e.stopPropagation();
      close(kind === "prompt" ? null : false);
    } else if (e.key === "Enter"){
      if (kind === "confirm"){ e.preventDefault(); close(true); }
      else if (e.target === field && !o.multiline){
        e.preventDefault(); close(field.value);
      } else if (kind === "prompt" && o.multiline && (e.ctrlKey || e.metaKey)){
        e.preventDefault(); close(field.value);
      }
    }
  });

  document.body.appendChild(wrap);
  requestAnimationFrame(() => wrap.classList.add("show"));
  const focusEl = (kind === "prompt") ? field : bo;
  if (focusEl) setTimeout(() => {
    focusEl.focus();
    if (kind === "prompt" && !o.multiline) focusEl.select();
  }, 30);
  return wrap;
}

/* ── публичный API: возвращают Promise, как нативные диалоги ──
   Использование:
     const ok = await frConfirm({title:"Удалить канал?",
       body:"Сообщения пропадут для всех.", danger:true, ok:"Удалить"});
     const name = await frPrompt({title:"Имя канала", value:""}); */
function frConfirm(opts){
  close(null);   /* предыдущий диалог, если был — отменяем */
  active = build(opts, "confirm");
  return new Promise((res) => { activeResolve = res; });
}
function frPrompt(opts){
  close(null);
  active = build(opts, "prompt");
  return new Promise((res) => { activeResolve = res; });
}

/* глобальный Esc (фокус может быть на body) */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && active) close(null);
});

window.frConfirm = frConfirm;
window.frPrompt = frPrompt;
window.frCloseModal = frCloseModal;

})();
