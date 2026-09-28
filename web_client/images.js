"use strict";
/* images.js — скриншоты и картинки в чате веб-клиента (v3.6.8).

   ЧТО УМЕЕТ:
   1. Загрузка скриншота/картинки прямо из чата:
      - кнопка со скрепкой в композере (выбор файла, можно несколько);
      - Ctrl+V — скриншот из буфера (Win+Shift+S → Ctrl+V, как все привыкли).
      Файл уходит на /send_file (тот же механизм, что у «Файлов»), в ленту
      приезжает обычное file-событие — его видят и веб, и Qt-клиент.
   2. Превью в ленте: file-события с именем-картинкой (png/jpg/gif/webp/
      bmp/svg) рисуются уменьшенной картинкой в бабле. <img> не умеет
      слать заголовки (X-Relay-Key нужен при включённом ключе доступа),
      поэтому картинка тянется fetch'ем с baseHeaders() и отдаётся как
      objectURL. objectURL кешируется по file_id: лента перерисовывается
      (диффы канала, поиск, подгрузка истории) — одна и та же картинка
      не перекачивается на каждую перерисовку.
   3. Лайтбокс поверх чата: клик по превью открывает полноэкранный
      просмотр. Зум — клик по картинке (1x ↔ 3x в точке клика), колесо
      мыши (1x–8x с якорем под курсором), кнопки +/−; пан — перетаскивание
      при увеличении. Закрытие: Esc, крестик, клик по фону.
   4. Темы: весь хром на CSS-переменных (--lb-backdrop и прочие токены
      style.css), светлые темы и скины получают свой цвет фона оверлея.

   Безопасность: имя файла рисуется textContent (не innerHTML), URL
   картинки — наш собственный objectURL, внешний src не вводится. */

/* ─────────── распознавание картинок по имени ─────────── */
const IMG_EXT_RE = /\.(png|jpe?g|gif|webp|bmp|svg)$/i;
function isImageName(name){
  return IMG_EXT_RE.test(String(name || ""));
}

/* ─────────── кеш objectURL превью ─────────── */
const IMG_URLS = new Map();     // ключ "fid" / "dm:fid" -> objectURL | "" (ошибка)
const IMG_INFLIGHT = new Map(); // ключ -> true (уже качаем)
const IMG_URL_CAP = 80;         // вытеснение по порядку вставки

function imgUrlKey(fid, dm){
  return (dm ? "dm:" : "") + fid;
}
function imgUrlCachePut(key, url){
  if (IMG_URLS.size >= IMG_URL_CAP){
    const first = IMG_URLS.keys().next().value;
    const old = IMG_URLS.get(first);
    if (old){ try { URL.revokeObjectURL(old); } catch(e){} }
    IMG_URLS.delete(first);
  }
  IMG_URLS.set(key, url);
}
function imgDownloadPath(fid, dm){
  return (dm ? "/dm_download/" : "/download/") + encodeURIComponent(fid);
}
/* Тянет картинку в кеш. Возвращает готовый objectURL, "" (ранее не
   скачалась) или null (качается сейчас). */
function imgLoadUrl(fid, dm){
  const key = imgUrlKey(fid, dm);
  if (IMG_URLS.has(key)){
    const v = IMG_URLS.get(key);
    if (v) return v;
    return "";              /* уже пробовали и не вышло — не долбим сервер */
  }
  if (IMG_INFLIGHT.has(key)) return null;
  IMG_INFLIGHT.set(key, true);
  fetch(imgDownloadPath(fid, dm), {headers: baseHeaders()})
    .then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.blob();
    })
    .then((blob) => {
      const url = URL.createObjectURL(blob);
      imgUrlCachePut(key, url);
      IMG_INFLIGHT.delete(key);
      document.querySelectorAll('img[data-ikey="' + key + '"]').forEach((el) => {
        el.src = url;
        el.classList.remove("img-wait");
        el.classList.add("img-ready");
      });
    })
    .catch(() => {
      IMG_URLS.set(key, "");          /* ошибка — не долбим сервер заново */
      IMG_INFLIGHT.delete(key);
      document.querySelectorAll('img[data-ikey="' + key + '"]').forEach((el) => {
        el.classList.remove("img-wait");
        el.classList.add("img-fail");
      });
    });
  return null;
}

/* ─────────── превью в бабле ───────────
   txt — контейнер .txt; f — {file_id, name, size, sha256}; dm — из ЛС.
   Превью вставляется НАД ссылкой-скачиванием, как в мессенджерах. */
function attachImagePreview(txt, f, dm){
  if (!txt || !f || !f.file_id || !isImageName(f.name)) return;
  const wrap = document.createElement("div");
  wrap.className = "imgprev";
  const img = document.createElement("img");
  img.alt = f.name;
  img.loading = "lazy";
  img.draggable = false;
  img.decoding = "async";
  img.title = "Нажми, чтобы приблизить";
  const key = imgUrlKey(f.file_id, dm);
  img.dataset.ikey = key;
  const cached = imgLoadUrl(f.file_id, dm);
  if (cached){
    img.src = cached;
    img.classList.add("img-ready");
  } else if (cached === ""){
    img.classList.add("img-fail");
  } else {
    img.classList.add("img-wait");
  }
  img.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    const url = IMG_URLS.get(key);
    if (!url) return;                 /* ещё качается / не скачалась */
    openImageLightbox({
      name: f.name, url: url,
      fileId: f.file_id, dm: !!dm, fileRef: f,
    });
  };
  wrap.appendChild(img);
  txt.insertBefore(wrap, txt.firstChild);
}

