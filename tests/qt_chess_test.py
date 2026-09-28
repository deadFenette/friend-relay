"""Регрессионные тесты шахмат (ChessEngine / ChessBoardWidget / ChessGameDialog / ChessBot).

Без сети: сетевые матчи проверяются через apply_net_state() c поддельными
ответами сервера, ChessBot — напрямую через process_command().

Запуск:
    QT_QPA_PLATFORM=offscreen python3 tests/qt_chess_test.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from lib.bots.chess import ChessBot
from qt_app.widgets.chess_game_dialog import (
    ChessAI,
    ChessBoardWidget,
    ChessEngine,
    ChessGameDialog,
)

app = QApplication(sys.argv)

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("OK   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def wait_for(cond, timeout_ms: int = 6000) -> bool:
    """Крутит event loop, пока cond() не станет True (или таймаут)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


FILES = "abcdefgh"
RANKS = "87654321"


def sq(name: str) -> int:
    """'e2' -> индекс клетки движка (row-major, a8 = 0)."""
    return RANKS.index(name[1]) * 8 + FILES.index(name[0])


def place(eng: ChessEngine, cell: str, piece: str) -> None:
    eng.board[sq(cell)] = piece


def clear(eng: ChessEngine) -> None:
    """Полностью очищает доску (для тестов с ручной расстановкой фигур).
    Без очистки на доске остаются фигуры начальной позиции — и мат/пат
    не детектируются, ведь у стороны есть лишние ходы."""
    for i in range(64):
        eng.board[i] = "."


# ═══════════════════════════════════════════════════════════════════
# 1. ChessEngine — правила
# ═══════════════════════════════════════════════════════════════════

eng = ChessEngine()
check("E1. начальная позиция: фигуры на местах",
      eng.board[sq("e2")] == "P" and eng.board[sq("e1")] == "K" and eng.board[sq("e8")] == "k")
check("E2. начальная позиция: 20 легальных ходов", len(eng.generate_moves()) == 20)

eng.make_move((sq("e2"), sq("e4"), None))
check("E3. e2e4: пешка сходила, en passant = e3, ход чёрных",
      eng.board[sq("e4")] == "P" and eng.board[sq("e2")] == "."
      and eng.en_passant == sq("e3") and eng.white_to_move is False)

# Мат Фолли: f3? e5 / g4? Qh4#
# ВАЖНО: сбрасываем движок — после E3 ходят белые ещё раз (e2e4 уже сделан
# белыми), и без сброса последовательность играет ходы вне очереди:
# is_checkmate() в итоге смотрит не на ту сторону и мат не видит.
eng.reset()
for fr, to in (("f2", "f3"), ("e7", "e5"), ("g2", "g4"), ("d8", "h4")):
    eng.make_move((sq(fr), sq(to), None))
check("E4. мат Фолли обнаруживается", eng.is_checkmate() and eng.is_check(eng.white_to_move))

# Пат: чёрный король a8, белый K b6 + ферзь c7 — ход чёрных, ходов нет, шаха нет
eng.reset()
clear(eng)  # только 3 фигуры — иначе чёрная армия даёт ходы и пат невозможен
place(eng, "a8", "k")
place(eng, "b6", "K")
place(eng, "c7", "Q")
eng.white_to_move = False
check("E5. пат обнаруживается", eng.is_stalemate() and not eng.is_check(False))

# Рокировка белых в короткую сторону
eng.reset()
place(eng, "f1", ".")
place(eng, "g1", ".")
check("E6. рокировка доступна, когда f1/g1 пусты",
      any(m[:2] == (sq("e1"), sq("g1")) for m in eng.generate_moves()))
eng.make_move((sq("e1"), sq("g1"), None))
check("E7. рокировка: король на g1, ладья переставилась на f1",
      eng.board[sq("g1")] == "K" and eng.board[sq("f1")] == "R" and eng.board[sq("h1")] == ".")

# Право рокировки теряется при ходе ладьи
eng.reset()
eng.make_move((sq("a1"), sq("a2"), None))
check("E8. ход ладьи a1 снимает право длинной рокировки", eng.castling["Q"] is False)

# Взятие на проходе, белые (регрессия бага ep_row)
eng.reset()
for fr, to in (("e2", "e4"), ("a7", "a6"), ("e4", "e5"), ("d7", "d5")):
    eng.make_move((sq(fr), sq(to), None))
ep_moves = [m for m in eng.generate_moves() if m[:2] == (sq("e5"), sq("d6"))]
check("E9. взятие на проходе e5xd6 доступно", len(ep_moves) == 1 and ep_moves[0][2] is None)
eng.make_move(ep_moves[0])
check("E10. взятие на проходе: пешка d5 снята, белая пешка на d6",
      eng.board[sq("d5")] == "." and eng.board[sq("d6")] == "P")

