"""Stateless native Responses requests; task and auxiliary calls share transport."""

import json
import threading
import time

import httpx

from .execution import ExecutionStopped


class ProviderError(RuntimeError):
    def __init__(self, message, *, code="provider_error"):
        super().__init__(message)
        self.code = code


class ContextOverflow(ProviderError):
    pass


class TransientProviderError(ProviderError):
    """A model request may be retried; no tool has executed from this response."""


class ResponsesClient:
    def __init__(self, config, trace):
        self.config = config
        self.trace = trace

    def request(self, instructions, items, tools, budget, **options):
        request_budget = budget.child(self.config.request_seconds)
        try:
            for attempt in range(3):
                request_budget.check()
                try:
                    return self._request_once(instructions, items, tools, request_budget, **options)
                except TransientProviderError:
                    if attempt == 2:
                        raise
                    delay = min(2**attempt, request_budget.remaining())
                    self.trace.record("model_retry", attempt=attempt + 1, delay_seconds=delay)
                    request_budget.cancelled.wait(delay)
                    request_budget.check()
        except ExecutionStopped:
            budget.check()  # User cancellation / overall deadline must still stop the task.
            raise ProviderError("Model request timed out", code="timeout") from None

    def _request_once(
        self,
        instructions,
        items,
        tools,
        budget,
        *,
        purpose="main",
        output_tokens=None,
        json_mode=False,
        reasoning_effort=None,
    ):
        config = self.config
        if not config.api_key:
            raise ProviderError("Set TERO_OPENAI_API_KEY in the environment or local configuration")
        payload = {
            "model": config.model,
            "instructions": instructions,
            "input": items,
            "tools": tools,
            "tool_choice": "auto" if tools else "none",
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
            "max_output_tokens": output_tokens or config.output_tokens,
            "include": ["reasoning.encrypted_content"],
        }
        if reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort}
        if json_mode:
            payload["text"] = {"format": {"type": "json_object"}}
        started = time.monotonic()
        duration = budget.remaining(config.request_seconds)
        expired = threading.Event()
        client = httpx.Client(timeout=httpx.Timeout(duration), follow_redirects=False)

        def expire():
            expired.set()
            client.close()

        timer = threading.Timer(duration, expire)
        timer.daemon = True
        timer.start()
        try:
            self.trace.record("model_requested", purpose=purpose, model=config.model)
            with client.stream(
                "POST",
                config.base_url.rstrip("/") + "/responses",
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Accept": "text/event-stream",
                    "User-Agent": "tero/0.2",
                },
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = self._read_bounded(response, budget).decode(errors="replace")
                    if any(
                        code in body.lower()
                        for code in (
                            "context_length_exceeded",
                            "context_window_exceeded",
                            "input_too_long",
                        )
                    ):
                        raise ContextOverflow(body[:2000])
                    error_type = (
                        TransientProviderError
                        if response.status_code in {408, 429} or response.status_code >= 500
                        else ProviderError
                    )
                    raise error_type(
                        f"HTTP {response.status_code}: {body[:2000]}",
                        code=f"http_{response.status_code}",
                    )
                if "text/event-stream" in response.headers.get("content-type", ""):
                    data = self._read_events(response, budget)
                else:
                    data = json.loads(self._read_bounded(response, budget))
            if expired.is_set():
                raise ProviderError("Model request timed out", code="timeout")
            budget.check()
            if data.get("status") != "completed":
                error = data.get("error") or {}
                if isinstance(error, dict) and error.get("code") in {
                    "server_error",
                    "internal_server_error",
                    "overloaded",
                    "rate_limit_exceeded",
                }:
                    raise TransientProviderError(str(error))
                raise ProviderError(
                    f"Incomplete model response: {data.get('incomplete_details') or data.get('error')}",
                    code="output_truncated"
                    if (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
                    else "provider_error",
                )
            output = data.get("output", [])
            calls = [item for item in output if item.get("type") == "function_call"]
            ids = [item.get("call_id") for item in calls]
            if any(not value for value in ids) or len(ids) != len(set(ids)):
                raise ProviderError("Response contains missing or duplicate tool call IDs")
            if not output:
                raise ProviderError("Response contained no output")
            self.trace.record(
                "model_finished",
                purpose=purpose,
                usage=data.get("usage", {}),
                seconds=round(time.monotonic() - started, 3),
            )
            return output
        except ExecutionStopped:
            raise
        except (httpx.HTTPError, OSError) as exc:
            budget.check()
            error_type = (
                TransientProviderError
                if not expired.is_set()
                and isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))
                else ProviderError
            )
            raise error_type(
                "Model request timed out" if expired.is_set() else str(exc),
                code="timeout"
                if expired.is_set() or isinstance(exc, httpx.TimeoutException)
                else "transport_error",
            ) from exc
        finally:
            timer.cancel()
            client.close()

    @staticmethod
    def _read_bounded(response, budget):
        pieces, size = [], 0
        for chunk in response.iter_bytes():
            budget.check()
            size += len(chunk)
            if size > 32 * 1024 * 1024:
                raise ProviderError("Provider response exceeded the transport limit")
            pieces.append(chunk)
        return b"".join(pieces)

    @staticmethod
    def _read_events(response, budget):
        pending = b""
        size = 0
        for chunk in response.iter_bytes():
            budget.check()
            size += len(chunk)
            if size > 32 * 1024 * 1024:
                raise ProviderError("Provider response exceeded the transport limit")
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if not line.startswith(b"data:"):
                    continue
                value = line[5:].strip()
                if not value or value == b"[DONE]":
                    continue
                event = json.loads(value)
                if event.get("type") == "response.completed":
                    return event["response"]
                elif event.get("type") in {"response.failed", "response.incomplete", "error"}:
                    error = event.get("response", {}).get("error") or event.get("error") or event
                    if isinstance(error, dict) and error.get("code") in {
                        "context_length_exceeded",
                        "context_window_exceeded",
                        "input_too_long",
                    }:
                        raise ContextOverflow(str(error))
                    error_type = (
                        TransientProviderError
                        if isinstance(error, dict)
                        and error.get("code")
                        in {
                            "server_error",
                            "internal_server_error",
                            "overloaded",
                            "rate_limit_exceeded",
                        }
                        else ProviderError
                    )
                    details = event.get("response", {}).get("incomplete_details") or error
                    if isinstance(details, dict):
                        details = {
                            key: details[key]
                            for key in ("code", "message", "reason")
                            if key in details
                        }
                    raise error_type(
                        f"{event.get('type')}: {details}",
                        code="output_truncated"
                        if isinstance(details, dict)
                        and details.get("reason") == "max_output_tokens"
                        else "provider_error",
                    )
        raise ProviderError("Stream ended before response.completed; no tools were executed")

    def summarize(self, instructions, value, budget, *, output_tokens):
        output = self.request(
            instructions,
            [{"role": "user", "content": json.dumps(value, ensure_ascii=False)}],
            [],
            budget,
            purpose="compaction",
            output_tokens=output_tokens,
            reasoning_effort="low",
        )
        if any(item.get("type") in {"function_call", "custom_tool_call"} for item in output):
            raise ProviderError("Summary attempted a tool call", code="unexpected_tool_call")
        return response_text(output)

    def json(self, instructions, value, budget, *, purpose, output_tokens=2048):
        output = self.request(
            instructions + "\nReturn only a JSON object.",
            [{"role": "user", "content": json.dumps(value, ensure_ascii=False)}],
            [],
            budget,
            purpose=purpose,
            output_tokens=output_tokens,
            json_mode=True,
            reasoning_effort="low",
        )
        return json.loads(response_text(output))


def response_text(output):
    return "\n".join(
        part.get("text", "")
        for item in output
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    ).strip()
