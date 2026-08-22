from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from trading_desk.config import canonical_json, sha256_hex

SCHEMA_VERSION = "agent-envelope-v1"
ANALYSIS_SCHEMA = "analysis-ledger-v1"
AGENT_ERROR = "AGENT_ERROR"
OK = "OK"
PROFILES = ("orchestrator", "research", "coding", "analysis-ledger")
DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class SchemaError(ValueError):
    """Strict JSON schema violation."""


def _is_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def validate_json_schema(schema: Mapping[str, Any], instance: Any, *, path: str = "$") -> None:
    if "const" in schema and instance != schema["const"]:
        raise SchemaError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path} is not an allowed value")
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(instance, item) for item in types):
            raise SchemaError(f"{path} has invalid type")
    if "pattern" in schema:
        if not isinstance(instance, str) or re.fullmatch(str(schema["pattern"]), instance) is None:
            raise SchemaError(f"{path} does not match pattern")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            raise SchemaError(f"{path} is too short")
        if "maxLength" in schema and len(instance) > int(schema["maxLength"]):
            raise SchemaError(f"{path} is too long")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool) and "minimum" in schema:
        if instance < schema["minimum"]:
            raise SchemaError(f"{path} is below minimum")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < int(schema["minItems"]):
            raise SchemaError(f"{path} has too few items")
        if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
            raise SchemaError(f"{path} has too many items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(instance):
                validate_json_schema(item_schema, item, path=f"{path}[{index}]")
    if not isinstance(instance, dict):
        return
    for key in schema.get("required", ()):
        if key not in instance:
            raise SchemaError(f"{path}.{key} is required")
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties", True)
    for key, value in instance.items():
        if key in properties:
            validate_json_schema(properties[key], value, path=f"{path}.{key}")
        elif additional is False:
            raise SchemaError(f"{path}.{key} is not allowed")
        elif isinstance(additional, Mapping):
            validate_json_schema(additional, value, path=f"{path}.{key}")


JOB_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "action",
        "artifact_refs",
        "input_bundle",
        "job_id",
        "logical_model",
        "pin",
        "profile",
        "provider",
        "schema_version",
    ],
    "properties": {
        "action": {"type": "string", "minLength": 1},
        "artifact_refs": {
            "type": "array",
            "items": {"type": "string", "pattern": DIGEST_PATTERN},
        },
        "input_bundle": {"type": "object"},
        "job_id": {"type": "string", "minLength": 1},
        "logical_model": {"type": "string", "minLength": 1},
        "pin": {"type": "string", "pattern": DIGEST_PATTERN},
        "profile": {"enum": list(PROFILES)},
        "provider": {"type": "string", "minLength": 1},
        "schema_version": {"const": SCHEMA_VERSION},
        "worktree": {
            "type": "object",
            "additionalProperties": False,
            "required": ["disposable", "path", "version_id"],
            "properties": {
                "disposable": {"const": True},
                "path": {"type": "string", "minLength": 1},
                "version_id": {"type": "string", "minLength": 1},
            },
        },
    },
}

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["artifacts", "job_id", "payload", "pin", "resolved_model_id", "status"],
    "properties": {
        "artifacts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["digest", "kind"],
                "properties": {
                    "digest": {"type": "string", "pattern": DIGEST_PATTERN},
                    "kind": {"type": "string", "minLength": 1},
                },
            },
        },
        "job_id": {"type": "string", "minLength": 1},
        "payload": {"type": "object"},
        "pin": {"type": "string", "pattern": DIGEST_PATTERN},
        "resolved_model_id": {"type": "string", "minLength": 1},
        "status": {"enum": [OK, AGENT_ERROR]},
    },
}

