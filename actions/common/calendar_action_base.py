"""Shared plumbing for every Calendar Info action.

Mixed in ahead of KeyAction/DialAction, e.g. `class NextEvent(CalendarActionMixin, KeyAction)`.
Plain mixin (no __init__) so it doesn't disturb the KeyAction/DialAction/ActionCore MRO.

Threading: `on_events_changed()` arrives on the GTK main thread (EventStore dispatches through
GLib.idle_add), `on_ready()` is main-thread, `on_tick()` runs on a fresh per-tick thread once a
second, and event-assigner callbacks run on per-event threads. Every hardware push therefore
goes through `self.ui()` (GLib.idle_add), and the only cross-thread state is the render-dedup
key - a single attribute write, so no lock is needed.
"""
from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta

from loguru import logger as log
from PIL import Image, ImageDraw
from gi.repository import GLib, Gio

from GtkHelper.GenerativeUI.ComboRow import ComboRow
from GtkHelper.GenerativeUI.SpinRow import SpinRow
from GtkHelper.GenerativeUI.SwitchRow import SwitchRow

from ...internal.events import (
    CalendarEvent,
    format_countdown,
    format_day,
    format_remaining,
    format_start,
    truncate,
)

# --- Icon / Color asset keys (registered in main.py, user-overridable in Settings) -------------

ICON_EVENT = "event_icon"
ICON_MEETING = "meeting_icon"
ICON_NO_EVENT = "no_event_icon"
ICON_ERROR = "error_icon"
ICON_AGENDA = "agenda_icon"
ICON_COUNTDOWN = "countdown_icon"
ICON_ALERT = "alert_icon"

COLOR_ICON = "icon_color"
COLOR_BG_NORMAL = "background_normal_color"
COLOR_BG_WARN = "background_warning_color"
COLOR_BG_URGENT = "background_urgent_color"
COLOR_BG_IN_PROGRESS = "background_in_progress_color"
COLOR_BG_NO_EVENT = "background_no_event_color"
COLOR_STRIPE_DEFAULT = "calendar_stripe_default_color"
COLOR_PROGRESS = "progress_bar_color"

# Filenames relative to assets/icons/material/ (pre-rendered 512x512 PNGs - see NOTICE.md there).
ICON_ASSET_DEFAULTS = {
    ICON_EVENT: "event.png",
    ICON_MEETING: "videocam.png",
    ICON_NO_EVENT: "event_available.png",
    ICON_ERROR: "event_busy.png",
    ICON_AGENDA: "calendar_today.png",
    ICON_COUNTDOWN: "schedule.png",
    ICON_ALERT: "notifications_active.png",
}

COLOR_ASSET_DEFAULTS = {
    COLOR_ICON: (255, 255, 255, 255),
    COLOR_BG_NORMAL: (0, 0, 0, 0),
    COLOR_BG_WARN: (255, 171, 0, 255),
    COLOR_BG_URGENT: (220, 53, 69, 255),
    COLOR_BG_IN_PROGRESS: (0, 140, 90, 255),
    COLOR_BG_NO_EVENT: (0, 0, 0, 0),
    COLOR_STRIPE_DEFAULT: (66, 133, 244, 255),
    COLOR_PROGRESS: (66, 133, 244, 255),
}

# What each label slot can show.
LABEL_CHOICES = ["none", "title", "countdown", "time", "day", "calendar", "location", "position"]

LEVEL_NORMAL = "normal"
LEVEL_WARN = "warn"
LEVEL_URGENT = "urgent"
LEVEL_IN_PROGRESS = "in_progress"

DEFAULT_WARN_MINUTES = 15
DEFAULT_URGENT_MINUTES = 5
DEFAULT_TITLE_CHARS = 12

STRIPE_FRACTION = 0.08   # calendar color bar width, as a fraction of the shorter side


def darken(color: tuple[int, int, int, int], factor: float = 0.45) -> tuple[int, int, int, int]:
    r, g, b, a = color
    return (round(r * factor), round(g * factor), round(b * factor), a)


