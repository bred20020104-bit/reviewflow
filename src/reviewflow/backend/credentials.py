"""Explicit private model/Key file access; no CWD discovery, network or logging.

The simplified configuration reads this private TOML for model metadata. Only the
separate Key resolver returns a SecretStr. Default inventory/demo never load it.
"""

import os
import stat
import tomllib
from pathlib import Path

from pydantic import SecretStr

from reviewflow.backend.config import ConfigError, ModelConfig, configured

MODEL_FIELDS = frozenset({"provider", "api_type", "model", "base_url"})
PRIVATE_FIELDS = MODEL_FIELDS | {"api_key"}
ERROR = "Private configuration rejected; check fields, placeholders and owner-only permissions."


def _read_private_file(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_mode & 0o077
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or before.st_size > 16384
            ):
                raise ValueError()
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(16385)
            after = os.fstat(fd)
            if len(data) > 16384 or (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError()
            values = tomllib.loads(data.decode("utf-8"))
            if set(values) not in ({"api_key"}, PRIVATE_FIELDS):
                raise ValueError()
            if any(type(value) is not str for value in values.values()):
                raise ValueError()
            return values
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise ConfigError(ERROR) from None


def read_private_model(path: Path) -> ModelConfig:
    """Read only metadata into the returned object; Key may still be a placeholder."""
    try:
        values = _read_private_file(path)
        if set(values) != PRIVATE_FIELDS:
            raise ValueError()
        return ModelConfig.model_validate(
            {
                **{name: values[name] for name in MODEL_FIELDS},
                "api_key_file": Path(path).name,
            }
        )
    except ValueError:
        raise ConfigError(ERROR) from None


def read_key_file(path: Path) -> SecretStr:
    """Supports both the unified model file and legacy Key-only TOML."""
    try:
        key = configured(_read_private_file(path)["api_key"])
        if len(key) > 4096 or any(c.isspace() for c in key):
            raise ValueError()
        return SecretStr(key)
    except ValueError:
        raise ConfigError(ERROR) from None


def resolve_key_file(config_path, model) -> SecretStr:
    if model.api_key_file is None:
        raise ConfigError("No file credential reference configured")
    # A filename in the explicitly selected trusted config directory, never target CWD.
    return read_key_file(Path(config_path).parent.resolve(strict=True) / model.api_key_file)
