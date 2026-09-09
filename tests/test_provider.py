import json

import pytest

from tero.execution import Budget
from tero.provider import ContextOverflow, ProviderError, ResponsesClient


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def iter_bytes(self):
        yield from self.chunks


def test_stream_preserves_native_call_and_encrypted_reasoning():
    output = [
        {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
        {"type": "function_call", "name": "read_file", "arguments": "{}", "call_id": "c1"},
    ]
    payload = (
        "data: "
        + json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": output,
                },
            }
        )
        + "\n\n"
    ).encode()
    data = ResponsesClient._read_events(Stream([payload[:19], payload[19:]]), Budget(10))
    assert data["output"] == output


def test_partial_stream_is_not_executable_output():
    stream = Stream([b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'])
    with pytest.raises(ProviderError, match="response.completed"):
        ResponsesClient._read_events(stream, Budget(10))


def test_stream_overflow_keeps_specific_error():
    payload = b'data: {"type":"response.failed","response":{"error":{"code":"context_length_exceeded"}}}\n\n'
    with pytest.raises(ContextOverflow):
        ResponsesClient._read_events(Stream([payload]), Budget(10))


def test_transient_request_retries_without_replaying_tools(tmp_path, monkeypatch):
    from tero.config import Config
    from tero.provider import TransientProviderError
    from tero.storage import Trace

    client = ResponsesClient(Config(), Trace(tmp_path / "trace", str))
    budgets = []

    def request_once(instructions, items, tools, budget, **options):
        budgets.append(budget)
        if len(budgets) < 3:
            raise TransientProviderError("HTTP 429")
        return [{"type": "message", "content": []}]

    monkeypatch.setattr(client, "_request_once", request_once)
    budget = Budget(10)
    monkeypatch.setattr(budget.cancelled, "wait", lambda seconds: False)
    assert client.request("instructions", [], [], budget)
    assert len(budgets) == 3
    assert all(item is budgets[0] for item in budgets)
    assert client.trace.metrics["retries"] == 2


@pytest.mark.parametrize("failure", [ProviderError("HTTP 400"), ContextOverflow("too long")])
def test_permanent_request_failure_is_not_retried(tmp_path, monkeypatch, failure):
    from tero.config import Config
    from tero.storage import Trace

    client = ResponsesClient(Config(), Trace(tmp_path / "trace", str))
    calls = []

    def request_once(*args, **kwargs):
        calls.append(1)
        raise failure

    monkeypatch.setattr(client, "_request_once", request_once)
    with pytest.raises(type(failure)):
        client.request("instructions", [], [], Budget(10))
    assert len(calls) == 1


def test_cancellation_stops_transient_retry(tmp_path, monkeypatch):
    from tero.config import Config
    from tero.execution import ExecutionStopped
    from tero.provider import TransientProviderError
    from tero.storage import Trace

    client = ResponsesClient(Config(), Trace(tmp_path / "trace", str))
    budget = Budget(10)

    def request_once(*args, **kwargs):
        budget.cancelled.set()
        raise TransientProviderError("HTTP 503")

    monkeypatch.setattr(client, "_request_once", request_once)
    with pytest.raises(ExecutionStopped, match="cancelled"):
        client.request("instructions", [], [], budget)
    assert client.trace.metrics["retries"] == 0
