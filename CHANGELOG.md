# Changelog

All notable changes to Lynx are documented here, in reverse chronological order.

This file starts from 2026-07-29. Earlier changes are recorded in each document's own version history section instead (e.g. the Installation Guide, Web UI Reference) - this centralized approach begins here rather than attempting to retroactively reconstruct everything that came before it.

## 2026-07-29

### Added
- Portable locator override for QRZ Logbook - when a contacted station is operating portable and hasn't updated their own QRZ profile, this lets you override the grid square used for that contact's distance/bearing calculation. Configurable from the Web UI's QRZ card, and shown on the OSD whenever active so it can't be silently forgotten. Deliberately built as a plain, generic config value - a future automated source (e.g. a GPS module, or a phone's GPS relayed over Bluetooth) could populate it without any changes needed downstream.
- "Test QRZ Logging" feature on the Diagnostics page - sends one real, clearly-marked test entry (safe to delete afterwards) and shows QRZ's complete, raw response. Useful for confirming a QRZ setup genuinely works without waiting for a real RF lock.
- New Diagnostics page event categories covering the notifications system's own lock/unlock confirmations, cancelled pending actions, and QRZ submission results (skipped/logged/failed, with full detail on failures) - previously only visible in terminal output, now on the same persistent, browser-visible timeline as everything else.
- 3cm amateur band added to QRZ band derivation, needed for QO-100 contacts.
- Mouse cursor is now hidden automatically on boot (labwc's HideCursor/WarpCursor keybind, triggered once the OSD overlay has confirmed starting) - not Pi500-specific, affects any Lynx install on labwc's default desktop behaviour. install.sh now creates the required `~/.config/labwc/rc.xml` automatically on a fresh install; existing installs need it added once by hand (see the Installation Guide).

### Fixed
- **QRZ Logbook submissions from genuine RF locks were silently failing.** Root cause: the live frequency reading was being labelled as kHz without actually being converted from the MHz value the Picotuner reports - a real 437MHz signal was miscalculated as 0.437MHz, which doesn't fall into any defined amateur band, leaving QRZ's `band` field empty and the whole submission rejected. Manual/test submissions never hit this path, which is why it went unnoticed for a while - they always used a fixed, already-correct test frequency.
- QRZ Logbook entries now correctly log the real satellite downlink frequency when an LNB is configured, rather than the raw IF frequency the Picotuner is actually tuned to (e.g. a genuine 10489.5MHz QO-100 contact was previously being logged as 739.5MHz).
- QRZ Logbook's `mode` field now includes the DVB-S2 standard name alongside the modcod (e.g. `DVB-S2 QPSK 8/9`), matching the correct, expected ADIF format - previously sent as just the bare modcod.
- Slack notifications' `{frequency}` placeholder was showing the wrong value (1000x too low) - same root cause as the QRZ frequency bug above, fixed by the same change.

### Changed
- Removed margin from the QRZ Logbook comment field to keep it shorter (MER is retained).
