#!/usr/bin/env bash
# Runs every time the container starts (devcontainer.json postStartCommand).
set -uo pipefail

# Named volumes can come up root-owned on some Docker setups; fix the top level only.
# (Never recurse: /workspaces/StreamController/data/plugins/<plugin id> is the bind-mounted host workspace.)
for d in /workspaces/StreamController/data /workspaces/StreamController/data/plugins /home/ubuntu/.claude /commandhistory; do
    [ -d "$d" ] && [ ! -w "$d" ] && sudo chown ubuntu:ubuntu "$d"
done

# Wayland socket. The host's XDG runtime dir is mounted at /run/host-user, but
# WAYLAND_DISPLAY has to stay a bare socket name (see devcontainer.json), so link the
# name into the container's own XDG_RUNTIME_DIR. A host without Wayland leaves no link
# behind and GTK falls back to X11 (run_dev.sh unsets WAYLAND_DISPLAY in that case:
# StreamController crashes on startup when it is set with no socket behind it).
WL="${WAYLAND_DISPLAY:-}"
if [ -n "$WL" ]; then
    WL="$(basename "$WL")"
    if [ -S "/run/host-user/$WL" ]; then
        ln -sfn "/run/host-user/$WL" "/run/user/1000/$WL"
    else
        echo "!!  No Wayland socket at /run/host-user/$WL; the container will use X11."
        rm -f "/run/user/1000/$WL"
    fi
fi

# Private session bus for the app (StreamController talks D-Bus for single-instance
# handling and its API). Only start one.
BUS=/run/user/1000/bus
if [ ! -S "$BUS" ] || ! dbus-send --bus="unix:path=$BUS" --dest=org.freedesktop.DBus --print-reply / org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
    rm -f "$BUS"
    dbus-daemon --session --address="unix:path=$BUS" --fork --print-address
fi
exit 0
