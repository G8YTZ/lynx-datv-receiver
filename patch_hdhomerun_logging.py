#!/usr/bin/env python3
"""
patch_hdhomerun_logging.py — log DVB-T2 contacts to QRZ and Slack.

The notifications manager polls the Picotuner and knows nothing else,
so a DVB-T2 contact was never logged: the OSD showed it, Pathfinder
drew a card for it, and the logbook stayed empty.

Approach
--------
A public method on the manager rather than lynx_app.py calling its
private ones. The settle-timer machinery, the QRZ de-duplication and
the Slack path all already exist; this reuses them with a source dict
supplied from outside, which is exactly what source_override was added
for.

The source carries two new optional keys:

  mode_name   the ADIF MODE string. The existing code hardcodes
              "DVB-S2 <modcod>" with a comment flagging the assumption -
              every modcod the Picotuner reports includes a modulation
              type, so DVB-S2 was safe. It is not safe now, and this is
              the branch that comment anticipated.

  comment     the free-text comment. For DVB-S2 that is auto-built as
              "<mode> | <mer>dB MER". DVB-T2 has no MER, so it carries
              what it does measure: "SNQ 100% | SEQ 100% | LVL 82%".
              Honest in a free-form field, where putting the same
              numbers under a dB-labelled heading would not be.

RST_SENT is "<margin>dB" for DVB-S2. With no margin that becomes a bare
"dB", so it falls back to the SNQ percentage - the nearest thing this
tuner has to a signal report, and a field QRZ expects something in.

Trigger
-------
The poller arms a settle timer once it has a callsign, and cancels it
on unlock - the same shape as tri_watch's per-receiver tracking. A
station keying up again re-arms the service-name fetch, so each
transmission is logged afresh rather than only the first after a tune.

Touches lynx_app.py and lynx_notifications.py.

Usage
-----
    python3 patch_hdhomerun_logging.py            # dry run
    python3 patch_hdhomerun_logging.py --apply    # back up and write

Then verify.py and restart.
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

APP = Path("lynx_app.py")
NOTIF = Path("lynx_notifications.py")
MARKER = "arm_external_contact"


# ==========================================================================
# lynx_notifications.py
# ==========================================================================

# --- 1. RST falls back to SNQ when there is no margin ---------------------

N_A_OLD = '''    comment = comment_override if comment_override is not None else f"{mode} | {mer}dB MER"
    rst_sent = f"{margin}dB"'''

N_A_NEW = '''    comment = comment_override if comment_override is not None else f"{mode} | {mer}dB MER"
    # "<margin>dB" is right for DVB-S2. With no margin - DVB-T2 does not
    # report one - that becomes a bare "dB", which is worse than
    # useless in a signal-report field. rst_override lets the caller
    # supply what its own receiver actually measures instead.
    if rst_override is not None:
        rst_sent = rst_override
    elif margin in (None, ""):
        rst_sent = ""
    else:
        rst_sent = f"{margin}dB"'''


N_B_OLD = '''    comment_override: replaces the normal, auto-built comment entirely'''

N_B_NEW = '''    rst_override: replaces the "<margin>dB" report entirely. DVB-T2 has
    no margin figure, so its caller supplies the signal quality figure
    its own tuner reports instead.

    comment_override: replaces the normal, auto-built comment entirely'''


# --- 2. the mode string stops assuming DVB-S2 -----------------------------

N_C_OLD = '''            adif_mode = f"DVB-S2 {src['modcod']}" if src["modcod"] else "DVB-S2"
            result = submit_qrz_logbook(api_key, site_callsign, call, src["frequency_khz"],
                                         adif_mode, src["mer"], src["margin"],
                                         portable_locator=portable_locator)'''

N_C_NEW = '''            # The assumption flagged above - that every contact is
            # DVB-S2 - held while the Picotuner was the only source.
            # A source that knows what it is says so, and this is the
            # branch that comment anticipated.
            adif_mode = src.get("mode_name") or (
                f"DVB-S2 {src['modcod']}" if src["modcod"] else "DVB-S2")
            result = submit_qrz_logbook(api_key, site_callsign, call, src["frequency_khz"],
                                         adif_mode, src["mer"], src["margin"],
                                         portable_locator=portable_locator,
                                         comment_override=src.get("comment"),
                                         rst_override=src.get("rst"))'''


# --- 3. the public entry point --------------------------------------------

N_D_OLD = '''    def _fire_qrz(self, qrz_cfg, site_callsign, source_override=None):'''

N_D_NEW = '''    def arm_external_contact(self, src, key="external"):
        """Log a contact from a source this manager does not poll.

        Built for the DVB-T2 tuner, which is a separate device on the
        network rather than a tuner this module watches. Everything
        after the trigger is identical to a Picotuner contact, so the
        settle timers, the QRZ de-duplication and the Slack path are
        reused rather than reimplemented - the difference is only in
        who noticed the station, not in what happens next.

        src is a picotuner_state-shaped dict, optionally carrying
        mode_name, comment and rst - see submit_qrz_logbook().

        The settle delay still applies: a station that keys up and
        immediately drops again should not reach the logbook, whatever
        noticed it.
        """
        cfg = self.get_config()
        notif_cfg = cfg.get('notifications', {})
        site_callsign = notif_cfg.get('station_callsign', '')
        qrz_cfg = notif_cfg.get('qrz', {})
        slack_cfg = notif_cfg.get('slack', {})

        if qrz_cfg.get('enabled', False):
            delay = float(qrz_cfg.get('settle_secs', 15.0))
            self._arm_action(
                f'{key}_qrz', delay,
                lambda: self._fire_qrz(qrz_cfg, site_callsign,
                                       source_override=src),
                f"QRZ ({key})")

        if slack_cfg.get('enabled', False):
            delay = float(slack_cfg.get('settle_secs', 15.0))
            self._arm_action(
                f'{key}_slack', delay,
                lambda: self._fire_slack(slack_cfg, site_callsign,
                                         source_override=src),
                f"Slack ({key})")

    def cancel_external_contact(self, key="external"):
        """Cancel pending timers for an external source that has gone.

        A station that stops before the settle time expires never
        happened, as far as the logbook is concerned - the same rule
        the polled sources follow.
        """
        self._cancel_action(f'{key}_qrz')
        self._cancel_action(f'{key}_slack')

    def _fire_qrz(self, qrz_cfg, site_callsign, source_override=None):'''


# --- 4. submit_qrz_logbook's signature ------------------------------------

N_E_OLD = '''                        mode, mer, margin, portable_locator="", comment_override=None):'''

N_E_NEW = '''                        mode, mer, margin, portable_locator="", comment_override=None,
                        rst_override=None):'''


# ==========================================================================
# lynx_app.py
# ==========================================================================

# --- 5. remember the program, so a re-lock can refetch the name -----------

A_A_OLD = '''            live["pending_program"] = (str(req.program)
                                       if req.program is not None else None)
            live["pending_freq_hz"] = req.freq'''

A_A_NEW = '''            live["pending_program"] = (str(req.program)
                                       if req.program is not None else None)
            live["pending_freq_hz"] = req.freq
            # Kept so a re-lock can ask again. Without it, a station
            # keying up after a gap kept the previous contact's name
            # and was never logged a second time.
            live["last_program"] = live["pending_program"]
            live["logged_callsign"] = ""'''


# --- 6. re-arm the fetch on re-lock, and log the contact ------------------

A_B_OLD = '''                    if tune_lock.acquire(blocking=False):
                        try:
                            print(f"[hdhr] {device_id} re-locked - "
                                  "restarting mpv")
                            restart_mpv(f"udp://@:{HDHR_VIDEO_PORT}",
                                        is_rf=False)
                            end_transition_cover()'''

A_B_NEW = '''                    # Ask for the service name again. A different
                    # station may be on now, and keeping the previous
                    # one's callsign would log the wrong contact.
                    with hdhr_lock:
                        _live = hdhr_states.get(device_id)
                        if _live is not None:
                            _live["pending_program"] = _live.get("last_program")
                            _live["logged_callsign"] = ""
                    if tune_lock.acquire(blocking=False):
                        try:
                            print(f"[hdhr] {device_id} re-locked - "
                                  "restarting mpv")
                            restart_mpv(f"udp://@:{HDHR_VIDEO_PORT}",
                                        is_rf=False)
                            end_transition_cover()'''


# --- 7. arm the logging once a callsign is known -------------------------

A_C_OLD = '''                            live["pending_program"] = None'''

A_C_NEW = '''                            live["pending_program"] = None

                    # A callsign, on an amateur frequency, not already
                    # logged for this transmission: that is a contact.
                    # Armed here rather than at lock, because at lock
                    # there is no callsign yet - it arrives with the
                    # service name a moment later.
                    _new_call = call.upper() if hdhr_is_amateur_band(
                        pending_freq) else ""
                    if (_new_call and current_mode == "dvbt"
                            and notification_manager is not None):
                        with hdhr_lock:
                            _l = hdhr_states.get(device_id)
                            _already = _l.get("logged_callsign") if _l else ""
                            if _l is not None:
                                _l["logged_callsign"] = _new_call
                        if _already != _new_call:
                            try:
                                notification_manager.arm_external_contact(
                                    _hdhr_contact_source(device_id),
                                    key="dvbt")
                            except Exception as e:
                                print(f"[hdhr] logging: "
                                      f"{type(e).__name__}: {e}")'''


# --- 8. the contact source builder ---------------------------------------

A_D_OLD = '''def hdhr_stop_all():'''

A_D_NEW = '''def _hdhr_contact_source(device_id):
    """A picotuner_state-shaped source dict for a DVB-T2 contact.

    The notifications module reads a fixed set of field names, so the
    honest ones are filled and the rest left empty rather than
    approximated. mer and margin stay None: SNQ is the closest thing
    this tuner reports and it is still not MER.

    mode_name, comment and rst carry what DVB-T2 actually measures.
    The comment field is free text, which is where a percentage can go
    without being mistaken for a dB figure.
    """
    st = None
    for d in hdhr_devices():
        if d["device_id"] == device_id:
            st = d
            break
    if not st:
        return {}

    lm = st.get("lock_mode") or ""
    std = "DVB-T2" if lm.endswith("dvbt2") else ("DVB-T" if "dvbt" in lm else "")
    bw = lm[1:2]
    mode_name = f"{std} {bw} MHz" if (std and bw) else (std or "DVB-T2")
    freq_hz = st.get("frequency_hz") or 0

    snq = st.get("signal_quality")
    seq = st.get("symbol_quality")
    lvl = st.get("signal_strength")

    return {
        "rx_callsign": st.get("callsign", ""),
        # kHz, matching the module's own convention - it divides by
        # 1000 again for the ADIF frequency, and a value in the wrong
        # unit here once put a 437 MHz contact outside every defined
        # band and had the whole submission rejected.
        "frequency_khz": freq_hz / 1000.0,
        "mer": None,
        "margin": None,
        "modcod": mode_name,
        "symbol_rate": "",
        "mode_name": mode_name,
        "comment": f"{mode_name} | SNQ {snq}% | SEQ {seq}% | LVL {lvl}%",
        "rst": f"SNQ {snq}%",
    }


def hdhr_stop_all():'''


NOTIF_EDITS = [
    ("rst fallback", N_A_OLD, N_A_NEW),
    ("rst docstring", N_B_OLD, N_B_NEW),
    ("mode branch", N_C_OLD, N_C_NEW),
    ("public entry point", N_D_OLD, N_D_NEW),
    ("submit signature", N_E_OLD, N_E_NEW),
]

APP_EDITS = [
    ("remember program", A_A_OLD, A_A_NEW),
    ("re-arm on re-lock", A_B_OLD, A_B_NEW),
    ("arm logging", A_C_OLD, A_C_NEW),
    ("contact source", A_D_OLD, A_D_NEW),
]


def check_and_patch(path: Path, edits: list):
    text = path.read_text(encoding="utf-8")
    print(f"\nchecking anchors in {path}\n")
    ok = True
    for name, anchor, _new in edits:
        count = text.count(anchor)
        if count == 1:
            print(f"  {name:<22} OK   one match")
        else:
            print(f"  {name:<22} FAIL {count} matches - expected exactly 1")
            ok = False
    if not ok:
        return None
    patched = text
    for _name, anchor, new in edits:
        patched = patched.replace(anchor, new, 1)
    return patched


def compiles(text: str, label: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        py_compile.compile(str(tmp_path), doraise=True)
        print(f"  {label:<22} OK   result parses")
        return True
    except py_compile.PyCompileError as e:
        print(f"  {label:<22} FAIL {e}")
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    args = parser.parse_args()

    for path in (APP, NOTIF):
        if not path.exists():
            print(f"ERROR: {path} not found. Run from the lynx directory.")
            return 2

    if MARKER in NOTIF.read_text(encoding="utf-8"):
        print(f"{NOTIF} already patched - nothing to do.")
        return 0
    if "HDHR_VIDEO_PORT" not in APP.read_text(encoding="utf-8"):
        print("ERROR: the earlier HDHomeRun patches have not been applied.")
        return 2

    notif_patched = check_and_patch(NOTIF, NOTIF_EDITS)
    app_patched = check_and_patch(APP, APP_EDITS)
    if notif_patched is None or app_patched is None:
        print("\nNo changes made to either file.")
        return 1

    print("\nsyntax\n")
    if not (compiles(notif_patched, "lynx_notifications.py")
            and compiles(app_patched, "lynx_app.py")):
        print("\nNo changes made to either file.")
        return 1

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path, patched in ((NOTIF, notif_patched), (APP, app_patched)):
        backup = path.with_suffix(path.suffix + f".bak-logging-{stamp}")
        shutil.copy2(path, backup)
        path.write_text(patched, encoding="utf-8")
        print(f"\n  {path} patched, backup {backup.name}")

    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
