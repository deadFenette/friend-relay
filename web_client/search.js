"use strict";
/* search.js — поиск по ВСЕЙ истории чата (v3.8.0).

   ЧТО УМЕЕТ:
   1. Панель «Поиск по истории» в шапке чата (рядом с 📌-панелью):
      запрос (минимум 2 символа) + необязательный фильтр по автору.
      Сервер ищет GET'ом /search по журналу ОБЩЕГО чата (весь history.jsonl,
      включая давно вытесненное из ленты) И по всем каналам. Регистр не
      важен, правки учтены (ищется актуальный текст), удалённое не находится.
   2. Прыжок к найденному: если бабл уже в ленте — подсветка (jumpToSeq);
      если глубже — докачиваем контекст (/events?before=seq+1) и вставляем
      над лентой тем же механизмом, что и подгрузка скроллом, затем прыжок.
      Для сообщений канала канал открывается сам; если сообщение глубже
      канального окна — честно сообщаем (каналы не умеют before).
   3. Подсветка совпадений в сниппете (DOM-спанами, никакого innerHTML).

   Честные ограничения (написаны и в подсказке панели):
   - зашифрованные сообщения не ищутся — сервер не может их прочитать (E2E);
   - ЛС не ищутся: они приватны и хранятся попарно, поиск по ним — отдельная
     задача из бэклога.

   Безопасность: весь текст результатов — только textContent; URL нет. */

/* ── панель ── */
function toggleHistPanel(show){
  const panel = $("histPanel");
  if (!panel) return;
  const want = (typeof show === "boolean") ? show
    : panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !want);
  if (want){
    $("histQ").focus();
    /* повторный поиск по уже набранному — чтобы панель не была пустой */
    if (($("histQ").value || "").trim().length >= 2) runHistorySearch();
  } else {
    histAbortInflight();
  }
}

/* ── сам поиск ── */
let HIST_REQ = 0;        /* счётчик запросов — устаревший ответ не рисуем */
let HIST_LAST_Q = "";    /* не долбим сервер одним и тем же запросом */

function histAbortInflight(){ HIST_REQ++; }

async function runHistorySearch(){
  const list = $("histList");
  if (!list) return;
  const q = ($("histQ").value || "").trim();
  const author = ($("histAuthor").value || "").trim();
  if (q.length < 2){
    $("histStat").textContent = "нужно минимум 2 символа";
    list.innerHTML = "";
    return;
  }
  const my = ++HIST_REQ;
  $("histStat").textContent = "ищу…";
  try {
    const {json} = await apiGet("/search?q=" + encodeURIComponent(q) +
      (author ? "&author=" + encodeURIComponent(author) : ""));
    if (my !== HIST_REQ) return;      /* приехал более свежий ответ */
    if (!json || json.ok === false){
      $("histStat").textContent = "ошибка: " + ((json && json.error) || "сервер");
      return;
    }
    HIST_LAST_Q = q.toLowerCase();
    const results = json.results || [];
    renderHistResults(results, json.total || results.length);
  } catch(e){
    if (my === HIST_REQ) $("histStat").textContent = "не ищется: " + e;
  }
}

/* ── отрисовка результатов ── */
function renderHistResults(results, total){
  const list = $("histList");
  list.innerHTML = "";
  const stat = $("histStat");
  if (!results.length){
    stat.textContent = "ничего не нашлось";
    list.innerHTML = '<div class="hint" style="padding:6px 8px">В истории ' +
      "общего чата и каналах такого нет. Зашифрованные сообщения не ищутся " +
      "(E2E), ЛС — приватны и поиском не затрагиваются.</div>";
    return;
  }
  stat.textContent = "найдено: " + results.length +
    (total > results.length ? " (показаны последние)" : "");
  for (const r of results){
    const row = document.createElement("div");
    row.className = "histrow";

    const who = document.createElement("span");
    who.className = "pwho";
    who.textContent = r.from || "?";
    who.style.color = (typeof avatarColor === "function")
      ? avatarColor(r.from) : "";

    const tm = document.createElement("span");
    tm.className = "ptm";
    tm.textContent = r.ts ? fmtTime(r.ts) : "";

    /* чип канала у результатов из каналов */
    if (r.channel){
      const ch = document.createElement("span");
      ch.className = "hchan";
      ch.textContent = "#" + r.channel;
      row.appendChild(ch);
    }

    const txt = document.createElement("span");
    txt.className = "ptxt";
    if (r.kind === "file" && r.file){
      txt.textContent = "файл: " + (r.file.name || "?");
    } else {
      histFillSnippet(txt, r.text || "", HIST_LAST_Q);
    }
    txt.title = txt.textContent;

    const jump = document.createElement("span");
    jump.className = "pjump";
    /* в общий чат прыгаем всегда (докачаем контекст), в канале — только
       если сообщение в канальном окне */
    const canJump = r.channel ? S.msgs.has(r.seq) : true;
    jump.textContent = canJump ? "→ открыть" : "глубоко в канале";
    if (canJump){
      row.onclick = () => histJumpTo(r);
    } else {
      row.classList.add("off");
      row.title = "Сообщение глубже канального окна (50) — открой канал " +
        "и листай историю";
    }

    row.appendChild(who); row.appendChild(tm);
    row.appendChild(txt); row.appendChild(jump);
    list.appendChild(row);
  }
}

