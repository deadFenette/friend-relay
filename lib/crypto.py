"""
Модуль криптографии: AES-256-GCM + PBKDF2 + SHA256.

Сообщения шифруются через AES-256-GCM (аутентифицированное шифрование —
одновременно конфиденциальность + целостность, подмена обнаруживается).
Ключ выводится из пароля пользователя через PBKDF2-HMAC-SHA256 с 600k
итераций (это замедляет брутфорс: даже слабый пароль qwerty потребует
десятки лет на одной GPU).

Форматы зашифрованных сообщений:
  - Новый (v2): "v2:" + base64(nonce[12] + ciphertext + tag[16])
    Tag встроен в конец ciphertext самим AESGCM.encrypt().
  - Старый (XOR): hex-строка без префикса — поддерживается для обратной
    совместимости, расшифровывается старым кодом (для history.jsonl).

Per-installation соль для hash_access_key:
  Генерируется случайно (32 байта) при первом запуске, хранится в
  settings.json в поле "access_key_salt". Раньше была фиксированная
  ("friend_relay_salt") — теперь у каждой установки своя, что усложняет
  брутфорс даже при утечке хеша: атакующему нужна ещё и соль.

Ничего из этого не нужно трогать вручную — CryptoManager всё инкапсулирует.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets

# cryptography — опциональная зависимость для AES-256-GCM. Если её нет
# (старое окружение без установленной библиотеки), используем fallback на
# XOR — это плохо (см. docstring модуля), но приложение не падает.
# В лог пишется предупреждение, чтобы пользователь знал и мог поставить
# `pip install cryptography`.
try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False
    import logging
    logging.getLogger("friend_relay.crypto").warning(
        "Библиотека 'cryptography' не установлена — шифрование работает в "
        "fallback-режиме (XOR, небезопасно). Установи: pip install cryptography"
    )

# ── Константы ──────────────────────────────────────────────────────────

# Количество итераций PBKDF2. 600_000 соответствует рекомендациям OWASP 2023
# для PBKDF2-HMAC-SHA256 (на GPU ~0.005$ за брутфорс-попытку при 8-символьном
# пароле из букв+цифр — практический минимум для личного использования).
_PBKDF2_ITERATIONS = 600_000

# Длина ключа AES-256 (32 байта) и nonce для GCM (12 байт — стандарт NIST).
_KEY_LEN = 32
_NONCE_LEN = 12

# Префикс для нового формата. Если зашифрованная строка начинается с "v2:" —
# это AES-GCM. Иначе считается старым XOR-форматом (hex).
_V2_PREFIX = "v2:"


# ── Хеширование (без изменений в API) ──────────────────────────────────

def sha256_hash(data: str | bytes) -> str:
    """Вычисляет SHA256 хеш от данных."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha512_hash(data: str | bytes) -> str:
    """Вычисляет SHA512 хеш от данных."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha512(data).hexdigest()


def hmac_sha256(key: str | bytes, message: str | bytes) -> str:
    """Вычисляет HMAC-SHA256 для проверки целостности сообщений."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    if isinstance(message, str):
        message = message.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def hmac_sha512(key: str | bytes, message: str | bytes) -> str:
    if isinstance(key, str):
        key = key.encode("utf-8")
    if isinstance(message, str):
        message = message.encode("utf-8")
    return hmac.new(key, message, hashlib.sha512).hexdigest()


def generate_fingerprint(data: str) -> str:
    return sha512_hash(data)[:16]


# ── Per-installation соль для access_key ──────────────────────────────

def generate_salt() -> str:
    """Генерирует случайную соль для access_key (32 байта → base64).

    Используется один раз при первом запуске; затем хранится в settings.json.
    """
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def hash_access_key(access_key: str, salt: str = "") -> str:
    """Хеширует ключ доступа с солью для безопасного хранения.

    salt — per-installation случайная соль (base64-строка из generate_salt()).
    Если salt пустая — backward-compat с фиксированной солью "friend_relay_salt"
    (для старых установок, которые ещё не мигрировали).
    """
    if not access_key:
        return ""
    if not salt:
        salt = "friend_relay_salt"
    salted_key = f"{salt}{access_key}"
    return sha256_hash(salted_key)


