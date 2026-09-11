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
in a named Docker volume, `calendar-info-claude-shared`. It survives container rebuilds, and
it is never bind-mounted from or copied to the host, so host credentials are not exposed to
the container and vice versa. Delete the volume (`docker volume ls | grep calendar-info`) to
sign out completely.

Unlike the other volumes it is *not* keyed by `${devcontainerId}`: the default container and
the KDE one below share it, so signing in, past sessions and Claude's memory carry across when
you switch between them. Everything else - the StreamController data dir, shell history - stays
per container. If you had signed in before this was shared, you will be asked to sign in once
more; to carry the old volume over instead, with the containers stopped:

```sh
docker volume ls | grep claude            # find the old …-claude-config-<id> volume
docker volume create calendar-info-claude-shared
docker run --rm -v <old>:/from -v calendar-info-claude-shared:/to alpine sh -c 'cp -a /from/. /to/'
```

## KDE Online Accounts (optional, opt-in container)

Only needed when working on the KDE integration, which lets a Google calendar take its
credentials from an account added in KDE's *System Settings -> Online Accounts* instead of an
OAuth client you registered yourself. **Everyone else can ignore this section** - the default
dev container is unchanged and never touches any of it.

Testing it needs a second container config, `.devcontainer/kde/`, because it mounts paths that
only exist on a KDE host. In VS Code: *Dev Containers: Reopen in Container* and pick
**"Calendar Info + KDE accounts"**.

### Before you rebuild

On the **host**, confirm the two mount sources exist, or the container will refuse to start:

```sh
ls /usr/share/accounts/providers/kde/     # kaccounts-providers - the OAuth definitions
ls ~/.config/libaccounts-glib/accounts.db # created when you add your first account
```

If `accounts.db` is missing, add an account in *System Settings -> Online Accounts* first.

### What it adds, and why

| | |
| --- | --- |
| `DESKTOP_ACCOUNTS=1` build arg | Installs `gir1.2-accounts-1.0`, `gir1.2-signon-2.0` and `gir1.2-goa-1.0`. The `kaccounts-providers` package is deliberately *not* installed: it pulls ~139 KDE/Qt packages for five XML files, which the mount below provides instead. |
| `/usr/share/accounts` -> `/run/host-accounts` (ro) | Provider definitions: OAuth client id, endpoints and scopes. `AG_PROVIDERS` points at the `providers/kde` subdirectory, because libaccounts-glib reads exactly one directory - it does not search subdirectories and does not accept a path list. |
| `~/.config/libaccounts-glib` -> `/run/host-libaccounts` (ro) | The account database. `post-start.sh` links it into `~/.config/libaccounts-glib` inside the container: libaccounts-glib's `ACCOUNTS` override names the *parent* of the `libaccounts-glib` directory, and what is mounted here is that directory itself. |
| `CALENDAR_INFO_ACCOUNTS_DBUS_ADDRESS` | The **host** session bus, where `signond` hands out tokens. The app itself keeps its own private bus, so `run_dev.sh --close-running` can never quit a StreamController running natively on your host. |

The daemons stay on the host: the container never sees a refresh token, it asks the host's
signond for a short-lived access token, and KWallet remains the thing guarding the secret.

### Checking the wiring

`post-start.sh` runs this on every start, and you can run it any time:

```sh
python3 .devcontainer/kde/check-accounts.py
```

It lists the providers it found, the accounts in the database, and whether signond answers.
It never requests a token, so it cannot touch the account itself.

### Notes

- **This container has its own StreamController data volume**, so your calendars from the
  default container are not there (Claude Code's volume *is* shared - see above). To copy the
  data dir across (with both containers stopped):
  ```sh
  docker volume ls | grep calendar-info        # find the two data volume names
  docker run --rm -v <old>:/from -v <new>:/to alpine sh -c 'cp -a /from/. /to/'
  ```
- The account database is mounted **read-only**. If libaccounts-glib refuses to open it that
  way, `post-start.sh` says so and falls back to a copy - which then goes stale, so restart
  the container after changing accounts on the host.
- Flatpak users of the released plugin need two `flatpak override --user` grants, because
  StreamController's manifest can't know about them: the session-bus name
  `com.google.code.AccountsSSO.SingleSignOn` (the plugin requests it through the app's own
  permission dialog) and read access to `xdg-config/libaccounts-glib` and `/usr/share/accounts`
  (the app has no dialog for filesystem grants, so the plugin shows the command to run). The
  GI typelibs installed here are a convenience: the plugin falls back to reading the account
  database and provider XML directly when `Accounts-1.0` isn't importable, which is the case
  inside the Flatpak.

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
