import json

from tero.storage import Trace


def test_closed_display_preserves_durable_trace(tmp_path):
    def closed(*args):
        raise BrokenPipeError("terminal closed")

    trace = Trace(tmp_path / "trace.jsonl", str, closed)
    trace.record("tool_finished", result={"status": "success"})
    trace.record("run_finished", status="completed")
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert [entry["event"] for entry in events] == ["tool_finished", "run_finished"]
    assert trace.display is None


def test_metrics_keep_missing_usage_distinct_from_zero(tmp_path):
    trace = Trace(tmp_path / "trace", str)
    trace.record(
        "model_finished",
        usage={
            "input_tokens": 12,
            "output_tokens": 3,
            "input_tokens_details": {"cached_tokens": 0},
        },
    )
    trace.record("model_finished", usage={})
    assert trace.metrics["input_tokens"] == 12
    assert trace.metrics["cached_tokens"] == 0
    assert trace.metrics["usage_complete"] is False
    missing = Trace(tmp_path / "missing", str)
    missing.record("model_finished", usage={})
    assert missing.metrics["input_tokens"] is None
