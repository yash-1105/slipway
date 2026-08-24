"""Running a local process with an explicit timeout.

Used by the sandbox and deploy implementations. Every call has a timeout; there
is no overload without one, deliberately.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


async def run(
    argv: list[str], *, timeout_seconds: float, stdin: bytes | None = None
) -> ProcessResult:
    """Run `argv` and return its outcome as a value.

    A non-zero exit is data, not an exception -- callers need the log. A
    timeout kills the process group and returns `timed_out=True`.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        out, err = await asyncio.wait_for(process.communicate(stdin), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        # Reap it, so the timeout does not leak a zombie into the worker.
        await process.wait()
        return ProcessResult(
            exit_code=-1,
            stdout="",
            stderr=f"timed out after {timeout_seconds}s: {' '.join(argv)}",
            duration_seconds=loop.time() - started,
            timed_out=True,
        )

    return ProcessResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
        duration_seconds=loop.time() - started,
    )
