"""K2 tests (issue #35): dag_node registry/coercion/run_node +
checkpoint_binding save/load/mark_emitted + crash-recovery replay."""

import pytest

from fusion_harness.checkpoint import CheckpointStore
from fusion_harness.checkpoint_binding import bind_node_state
from fusion_harness.dag_node import (
    BaseNode,
    NodeContext,
    NodeRegistry,
    NodeRegistryError,
    NodeResult,
    ParamCoercionError,
    coerce_params,
    run_node,
)
from fusion_harness.outbox import OutboxStore


def _ctx(run_id="r1", node_id="n1"):
    return NodeContext(run_id=run_id, node_id=node_id)


class TestCoercion:
    def test_positive_matrix(self):
        schema = {
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "number"},
                "c": {"type": "boolean"},
                "d": {"type": "array"},
                "e": {"type": "object"},
                "f": {"type": "string"},
                "g": {"type": "enum", "choices": ["x", "y"]},
            }
        }
        out = coerce_params(
            {
                "a": "7",
                "b": "2.5",
                "c": "yes",
                "d": '["p","q"]',
                "e": '{"k":1}',
                "f": "hello",
                "g": "y",
            },
            schema,
        )
        assert out == {
            "a": 7,
            "b": 2.5,
            "c": True,
            "d": ["p", "q"],
            "e": {"k": 1},
            "f": "hello",
            "g": "y",
        }

    def test_negative_matrix_lists_every_bad_field(self):
        schema = {
            "properties": {
                "i": {"type": "integer"},
                "n": {"type": "number"},
                "b": {"type": "boolean"},
                "a": {"type": "array"},
                "o": {"type": "object"},
                "s": {"type": "string"},
            }
        }
        with pytest.raises(ParamCoercionError) as exc:
            coerce_params(
                {"i": "abc", "n": None, "b": "maybe", "a": "no-json", "o": 5, "s": 12}, schema
            )
        joined = "; ".join(exc.value.errors)
        assert "field 'i' expected integer" in joined
        assert "field 'n' expected number" in joined
        assert "field 'b' expected boolean" in joined
        assert "field 'a' expected array" in joined
        assert "field 'o' expected object" in joined
        assert "field 's' expected string" in joined

    def test_bool_never_coerces_to_integer(self):
        with pytest.raises(ParamCoercionError):
            coerce_params({"x": True}, {"properties": {"x": {"type": "integer"}}})

    def test_non_integral_float_rejected(self):
        with pytest.raises(ParamCoercionError):
            coerce_params({"x": 5.9}, {"properties": {"x": {"type": "integer"}}})

    def test_undeclared_keys_pass_through(self):
        assert coerce_params({"extra": {"z": 1}}, {"properties": {}}) == {"extra": {"z": 1}}

    def test_enum_rejects_unknown_choice(self):
        with pytest.raises(ParamCoercionError) as exc:
            coerce_params({"m": "z"}, {"properties": {"m": {"type": "enum", "choices": ["x"]}}})
        assert "field 'm' expected one of" in exc.value.errors[0]


class TestRegistry:
    def test_duplicate_type_rejected(self):
        reg = NodeRegistry()

        class A(BaseNode):
            node_type = "dup"

        class B(BaseNode):
            node_type = "dup"

        reg.register(A)
        with pytest.raises(NodeRegistryError):
            reg.register(B)
        assert reg.get("dup") is A

    def test_same_class_reregister_idempotent(self):
        reg = NodeRegistry()

        class A(BaseNode):
            node_type = "same"

        reg.register(A)
        reg.register(A)
        assert reg.list() == ["same"]

    def test_unknown_type_raises(self):
        with pytest.raises(NodeRegistryError):
            NodeRegistry().create("missing")

    def test_decorator_form(self):
        reg = NodeRegistry()

        @reg.decorator("dec-node")
        class DecNode(BaseNode):
            node_type = "dec-node"

            def execute(self, ctx, params):
                return {"ok": True}

        assert reg.get("dec-node") is DecNode
        assert DecNode.node_type == "dec-node"

    def test_module_level_register(self):
        from fusion_harness.dag_node import default_registry, register

        @register("mod-node")
        class ModNode(BaseNode):
            node_type = "mod-node"

        assert default_registry.get("mod-node") is ModNode


