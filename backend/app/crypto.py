from cryptography.fernet import Fernet, MultiFernet

from app.config import settings

try:
    _fernets = [Fernet(key.strip().encode()) for key in settings.secret_key.split(",") if key.strip()]
    if not _fernets:
        raise ValueError("no keys configured")
    _fernet = MultiFernet(_fernets)
except Exception:
    # Keep the API process alive in a fail-closed configuration-error state.
    # encrypt()/decrypt() below remain unavailable until the key is corrected.
    _fernet = None


def _require_fernet() -> MultiFernet:
    if _fernet is None:
        raise RuntimeError("Credential encryption is unavailable because NBM_SECRET_KEY is not a valid Fernet key.")
    return _fernet


def encrypt(value: str) -> str:
    return _require_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _require_fernet().decrypt(value.encode()).decode()
