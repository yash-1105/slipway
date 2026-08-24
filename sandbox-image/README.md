# sandbox-image

The container agent tool calls run in.

    make sandbox-image        # builds slipway/sandbox:dev

`SLIPWAY_SANDBOX_IMAGE` selects which tag the sandbox seam starts.

## What is enforced where

The image provides the toolchain. Everything about *confinement* is applied by
`app/sandbox/impl/docker.py` when it starts a container, not by this Dockerfile:

| Control | Applied by |
| --- | --- |
| Read-only root filesystem | `--read-only` at run time |
| Writable scratch | `--tmpfs /tmp` at run time |
| No capabilities | `--cap-drop ALL --security-opt no-new-privileges` |
| No network, or an allowlisted bridge | `--network` at run time |
| CPU, memory and pid caps | `--cpus`, `--memory`, `--pids-limit` |
| Unprivileged user | this Dockerfile (`USER agent`) |

That split is deliberate: an image cannot confine itself, and a container
started without those flags would be a normal container with a normal blast
radius. If you start one by hand for debugging, you are outside the sandbox.

## Adding a tool

Adding to the `apt-get install` list widens what a generated application can be
built from, and every entry is something we may end up shipping to a client.
Justify it in the commit message.
