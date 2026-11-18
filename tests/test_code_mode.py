"""Tests for code-mode tool executor (issue #50)."""

import json
import os

import pytest

from fusion_harness.code_mode import (
    CODE_MODE_SYSTEM_PROMPT,
    CodeModeMetrics,
    CodeModeTurn,
    inject_code_mode_messages,
    parse_code_block,
    render_stubs,
)
from fusion_harness.permissions import PermissionEngine, PermissionMode, PermissionRule
from fusion_harness.tokens import HeuristicTokenCounter, count_messages

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "Search the notes index",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
            "x-return-shape": {"hits": ["str"], "total": 0},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Read one note by id",
            "parameters": {
                "type": "object",
                "properties": {"note_id": {"type": "string"}},
                "required": ["note_id"],
            },
        },
    },
]


class _PydLike:
    def __init__(self, d):
        self._d = d

    def model_dump(self):
        return self._d


class _Executor:
    def __init__(self, fail_times=0, error=RuntimeError("boom")):
        self.fail_times = fail_times
        self.error = error
        self.scripts = []
        self.namespaces = []

    def execute(self, script, namespace):
        self.scripts.append(script)
        self.namespaces.append(namespace)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        return {"final": True}


def _turn(**kw):
    return CodeModeTurn(TOOLS, _Executor(**kw))


class TestRenderStubs:
    def test_parity_with_fusion_mlx_renderer(self):
        pytest.importorskip("fusion_mlx")
        from fusion_mlx.api.code_mode import build_code_mode_stubs

        assert render_stubs(TOOLS) == build_code_mode_stubs(TOOLS)

    def test_signature_and_shape_hint(self):
        out = render_stubs(TOOLS)
        assert "def search_notes(query: str, limit: int = None) -> Any:" in out
        assert '# returns: {"hits": ["str"], "total": 0}' in out
        assert "def read_note(note_id: str) -> Any:" in out
        assert '"""Search the notes index"""' in out

    def test_pydantic_objects_normalized(self):
        out = render_stubs([_PydLike(TOOLS[0])])
        assert "def search_notes(" in out

    def test_degenerate_inputs_empty(self):
        assert render_stubs([]) == ""
        assert render_stubs(None) == ""
        assert "def " not in render_stubs([{"type": "function"}])

    def test_synthesized_shape_hint(self):
        tool = {
            "function": {
                "name": "t",
                "parameters": {},
                "returns": {"properties": {"a": {}, "b": {}}},
            }
        }
        assert '# returns: dict with keys ["a", "b"]' in render_stubs([tool])

    def test_optional_vs_required(self):
        out = render_stubs(TOOLS)
        assert "query: str," in out
        assert "limit: int = None" in out


class TestParseCodeBlock:
    def test_python_fence(self):
        assert parse_code_block("x\n```python\nresult = 1\n```\n") == ["result = 1"]

    def test_bare_fence(self):
        assert parse_code_block("```\nresult = 2\n```") == ["result = 2"]

    def test_no_block(self):
        assert parse_code_block("plain answer") == []
        assert parse_code_block("") == []
        assert parse_code_block(None) == []

    def test_multiple_blocks_in_order(self):
        out = parse_code_block("```python\na=1\n```\ntext\n```python\nb=2\n```")
        assert out == ["a=1", "b=2"]


