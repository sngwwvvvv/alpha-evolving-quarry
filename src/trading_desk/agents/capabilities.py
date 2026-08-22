from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from trading_desk.state.db import Database
from trading_desk.validation.gates import ResultBundle
from trading_desk.validation.oos import SealedOos

ALLOWED_KEYS = {
    "orchestrator": frozenset({"action", "job_ref", "worker_profile"}),
    "research": frozenset({"development", "invariants", "ledger", "public_sources"}),
    "coding": frozenset({"allow_dependencies", "development", "invariants", "mutation", "worktree"}),
    "analysis-ledger": frozenset({"result"}),
}
CREDENTIAL_TOKENS = frozenset(
    {"api_key", "apikey", "authorization", "credential", "credentials", "password", "secret", "token"}
)
PAPER_TOKENS = frozenset({"paper"})
OOS_TOKENS = frozenset({"holdout", "oos", "sealed"})
DB_TOKENS = frozenset({"database", "db", "sqlite", "sqlite3"})
WORKER_PROFILES = frozenset({"analysis-ledger", "coding", "research"})
FORBIDDEN_ORCHESTRATOR_ACTIONS = frozenset(
    {"approve_paper", "control_positions", "decide_validation", "modify_strategy", "read_oos"}
)
_SECRET_ENV = ("DEEPSEEK_API_KEY", "KIMI_API_KEY")
_DB_PATH_RE = re.compile(r"\.(sqlite3?|db)(?:\b|$)", re.IGNORECASE)


class CapabilityError(ValueError):
    """Profile input or envelope violated a capability boundary."""


def _tokens(key: Any) -> set[str]:
    raw = str(key).lower()
    parts = {part for part in re.split(r"[^a-z0-9]+", raw) if part}
    parts.add(raw)
    parts.add(re.sub(r"[^a-z0-9]+", "", raw))
    return parts


def reject_credentials(value: Any) -> None:
    secrets = [os.environ.get(name) or "" for name in _SECRET_ENV]
    secrets = [item for item in secrets if len(item) >= 8]

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, item in node.items():
                if _tokens(key) & CREDENTIAL_TOKENS:
                    raise CapabilityError("credential cannot enter envelopes or artifacts")
                walk(item)
            return
        if isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)
            return
        if isinstance(node, str):
            for secret in secrets:
                if secret in node:
                    raise CapabilityError("credential cannot enter envelopes or artifacts")

    walk(value)


def _scan(profile: str, value: Any, *, allow_oos: bool | None = None, _seen: set[int] | None = None) -> None:
    allow = profile == "analysis-ledger" if allow_oos is None else allow_oos
    if value is None or isinstance(value, (int, float, bool, bytes)):
        return
    seen = _seen if _seen is not None else set()
    marker = id(value)
    if marker in seen:
        return
    if not isinstance(value, (str, int, float, bool)):
        seen.add(marker)
    if isinstance(value, SealedOos) or type(value).__name__ == "SealedOos":
        if not allow:
            raise CapabilityError("OOS artifacts cannot enter this profile bundle")
        return
    if isinstance(value, ResultBundle):
        if value.kind.lower() == "oos" and not allow:
            raise CapabilityError("OOS artifacts cannot enter this profile bundle")
        _scan(profile, value.to_payload(), allow_oos=allow, _seen=seen)
        return
    if type(value).__name__ == "LedgerBundle":
        if str(getattr(value, "kind", "")).lower() == "oos" and not allow:
            raise CapabilityError("OOS artifacts cannot enter this profile bundle")
        payload = value.to_payload() if hasattr(value, "to_payload") else None
        if payload is not None:
            _scan(profile, payload, allow_oos=allow, _seen=seen)
        return
    if isinstance(value, Database) or type(value).__name__ == "Database":
        raise CapabilityError("database cannot enter agent bundles")
    if isinstance(value, sqlite3.Connection):
        raise CapabilityError("database cannot enter agent bundles")
    if "paper" in type(value).__name__.lower():
        raise CapabilityError("paper cannot enter agent bundles")
    if isinstance(value, Mapping):
        for key, item in value.items():
            tokens = _tokens(key)
            if not allow and tokens & OOS_TOKENS:
                raise CapabilityError("OOS artifacts cannot enter this profile bundle")
            if tokens & PAPER_TOKENS:
                raise CapabilityError("paper cannot enter agent bundles")
            if tokens & DB_TOKENS:
                raise CapabilityError("database cannot enter agent bundles")
            if tokens & CREDENTIAL_TOKENS:
                raise CapabilityError("credential cannot enter agent bundles")
            _scan(profile, item, allow_oos=allow, _seen=seen)
        if not allow and str(value.get("kind", "")).lower() == "oos":
            raise CapabilityError("OOS artifacts cannot enter this profile bundle")
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _scan(profile, item, allow_oos=allow, _seen=seen)
        return
    if isinstance(value, str) and _DB_PATH_RE.search(value):
        raise CapabilityError("database cannot enter agent bundles")


