"""Plugin-level settings screen: display options plus the calendar sources.

Built manually because plugin-level settings have no GenerativeUI widget set (that's only for
per-action settings stored in page JSON). Returned from PluginBase.get_settings_area(), which
the app drops into an Adw.PreferencesPage - so this has to be a single Adw.PreferencesGroup.

Layout, top to bottom: option rows, then one "Calendar sources" list. A source is either an
account (an expander holding the calendars read through it) or a standalone calendar (an
iCalendar feed, which has no account behind it). The single + button opens a dialog whose
choices are built from what the backend reports - the accounts your desktop already has, and
the calendar types that can be added by hand - so a new source or provider shows up here
without this file learning its name.
"""
import functools
import subprocess
import threading
import time
import uuid
import zoneinfo

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Gio

from loguru import logger as log

from .internal.events import TZ_EVENT, TZ_LOCAL, TZ_UTC, format_clock, local_tz, resolve_tz

TIME_FORMAT_OPTIONS = [("auto", "System default"), ("12", "12-hour"), ("24", "24-hour")]
DEFAULT_COLOR = (66, 133, 244, 255)
DEFAULT_ACCOUNT_PROVIDER = "oauth"
# How each account provider (backend/accounts/registry.py) is named in the UI.
PROVIDER_LABELS = {"oauth": "Google OAuth client", "kde": "KDE Online Accounts"}


@functools.lru_cache(maxsize=1)
def available_timezones() -> list[str]:
    """IANA zones for the picker. Sorted, and without the legacy single-word aliases that
    would otherwise bury the real ones (Etc/UTC and friends stay - people look for those)."""
    zones = [z for z in zoneinfo.available_timezones() if "/" in z]
    return sorted(zones)


AUTH_POLL_SECONDS = 2
AUTH_TIMEOUT_SECONDS = 320

# The Cloud Console has no API for creating a project, enabling an API or minting an OAuth
# client (the one that existed, the IAP OAuth Admin API, was shut down in March 2026), so the
# best we can do is put the user on the exact page for each step. Wording and ordering follow
# Home Assistant's setup instructions, which the same console changes keep in sync with us.
GOOGLE_SETUP_STEPS = [
    ("1. Create a Google Cloud project",
     "Any name will do. Skip if you already have one you want to reuse.",
     "https://console.cloud.google.com/projectcreate"),
    ("2. Enable the Google Calendar API",
     "Check the project selector at the top of the page first, then press Enable.",
     "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com"),
    ("3. Configure the consent screen",
     "App name and support email; choose External as the audience.",
     "https://console.cloud.google.com/auth/branding"),
    ("4. Publish the app",
     "Under Audience, press Publish app - otherwise Google expires the login every 7 days. "
     "Google will warn that the app is unverified; that is expected for your own client.",
     "https://console.cloud.google.com/auth/audience"),
    ("5. Create the OAuth client",
     "Create client → application type Desktop app. No redirect URI to fill in: this plugin "
     "receives the reply on 127.0.0.1.",
     "https://console.cloud.google.com/auth/clients"),
]


def open_uri(uri: str) -> None:
    """Open a link in the user's browser, the same way the actions open meeting links."""
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
        return
    except Exception as e:
        log.warning(f"Calendar Info - Gio could not open {uri}: {e}; trying xdg-open")
    try:
        subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.error(f"Calendar Info - xdg-open failed for {uri}: {e}")


def _rgba_from_hex(value: str) -> list[int] | None:
    """Google hands calendar colors back as '#9fe1e7'; reuse them for the key stripe."""
    text = (value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return [int(text[i:i + 2], 16) for i in (0, 2, 4)] + [255]
    except ValueError:
        return None


def _rgba_from_tuple(color) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    r, g, b, a = (list(color) + [255, 255, 255, 255])[:4]
    rgba.red, rgba.green, rgba.blue, rgba.alpha = r / 255, g / 255, b / 255, a / 255
    return rgba


def _tuple_from_rgba(rgba: Gdk.RGBA) -> list[int]:
    return [round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255), round(rgba.alpha * 255)]


def _escape(text: str) -> str:
    """Every hand-built Adw row title is parsed as Pango markup, so a calendar called
    'Personal & Family' would silently blank the row."""
    return GLib.markup_escape_text(str(text or ""))


def _account_title(account: dict) -> str:
    return account.get("email") or account.get("label") or "Account"


def _provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider)


