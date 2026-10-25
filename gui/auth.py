"""Access key for the GUI on a public server: one key, a login page and a session cookie.

The key is GUI_ACCESS_KEY or, when it is empty, 40 random characters generated on
the first start and kept next to the runs (GUI_ACCESS_KEY_FILE, by default
DATA_DIR/gui-access-key). The cookie is an HMAC of the key, so a restart keeps
people signed in and a new key signs everybody out."""
from __future__ import annotations
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import string

log = logging.getLogger('alfa.gui.auth')
KEY_LENGTH = 40
ALPHABET = string.ascii_letters + string.digits
COOKIE = 'alfa_session'
ROOT = Path(__file__).resolve().parents[1]


def generate() -> str:
    return ''.join(secrets.choice(ALPHABET) for _ in range(KEY_LENGTH))


def key_file() -> Path:
    explicit = os.getenv('GUI_ACCESS_KEY_FILE', '').strip()
    if explicit:
        return Path(explicit)
    return Path(os.getenv('DATA_DIR', '').strip() or ROOT/'.data')/'gui-access-key'


def resolve_key() -> str:
    """The configured key, else the saved one, else a new one written to key_file()."""
    key = os.getenv('GUI_ACCESS_KEY', '').strip()
    if key:
        if len(key) < KEY_LENGTH:
            log.warning('GUI_ACCESS_KEY короче %s символов — для открытого сервера это слабый ключ', KEY_LENGTH)
        return key
    file = key_file()
    try:
        key = file.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        key = ''
    if not key:
        key = generate()
        file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(key + '\n')
        log.warning('Создан ключ доступа к интерфейсу (%s): %s', file, key)
    else:
        log.info('Ключ доступа к интерфейсу — в %s', file)
    return key


def session_token(key: str) -> str:
    return hmac.new(key.encode(), b'alfa-gui-session', hashlib.sha256).hexdigest()


def key_matches(key: str, given: str) -> bool:
    return hmac.compare_digest(key.encode(), given.strip().encode())


def cookie_valid(key: str, header: str) -> bool:
    for part in (header or '').split(';'):
        name, _, value = part.strip().partition('=')
        if name == COOKIE and hmac.compare_digest(value.encode(), session_token(key).encode()):
            return True
    return False
