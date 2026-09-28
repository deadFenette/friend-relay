"""voice.client — клиентская часть голосового стека.

engine.py   — VoiceClient: сеть, потоки sounddevice, публичный API
capture.py  — MicChain: обработка микрофона (high-pass + soft gate)
playback.py — PlaybackBuffer: адаптивный джиттер-буфер плейаута
"""
from voice.client.engine import VoiceClient

__all__ = ["VoiceClient"]