class CalendarSettingsGroup(Adw.PreferencesGroup):
    def __init__(self, plugin_base):
        super().__init__(
            title="Calendar Info",
            description="Add the accounts and calendar feeds your keys read from.",
        )
        self.plugin_base = plugin_base
        self._calendar_rows: dict[str, CalendarRow] = {}
        self._account_rows: dict[tuple[str, str], AccountRow] = {}
        self._store_token = None
        self._auth_flow_id = None
        self._add_dialog = None

        settings = plugin_base.get_settings()

        # --- options ---------------------------------------------------------------------
        self.refresh_row = Adw.SpinRow(
            title="Refresh Interval (minutes)",
            adjustment=Gtk.Adjustment.new(int(settings.get("refresh_minutes", 5) or 5), 1, 120, 1, 5, 0),
        )
        self.refresh_row.set_digits(0)
        self.refresh_row.connect("changed", lambda row: self._save("refresh_minutes", int(row.get_value())))
        self.add(self.refresh_row)

        self.days_row = Adw.SpinRow(
            title="Look Ahead (days)", subtitle="How far ahead to fetch events",
            adjustment=Gtk.Adjustment.new(int(settings.get("days_ahead", 7) or 7), 1, 31, 1, 7, 0),
        )
        self.days_row.set_digits(0)
        self.days_row.connect("changed", lambda row: self._save("days_ahead", int(row.get_value())))
        self.add(self.days_row)

        self.time_format_row = Adw.ComboRow(title="Time Format")
        self.time_format_row.set_model(Gtk.StringList.new([label for _, label in TIME_FORMAT_OPTIONS]))
        current = settings.get("time_format", "auto")
        self.time_format_row.set_selected(next((i for i, (key, _) in enumerate(TIME_FORMAT_OPTIONS) if key == current), 0))
        self.time_format_row.connect("notify::selected", self._on_time_format_changed)
        self.add(self.time_format_row)

        self.timezone_row = Adw.ComboRow(
            title="Display Timezone",
            subtitle="Which timezone event times are shown in",
        )
        self._timezone_values = [TZ_LOCAL, TZ_EVENT, TZ_UTC] + available_timezones()
        labels = [
            f"System default ({local_tz()})",
            "Event's own timezone",
            "UTC",
        ] + available_timezones()
        self.timezone_row.set_model(Gtk.StringList.new(labels))
        # 400+ zones, so the dropdown gets a search box. Its default is prefix matching,
        # which makes a zone unfindable by its city ("new" would not match
        # "America/New_York") - substring matching is what people expect here.
        self.timezone_row.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.timezone_row.set_enable_search(True)
        self.timezone_row.set_search_match_mode(Gtk.StringFilterMatchMode.SUBSTRING)
        current_tz = str(settings.get("display_timezone") or TZ_LOCAL)
        self.timezone_row.set_selected(
            self._timezone_values.index(current_tz) if current_tz in self._timezone_values else 0)
        self.timezone_row.connect("notify::selected", self._on_timezone_changed)
        self.add(self.timezone_row)

        self.hide_all_day_row = Adw.SwitchRow(
            title="Hide All-Day Events", subtitle="Overrides the per-action 'Include All-Day Events' switch",
            active=bool(settings.get("hide_all_day", False)),
        )
        self.hide_all_day_row.connect("notify::active", lambda row, _p: self._save("hide_all_day", row.get_active()))
        self.add(self.hide_all_day_row)

        self.status_label = Gtk.Label(label="", css_classes=["dim-label"])
        self.refresh_button = Gtk.Button(label="Refresh Now", valign=Gtk.Align.CENTER)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)
        status_row = Adw.ActionRow(title="Status")
        status_row.add_suffix(self.status_label)
        status_row.add_suffix(self.refresh_button)
        self.add(status_row)

        # --- calendar sources --------------------------------------------------------------
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_top=18, margin_bottom=6)
        header.append(Gtk.Label(label="Calendar sources", xalign=0, hexpand=True, css_classes=["heading"]))
        self.add_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a calendar source",
                                     css_classes=["flat"])
        self.add_button.connect("clicked", self._on_add_clicked)
        header.append(self.add_button)
        self.add(header)

        self.sources_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.add(self.sources_list)

        self.empty_row = Adw.ActionRow(
            title="No calendar sources yet",
            subtitle="Press + to link an account from your desktop or add an iCalendar address",
        )

        self._refresh_sources()
        self.update_status()
        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)

    # --- lifecycle -------------------------------------------------------------------------

    def _on_realize(self, *args) -> None:
        if self._store_token is None:
            self._store_token = self.plugin_base.event_store.subscribe(self.update_status)

    def _on_unrealize(self, *args) -> None:
        if self._store_token is not None:
            self.plugin_base.event_store.unsubscribe(self._store_token)
            self._store_token = None

    # --- persistence -----------------------------------------------------------------------

    def _save(self, key: str, value) -> None:
        settings = self.plugin_base.get_settings()
        settings[key] = value
        self.plugin_base.set_settings(settings)
        self.plugin_base.on_settings_changed()

    def _save_calendars(self) -> None:
        """Persist what the rows currently hold. Does not rebuild the list - the row being
        edited would be destroyed under the cursor."""
        self.plugin_base.set_calendars([row.to_dict() for row in self._calendar_rows.values()])
        self.update_status()

    def _on_time_format_changed(self, row, _param) -> None:
        key, _ = TIME_FORMAT_OPTIONS[row.get_selected()]
        self._save("time_format", key)

    def _on_timezone_changed(self, row, _param) -> None:
        index = row.get_selected()
        if 0 <= index < len(self._timezone_values):
            self._save("display_timezone", self._timezone_values[index])

    def _on_refresh_clicked(self, button) -> None:
        self.plugin_base.refresh_now()
        self.status_label.set_label("Refreshing…")

    # --- the sources list --------------------------------------------------------------------

    def _refresh_sources(self) -> None:
        """Rebuild the list from the saved configuration: one row per account (holding the
        calendars read through it), then one per calendar that has no account."""
        for row in list(self._account_rows.values()):
            self.sources_list.remove(row)
        for row in list(self._calendar_rows.values()):
            if row.get_parent() is self.sources_list:
                self.sources_list.remove(row)
        if self.empty_row.get_parent() is not None:
            self.sources_list.remove(self.empty_row)
        self._account_rows.clear()
        self._calendar_rows.clear()

        calendars = self.plugin_base.get_calendars()
        for account in self.plugin_base.get_accounts():
            key = (account["provider"], account["id"])
            mine = [c for c in calendars
                    if c["account_provider"] == account["provider"] and c["account_id"] == account["id"]]
            row = AccountRow(self, account, mine)
            self.sources_list.append(row)
            self._account_rows[key] = row

        for calendar in calendars:
            if not calendar["account_id"]:
                row = CalendarRow(self, calendar)
                self.sources_list.append(row)
                self._calendar_rows[calendar["id"]] = row

        if not self._account_rows and not self._calendar_rows:
            self.sources_list.append(self.empty_row)
        self.update_status()

    def register_calendar_row(self, row: "CalendarRow") -> None:
        """Account rows build their own calendar rows; they still have to be saved and status
        -updated with everything else."""
        self._calendar_rows[row.calendar_id] = row

    def remove_calendar(self, calendar_id: str) -> None:
        row = self._calendar_rows.pop(calendar_id, None)
        if row is not None:
            parent = row.get_parent()
            if parent is self.sources_list:
                self.sources_list.remove(row)
        self._save_calendars()
        self._refresh_sources()

    def add_calendars(self, calendars: list[dict]) -> None:
        """Append new calendar entries and persist, without disturbing existing rows' edits."""
        if not calendars:
            return
        existing = [row.to_dict() for row in self._calendar_rows.values()]
        self.plugin_base.set_calendars(existing + calendars)
        self._refresh_sources()
        self.plugin_base.refresh_now()

    # --- add a source --------------------------------------------------------------------------

    def _on_add_clicked(self, button) -> None:
        """Open the add dialog straight away and fill it in when the backend answers - both
        the source descriptions and desktop discovery are round trips."""
        self._add_view = Adw.NavigationView()
        dialog = Adw.Dialog(title="Add a calendar source", content_width=560, content_height=560)
        dialog.set_child(self._add_view)
        self._add_dialog = dialog

        self._add_page_box = Adw.PreferencesPage()
        loading = Adw.PreferencesGroup()
        loading.add(Adw.ActionRow(title="Looking for accounts on this desktop…"))
        self._add_page_box.add(loading)
        self._add_view.push(Adw.NavigationPage(child=_with_header(self._add_page_box),
                                               title="Add a calendar source"))
        dialog.present(self)
        threading.Thread(target=self._load_add_options, name="calendar_add_options", daemon=True).start()

    def _load_add_options(self) -> None:
        sources = self.plugin_base.describe_sources()
        discovered = self.plugin_base.list_desktop_accounts()
        # Reporting only - asking would pop the app's permission dialog at someone who may
        # only want to paste an .ics address.
        missing = self.plugin_base.missing_provider_permissions("kde")
        GLib.idle_add(self._populate_add_dialog, sources, discovered, missing)

    def _populate_add_dialog(self, sources: list[dict], discovered: dict,
                             missing: list[str]) -> None:
        if self._add_dialog is None:
            return
        page = Adw.PreferencesPage()
        linked = {(a["provider"], a["id"]) for a in self.plugin_base.get_accounts()}
        accounts = [a for a in (discovered.get("accounts") or []) if (a["provider"], a["id"]) not in linked]

        desktop = Adw.PreferencesGroup(
            title="From this desktop",
            description="Accounts already set up in your desktop's online accounts. The desktop "
                        "keeps the login; this plugin never stores one.",
        )
        if accounts:
            for account in accounts:
                row = Adw.ActionRow(title=_escape(_account_title(account)),
                                    subtitle=_escape(_provider_label(account["provider"])))
                if account.get("supported"):
                    button = Gtk.Button(label="Link", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
                    button.connect("clicked", lambda _b, a=account: self._link_desktop_account(a))
                    row.add_suffix(button)
                else:
                    # Discovery found it and knows what it is; nothing can read it yet.
                    row.set_subtitle(_escape(account.get("detail") or "Not supported yet"))
                    row.set_sensitive(False)
                desktop.add(row)
        else:
            message = discovered.get("error") or (
                "No accounts found. Add one in System Settings → Online Accounts, or use a "
                "manual option below.")
            desktop.add(Adw.ActionRow(title="Nothing to link", subtitle=_escape(message), subtitle_lines=3))
        if missing:
            # Discovery only reads a file, so accounts can be listed while the login service is
            # still out of reach - say so here rather than at the first failed token request.
            row = Adw.ActionRow(
                title="One permission is still needed",
                subtitle="StreamController's sandbox cannot reach the service that hands out "
                         "your desktop's logins yet.",
                subtitle_lines=3,
            )
            button = Gtk.Button(label="Show command", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *a: self._show_permission_commands(missing))
            row.add_suffix(button)
            desktop.add(row)
        page.add(desktop)

        manual = Adw.PreferencesGroup(title="Add manually")
        has_desktop_google = any(a.get("calendar_type") == "google" for a in accounts)
        for source in sources:
            if source["needs_account"]:
                for provider in source["providers"]:
                    row = Adw.ActionRow(
                        title=_escape(f"{source['label']} ({_provider_label(provider)})"),
                        subtitle="Register your own OAuth client in the Google Cloud console"
                                 + (" - your desktop already has a Google account, linking that "
                                    "above is simpler" if has_desktop_google else ""),
                        subtitle_lines=3, activatable=True,
                    )
                    row.connect("activated", lambda _r: self._push_oauth_page())
                    manual.add(row)
            else:
                row = Adw.ActionRow(title=_escape(source["label"]),
                                    subtitle="A public or secret address, or a file on this machine",
                                    activatable=True)
                row.connect("activated", lambda _r, s=source: self._push_manual_page(s))
                manual.add(row)
        page.add(manual)

        self._add_view.pop()
        self._add_view.push(Adw.NavigationPage(child=_with_header(page), title="Add a calendar source"))

    def _close_add_dialog(self) -> None:
        if self._add_dialog is not None:
            self._add_dialog.close()
            self._add_dialog = None

    # --- add: a calendar typed in by hand ------------------------------------------------------

    def _push_manual_page(self, source: dict) -> None:
        """One entry row per field the source says it needs, so a new manual type needs no
        code here."""
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_escape(source["label"]))
        name_row = Adw.EntryRow(title="Name", text="New calendar")
        group.add(name_row)
        field_rows = {}
        for field in source["manual_fields"]:
            row = Adw.EntryRow(title=_escape(field.get("label") or field["key"]))
            if field.get("placeholder"):
                row.set_tooltip_text(field["placeholder"])
            group.add(row)
            field_rows[field["key"]] = row
        page.add(group)

        add_button = Gtk.Button(label="Add", css_classes=["suggested-action"])
        add_button.connect("clicked", lambda _b: self._on_manual_add(source, name_row, field_rows))
        self._add_view.push(Adw.NavigationPage(child=_with_header(page, add_button),
                                               title=source["label"]))

    def _on_manual_add(self, source: dict, name_row, field_rows: dict) -> None:
        calendar = {
            "id": uuid.uuid4().hex,
            "name": name_row.get_text().strip() or "Calendar",
            "type": source["id"],
            "source": "",
            "account_provider": "",
            "account_id": "",
            "google_calendar": "",
            "enabled": True,
            "color": list(DEFAULT_COLOR),
        }
        for key, row in field_rows.items():
            calendar[key] = row.get_text().strip()
        self._close_add_dialog()
        self.add_calendars([calendar])

    # --- add: Google through your own OAuth client ---------------------------------------------

    def _push_oauth_page(self) -> None:
        credentials = self.plugin_base.get_google_credentials()
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Your own OAuth client",
            description="Calendar Info ships no Google credentials, so nothing here is shared "
                        "with other users and no app verification is involved.",
        )
        self.client_id_row = Adw.EntryRow(title="Client ID", text=credentials["client_id"],
                                          show_apply_button=True)
        self.client_id_row.connect("apply", lambda *a: self._save_google_credentials())
        group.add(self.client_id_row)
        self.client_secret_row = Adw.PasswordEntryRow(title="Client secret",
                                                      text=credentials["client_secret"])
        self.client_secret_row.connect("apply", lambda *a: self._save_google_credentials())
        group.add(self.client_secret_row)

        guide_row = Adw.ActionRow(
            title="Where do these come from?",
            subtitle="Google has no API to create them - the guide opens each console page in order.",
            subtitle_lines=2,
        )
        guide_button = Gtk.Button(label="Open guide", valign=Gtk.Align.CENTER)
        guide_button.connect("clicked", lambda *a: self._show_setup_guide())
        guide_row.add_suffix(guide_button)
        group.add(guide_row)
        page.add(group)

        self.connect_status = Gtk.Label(label="", css_classes=["dim-label"], wrap=True, xalign=0)
        status_group = Adw.PreferencesGroup()
        status_group.add(self.connect_status)
        page.add(status_group)

        self.connect_button = Gtk.Button(label="Connect", css_classes=["suggested-action"])
        self.connect_button.connect("clicked", self._on_connect_clicked)
        self._add_view.push(Adw.NavigationPage(child=_with_header(page, self.connect_button),
                                               title="Google Calendar"))

    def _save_google_credentials(self) -> None:
        self.plugin_base.set_google_credentials(self.client_id_row.get_text(),
                                                self.client_secret_row.get_text())

    def _on_connect_clicked(self, button) -> None:
        if self._auth_flow_id is not None:
            self.plugin_base.google_cancel_auth(self._auth_flow_id)
            self._auth_flow_id = None
            self._set_connecting(False)
            self.connect_status.set_label("Cancelled")
            return
        # Whatever is in the entry rows is what we authorize with, applied or not.
        self._save_google_credentials()
        credentials = self.plugin_base.get_google_credentials()
        if not credentials["client_id"]:
            self.connect_status.set_label("Enter a client ID first")
            return
        self._set_connecting(True)
        self.connect_status.set_label("Asking Google…")
        threading.Thread(target=self._start_auth_thread, args=(credentials,),
                         name="calendar_google_connect", daemon=True).start()

    def _start_auth_thread(self, credentials: dict) -> None:
        result = self.plugin_base.google_start_auth(credentials["client_id"], credentials["client_secret"])
        GLib.idle_add(self._on_auth_started, result)

    def _on_auth_started(self, result: dict) -> None:
        if not result.get("ok"):
            self._set_connecting(False)
            self.connect_status.set_label(result.get("error") or "Could not start the authorization")
            return
        self._auth_flow_id = result.get("flow_id")
        self.connect_status.set_label("Waiting for the browser…")
        open_uri(result["auth_url"])
        threading.Thread(target=self._poll_auth_thread, args=(self._auth_flow_id,),
                         name="calendar_google_poll", daemon=True).start()

    def _poll_auth_thread(self, flow_id: str) -> None:
        deadline = time.monotonic() + AUTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._auth_flow_id != flow_id:
                return                      # cancelled, or superseded by another attempt
            result = self.plugin_base.google_poll_auth(flow_id)
            if result.get("state") != "pending":
                GLib.idle_add(self._on_auth_finished, flow_id, result)
                return
            time.sleep(AUTH_POLL_SECONDS)
        GLib.idle_add(self._on_auth_finished, flow_id,
                      {"state": "error", "error": "Timed out waiting for Google"})

    def _on_auth_finished(self, flow_id: str, result: dict) -> None:
        if self._auth_flow_id != flow_id:
            return
        self._auth_flow_id = None
        self._set_connecting(False)
        if result.get("state") != "ok":
            self.connect_status.set_label(result.get("error") or "Authorization failed")
            return
        account = {"provider": DEFAULT_ACCOUNT_PROVIDER, "id": result.get("account_id", ""),
                   "label": "", "email": result.get("email", ""), "calendar_type": "google"}
        self.plugin_base.add_account(account["provider"], account["id"], email=account["email"],
                                     calendar_type="google")
        self._close_add_dialog()
        self._refresh_sources()
        # Linking is only half the job - go straight to picking the calendars.
        self._pick_calendars(account)

    def _set_connecting(self, connecting: bool) -> None:
        self.connect_button.set_label("Cancel" if connecting else "Connect")
        self.connect_button.set_sensitive(True)
        if connecting:
            self.connect_button.remove_css_class("suggested-action")
        else:
            self.connect_button.add_css_class("suggested-action")

    # --- add: an account the desktop already has ------------------------------------------------

    def _link_desktop_account(self, account: dict) -> None:
        self.plugin_base.ensure_provider_permissions(account["provider"])
        self._close_add_dialog()
        self.status_label.set_label(f"Linking {_account_title(account)}…")
        threading.Thread(target=self._link_desktop_thread, args=(account,),
                         name="calendar_desktop_link", daemon=True).start()

    def _link_desktop_thread(self, account: dict) -> None:
        # The primary calendar's id is the account's address - the same trick the OAuth flow
        # uses - and listing it is the first real use of the desktop's token.
        result = self.plugin_base.list_calendars(account.get("calendar_type") or "google",
                                                 account["provider"], account["id"])
        email = ""
        for calendar in result.get("calendars") or []:
            if calendar.get("primary") and "@" in str(calendar.get("id") or ""):
                email = str(calendar["id"])
                break
        GLib.idle_add(self._on_desktop_linked, account, email, result)

    def _on_desktop_linked(self, account: dict, email: str, result: dict) -> None:
        # Linked even if the first token request failed: the account row's "Add calendars"
        # retries, and in a Flatpak the fix is granting the permissions shown below.
        self.plugin_base.add_account(account["provider"], account["id"],
                                     label=account.get("label", ""), email=email,
                                     calendar_type=account.get("calendar_type") or "google")
        linked = dict(account, email=email)
        self._refresh_sources()
        if result.get("ok"):
            self.status_label.set_label(f"Linked {email or _account_title(account)}")
            self._show_calendar_picker(linked, result)
        else:
            self.status_label.set_label(f"Linked, but the desktop login failed: {result.get('error')}")
            self._show_permission_commands(self.plugin_base.ensure_provider_permissions(account["provider"]))

    def _show_permission_commands(self, commands: list[str]) -> None:
        """A Flatpak hides the desktop's login service until the user grants the bus name. The
        app's own dialog asks for it, but it can be dismissed - and nothing asks for filesystem
        paths - so whatever is still missing is shown as a command to run."""
        if not commands:
            return
        dialog = Adw.AlertDialog(
            heading="Let StreamController reach your desktop accounts",
            body=("StreamController runs in a Flatpak sandbox, which hides the service that "
                  "hands out your desktop's logins. Run this once in a terminal, then restart "
                  "StreamController:"),
        )
        dialog.set_extra_child(Gtk.Label(label="\n".join(commands), selectable=True, wrap=True,
                                         xalign=0, css_classes=["monospace"]))
        dialog.add_response("ok", "OK")
        dialog.present(self)

    # --- an account's calendars ------------------------------------------------------------------

    def _pick_calendars(self, account: dict) -> None:
        self.status_label.set_label("Loading calendars…")
        threading.Thread(target=self._list_calendars_thread, args=(account,),
                         name="calendar_list", daemon=True).start()

    def _list_calendars_thread(self, account: dict) -> None:
        result = self.plugin_base.list_calendars(account.get("calendar_type") or "google",
                                                 account["provider"], account["id"])
        GLib.idle_add(self._show_calendar_picker, account, result)

    def _show_calendar_picker(self, account: dict, result: dict) -> None:
        if not result.get("ok"):
            self.status_label.set_label(result.get("error") or "Could not list calendars")
            return
        self.update_status()

        already = {(c["account_provider"], c["account_id"], c["google_calendar"])
                   for c in self.plugin_base.get_calendars()}
        group = Adw.PreferencesGroup(
            title=_escape(f"Calendars on {_account_title(account)}"),
            description="Each one you add becomes a calendar entry with its own color and switch.",
        )
        checks: list[tuple[dict, Gtk.CheckButton]] = []
        for calendar in result.get("calendars", []):
            row = Adw.ActionRow(title=_escape(calendar.get("name") or calendar.get("id", "")),
                                subtitle=_escape(calendar.get("id", "")))
            if (account["provider"], account["id"], calendar.get("id")) in already:
                row.set_subtitle("Already added")
                row.set_sensitive(False)
            else:
                check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                row.add_prefix(check)
                row.set_activatable_widget(check)
                checks.append((calendar, check))
            group.add(row)

        page = Adw.PreferencesPage()
        page.add(group)
        add_button = Gtk.Button(label="Add selected", css_classes=["suggested-action"])
        dialog = Adw.Dialog(title="Add calendars", child=_with_header(page, add_button),
                            content_width=560, content_height=560)
        add_button.connect("clicked", self._on_add_account_calendars, dialog, account, checks)
        dialog.present(self)

    def _on_add_account_calendars(self, _button, dialog, account: dict, checks) -> None:
        calendar_type = account.get("calendar_type") or "google"
        added = []
        for calendar, check in checks:
            if not check.get_active():
                continue
            added.append({
                "id": uuid.uuid4().hex,
                "name": calendar.get("name") or "Calendar",
                "type": calendar_type,
                "source": "",
                "account_provider": account["provider"],
                "account_id": account["id"],
                "google_calendar": calendar.get("id", ""),
                "enabled": True,
                "color": _rgba_from_hex(calendar.get("color", "")) or list(DEFAULT_COLOR),
            })
        dialog.close()
        self.add_calendars(added)
        if added:
            self.status_label.set_label(f"Added {len(added)} calendar{'' if len(added) == 1 else 's'}")

    # --- disconnecting an account -----------------------------------------------------------------

    def _confirm_disconnect(self, account: dict) -> None:
        dialog = Adw.AlertDialog(
            heading="Disconnect this account?",
            body=(f"{_account_title(account)} will be unlinked, whatever login this plugin stored "
                  "for it revoked, and every calendar reading through it removed."),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("disconnect", "Disconnect")
        dialog.set_response_appearance("disconnect", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_disconnect_response, account)
        dialog.present(self)

    def _on_disconnect_response(self, _dialog, response: str, account: dict) -> None:
        if response != "disconnect":
            return
        # Revoking talks to the network, so drop the rows now and do that part on a thread.
        self.status_label.set_label("Disconnecting…")
        threading.Thread(target=self._disconnect_thread, args=(account,),
                         name="calendar_disconnect", daemon=True).start()

    def _disconnect_thread(self, account: dict) -> None:
        self.plugin_base.remove_account(account["provider"], account["id"])
        GLib.idle_add(self._on_disconnected)

    def _on_disconnected(self) -> None:
        self._refresh_sources()
        self.status_label.set_label("Disconnected")

    # --- setup guide -------------------------------------------------------------------------

    def _show_setup_guide(self) -> None:
        page = Adw.PreferencesPage()
        steps = Adw.PreferencesGroup(
            title="Create your own Google OAuth client",
            description=(
                "Calendar Info ships no Google credentials of its own, so nothing here is shared "
                "with other users and no app verification is involved. Work through the steps in "
                "order, then paste the client ID and secret into the settings."
            ),
        )
        for title, subtitle, url in GOOGLE_SETUP_STEPS:
            row = Adw.ActionRow(title=title, subtitle=subtitle, subtitle_lines=3)
            button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda _b, u=url: open_uri(u))
            row.add_suffix(button)
            steps.add(row)
        page.add(steps)

        notes = Adw.PreferencesGroup(title="What to expect")
        for title, subtitle in [
            ("Google will call the app unverified",
             "Verification only matters for apps distributed to other people. On that screen "
             "choose Advanced, then the 'Go to …' link with your app's name."),
            ("Keep the app published",
             "An app left in Testing has its login expire after 7 days, and the calendars go stale "
             "with an 'invalid_grant' error."),
            ("Read-only access",
             "The only scope requested is calendar.readonly, so nothing here can change your "
             "calendar."),
        ]:
            notes.add(Adw.ActionRow(title=title, subtitle=subtitle, subtitle_lines=3))
        page.add(notes)

        dialog = Adw.Dialog(title="Google Calendar setup", child=_with_header(page),
                            content_width=620, content_height=620)
        dialog.present(self)

    # --- status ----------------------------------------------------------------------------

    def update_status(self) -> None:
        store = self.plugin_base.event_store
        statuses = store.get_statuses()
        for calendar_id, row in self._calendar_rows.items():
            row.update_status(statuses.get(calendar_id))
        for row in self._account_rows.values():
            row.update_status(statuses)

        if not store.is_backend_connected():
            text = "Starting calendar service…"
        else:
            updated = store.get_last_updated()
            count = len(store.get_events())
            if updated is None:
                text = "Waiting for first refresh"
            else:
                plural = "" if count == 1 else "s"
                options = self.plugin_base.options
                # "event" mode has no single zone for a timestamp of our own; use the machine's.
                tz = resolve_tz(options.display_timezone if options.display_timezone != TZ_EVENT else TZ_LOCAL)
                text = (f"{count} event{plural} · updated "
                        f"{format_clock(updated, options.time_format, tz)}")
        self.status_label.set_label(text)