/* Сниппет с подсветкой первого совпадения: три text-узла + span.hl,
   без единого innerHTML — безопасно при любом тексте. */
function histFillSnippet(el, text, ql){
  const MAX = 160;
  let t = text.replace(/\s+/g, " ").trim();
  if (t.length > MAX) t = t.slice(0, MAX) + "…";
  const lower = t.toLowerCase();
  const at = ql ? lower.indexOf(ql) : -1;
  if (at < 0){ el.textContent = t; return; }
  const pre = Math.max(0, at - 30);          /* чуть контекста слева */
  if (pre > 0) el.appendChild(document.createTextNode("…" + t.slice(pre, at)));
  const mark = document.createElement("span");
  mark.className = "hl";
  mark.textContent = t.slice(at, at + ql.length);
  el.appendChild(mark);
  el.appendChild(document.createTextNode(t.slice(at + ql.length)));
}

/* ── прыжок к найденному ── */
async function histJumpTo(r){
  toggleHistPanel(false);
  /* результат из канала: открываем канал, дождавшись его канального
     окна. Вне окна честно говорим — каналы не умеют докачку before,
     и ЧУЖОЙ (общечатовский) контекст вокруг seq вставлять нельзя. */
  if (r.channel){
    if (S.channel !== r.channel) openChannel(r.channel);
    await loadChannelMessages();      /* дождаться канального окна */
    if (S.msgs.has(r.seq)){ jumpToSeq(r.seq); return; }
    toast("сообщение глубже канального окна — откроется, когда история " +
          "канала подрастёт");
    return;
  }
  /* общий чат: вернуться из канала, если мы в нём */
  if (S.channelOpen) openChannel("");
  if (S.msgs.has(r.seq)){
    jumpToSeq(r.seq);
    return;
  }
  /* сообщение глубже ленты: докачиваем контекст ДО него (сервер умеет
     before только для общего чата) и вставляем над текущим верхом.
     (v3.8.0) ok строгим сравнением: сервер /events не шлёт ok:true
     (см. фикс в loadOlderMessages) — нестрогая проверка отбрасывала
     бы честный батч. */
  const {json} = await apiGet("/events?before=" + (r.seq + 1) + "&count=40");
  if (!json || json.ok === false || !json.events || !json.events.length){
    toast("не удалось открыть сообщение в ленте");
    return;
  }
  const feed = $("feed");
  const scrollBefore = feed.scrollHeight;
  const anchor = feed.firstChild;
  const batchEls = [];
  let targetRendered = false;
  for (const ev of json.events){
    if (ev.kind !== "text" && ev.kind !== "file") continue;
    if (S.msgs.has(ev.seq)){                   /* уже в ленте */
      if (ev.seq === r.seq) targetRendered = true;
      continue;
    }
    if (ev.kind === "text"){
      addBubble({seq: ev.seq, from: ev.from, text: displayText(ev),
        ts: ev.ts, me: ev.from === S.name, ev});
    } else {
      addBubble({seq: ev.seq, from: ev.from, ts: ev.ts, file: true,
        me: ev.from === S.name, ev,
        text: {file_id: ev.file_id, name: ev.name, size: ev.size,
          sha256: ev.sha256}});
    }
    const el = S.msgs.get(ev.seq);
    if (el && !batchEls.includes(el)) batchEls.push(el);
    if (ev.seq === r.seq) targetRendered = true;
  }
  for (const el of batchEls) feed.insertBefore(el, anchor);
  /* позиция чтения не прыгает: докручиваем ровно на добавленную высоту */
  feed.scrollTop = feed.scrollHeight - scrollBefore;
  if (targetRendered) jumpToSeq(r.seq);
  else toast("сообщение глубже истории — листай ленту скроллом вверх");
}

/* Esc закрывает панель, когда она открыта (до общих Esc-хендлеров дела
   не доходит: они проверяют свои зоны) */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const panel = $("histPanel");
  if (panel && !panel.classList.contains("hidden")) toggleHistPanel(false);
});