class CalendarActionMixin:
    _RENDER_CACHE_ATTRS = ("_last_render_key", "_last_rendered_labels")

    # --- lifecycle ------------------------------------------------------------------

    def _reset_render_cache(self) -> None:
        for attr in self._RENDER_CACHE_ATTRS:
            setattr(self, attr, None)

    def on_ready(self) -> None:
        # Idempotent: on_ready() re-fires on every page (re)load on the same instance, so an
        # unconditional subscribe would leak one subscription per revisit.
        if getattr(self, "_store_token", None) is None:
            self._store_token = self.plugin_base.event_store.subscribe(self.on_events_changed)
        self._reset_render_cache()
        self.render()

    def on_disconnect(self) -> None:
        token = getattr(self, "_store_token", None)
        if token is not None:
            self.plugin_base.event_store.unsubscribe(token)
            self._store_token = None
        super().on_disconnect()

    def on_events_changed(self) -> None:
        """EventStore fan-out (main thread). Force a repaint on the next render."""
        self._last_render_key = None
        self.render()

    def on_tick(self) -> None:
        """Once a second on a tick thread. Cheap when nothing displayed has changed."""
        self.render()

    def render(self) -> None:
        """Override: compute what should be on the hardware, compare against
        `_last_render_key`, and push only on change."""

    def can_render(self) -> bool:
        # set_media()/set_label() raise before the framework has called on_ready().
        return bool(getattr(self, "on_ready_called", False))

    # EventAssigner forwards the hardware callback's data (even None) to every callback;
    # the base classes' zero-arg defaults would crash on it, so give them a tolerant signature.
    def on_key_down(self, data=None) -> None: pass
    def on_key_up(self, data=None) -> None: pass
    def on_key_short_up(self, data=None) -> None: pass
    def on_key_hold_start(self, data=None) -> None: pass
    def on_key_hold_stop(self, data=None) -> None: pass
    def on_dial_down(self, data=None) -> None: pass
    def on_dial_up(self, data=None) -> None: pass
    def on_dial_short_up(self, data=None) -> None: pass
    def on_dial_hold_start(self, data=None) -> None: pass
    def on_dial_hold_stop(self, data=None) -> None: pass
    def on_dial_turn_cw(self, data=None) -> None: pass
    def on_dial_turn_ccw(self, data=None) -> None: pass
    def on_dial_short_touch_press(self, data=None) -> None: pass
    def on_dial_long_touch_press(self, data=None) -> None: pass

    # --- small utilities -------------------------------------------------------------

    def ui(self, fn, *args, **kwargs) -> None:
        """Marshal a set_media()/set_label()-style call onto the GTK main thread."""
        GLib.idle_add(lambda: fn(*args, **kwargs))

    @staticmethod
    def now() -> datetime:
        return datetime.now().astimezone()

    def get_display_size(self, fallback: tuple[int, int] = (72, 72)) -> tuple[int, int]:
        try:
            width, height = self.get_state().controller_input.get_image_size()
        except Exception:
            return fallback
        if width <= 0 or height <= 0:
            return fallback
        return width, height

    @property
    def store(self):
        return self.plugin_base.event_store

    @property
    def options(self):
        return self.plugin_base.options

    def open_link(self, url: str | None) -> None:
        """Open a meeting link in the default browser. Runs on the event thread."""
        if not url:
            log.info(f"{self.action_id} - no link to open")
            return
        log.info(f"{self.action_id} - opening {url}")
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
            return
        except Exception as e:
            log.warning(f"{self.action_id} - Gio could not open {url}: {e}; trying xdg-open")
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.error(f"{self.action_id} - xdg-open failed for {url}: {e}")
            self.ui(self.show_error, 2)

    def refresh_calendars(self) -> None:
        self.plugin_base.refresh_now()

    # --- shared settings rows -----------------------------------------------------------

    def setup_label_rows(self, on_change, top_default="countdown", middle_default="none", bottom_default="title") -> None:
        self._last_rendered_labels = None
        # get_value(fallback) returns the literal fallback when the key is unset - it ignores the
        # row's own default - so remember the defaults each action asked for and pass those.
        self._label_defaults = (top_default, middle_default, bottom_default)
        self.top_label_row = ComboRow(self, "top_label", top_default, items=LABEL_CHOICES, title="Top Label", on_change=on_change)
        self.middle_label_row = ComboRow(self, "middle_label", middle_default, items=LABEL_CHOICES, title="Middle Label", on_change=on_change)
        self.bottom_label_row = ComboRow(self, "bottom_label", bottom_default, items=LABEL_CHOICES, title="Bottom Label", on_change=on_change)
        self.title_chars_row = SpinRow(
            self, "title_chars", DEFAULT_TITLE_CHARS, min=4, max=40, step=1, digits=0,
            title="Max Text Length", subtitle="Characters before a title/location is cut with …",
            on_change=on_change,
        )

    def setup_alert_rows(self, on_change) -> None:
        self.warn_minutes_row = SpinRow(
            self, "warn_minutes", DEFAULT_WARN_MINUTES, min=0, max=240, step=1, digits=0,
            title="Warning At (minutes before)", subtitle="Background turns to the warning color; 0 disables",
            on_change=on_change,
        )
        self.urgent_minutes_row = SpinRow(
            self, "urgent_minutes", DEFAULT_URGENT_MINUTES, min=0, max=120, step=1, digits=0,
            title="Urgent At (minutes before)", subtitle="Background turns to the urgent color; 0 disables",
            on_change=on_change,
        )
        self.flash_row = SwitchRow(self, "flash_urgent", True, title="Flash While Urgent", on_change=on_change)
        self.show_in_progress_row = SwitchRow(
            self, "show_in_progress", True, title="Show Running Event",
            subtitle="Off: jump to the next event as soon as one starts", on_change=on_change,
        )
        self.include_all_day_row = SwitchRow(
            self, "include_all_day", False, title="Include All-Day Events", on_change=on_change,
        )
        self.show_stripe_row = SwitchRow(self, "show_stripe", True, title="Show Calendar Color Bar", on_change=on_change)
        self.show_icon_row = SwitchRow(self, "show_icon", True, title="Show Icon", on_change=on_change)

    # Typed accessors: GenerativeUI.get_value(fallback) returns the literal fallback when the key
    # is unset (ignoring the row's own default), so always pass the row's default here.
    def warn_minutes(self) -> int: return int(self.warn_minutes_row.get_value(fallback=DEFAULT_WARN_MINUTES))
    def urgent_minutes(self) -> int: return int(self.urgent_minutes_row.get_value(fallback=DEFAULT_URGENT_MINUTES))
    def flash_enabled(self) -> bool: return bool(self.flash_row.get_value(fallback=True))
    def show_in_progress(self) -> bool: return bool(self.show_in_progress_row.get_value(fallback=True))
    def include_all_day(self) -> bool:
        return bool(self.include_all_day_row.get_value(fallback=False)) and not self.options.hide_all_day
    def show_stripe(self) -> bool: return bool(self.show_stripe_row.get_value(fallback=True))
    def show_icon(self) -> bool: return bool(self.show_icon_row.get_value(fallback=True))
    def title_chars(self) -> int: return int(self.title_chars_row.get_value(fallback=DEFAULT_TITLE_CHARS))

    # --- event -> display logic -------------------------------------------------------------

    def alert_level(self, event: CalendarEvent | None, now: datetime) -> str:
        if event is None:
            return LEVEL_NORMAL
        if event.is_in_progress(now):
            return LEVEL_IN_PROGRESS
        if self.store.is_dismissed(event.uid):
            return LEVEL_NORMAL
        seconds = event.seconds_until_start(now)
        urgent, warn = self.urgent_minutes(), self.warn_minutes()
        if urgent > 0 and seconds <= urgent * 60:
            return LEVEL_URGENT
        if warn > 0 and seconds <= warn * 60:
            return LEVEL_WARN
        return LEVEL_NORMAL

    def label_value(self, event: CalendarEvent | None, kind: str, now: datetime) -> str:
        if kind == "none":
            return ""
        if event is None:
            return "No events" if kind == "title" else ""
        if kind == "title":
            return truncate(event.title, self.title_chars())
        if kind == "countdown":
            if event.is_in_progress(now):
                return format_remaining(event.seconds_until_end(now))
            if event.all_day:
                return format_day(event.start, now)
            return format_countdown(event.seconds_until_start(now))
        if kind == "time":
            return format_start(event, now, self.options.time_format)
        if kind == "day":
            return format_day(event.start, now)
        if kind == "calendar":
            return truncate(event.calendar_name, self.title_chars())
        if kind == "location":
            return truncate(event.location, self.title_chars())
        if kind == "position":
            return self.position_text(event, now)
        return ""

    def position_text(self, event: CalendarEvent | None, now: datetime) -> str:
        """"2/5"-style indicator. Browsing actions override this; a plain key has none."""
        return ""

    def label_values(self, event: CalendarEvent | None, now: datetime) -> tuple[str, str, str]:
        top_default, middle_default, bottom_default = self._label_defaults
        return (
            self.label_value(event, self.top_label_row.get_value(fallback=top_default), now),
            self.label_value(event, self.middle_label_row.get_value(fallback=middle_default), now),
            self.label_value(event, self.bottom_label_row.get_value(fallback=bottom_default), now),
        )

    def push_labels(self, labels: tuple[str, str, str], force: bool = False) -> None:
        if not force and labels == getattr(self, "_last_rendered_labels", None):
            return
        self._last_rendered_labels = labels
        top, middle, bottom = labels
        self.ui(self.set_top_label, top)
        self.ui(self.set_center_label, middle)
        self.ui(self.set_bottom_label, bottom)

    def flash_phase(self) -> bool:
        """Alternates every second; drives the urgent flash without a timer."""
        return int(time.time()) % 2 == 0

    def icon_for(self, event: CalendarEvent | None) -> str:
        if event is None:
            return ICON_ERROR if (self.store.has_errors() and not self.store.get_events()) else ICON_NO_EVENT
        return ICON_MEETING if event.meeting_link else ICON_EVENT

    # --- compositing ------------------------------------------------------------------------

    def background_color(self, level: str, event: CalendarEvent | None, flash_on: bool) -> tuple[int, int, int, int]:
        if event is None:
            return self.get_asset_color(COLOR_BG_NO_EVENT)
        if level == LEVEL_IN_PROGRESS:
            return self.get_asset_color(COLOR_BG_IN_PROGRESS)
        if level == LEVEL_URGENT:
            color = self.get_asset_color(COLOR_BG_URGENT)
            return color if (flash_on or not self.flash_enabled()) else darken(color)
        if level == LEVEL_WARN:
            return self.get_asset_color(COLOR_BG_WARN)
        return self.get_asset_color(COLOR_BG_NORMAL)

    def compose(self, size: tuple[int, int], level: str, event: CalendarEvent | None, flash_on: bool,
                icon_key: str | None, icon_box: tuple[float, float, float, float] | None = None,
                icon_margin: float = 0.22, stripe: bool = True) -> Image.Image:
        """Background + calendar color stripe + tinted icon. Labels are layered by the app."""
        width, height = size
        image = Image.new("RGBA", (width, height), self.background_color(level, event, flash_on))
        if stripe and event is not None:
            stripe_w = max(2, round(min(width, height) * STRIPE_FRACTION))
            color = event.color or self.get_asset_color(COLOR_STRIPE_DEFAULT)
            ImageDraw.Draw(image).rectangle([0, 0, stripe_w, height], fill=color)
        if icon_key:
            box = icon_box or (0, 0, width, height)
            self.paste_asset_icon(image, icon_key, COLOR_ICON, box, margin_fraction=icon_margin)
        return image

    def draw_bar(self, image: Image.Image, fraction: float, height_fraction: float = 0.12,
                 color_key: str = COLOR_PROGRESS) -> None:
        width, height = image.size
        bar_h = max(2, round(height * height_fraction))
        fill_w = round(width * max(0.0, min(1.0, fraction)))
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, height - bar_h, width, height], fill=darken(self.get_asset_color(color_key), 0.3))
        if fill_w > 0:
            draw.rectangle([0, height - bar_h, fill_w, height], fill=self.get_asset_color(color_key))

    def get_asset_icon_image(self, icon_key: str, size: int) -> Image.Image | None:
        values = self.plugin_base.asset_manager.icons.get_asset_values(icon_key)
        if not values:
            return None
        _, rendered = values
        if rendered is None:
            return None
        return rendered.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")

    def get_asset_color(self, color_key: str) -> tuple[int, int, int, int]:
        color = self.plugin_base.asset_manager.colors.get_asset_values(color_key)
        return tuple(color) if color is not None else COLOR_ASSET_DEFAULTS.get(color_key, (255, 255, 255, 255))

    def paste_asset_icon(self, canvas: Image.Image, icon_key: str, color_key: str,
                         box: tuple[float, float, float, float], margin_fraction: float = 0.15) -> None:
        """Pastes an Icon asset, square and centered with a margin, into `box` on `canvas`,
        tinted with a Color asset - the icon's own alpha is the mask, so any shape the user
        swaps in gets recolored the same way the bundled Material Icons do."""
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        size = round(min(w, h) * (1 - margin_fraction * 2))
        if size <= 0:
            return
        base = self.get_asset_icon_image(icon_key, size)
        if base is None:
            return
        r, g, b, a = self.get_asset_color(color_key)
        colored = Image.new("RGBA", base.size, (r, g, b, 0))
        alpha = base.getchannel("A")
        if a != 255:
            alpha = alpha.point(lambda v: v * a // 255)
        colored.putalpha(alpha)
        px, py = round(x0 + (w - size) / 2), round(y0 + (h - size) / 2)
        canvas.paste(colored, (px, py), colored)
