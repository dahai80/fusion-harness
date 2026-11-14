"""DAG node execution mechanics (issue #35, K2, ported from cowork node.py).

Declarative node types, registry with duplicate rejection, strict param
coercion (kernel differs from cowork's never-fail coercion on purpose:
silent type drift is a mechanism-level bug here — errors name the field,
expected type, and received value). Zero model calls. Workflow composition
(edges/phases/HITL) stays in fusion-cowork by design.
"""

import inspect
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_JSON_TYPES = ("string", "integer", "number", "boolean", "array", "object")
_TRUE_STRINGS = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "n", "off"})


class NodeRegistryError(ValueError):
    """Unknown node type or duplicate registration."""


class ParamCoercionError(ValueError):
    """Strict coercion failed; .errors carries per-field messages."""

    def __init__(self, errors: List[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class NodeContext:
    run_id: str
    node_id: str
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("fusion.node"))
    artifact_store: Any = None
    checkpoint_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None

    def write_artifact(self, worker_name: str, content: str, fmt: str = "txt") -> str:
        if self.artifact_store is None:
            raise NodeRegistryError("no artifact store wired into NodeContext")
        return self.artifact_store.write_artifact(self.run_id, worker_name, content, fmt)

    def checkpoint(self, state: Dict[str, Any]) -> None:
        if self.checkpoint_hook is not None:
            self.checkpoint_hook(self.node_id, state)


@dataclass
class NodeResult:
    node_id: str = ""
    status: str = "success"
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0

    def validate(self) -> bool:
        return self.status == "success"


class BaseNode:
    node_type: str = ""
    description: str = ""
    inputs: Dict[str, Any] = {}
    outputs: Dict[str, Any] = {}

    def __init__(self, node_id: str = "") -> None:
        self.id = node_id or "node_%s" % uuid.uuid4().hex[:8]

    def validate(self, params: Dict[str, Any]) -> List[str]:
        return []

    def execute(self, ctx: NodeContext, params: Dict[str, Any]) -> Any:
        raise NotImplementedError("node %s does not implement execute" % self.node_type)


async def run_node(node: BaseNode, ctx: NodeContext, params: Dict[str, Any]) -> NodeResult:
    """Invoke node.execute, awaiting async bodies; errors become failed results."""
    started = time.monotonic()
    ctx.node_id = ctx.node_id or node.id
    try:
        outcome = node.execute(ctx, params)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        duration = int((time.monotonic() - started) * 1000)
        if isinstance(outcome, NodeResult):
            result = outcome
        elif isinstance(outcome, dict):
            result = NodeResult(node_id=ctx.node_id, data=outcome)
        else:
            result = NodeResult(
                node_id=ctx.node_id, status="failed", error="unsupported execute return type"
            )
        result.duration_ms = result.duration_ms or duration
        result.node_id = result.node_id or ctx.node_id
        return result
    except Exception as exc:
        duration = int((time.monotonic() - started) * 1000)
        logger.error("node %s execute failed: %s", node.node_type, exc)
        return NodeResult(
            node_id=ctx.node_id, status="failed", error=str(exc), duration_ms=duration
        )


def _describe(value: Any) -> str:
    return "%s (%r)" % (type(value).__name__, value)


def _coerce_integer(name: str, value: Any, errors: List[str]) -> Any:
    if isinstance(value, bool):
        errors.append("field '%s' expected integer, received bool" % name)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        errors.append("field '%s' expected integer, received non-integral float %r" % (name, value))
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            parsed = float(text)
        except ValueError:
            errors.append(
                "field '%s' expected integer, received str %r (not numeric)" % (name, value)
            )
            return value
        if parsed.is_integer():
            return int(parsed)
        errors.append("field '%s' expected integer, received str %r (non-integral)" % (name, value))
        return value
    errors.append("field '%s' expected integer, received %s" % (name, _describe(value)))
    return value


def _coerce_number(name: str, value: Any, errors: List[str]) -> Any:
    if isinstance(value, bool):
        errors.append("field '%s' expected number, received bool" % name)
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            errors.append("field '%s' expected number, received str %r" % (name, value))
            return value
    errors.append("field '%s' expected number, received %s" % (name, _describe(value)))
    return value


def _coerce_boolean(name: str, value: Any, errors: List[str]) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        errors.append("field '%s' expected boolean, received str %r" % (name, value))
        return value
    errors.append("field '%s' expected boolean, received %s" % (name, _describe(value)))
    return value


def _coerce_array(name: str, value: Any, errors: List[str]) -> Any:
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set, frozenset)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except ValueError:
                pass
        errors.append(
            "field '%s' expected array, received str %r (not a JSON array)" % (name, value)
        )
        return value
    errors.append("field '%s' expected array, received %s" % (name, _describe(value)))
    return value