# Взятие на проходе, чёрные (вторая сторона того же бага)
eng.reset()
for fr, to in (("a2", "a3"), ("e7", "e5"), ("h2", "h3"), ("e5", "e4"), ("d2", "d4")):
    eng.make_move((sq(fr), sq(to), None))
ep_moves = [m for m in eng.generate_moves() if m[:2] == (sq("e4"), sq("d3"))]
check("E11. взятие на проходе e4xd3 (чёрными) доступно",
      len(ep_moves) == 1 and ep_moves[0][2] is None)
eng.make_move(ep_moves[0])
check("E12. взятие на проходе чёрными: пешка d4 снята, чёрная на d3",
      eng.board[sq("d4")] == "." and eng.board[sq("d3")] == "p")

# Превращение со взятием: ставим БЕЛУЮ пешку на a7 — в начальной позиции
# там стоит ЧЁРНАЯ пешка, и «a7xb8» для белых просто не существует.
eng.reset()
place(eng, "a7", "P")
promo_moves = [m for m in eng.generate_moves() if m[:2] == (sq("a7"), sq("b8"))]
check("E13. превращение a7xb8: 4 варианта (Q/R/B/N)",
      sorted(m[2] for m in promo_moves) == ["B", "N", "Q", "R"])
eng.make_move((sq("a7"), sq("b8"), "Q"))
check("E14. превращение: ферзь на b8", eng.board[sq("b8")] == "Q" and eng.board[sq("a7")] == ".")

# Undo восстанавливает позицию байт в байт
eng.reset()
snapshot = list(eng.board)
eng.make_move((sq("e2"), sq("e4"), None))
eng.make_move((sq("e7"), sq("e5"), None))
eng.make_move((sq("g1"), sq("f3"), None))
eng.undo_move()
eng.undo_move()
eng.undo_move()  # сделано 3 хода — отменить нужно все 3
check("E15. undo: позиция и флаги восстановлены",
      eng.board == snapshot and eng.white_to_move is True and eng.history == [])

# ═══════════════════════════════════════════════════════════════════
# 2. ChessAI — поиск хода
# ═══════════════════════════════════════════════════════════════════

ai = ChessAI(depth=2)
eng.reset()
before = (list(eng.board), dict(eng.castling), eng.en_passant, eng.white_to_move)
mv = ai.find_best_move(eng)
check("A1. ИИ вернул легальный ход", mv is not None and mv in eng.generate_moves())
after = (list(eng.board), dict(eng.castling), eng.en_passant, eng.white_to_move)
check("A2. ИИ не портит позицию (make/undo сбалансированы)", before == after)

# Мат в 1 ход: Re1-e8# — чистая позиция: чёрный король h8 заперт своими
# пешками f7/g7/h7, e-файл свободен, у чёрных нет коня g8 (иначе Ne7
# закрывал бы шах). Белый король в углу — не мешает.
eng.reset()
clear(eng)
place(eng, "h8", "k")
place(eng, "f7", "p")
place(eng, "g7", "p")
place(eng, "h7", "p")
place(eng, "e1", "R")
place(eng, "a1", "K")
mate_move = ai.find_best_move(eng)
eng.make_move(mate_move)
check("A3. ИИ находит мат в 1 ход (Re8#)", eng.is_checkmate())

# ═══════════════════════════════════════════════════════════════════
# 3. UCI-конвертация (все точки входа должны совпадать)
# ═══════════════════════════════════════════════════════════════════

check("U1. move_to_uci: e2e4",
      ChessBoardWidget._move_to_uci(sq("e2"), sq("e4"), None) == "e2e4")
check("U2. move_to_uci: превращение a7b8q",
      ChessBoardWidget._move_to_uci(sq("a7"), sq("b8"), "Q") == "a7b8q")

w_tmp = ChessBoardWidget()
check("U3. uci_to_move (доска): e2e4",
      w_tmp._uci_to_move("e2e4") == (sq("e2"), sq("e4"), None))
check("U4. uci_to_move (доска): превращение e7e8q",
      w_tmp._uci_to_move("e7e8q") == (sq("e7"), sq("e8"), "Q"))
bad_cases = ["", "e2e", "i2i4", "e9e5", "e2e4x", None, 12345]
check("U5. невалидный UCI отбрасывается (7 вариантов)",
      all(w_tmp._uci_to_move(b) is None for b in bad_cases))
check("U6. uci_to_move (бот) совпадает с доской",
      ChessBot._uci_to_move(ChessEngine(), "e2e4") == (sq("e2"), sq("e4"), None))

