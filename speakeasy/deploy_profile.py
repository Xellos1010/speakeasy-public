"""Deployment profile: per-deployment settings that live outside the repository.

Resolution order for the profile file:
  1. $SPEAKEASY_PROFILE  -- a directory (uses <dir>/profile.json) or a path to a .json file
  2. $XDG_CONFIG_HOME/speakeasy/profile/profile.json  (default ~/.config/speakeasy/profile/profile.json)
  3. none -> built-in defaults (every accessor returns its fallback)

The profile holds hostnames, addresses, paths and the NAMES of secrets (env var names),
never secret values. Format and schema: docs/PROFILE.md and speakeasy/profile.schema.json.
This is distinct from the voice/portable profile under ~/.speakeasy/profile/.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("speakeasy.deploy_profile")

ENV_VAR = "SPEAKEASY_PROFILE"
FILE_NAME = "profile.json"
SCHEMA_PATH = Path(__file__).with_name("profile.schema.json")


class ProfileError(ValueError):
    """The profile file is unreadable or violates the schema."""


def default_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "speakeasy" / "profile"


def profile_path() -> Path | None:
    """Path of the profile file that would be loaded, or None if there is none."""
    env = os.environ.get(ENV_VAR)
    if env:
        p = Path(env).expanduser()
        p = p / FILE_NAME if p.is_dir() else p
        return p if p.is_file() else None
    p = default_dir() / FILE_NAME
    return p if p.is_file() else None


# --- minimal JSON-Schema subset validator (stdlib only) -------------------------------

def _resolve(schema: dict, root: dict) -> dict:
    ref = schema.get("$ref")
    if not ref:
        return schema
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _validate(value: Any, schema: dict, root: dict, path: str, errs: list[str]) -> None:
    schema = _resolve(schema, root)
    t = schema.get("type")
    checks = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if t in checks:
        ok = isinstance(value, checks[t]) and not (t == "integer" and isinstance(value, bool))
        if not ok:
            errs.append(f"{path or '$'}: expected {t}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path or '$'}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errs.append(f"{path}: too short")
        if len(value) > schema.get("maxLength", 10**9):
            errs.append(f"{path}: too long")
        pat = schema.get("pattern")
        if pat and not re.search(pat, value):
            errs.append(f"{path}: does not match the required format")
    if isinstance(value, list):
        for i, item in enumerate(value):
            if "items" in schema:
                _validate(item, schema["items"], root, f"{path}[{i}]", errs)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path or '$'}: missing required key {req!r}")
        addl = schema.get("additionalProperties", True)
        for k, v in value.items():
            sub = f"{path}.{k}" if path else k
            if k in props:
                _validate(v, props[k], root, sub, errs)
            elif addl is False:
                errs.append(f"{sub}: unknown key")
            elif isinstance(addl, dict):
                _validate(v, addl, root, sub, errs)


def validate(data: Any) -> list[str]:
    """Return a list of human-readable schema violations (empty = valid)."""
    schema = json.loads(SCHEMA_PATH.read_text())
    errs: list[str] = []
    _validate(data, schema, schema, "", errs)
    return errs


# --- profile object ------------------------------------------------------------------

@dataclass
class DeployProfile:
    data: dict = field(default_factory=dict)
    source: Path | None = None

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str, default: Path | None = None) -> Path | None:
        v = self.get(dotted)
        return Path(str(v)).expanduser() if v else default

    def secret_from_env(self, dotted_env_name_key: str) -> str | None:
        """Value of the env var whose NAME the profile stores at the given key. The value is
        returned to the caller and never logged or printed by this module."""
        name = self.get(dotted_env_name_key)
        return os.environ.get(name) if name else None


def load(strict: bool = False) -> DeployProfile:
    """Load the active profile. Absent -> empty defaults. Invalid -> ProfileError when strict,
    otherwise a warning and empty defaults (the app must still start)."""
    p = profile_path()
    if p is None:
        return DeployProfile()
    try:
        data = json.loads(p.read_text())
        errs = validate(data)
        if errs:
            raise ProfileError("; ".join(errs))
        return DeployProfile(data=data, source=p)
    except (OSError, ValueError) as e:
        if strict:
            raise ProfileError(f"{p}: {e}") from e
        log.warning("ignoring invalid deployment profile %s: %s", p, e)
        return DeployProfile()


def accounts_path() -> Path:
    """Claude accounts file for rewrite modes (profile accounts.accounts_file, else
    ~/.claude-accounts.json). Evaluated at call time."""
    return load().path("accounts.accounts_file", Path.home() / ".claude-accounts.json")  # type: ignore[return-value]
