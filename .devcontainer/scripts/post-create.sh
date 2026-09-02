#!/usr/bin/env bash
# Runs once after the container is created (devcontainer.json postCreateCommand).
set -euo pipefail

PLUGIN_ID="net_red-tux_calendar_info"
APP=/workspaces/StreamController
SCRIPTS="$(cd "$(dirname "$0")" && pwd)"

echo "==> StreamController checkout: $(git -C "$APP" log -1 --format='%h %s (%D)')"

# shellcheck disable=SC1091
. /app/.venv/bin/activate

echo "==> Installing StreamController's Python requirements"
uv pip install -r "$APP/requirements.txt"

# Rebuild Pillow from source against the *system* FreeType. The prebuilt manylinux wheel
# bundles its own private FreeType; once GTK (which loads the system one) activates
# against a real display in the same process, PIL's text measurement returns garbage and
# labels silently render off-canvas. LD_PRELOAD does not fix it; a relink does.
PILLOW_PIN="$(grep -i '^pillow==' "$APP/requirements.txt" | head -n1 | cut -d= -f3 | tr -d '[:space:]')"
if [ -n "$PILLOW_PIN" ]; then
    echo "==> Rebuilding pillow==$PILLOW_PIN against the system FreeType"
    uv pip install --no-binary pillow --reinstall --no-deps "pillow==$PILLOW_PIN"
else
    echo "!!  Could not find a pillow pin in requirements.txt; skipping the FreeType rebuild"
fi

echo "==> Preparing the repo-local data dir"
mkdir -p "$APP/data/plugins" "$APP/data/logs"
if [ ! -e "$APP/data/plugins/$PLUGIN_ID/manifest.json" ]; then
    echo "!!  Expected this repository to be mounted at $APP/data/plugins/$PLUGIN_ID"
    echo "!!  (see workspaceMount in devcontainer.json). StreamController will not see the plugin."
fi

echo "==> Installing run_dev.sh into $APP"
install -m 0755 "$SCRIPTS/run_dev.sh" "$APP/run_dev.sh"

# Pre-build the plugin's backend venv so the first app launch doesn't stall on it.
# StreamController would do this itself (PluginBase.launch_backend -> __install__.py).
PLUGIN_DIR="$APP/data/plugins/$PLUGIN_ID"
if [ -f "$PLUGIN_DIR/__install__.py" ]; then
    echo "==> Building the plugin backend venv"
    (cd "$PLUGIN_DIR" && python "__install__.py") || echo "!!  Backend venv build failed; StreamController will retry on launch"
fi

cat <<'MSG'

==> Done.
    Run StreamController:   /workspaces/StreamController/run_dev.sh      (or the "StreamController (devel)" launch config)
    Optional - Claude Code: run `claude` in a terminal and sign in once. Its config,
    credentials and session history live in a named volume, so they survive rebuilds.
MSG
