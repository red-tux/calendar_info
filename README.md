# Calendar Info

A [StreamController](https://github.com/StreamController/StreamController) plugin that puts your
calendar on a Stream Deck: the next event with a live countdown that changes color as it
approaches, a browsable agenda, and a press to join the meeting.

Works with any iCalendar (`.ics`) feed - a URL, a `webcal://` address, or a local file - so it
covers Google Calendar, Outlook/Microsoft 365, Nextcloud, iCloud, Fastmail and friends without
any OAuth setup. Google Calendar can additionally be connected through the Google Calendar API,
which trades a longer one-time setup for events that appear as soon as they are created.

## Setup

1. **Get your calendar's iCalendar address.** Google Calendar: *Settings → (your calendar) →
   Integrate calendar → Secret address in iCal format*. Treat it like a password; anyone with
   the address can read the calendar.
2. In **StreamController**: *Plugins → Calendar Info → settings (gear)*.
3. Click **+** under *Calendars*, give it a name, paste the address into *Address* and press
   Enter, pick a color. **Test** fetches it once and tells you how many events it found.
4. Add more calendars the same way. Each gets its own color bar on the keys.

Options on the same screen: refresh interval (default 5 minutes), how many days to look ahead
(default 7), 12/24-hour time, **Display Timezone**, and hiding all-day events everywhere.

### Display Timezone

Times on the keys follow this setting:

- **System default** - the machine's timezone (what the app has always done). The label names
  the zone in use, which is worth a glance: a container or a service started without `TZ` often
  runs in UTC, and that is the usual reason a meeting shows up hours off.
- **Event's own timezone** - each event in the zone it was created in (the `TZID` of an `.ics`
  event, or Google's `timeZone`). Handy when you keep meetings in a colleague's zone.
- **UTC**
- **Any IANA zone** (`America/New_York`, `Europe/Berlin`, …) - the list is searchable and
  matches anywhere in the name, so typing `new` or `berlin` finds the zone without knowing
  which region it is filed under.

Countdowns are durations and never change with this setting. All-day events keep their own
date in every zone; only "Today"/"Tomorrow" is judged in the zone you picked.

The last successful download of each calendar is kept on disk, so a network outage keeps
showing your schedule instead of blanking the deck.

## Google Calendar over the API (optional)

The `.ics` route above needs no accounts and is the recommended default. Its one real drawback
is freshness: Google serves the secret address from a cache that can lag by hours, so a meeting
you just accepted may not reach your deck for a while. Connecting the Google Calendar API
instead gives you events within one refresh interval, the calendar's own color, and Google
Meet links straight from the event rather than guessed out of its description.

Like [Home Assistant](https://www.home-assistant.io/integrations/google/), this plugin ships no
Google credentials of its own: you create an OAuth client in your own Google Cloud project and
paste it in. Nothing is shared with other users, there is no app verification to wait for, and
you can revoke it at any time. Google has no API for any of these steps, so the plugin's
**Setup guide** button opens the right console page for each one in turn:

1. **Create a Google Cloud project** - [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate)
2. **Enable the Google Calendar API** - [API library](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com), with your project selected
3. **Configure the consent screen** - [Branding](https://console.cloud.google.com/auth/branding): app name, support email, audience *External*
4. **Publish the app** - [Audience](https://console.cloud.google.com/auth/audience) → *Publish app*. If you leave it in *Testing*, Google expires the login after 7 days and your calendars go stale with an `invalid_grant` error. Google will call the app unverified: that only matters for apps handed to other people, so choose *Advanced* → the *Go to …* link to continue.
5. **Create the OAuth client** - [Clients](https://console.cloud.google.com/auth/clients) → *Create client* → application type **Desktop app**. There is no redirect URI to fill in: the plugin listens on `127.0.0.1` and Google replies there directly.

Then in *Plugins → Calendar Info → settings*, under **Google Calendar**: paste the **Client ID**
and **Client secret**, press **Connect**, and approve in the browser tab that opens. Once the
account is linked, **Add calendars** lists the calendars on it and each one you tick becomes a
normal calendar entry - same color swatch, same on/off switch, same actions.

The only scope requested is `calendar.readonly`, so the plugin cannot change your calendar.
The refresh token is stored in `credentials/` inside the plugin folder, mode 0600, and never in
the settings file. **Disconnect** revokes it with Google and deletes both the token and every
calendar that was reading through it.

### When something goes wrong

| What you see | What it means |
| --- | --- |
| `invalid_grant` after about a week | The OAuth app is still in *Testing*. Publish it (step 4), then Connect again. |
| `invalid_client` | The client ID or secret is truncated or from a different project. |
| `redirect_uri_mismatch` | The client is not of type *Desktop app*. Create a new one. |
| "The Google Calendar API is not enabled…" | Step 2 was skipped or ran against another project. The message carries the exact link to enable it. |
| "Reconnect needed" on a calendar | Access was revoked (password change, or removed at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)). Connect the account again. |

## The actions

| Action | Input | What it does |
| --- | --- | --- |
| **Next Event** | Key | Your next (or currently running) event: countdown + title. Background turns amber, then red and flashing, as the start nears. Press to open the meeting link, hold to dismiss the alert. |
| **Agenda** | Key | Step through today's or all upcoming events on one key (press = next, hold = previous). Shows *2/5*-style position. |
| **Upcoming (Dial)** | Dial | Turn to browse upcoming events, press to join, hold to jump back to the next one, tap the screen to dismiss an alert. A bar fills during the warning window and then tracks the running event. |

Every action's gestures are rebindable through StreamController's **Event Assigner** in the
action's configuration. Functions with no default gesture (e.g. *Skip Event*, *Refresh
Calendars*) can be bound there too.

### Which calendars a key shows

Every action has a **Calendars** section in its configuration. It starts on *All calendars*,
which follows whatever is configured in the plugin settings. Turn that off and tick individual
calendars to narrow one key down - so a *Next Event* key can watch only your work calendar
while an *Agenda* key next to it browses everything.

The choice is per key (it lives in the page, not in the plugin settings), so the same calendar
can drive several keys with different scopes. Ticking nothing is treated as "all", and a
calendar removed from the plugin settings is dropped from the selection rather than blanking
the key.

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
