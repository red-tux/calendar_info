#!/usr/bin/env bash
# Runs every time the KDE-flavoured container starts (see kde/devcontainer.json).
# Does everything the default post-start does, then wires up the host's account database.
set -uo pipefail

SCRIPTS="$(cd "$(dirname "$0")/../scripts" && pwd)"
KDE_DIR="$(cd "$(dirname "$0")" && pwd)"

bash "$SCRIPTS/post-start.sh"

HOST_DB_DIR=/run/host-libaccounts
LOCAL_DB_DIR="$HOME/.config/libaccounts-glib"

echo
echo "==> KDE Online Accounts"
if [ ! -d "$HOST_DB_DIR" ]; then
    echo "!!  $HOST_DB_DIR is not mounted - is this container really using .devcontainer/kde?"
    exit 0
fi
if [ ! -f "$HOST_DB_DIR/accounts.db" ]; then
    echo "!!  No accounts.db on the host yet. Add an account in System Settings -> Online"
    echo "    Accounts, then restart this container."
    exit 0
fi

# libaccounts-glib opens <basedir>/libaccounts-glib/accounts.db, where basedir is $ACCOUNTS
# if set and $XDG_CONFIG_HOME otherwise - i.e. the override names the *parent*, and the host
# directory is mounted as the libaccounts-glib directory itself, so it gets linked into place
# instead. The mount is read-only; if the library insists on opening it read-write, fall back
# to a copy (which then goes stale whenever accounts change on the host, hence the warning).
mkdir -p "$(dirname "$LOCAL_DB_DIR")"
if [ ! -L "$LOCAL_DB_DIR" ] || [ "$(readlink "$LOCAL_DB_DIR")" != "$HOST_DB_DIR" ]; then
    rm -rf "$LOCAL_DB_DIR"
    ln -sfn "$HOST_DB_DIR" "$LOCAL_DB_DIR"
fi

# Accounts.Manager() is plain g_object_new, which skips the GInitable init that opens the
# database - the handle stays NULL and every query is a GLib CRITICAL rather than an error.
# Accounts.Manager.new() is the constructor that actually opens it, and a failure to open is
# still only a CRITICAL, so make those fatal to get a non-zero exit out of the probe.
if ! python3 - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Accounts", "1.0")
from gi.repository import Accounts, GLib
GLib.log_set_always_fatal(GLib.LogLevelFlags.LEVEL_CRITICAL)
manager = Accounts.Manager.new()
if manager is None:
    raise SystemExit(1)
manager.list()
PY
then
    echo "!!  The read-only account database could not be opened; using a copy instead."
    echo "!!  Restart the container after changing accounts on the host to refresh it."
    rm -f "$LOCAL_DB_DIR"
    mkdir -p "$LOCAL_DB_DIR"
    # The -wal sibling has to come too: KDE leaves the database in WAL mode, so a freshly
    # added account lives entirely in the write-ahead log and a lone accounts.db copy reads
    # back as zero accounts. (-shm is a rebuildable index; sqlite recreates it.)
    cp "$HOST_DB_DIR"/accounts.db "$HOST_DB_DIR"/accounts.db-wal "$LOCAL_DB_DIR"/ 2>/dev/null \
        || cp "$HOST_DB_DIR"/accounts.db "$LOCAL_DB_DIR"/
    chmod u+w "$LOCAL_DB_DIR"/accounts.db*
fi

python3 "$KDE_DIR/check-accounts.py"
exit 0
