import argparse

from app import crypto, models
from app.database import SessionLocal


def rotate_secrets() -> int:
    db = SessionLocal()
    try:
        for instance in db.query(models.NetboxInstance).all():
            instance.api_token_encrypted = crypto.encrypt(crypto.decrypt(instance.api_token_encrypted))
        for target in db.query(models.GithubTarget).all():
            target.pat_encrypted = crypto.encrypt(crypto.decrypt(target.pat_encrypted))
        db.commit()
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=["rotate-secrets"])
    args = parser.parse_args()
    return rotate_secrets() if args.command == "rotate-secrets" else 2


if __name__ == "__main__":
    raise SystemExit(main())