/* ─────────── загрузка скриншота из чата ─────────── */
async function uploadChatImages(files){
  if (!files || !files.length) return;
  if (!S.token){
    warn("chatWarn", "Сначала подключись к серверу");
    return;
  }
  const imgs = [...files].filter((f) =>
    (f.type || "").startsWith("image/") || isImageName(f.name));
  if (!imgs.length){
    warn("chatWarn", "Это не картинка — нужен скриншот или файл изображения");
    return;
  }
  for (const f of imgs) await uploadChatImage(f);
}

async function uploadChatImage(file){
  warn("chatWarn", "");
  try {
    /* имя из буфера обмена Chromium — "image.png"; если пусто — соберём */
    let name = file.name || "";
    if (!name || name === "image"){
      name = "скриншот " +
        new Date().toLocaleString("ru-RU",
          {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"}) +
        ".png";
    }
    const buf = await file.arrayBuffer();
    const sha = await sha256OfBuffer(buf);   /* forge на http, subtle на https */
    const upHeaders = baseHeaders({
      "X-Relay-Filename": headerEncode(name),
      "X-Relay-SHA256": sha,
      "Content-Type": "application/octet-stream",
    });
    if (S.token){
      try {
        upHeaders["X-Relay-Auth"] = S.token + ":" +
          await hmacHexBin(S.token, binStrOfBuffer(buf));
      } catch(e){}
    }
    const r = await fetch("/send_file", {
      method: "POST", headers: upHeaders, body: buf,
    });
    const json = await safeJson(r) || {ok: false, error: "HTTP " + r.status};
    if (json.ok){
      toast("Скриншот отправлен: " + name);
      /* событие file приедет в ленту ближайшим pollTick — дергаем сразу */
      await pollTick();
    } else {
      warn("chatWarn", "не залилось: " + (json.error || r.status));
    }
  } catch(e){
    warn("chatWarn", "не залилось: " + e);
  }
}

/* Ctrl+V со скриншотом в буфере — грузим сразу, когда открыт чат.
   Текстовые вставки и вставки НЕ на вкладке чата не трогаем. */
document.addEventListener("paste", (e) => {
  if (!S.token) return;
  const chatTab = $("tab-chat");
  if (!chatTab || chatTab.classList.contains("hidden")) return;
  const files = e.clipboardData && e.clipboardData.files;
  if (!files || !files.length) return;
  const imgs = [...files].filter((f) =>
    (f.type || "").startsWith("image/") || isImageName(f.name));
  if (!imgs.length) return;
  e.preventDefault();
  uploadChatImages(imgs);
});

/* кнопка в композере чата */
(function wireComposerImageButton(){
  const btn = $("btnImg"), inp = $("imgInput");
  if (!btn || !inp) return;
  btn.onclick = () => inp.click();
  inp.onchange = () => {
    if (inp.files && inp.files.length) uploadChatImages(inp.files);
    inp.value = "";               /* тот же файл можно выбрать повторно */
  };
})();

/* ─────────── лайтбокс ─────────── */
let LB = null;   // состояние открытого лайтбокса

function lbApply(){
  if (!LB) return;
  LB.img.style.transform =
    "translate(" + LB.tx + "px," + LB.ty + "px) scale(" + LB.scale + ")";
  if (LB.zoomLbl) LB.zoomLbl.textContent = Math.round(LB.scale * 100) + "%";
  LB.ov.classList.toggle("zoomed", LB.scale > 1.01);
}
function lbSetScale(scale, ax, ay){
  /* ax, ay — точка-якорь в координатах stage (клиентские); при их
     отсутствии масштабируем вокруг центра. */
  if (!LB) return;
  const s = Math.min(8, Math.max(1, scale));
  if (s <= 1.01){
    LB.scale = 1; LB.tx = 0; LB.ty = 0;
    LB.img.style.transformOrigin = "50% 50%";
    lbApply();
    return;
  }
  if (ax !== undefined && ax !== null){
    /* переносим якорь в точку курсора: картинка не «уплывает» */
    const r = LB.img.getBoundingClientRect();
    const ox = Math.min(Math.max(ax - r.left, 0.1), r.width - 0.1);
    const oy = Math.min(Math.max(ay - r.top, 0.1), r.height - 0.1);
    LB.img.style.transformOrigin =
      (ox / r.width * 100).toFixed(2) + "% " +
      (oy / r.height * 100).toFixed(2) + "%";
  }
  LB.scale = s;
  lbApply();
}
function closeImageLightbox(){
  if (LB.__esc) document.removeEventListener("keydown", LB.__esc);
  /* ревокаем только СОБСТВЕННЫЙ blob (лайтбокс мог открыть картинку,
     которой нет в кеше превью); кешевый ревочит imgUrlCachePut */
  const cached = LB.fileId
    ? IMG_URLS.get(imgUrlKey(LB.fileId, LB.dm)) : LB.__url;
  if (LB.__url && LB.__url !== cached){
    try { URL.revokeObjectURL(LB.__url); } catch(e){}
  }
  LB.ov.remove();
  document.body.classList.remove("lb-open");
  LB = null;
}
function openImageLightbox(o){
  if (!o || !o.url) return;
  if (LB) closeImageLightbox();

  const ov = document.createElement("div");
  ov.className = "lightbox";
  ov.setAttribute("role", "dialog");
  ov.setAttribute("aria-modal", "true");
  ov.setAttribute("aria-label", "Просмотр изображения");

  const bar = document.createElement("div");
  bar.className = "lb-bar";
  const nm = document.createElement("span");
  nm.className = "lb-name";
  nm.textContent = o.name || "изображение";    /* textContent: не innerHTML */
  nm.title = nm.textContent;
  const zoomLbl = document.createElement("span");
  zoomLbl.className = "lb-zoom";
  zoomLbl.textContent = "100%";
  const mkBtn = (icon, title) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sec";
    b.title = title;
    b.setAttribute("aria-label", title);
    b.appendChild(frIcon(icon));
    return b;
  };
  const btnOut = mkBtn("zoom-out", "Уменьшить (колесо мыши)");
  const btnIn = mkBtn("zoom-in", "Увеличить (или клик по картинке)");
  const btnDl = mkBtn("download", "Скачать оригинал");
  const btnX = mkBtn("x", "Закрыть (Esc)");
  btnX.classList.add("lb-x");
  btnOut.onclick = (e) => { e.stopPropagation(); lbSetScale(LB.scale / 1.5); };
  btnIn.onclick = (e) => { e.stopPropagation(); lbSetScale(LB.scale * 1.5); };
  btnDl.onclick = (e) => {
    e.stopPropagation();
    try {
      if (o.dm && typeof dmDownloadFile === "function") dmDownloadFile(o.fileRef);
      else if (typeof downloadFile === "function") downloadFile(o.fileRef);
    } catch(err){}
  };
  btnX.onclick = (e) => { e.stopPropagation(); closeImageLightbox(); };
  bar.appendChild(nm); bar.appendChild(zoomLbl);
  bar.appendChild(btnOut); bar.appendChild(btnIn); bar.appendChild(btnDl);
  bar.appendChild(btnX);

  const stage = document.createElement("div");
  stage.className = "lb-stage";
  const img = document.createElement("img");
  img.alt = o.name || "";
  img.draggable = false;
  img.decoding = "async";
  img.src = o.url;
  stage.appendChild(img);
  ov.appendChild(stage); ov.appendChild(bar);
  document.body.appendChild(ov);
  document.body.classList.add("lb-open");

  LB = {
    ov, stage, img, zoomLbl, scale: 1, tx: 0, ty: 0,
    fileId: o.file_id || o.fileId || "", dm: !!o.dm, __url: o.url,
  };
  lbApply();

  /* Esc — закрыть (поверх всех прочих Esc-хендлеров приложения) */
  LB.__esc = (e) => {
    if (e.key === "Escape"){
      e.preventDefault();
      e.stopPropagation();
      closeImageLightbox();
    }
  };
  document.addEventListener("keydown", LB.__esc);

  /* клик по картинке — зум 1x ↔ 3x в точке клика */
  img.addEventListener("click", (e) => {
    e.stopPropagation();
    if (LB.scale > 1.01) lbSetScale(1);
    else lbSetScale(3, e.clientX, e.clientY);
  });

  /* колесо — плавный зум с якорем под курсором */
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const k = Math.exp(-e.deltaY * 0.0022);
    lbSetScale(LB.scale * k, e.clientX, e.clientY);
  }, {passive: false});

  /* клик по фону (stage, не по картинке) — закрыть */
  stage.addEventListener("click", (e) => {
    if (e.target === stage) closeImageLightbox();
  });

  /* перетаскивание при увеличении — пан */
  let dragging = false, px = 0, py = 0;
  img.addEventListener("pointerdown", (e) => {
    if (LB.scale <= 1.01) return;
    dragging = true; px = e.clientX; py = e.clientY;
    try { img.setPointerCapture(e.pointerId); } catch(err){}
    e.preventDefault();
  });
  img.addEventListener("pointermove", (e) => {
    if (!dragging || !LB) return;
    LB.tx += e.clientX - px;
    LB.ty += e.clientY - py;
    px = e.clientX; py = e.clientY;
    lbApply();
  });
  const stopDrag = () => { dragging = false; };
  img.addEventListener("pointerup", stopDrag);
  img.addEventListener("pointercancel", stopDrag);
}

/* страница уходит — отдаём objectURL кеша браузеру */
window.addEventListener("pagehide", () => {
  for (const [, url] of IMG_URLS){
    if (url){ try { URL.revokeObjectURL(url); } catch(e){} }
  }
  IMG_URLS.clear();
  if (LB) closeImageLightbox();
});
