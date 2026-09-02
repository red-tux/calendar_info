"""Plugin-level settings screen: the calendar list plus refresh/display options.

Built manually because plugin-level settings have no GenerativeUI widget set (that's only for
per-action settings stored in page JSON). Returned from PluginBase.get_settings_area(), which
the app drops into an Adw.PreferencesPage - so this has to be a single Adw.PreferencesGroup.

Layout, top to bottom: option rows, then a "Calendars" list where each calendar is an
expander row holding its name, address, color, test and remove controls.
"""
import threading
import uuid

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk

from .internal.events import format_clock

TIME_FORMAT_OPTIONS = [("auto", "System default"), ("12", "12-hour"), ("24", "24-hour")]
DEFAULT_COLOR = (66, 133, 244, 255)


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
            title="No calendars yet", subtitle="Click + to add an iCalendar address or file",
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

    def _on_refresh_clicked(self, button) -> None:
        self.plugin_base.refresh_now()
        self.status_label.set_label("Refreshing…")

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
                text = f"{count} event{plural} · updated {format_clock(updated, self.plugin_base.options.time_format)}"
        self.status_label.set_label(text)


class CalendarRow(Adw.ExpanderRow):
    def __init__(self, group: CalendarSettingsGroup, calendar: dict):
        super().__init__(title=calendar["name"] or "Calendar", subtitle="")
        self.group = group
        self.calendar_id = calendar["id"]

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
            "source": self.source_row.get_text().strip(),
            "enabled": self.enabled_switch.get_active(),
            "color": _tuple_from_rgba(self.swatch.get_rgba()),
        }

    def _on_name_applied(self, *args) -> None:
        self.set_title(self.name_row.get_text().strip() or "Calendar")
        self.group._save_calendars()

    def update_status(self, status) -> None:
        if not self.enabled_switch.get_active():
            self.set_subtitle("Disabled")
        elif not self.source_row.get_text().strip():
            self.set_subtitle("No address set")
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
