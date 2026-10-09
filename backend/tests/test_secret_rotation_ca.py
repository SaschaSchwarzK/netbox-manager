import importlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from app import config, models
from app.routers.instances import _validate_ca_bundle
from app.services import netbox_client


def certificate_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "private-ca")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_multifernet_decrypts_old_key_and_encrypts_with_first(monkeypatch):
    import app.crypto as crypto
    original = config.settings.secret_key
    new_key, old_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(config.settings, "secret_key", f"{new_key},{old_key}")
    importlib.reload(crypto)
    old_ciphertext = Fernet(old_key.encode()).encrypt(b"secret").decode()
    assert crypto.decrypt(old_ciphertext) == "secret"
    assert Fernet(new_key.encode()).decrypt(crypto.encrypt("rotated").encode()) == b"rotated"
    monkeypatch.setattr(config.settings, "secret_key", original)
    importlib.reload(crypto)


def test_ca_bundle_is_validated_and_written_mode_0600(tmp_path, monkeypatch):
    pem = certificate_pem()
    assert _validate_ca_bundle(pem) == pem
    instance = models.NetboxInstance(id="test-ca", name="test", base_url="https://netbox",
                                     api_token_encrypted="x", ca_bundle_pem=pem)
    path = netbox_client.verify_for_instance(instance)
    import os
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_invalid_ca_bundle_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_ca_bundle("not a certificate")
    assert exc.value.status_code == 422
