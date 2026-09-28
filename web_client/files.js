"use strict";
/* files.js — файловая витрина: список, заливка, скачивание с прогрессом.

   Багфикс v1.8.2: sha256 файла считался ТОЛЬКО через crypto.subtle —
   на http://IP (не secure context) его нет, и заливка всегда падала.
   Теперь основной путь — forge (работает и на http), crypto.subtle —
   запасной для https. */

/* v1.9.7 → v3.2: иконка по расширению файла.
   Теперь возвращается ИМЯ иконки из спрайта icons.js (монолайн SVG
   вместо эмодзи) — витрина выглядит как часть системы, а не как
   папка с картинками. frIcon живёт в core.js, спрайт — в icons.js. */
function fileIconOf(name){
  const ext = (name.split(".").pop() || "").toLowerCase();
  if (["png","jpg","jpeg","gif","webp","bmp","svg"].includes(ext)) return "file-image";
  if (["mp4","mkv","avi","mov","webm"].includes(ext)) return "file-video";
  if (["mp3","wav","flac","ogg","m4a"].includes(ext)) return "file-audio";
  if (["zip","rar","7z","tar","gz"].includes(ext)) return "file-archive";
  if (["pdf"].includes(ext)) return "file-pdf";
  if (["doc","docx","odt","txt","md","rtf"].includes(ext)) return "file-doc";
  if (["exe","msi","bat"].includes(ext)) return "file-exe";
  if (["py","js","ts","html","css","json","c","cpp","java","sh"].includes(ext)) return "file-code";
  return "file-package";
}

async function loadFiles(){
  try {
    const {json} = await apiGet("/files");
    const box = $("filesList");
    box.innerHTML = "";
    const files = json.files || [];
    if (!files.length){
      box.innerHTML = '<div class="hint">Витрина пуста — залей первый файл.</div>';
      return;
    }
    for (const f of files){
      const row = document.createElement("div");
      row.className = "filerow";
      row.dataset.fid = f.file_id || "";   /* v1.9.5: ищем строку по id, не по вхождению имени */
      /* v3.2: плитка-иконка типа файла — из спрайта, монолайн */
      const ico = document.createElement("span");
      ico.className = "fico";
      ico.appendChild(frIcon(fileIconOf(f.name || "")));
      const nm = document.createElement("div");
      nm.className = "nm";
      nm.textContent = f.name || f.file_id;
      const own = document.createElement("span");
      own.className = "fown";
      own.textContent = f.from || "?";
      const sz = document.createElement("div");
      sz.className = "sz";
      sz.textContent = fmtSize(f.size);
      const btn = document.createElement("button");
      btn.className = "sec";
      btn.appendChild(frIcon("download"));
      btn.appendChild(document.createTextNode("Скачать"));
      btn.onclick = () => downloadFile(f);
      const bar = document.createElement("div");
      bar.className = "pbar hidden"; bar.style.flexBasis = "100%";
      const inner = document.createElement("div");
      bar.appendChild(inner);
      const stat = document.createElement("div");
      stat.className = "dlstat"; stat.style.flexBasis = "100%";
      row.appendChild(ico); row.appendChild(nm); row.appendChild(own);
      row.appendChild(sz); row.appendChild(btn);
      row.appendChild(bar); row.appendChild(stat);
      box.appendChild(row);
    }
  } catch(e){
    $("filesList").innerHTML = '<div class="warn">не удалось получить список</div>';
  }
}

/* Скачивание с прогрессом и скоростью (format как в lib/formatters) */
async function downloadFile(f){
  if (!f || !f.file_id) return;
  const rows = [...document.querySelectorAll("#filesList .filerow")];
  /* v1.9.5: по file_id (уникален). Поиск по вхождению имени путал строки
     при похожих именах («a.txt» vs «новый a.txt») и дубликатах. */
  const row = rows.find(r => r.dataset.fid && r.dataset.fid === f.file_id)
    || rows.find(r => r.querySelector(".nm").textContent.includes(f.name));
  const bar = row ? row.querySelector(".pbar") : null;
  const inner = bar ? bar.firstChild : null;
  const stat = row ? row.querySelector(".dlstat") : null;
  try {
    if (bar) bar.classList.remove("hidden");
    const r = await fetch("/download/" + f.file_id, {headers: baseHeaders()});
    if (!r.ok){ if (stat) stat.textContent = "ошибка " + r.status; return; }
    const total = +(r.headers.get("Content-Length") || f.size || 0);
    const reader = r.body.getReader();
    const chunks = []; let got = 0;
    const win = []; /* скользящее окно скорости: [t, bytes] */
    while (true){
      const {done, value} = await reader.read();
      if (done) break;
      chunks.push(value); got += value.length;
      const now = performance.now();
      win.push([now, value.length]);
      while (win.length && now - win[0][0] > 2000) win.shift();
      const span = Math.max(win[0] ? (now - win[0][0]) : 1, 1) / 1000;
      const sum = win.reduce((a, w) => a + w[1], 0);
      const speed = sum / Math.max(span, 0.001);
      const pct = total ? Math.floor(got / total * 100) : 0;
      if (inner) inner.style.width = pct + "%";
      if (stat) stat.textContent = pct + "% · " + fmtSize(speed) + "/с · "
        + fmtSize(got) + (total ? "/" + fmtSize(total) : "");
    }
    const blob = new Blob(chunks);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = f.name || f.file_id;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    if (stat) stat.textContent = "готово: " + fmtSize(got);
  } catch(e){
    if (stat) stat.textContent = "ошибка: " + e;
  } finally { if (bar) setTimeout(() => bar.classList.add("hidden"), 1500); }
}

async function uploadFile(){
  const file = $("fileInput").files[0];
  if (!file){ $("upStat").textContent = "выбери файл"; return; }
  $("upStat").textContent = "считаю sha256…";
  try {
    const buf = await file.arrayBuffer();
    const sha = await sha256OfBuffer(buf);   /* forge на http, subtle на https */
    $("upStat").textContent = "заливаю " + fmtSize(file.size) + "…";
    /* /send_file: тело — сырые байты файла, имя и хеш в заголовках
       (см. lib/server/http_api.py _post_send_file).
       v1.9.5: X-Relay-Auth = HMAC(session_token, байты файла) — сервер
       проверяет подпись инкрементально; без неё при включённом ключе
       доступа загрузка теперь отвергается (защита от подмены отправителя). */
    const upHeaders = baseHeaders({
      "X-Relay-Filename": headerEncode(file.name || "файл"),
      "X-Relay-SHA256": sha,
      "Content-Type": "application/octet-stream",
    });
    if (S.token){
      try {
        upHeaders["X-Relay-Auth"] = S.token + ":" + await hmacHexBin(S.token, binStrOfBuffer(buf));
      } catch(e){}
    }
    const r = await fetch("/send_file", {
      method: "POST",
      headers: upHeaders,
      body: buf,
    });
    const json = await safeJson(r) || {ok: false, error: "HTTP " + r.status};
    if (json.ok){
      $("upStat").textContent = "залито: " + (json.file_id || "").slice(0, 8) + "…";
      $("fileInput").value = "";
      await loadFiles(); await pollTick();
    } else {
      $("upStat").textContent = "ошибка: " + (json.error || r.status);
    }
  } catch(e){ $("upStat").textContent = "ошибка: " + e; }
}