# ═══════════════════════════════════════════════════════════════════
# 4. ChessBoardWidget — доска, ходы, ИИ
# ═══════════════════════════════════════════════════════════════════

w = ChessBoardWidget()
w.ai.depth = 1  # ускоряем тестовый ИИ
w._try_move(sq("e2"), sq("e4"))
check("W1. ход игрока применился: last_move + ход чёрных",
      w.last_move == (sq("e2"), sq("e4")) and w.engine.white_to_move is False)
check("W2. ИИ ответил (ход вернулся белым, fullmove=2)",
      wait_for(lambda: w.engine.white_to_move and not w.ai_thinking)
      and w.engine.fullmove == 2)
check("W3. флаг ai_thinking сброшен после хода ИИ", w.ai_thinking is False)

# Смена режима и рестарт
w.set_mode(ChessBoardWidget.MODE_PVP)
check("W4. set_mode(PVP): доска сброшена, ходят белые",
      w.mode == ChessBoardWidget.MODE_PVP and w.engine.white_to_move is True
      and len(w.engine.history) == 0)
w.set_skin("gothic")
check("W5. set_skin применяется", w.piece_skin == "gothic")
w.set_skin("no-such-skin")
check("W6. неизвестный скин отклонён", w.piece_skin == "gothic")

# Переворот доски по клавише F
w.flipped = False
w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F, Qt.NoModifier))
check("W7. F переворачивает доску", w.flipped is True)

# Превращение без модального диалога: прямой вызов обработчика выбора
w2 = ChessBoardWidget()
place(w2.engine, "a7", "P")  # белая пешка на a7 (в начальной позиции там чёрная)


class _FakeDlg:
    def __init__(self):
        self.accepted = False

    def accept(self):
        self.accepted = True


dlg = _FakeDlg()
w2._on_promo_chosen("Q", {"Q": (sq("a7"), sq("b8"), "Q")}, dlg)
check("W8. выбор ферзя при превращении: диалог принят, фигура на b8",
      dlg.accepted and w2.engine.board[sq("b8")] == "Q" and w2._promo_pending is None)

# ═══════════════════════════════════════════════════════════════════
# 5. Сетевая синхронизация доски (apply_net_state)
# ═══════════════════════════════════════════════════════════════════

wn = ChessBoardWidget()
game_overs: list[tuple] = []
wn.set_mode(ChessBoardWidget.MODE_NET)
wn.start_net_match(color=False, match_id="ABCD", opponent_name="Вася")
check("N1. старт матча за чёрных: доска перевёрнута, матч установлен",
      wn.net_color is False and wn.net_match_id == "ABCD" and wn.flipped is True)

wn.on_game_over = lambda *a: game_overs.append(a)
wn.apply_net_state({
    "match_id": "ABCD", "white": "Вася", "black": "tester",
    "status": "playing", "moves": ["e2e4", "e7e5"], "result": None,
})
check("N2. два серверных хода применены к доске",
      wn.net_applied_moves == 2 and wn.engine.board[sq("e4")] == "P"
      and wn.engine.board[sq("e5")] == "p")

# Повторный опрос с тем же состоянием не должен применять ходы повторно
wn.apply_net_state({
    "match_id": "ABCD", "white": "Вася", "black": "tester",
    "status": "playing", "moves": ["e2e4", "e7e5"], "result": None,
})
check("N3. повторный poll не дублирует ходы", wn.net_applied_moves == 2)

wn.apply_net_state({
    "match_id": "ABCD", "white": "Вася", "black": "tester",
    "status": "finished", "result": "white",
    "moves": ["e2e4", "e7e5"],
})
check("N4. завершённый матч: game_over + колбэк поражения",
      wn.game_over is True and bool(game_overs) and game_overs[0][0] == "win_white")

# Мы белые: наш ход уходит в on_net_move и не дублируется при poll
wn2 = ChessBoardWidget()
sent: list[str] = []
wn2.on_net_move = sent.append
wn2.set_mode(ChessBoardWidget.MODE_NET)
wn2.start_net_match(color=True, match_id="XYZ", opponent_name="Петя")
wn2._try_move(sq("e2"), sq("e4"))
check("N5. наш ход: уведомление on_net_move('e2e4') + счётчик applied",
      sent == ["e2e4"] and wn2.net_applied_moves == 1)
wn2.apply_net_state({
    "match_id": "XYZ", "white": "tester", "black": "Петя",
    "status": "playing", "moves": ["e2e4"], "result": None,
})
check("N6. сервер вернул наш ход — повторно не применён",
      wn2.net_applied_moves == 1 and len(wn2.engine.history) == 1)