def _with_header(page, end_button=None):
    """An Adw page wrapped in the toolbar view every dialog here uses."""
    header = Adw.HeaderBar()
    if end_button is not None:
        header.pack_end(end_button)
    toolbar = Adw.ToolbarView(content=page)
    toolbar.add_top_bar(header)
    return toolbar


class AccountRow(Adw.ExpanderRow):
    """One linked account and the calendars read through it."""

    def __init__(self, group: CalendarSettingsGroup, account: dict, calendars: list[dict]):
        super().__init__(title=_escape(_account_title(account)),
                         subtitle=_escape(_provider_label(account["provider"])))
        self.group = group
        self.account = account
        self.calendar_rows: list[CalendarRow] = []

        for calendar in calendars:
            row = CalendarRow(group, calendar)
            self.add_row(row)
            group.register_calendar_row(row)
            self.calendar_rows.append(row)

        if not calendars:
            self.add_row(Adw.ActionRow(title="No calendars from this account yet",
                                       subtitle="Press Add calendars to choose some"))

        actions = Adw.ActionRow(title="Manage this account")
        add_button = Gtk.Button(label="Add calendars", valign=Gtk.Align.CENTER)
        add_button.connect("clicked", lambda *a: self.group._pick_calendars(self.account))
        actions.add_suffix(add_button)
        remove_button = Gtk.Button(label="Disconnect", valign=Gtk.Align.CENTER,
                                   css_classes=["destructive-action"])
        remove_button.connect("clicked", lambda *a: self.group._confirm_disconnect(self.account))
        actions.add_suffix(remove_button)
        self.add_row(actions)

    def update_status(self, statuses: dict) -> None:
        provider = _provider_label(self.account["provider"])
        count = len(self.calendar_rows)
        if not count:
            self.set_subtitle(_escape(f"{provider} · no calendars"))
            return
        broken = sum(1 for row in self.calendar_rows
                     if (status := statuses.get(row.calendar_id)) is not None and not status.ok)
        suffix = f" · {broken} needs attention" if broken else ""
        plural = "" if count == 1 else "s"
        self.set_subtitle(_escape(f"{provider} · {count} calendar{plural}{suffix}"))