PAYLOAD_SCHEMAS: dict[str, dict[str, Any]] = {
    "orchestrator": {
        "type": "object",
        "additionalProperties": False,
        "required": ["result_handle", "worker_profile"],
        "properties": {
            "result_handle": {"type": "string", "minLength": 1},
            "worker_profile": {"enum": ["research", "coding", "analysis-ledger"]},
        },
    },
    "research": {
        "type": "object",
        "additionalProperties": False,
        "required": ["mutation"],
        "properties": {
            "mutation": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "expected_causal_effect",
                    "files_and_fields",
                    "hypothesis",
                    "invariant_diff",
                ],
                "properties": {
                    "expected_causal_effect": {"type": "string", "minLength": 1},
                    "files_and_fields": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "hypothesis": {"type": "string", "minLength": 1},
                    "invariant_diff": {"type": "string", "minLength": 1},
                },
            }
        },
    },
    "coding": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "added_dependencies",
            "changed_files",
            "commit",
            "invariant_check",
            "test_results",
        ],
        "properties": {
            "added_dependencies": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "changed_files": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "commit": {"type": "string", "minLength": 1},
            "invariant_check": {"const": "unchanged"},
            "test_results": {
                "type": "object",
                "additionalProperties": False,
                "required": ["failed", "passed"],
                "properties": {
                    "failed": {"type": "integer", "minimum": 0},
                    "passed": {"type": "boolean"},
                },
            },
        },
    },
    "analysis-ledger": {
        "type": "object",
        "additionalProperties": False,
        "required": ["analysis", "markdown_draft", "mutation", "schema"],
        "properties": {
            "analysis": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "loss_drivers": {"type": "array", "items": {"type": "string"}},
                },
            },
            "markdown_draft": {"type": "string", "minLength": 1},
            "mutation": {"type": "null"},
            "schema": {"const": ANALYSIS_SCHEMA},
        },
    },
}


def validate_job_envelope(envelope: Mapping[str, Any]) -> None:
    validate_json_schema(JOB_ENVELOPE_SCHEMA, envelope)
    if envelope["profile"] == "coding" and "worktree" not in envelope:
        raise SchemaError("$.worktree is required")
    if envelope["profile"] != "coding" and "worktree" in envelope:
        raise SchemaError("$.worktree is not allowed")


def validate_agent_response(profile: str, payload: Mapping[str, Any]) -> None:
    validate_json_schema(RESPONSE_SCHEMA, payload)
    if payload["status"] == OK:
        try:
            validate_json_schema(PAYLOAD_SCHEMAS[profile], payload["payload"])
        except KeyError as exc:
            raise SchemaError(f"unknown profile {profile}") from exc


@dataclass(frozen=True, slots=True)
class AgentJob:
    job_id: str
    profile: str
    action: str
    input_bundle: dict[str, Any]
    logical_model: str
    provider: str
    schema_version: str = SCHEMA_VERSION
    artifact_refs: tuple[str, ...] = ()
    worktree: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_bundle", dict(self.input_bundle))
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
        if self.worktree is not None:
            object.__setattr__(self, "worktree", dict(self.worktree))

    def pin_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "artifact_refs": list(self.artifact_refs),
            "input_bundle": self.input_bundle,
            "job_id": self.job_id,
            "logical_model": self.logical_model,
            "profile": self.profile,
            "provider": self.provider,
            "schema_version": self.schema_version,
        }
        if self.worktree is not None:
            payload["worktree"] = self.worktree
        return payload

    @property
    def pin(self) -> str:
        return sha256_hex(canonical_json(self.pin_payload()))

    def to_envelope(self) -> dict[str, Any]:
        envelope = self.pin_payload()
        envelope["pin"] = self.pin
        return envelope


@dataclass(frozen=True, slots=True)
class AgentResult:
    job_id: str
    status: str
    pin: str
    attempts: int
    payload: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[dict[str, str], ...] = ()
    resolved_model_id: str | None = None
    resolved_fingerprint: str | None = None
    reason: str | None = None
    profile: str = ""
    logical_model: str = ""
    provider: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "artifacts", tuple(dict(item) for item in self.artifacts))

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "artifacts": [dict(item) for item in self.artifacts],
            "job_id": self.job_id,
            "logical_model": self.logical_model,
            "payload": dict(self.payload),
            "pin": self.pin,
            "profile": self.profile,
            "provider": self.provider,
            "reason": self.reason,
            "resolved_fingerprint": self.resolved_fingerprint,
            "resolved_model_id": self.resolved_model_id,
            "status": self.status,
        }
