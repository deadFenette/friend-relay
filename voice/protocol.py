"""Проводной протокол голосового канала (FRVC) — единый источник правды.

Кадр (конверт заморожен с v1.6, аудио-параметры — v2.0):
    4 байта  magic "FRVC"
    1 байт   flags: 0x01 muted, 0x02 silence, 0x04 control/JSON
    2 байта  length (little-endian)
    N байт   payload: PCM int16 mono 48kHz (голос, 1920 байт = 20мс)
             либо JSON (control)

v2.0 (аудио-формат): 16 кГц/10мс → 48 кГц/20мс. Fullband вместо
«телефонного» звука, вдвое меньше кадров в секунду (меньше оверхеда
TCP и джиттера), 20мс-кадры — стандарт VoIP (Discord/WhatsApp).

Совместимость: конверт кадра не изменился. Opus-путь совместим и между
версиями (libopus сам ресемплит поток любой частоты). RAW-PCM путь
(фолбэк без opuslib_next) частотозависим: старый клиент + новый микшер
даст «бурундука» — обновляйте обе стороны вместе (приложение
распространяется одним архивом).

Control-кадры (flags & FLAG_CONTROL) — служебные JSON:
    {"t": "spk",    "name": "Вася", "on": true}     — Вася начал/кончил
    {"t": "spk_all","states": {...}, "codec": "..."} — снапшот новому
                    клиенту; поле codec — какой кодек умеет микшер
                    ("opus" | "pcm"), старые клиенты поле игнорируют
    {"t": "roster", "names": [...]}                  — состав канала
    {"t": "codec",  "codec": "opus"}  клиент → микшер: прошу opus
                    микшер → клиент: подтверждение (opus) или отказ
                    (pcm) — после отказа клиент живёт на PCM

Согласование кодека НЕ меняет конверт кадра: payload голосового кадра
при согласованном opus — opus-данные (размер переменный, length его
описывает). Silence-кадры всегда несут нулевой payload (клиент
игнорирует payload при флаге silence), поэтому смена кодека на лету
для слушателей безопасна.
"""
from __future__ import annotations

import struct

# ── Аудио-параметры (общие для микшера, клиента и моста) ─────────────
SAMPLE_RATE = 48000         # Hz — fullband: тот же стандарт, что у Discord
CHANNELS = 1
FRAME_MS = 20               # длительность кадра
PCM_FRAME_SAMPLES = 960     # 20мс при 48kHz
PCM_FRAME_BYTES = PCM_FRAME_SAMPLES * 2  # int16 = 2 байта на сэмпл

# Протокол
MAGIC = b"FRVC"
FRAME_HEADER_LEN = 7        # 4 magic + 1 flags + 2 length
MAX_PAYLOAD_BYTES = 4096    # PCM-кадр 1920 + запас (защита от мусора в length)

# flags
FLAG_MUTED = 0x01
FLAG_SILENCE = 0x02
FLAG_CONTROL = 0x04  # payload = JSON, не PCM

# VAD: RMS ниже этого — silence frame (экономим трафик и CPU микшера).
# Шкала амплитуд int16 не зависит от частоты дискретизации: 300 = «тишина»
# и на 16к, и на 48к. Обычная речь ~2000-5000 RMS.
VAD_THRESHOLD = 300.0


def make_frame(flags: int, payload: bytes) -> bytes:
    """Собирает кадр FRVC: magic + flags + length + payload."""
    return MAGIC + bytes([flags]) + struct.pack("<H", len(payload)) + payload


def parse_frame_header(header: bytes) -> tuple[int, int]:
    """Разбирает 7-байтовый заголовок → (flags, length)."""
    flags = header[4]
    (length,) = struct.unpack("<H", header[5:7])
    return flags, length


def frame_align_cut(buf: bytearray, want: int) -> int:
    """Сколько байт можно безопасно отрезать С НАЧАЛА буфера, не попав
    внутрь кадра. Кадр = magic(4) + flags(1) + len(2) + payload: режем
    только до границы следующего полного кадра (v1.9.5). Раньше
    del buf[:64*1024] рубил ПОСЕРЕДИНЕ кадра — поток клиента
    рассинхронизировался по magic «FRVC» и все последующие кадры
    превращались в мусор до переподключения."""
    i = 0
    n = len(buf)
    best = 0
    while i + FRAME_HEADER_LEN <= n and i < want:
        frame_len = int.from_bytes(buf[i + 5:i + 7], "little")
        end = i + FRAME_HEADER_LEN + frame_len
        if end > n:
            break  # кадр ещё не весь в буфере — не отрезаем
        best = end
        i = end
    return best
