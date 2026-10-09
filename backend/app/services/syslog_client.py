"""
Forwards audit-log entries to a remote syslog server as structured RFC 5424
messages. Entirely config-driven (see Settings) and not editable from the
UI by design — this is infrastructure wiring, not a per-user preference.

Forwarding is hooked onto the ORM itself (an `after_insert` event on
DeviceTypePushHistory) rather than called explicitly at each of the several
places that write an audit entry, so a new audit-logging call site added
later doesn't silently forget to forward — it happens automatically, once,
in one place.
"""
import os
import socket
from datetime import datetime, timezone

from sqlalchemy import event

from app.config import settings
from app.models import AuditEvent, DeviceTypePushHistory

FACILITY_CODES = {
    "kern": 0, "user": 1, "mail": 2, "daemon": 3, "auth": 4, "syslog": 5,
    "lpr": 6, "news": 7, "uucp": 8, "cron": 9, "authpriv": 10, "ftp": 11,
    "local0": 16, "local1": 17, "local2": 18, "local3": 19,
    "local4": 20, "local5": 21, "local6": 22, "local7": 23,
}

# RFC 5424's own example enterprise number, reused here the same way the RFC
# itself does for a private structured-data element with no registered PEN.
_SD_ID = "auditEntry@32473"


def _escape_sd_value(value) -> str:
    if value is None:
        return "-"
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def build_rfc5424_message(entry: dict) -> str:
    facility_code = FACILITY_CODES.get(settings.syslog_facility, FACILITY_CODES["local0"])
    severity = 6 if entry.get("status") == "success" else 4  # informational vs. warning
    pri = facility_code * 8 + severity

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    hostname = (socket.gethostname() or "-")[:255]
    app_name = settings.syslog_app_name[:48] or "netbox-manager"
    procid = str(os.getpid())
    msgid = (entry.get("action_type") or "audit")[:32]

    structured_data = (
        f'[{_SD_ID} '
        f'actor="{_escape_sd_value(entry.get("actor_name"))}" '
        f'actorEmail="{_escape_sd_value(entry.get("actor_email"))}" '
        f'action="{_escape_sd_value(entry.get("action_type"))}" '
        f'target="{_escape_sd_value(entry.get("target_name"))}" '
        f'filePath="{_escape_sd_value(entry.get("file_path"))}" '
        f'status="{_escape_sd_value(entry.get("status"))}"]'
    )
    msg = entry.get("detail") or "-"

    return f"<{pri}>1 {timestamp} {hostname} {app_name} {procid} {msgid} {structured_data} {msg}"


def send_raw(message: str) -> None:
    """Raises on failure — callers decide whether that's fatal (test button) or swallowed (auto-forward)."""
    payload = message.encode("utf-8")
    if settings.syslog_protocol == "tcp":
        with socket.create_connection((settings.syslog_host, settings.syslog_port), timeout=3) as sock:
            sock.sendall(payload + b"\n")  # RFC 6587 non-transparent (newline-delimited) framing
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(3)
            sock.sendto(payload, (settings.syslog_host, settings.syslog_port))
        finally:
            sock.close()


def send_audit_entry(entry: dict) -> None:
    if not settings.syslog_enabled or not settings.syslog_host:
        return
    try:
        send_raw(build_rfc5424_message(entry))
    except Exception:
        # A syslog delivery failure must never break the actual audit-log
        # write it's shadowing, or the request that triggered it.
        pass


def send_test_message() -> dict:
    if not settings.syslog_enabled:
        return {"ok": False, "detail": "Syslog forwarding is disabled (NBM_SYSLOG_ENABLED is not set)."}
    if not settings.syslog_host:
        return {"ok": False, "detail": "No syslog host configured (NBM_SYSLOG_HOST is empty)."}
    message = build_rfc5424_message({
        "action_type": "test", "target_name": "-", "file_path": "-",
        "status": "success", "detail": "Test message from NetBox Manager.",
        "actor_name": "-", "actor_email": None,
    })
    try:
        send_raw(message)
        return {"ok": True, "detail": f"Sent via {settings.syslog_protocol.upper()} to {settings.syslog_host}:{settings.syslog_port}."}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


@event.listens_for(DeviceTypePushHistory, "after_insert")
def _forward_audit_entry_to_syslog(mapper, connection, target: DeviceTypePushHistory) -> None:
    send_audit_entry({
        "action_type": target.target_type,
        "target_name": target.target_name,
        "file_path": target.file_path,
        "status": target.status,
        "detail": target.detail,
        "actor_name": target.actor_name,
        "actor_email": target.actor_email,
    })


@event.listens_for(AuditEvent, "after_insert")
def _forward_generic_audit_event(mapper, connection, target: AuditEvent) -> None:
    send_audit_entry({
        "action_type": target.action, "target_type": target.resource_type,
        "target_name": target.resource_id or "application", "status": target.status,
        "detail": target.detail, "actor_sub": target.actor_sub,
        "actor_name": target.actor_name, "actor_email": target.actor_email,
    })