def _materialize(value: Any) -> Any:
    if isinstance(value, ResultBundle) or hasattr(value, "to_payload"):
        return value.to_payload()
    if isinstance(value, Mapping):
        return {str(key): _materialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_materialize(item) for item in value]
    return value


def _add(profile: str, payload: dict[str, Any], key: str, value: Any) -> None:
    _scan(profile, {key: value})
    if key not in ALLOWED_KEYS[profile]:
        raise CapabilityError(f"unsupported {profile} input")
    payload[key] = _materialize(value)


def _assemble(profile: str, *parts: Any, **kwargs: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for part in parts:
        if isinstance(part, ResultBundle):
            key = "result" if profile == "analysis-ledger" else "development"
            _add(profile, payload, key, part)
        elif isinstance(part, SealedOos) or type(part).__name__ == "SealedOos":
            _scan(profile, part)
        elif isinstance(part, Mapping):
            for key, value in part.items():
                _add(profile, payload, str(key), value)
        elif part is not None:
            raise CapabilityError("unsupported input")
    for key, value in kwargs.items():
        _add(profile, payload, str(key), value)
    reject_credentials(payload)
    return payload


def _require_worktree(worktree: Any) -> dict[str, Any]:
    if not isinstance(worktree, Mapping):
        raise CapabilityError("coding requires disposable worktree metadata")
    path = str(worktree.get("path") or "").strip()
    version_id = str(worktree.get("version_id") or "").strip()
    if worktree.get("disposable") is not True or not path or not version_id:
        raise CapabilityError("coding requires disposable worktree metadata")
    _scan("coding", worktree)
    return {"disposable": True, "path": path, "version_id": version_id}


def orchestrator_inputs(
    *,
    action: str,
    worker_profile: str,
    job_ref: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    _scan("orchestrator", kwargs)
    if action in FORBIDDEN_ORCHESTRATOR_ACTIONS:
        raise CapabilityError(f"forbidden orchestrator action: {action}")
    if worker_profile not in WORKER_PROFILES:
        raise CapabilityError("unknown worker profile")
    payload: dict[str, Any] = {"action": action, "worker_profile": worker_profile}
    if job_ref is not None:
        payload["job_ref"] = job_ref
    return _assemble("orchestrator", payload, **kwargs)


def research_inputs(*parts: Any, **kwargs: Any) -> dict[str, Any]:
    return _assemble("research", *parts, **kwargs)


def coding_inputs(*parts: Any, worktree: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    if worktree is None:
        worktree = kwargs.pop("worktree", None)
    checked = _require_worktree(worktree)
    allow_dependencies = bool(kwargs.pop("allow_dependencies", False))
    payload = _assemble("coding", *parts, **kwargs)
    payload["allow_dependencies"] = allow_dependencies
    payload["worktree"] = checked
    return payload


def analysis_ledger_inputs(*parts: Any, **kwargs: Any) -> dict[str, Any]:
    for key in kwargs:
        if _tokens(key) & CREDENTIAL_TOKENS:
            raise CapabilityError("credential cannot enter agent bundles")
    result = kwargs.get("result")
    extras = {key: value for key, value in kwargs.items() if key != "result"}
    if parts:
        if result is not None or len(parts) != 1:
            raise CapabilityError("analysis-ledger accepts a result bundle only")
        result = parts[0]
    if extras or not isinstance(result, ResultBundle):
        raise CapabilityError("analysis-ledger accepts a result bundle only")
    _scan("analysis-ledger", result, allow_oos=True)
    reject_credentials({"result": result.to_payload()})
    return {"result": result.to_payload()}
