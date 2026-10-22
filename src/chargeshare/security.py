"""住戶密碼雜湊（標準函式庫 scrypt）與 session 金鑰。"""

import hashlib
import hmac
import os
import secrets
from pathlib import Path

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    scheme, salt_hex, digest_hex = stored.split("$")
    if scheme != "scrypt":
        raise ValueError(f"unsupported password scheme {scheme!r}")
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P)
    return hmac.compare_digest(digest.hex(), digest_hex)


def load_or_create_secret_key(path: Path) -> str:
    """讀取 session 金鑰；不存在時產生隨機金鑰並以僅擁有者可讀寫（0600）的權限保存。"""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # 另一個行程剛好同時建立，沿用它的金鑰
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key
