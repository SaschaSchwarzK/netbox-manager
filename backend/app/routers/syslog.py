from fastapi import APIRouter

from app import schemas
from app.config import settings
from app.services import syslog_client

router = APIRouter(prefix="/api/syslog", tags=["syslog"])


@router.get("/settings", response_model=schemas.SyslogSettingsOut)
def get_settings():
    return schemas.SyslogSettingsOut(
        enabled=settings.syslog_enabled,
        protocol=settings.syslog_protocol,
        host=settings.syslog_host,
        port=settings.syslog_port,
        facility=settings.syslog_facility,
        app_name=settings.syslog_app_name,
    )


@router.post("/test", response_model=schemas.SyslogTestResult)
def test_syslog():
    result = syslog_client.send_test_message()
    return schemas.SyslogTestResult(**result)