class TestRunNode:
    def test_sync_execute(self):
        class SyncNode(BaseNode):
            node_type = "sync"

            def execute(self, ctx, params):
                return {"echo": params["x"]}

        import asyncio

        result = asyncio.run(run_node(SyncNode("sx"), _ctx(node_id="sx"), {"x": 1}))
        assert result.status == "success"
        assert result.data == {"echo": 1}
        assert result.node_id == "sx"

    def test_async_execute(self):
        class AsyncNode(BaseNode):
            node_type = "async"

            async def execute(self, ctx, params):
                return {"v": params["x"] * 2}

        import asyncio

        result = asyncio.run(run_node(AsyncNode(), _ctx(), {"x": 21}))
        assert result.data == {"v": 42}

    def test_exception_becomes_failed_result(self):
        class Boom(BaseNode):
            node_type = "boom"

            def execute(self, ctx, params):
                raise RuntimeError("explode")

        import asyncio

        result = asyncio.run(run_node(Boom(), _ctx(), {}))
        assert result.status == "failed"
        assert "explode" in result.error

    def test_context_artifact_writer(self):
        from fusion_harness.artifact_channel import ArtifactStore
        import tempfile

        store = ArtifactStore(tempfile.mkdtemp(prefix="k2-ctx-"))
        ctx = _ctx(run_id="run9")
        ctx.artifact_store = store
        ref = ctx.write_artifact("node1", "payload", "txt")
        assert ref == "run9/node1.txt"

    def test_result_validate(self):
        assert NodeResult(status="success").validate() is True
        assert NodeResult(status="failed").validate() is False


class TestNodeStateBinder:
    def _binder(self, run_id="run-x"):
        store = CheckpointStore(":memory:")
        outbox = OutboxStore(":memory:")
        return bind_node_state(store, outbox, run_id)

    def test_save_load_roundtrip(self):
        binder = self._binder()
        binder.save("n1", {"progress": 3, "items": ["a"]})
        assert binder.load("n1") == {"progress": 3, "items": ["a"]}

    def test_load_missing_returns_none(self):
        assert self._binder().load("ghost") is None

    def test_mark_emitted_runs_once(self):
        binder = self._binder()
        calls = []

        def effect():
            calls.append(1)
            return "done"

        first, replayed1 = binder.mark_emitted("n1", "send-email", effect)
        second, replayed2 = binder.mark_emitted("n1", "send-email", effect)
        assert first == "done"
        assert replayed1 is False
        assert second == "done"
        assert replayed2 is True
        assert len(calls) == 1

    def test_has_emitted(self):
        binder = self._binder()
        assert binder.has_emitted("n1", "ev") is False
        binder.mark_emitted("n1", "ev", None)
        assert binder.has_emitted("n1", "ev") is True

    def test_crash_recovery_replay_three_node_chain(self):
        store = CheckpointStore(":memory:")
        outbox = OutboxStore(":memory:")

        binder1 = bind_node_state(store, outbox, "run-crash")
        for nid in ("fetch", "transform", "load"):
            binder1.save(nid, {"done": True})
        binder1.mark_emitted("fetch", "side-effect", lambda: "sent")

        # simulate crash + fresh bind over the same durable stores
        binder2 = bind_node_state(store, outbox, "run-crash")
        recovered = {nid: binder2.load(nid) for nid in ("fetch", "transform", "load")}
        assert all(state == {"done": True} for state in recovered.values())
        replayed_result, was_replayed = binder2.mark_emitted(
            "fetch", "side-effect", lambda: pytest.fail("must not re-execute")
        )
        assert was_replayed is True
        assert replayed_result == "sent"
