"""Code-mode tool executor (issue #50, ports fusion-mlx M10).

Instead of the OpenAI tool-call JSON round-trip the model writes a
Python script against stub functions rendered from the harness tool
registry; the paper (arXiv 2611.19123) measures 70-90% tool-token
savings on multi-turn trajectories because intermediate results live
in Python variables instead of the context. This module is the
prompt-side transform + orchestration only: render_stubs /
parse_code_block / inject_code_mode_messages are pure functions,
CodeModeTurn maps stub names to tool callables, checks the script as
a WHOLE against the permission engine (ruled: no per-stub granularity
for v1 — the script inherits the initiating agent's allowlist), and
feeds execution tracebacks back as the next message for self-repair.
harness never executes code itself — execution backends implement the
CodeExecutor protocol. Zero model calls; stdlib only.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

CODE_MODE_SYSTEM_PROMPT = """You write Python code to call tools.
A stub module for the available tools is provided below. Rules:
- Call stub functions exactly as declared; do not invent functions or parameters.
- Return values have the documented shapes; do not reformat them.
- Respond with a single ```python code block and nothing else.
- Assign the final answer to a variable named `result`."""

_PY_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "null": "None",
}

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


class CodeModeError(ValueError):
    """Malformed code-mode turn."""


class UnknownStubError(CodeModeError):
    """Script calls a function that is not a rendered stub."""


def _py_type(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0] if t else None)
    if t == "array":
        item = schema.get("items")
        inner = _py_type(item) if isinstance(item, dict) else "Any"
        return "list[%s]" % inner
    if t == "object":
        return "dict"
    return _PY_TYPES.get(t, "Any")


def _shape_hint(function: dict) -> str:
    explicit = function.get("x-return-shape")
    if explicit is not None:
        return json.dumps(explicit, ensure_ascii=False)
    returns = function.get("returns") or function.get("x-returns")
    if isinstance(returns, dict):
        props = returns.get("properties")
        if isinstance(props, dict):
            return "dict with keys " + json.dumps(sorted(props), ensure_ascii=False)
    return "JSON value (shape not declared by the tool author)"


def render_stubs(tools) -> str:
    """Render tool definitions as a Python stub module with signatures
    and return-shape hints. Pydantic-shaped inputs are normalized via
    model_dump() first (M10 #1644 gotcha). Degenerate inputs yield an
    empty module — code_mode is best-effort prompt shaping."""
    if not tools:
        return ""
    lines = ["# Tool stubs (do not edit; shapes are binding)"]
    for tool in tools:
        if hasattr(tool, "model_dump"):
            tool = tool.model_dump()
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        name = fn["name"]
        desc = " ".join(str(fn.get("description", "")).split())
        params = fn.get("parameters") or {}
        props = params.get("properties") if isinstance(params, dict) else None
        required = set()
        if isinstance(params, dict):
            required = set(params.get("required") or [])
        sig_parts = []
        if isinstance(props, dict) and props:
            for pname, pschema in props.items():
                ptype = _py_type(pschema) if isinstance(pschema, dict) else "Any"
                sig_parts.append(
                    "%s: %s" % (pname, ptype) + ("" if pname in required else " = None")
                )
        lines.append("def %s(%s) -> Any:" % (name, ", ".join(sig_parts)))
        if desc:
            lines.append('    """%s"""' % desc)
        lines.append("    # returns: %s" % _shape_hint(fn))
        lines.append("    ...")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_code_block(text) -> list:
    """Extract fenced python blocks from a model response, in order.
    Unfenced text is ignored — callers execute only declared blocks."""
    if not text:
        return []
    return [m.group(1).strip() for m in re.finditer(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)]


def inject_code_mode_messages(messages: list, tools) -> list:
    """New message list with the code-mode system prompt + stub module
    merged into the leading system message (chat templates require
    system first and often reject a second one). No-op without tools."""
    stubs = render_stubs(tools)
    if not stubs:
        return list(messages)
    system = "%s\n\n```python\n%s```" % (CODE_MODE_SYSTEM_PROMPT, stubs)
    if messages and messages[0].get("role") == "system":
        merged = dict(messages[0])
        merged["content"] = "%s\n\n%s" % (system, merged.get("content", ""))
        return [merged, *messages[1:]]
    return [{"role": "system", "content": system}, *messages]


class CodeExecutor(Protocol):
    """Execution backend contract. harness never runs code itself;
    consumers (studio/cowork/code) wire sandboxes or subprocesses."""

    def execute(self, script: str, namespace: dict) -> Any: ...


@dataclass
class CodeModeMetrics:
    """Dual metric reporting (aligned with M15): elicitation and
    expansion are reported separately, never conflated."""

    elicitation_prompt_tokens: int = 0
    elicitation_native_tokens: int = 0
    expansion_attempts: int = 0
    expansion_successes: int = 0

    @property
    def elicitation_savings(self) -> float:
        if self.elicitation_native_tokens <= 0:
            return 0.0
        return 1.0 - self.elicitation_prompt_tokens / self.elicitation_native_tokens

    @property
    def expansion_success_rate(self) -> float:
        if self.expansion_attempts <= 0:
            return 0.0
        return self.expansion_successes / self.expansion_attempts

    def report(self) -> dict:
        return {
            "elicitation_savings": round(self.elicitation_savings, 4),
            "expansion_success_rate": round(self.expansion_success_rate, 4),
        }


@dataclass
class CodeModeTurnResult:
    ok: bool
    value: Any = None
    error: str = ""
    iterations: int = 0
    called_tools: list = field(default_factory=list)
    transcript: list = field(default_factory=list)


class CodeModeTurn:
    """One agent turn in code mode: parse the model's code block, map
    stub names to tool callables, pass the script as a whole through
    the permission engine, execute via the wired backend, and loop
    execution exceptions back as the next message for self-repair."""

    def __init__(
        self,
        tools,
        executor: CodeExecutor,
        permission_engine=None,
        budget_ref=None,
        telemetry_engine=None,
        max_iterations: int = 3,
    ) -> None:
        self.executor = executor
        self.permissions = permission_engine
        self.budget_ref = budget_ref
        self.telemetry_engine = telemetry_engine
        self.max_iterations = max_iterations
        self.tool_names = set(self._tool_names(tools))
        self.metrics = CodeModeMetrics()

    @staticmethod
    def _tool_names(tools) -> list:
        names = []
        for tool in tools or []:
            if hasattr(tool, "model_dump"):
                tool = tool.model_dump()
            fn = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(fn, dict) and fn.get("name"):
                names.append(fn["name"])
        return names

    def run(self, text: str, namespace: dict, ctx: Any = None) -> CodeModeTurnResult:
        blocks = parse_code_block(text)
        if not blocks:
            return CodeModeTurnResult(ok=False, error="no python code block in model response")
        script = blocks[0]
        called = self._called_names(script)
        for name in called:
            if name not in self.tool_names:
                logger.warning("code-mode unknown stub call name=%s", name)
                return CodeModeTurnResult(
                    ok=False,
                    error="unknown function '%s' — call only rendered stubs" % name,
                    iterations=1,
                    called_tools=called,
                )
        if not self._permission_ok(script, called, ctx):
            return CodeModeTurnResult(
                ok=False,
                error="permission denied: script exceeds the agent allowlist",
                iterations=1,
                called_tools=called,
            )
        if self._budget_exceeded(ctx):
            return CodeModeTurnResult(
                ok=False, error="token budget exceeded", iterations=0, called_tools=called
            )
        transcript = []
        value = None
        last_error = ""
        for iteration in range(1, self.max_iterations + 1):
            try:
                value = self.executor.execute(script, namespace)
                self._telemetry({"dtype": "code", "event": "execute", "iteration": iteration})
                return CodeModeTurnResult(
                    ok=True,
                    value=value,
                    iterations=iteration,
                    called_tools=called,
                    transcript=transcript,
                )
            except Exception as exc:
                last_error = "%s: %s" % (type(exc).__name__, exc)
                transcript.append({"iteration": iteration, "error": last_error})
                logger.info(
                    "code-mode execution error feeds back for self-repair iteration=%d err=%s",
                    iteration,
                    last_error,
                )
                script = "Traceback (most recent call last):\n%s\n" % last_error
        return CodeModeTurnResult(
            ok=False, error=last_error, iterations=self.max_iterations, called_tools=called
        )

    def _called_names(self, script: str) -> list:
        seen = []
        for m in _CALL_RE.finditer(script):
            if m.group(1) not in (
                "def",
                "print",
                "len",
                "range",
                "str",
                "int",
                "float",
                "list",
                "dict",
                "set",
                "sum",
                "sorted",
                "open",
                "isinstance",
            ):
                seen.append(m.group(1))
        return sorted(set(seen))

    def _permission_ok(self, script: str, called: list, ctx: Any) -> bool:
        """One admission pass for the whole script (ruled v1 semantics: no
        per-stub granularity). Every called stub must be allowed — unmatched
        tools evaluate to "ask" in an armed engine, which denies, so the
        script inherits exactly the initiating agent's allowlist. Execution
        is never intercepted per-stub. Any engine error is fail-closed."""
        if self.permissions is None:
            return True
        try:
            for name in called:
                decision = self.permissions.evaluate(name, {"script": script}, ctx=ctx)
                allowed = getattr(decision, "decision", decision) == "allow"
                if not allowed:
                    logger.info(
                        "code-mode script denied tool=%s rule=%s reason=%s",
                        name,
                        getattr(decision, "rule_source", ""),
                        getattr(decision, "reason", ""),
                    )
                    return False
            return True
        except Exception as exc:
            logger.warning("code-mode permission check failed: %s — fail-closed", exc)
            return False

    def _budget_exceeded(self, ctx: Any) -> bool:
        if self.budget_ref is None:
            return False
        graph_id = getattr(ctx, "graph_id", "") or ""
        try:
            try:
                return bool(self.budget_ref.is_exceeded(graph_id))
            except TypeError:
                return bool(self.budget_ref.is_exceeded())
        except Exception as exc:
            logger.warning("code-mode budget check failed: %s", exc)
            return False

    def _telemetry(self, payload: dict) -> None:
        if self.telemetry_engine is None:
            return
        try:
            self.telemetry_engine.record(payload)
        except AttributeError:
            logger.debug("telemetry engine has no record(); skipping")
        except Exception as exc:
            logger.warning("telemetry record failed: %s", exc)


__all__ = [
    "CODE_MODE_SYSTEM_PROMPT",
    "CodeExecutor",
    "CodeModeError",
    "CodeModeMetrics",
    "CodeModeTurn",
    "CodeModeTurnResult",
    "UnknownStubError",
    "inject_code_mode_messages",
    "parse_code_block",
    "render_stubs",
]
