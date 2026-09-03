"""Plugin-level settings screen: the calendar list plus refresh/display options.

Built manually because plugin-level settings have no GenerativeUI widget set (that's only for
per-action settings stored in page JSON). Returned from PluginBase.get_settings_area(), which
the app drops into an Adw.PreferencesPage - so this has to be a single Adw.PreferencesGroup.

Layout, top to bottom: option rows, then a "Calendars" list where each calendar is an
expander row holding its name, address, color, test and remove controls.
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


class CalendarSettingsGroup(Adw.PreferencesGroup):
    def __init__(self, plugin_base):
        super().__init__(
            title="Calendar Info",
            description=(
                "Any iCalendar (.ics) address or file works. Google Calendar: Settings → the "
                "calendar → 'Integrate calendar' → copy the 'Secret address in iCal format'."
            ),
        )
        self.plugin_base = plugin_base
        self._rows: dict[str, CalendarRow] = {}
        self._store_token = None

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

        # --- google account --------------------------------------------------------------
        self._auth_flow_id = None
        self._build_google_section()

        # --- calendars -------------------------------------------------------------------
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_top=18, margin_bottom=6)
        header.append(Gtk.Label(label="Calendars", xalign=0, hexpand=True, css_classes=["heading"]))
        add_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add calendar", css_classes=["flat"])
        add_button.connect("clicked", self._on_add_clicked)
        header.append(add_button)
        self.add(header)

        self.calendar_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.add(self.calendar_list)

        self.empty_row = Adw.ActionRow(
            title="No calendars yet",
            subtitle="Click + for an iCalendar address or file, or connect a Google account above",
        )
        for calendar in plugin_base.get_calendars():
            self._add_row(calendar)
        self._update_empty_row()

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
        self.plugin_base.set_calendars([row.to_dict() for row in self._rows.values()])
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

    # --- google account ----------------------------------------------------------------------
    #
    # No OAuth client ships with the plugin: the user registers one in their own Google Cloud
    # project, exactly as Home Assistant's application_credentials does. That is what keeps
    # every install clear of Google's verification process and shared quota.

    def _build_google_section(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_top=18, margin_bottom=6)
        header.append(Gtk.Label(label="Google Calendar", xalign=0, hexpand=True, css_classes=["heading"]))
        guide_button = Gtk.Button(label="Setup guide", css_classes=["flat"], valign=Gtk.Align.CENTER)
        guide_button.connect("clicked", lambda *a: self._show_setup_guide())
        header.append(guide_button)
        self.add(header)

        self.google_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.add(self.google_list)

        credentials = self.plugin_base.get_google_credentials()
        self.client_row = Adw.ExpanderRow(title="OAuth client", subtitle="")
        self.client_id_row = Adw.EntryRow(title="Client ID", text=credentials["client_id"],
                                          show_apply_button=True)
        self.client_id_row.connect("apply", lambda *a: self._save_google_credentials())
        self.client_row.add_row(self.client_id_row)
        self.client_secret_row = Adw.PasswordEntryRow(title="Client secret",
                                                      text=credentials["client_secret"])
        self.client_secret_row.connect("apply", lambda *a: self._save_google_credentials())
        self.client_row.add_row(self.client_secret_row)

        guide_row = Adw.ActionRow(
            title="Where do these come from?",
            subtitle="Google has no API to create them - the guide opens each console page in order.",
        )
        guide_row_button = Gtk.Button(label="Open guide", valign=Gtk.Align.CENTER)
        guide_row_button.connect("clicked", lambda *a: self._show_setup_guide())
        guide_row.add_suffix(guide_row_button)
        self.client_row.add_row(guide_row)
        self.google_list.append(self.client_row)

        self.connect_status = Gtk.Label(label="", css_classes=["dim-label"], wrap=True,
                                        xalign=1, max_width_chars=40)
        self.connect_button = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER,
                                         css_classes=["suggested-action"])
        self.connect_button.connect("clicked", self._on_connect_clicked)
        self.connect_row = Adw.ActionRow(
            title="Connect a Google account",
            subtitle="Opens Google in your browser; the reply comes back to 127.0.0.1.",
        )
        self.connect_row.add_suffix(self.connect_status)
        self.connect_row.add_suffix(self.connect_button)
        self.google_list.append(self.connect_row)

        self._account_rows: dict[str, Adw.ActionRow] = {}
        self._refresh_google_rows()

    def _save_google_credentials(self) -> None:
        self.plugin_base.set_google_credentials(self.client_id_row.get_text(),
                                                self.client_secret_row.get_text())
        self._refresh_google_rows()

    def _refresh_google_rows(self) -> None:
        credentials = self.plugin_base.get_google_credentials()
        if credentials["client_id"]:
            client_id = credentials["client_id"]
            shown = client_id if len(client_id) <= 24 else client_id[:12] + "…" + client_id[-8:]
            self.client_row.set_subtitle(shown)
        else:
            self.client_row.set_subtitle("Not configured yet")
        self.connect_button.set_sensitive(bool(credentials["client_id"]) and self._auth_flow_id is None)

        for row in self._account_rows.values():
            self.google_list.remove(row)
        self._account_rows.clear()
        # Linked accounts sit between the client row and the connect row.
        for position, account in enumerate(self.plugin_base.get_google_accounts(), start=1):
            row = Adw.ActionRow(title=account["email"] or "Google account", subtitle="Linked")
            add_button = Gtk.Button(label="Add calendars", valign=Gtk.Align.CENTER)
            add_button.connect("clicked", lambda _b, a=account: self._pick_google_calendars(a))
            row.add_suffix(add_button)
            remove_button = Gtk.Button(label="Disconnect", valign=Gtk.Align.CENTER,
                                       css_classes=["destructive-action"])
            remove_button.connect("clicked", lambda _b, a=account: self._confirm_disconnect(a))
            row.add_suffix(remove_button)
            self.google_list.insert(row, position)
            self._account_rows[account["id"]] = row

    # --- consent flow ------------------------------------------------------------------------

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
        if result.get("state") == "ok":
            email = result.get("email") or "your account"
            self.plugin_base.add_google_account(result.get("account_id", ""), result.get("email", ""))
            self.connect_status.set_label(f"Connected {email}")
            self._refresh_google_rows()
        else:
            self.connect_status.set_label(result.get("error") or "Authorization failed")
            self._refresh_google_rows()

    def _set_connecting(self, connecting: bool) -> None:
        self.connect_button.set_label("Cancel" if connecting else "Connect")
        self.connect_button.set_sensitive(True)
        if connecting:
            self.connect_button.remove_css_class("suggested-action")
        else:
            self.connect_button.add_css_class("suggested-action")

    def _confirm_disconnect(self, account: dict) -> None:
        dialog = Adw.AlertDialog(
            heading="Disconnect this Google account?",
            body=(f"{account['email']} will be unlinked, its stored login revoked, and every "
                  "calendar reading through it removed."),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("disconnect", "Disconnect")
        dialog.set_response_appearance("disconnect", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_disconnect_response, account)
        dialog.present(self)

    def _on_disconnect_response(self, _dialog, response: str, account: dict) -> None:
        if response != "disconnect":
            return
        # Revoking talks to Google, so drop the rows now and do the network part on a thread.
        for calendar_id in [cid for cid, row in self._rows.items()
                            if row.to_dict().get("account_id") == account["id"]]:
            row = self._rows.pop(calendar_id, None)
            if row is not None:
                self.calendar_list.remove(row)
        self._update_empty_row()
        self.connect_status.set_label("Disconnecting…")
        threading.Thread(target=self._disconnect_thread, args=(account,),
                         name="calendar_google_disconnect", daemon=True).start()

    def _disconnect_thread(self, account: dict) -> None:
        self.plugin_base.remove_google_account(account["id"])
        GLib.idle_add(self._on_disconnected)

    def _on_disconnected(self) -> None:
        self._refresh_google_rows()
        self.connect_status.set_label("Disconnected")

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

        header = Adw.HeaderBar()
        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(header)
        dialog = Adw.Dialog(title="Google Calendar setup", child=toolbar,
                            content_width=620, content_height=620)
        dialog.present(self)

    # --- calendar picker ---------------------------------------------------------------------

    def _pick_google_calendars(self, account: dict) -> None:
        self.connect_status.set_label("Loading calendars…")
        threading.Thread(target=self._list_calendars_thread, args=(account,),
                         name="calendar_google_list", daemon=True).start()

    def _list_calendars_thread(self, account: dict) -> None:
        result = self.plugin_base.google_list_calendars(account["id"])
        GLib.idle_add(self._show_calendar_picker, account, result)

    def _show_calendar_picker(self, account: dict, result: dict) -> None:
        if not result.get("ok"):
            self.connect_status.set_label(result.get("error") or "Could not list calendars")
            return
        self.connect_status.set_label("")

        already = {(c["account_id"], c["google_calendar"]) for c in self.plugin_base.get_calendars()}
        group = Adw.PreferencesGroup(
            title=f"Calendars on {account['email']}",
            description="Each one you add becomes a calendar entry with its own color and switch.",
        )
        checks: list[tuple[dict, Gtk.CheckButton]] = []
        for calendar in result.get("calendars", []):
            row = Adw.ActionRow(title=calendar.get("name") or calendar.get("id", ""),
                                subtitle=calendar.get("id", ""))
            if (account["id"], calendar.get("id")) in already:
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
        header = Adw.HeaderBar()
        add_button = Gtk.Button(label="Add selected", css_classes=["suggested-action"])
        header.pack_end(add_button)
        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(header)
        dialog = Adw.Dialog(title="Add Google calendars", child=toolbar,
                            content_width=560, content_height=560)
        add_button.connect("clicked", self._on_add_google_calendars, dialog, account, checks)
        dialog.present(self)

    def _on_add_google_calendars(self, _button, dialog, account: dict, checks) -> None:
        added = 0
        for calendar, check in checks:
            if not check.get_active():
                continue
            self._add_row({
                "id": uuid.uuid4().hex,
                "name": calendar.get("name") or "Google calendar",
                "type": "google",
                "source": "",
                "account_id": account["id"],
                "google_calendar": calendar.get("id", ""),
                "enabled": True,
                "color": _rgba_from_hex(calendar.get("color", "")) or list(DEFAULT_COLOR),
            })
            added += 1
        dialog.close()
        if added:
            self._update_empty_row()
            self._save_calendars()
            self.plugin_base.refresh_now()
            self.connect_status.set_label(f"Added {added} calendar{'' if added == 1 else 's'}")

    # --- calendar rows ---------------------------------------------------------------------

    def _on_add_clicked(self, button) -> None:
        calendar = {"id": uuid.uuid4().hex, "name": "New calendar", "source": "", "enabled": True, "color": list(DEFAULT_COLOR)}
        row = self._add_row(calendar)
        self._update_empty_row()
        row.set_expanded(True)
        self._save_calendars()

    def _add_row(self, calendar: dict) -> "CalendarRow":
        row = CalendarRow(self, calendar)
        self._rows[calendar["id"]] = row
        self.calendar_list.append(row)
        return row

    def remove_calendar(self, calendar_id: str) -> None:
        row = self._rows.pop(calendar_id, None)
        if row is not None:
            self.calendar_list.remove(row)
        self._update_empty_row()
        self._save_calendars()

    def _update_empty_row(self) -> None:
        has_rows = bool(self._rows)
        if not has_rows and self.empty_row.get_parent() is None:
            self.calendar_list.append(self.empty_row)
        elif has_rows and self.empty_row.get_parent() is not None:
            self.calendar_list.remove(self.empty_row)

    # --- status ----------------------------------------------------------------------------

    def update_status(self) -> None:
        store = self.plugin_base.event_store
        statuses = store.get_statuses()
        for calendar_id, row in self._rows.items():
            row.update_status(statuses.get(calendar_id))

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


class CalendarRow(Adw.ExpanderRow):
    def __init__(self, group: CalendarSettingsGroup, calendar: dict):
        super().__init__(title=calendar["name"] or "Calendar", subtitle="")
        self.group = group
        self.calendar_id = calendar["id"]
        self.calendar_type = calendar.get("type") or "ics"
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

        if self.calendar_type == "google":
            # A Google calendar is addressed by account + calendar id, both chosen in the
            # picker, so there is nothing here to type - or to test separately: the account's
            # own status row already reports what the last fetch did.
            account = next((a for a in group.plugin_base.get_google_accounts()
                            if a["id"] == self.account_id), None)
            self.add_row(Adw.ActionRow(
                title="Google account",
                subtitle=account["email"] if account else "Account no longer linked",
            ))
            self.add_row(Adw.ActionRow(title="Calendar", subtitle=self.google_calendar or "-"))
        else:
            self.source_row = Adw.EntryRow(
                title="Address (.ics URL, webcal:// or file path) - press Enter to apply",
                text=calendar.get("source", ""), show_apply_button=True,
            )
            self.source_row.connect("apply", lambda *a: self.group._save_calendars())
            self.add_row(self.source_row)

            self.test_label = Gtk.Label(label="", css_classes=["dim-label"], wrap=True, xalign=1, max_width_chars=40)
            self.test_button = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
            self.test_button.connect("clicked", self._on_test_clicked)
            test_row = Adw.ActionRow(title="Check this calendar", subtitle="Fetches the address once and reports what it found")
            test_row.add_suffix(self.test_label)
            test_row.add_suffix(self.test_button)
            self.add_row(test_row)

        remove_button = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
        remove_button.connect("clicked", lambda *a: self.group.remove_calendar(self.calendar_id))
        remove_row = Adw.ActionRow(title="Remove calendar")
        remove_row.add_suffix(remove_button)
        self.add_row(remove_row)

    def to_dict(self) -> dict:
        return {
            "id": self.calendar_id,
            "name": self.name_row.get_text().strip() or "Calendar",
            "type": self.calendar_type,
            "source": self.source_row.get_text().strip() if self.source_row is not None else "",
            "account_id": self.account_id,
            "google_calendar": self.google_calendar,
            "enabled": self.enabled_switch.get_active(),
            "color": _tuple_from_rgba(self.swatch.get_rgba()),
        }

    def _on_name_applied(self, *args) -> None:
        self.set_title(self.name_row.get_text().strip() or "Calendar")
        self.group._save_calendars()

    def update_status(self, status) -> None:
        if not self.enabled_switch.get_active():
            self.set_subtitle("Disabled")
        elif self.source_row is not None and not self.source_row.get_text().strip():
            self.set_subtitle("No address set")
        elif status is not None and status.needs_reauth:
            self.set_subtitle(f"Reconnect needed: {status.error}")
        elif status is None:
            self.set_subtitle("Not fetched yet")
        elif status.ok:
            plural = "" if status.event_count == 1 else "s"
            self.set_subtitle(f"{status.event_count} event{plural} in the fetch window")
        else:
            suffix = " (showing last good copy)" if status.from_cache else ""
            self.set_subtitle(f"Error: {status.error}{suffix}")

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
