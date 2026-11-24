#!/usr/bin/env python3
"""How this Remote Rx is attached to the network, for the heartbeat.

A Remote Rx is in a loft, or on a hill, or in somebody else's shack.
Nobody can ssh to it to run `iw dev wlan0 link`, which is exactly the
command you want the moment its pictures start breaking up - and the
answer is the one that explains most faults.

Worth the file, because this was learned the hard way (24 Nov 2026).
A card at 25.1 dB MER with 18.7 dB of margin - an immaculate signal off
the air - produced grey frames and freezes on the repeater, because its
Wi-Fi was on a distant AP and SRT was dropping packets it could not
retransmit in time. The RF was perfect and the pictures were broken,
and the panel had no way to say why. Hours went on the decoder, the
branch, the SD card and the encoder before anybody thought to look at
the radio.

Read from /proc/net/wireless and sysfs, not by running `iw`:

  - iw is not installed on a Lite image, and shelling out to a tool
    that may not be there, from a machine nobody can reach, to answer a
    question asked every two seconds, is three bad ideas at once.
  - /proc/net/wireless has been stable for twenty years. iw's output
    format is for humans and has changed.

Same reasoning as detect_tuner.py reading sysfs rather than lsusb.

What is NOT here, deliberately: bitrate and the name of the AP. Both
need netlink, which needs a library or a subprocess, and neither
changes what anybody would do about a weak signal.
"""

import glob
import os

PROC_WIRELESS = "/proc/net/wireless"
SYS_NET = "/sys/class/net"

# dBm boundaries for the plain-words verdict. 2.4 GHz, which is all a
# Zero 2 W has. Deliberately pessimistic at the margins: a Remote Rx
# sends a continuous transport stream, so it is a far harsher load than
# the web browsing these numbers usually describe.
GOOD_DBM = -60.0        # at or above this: no concerns
FINE_DBM = -70.0        # above this: fine in practice
MARGINAL_DBM = -80.0    # above this: expect bursts of loss under load


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def quality_words(dbm):
    """A verdict a person can act on, from a number they cannot."""
    if dbm is None:
        return ""
    if dbm >= GOOD_DBM:
        return "strong"
    if dbm >= FINE_DBM:
        return "fine"
    if dbm >= MARGINAL_DBM:
        return "marginal"
    return "poor"


def wireless(root=""):
    """(interface, dbm, link_quality) for the first wireless interface
    with a signal, or (None, None, None).

    /proc/net/wireless, two header lines then one per interface:

      wlan0: 0000   52.  -66.  -256   0  0  0  0  0  0
             status  link level noise ...

    The trailing dots are part of the format, not decimals. Level is
    dBm when it is negative, which it is on every driver worth having;
    a few ancient ones report an unsigned 0-255 scale instead, and
    those are reported as unknown rather than guessed at.
    """
    text = _read(os.path.join(root + PROC_WIRELESS))
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) < 3:
            continue
        try:
            link = float(parts[1].rstrip("."))
            level = float(parts[2].rstrip("."))
        except ValueError:
            continue
        if level >= 0:
            # Not dBm - an old driver's 0-255 scale. Say nothing rather
            # than report a positive "signal strength" nobody can read.
            return name.strip(), None, int(link)
        return name.strip(), int(level), int(link)
    return None, None, None


def wired(root=""):
    """The name of an up, non-loopback, non-wireless interface, or None.

    Only consulted when there is no wireless one, so a card with both
    reports the radio - which is the one that can go wrong quietly.
    """
    for path in sorted(glob.glob(os.path.join(root + SYS_NET, "*"))):
        name = os.path.basename(path)
        if name == "lo" or os.path.isdir(os.path.join(path, "wireless")):
            continue
        if _read(os.path.join(path, "operstate")) == "up":
            return name
    return None


def describe(root=""):
    """(kind, dbm, link) - kind is "wifi", "wired" or "-".

    dbm and link are None when they do not apply or cannot be read.
    """
    name, dbm, link = wireless(root)
    if name:
        return "wifi", dbm, link
    return ("wired" if wired(root) else "-"), None, None


def heartbeat_line(root=""):
    """"NET <kind> <dbm> <link>", three fixed fields, "-" for unknown.

    Fixed-width by design, as VER and TUN are: a parser on the far end
    should never have to work out how many fields it was sent.
    """
    kind, dbm, link = describe(root)
    return "NET {} {} {}".format(kind,
                                 "-" if dbm is None else dbm,
                                 "-" if link is None else link)


if __name__ == "__main__":
    k, d, l = describe()
    print(heartbeat_line())
    if d is not None:
        print(f"  {d} dBm ({quality_words(d)}), link quality {l}")