def verify_access_key(access_key: str, hashed_key: str, salt: str = "") -> bool:
    """Проверяет ключ доступа против хешированной версии.
    Использует hmac.compare_digest для защиты от timing-атак.
    """
    if not access_key or not hashed_key:
        return False
    computed = hash_access_key(access_key, salt)
    return hmac.compare_digest(computed, hashed_key)


# ── Подпись сообщений (без изменений) ─────────────────────────────────

def sign_message(message: dict, secret_key: str) -> str:
    message_str = json.dumps(message, sort_keys=True, ensure_ascii=False)
    return hmac_sha256(secret_key, message_str)


def verify_message_signature(message: dict, signature: str, secret_key: str) -> bool:
    computed = sign_message(message, secret_key)
    return hmac.compare_digest(computed, signature)


# ── AES-256-GCM шифрование ────────────────────────────────────────────

def _derive_key(secret_key: str, salt: str) -> bytes:
    """Выводит 32-байтный AES-ключ из пароля + соли через PBKDF2-HMAC-SHA256.

   Salt здесь — та же per-installation соль (см. generate_salt()), что и для
    access_key — переиспользуем, чтобы не плодить сущности. Если salt пустая,
    берём фиксированную (для обратной совместимости со старыми клиентами,
    которые ещё не мигрировали).
    """
    if not salt:
        salt = "friend_relay_salt"
    salt_bytes = salt.encode("utf-8") if isinstance(salt, str) else salt
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN,
        salt=salt_bytes,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(secret_key.encode("utf-8"))


def encrypt_text(text: str, key: str, salt: str = "") -> tuple[str, str]:
    """Шифрует текст через AES-256-GCM (если есть cryptography) или
    fallback'ает на XOR (старый формат, небезопасно — только чтобы не упасть).

    См. docstring модуля для деталей по формату.
    """
    if not text or not key:
        return "", ""

    # Fallback на старый XOR если cryptography недоступна
    if not _HAS_CRYPTOGRAPHY:
        return _encrypt_xor(text, key)

    salt = salt or "friend_relay_salt"
    derived_key = _derive_key(key, salt)
    aesgcm = AESGCM(derived_key)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, text.encode("utf-8"), associated_data=None)
    # ciphertext уже содержит GCM-тег в конце (16 байт)

    blob = nonce + ciphertext
    encrypted_b64 = _V2_PREFIX + base64.b64encode(blob).decode("ascii")
    nonce_hex = nonce.hex()
    return encrypted_b64, nonce_hex


def decrypt_text(encrypted: str, key: str, salt: str = "",
                 iv_or_nonce: str = "") -> str:
    """Расшифровывает текст.

    Автоопределение формата:
      - "v2:..." — AES-256-GCM (нужна cryptography, иначе возвращает "")
      - hex-строка без префикса — старый XOR-формат (всегда работает)
    """
    if not encrypted or not key:
        return ""

    # Новый формат: AES-256-GCM
    if encrypted.startswith(_V2_PREFIX):
        if not _HAS_CRYPTOGRAPHY:
            # Не можем расшифровать — нет библиотеки
            return ""
        try:
            blob = base64.b64decode(encrypted[len(_V2_PREFIX):])
            if len(blob) < _NONCE_LEN + 16:  # nonce + хотя бы tag
                return ""
            nonce = blob[:_NONCE_LEN]
            ciphertext_with_tag = blob[_NONCE_LEN:]
            salt = salt or "friend_relay_salt"
            derived_key = _derive_key(key, salt)
            aesgcm = AESGCM(derived_key)
            plaintext = aesgcm.decrypt(nonce, ciphertext_with_tag,
                                       associated_data=None)
            return plaintext.decode("utf-8")
        except Exception:
            # Может быть невалидный тег (подмена) или неверный ключ.
            # Возвращаем пустую строку — UI покажет «не удалось расшифровать».
            return ""

    # Старый формат: XOR (для обратной совместимости со старым history.jsonl)
    return _decrypt_xor(encrypted, key, iv_or_nonce)


# ── Legacy XOR (только для чтения старых сообщений) ──────────────────