class CalendarRow(Adw.ExpanderRow):
    """One configured calendar: either standalone (an .ics feed, with its address here) or one
    read through an account, in which case the account row above owns the connection details."""

    def __init__(self, group: CalendarSettingsGroup, calendar: dict):
        super().__init__(title=_escape(calendar["name"] or "Calendar"), subtitle="")
        self.group = group
        self.calendar = dict(calendar)
        self.calendar_id = calendar["id"]
        self.calendar_type = calendar.get("type") or "ics"
        self.account_provider = calendar.get("account_provider") or ""
        self.account_id = calendar.get("account_id", "")
        self.google_calendar = calendar.get("google_calendar", "")
        self.source_row = None

        self.swatch = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        self.swatch.set_rgba(_rgba_from_tuple(calendar.get("color", DEFAULT_COLOR)))
        self.swatch.connect("notify::rgba", lambda *a: self.group._save_calendars())
        self.add_prefix(self.swatch)

        self.enabled_switch = Gtk.Switch(active=bool(calendar.get("enabled", True)), valign=Gtk.Align.CENTER)
        self.enabled_switch.connect("notify::active", lambda *a: self.group._save_calendars())
        self.add_suffix(self.enabled_switch)

        self.name_row = Adw.EntryRow(title="Name", text=calendar.get("name", ""), show_apply_button=True)
        self.name_row.connect("apply", self._on_name_applied)
        self.add_row(self.name_row)

        if not self.account_id:
            self._build_standalone_rows(calendar)

        remove_button = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
        remove_button.connect("clicked", lambda *a: self.group.remove_calendar(self.calendar_id))
        remove_row = Adw.ActionRow(title="Remove calendar")
        remove_row.add_suffix(remove_button)
        self.add_row(remove_row)

    def _build_standalone_rows(self, calendar: dict) -> None:
        self.source_row = Adw.EntryRow(
            title="Address (.ics URL, webcal:// or file path) - press Enter to apply",
            text=calendar.get("source", ""), show_apply_button=True,
        )
        self.source_row.connect("apply", lambda *a: self.group._save_calendars())
        self.add_row(self.source_row)

        self.test_label = Gtk.Label(label="", css_classes=["dim-label"], wrap=True, xalign=1, max_width_chars=40)
        self.test_button = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        self.test_button.connect("clicked", self._on_test_clicked)
        test_row = Adw.ActionRow(title="Check this calendar",
                                 subtitle="Fetches the address once and reports what it found")
        test_row.add_suffix(self.test_label)
        test_row.add_suffix(self.test_button)
        self.add_row(test_row)

    def to_dict(self) -> dict:
        return {
            "id": self.calendar_id,
            "name": self.name_row.get_text().strip() or "Calendar",
            "type": self.calendar_type,
            "source": self.source_row.get_text().strip() if self.source_row is not None else "",
            "account_provider": self.account_provider,
            "account_id": self.account_id,
            "google_calendar": self.google_calendar,
            "enabled": self.enabled_switch.get_active(),
            "color": _tuple_from_rgba(self.swatch.get_rgba()),
        }

    def _on_name_applied(self, *args) -> None:
        self.set_title(_escape(self.name_row.get_text().strip() or "Calendar"))
        self.group._save_calendars()

    def update_status(self, status) -> None:
        if not self.enabled_switch.get_active():
            self.set_subtitle("Disabled")
        elif self.source_row is not None and not self.source_row.get_text().strip():
            self.set_subtitle("No address set")
        elif status is not None and status.needs_reauth:
            self.set_subtitle(_escape(f"Reconnect needed: {status.error}"))
        elif status is None:
            self.set_subtitle("Not fetched yet")
        elif status.ok:
            plural = "" if status.event_count == 1 else "s"
            self.set_subtitle(f"{status.event_count} event{plural} in the fetch window")
        else:
            suffix = " (showing last good copy)" if status.from_cache else ""
            self.set_subtitle(_escape(f"Error: {status.error}{suffix}"))

    def _on_test_clicked(self, button) -> None:
        source = self.source_row.get_text().strip()
        if not source:
            self.test_label.set_label("Enter an address first")
            return
        button.set_sensitive(False)
        self.test_label.set_label("Checking…")
        threading.Thread(target=self._test_thread, args=(source,), name="calendar_test", daemon=True).start()

    def _test_thread(self, source: str) -> None:
        result = self.group.plugin_base.test_calendar_source(source)
        GLib.idle_add(self._on_test_done, result)

    def _on_test_done(self, result: dict) -> None:
        self.test_button.set_sensitive(True)
        if result.get("ok"):
            count = result.get("count", 0)
            sample = ", ".join(result.get("sample") or [])
            text = f"OK: {count} event{'' if count == 1 else 's'}"
            if sample:
                text += f" ({sample})"
        else:
            text = f"Failed: {result.get('error') or 'unknown error'}"
        self.test_label.set_label(text)
