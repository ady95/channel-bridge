"""설정 로딩.

두 갈래로 나뉜다.
  - Settings      : 런타임 환경변수 (DB 접속, 로그 레벨)
  - AppConfig     : 브릿지 정의 YAML (워크스페이스, 브릿지, 옵션)

자격증명은 YAML 에 담지 않는다. YAML 은 환경변수 **이름**만 참조하고
(`token_env`), 실제 값은 환경에서 읽는다. (NFR-3)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from chbridge.cir import Platform

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEV_DIR = PROJECT_ROOT / "dev"


def load_env_files(*paths: Path) -> None:
    """개발 편의용 .env 로더.

    이미 환경에 있는 값은 덮어쓰지 않는다. 컨테이너 배포에서는 이 함수가
    아무 것도 하지 않고 실제 환경변수가 그대로 쓰인다.
    """
    targets = paths or (DEV_DIR / ".env", DEV_DIR / ".tokens.env")
    for path in targets:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    database_url: str = Field(default="", validation_alias="CHBRIDGE_DATABASE_URL")
    log_level: str = Field(default="INFO", validation_alias="CHBRIDGE_LOG_LEVEL")
    log_json: bool = Field(default=False, validation_alias="CHBRIDGE_LOG_JSON")
    bridges_file: Path = Field(
        default=DEV_DIR / "bridges.yaml", validation_alias="CHBRIDGE_BRIDGES_FILE"
    )

    # 개발 환경 조립용. CHBRIDGE_DATABASE_URL 이 없을 때만 쓴다.
    host_addr: str = Field(default="127.0.0.1", validation_alias="HOST_ADDR")
    bridge_db_port: str = Field(default="5433", validation_alias="BRIDGE_DB_PORT")
    bridge_db_password: str = Field(default="", validation_alias="BRIDGE_DB_PASSWORD")

    # 이벤트 처리 실패 시 재시도 한계. 초과하면 DLQ(status='dead')로 보낸다.
    max_attempts: int = Field(default=5, validation_alias="CHBRIDGE_MAX_ATTEMPTS")

    # 주기적 정합성 스윕 간격(초). 재연결이 우리 모르게 일어나도 유실을 메운다.
    reconcile_seconds: float = Field(default=60.0, validation_alias="CHBRIDGE_RECONCILE_SECONDS")

    @model_validator(mode="after")
    def _compose_dsn(self) -> Self:
        if not self.database_url and self.bridge_db_password:
            dsn = (
                f"postgresql://bridge:{self.bridge_db_password}"
                f"@{self.host_addr}:{self.bridge_db_port}/bridge"
            )
            object.__setattr__(self, "database_url", dsn)
        return self


# ============================================================ 브릿지 정의 YAML


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    platform: Platform
    alias: str
    # Mattermost 전용. Slack 은 API 엔드포인트가 고정이라 불필요.
    base_url: str | None = None
    # 자격증명이 담긴 환경변수 이름. 값 자체를 YAML 에 쓰지 않는다.
    token_env: str
    # Slack Socket Mode 전용 App-Level Token 환경변수 이름.
    app_token_env: str | None = None

    def token(self) -> str:
        value = os.environ.get(self.token_env, "")
        if not value:
            raise ConfigError(f"워크스페이스 {self.id!r}: 환경변수 {self.token_env} 가 비어 있음")
        return value

    def app_token(self) -> str | None:
        if self.app_token_env is None:
            return None
        value = os.environ.get(self.app_token_env, "")
        if not value:
            raise ConfigError(
                f"워크스페이스 {self.id!r}: 환경변수 {self.app_token_env} 가 비어 있음"
            )
        return value

    @model_validator(mode="after")
    def _check_platform_fields(self) -> Self:
        if self.platform is Platform.MATTERMOST and not self.base_url:
            raise ValueError(f"워크스페이스 {self.id!r}: mattermost 는 base_url 이 필요합니다")
        if self.platform is Platform.SLACK and not self.app_token_env:
            raise ValueError(
                f"워크스페이스 {self.id!r}: slack 은 app_token_env(Socket Mode) 가 필요합니다"
            )
        return self


class EndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str
    # 사람이 읽는 채널 이름. 기동 시 channel_id 로 해석한다.
    channel: str | None = None
    # Mattermost 는 채널 이름이 팀 범위에서만 유일하므로 team 이 필요하다.
    # Slack 은 워크스페이스 전역이라 불필요.
    team: str | None = None
    # 이름 해석을 건너뛰려면 id 를 직접 지정한다.
    channel_id: str | None = None

    @model_validator(mode="after")
    def _need_one(self) -> Self:
        if not self.channel and not self.channel_id:
            raise ValueError("channel 또는 channel_id 중 하나는 있어야 합니다")
        return self


class BridgeOptions(BaseModel):
    """브릿지별 토글 (FR-1.3)."""

    model_config = ConfigDict(extra="forbid")

    relay_edits: bool = True
    relay_deletes: bool = True
    relay_files: bool = True
    # Phase 3 에서 구현. 그때까지 기본 비활성.
    relay_reactions: bool = False
    # 봇/웹훅/앱이 보낸 메시지 전달 여부 (FR-2.4)
    relay_bot_messages: bool = False
    # @channel / @here 가 반대편에서 실제 알림을 유발할지
    allow_channel_mentions: bool = False


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    endpoints: list[EndpointConfig]
    options: BridgeOptions = Field(default_factory=BridgeOptions)
    paused: bool = False

    @model_validator(mode="after")
    def _need_two_endpoints(self) -> Self:
        if len(self.endpoints) < 2:
            raise ValueError(f"브릿지 {self.id!r}: Endpoint 가 2개 이상이어야 합니다")
        return self


class ConfigError(Exception):
    pass


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaces: list[WorkspaceConfig]
    bridges: list[BridgeConfig]

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        known = {w.id for w in self.workspaces}
        if len(known) != len(self.workspaces):
            raise ValueError("workspaces 에 중복 id 가 있습니다")

        bridge_ids = {b.id for b in self.bridges}
        if len(bridge_ids) != len(self.bridges):
            raise ValueError("bridges 에 중복 id 가 있습니다")

        # 토폴로지 검증 (PRD 5.1): 한 채널은 하나의 브릿지에만 속한다.
        # DB 제약으로도 막지만, 기동 즉시 알려주는 편이 낫다.
        seen: dict[tuple[str, str], str] = {}
        for bridge in self.bridges:
            for ep in bridge.endpoints:
                if ep.workspace not in known:
                    raise ValueError(
                        f"브릿지 {bridge.id!r}: 알 수 없는 워크스페이스 {ep.workspace!r}"
                    )
                ref = (ep.workspace, ep.channel or ep.channel_id or "")
                if ref in seen:
                    raise ValueError(
                        f"채널 {ref[0]}/{ref[1]} 이 브릿지 {seen[ref]!r} 와 "
                        f"{bridge.id!r} 에 중복 등록되었습니다. "
                        f"루프가 발생하므로 허용되지 않습니다."
                    )
                seen[ref] = bridge.id
        return self

    def workspace(self, workspace_id: str) -> WorkspaceConfig:
        for w in self.workspaces:
            if w.id == workspace_id:
                return w
        raise ConfigError(f"알 수 없는 워크스페이스: {workspace_id!r}")

    @classmethod
    def load(cls, path: Path) -> Self:
        if not path.exists():
            raise ConfigError(f"브릿지 정의 파일이 없습니다: {path}")
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ConfigError(f"브릿지 정의 파일 형식이 잘못되었습니다: {path}")
        return cls.model_validate(raw)
