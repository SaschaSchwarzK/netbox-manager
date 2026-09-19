import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


class NetboxInstance(Base):
    __tablename__ = "netbox_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_csv: Mapped[str] = mapped_column(String(512), default="")
    requires_approved_pr: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    @property
    def tags(self) -> list[str]:
        return [t.strip() for t in self.tags_csv.split(",") if t.strip()]

    @tags.setter
    def tags(self, value: list[str]) -> None:
        self.tags_csv = ",".join(t.strip() for t in value if t.strip())


class GithubTarget(Base):
    __tablename__ = "github_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    repo: Mapped[str] = mapped_column(String(256), nullable=False)  # "org/repo"
    branch: Mapped[str] = mapped_column(String(128), default="main")
    path_pattern: Mapped[str] = mapped_column(
        String(256), default="device-types/{manufacturer}/{slug}.yml"
    )
    custom_fields_path: Mapped[str] = mapped_column(String(256), default="custom-fields/template.yml")
    pat_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DriftRecord(Base):
    __tablename__ = "drift_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    instance_id: Mapped[str] = mapped_column(String(36), ForeignKey("netbox_instances.id"), nullable=False)
    repo_target_id: Mapped[str] = mapped_column(String(36), ForeignKey("github_targets.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32))  # in_sync / drift / missing / error
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DeviceTypePushHistory(Base):
    __tablename__ = "push_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    repo_target_id: Mapped[str] = mapped_column(String(36), ForeignKey("github_targets.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32))  # "netbox" or "github"
    target_name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32))  # success/error
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
