"""Bounded shell execution. cwd is an execution location, not a sandbox."""

import os
import selectors
import signal
import subprocess
import time

from .execution import ExecutionStopped

SHELL_ENV = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PWD",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "USER",
)
CAPTURE_BYTES = 64 * 1024


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(command, root, timeout, budget):
    if os.name != "posix":
        raise RuntimeError("This interview version supports macOS/Linux process groups only")
    deadline = time.monotonic() + budget.remaining(timeout)
    env = {key: os.environ[key] for key in SHELL_ENV if key in os.environ}
    env["PWD"] = str(root)
    process = subprocess.Popen(
        command,
        shell=True,
        executable="/bin/sh",
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = False
    reason = ""
    try:
        with selectors.DefaultSelector() as selector:
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                try:
                    budget.check()
                    if time.monotonic() >= deadline:
                        raise ExecutionStopped("tool_timeout")
                except ExecutionStopped as exc:
                    reason = str(exc)
                    stop_process(process)
                    break
                for key, _event in selector.select(
                    timeout=min(0.1, max(0, deadline - time.monotonic()))
                ):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    data = output[key.data]
                    data.extend(chunk)
                    if len(data) > CAPTURE_BYTES:
                        del data[CAPTURE_BYTES // 2 : -CAPTURE_BYTES // 2]
                        truncated = True
            while process.poll() is None:
                budget.check()
                if time.monotonic() >= deadline:
                    reason = "tool_timeout"
                    stop_process(process)
                    break
                time.sleep(0.02)
    except BaseException:
        stop_process(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
    # Terminate lingering descendants in the process group, even if the shell exited first.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    result = {name: data.decode("utf-8", errors="replace") for name, data in output.items()}
    if truncated:
        result = {
            name: text[: len(text) // 2] + "\n[output truncated]\n" + text[len(text) // 2 :]
            for name, text in result.items()
        }
    return {
        **result,
        "exit_code": process.returncode,
        "stop_reason": reason,
        "truncated": truncated,
    }