class TestInjectMessages:
    def test_merges_into_leading_system(self):
        msgs = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
        out = inject_code_mode_messages(msgs, TOOLS)
        assert len(out) == 2
        assert out[0]["content"].startswith(CODE_MODE_SYSTEM_PROMPT)
        assert "def search_notes(" in out[0]["content"]
        assert out[0]["content"].endswith("Be brief.")

    def test_prepends_without_system(self):
        out = inject_code_mode_messages([{"role": "user", "content": "hi"}], TOOLS)
        assert out[0]["role"] == "system"
        assert CODE_MODE_SYSTEM_PROMPT in out[0]["content"]

    def test_no_tools_noop(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert inject_code_mode_messages(msgs, []) == msgs


class TestOrchestration:
    def test_happy_path_executes_script(self):
        turn = _turn()
        out = turn.run("```python\nresult = search_notes(query='x')\n```", {}, ctx=None)
        assert out.ok is True
        assert out.value == {"final": True}
        assert out.called_tools == ["search_notes"]
        assert out.iterations == 1

    def test_unknown_function_rejected(self):
        turn = _turn()
        out = turn.run("```python\nresult = hack_system('rm -rf /')\n```", {})
        assert out.ok is False
        assert "unknown function 'hack_system'" in out.error
        assert out.iterations == 1

    def test_non_stub_import_ignored_when_not_called(self):
        turn = _turn()
        out = turn.run("```python\nimport os\nresult = read_note(note_id='n1')\n```", {})
        assert out.ok is True

    def test_exception_feeds_back_for_self_repair(self):
        turn = CodeModeTurn(TOOLS, _Executor(fail_times=1))
        out = turn.run("```python\nresult = read_note(note_id='n1')\n```", {})
        assert out.ok is True
        assert out.iterations == 2
        assert "RuntimeError: boom" in out.transcript[0]["error"]

    def test_max_iterations_exhausted(self):
        turn = CodeModeTurn(TOOLS, _Executor(fail_times=99), max_iterations=3)
        out = turn.run("```python\nresult = read_note(note_id='n1')\n```", {})
        assert out.ok is False
        assert out.iterations == 3
        assert "RuntimeError: boom" in out.error

    def test_no_block_rejected(self):
        assert _turn().run("no code here", {}).ok is False


class TestPermissionGating:
    def _armed(self):
        engine = PermissionEngine(mode=PermissionMode.DEFAULT)
        engine.add_rule(PermissionRule(action="allow", tool="read_note"))
        return engine

    def test_script_in_allowlist_passes(self):
        turn = CodeModeTurn(TOOLS, _Executor(), permission_engine=self._armed())
        out = turn.run("```python\nresult = read_note(note_id='n1')\n```", {})
        assert out.ok is True

    def test_out_of_allowlist_denied(self):
        turn = CodeModeTurn(TOOLS, _Executor(), permission_engine=self._armed())
        out = turn.run("```python\nresult = search_notes(query='x')\n```", {})
        assert out.ok is False
        assert "permission denied" in out.error

    def test_whole_script_one_admission_pass(self):
        calls = []
        engine = PermissionEngine(mode=PermissionMode.DEFAULT)
        engine.add_rule(PermissionRule(action="allow", tool="read_note"))

        orig = engine.evaluate

        def spy(tool_name, args, ctx=None, **kw):
            calls.append(tool_name)
            return orig(tool_name, args, ctx=ctx, **kw)

        engine.evaluate = spy
        turn = CodeModeTurn(TOOLS, _Executor(), permission_engine=engine)
        turn.run("```python\nresult = read_note(note_id='n1')\n```", {})
        assert calls == ["read_note"]

    def test_permission_engine_error_fail_closed(self):
        class Bad:
            def evaluate(self, *a, **kw):
                raise RuntimeError("engine down")

        turn = CodeModeTurn(TOOLS, _Executor(), permission_engine=Bad())
        out = turn.run("```python\nresult = read_note(note_id='n1')\n```", {})
        assert out.ok is False
        assert "permission denied" in out.error

    def test_unarmed_engine_allows(self):
        turn = CodeModeTurn(TOOLS, _Executor(), permission_engine=PermissionEngine())
        assert turn.run("```python\nresult = read_note(note_id='n1')\n```", {}).ok is True


class TestBudgetAndTelemetry:
    def test_budget_exceeded_refuses_turn(self):
        class Budget:
            def is_exceeded(self, graph_id=""):
                return True

        turn = CodeModeTurn(TOOLS, _Executor(), budget_ref=Budget())
        out = turn.run("```python\nresult = read_note(note_id='n1')\n```", {})
        assert out.ok is False
        assert "budget exceeded" in out.error

    def test_zero_arg_budget_supported(self):
        class Budget:
            def is_exceeded(self):
                return True

        turn = CodeModeTurn(TOOLS, _Executor(), budget_ref=Budget())
        assert (
            "budget exceeded"
            in turn.run("```python\nresult = read_note(note_id='n1')\n```", {}).error
        )

    def test_under_budget_executes(self):
        class Budget:
            def is_exceeded(self, graph_id=""):
                return False

        turn = CodeModeTurn(TOOLS, _Executor(), budget_ref=Budget())
        assert turn.run("```python\nresult = read_note(note_id='n1')\n```", {}).ok is True

    def test_telemetry_code_axis(self):
        seen = []

        class Telemetry:
            def record(self, payload):
                seen.append(payload)

        turn = CodeModeTurn(TOOLS, _Executor(), telemetry_engine=Telemetry())
        turn.run("```python\nresult = read_note(note_id='n1')\n```", {})
        assert seen and seen[0]["dtype"] == "code"

    def test_telemetry_absent_record_noop(self):
        class Telemetry:
            pass

        turn = CodeModeTurn(TOOLS, _Executor(), telemetry_engine=Telemetry())
        assert turn.run("```python\nresult = read_note(note_id='n1')\n```", {}).ok is True


class TestMetrics:
    def test_dual_reporting_separate(self):
        m = CodeModeMetrics(
            elicitation_prompt_tokens=300,
            elicitation_native_tokens=1000,
            expansion_attempts=4,
            expansion_successes=3,
        )
        rep = m.report()
        assert rep["elicitation_savings"] == 0.7
        assert rep["expansion_success_rate"] == 0.75

    def test_zero_division_guards(self):
        assert CodeModeMetrics().report() == {
            "elicitation_savings": 0.0,
            "expansion_success_rate": 0.0,
        }


class TestTrajectoryReplay:
    def test_30_turn_replay_reduction_in_band(self):
        counter = HeuristicTokenCounter()
        tool_json = json.dumps({"hits": ["note " + "x" * 120] * 4, "total": 97})
        native = [{"role": "system", "content": "native tool-calling agent"}]
        for _ in range(30):
            native.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "search_notes",
                                "arguments": json.dumps({"query": "q" * 20, "limit": 5}),
                            }
                        },
                        {
                            "function": {
                                "name": "read_note",
                                "arguments": json.dumps({"note_id": "n-12345678"}),
                            }
                        },
                    ],
                }
            )
            native.append({"role": "tool", "content": tool_json})
            native.append({"role": "tool", "content": tool_json})
        native_tokens = count_messages(native, counter)

        stubs = render_stubs(TOOLS)
        code_mode_msgs = [
            {"role": "system", "content": "code-mode agent\n\n```python\n%s```" % stubs}
        ]
        for i in range(30):
            code_mode_msgs.append(
                {
                    "role": "assistant",
                    "content": "```python\nhits = search_notes(query='q' * 20, limit=5)\nnote = read_note(note_id='n-12345678')\nresult = {'top': hits['hits'][0], 'note': len(note)}\n```",
                }
            )
            if i % 6 == 5:
                code_mode_msgs.append({"role": "user", "content": "progress: " + "s" * 40})
        code_mode_tokens = count_messages(code_mode_msgs, counter)

        savings = 1.0 - code_mode_tokens / native_tokens
        assert 0.70 <= savings <= 0.97, "savings %.3f outside paper band" % savings


class TestZeroModelCalls:
    def test_no_model_imports(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "fusion_harness", "code_mode.py"),
            encoding="utf-8",
        ).read()
        for banned in ("llm_gateway", "MLXClient", "mlx_client", ".chat(", "compactor"):
            assert banned not in src
