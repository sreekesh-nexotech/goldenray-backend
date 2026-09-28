"""Key material helpers used by the settings modules.

* RS256 keys for SimpleJWT: production reads PEM files from the paths in the environment and refuses to start
  without them; dev/test generate a 2048-bit keypair into ``var/keys/`` on first use (never committed).
* Fernet keys: production reads ``FERNET_KEYS``; dev generates a key into ``var/keys/`` on first use; test settings
  generate an in-memory key per process.

The public key is always derivable from the private key, so dev/test keep a single file. That makes key creation
race-free across concurrent processes: the file is written to a temporary name and hard-linked into place, so the
first writer wins and every process reads a complete key.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

JWT_PRIVATE_KEY_FILENAME = "jwt_private.pem"
FERNET_KEY_FILENAME = "fernet.key"


def _write_once(path: Path, content: bytes) -> None:
    """Create ``path`` with ``content`` unless it already exists (atomic, first writer wins)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp_name, 0o600)
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(tmp_name)


def generate_rsa_private_pem(key_size: int = 2048) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    return key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8, encryption_algorithm=serialization.NoEncryption())


def public_pem_from_private(private_pem: str | bytes) -> str:
    data = private_pem.encode() if isinstance(private_pem, str) else private_pem
    key = serialization.load_pem_private_key(data, password=None)
    return key.public_key().public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def ensure_dev_jwt_keypair(key_dir: Path) -> Path:
    """Return the dev/test private key path, generating a 2048-bit RSA key there if missing."""
    path = Path(key_dir) / JWT_PRIVATE_KEY_FILENAME
    if not path.exists():
        _write_once(path, generate_rsa_private_pem())
    return path


def load_jwt_keys(private_path: str | Path | None, public_path: str | Path | None = None) -> tuple[str | None, str | None]:
    """Read the PEM pair. The public key is derived from the private key when no public path is given."""
    if not private_path or not Path(private_path).is_file():
        return None, None
    private_pem = Path(private_path).read_text()
    if public_path and Path(public_path).is_file():
        public_pem = Path(public_path).read_text()
    else:
        public_pem = public_pem_from_private(private_pem)
    return private_pem, public_pem


def ensure_dev_fernet_key(key_dir: Path) -> str:
    path = Path(key_dir) / FERNET_KEY_FILENAME
    if not path.exists():
        _write_once(path, Fernet.generate_key())
    return path.read_text().strip()


def valid_fernet_key(key: str) -> bool:
    try:
        Fernet(key.encode())
    except (ValueError, TypeError):
        return False
    return True
