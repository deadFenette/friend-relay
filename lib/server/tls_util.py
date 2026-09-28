"""
Самоподписанный TLS-сертификат для HTTPS веб-клиента.

Зачем: браузер выдаёт микрофон (getUserMedia) и crypto.subtle только в
secure context (HTTPS). Для приватной сети (ZeroTier/локалка) генерируем
самоподписанный сертификат автоматически при первом старте: браузер один
раз покажет предупреждение («Дополнительно → Перейти на сайт»), дальше —
полноценный secure context без ручных флагов.

Сертификат и ключ хранятся в data_dir хоста (tls_cert.pem / tls_key.pem)
и переиспользуются между запусками — отпечаток в браузере не меняется.
SAN включают localhost и все текущие IPv4 машины (в т.ч. ZeroTier 100.x);
если IP потом сменится, браузер всё равно позволит продолжить вручную.

Зависимость: cryptography (уже нужна для AES-256-GCM). Если её нет —
HTTPS/WSS просто не поднимаются, HTTP-сервер работает как раньше.
"""
from __future__ import annotations

import logging
import socket
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("friend_relay.tls")

_CERT_NAME = "tls_cert.pem"
_KEY_NAME = "tls_key.pem"
# Перевыпускаем заранее: если серту осталось жить меньше этого — новый.
_RENEW_MARGIN = timedelta(days=30)


def _local_ips() -> list[str]:
    """Все IPv4-адреса текущей машины (loopback + LAN + ZeroTier/VPN).

    v3.4.1: единый источник — lib.net_utils.get_interfaces() (WinAPI+fallbacks).
    Раньше getaddrinfo + connect(8.8.8.8) при включённом VPN возвращали только
    VPN-IP, и в SAN-сертификата не попадали ZeroTier-адреса → браузер ругался
    на mismatch, заходя по https://ZT-адресу. Теперь — все интерфейсы сразу.
    """
    try:
        from lib.net_utils import get_interfaces
    except Exception:
        # Fallback на старый метод (критично, если циклический импорт — но
        # net_utils.py не импортирует tls_util, так что не должно случиться).
        ips = {"127.0.0.1"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                ips.add(s.getsockname()[0])
            finally:
                s.close()
        except OSError:
            pass
        return sorted(ips)
    seen: set[str] = set()
    out: list[str] = []
    for i in get_interfaces():
        if i.ip not in seen:
            seen.add(i.ip)
            out.append(i.ip)
    # loopback всегда явно (даже если get_interfaces не включил)
    if "127.0.0.1" not in seen:
        out.insert(0, "127.0.0.1")
    return out


def _build_cert(cert_file: Path, key_file: Path) -> bool:
    """Генерирует самоподписанный сертификат + ключ. True при успехе.

    (v3.4.2 fix) Ключ — ECDSA P-256 вместо RSA-2048.

    ЗАМЕРЕНО на машине пользователя (см. docs/HANG_DIAGNOSIS_v3.4.1.md):
    генерация RSA-2048
    занимала 84.57 СЕКУНДЫ (слабый/загруженный CPU, нехватка энтропии) —
    relay.start() всё это время висел, а он вызывался из GUI-потока:
    окно «Не отвечает», выглядит как полное зависание. ECDSA P-256
    генерируется за миллисекунды на любом CPU. Поддержка: все браузеры
    с ~2016 г. (Chrome/Firefox/Edge/Safari), Windows 10+, OpenSSL 1.0.2+.
    Если EC по какой-то причине недоступен — откат на RSA как раньше."""
    try:
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        log.warning("HTTPS не поднят: нет библиотеки cryptography "
                    "(pip install cryptography)")
        return False

    now = datetime.now(UTC)
    key = None
    # Быстрый путь: ECDSA P-256 (миллисекунды даже на слабом CPU)
    try:
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        sign_hash = hashes.SHA256()
    except Exception as e:  # экзотическая сборка cryptography — откат на RSA
        log.info("ECDSA недоступен (%s) — генерирую RSA-2048 (медленно)", e)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        sign_hash = hashes.SHA256()
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "friend-relay"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "friend_relay"),
    ])
    sans: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]
    for ip in _local_ips():
        try:
            sans.append(x509.IPAddress(ipaddress.IPv4Address(ip)))
        except ValueError:
            continue
    # Ограничим SAN ~64 записями — на всякий случай. У обычного хоста 3-6,
    # и при смене IP браузер всё равно позволяет продолжить вручную.
    if len(sans) > 64:
        sans = sans[:64]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # самоподписанный
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))  # запас на кривые часы
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .sign(key, sign_hash)
    )
    try:
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        try:
            key_file.chmod(0o600)  # на Windows молча игнорируется — ок
        except OSError:
            pass
        return True
    except OSError as e:
        log.warning("HTTPS не поднят: не удалось записать сертификат: %s", e)
        return False


def ensure_tls_files(data_dir: Path) -> tuple[Path, Path] | None:
    """Возвращает (cert, key), создавая/обновляя их при необходимости.
    None — cryptography недоступна или файлы не записались."""
    cert_file = data_dir / _CERT_NAME
    key_file = data_dir / _KEY_NAME
    if cert_file.exists() and key_file.exists():
        # Переиспользуем, пока серт жив (проверяем с запасом _RENEW_MARGIN)
        try:
            from cryptography import x509

            cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
            expires = cert.not_valid_after_utc
            if expires - datetime.now(UTC) > _RENEW_MARGIN:
                return cert_file, key_file
            log.info("TLS-сертификат скоро истекает (%s) — перевыпускаю", expires)
        except ImportError:
            return None
        except Exception as e:  # битый файл — пересоздаём
            log.info("TLS-сертификат не читается (%s) — пересоздаю", e)
    data_dir.mkdir(parents=True, exist_ok=True)
    if not _build_cert(cert_file, key_file):
        return None
    return cert_file, key_file


def make_server_ssl_context(cert_file: Path, key_file: Path) -> ssl.SSLContext | None:
    """SSLContext для серверной стороны (TLS 1.2+)."""
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
        return ctx
    except (ssl.SSLError, OSError) as e:
        log.warning("HTTPS не поднят: не удалось загрузить сертификат: %s", e)
        return None
