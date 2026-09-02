# Dev container

A ready-to-run StreamController development environment with this plugin mounted where the
app loads it from. Derived from [StreamController's own devcontainer](https://github.com/StreamController/StreamController/tree/main/.devcontainer).

## Layout inside the container

| Path | What |
| --- | --- |
| `/workspaces/StreamController` | StreamController source, cloned at image build time at the ref you choose |
| `/workspaces/StreamController/data` | The app's data dir (`--data data`): pages, settings, logs. **Named volume**, survives rebuilds |
| `/workspaces/StreamController/data/plugins/net_red-tux_calendar_info` | **This repository**, bind-mounted from the host. This is the VS Code workspace folder |
| `/app/.venv` | The app's Python environment (uv-managed, same as upstream) |
| `/home/ubuntu/.claude` | Claude Code config (named volume, see below) |

## Prerequisites (host)

- Docker and VS Code with the *Dev Containers* extension.
- Linux desktop. The container gets the host's X11 socket and XDG runtime dir (Wayland +
  PulseAudio/PipeWire sockets) so the GTK window shows up on your desktop. Either X11-only or
  Wayland-only hosts work; GTK picks whichever is reachable.
- For real Stream Deck hardware: the container is privileged with `/dev/bus/usb` passed
  through, but the **host** still needs the udev rules from StreamController's README so the
  device is accessible to a non-root user. Without hardware, use the *no hardware* launch
  config or add a fake deck from the app's Settings.

## First run

1. Open this repository in VS Code and choose **Reopen in Container**.
2. The image build clones StreamController and installs system libraries; the post-create
   step installs the app's Python requirements, rebuilds Pillow against the system FreeType
   (see below), copies `run_dev.sh` into the app checkout, and pre-builds this plugin's
   backend venv. Expect several minutes the first time.
3. Run the app: `/workspaces/StreamController/run_dev.sh` in a terminal, or press F5 with the
   **StreamController (devel)** launch configuration to attach the debugger (breakpoints in
   this repo work as-is: the app loads the plugin from the same path VS Code edits).

Logs: `data/logs/logs.log` (structured, loguru) and `data/logs/run-console.log` (console
output of the last `run_dev.sh` run).

## Choosing the StreamController version

The `STREAMCONTROLLER_REF` build argument (branch, tag, or full commit sha; default `main`)
selects what gets cloned. Set it before opening the container:

```sh
export STREAMCONTROLLER_REF=1.5.0-beta.15   # then "Dev Containers: Rebuild Container"
```

`STREAMCONTROLLER_REPO` (a fork) and `STREAMCONTROLLER_PYTHON` (default 3.12) work the same
way. To switch without a rebuild, inside the container:

```sh
fetch-streamcontroller "" <ref> && uv pip install -r /workspaces/StreamController/requirements.txt
```

## Claude Code (optional)

Claude Code is installed through the official
[dev container feature](https://github.com/anthropics/devcontainer-features/tree/main/src/claude-code),
which also adds its VS Code extension. Nothing else depends on it; if you don't use Claude,
ignore it.

To use it: open a terminal in the container, run `claude`, and sign in once through the
browser prompt (paste the code back into the terminal if the callback doesn't reach the
container). Its configuration directory (`CLAUDE_CONFIG_DIR=/home/ubuntu/.claude`, holding
the login token, settings, memory, and session transcripts so `claude --resume` works) lives
in a named Docker volume keyed to this workspace. It survives container rebuilds, and it is
never bind-mounted from or copied to the host, so host credentials are not exposed to the
container and vice versa. Delete the volume (`docker volume ls | grep calendar-info`) to sign
out completely.

## Why Pillow is rebuilt

The pip wheel for Pillow bundles its own FreeType. Once GTK (linked against the system
FreeType) activates against a real display in the same process, PIL's text measurement
returns garbage and key labels silently vanish. Rebuilding Pillow from source against the
system library (`post-create.sh`) leaves one FreeType in the process. The Flatpak build of
StreamController does not have this problem.

## Known limitations

- `--device=/dev/bus/usb` requires that path to exist on the host (it does on any Linux
  desktop; WSL2 needs usbipd). Remove that `runArgs` line if the container refuses to start.
- Opening a meeting link from an action runs `xdg-open` **inside the container**, where no
  browser is installed. The link is logged at INFO level in `logs.log` so you can verify it.
