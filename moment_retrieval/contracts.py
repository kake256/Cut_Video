from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ContractError:
    code: str
    message: str
    details: dict[str, Any] | None = None


def success(command: str, data: dict[str, Any], warnings: list[str] | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "command": command,
        "data": data,
        "warnings": warnings or [],
    }


def failure(command: str, error: ContractError) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "command": command,
        "error": asdict(error),
    }


def public_artifact_path(path: Path) -> str:
    """CLI is local, but keep serialization centralized for a future HTTP boundary."""
    return str(Path(path))