wn2.apply_net_state(None)
check("N7. match=None: выход из матча", wn2.net_match_id is None)

# ═══════════════════════════════════════════════════════════════════
# 6. ChessGameDialog — офлайн-обвязка
# ═══════════════════════════════════════════════════════════════════

dlg_app = ChessGameDialog(None, "", "tester", "")
check("D1. без base_url рейтинг показывает заглушку",
      "Нет подключения" in dlg_app._leaderboard_label.text())
dlg_app._set_mode(ChessBoardWidget.MODE_PVP)
check("D2. переключение в PVP синхронизирует кнопки",
      dlg_app._btn_mode_pvp.isChecked()
      and not dlg_app._btn_mode_ai.isChecked()
      and dlg_app._game.board.mode == ChessBoardWidget.MODE_PVP)
dlg_app._set_mode(ChessBoardWidget.MODE_AI)
check("D3. возврат в ИИ-режим", dlg_app._btn_mode_ai.isChecked())
dlg_app.close()
check("D4. диалог закрывается без сети (closeEvent не падает)", True)

# ═══════════════════════════════════════════════════════════════════
# 7. ChessBot — серверные команды без сети
# ═══════════════════════════════════════════════════════════════════

bot = ChessBot(Path(tempfile.mkdtemp()))
open_res = bot.process_command("Тестер", "", [])
check("B1. !chess открывает игру", isinstance(open_res, dict) and open_res["action"] == "open_game")
check("B2. !chess top открывает рейтинг",
      bot.process_command("Тестер", "top", [])["tab"] == "leaderboard")

saved = bot.process_command("Тестер", "result", ["win"])
check("B3. результат win сохраняется и растит ELO",
      saved["action"] == "score_saved" and saved["best"] > 1200)

scores = bot.process_command("Тестер", "getscores", [])
check("B4. getscores: silent-команда с таблицей",
      scores.get("silent") is True and scores["scores"][0]["name"] == "Тестер")
check("B5. неверный результат отклоняется",
      bot.process_command("Тестер", "result", ["smth"])["action"] == "error")

# Полный сетевой матч: host → join → контроль ходов → state
host_res = bot.process_command("Алиса", "host", [])
mid = host_res["match"]["match_id"]
check("B6. host создаёт матч, Алиса белые",
      host_res["action"] == "match_created" and host_res["match"]["white"] == "Алиса")

join_res = bot.process_command("Боб", "join", [mid])
check("B7. join подключает Боба чёрными",
      join_res["action"] == "match_joined" and join_res["match"]["black"] == "Боб")

check("B8. ход не в свою очередь отклоняется",
      bot.process_command("Боб", "move", ["e7e5"])["action"] == "error")
check("B9. нелегальный ход отклоняется",
      bot.process_command("Алиса", "move", ["e2e6"])["action"] == "error")
check("B10. легальный ход принимается",
      bot.process_command("Алиса", "move", ["e2e4"])["action"] == "move_accepted")

state = bot.process_command("Боб", "state", [])
check("B11. state отдаёт матч с ходом e2e4", state["match"]["moves"] == ["e2e4"])

# Мат Фолли на сервере: f3 e5 g4?? Qh4# — Боб выигрывает
bot2 = ChessBot(Path(tempfile.mkdtemp()))
mid2 = bot2.process_command("Алиса", "host", [])["match"]["match_id"]
bot2.process_command("Боб", "join", [mid2])
for who, uci in (("Алиса", "f2f3"), ("Боб", "e7e5"), ("Алиса", "g2g4"), ("Боб", "d8h4")):
    fin = bot2.process_command(who, "move", [uci])
check("B12. матующий ход завершает матч (победа чёрных)",
      fin["action"] == "match_finished" and fin["match"]["result"] == "black")
elo = bot2.get_data("players", {})
check("B13. ELO обновился у обоих игроков",
      "Алиса" in elo and "Боб" in elo and elo["Боб"]["wins"] == 1)

# Сдача
bot3 = ChessBot(Path(tempfile.mkdtemp()))
mid3 = bot3.process_command("А", "host", [])["match"]["match_id"]
bot3.process_command("Б", "join", [mid3])
bot3.process_command("А", "move", ["e2e4"])
res_res = bot3.process_command("А", "resign", [])
check("B14. resign: победа присуждена сопернику",
      res_res["action"] == "match_finished" and res_res["match"]["result"] == "black")

leave_res = bot3.process_command("А", "leave", [])
check("B15. leave после завершённого матча освобождает игрока",
      leave_res["action"] == "match_left")

# ═══════════════════════════════════════════════════════════════════

print()
if failures:
    print(f"CHESS REGRESSION TESTS FAILED: {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("CHESS REGRESSION TESTS OK")
