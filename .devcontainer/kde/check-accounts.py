#!/usr/bin/env python3
"""Report whether this container can see the host's KDE Online Accounts.

Run it any time (`python3 .devcontainer/kde/check-accounts.py`); post-start.sh runs it once
per container start so a broken mount shows up immediately instead of as a mysteriously
empty account list in the plugin settings.

It only reads: account names and ids, provider definitions, and whether signond answers on
the bus. It never requests a token, so it cannot touch your Google account.
"""
import os
import sys

import gi

gi.require_version("Accounts", "1.0")
from gi.repository import Accounts, Gio, GLib  # noqa: E402

SIGNOND = "com.google.code.AccountsSSO.SingleSignOn"


def count_files(path: str, suffix: str) -> int:
    try:
        return len([f for f in os.listdir(path) if f.endswith(suffix)])
    except OSError:
        return -1


def check_definitions() -> bool:
    print("== Account definitions (from the host's /usr/share/accounts)")
    ok = True
    for var, suffix in (("AG_PROVIDERS", ".provider"),
                        ("AG_SERVICES", ".service"),
                        ("AG_SERVICE_TYPES", ".service-type")):
        path = os.environ.get(var)
        if not path:
            print(f"   {var:18} unset - libaccounts-glib will use its built-in default")
            continue
        found = count_files(path, suffix)
        if found < 0:
            print(f"   {var:18} {path} MISSING")
            ok = False
        else:
            # Providers are the only ones we actually need; KDE ships no calendar service.
            note = "  <- no *.provider here, check the path" if found == 0 and var == "AG_PROVIDERS" else ""
            print(f"   {var:18} {path} ({found} x {suffix}){note}")
            ok = ok and not (found == 0 and var == "AG_PROVIDERS")
    return ok


def check_accounts() -> bool:
    print("\n== Accounts in the host's database")
    database = os.path.join(GLib.get_user_config_dir(), "libaccounts-glib", "accounts.db")
    if not os.path.exists(database):
        # Calling into the library without a database only produces a GLib CRITICAL.
        print(f"   no database at {database}")
        print("   -> the host's ~/.config/libaccounts-glib is not linked in; see post-start.sh")
        return False
    print(f"   database: {database}"
          f"{' (a copy, restart to refresh)' if not os.path.islink(os.path.dirname(database)) else ''}")
    manager = Accounts.Manager.new()
    providers = [p.get_name() for p in manager.list_providers()]
    print(f"   providers known: {', '.join(providers) if providers else '(none)'}")
    account_ids = manager.list()
    if not account_ids:
        print("   no accounts found")
        print("   -> add one in System Settings -> Online Accounts on the host, or check that")
        print("      ~/.config/libaccounts-glib is mounted (see post-start output above)")
        return False
    for account_id in account_ids:
        account = manager.get_account(account_id)
        if account is None:
            continue
        services = [s.get_name() for s in account.list_enabled_services()]
        print(f"   [{account_id}] {account.get_display_name()!r} provider={account.get_provider_name()} "
              f"enabled={account.get_enabled()} services={services or '(none enabled)'}")
    return True


def check_signond() -> bool:
    print("\n== signond (the host's token daemon)")
    address = os.environ.get("CALENDAR_INFO_ACCOUNTS_DBUS_ADDRESS")
    print("   bus:", address or "(unset - would fall back to this container's session bus)")
    if not address:
        return False
    try:
        connection = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        reply = connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (SIGNOND,)), GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error as e:
        print(f"   cannot reach that bus: {e.message}")
        return False
    running = reply.unpack()[0]
    # signond is D-Bus activated, so "not running" is normal until something asks for a token.
    print(f"   {SIGNOND}: {'running' if running else 'not running (it starts on demand)'}")
    return True


if __name__ == "__main__":
    results = [check_definitions(), check_accounts(), check_signond()]
    print()
    if all(results):
        print("KDE account integration looks wired up.")
    else:
        print("Something above is not wired up yet - the plugin will simply offer no KDE accounts.")
    sys.exit(0)
