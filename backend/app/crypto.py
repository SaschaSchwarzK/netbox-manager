from cryptography.fernet import Fernet

from app.config import settings

try:
    _fernet = Fernet(settings.secret_key.encode())
except Exception as exc:
    raise RuntimeError(
        "NBM_SECRET_KEY is not set to a valid Fernet key (this happens if backend/.env "
        "was never created from backend/.env.example, or still has the placeholder value). "
        "Generate one with:\n"
        '  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
        "and set NBM_SECRET_KEY to the result in backend/.env, then restart."
    ) from exc


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()
