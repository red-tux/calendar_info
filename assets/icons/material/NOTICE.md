These icons (`event`, `event_available`, `event_busy`, `calendar_today`, `today`, `schedule`,
`videocam`, `notifications_active`, `chevron_left`, `chevron_right` - both the `.svg` sources
and the `.png` renders of them) are from Google's Material Icons set:

https://github.com/google/material-design-icons

Copyright Google LLC, licensed under the Apache License, Version 2.0 (see `LICENSE` in this
directory, or https://www.apache.org/licenses/LICENSE-2.0).

The `.svg` files are unmodified. The `.png` files are plain rasterizations of the same SVGs at
512x512 (via `cairosvg`), generated once and committed rather than rendered at runtime, and are
what's actually registered as this plugin's default icon assets. StreamController's own
SVG-to-image loading path squashes SVG icon assets to a non-square aspect ratio, so registering
pre-rendered square PNGs sidesteps that. The icon's shape and tint are applied at render time
(`actions/common/calendar_action_base.py`, `paste_asset_icon`) and are independently
user-overridable through this plugin's Settings dialog (Assets / Colors tabs).
