# Calendar Info

A [StreamController](https://github.com/StreamController/StreamController) plugin that puts your
calendar on a Stream Deck: the next event with a live countdown that changes color as it
approaches, a browsable agenda, and a press to join the meeting.

Works with any iCalendar (`.ics`) feed - a URL, a `webcal://` address, or a local file - so it
covers Google Calendar, Outlook/Microsoft 365, Nextcloud, iCloud, Fastmail and friends without
any OAuth setup.

## Setup

1. **Get your calendar's iCalendar address.** Google Calendar: *Settings → (your calendar) →
   Integrate calendar → Secret address in iCal format*. Treat it like a password; anyone with
   the address can read the calendar.
2. In **StreamController**: *Plugins → Calendar Info → settings (gear)*.
3. Click **+** under *Calendars*, give it a name, paste the address into *Address* and press
   Enter, pick a color. **Test** fetches it once and tells you how many events it found.
4. Add more calendars the same way. Each gets its own color bar on the keys.

Options on the same screen: refresh interval (default 5 minutes), how many days to look ahead
(default 7), 12/24-hour time, and hiding all-day events everywhere.

The last successful download of each calendar is kept on disk, so a network outage keeps
showing your schedule instead of blanking the deck.

## The actions

| Action | Input | What it does |
| --- | --- | --- |
| **Next Event** | Key | Your next (or currently running) event: countdown + title. Background turns amber, then red and flashing, as the start nears. Press to open the meeting link, hold to dismiss the alert. |
| **Agenda** | Key | Step through today's or all upcoming events on one key (press = next, hold = previous). Shows *2/5*-style position. |
| **Upcoming (Dial)** | Dial | Turn to browse upcoming events, press to join, hold to jump back to the next one, tap the screen to dismiss an alert. A bar fills during the warning window and then tracks the running event. |

Every action's gestures are rebindable through StreamController's **Event Assigner** in the
action's configuration. Functions with no default gesture (e.g. *Skip Event*, *Refresh
Calendars*) can be bound there too.

### Labels

Each action has three label slots (top/middle/bottom) that can show any of:

`title` · `countdown` (e.g. `12m`, `1h05`, `25m left`) · `time` (start, e.g. `14:30` or
`Tomorrow 09:00`) · `day` · `calendar` · `location` · `position` (`2/5`) · `none`.

*Max Text Length* controls where long titles are cut with an ellipsis.

### Alerts

| Setting | Default |
| --- | --- |
| Warning At (minutes before) | 15 |
| Urgent At (minutes before) | 5 |
| Flash While Urgent | on |
| Show Running Event | on (off: jump to the next event as soon as one starts) |
| Include All-Day Events | off |
| Show Calendar Color Bar / Show Icon | on |

**Dismiss Alert** silences the color/flash for that one event only. **Skip Event** hides an
event from every *Next Event* key so the following one shows instead (Agenda and the dial still
list it). Both reset when the event ends.

### Meeting links

The icon switches to a camera when an event has a join link. Links are taken from, in order,
the event's conferencing property (what Google Meet sets), the location, the description, and
the URL field, recognising Google Meet, Zoom, Teams, Webex, Whereby, GoToMeeting, BlueJeans,
Jitsi and Discord, with any other `https://` link as a fallback.

### Colors and icons

All background colors (normal / warning / urgent / running / no event), the icon tint, the
progress bar color and every icon are user-overridable in the plugin's *Settings → Assets /
Colors* tabs.

## Development

See [`.devcontainer/README.md`](.devcontainer/README.md) for a one-click VS Code environment
with StreamController pre-installed and this plugin mounted in place, and [`CLAUDE.md`](CLAUDE.md)
for a map of the code. Unit tests for the parsing and scheduling logic run without
StreamController:

```sh
python3 -m venv .venv-test && . .venv-test/bin/activate
pip install icalendar recurring-ical-events requests
python3 -m unittest discover -s tests -t .
```

## License

MIT (see `LICENSE`). Bundled icons are Google's Material Icons, Apache License 2.0 - see
`assets/icons/material/NOTICE.md`.
