#!/usr/bin/env bash
# Convenience wrapper for manually testing plugins against the repo-local data dir.
# Clears the previous run's logs, closes any already-running instance, and tees
# console output (GTK warnings, uncaught tracebacks) alongside the loguru log file.
#
# post-create.sh copies this file to /workspaces/StreamController/run_dev.sh (the StreamController checkout root)
# inside the dev container, which is where it has to live: it cd's to its own directory.
set -e
cd "$(dirname "$0")"

mkdir -p data/logs
: > data/logs/logs.log

CONSOLE_LOG="data/logs/run-console.log"
echo "Structured log: data/logs/logs.log"
echo "Console output (this run): $CONSOLE_LOG"
echo

python3 main.py --devel --data data --close-running 2>&1 | tee "$CONSOLE_LOG"