def _coerce_object(name: str, value: Any, errors: List[str]) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            errors.append("field '%s' expected object, received str %r (not JSON)" % (name, value))
            return value
        if isinstance(parsed, dict):
            return parsed
    errors.append("field '%s' expected object, received %s" % (name, _describe(value)))
    return value


def _coerce_enum(name: str, value: Any, choices: List[Any], errors: List[str]) -> Any:
    if value in choices:
        return value
    errors.append("field '%s' expected one of %r, received %r" % (name, choices, value))
    return value


def coerce_params(params: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    """Strict coercion against a JSON-schema-shaped spec; raises
    ParamCoercionError listing every field + expected + received on failure.
    Keys without a declared type pass through untouched."""
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    errors: List[str] = []
    coerced: Dict[str, Any] = {}
    for key, value in params.items():
        decl = properties.get(key)
        if not isinstance(decl, dict):
            coerced[key] = value
            continue
        decl_type = decl.get("type", "string")
        if decl_type == "integer":
            coerced[key] = _coerce_integer(key, value, errors)
        elif decl_type == "number":
            coerced[key] = _coerce_number(key, value, errors)
        elif decl_type == "boolean":
            coerced[key] = _coerce_boolean(key, value, errors)
        elif decl_type == "array":
            coerced[key] = _coerce_array(key, value, errors)
        elif decl_type == "object":
            coerced[key] = _coerce_object(key, value, errors)
        elif decl_type == "enum":
            coerced[key] = _coerce_enum(key, value, decl.get("choices", []), errors)
        elif decl_type == "string":
            if isinstance(value, str):
                coerced[key] = value
            else:
                errors.append("field '%s' expected string, received %s" % (key, _describe(value)))
                coerced[key] = value
        else:
            coerced[key] = value
    if errors:
        raise ParamCoercionError(errors)
    return coerced


class NodeRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._types: Dict[str, type] = {}

    def register(self, node_class: type, node_type: str = "") -> type:
        name = node_type or getattr(node_class, "node_type", "")
        if not name:
            raise NodeRegistryError("node_class %r has no node_type" % node_class)
        with self._lock:
            existing = self._types.get(name)
            if existing is not None and existing is not node_class:
                raise NodeRegistryError(
                    "duplicate node type: %s already registered by %s" % (name, existing)
                )
            node_class.node_type = name
            self._types[name] = node_class
        logger.info("node type registered: %s", name)
        return node_class

    def get(self, node_type: str) -> Optional[type]:
        with self._lock:
            return self._types.get(node_type)

    def list(self) -> List[str]:
        with self._lock:
            return sorted(self._types)

    def create(self, node_type: str, node_id: str = "", **kwargs: Any) -> BaseNode:
        node_class = self.get(node_type)
        if node_class is None:
            raise NodeRegistryError("unknown node type: %s" % node_type)
        return node_class(node_id=node_id, **kwargs)

    def decorator(self, node_type: str) -> Callable[[type], type]:
        def wrap(node_class: type) -> type:
            return self.register(node_class, node_type)

        return wrap


default_registry = NodeRegistry()


def register(node_type: str) -> Callable[[type], type]:
    """Module-level decorator form: @register("http_fetch")."""
    return default_registry.decorator(node_type)


__all__ = [
    "BaseNode",
    "NodeContext",
    "NodeRegistry",
    "NodeRegistryError",
    "NodeResult",
    "ParamCoercionError",
    "coerce_params",
    "run_node",
    "register",
    "default_registry",
]
