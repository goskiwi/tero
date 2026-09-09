import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tero.cli import approve
from tero.config import Config
from tero.execution import Budget, ExecutionStopped
from tero.provider import ResponsesClient
from tero.storage import Trace


def test_approval_interrupt_is_not_a_denial(monkeypatch):
    def interrupt(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    with pytest.raises(KeyboardInterrupt):
        approve("write_file", {"path": "a.py"})


@pytest.mark.parametrize("send_headers", [False, True])
def test_cancel_interrupts_silent_http_response(tmp_path, monkeypatch, send_headers):
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if send_headers:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
            entered.set()
            release.wait(5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    budget = Budget(20)
    errors = []
    client = ResponsesClient(
        Config(api_key="test-only", base_url=f"http://127.0.0.1:{server.server_port}"),
        Trace(tmp_path / "trace", str),
    )

    def invoke():
        try:
            client.request("test", [{"role": "user", "content": "test"}], [], budget)
        except Exception as exc:  # noqa: BLE001 - inspect the cross-thread exception
            errors.append(exc)

    request = threading.Thread(target=invoke, daemon=True)
    try:
        request.start()
        assert entered.wait(3)
        budget.cancelled.set()
        request.join(2)
        assert not request.is_alive(), "Cancellation waited for request timeout"
        assert len(errors) == 1 and isinstance(errors[0], ExecutionStopped)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        request.join(3)
