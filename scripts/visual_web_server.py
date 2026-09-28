"""Визуальная проверка веб-клиента: поднимаем сервер на 127.0.0.1:8420
и держим его живым для скриншотов браузером."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.relay_server import RelayServer

tmp = Path(tempfile.mkdtemp(prefix="fr_visual_"))
relay = RelayServer(tmp, host_name="VisualHost", access_key="",
                    max_file_size=10 * 1024 * 1024)
print("START", relay.start("127.0.0.1", 8420), flush=True)
import signal


def stop(*a):
    relay.stop()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
print("READY on http://127.0.0.1:8420/", flush=True)
import time

while True:
    time.sleep(60)
