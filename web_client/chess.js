"use strict";
/* chess.js — вкладка шахмат: PVP через /bot_command (host → код → join),
   судья — ChessBot на сервере.

   v2.0.3: таблица лидеров (GET /leaderboard — топ ELO от ChessBot,
   сервер сам знает, есть ли бот; без него — пустой список).
   Загружается при открытии вкладки и по кнопке ↻, поллить её каждый
   тик незачем: ELO меняется только по завершении партии. */

var CH = {match: null, sel: -1, myColor: null, busy: false, lbBusy: false};
const GLYPH = {k:"\u265A",q:"\u265B",r:"\u265C",b:"\u265D",n:"\u265E",p:"\u265F"};
const idxOf = fr => (8 - +fr[1]) * 8 + "abcdefgh".indexOf(fr[0]);
const nameOf = i => "abcdefgh"[i % 8] + (8 - Math.floor(i / 8));

function startBoard(){
  const b = Array(64).fill(null);
  const back = ["r","n","b","q","k","b","n","r"];
  for (let f = 0; f < 8; f++){
    b[8 + f]  = {t:"p", w:false};   // чёрные пешки a7..h7
    b[48 + f] = {t:"p", w:true};    // белые пешки a2..h2
    b[f]      = {t:back[f], w:false};
    b[56 + f] = {t:back[f], w:true};
  }
  return b;
}
function applyUci(b, uci){
  const from = idxOf(uci.slice(0,2)), to = idxOf(uci.slice(2,4));
  const promo = uci[4], pc = b[from];
  if (!pc) return;
  const ff = from % 8, tf = to % 8;
  const fr = 8 - Math.floor(from / 8);
  /* взятие на проходе: пешка ушла по диагонали на пустую клетку */
  if (pc.t === "p" && ff !== tf && !b[to]) b[(8 - fr) * 8 + tf] = null;
  b[to] = promo ? {t:promo, w:pc.w} : pc;
  b[from] = null;
  /* рокировка: король сдвинулся на 2 файла — переставляем ладью */
  if (pc.t === "k" && Math.abs(tf - ff) === 2){
    const row = pc.w ? 7 : 0;
    if (tf === 6){ b[row*8+5] = b[row*8+7]; b[row*8+7] = null; }
    else { b[row*8+3] = b[row*8+0]; b[row*8+0] = null; }
  }
}
function replay(moves){
  const b = startBoard();
  for (const u of moves || []) applyUci(b, u);
  return b;
}
/* черновые подсказки (без фильтра шаха — финальный судья сервер) */
function pseudoMoves(b, i){
  const pc = b[i]; if (!pc) return [];
  const f = i % 8, r = 8 - Math.floor(i / 8), out = [];
  const push = (ff, rr) => {
    if (ff < 0 || ff > 7 || rr < 1 || rr > 8) return;
    const j = (8 - rr) * 8 + ff, t = b[j];
    if (!t || t.w !== pc.w) out.push(j);
  };
  const ray = (df, dr) => { let ff = f, rr = r;
    while (true){ ff += df; rr += dr;
      if (ff < 0 || ff > 7 || rr < 1 || rr > 8) break;
      const j = (8 - rr) * 8 + ff, t = b[j];
      if (!t){ out.push(j); continue; }
      if (t.w !== pc.w) out.push(j);
      break; } };
  if (pc.t === "p"){
    const dir = pc.w ? 1 : -1;
    if (r + dir >= 1 && r + dir <= 8){
      if (!b[(8-(r+dir))*8 + f]){
        out.push((8-(r+dir))*8 + f);
        if (r === (pc.w ? 2 : 7) && !b[(8-(r+2*dir))*8 + f])
          out.push((8-(r+2*dir))*8 + f);
      }
      for (const df of [-1, 1]){
        const ff = f + df, rr = r + dir;
        if (ff < 0 || ff > 7) continue;
        const t = b[(8-rr)*8 + ff];
        if (t && t.w !== pc.w) out.push((8-rr)*8 + ff);
      }
    }
  } else if (pc.t === "n"){
    for (const [df,dr] of [[1,2],[2,1],[-1,2],[-2,1],[1,-2],[2,-1],[-1,-2],[-2,-1]])
      push(f+df, r+dr);
  } else if (pc.t === "b")
    for (const [df,dr] of [[1,1],[1,-1],[-1,1],[-1,-1]]) ray(df,dr);
  else if (pc.t === "r")
    for (const [df,dr] of [[1,0],[-1,0],[0,1],[0,-1]]) ray(df,dr);
  else if (pc.t === "q")
    for (const [df,dr] of [[1,1],[1,-1],[-1,1],[-1,-1],[1,0],[-1,0],[0,1],[0,-1]])
      ray(df,dr);
  else {
    for (const [df,dr] of [[1,1],[1,-1],[-1,1],[-1,-1],[1,0],[-1,0],[0,1],[0,-1]])
      push(f+df, r+dr);
    const row = pc.w ? 7 : 0;   // примерная рокировка (проверит сервер)
    if (f === 4 && row === (pc.w ? 7 : 0)){
      if (!b[row*8+5] && !b[row*8+6] && b[row*8+7] &&
          b[row*8+7].t === "r" && b[row*8+7].w === pc.w) out.push(row*8+6);
      if (!b[row*8+3] && !b[row*8+2] && !b[row*8+1] && b[row*8+0] &&
          b[row*8+0].t === "r" && b[row*8+0].w === pc.w) out.push(row*8+2);
    }
  }
  return [...new Set(out)].filter(j => j !== i);
}
function renderBoard(){
  const b = replay(CH.match ? CH.match.moves : []);
  const board = $("board");
  board.innerHTML = "";
  const flip = CH.myColor === "black";
  const mv = (CH.match && CH.match.moves) || [];
  const last = mv.length ? mv[mv.length - 1] : null;
  const lastFrom = last ? idxOf(last.slice(0,2)) : -1;
  const lastTo = last ? idxOf(last.slice(2,4)) : -1;
  const turn = mv.length % 2 ? "black" : "white";
  const canMove = CH.match && CH.match.status === "playing" &&
    CH.myColor && turn === CH.myColor;
  const hints = (CH.sel >= 0 && b[CH.sel]) ? pseudoMoves(b, CH.sel) : [];
  for (let row = 0; row < 8; row++){
    for (let col = 0; col < 8; col++){
      const i = flip ? (63 - (row * 8 + col)) : (row * 8 + col);
      const sq = document.createElement("div");
      sq.className = "sq " + (((Math.floor(i/8) + i % 8) % 2) ? "d" : "l");
      if (i === CH.sel) sq.classList.add("sel");
      if (i === lastFrom || i === lastTo) sq.classList.add("last");
      const pc = b[i];
      if (pc){
        const s = document.createElement("span");
        s.textContent = GLYPH[pc.t];
        s.style.color = pc.w ? "#fdfdfd" : "#1a1a1a";
        s.style.textShadow = pc.w
          ? "0 1px 2px rgba(0,0,0,.7), 0 0 1px #000"
          : "0 1px 1px rgba(255,255,255,.25)";
        sq.appendChild(s);
      }
      if (canMove && pc && pc.w === (CH.myColor === "white"))
        sq.classList.add("mine");
      if (canMove && CH.sel >= 0 && hints.includes(i)) sq.classList.add("mv");
      sq.onclick = () => onSquare(i);
      board.appendChild(sq);
    }
  }
}
function onSquare(i){
  if (!CH.match || CH.match.status !== "playing" || !CH.myColor) return;
  const b = replay(CH.match.moves);
  const turn = CH.match.moves.length % 2 ? "black" : "white";
  if (turn !== CH.myColor){ chessWarn("сейчас ход соперника"); return; }
  const pc = b[i];
  const mine = pc && pc.w === (CH.myColor === "white");
  if (CH.sel >= 0 && CH.sel !== i && !mine){
    const uci = nameOf(CH.sel) + nameOf(i);
    CH.sel = -1; renderBoard();
    sendMove(uci);
    return;
  }
  CH.sel = mine ? i : -1;
  chessWarn("");
  renderBoard();
}
async function sendMove(uci){
  chessWarn("");
  const b = replay(CH.match.moves);
  const pc = b[idxOf(uci.slice(0,2))];
  const toRank = +uci[3];
  if (pc && pc.t === "p" && toRank === (pc.w ? 8 : 1)) uci += "q";
  handleChessAction(await chessCmd("move", [uci]));
}
async function chessCmd(command, args){
  try {
    const {json} = await apiPost("/bot_command",
      {bot_id: "chess", command, args});
    if (!json.ok){ chessWarn(json.error || "ошибка сервера"); return null; }
    return json;
  } catch(e){ chessWarn("нет связи: " + e); return null; }
}
function chessWarn(m){ $("chessWarn").textContent = m || ""; }
function handleChessAction(json){
  if (!json) return;
  const a = json.bot_action || {};
  if (a.match) applyMatch(a.match);
  else if (a.action === "match_left") applyMatch(null);
  else if (a.action === "match_state" && !a.match) applyMatch(null);
  if (a.action === "error" && a.text) chessWarn(a.text);
  else if (a.text && a.action !== "match_state")
    $("chessStatus").textContent = a.text;
}
function applyMatch(m){
  if (!m && !CH.match) return;
  const same = CH.match && m &&
    JSON.stringify(CH.match) === JSON.stringify(m);
  const oldLen = CH.match ? (CH.match.moves || []).length : -1;
  CH.match = m;
  if (!m){ clearMatchUI(); return; }
  if (same) return;
  CH.myColor = m.white === S.name ? "white"
    : (m.black === S.name ? "black" : null);
  if ((m.moves || []).length !== oldLen) CH.sel = -1;
  updateChessUI();
}
function clearMatchUI(){
  CH.myColor = null; CH.sel = -1;
  $("btnChessResign").classList.add("hidden");
  $("btnChessLeave").classList.add("hidden");
  $("chessPlayers").textContent = "";
  $("moveList").innerHTML = "";
  $("chessStatus").textContent =
    "Нет активного матча. Создай матч и передай код другу.";
  renderBoard();
}
function updateChessUI(){
  const m = CH.match, mv = m.moves || [];
  $("btnChessResign").classList.toggle("hidden", m.status !== "playing");
  $("btnChessLeave").classList.remove("hidden");
  const vs = "белые: " + (m.white || "\u2014") +
    " \u00B7 чёрные: " + (m.black || "ждём\u2026");
  $("chessPlayers").textContent =
    vs + (CH.myColor ? "" : " \u00B7 ты наблюдатель");
  const ml = $("moveList"); ml.innerHTML = "";
  for (let k = 0; k < mv.length; k += 2){
    const d = document.createElement("div");
    d.textContent = (k/2 + 1) + ". " + mv[k] + (mv[k+1] ? "  " + mv[k+1] : "");
    ml.appendChild(d);
  }
  ml.scrollTop = ml.scrollHeight;
  const turnW = mv.length % 2 === 0;
  if (m.status === "waiting")
    $("chessStatus").textContent =
      "Матч " + m.match_id + " ждёт второго игрока. Код: " + m.match_id;
  else if (m.status === "playing"){
    if (!CH.myColor)
      $("chessStatus").textContent =
        "Идёт партия (ход " + (turnW ? "белых" : "чёрных") + ")";
    else if (turnW === (CH.myColor === "white"))
      $("chessStatus").textContent =
        "Твой ход (" + (CH.myColor === "white" ? "белые" : "чёрные") + ")";
    else
      $("chessStatus").textContent = "Ход соперника\u2026";
  } else {
    const res = m.result === "draw" ? "ничья"
      : "победа: " + (m.result === "white"
        ? (m.white || "белые") : (m.black || "чёрные"));
    $("chessStatus").textContent = "\uD83C\uDFC1 Матч завершён — " + res;
  }
  renderBoard();
}
async function pollChess(){
  if (CH.busy) return;
  CH.busy = true;
  try { handleChessAction(await chessCmd("state", [])); }
  finally { CH.busy = false; }
}

