#!/usr/bin/env python3
"""Tests for slave/ts_info.py - reading a transport stream's own
description of itself.

Run:  python3 test_ts_info.py

Worth having rather than testing on hardware, because the hardware
cannot exercise most of it. A Remote Rx carries one service with a
short name in a section that fits in one packet, so a bench test
proves only the easy path and says nothing about a section crossing a
packet boundary, a PMT arriving before the PAT that points at it, or
what happens when the signal goes away. Each of those is a real case
and each is below.

The streams here are built to the MPEG-2 and DVB specifications rather
than captured, so a test failing means the parser disagrees with the
standard, which is the thing worth knowing.
"""

import sys
import time
import unittest

sys.path.insert(0, "slave")
import ts_info          # noqa: E402


def section(table_id, body):
    """A PSI section: header, body, and four bytes where a CRC goes.

    The parser does not check the CRC - see its own docstring for why -
    so the bytes themselves do not matter, only that four of them are
    there, because section_length counts them.
    """
    length = len(body) + 4
    return (bytes([table_id, 0xB0 | ((length >> 8) & 0x0F), length & 0xFF])
            + body + b"\x00" * 4)


def packet(pid, payload, start=True, cc=0):
    """One 188-byte transport packet, payload only, no adaptation field."""
    hdr = bytes([0x47,
                 (0x40 if start else 0) | ((pid >> 8) & 0x1F),
                 pid & 0xFF,
                 0x10 | (cc & 0x0F)])
    # payload_unit_start_indicator means the first payload byte is a
    # pointer to where the section begins. Zero: it begins at once.
    body = (b"\x00" if start else b"") + payload
    return (hdr + body).ljust(188, b"\xFF")


def packet_with_af(pid, payload, start=True, cc=0, stuffing=10):
    """The same, but with an adaptation field in front of the payload.

    Real streams use these constantly for PCR and for padding, and the
    payload offset then depends on the adaptation field's length. Get
    that wrong and every table is read from the wrong byte.
    """
    body = (b"\x00" if start else b"") + payload
    af = bytes([stuffing - 1, 0x00]) + b"\xFF" * (stuffing - 2)
    hdr = bytes([0x47,
                 (0x40 if start else 0) | ((pid >> 8) & 0x1F),
                 pid & 0xFF,
                 0x30 | (cc & 0x0F)])     # 0x30: adaptation field AND payload
    return (hdr + af + body).ljust(188, b"\xFF")


def pat(programme=1, pmt_pid=0x1000):
    body = (bytes([0x00, 0x01, 0xC1, 0x00, 0x00])
            + bytes([(programme >> 8) & 0xFF, programme & 0xFF,
                     0xE0 | ((pmt_pid >> 8) & 0x1F), pmt_pid & 0xFF]))
    return section(0x00, body)


def pmt(streams=((36, 0x1001), (15, 0x1002))):
    body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00]) + bytes([0xF0, 0x01]) \
           + bytes([0xF0, 0x00])
    for es_type, es_pid in streams:
        body += bytes([es_type, 0xE0 | ((es_pid >> 8) & 0x1F), es_pid & 0xFF,
                       0xF0, 0x00])
    return section(0x02, body)


def sdt(name=b"G8YTZ", provider=b"Lenham"):
    d = (bytes([0x48, 2 + len(provider) + 1 + len(name), 0x01, len(provider)])
         + provider + bytes([len(name)]) + name)
    body = (bytes([0x00, 0x01, 0xC1, 0x00, 0x00])
            + bytes([0x00, 0x01, 0xFF])
            + bytes([0x00, 0x01, 0xFC,
                     0x80 | ((len(d) >> 8) & 0x0F), len(d) & 0xFF]) + d)
    return section(0x42, body)


def full_stream():
    return (packet(0x0000, pat()) + packet(0x0011, sdt())
            + packet(0x1000, pmt()))


