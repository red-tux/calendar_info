#!/usr/bin/env bash
# Convenience wrapper for manually testing plugins against the repo-local data dir.
# Clears the previous run's logs, closes any already-running instance, and tees
# console output (GTK warnings, uncaught tracebacks) alongside the loguru log file.
#
# post-create.sh copies this file to /workspaces/StreamController/run_dev.sh (the StreamController checkout root)
# inside the dev container, which is where it has to live: it cd's to its own directory.
set -e
cd "$(dirname "$0")"

# WAYLAND_DISPLAY must be a bare socket name that resolves under XDG_RUNTIME_DIR: the
# `wayland` package StreamController initialises joins the two blindly, so an absolute
# path (what older containers of this repo set) or a stale name aborts startup with a
# FileNotFoundError before any window appears.
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    wl="$(basename "$WAYLAND_DISPLAY")"
    if [ ! -S "${XDG_RUNTIME_DIR:-/run/user/1000}/$wl" ] && [ -S "/run/host-user/$wl" ]; then
        ln -sfn "/run/host-user/$wl" "${XDG_RUNTIME_DIR:-/run/user/1000}/$wl"
    fi
    if [ -S "${XDG_RUNTIME_DIR:-/run/user/1000}/$wl" ]; then
        export WAYLAND_DISPLAY="$wl"
    else
        echo "No Wayland socket for '$WAYLAND_DISPLAY'; falling back to X11 (${DISPLAY:-no DISPLAY either})"
        unset WAYLAND_DISPLAY
    fi
fi

mkdir -p data/logs
: > data/logs/logs.log

CONSOLE_LOG="data/logs/run-console.log"
echo "Structured log: data/logs/logs.log"
echo "Console output (this run): $CONSOLE_LOG"
echo

python3 main.py --devel --data data --close-running 2>&1 | tee "$CONSOLE_LOG"