/* ── таблица лидеров (v2.0.3) ──
   GET /leaderboard → [{name, score(=ELO), wins, losses, draws}, …].
   Своя строка подсвечивается (tr.merow). Пустой ответ — честная плашка,
   а не ошибка: ChessBot мог не быть включён на хосте. */
async function loadLeaderboard(){
  const box = $("lbBox");
  if (!box || CH.lbBusy) return;
  CH.lbBusy = true;
  try {
    const {json} = await apiGet("/leaderboard");
    const rows = (json && json.scores) || [];
    if (!rows.length){
      box.textContent = "Рейтинг пуст — сыграй партию с другом или ботом, "
        + "и здесь появится твой ELO.";
      return;
    }
    box.innerHTML = "";
    const t = document.createElement("table");
    for (let i = 0; i < rows.length; i++){
      const r = rows[i];
      const tr = document.createElement("tr");
      if (r.name === S.name) tr.className = "merow";
      const rank = document.createElement("td");
      rank.className = "lbrank";
      /* v3.2: медали топ-3 — монолайн-иконка с классом цвета
         (gold/silver/bronze), вместо эмодзи 🥇🥈🥉 */
      if (i < 3){
        rank.appendChild(frIcon("medal"));
        rank.classList.add(["gold", "silver", "bronze"][i]);
        rank.title = ["золото", "серебро", "бронза"][i];
      } else {
        rank.textContent = String(i + 1);
      }
      const nm = document.createElement("td");
      nm.textContent = r.name || "?";
      nm.style.fontWeight = r.name === S.name ? "800" : "600";
      const elo = document.createElement("td");
      elo.className = "lbelo";
      elo.textContent = Math.round(r.score || 0) + " ELO";
      const rec = document.createElement("td");
      rec.className = "lbrec";
      rec.textContent = "+" + (r.wins || 0) + " =" + (r.draws || 0)
        + " −" + (r.losses || 0);
      rec.title = "победы · ничьи · поражения";
      tr.appendChild(rank); tr.appendChild(nm);
      tr.appendChild(elo); tr.appendChild(rec);
      t.appendChild(tr);
    }
    box.appendChild(t);
  } catch(e){
    box.textContent = "рейтинг недоступен: " + e;
  } finally { CH.lbBusy = false; }
}
$("btnChessHost").onclick = async () => {
  chessWarn("");
  handleChessAction(await chessCmd("host", []));
};
$("btnChessJoin").onclick = doChessJoin;
$("chessCode").addEventListener("keydown", e => {
  if (e.key === "Enter") doChessJoin();
});
async function doChessJoin(){
  chessWarn("");
  const code = $("chessCode").value.trim().toUpperCase();
  if (!code){ chessWarn("введи код матча"); return; }
  handleChessAction(await chessCmd("join", [code]));
}
$("btnChessResign").onclick = () => {
  if (confirm("Сдаться? Соперник получит победу."))
    chessCmd("resign", []).then(handleChessAction);
};
$("btnChessLeave").onclick = () =>
  chessCmd("leave", []).then(handleChessAction);
$("btnChessRefresh").onclick = () => {
  chessCmd("state", []).then(handleChessAction);
  loadLeaderboard();          /* v2.0.3: рейтинг живёт рядом с кнопкой ↻ */
};
$("btnLbRefresh").onclick = loadLeaderboard;