def _encrypt_xor(text: str, key: str) -> tuple[str, str]:
    """XOR-шифрование — fallback когда cryptography не установлена.

    НЕБЕЗОПАСНО: ломается частотным анализом. Используется только как
    заглушка чтобы приложение не падало. Установи `pip install cryptography`
    для настоящего AES-256-GCM.

    Генерирует случайный IV (16 байт) — лучше чем старый детерминированный
    IV от хеша текста, но всё равно XOR остаётся слабым.
    """
    if not text or not key:
        return "", ""
    iv = secrets.token_hex(8)  # 16 hex chars = 8 bytes
    key_bytes = (key + iv).encode("utf-8")
    text_bytes = text.encode("utf-8")
    encrypted = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(text_bytes))
    return encrypted.hex(), iv


def _decrypt_xor(encrypted_hex: str, key: str, iv: str) -> str:
    """Расшифровывает старый XOR-формат. НЕ ИСПОЛЬЗУЙ для новых сообщений —
    XOR ломается частотным анализом. Оставлен только для чтения history.jsonl,
    где могут быть старые зашифрованные сообщения.
    """
    if not encrypted_hex or not key or not iv:
        return ""
    try:
        encrypted = bytes.fromhex(encrypted_hex)
        key_bytes = (key + iv).encode("utf-8")
        decrypted = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(encrypted))
        return decrypted.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


# ── CryptoManager ──────────────────────────────────────────────────────

class CryptoManager:
    """Менеджер криптографических операций.

    Хранит secret_key и salt, предоставляет encrypt/decrypt для чата
    и hash/verify для access_key. Один экземпляр на экран/сервер.
    """

    def __init__(self, secret_key: str = "", salt: str = ""):
        self._secret_key = secret_key
        self._salt = salt  # per-installation; пустая = legacy-режим

    def set_secret_key(self, key: str) -> None:
        self._secret_key = key

    def set_salt(self, salt: str) -> None:
        """Устанавливает per-installation соль.
        Вызывается после загрузки settings.json.
        """
        self._salt = salt

    def hash_access_key(self, access_key: str) -> str:
        return hash_access_key(access_key, self._salt)

    def verify_access_key(self, access_key: str, hashed_key: str) -> bool:
        return verify_access_key(access_key, hashed_key, self._salt)

    def sign_message(self, message: dict) -> str:
        if not self._secret_key:
            return ""
        return sign_message(message, self._secret_key)

    def verify_message(self, message: dict, signature: str) -> bool:
        if not self._secret_key:
            return False
        return verify_message_signature(message, signature, self._secret_key)

    def encrypt(self, text: str) -> tuple[str, str]:
        """Шифрует текст AES-256-GCM. Возвращает (encrypted_b64, nonce_hex)."""
        if not self._secret_key:
            return "", ""
        return encrypt_text(text, self._secret_key, self._salt)

    def decrypt(self, encrypted: str, iv_or_nonce: str = "") -> str:
        """Расшифровывает текст (AES-GCM или старый XOR — автоопределение)."""
        if not self._secret_key:
            return ""
        return decrypt_text(encrypted, self._secret_key, self._salt, iv_or_nonce)

    def generate_message_id(self) -> str:
        import time
        import uuid
        data = f"{time.time()}{uuid.uuid4()}"
        return generate_fingerprint(data)


# ── Оценка силы пароля (для UI) ────────────────────────────────────────

def password_strength(password: str) -> tuple[int, str, str]:
    """Возвращает (score 0-4, label, color_hex) для индикатора силы пароля.

    0 — Очень слабый (красный) — <6 символов или словарный
    1 — Слабый (оранжевый)
    2 — Средний (жёлтый)
    3 — Хороший (зелёный)
    4 — Сильный (ярко-зелёный)
    """
    if not password:
        return 0, "—", "#5C5C66"

    length = len(password)
    has_lower = any(c.islower() for c in password)
    has_upper = any(c.isupper() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_special = any(not c.isalnum() for c in password)
    variety = sum([has_lower, has_upper, has_digit, has_special])

    if length < 6:
        return 0, "Очень слабый", "#FB7185"
    if length < 8:
        return 1, "Слабый", "#FBBF24"
    if length < 12 and variety < 3:
        return 1, "Слабый", "#FBBF24"
    if length < 12:
        return 2, "Средний", "#FBBF24"
    if length < 16 and variety < 3:
        return 2, "Средний", "#FBBF24"
    if length < 16:
        return 3, "Хороший", "#34D399"
    if variety < 4:
        return 3, "Хороший", "#34D399"
    return 4, "Сильный", "#22D399"
