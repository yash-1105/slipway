"""sandbox seam: a Docker container per run.

Shells out to the `docker` CLI rather than taking a Docker SDK dependency; the
CLI is already a hard requirement of this machine and the SDK is not.
"""

from __future__ import annotations

import shlex

import structlog

from app.integrations.process import run as run_process
from app.sandbox.base import (
    ExecResult,
    Sandbox,
    SandboxFailure,
    SandboxHandle,
    SandboxSpec,
    StartResult,
)

log = structlog.get_logger(__name__)

#: Scratch space inside the sandbox container, which is otherwise read-only.
_SCRATCH_MOUNT = "/tmp"  # noqa: S108

#: The bridge network the egress proxy sits on. Sandboxes with an empty
#: allowlist get `none` instead, so a misconfigured proxy fails closed.
_ALLOWLIST_NETWORK = "slipway-sandbox"


class DockerSandbox(Sandbox):
    def __init__(self, *, docker_binary: str, name_prefix: str) -> None:
        self._docker = docker_binary
        self._prefix = name_prefix

    def name_for(self, run_id: object) -> str:
        """Deterministic, so a retried job addresses the same container."""
        return f"{self._prefix}{run_id}"

    async def start(
        self, handle: SandboxHandle, spec: SandboxSpec, *, timeout_seconds: float
    ) -> StartResult:
        # Idempotent: if the container already exists from a previous attempt,
        # adopt it rather than failing the retry.
        existing = await run_process(
            [self._docker, "inspect", "--format", "{{.State.Running}}", handle.name],
            timeout_seconds=timeout_seconds,
        )
        if existing.ok:
            if existing.stdout.strip() != "true":
                started = await run_process(
                    [self._docker, "start", handle.name], timeout_seconds=timeout_seconds
                )
                if not started.ok:
                    return SandboxFailure("create_failed", started.stderr.strip())
            return handle

        argv = [
            self._docker, "run", "--detach",
            "--name", handle.name,
            "--label", f"slipway.run_id={handle.run_id}",
            "--cpus", str(spec.cpu_limit),
            "--memory", f"{spec.memory_limit_mb}m",
            "--pids-limit", "512",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--read-only",
            # A writable scratch mount inside the read-only container. The
            # path is the sandbox's own /tmp, not the orchestrator host's.
            "--tmpfs", f"{_SCRATCH_MOUNT}:rw,noexec,nosuid,size=256m",
            "--volume", f"{spec.workspace_host_path}:/workspace:rw",
            "--workdir", "/workspace",
        ]
        # No network unless an allowlist says otherwise. The allowlist is
        # applied by the proxy the image is built to talk through; `none` here
        # is the backstop that makes a misconfigured proxy fail closed.
        network = _ALLOWLIST_NETWORK if spec.network_allowlist else "none"
        argv += ["--network", network]
        for key, value in spec.env.items():
            argv += ["--env", f"{key}={value}"]
        argv += [spec.image, "sleep", "infinity"]

        created = await run_process(argv, timeout_seconds=timeout_seconds)
        if created.ok:
            return handle

        stderr = created.stderr.strip()
        if "No such image" in stderr or "manifest unknown" in stderr:
            return SandboxFailure("image_missing", stderr)
        return SandboxFailure("create_failed", stderr or f"docker run exited {created.exit_code}")

    async def exec(
        self, handle: SandboxHandle, command: list[str], *, timeout_seconds: float
    ) -> ExecResult:
        result = await run_process(
            [self._docker, "exec", handle.name, *command], timeout_seconds=timeout_seconds
        )
        return ExecResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            timed_out=result.timed_out,
        )

    async def write_file(
        self, handle: SandboxHandle, path: str, content: bytes, *, timeout_seconds: float
    ) -> None:
        result = await run_process(
            [self._docker, "exec", "-i", handle.name, "sh", "-c", f"cat > {shlex.quote(path)}"],
            timeout_seconds=timeout_seconds,
            stdin=content,
        )
        if not result.ok:
            # Writing a file is infrastructure, not agent work: a caller cannot
            # do anything useful with a partial write, so this raises.
            raise OSError(f"sandbox write {path} failed: {result.stderr.strip()}")

    async def read_file(
        self, handle: SandboxHandle, path: str, *, timeout_seconds: float
    ) -> bytes | None:
        result = await run_process(
            [self._docker, "exec", handle.name, "cat", path], timeout_seconds=timeout_seconds
        )
        if not result.ok:
            return None
        return result.stdout.encode()

    async def stop(self, handle: SandboxHandle, *, timeout_seconds: float) -> None:
        # `--force` so that stopping something already gone is success.
        await run_process(
            [self._docker, "rm", "--force", "--volumes", handle.name],
            timeout_seconds=timeout_seconds,
        )

    async def list_live(self) -> list[SandboxHandle]:
        from uuid import UUID

        result = await run_process(
            [
                self._docker, "ps", "--all", "--filter", "label=slipway.run_id",
                "--format", "{{.Names}}\t{{.Label \"slipway.run_id\"}}",
            ],
            timeout_seconds=30.0,
        )
        if not result.ok:
            return []

        handles: list[SandboxHandle] = []
        for line in result.stdout.splitlines():
            name, _, raw_run_id = line.partition("\t")
            if not name or not raw_run_id:
                continue
            try:
                run_id = UUID(raw_run_id.strip())
            except ValueError:
                # A container wearing our label with an unparseable run id is
                # not ours to reconcile; leave it for a human.
                log.warning("sandbox.unparseable_label", container=name, label=raw_run_id)
                continue
            handles.append(SandboxHandle(run_id=run_id, name=name, backend="docker"))
        return handles
