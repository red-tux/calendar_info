# Calendar Info

A [StreamController](https://github.com/StreamController/StreamController) plugin that puts your
calendar on a Stream Deck: the next event with a live countdown that changes color as it
approaches, a browsable agenda, and a press to join the meeting.

Works with any iCalendar (`.ics`) feed - a URL, a `webcal://` address, or a local file - so it
covers Google Calendar, Outlook/Microsoft 365, Nextcloud, iCloud, Fastmail and friends without
any OAuth setup. Google Calendar can additionally be connected through the Google Calendar API,
which trades a longer one-time setup for events that appear as soon as they are created.

## Setup

In **StreamController**, open *Plugins → Calendar Info → settings (gear)* and press **+** under
*Calendar sources*. What you get offered depends on what is available:

- **From this desktop** - any account already set up in your desktop's online accounts (KDE
  Plasma today). Pick one and Calendar Info works out what it is and lists its calendars. No
  addresses to copy, no OAuth client to register, and the desktop keeps the login.
- **iCalendar address or file** - the universal option. Paste a `.ics` URL, a `webcal://`
  address or a path to a local file. Google Calendar's is under *Settings → (your calendar) →
  Integrate calendar → Secret address in iCal format*; treat it like a password, since anyone
  with the address can read the calendar. **Test** fetches it once and reports what it found.
- **Google Calendar (Google OAuth client)** - the API route, for fresher events than the secret
  address can give. It needs a one-time Cloud console setup; see below.

An account appears in the list as an expander holding the calendars read through it; an
iCalendar feed appears on its own. Each calendar has its own name, color bar and on/off switch.
**Add calendars** on an account row picks up calendars added later.

Options on the same screen: refresh interval (default 5 minutes), how many days to look ahead
(default 7), 12/24-hour time, **Display Timezone**, and hiding all-day events everywhere.

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

Then press **+** under *Calendar sources*, choose **Google Calendar (Google OAuth client)**,
paste the **Client ID** and **Client secret**, press **Connect**, and approve in the browser tab
that opens. The calendar picker opens straight afterwards: each one you tick becomes a normal
calendar entry under the account - same color swatch, same on/off switch, same actions.

The only scope requested is `calendar.readonly`, so the plugin cannot change your calendar.
The refresh token is stored in `credentials/` inside the plugin folder, mode 0600, and never in
the settings file. **Disconnect** revokes it with Google and deletes both the token and every
calendar that was reading through it.

### Or: use the Google account from your desktop (KDE)

If you run KDE Plasma and your Google account is already in *System Settings → Online
Accounts*, skip the whole OAuth client setup: press **+** under *Calendar sources* and pick it
under *From this desktop*. The desktop's own login is used and the plugin stores no token at all
(it asks the desktop for a short-lived one when it needs it). Disconnecting only unlinks it from
the plugin; the desktop account itself is untouched.

Accounts of a kind Calendar Info cannot read yet - a Nextcloud account, say - are listed there
too, greyed out and labelled with what is missing, so you can see what a future version will
pick up.

On the Flatpak build of StreamController, the sandbox hides the desktop's login service, so the
first time you link an account the plugin asks for that permission through StreamController's
own dialog. If you would rather grant it up front, or the dialog does not appear:

```sh
flatpak override --user --talk-name=com.google.code.AccountsSSO.SingleSignOn com.core447.StreamController
flatpak kill com.core447.StreamController      # overrides only apply at sandbox setup
```

Nothing else is needed: the account database lives under your home directory, which
StreamController's manifest already grants access to, and nothing in its packaging changes.

### When something goes wrong

| What you see | What it means |
| --- | --- |
| `invalid_grant` after about a week | The OAuth app is still in *Testing*. Publish it (step 4), then Connect again. |
| `invalid_client` | The client ID or secret is truncated or from a different project. |
| `redirect_uri_mismatch` | The client is not of type *Desktop app*. Create a new one. |
| "The Google Calendar API is not enabled…" | Step 2 was skipped or ran against another project. The message carries the exact link to enable it. |
| "Reconnect needed" on a calendar | Access was revoked (password change, or removed at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)). Connect the account again. |
| "KDE Online Accounts could not provide a login…" | The desktop's stored login has expired or been revoked. Open *System Settings → Online Accounts*, re-authenticate the account, then refresh. In a Flatpak, also check the two permissions described above. |
| "No desktop accounts found" | No Google account in *System Settings → Online Accounts* yet. |

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