class TestTsInfo(unittest.TestCase):

    def test_reads_a_whole_service(self):
        info = ts_info.TsInfo()
        info.feed(full_stream())
        self.assertEqual(info.snapshot(), {
            "service": "G8YTZ", "provider": "Lenham",
            "video_codec": "H265", "audio_codec": "AAC"})

    def test_pmt_before_the_pat_that_points_at_it(self):
        """The PMT PID is only known from the PAT, so a PMT arriving
        first must be ignored and picked up on its next repeat. Real
        streams repeat every few hundred milliseconds, so this is the
        ordinary case at start-up rather than an edge one."""
        info = ts_info.TsInfo()
        info.feed(packet(0x1000, pmt()))
        self.assertEqual(info.snapshot()["video_codec"], "")
        info.feed(packet(0x0000, pat()) + packet(0x1000, pmt()))
        self.assertEqual(info.snapshot()["video_codec"], "H265")

    def test_section_crossing_a_packet_boundary(self):
        """A section longer than one packet's payload. Forced here with
        a long provider name; in the field a PMT with a few descriptors
        does it. Without reassembly the callsign would appear only on
        whichever repeat happened to fit, which is the kind of fault
        that looks like a flaky transmitter."""
        s = sdt(name=b"G8YTZ", provider=b"L" * 200)
        self.assertGreater(len(s), 184)
        info = ts_info.TsInfo()
        info.feed(packet(0x0011, s[:183], True)
                  + packet(0x0011, s[183:], False, cc=1))
        self.assertEqual(info.snapshot()["service"], "G8YTZ")

    def test_adaptation_field_offsets_the_payload(self):
        info = ts_info.TsInfo()
        info.feed(packet_with_af(0x0000, pat())
                  + packet_with_af(0x0011, sdt())
                  + packet_with_af(0x1000, pmt()))
        self.assertEqual(info.snapshot()["service"], "G8YTZ")
        self.assertEqual(info.snapshot()["video_codec"], "H265")

    def test_codecs(self):
        for es_type, expected in ((27, "H264"), (36, "H265"), (2, "MPEG2")):
            info = ts_info.TsInfo()
            info.feed(packet(0x0000, pat())
                      + packet(0x1000, pmt(streams=((es_type, 0x1001),
                                                    (3, 0x1002)))))
            self.assertEqual(info.snapshot()["video_codec"], expected)
            self.assertEqual(info.snapshot()["audio_codec"], "MPA")

    def test_unknown_stream_type_reports_its_number(self):
        """A blank would suggest there was no video at all. The number
        is what anybody would look up anyway."""
        info = ts_info.TsInfo()
        info.feed(packet(0x0000, pat())
                  + packet(0x1000, pmt(streams=((209, 0x1001),))))
        self.assertEqual(info.snapshot()["video_codec"], "type 209")

    def test_control_codes_are_stripped_from_names(self):
        """DVB puts a character-table selector in the first byte and
        encoders put control codes inside the string. Callsigns are
        ASCII, so anything below 0x20 goes."""
        info = ts_info.TsInfo()
        info.feed(packet(0x0011, sdt(name=b"\x05G8YTZ", provider=b"\x01GB3OO")))
        self.assertEqual(info.snapshot()["service"], "G8YTZ")
        self.assertEqual(info.snapshot()["provider"], "GB3OO")

    def test_the_callsign_does_not_outlive_the_stream(self):
        """A callsign left on the panel over a transmitter that has
        gone off the air is worse than no callsign."""
        original = ts_info.STALE_SECS
        ts_info.STALE_SECS = 0.2
        try:
            info = ts_info.TsInfo()
            info.feed(full_stream())
            self.assertEqual(info.snapshot()["service"], "G8YTZ")
            time.sleep(0.3)
            self.assertEqual(info.snapshot()["service"], "")
        finally:
            ts_info.STALE_SECS = original

    def test_clear_forgets_everything(self):
        info = ts_info.TsInfo()
        info.feed(full_stream())
        info.clear()
        self.assertEqual(info.snapshot()["service"], "")
        self.assertEqual(info.snapshot()["video_codec"], "")

    def test_rubbish_does_not_raise(self):
        """This runs on a machine nobody can reach, in the path the
        video takes. It may lose information; it may not throw."""
        info = ts_info.TsInfo()
        for junk in (b"", b"\x00" * 188, b"\x47" + b"\xFF" * 187,
                     bytes(range(256)) * 3, b"\x47\xFF\xFF\xFF" + b"\x00" * 184,
                     full_stream()[:100]):
            info.feed(junk)

    def test_a_section_claiming_an_absurd_length_is_ignored(self):
        """Section length is 12 bits, so a corrupt one can claim far
        more than a section may hold. Taken at face value it would sit
        waiting for bytes that never come and swallow every real
        section behind it."""
        info = ts_info.TsInfo()
        bad = bytes([0x42, 0xBF, 0xFF]) + b"\x00" * 180
        info.feed(packet(0x0011, bad))
        info.feed(packet(0x0011, sdt()))
        self.assertEqual(info.snapshot()["service"], "G8YTZ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
