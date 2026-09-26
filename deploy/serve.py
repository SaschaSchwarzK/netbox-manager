"""Run the API and Caddy in one minimal, shell-free container."""
from __future__ import annotations

import datetime
import ipaddress
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


processes: list[subprocess.Popen] = []
stopping = False
CERTIFICATE_PATH = Path("/app/certs/cert.pem")
PRIVATE_KEY_PATH = Path("/app/certs/key.pem")


def ensure_certificate() -> None:
    """Create the fixed-path development certificate when the volume is empty."""
    if CERTIFICATE_PATH.is_file() and PRIVATE_KEY_PATH.is_file():
        return

    CERTIFICATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NetBox Manager"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("netbox-manager"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address("::1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )

    PRIVATE_KEY_PATH.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(PRIVATE_KEY_PATH, 0o600)
    CERTIFICATE_PATH.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    os.chmod(CERTIFICATE_PATH, 0o644)
    print(f"Generated self-signed TLS certificate at {CERTIFICATE_PATH}", flush=True)


def stop_processes(*_args) -> None:
    global stopping
    if stopping:
        return
    stopping = True
    for process in processes:
        if process.poll() is None:
            process.terminate()


def main() -> int:
    signal.signal(signal.SIGTERM, stop_processes)
    signal.signal(signal.SIGINT, stop_processes)
    ensure_certificate()

    processes.extend(
        [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                    "--no-access-log",
                ]
            ),
            subprocess.Popen(
                [
                    "/usr/bin/caddy",
                    "run",
                    "--config",
                    "/app/Caddyfile",
                    "--adapter",
                    "caddyfile",
                ]
            ),
        ]
    )

    exit_code = 0
    while not stopping:
        for process in processes:
            result = process.poll()
            if result is not None:
                exit_code = result or 1
                stop_processes()
                break
        time.sleep(0.25)

    deadline = time.monotonic() + 10
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
