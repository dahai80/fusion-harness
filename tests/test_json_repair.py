"""Corpus tests for the stdlib JSON-repair helper (issue #67): each common
LLM failure mode has a case with an expected repair; failure raises."""

import os

import pytest

from fusion_harness.json_repair import JSONRepairError, parse_repair


class TestStrictPassThrough:
    def test_valid_json_untouched(self):
        assert parse_repair('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}

    def test_scalars(self):
        assert parse_repair("42") == 42
        assert parse_repair('"x"') == "x"
        assert parse_repair("true") is True
        assert parse_repair("null") is None


class TestFences:
    def test_json_fence(self):
        assert parse_repair('```json\n{"a": 1}\n```') == {"a": 1}

    def test_bare_fence(self):
        assert parse_repair('```\n{"a": 1}\n```') == {"a": 1}

    def test_fence_with_language_tag(self):
        assert parse_repair('```python\n{"a": 1}\n```') == {"a": 1}


class TestStrayProse:
    def test_prose_around_object(self):
        text = 'Here is the result: {"action": "deploy", "target": "db"} as requested.'
        assert parse_repair(text) == {"action": "deploy", "target": "db"}

    def test_prose_around_array(self):
        text = "Options: [1, 2, 3] pick one."
        assert parse_repair(text) == [1, 2, 3]

    def test_prose_with_braces_inside_strings(self):
        text = 'Answer {"cmd": "echo \\"{}{}\\""} done'
        assert parse_repair(text) == {"cmd": 'echo "{}{}"'}


class TestSingleQuotes:
    def test_single_quoted_object(self):
        assert parse_repair("{'a': 1, 'b': 'two'}") == {"a": 1, "b": "two"}

    def test_single_quotes_with_embedded_double(self):
        assert parse_repair("{'say': \"hi\"}") == {"say": "hi"}

    def test_single_quotes_inside_double_untouched(self):
        assert parse_repair('{"it\'s": 1}') == {"it's": 1}


class TestTrailingCommas:
    def test_object_trailing_comma(self):
        assert parse_repair('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}

    def test_nested_trailing_commas(self):
        assert parse_repair('{"a": [1, 2,], "b": {"c": 3,}}') == {"a": [1, 2], "b": {"c": 3}}


class TestTruncated:
    def test_truncated_object(self):
        assert parse_repair('{"a": 1, "b": 2') == {"a": 1, "b": 2}

    def test_truncated_nested(self):
        assert parse_repair('{"outer": {"inner": [1, 2') == {"outer": {"inner": [1, 2]}}

    def test_truncated_string_value(self):
        assert parse_repair('{"msg": "hello wor') == {"msg": "hello wor"}

    def test_truncated_after_colon(self):
        assert parse_repair('{"a":') == {}

    def test_truncated_escaped_char(self):
        assert parse_repair('{"path": "C:\\\\') == {"path": "C:"}


class TestCombinations:
    def test_fenced_prose_single_quotes_trailing(self):
        text = "```json\n{'result': 'ok', 'n': 2,}\n```"
        assert parse_repair(text) == {"result": "ok", "n": 2}

    def test_prose_wrapped_truncated(self):
        text = 'Final answer: {"status": "done", "items": [1, 2'
        assert parse_repair(text) == {"status": "done", "items": [1, 2]}


class TestNoSilentWrongData:
    def test_garbage_raises(self):
        with pytest.raises(JSONRepairError):
            parse_repair("this is not json at all :)")

    def test_empty_raises(self):
        with pytest.raises(JSONRepairError):
            parse_repair("")

    def test_non_str_raises(self):
        with pytest.raises(JSONRepairError):
            parse_repair({"already": "parsed"})  # type: ignore[arg-type]

    def test_no_model_imports(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "fusion_harness", "json_repair.py"),
            encoding="utf-8",
        ).read()
        for banned in ("llm_gateway", "MLXClient", "mlx_client", ".chat(", "compactor"):
            assert banned not in src
