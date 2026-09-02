#!/usr/bin/env bash
# Runs every time the container starts (devcontainer.json postStartCommand).
set -uo pipefail

# Named volumes can come up root-owned on some Docker setups; fix the top level only.
# (Never recurse: /workspaces/StreamController/data/plugins/<plugin id> is the bind-mounted host workspace.)
for d in /workspaces/StreamController/data /workspaces/StreamController/data/plugins /home/ubuntu/.claude /commandhistory; do
    [ -d "$d" ] && [ ! -w "$d" ] && sudo chown ubuntu:ubuntu "$d"
done

# Private session bus for the app (StreamController talks D-Bus for single-instance
# handling and its API). Only start one.
BUS=/run/user/1000/bus
if [ ! -S "$BUS" ] || ! dbus-send --bus="unix:path=$BUS" --dest=org.freedesktop.DBus --print-reply / org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
    rm -f "$BUS"
    dbus-daemon --session --address="unix:path=$BUS" --fork --print-address
fi
exit 0
