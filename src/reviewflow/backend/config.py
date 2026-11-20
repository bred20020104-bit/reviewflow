"""Explicit, bounded TOML and private-model loading. No implicit discovery or network."""

import errno
import json
import os
import re
import stat
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, ValidationError, field_validator, model_validator

from reviewflow.review.contracts import RelativePath, ReviewMessage, _unique


class ConfigError(ValueError):
    pass


def configured(value: str) -> str:
    if not value.strip() or "TODO" in value.upper() or any(ord(c) < 32 for c in value):
        raise ValueError("Unconfigured value")
    return value


def https_url(value: str) -> str:
    configured(value)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError("Only explicit credential-free HTTPS endpoints are allowed")
    return value


def money(value: str) -> str:
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,12})?", value):
        raise ValueError("Expected finite nonnegative decimal string")
    return value


Text = Annotated[str, Field(min_length=1, max_length=512), AfterValidator(configured)]
Money = Annotated[str, AfterValidator(money)]


class ModelConfig(ReviewMessage):
    provider: Text
    api_type: Text
    model: Text
    base_url: Annotated[Text, AfterValidator(https_url)]
    api_key_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")] | None = None
    api_key_file: (
        Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.toml$")] | None
    ) = None

    @model_validator(mode="after")
    def one_credential_source(self) -> Self:
        if (self.api_key_env is None) == (self.api_key_file is None):
            raise ValueError("Select exactly one credential reference")
        return self


class BudgetConfig(ReviewMessage):
    max_run_cost_usd: Money = "1.00"
    max_call_cost_usd: Money = "0.25"
    # Unknown prices are not zero/free: transport refuses sends until resolved.
    input_price_per_million_usd: Money | None = None
    output_price_per_million_usd: Money | None = None
    max_input_tokens: Annotated[int, Field(ge=1, le=1000000)] = 16000
    max_output_tokens: Annotated[int, Field(ge=1, le=100000)] = 4000
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 60
    max_retries: Literal[0] = 0
    max_calls: Annotated[int, Field(ge=1, le=100)] = 12

    @field_validator("max_retries", mode="before")
    @classmethod
    def strict_retries(cls, value):
        if type(value) is not int:
            raise ValueError("Retries must be an integer")
        return value

    @model_validator(mode="after")
    def valid_caps(self) -> Self:
        if not (0 < Decimal(self.max_call_cost_usd) <= Decimal(self.max_run_cost_usd)):
            raise ValueError("Invalid budget caps")
        if type(self.max_retries) is not int:
            raise ValueError("Invalid retries")
        return self


class PrivacyConfig(ReviewMessage):
    allowed_project_root: Text
    allowed_paths: Annotated[
        tuple[RelativePath, ...], Field(min_length=1, max_length=200), AfterValidator(_unique)
    ]
    allow_code_upload: bool = False
    allow_telemetry: Literal[False] = False

    @field_validator("allow_telemetry", mode="before")
    @classmethod
    def strict_telemetry(cls, value):
        if type(value) is not bool:
            raise ValueError("Telemetry must be boolean")
        return value

    @model_validator(mode="after")
    def absolute_root(self) -> Self:
        if not Path(self.allowed_project_root).is_absolute():
            raise ValueError("Root must be absolute")
        if type(self.allow_telemetry) is not bool:
            raise ValueError("Invalid telemetry flag")
        return self


class AppConfig(ReviewMessage):
    model: ModelConfig
    budget: BudgetConfig
    privacy: PrivacyConfig


class LocalReference(ReviewMessage):
    secrets_file: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.toml$")]


class LocalSettings(ReviewMessage):
    """Private-file model metadata plus defaults; no Key and no preauthorized scope."""

    model: ModelConfig
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    def bind(self, source, inventory) -> AppConfig:
        try:
            return AppConfig(
                model=self.model,
                budget=self.budget,
                privacy=PrivacyConfig(
                    allowed_project_root=str(source.info.root.resolve()),
                    allowed_paths=tuple(
                        r.path for r in inventory.snapshot.records if r.status == "included"
                    ),
                ),
            )
        except ValueError:
            raise ConfigError("Selected project has no valid included scope") from None


def open_regular_nofollow(path: Path) -> int:
    """Open one regular file. A final symlink is never followed."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif stat.S_ISLNK(os.lstat(path).st_mode):
        raise OSError(errno.ELOOP, "Refusing to follow a symlink", os.fspath(path))
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return os.open(os.fspath(path), flags)


def same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    """True when the open file did not change. Windows fstat creation time can drift."""
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        return False
    if os.name != "posix":
        return True
    return before.st_ino == after.st_ino and before.st_ctime_ns == after.st_ctime_ns


def read_bounded(path: Path, limit: int = 131072) -> bytes:
    """Reject final symlinks and nonregular files before bounded reading."""
    fd = open_regular_nofollow(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("Invalid input file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        if len(data) > limit or not same_file_snapshot(before, after):
            raise ValueError("Input changed or exceeds limit")
        return data
    finally:
        os.close(fd)


def parse_config(data: dict) -> AppConfig:
    try:
        # TOML arrays become JSON arrays; strict field scalars remain strict.
        return AppConfig.model_validate_json(json.dumps(data, allow_nan=False))
    except (ValueError, TypeError, ValidationError, OverflowError):
        raise ConfigError(
            "Invalid configuration: complete placeholders and check schema."
        ) from None


def load_config(path: Path) -> AppConfig | LocalSettings:
    try:
        data = tomllib.loads(read_bounded(path).decode("utf-8"))
        if "secrets_file" in data:
            from reviewflow.backend.credentials import read_private_model

            reference = LocalReference.model_validate(data)
            private_path = Path(path).parent.resolve(strict=True) / reference.secrets_file
            return LocalSettings(model=read_private_model(private_path))
        return parse_config(data)
    except (OSError, ValueError):
        raise ConfigError(
            "Invalid configuration: complete placeholders and check schema."
        ) from None
