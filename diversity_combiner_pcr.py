#!/usr/bin/env python3

# Copyright (C) 2026 Justin, G8YTZ / EI3IOB
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
diversity_combiner_pcr.py — PCR-segment-based diversity combiner.

NOT yet integrated into Lynx. A genuine architectural rebuild, not a
patch on the earlier packet-sequence-based combiner (diversity_
combiner.py) — that approach spent an entire session chasing bugs
that all traced back to the same root cause: continuity-counter-
based sequence numbers are entirely LOCALLY reconstructed by each
receiver, with no connection to the transmission itself. A stream
that goes silent and resumes has no way to know how much was missed,
because a 4-bit counter cannot reveal a gap's true size.

PCR (Program Clock Reference) solves this properly: it's a real
timestamp embedded directly in the transport stream's own adaptation
field, tied to the TRANSMITTER's own clock. Both receivers, watching
the same broadcast, see the exact same PCR values — a genuine shared
reference point neither side has to estimate or reconstruct. This is
the same approach BATC's ARISS HamTV Ground Station Merger uses in
real, continuous production to combine geographically diverse ground
stations receiving HamTV from the ISS (confirmed directly: PCR-
delimited segments, earliest arrival wins).

DESIGN:
  - The stream is divided into segments at each PCR boundary — the
    packets from one PCR value up to (but not including) the next.
    This is coarser than per-packet matching (typically tens of
    packets per segment) and matches on a value the transmitter
    itself guarantees is identical across both receivers, rather
    than something either side has to keep numerically aligned.
  - For each PCR value, whichever source's segment arrives first
    and is error-free is used immediately — no artificial waiting,
    no preference window. If the first arrival has an error, the
    other source gets a short chance to provide a clean version of
    the SAME segment before it's declared a genuine gap.
  - No sequence numbers, no wrap tracking, no reanchoring — the
    entire class of bugs from the previous version doesn't apply
    here, because there's no local state to fall out of sync in
    the first place.

Usage:
    python3 diversity_combiner_pcr.py \\
        --port-a 9941 --port-b 9942 --out-port 9943 \\
        --stats-interval 1.0 --stats-file diversity_stats_pcr.jsonl

SCOPE: PCR is typically only present on the video PID (or another
single designated PID), roughly every ~40ms in practice (DVB spec
requires at least every 100ms). Other PIDs (audio, PAT/PMT, etc.)
don't carry PCR themselves — they're grouped into whichever PCR
segment they physically arrived within, and travel with it as one
unit rather than being matched independently.
"""

import socket
import heapq
import sys

# Line-buffer stdout explicitly. When redirected to a file (as ours
# always is via lynx_start.sh), Python defaults to block-buffering
# rather than line-buffering — output only reaches the file once an
# internal buffer fills, not on each print(). The main stats output
# is frequent/verbose enough to hit that threshold regularly on its
# own, but the newer, shorter diagnostic prints below were found to
# not reliably show up in the log for long stretches as a result —
# confirmed directly: the code was genuinely running (present and
# correct in the deployed file) but its output wasn't reaching disk
# promptly. This ensures every print() below is flushed immediately.
sys.stdout.reconfigure(line_buffering=True)
import time
import argparse
import threading
import json
import os
import tracemalloc
from datetime import datetime
from collections import deque

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
DEBUG = False
SEGMENT_GRACE = 0.05     # seconds — how long to wait for the OTHER
                          # source's version of a segment, if the first
                          # arrival had an error. Generous relative to
                          # the sub-4ms internal skew measured earlier
                          # tonight, without adding much real latency.
MAX_SEGMENT_AGE = 0.300  # 300ms - absolute cap on how long any segment is ever waited on, regardless of
                          # reason. Fixes a confirmed issue: without this, the decision loop could wait
                          # indefinitely on a stuck segment while newer ones piled up behind it, then dump
                          # that entire backlog in a burst once the stuck segment finally resolved (e.g.
                          # after a momentary "false lock") - delivering visibly stale video/audio well
                          # after the fact ("old frames flashing up"), rather than ever giving up on an
                          # unrecoverable segment and moving on.
STATS_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10MB - rotate the append-only diagnostic JSONL log past this size

STRAGGLER_TOLERANCE_TICKS = 60 * 27_000_000  # 60 seconds' worth of PCR ticks AT THE CORRECT 27MHz
                          # RESOLUTION - parse_ts_packet() returns the full PCR (base*300+extension), not
                          # the 90kHz base alone. The previous version of this constant (60*90000) was
                          # silently 300x too small (~0.2s instead of 60s) - smaller than max_segment_age
                          # itself, causing near-constant false discontinuity misfires. Confirmed live:
                          # physically impossible "188 day" jump sizes in the log were the direct symptom.
PCR_SANITY_TOLERANCE_TICKS = 5 * 27_000_000  # 5 seconds' worth of PCR ticks - deliberately much
                          # tighter than STRAGGLER_TOLERANCE_TICKS above. That tolerance is meant to be
                          # generous, to avoid mistaking a genuine stream reset for ordinary jitter. This
                          # one is the opposite: how far a single source's PCR could plausibly jump
                          # between consecutive segments under any normal condition. Confirmed live: a
                          # transient RF disruption produced a single, wildly out-of-range PCR value with
                          # its TEI bit still clear, which the ordering logic then trusted at face value -
                          # this catches that case directly, independent of has_error.
PCR_RESET_RATE_TOLERANCE_TICKS = int(0.5 * 27_000_000)  # 0.5 seconds' worth of PCR ticks - how far
                          # the ACTUAL pcr delta between two candidate reset values may deviate from
                          # what the REAL elapsed time between their arrivals would predict, before a
                          # reset is confirmed. Confirmed live as necessary: under sustained, severe
                          # interference (not a clean one-off transition), the receiver can produce
                          # several different, unrelated garbage PCR values in quick succession - two of
                          # them landing within PCR_SANITY_TOLERANCE_TICKS of each other by pure chance
                          # was enough to falsely "confirm" a reset and let real corruption through. A
                          # genuine, continuing PCR sequence advances at a fixed, known rate (27MHz);
                          # unrelated garbage has no reason to happen to match that rate too.


def log(msg: str):
    """print() with a wall-clock timestamp prefix. This log previously
    had no timestamps at all - only relative ordering - which made
    correlating a specific event (e.g. a burst of implausible PCR
    values) against the diagnostics timeline (which IS timestamped)
    a matter of guesswork rather than a direct, precise match."""
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {msg}")


def parse_ts_packet(data: bytes):
    """Returns (pid, tei, pcr_or_None) for a single 188-byte TS
    packet, or None if this doesn't look like a valid TS packet.
    pcr, when present, is the 27MHz-resolution combined PCR value."""
    if len(data) < 4 or data[0] != TS_SYNC_BYTE:
        return None
    pid = ((data[1] & 0x1F) << 8) | data[2]
    tei = (data[1] >> 7) & 0x01

    adaptation_field_control = (data[3] & 0x30) >> 4
    pcr = None
    if adaptation_field_control in (2, 3) and len(data) >= 6:
        adaptation_field_length = data[4]
        if adaptation_field_length > 0:
            flags = data[5]
            pcr_flag = (flags & 0x10) != 0
            if pcr_flag and len(data) >= 12:
                b = data[6:12]
                pcr_base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
                pcr_ext = ((b[4] & 0x01) << 8) | b[5]
                pcr = pcr_base * 300 + pcr_ext

    return pid, tei, pcr


class Stats:
    def __init__(self, window_seconds: float = 10.0):
        self.lock = threading.Lock()
        self.from_a = 0
        self.from_b = 0
        self.from_a_corrupted = 0  # delivered from A despite being known-errored (no clean alternative in time)
        self.from_b_corrupted = 0
        self.gaps = 0
        self.a_clean_pkts = 0
        self.a_bad_pkts = 0
        self.b_clean_pkts = 0
        self.b_bad_pkts = 0
        # Rolling window — a genuine live view of CURRENT conditions.
        # The cumulative figures above are since-start averages: once
        # a gap is counted it's permanently baked into that run's
        # percentage forever, even if conditions improve immediately
        # afterward — confirmed as the actual explanation for "0.3%
        # loss, then 0% after a restart with no RF changes at all."
        # A rolling window answers "how is it doing right now",
        # which is what actually matters when watching the effect of
        # adjusting an antenna live.
        self.window_seconds = window_seconds
        self.window_events = deque()  # (monotonic_time, 'A'/'B'/'gap', corrupted_bool)

        # Tight, immediate signal for "how long has output genuinely
        # been clean" - deliberately separate from the rolling window
        # above, which averages over ~10s/500 segments and so still
        # shows recent corruption in its figures for several seconds
        # after a real disturbance has already cleared. A restart
        # breaker deciding "is it safe to retry now" needs something
        # much more immediate than that average - confirmed live: the
        # combiner's own output was already fully clean for well over
        # a minute while a fixed-timer breaker was still blindly
        # suppressing every restart attempt regardless.
        self.last_bad_segment_at = None  # monotonic time of the most
                                          # recent gap or corrupted
                                          # segment - None means clean
                                          # since this process started

    def record_segment(self, source: str, corrupted: bool = False):
        with self.lock:
            if source == 'A':
                self.from_a += 1
                if corrupted:
                    self.from_a_corrupted += 1
            elif source == 'B':
                self.from_b += 1
                if corrupted:
                    self.from_b_corrupted += 1
            else:
                self.gaps += 1
            self.window_events.append((time.monotonic(), source, corrupted))
            if corrupted or source not in ('A', 'B'):
                self.last_bad_segment_at = time.monotonic()

    def record_input_packet(self, source: str, clean: bool):
        with self.lock:
            if source == 'A':
                if clean:
                    self.a_clean_pkts += 1
                else:
                    self.a_bad_pkts += 1
            else:
                if clean:
                    self.b_clean_pkts += 1
                else:
                    self.b_bad_pkts += 1

    def snapshot(self):
        with self.lock:
            total = self.from_a + self.from_b + self.gaps
            a_total = self.a_clean_pkts + self.a_bad_pkts
            b_total = self.b_clean_pkts + self.b_bad_pkts

            # Purge window entries older than the window — this is
            # what makes it "rolling" rather than also cumulative.
            cutoff = time.monotonic() - self.window_seconds
            while self.window_events and self.window_events[0][0] < cutoff:
                self.window_events.popleft()
            w_a = sum(1 for (_, s, c) in self.window_events if s == 'A' and not c)
            w_b = sum(1 for (_, s, c) in self.window_events if s == 'B' and not c)
            w_a_corrupted = sum(1 for (_, s, c) in self.window_events if s == 'A' and c)
            w_b_corrupted = sum(1 for (_, s, c) in self.window_events if s == 'B' and c)
            w_gap = sum(1 for (_, s, c) in self.window_events if s == 'gap')
            w_total = len(self.window_events)

            return {
                "t": time.time(),
                "segments_from_a": self.from_a,
                "segments_from_b": self.from_b,
                "segments_from_a_corrupted": self.from_a_corrupted,
                "segments_from_b_corrupted": self.from_b_corrupted,
                "segment_gaps": self.gaps,
                "total_segments": total,
                "pct_a": round(self.from_a / total * 100, 2) if total else 0.0,
                "pct_b": round(self.from_b / total * 100, 2) if total else 0.0,
                "pct_a_corrupted": round(self.from_a_corrupted / total * 100, 2) if total else 0.0,
                "pct_b_corrupted": round(self.from_b_corrupted / total * 100, 2) if total else 0.0,
                "pct_gap": round(self.gaps / total * 100, 2) if total else 0.0,
                "a_quality_pct": round(self.a_clean_pkts / a_total * 100, 2) if a_total else 0.0,
                "b_quality_pct": round(self.b_clean_pkts / b_total * 100, 2) if b_total else 0.0,
                "a_pkts": a_total,
                "b_pkts": b_total,
                # Rolling window (last window_seconds only) — the live view
                "window_pct_a": round(w_a / w_total * 100, 2) if w_total else 0.0,
                "window_pct_b": round(w_b / w_total * 100, 2) if w_total else 0.0,
                "window_pct_a_corrupted": round(w_a_corrupted / w_total * 100, 2) if w_total else 0.0,
                "window_pct_b_corrupted": round(w_b_corrupted / w_total * 100, 2) if w_total else 0.0,
                "window_pct_gap": round(w_gap / w_total * 100, 2) if w_total else 0.0,
                "window_total": w_total,
                # None means clean since this combiner process started -
                # deliberately distinct from a large number, so a reader
                # doesn't need to guess whether "never seen a bad
                # segment" and "saw one a very long time ago" mean the
                # same thing.
                "seconds_since_bad_segment": (
                    round(time.monotonic() - self.last_bad_segment_at, 2)
                    if self.last_bad_segment_at is not None else None
                ),
            }


def stats_reporter(stats: Stats, interval: float, stats_file: str, live_snapshot_path: str,
                    stop_event: threading.Event, decider_holder: dict = None):
    while not stop_event.is_set():
        time.sleep(interval)
        snap = stats.snapshot()
        if decider_holder is not None and 'decider' in decider_holder:
            decider = decider_holder['decider']
            snap['preferred_source'] = decider.preferred_source
            snap['mer_a'] = decider._mer_a
            snap['mer_b'] = decider._mer_b
            mer_a_str = f"{decider._mer_a:.1f}dB" if decider._mer_a is not None else "?"
            mer_b_str = f"{decider._mer_b:.1f}dB" if decider._mer_b is not None else "?"
            log(f"[preferred_source_status]  currently: {decider.preferred_source}   "
                  f"MER A: {mer_a_str}  B: {mer_b_str}")
        log(f"[input quality]     A: {snap['a_quality_pct']:.1f}% clean ({snap['a_pkts']} pkts)   "
              f"B: {snap['b_quality_pct']:.1f}% clean ({snap['b_pkts']} pkts)")
        log(f"[LIVE, last {stats.window_seconds:.0f}s]  A: {snap['window_pct_a']:.1f}% (corrupted: {snap['window_pct_a_corrupted']:.1f}%)  "
              f"B: {snap['window_pct_b']:.1f}% (corrupted: {snap['window_pct_b_corrupted']:.1f}%)  "
              f"gaps: {snap['window_pct_gap']:.1f}%  ({snap['window_total']} segments)")
        log(f"[since start]       A: {snap['pct_a']:.1f}% (corrupted: {snap['pct_a_corrupted']:.1f}%)  "
              f"B: {snap['pct_b']:.1f}% (corrupted: {snap['pct_b_corrupted']:.1f}%)  "
              f"gaps: {snap['pct_gap']:.1f}%  (total {snap['total_segments']} segments)")
        if live_snapshot_path:
            # Always-overwritten single JSON object, one file — for
            # a consumer that just wants "the current numbers" (e.g.
            # Lynx's own /api/status), distinct from the append-only
            # JSONL log below which is for later offline analysis.
            # Write-then-rename avoids a reader ever seeing a
            # half-written file mid-update.
            tmp_path = live_snapshot_path + ".tmp"
            with open(tmp_path, 'w') as f:
                json.dump(snap, f)
            os.replace(tmp_path, live_snapshot_path)
        if stats_file:
            # Confirmed live: this file grows unbounded with no
            # rotation - reached 19MB in one long session, since it's
            # a relative path that (unlike /tmp) persists across
            # reboots. Simple size-based rotation: once past
            # STATS_FILE_MAX_BYTES, the old file is kept as a single
            # ".old" backup (not an indefinitely-growing numbered
            # series) and a fresh one starts - bounded total disk use
            # while still keeping some recent history for offline
            # analysis, which is this file's only actual purpose.
            try:
                if os.path.exists(stats_file) and os.path.getsize(stats_file) > STATS_FILE_MAX_BYTES:
                    os.replace(stats_file, stats_file + ".old")
            except OSError:
                pass  # rotation failing is never worth losing the actual stats write over
            with open(stats_file, 'a') as f:
                f.write(json.dumps(snap) + "\n")


class Segmenter:
    """Groups incoming packets into PCR-delimited segments. A new
    segment begins every time a packet carrying a PCR value is seen;
    everything since the previous PCR-carrying packet belongs to
    that previous segment.

    MAX_PACKETS_WITHOUT_PCR guards against a real failure mode: if
    PCR genuinely stops arriving for an extended period (e.g. a
    sustained fade or corruption specifically on the PCR-carrying
    PID), current_packets previously grew completely unbounded,
    since it's only ever reset when a NEW pcr value arrives — with
    no cap, this was a second, separate memory leak, invisible to
    the cleanup already applied to the main combining loop's own
    bookkeeping (that cleanup only runs once a segment is actually
    completed, which never happens in this failure mode at all).
    Confirmed directly: a live combiner reached ~2.6GB RSS overnight
    despite the earlier fix, traced to exactly this."""
    MAX_PACKETS_WITHOUT_PCR = 5000  # generous — a normal segment is a few hundred packets at most; this is purely a safety bound

    def __init__(self):
        self.current_pcr = None
        self.current_packets = []  # list of (packet_bytes, tei)
        self._dropped_for_no_pcr = 0  # rate-limits the warning below

    def add_packet(self, packet: bytes, tei: bool, pcr):
        """Returns (closed_pcr, closed_packets, closed_has_error) if
        this packet closed out a previous segment, else None."""
        result = None
        if pcr is not None:
            if self.current_pcr is not None:
                has_error = any(t for (_, t) in self.current_packets)
                result = (self.current_pcr, self.current_packets, has_error)
            self.current_pcr = pcr
            self.current_packets = [(packet, tei)]
        else:
            if len(self.current_packets) >= self.MAX_PACKETS_WITHOUT_PCR:
                # PCR hasn't arrived in far longer than any real
                # segment should take — discard rather than grow
                # further. This data can't be properly attributed to
                # a segment without a PCR boundary anyway.
                self.current_packets = [(packet, tei)]
                self._dropped_for_no_pcr += 1
                if self._dropped_for_no_pcr <= 5 or self._dropped_for_no_pcr % 100 == 0:
                    log(f"[warning] No PCR seen for {self.MAX_PACKETS_WITHOUT_PCR} packets — "
                          f"discarding buffer (occurrence #{self._dropped_for_no_pcr})")
            else:
                self.current_packets.append((packet, tei))
        return result


class SegmentDecider:
    """Core PCR-segment decision logic - heap-based version. Same
    correctness requirements as the original (immediate output on
    first clean arrival, grace period for errored-first-arrival, hard
    cap on wait time via MAX_SEGMENT_AGE, strict order preservation,
    straggler tolerance with discontinuity handling) but with less
    synchronized state: no separate pcr_order/pcr_seen/decided_up_to_index
    trio, and no periodic trim step - the heap and its dedup set
    naturally shrink as segments are decided. Validated against the
    same 37-check test harness as the original (test_combiner_harness.py
    and test_heap_decider_prototype.py), including the two scenarios
    that specifically matter here: a source dropping out entirely
    (test 2) and a source persistently lagging, matching the original
    high-motion crash report (test 4). THIS IS A LIVE TEST VERSION -
    not yet validated against real hardware; keep the proven original
    backed up and easy to restore."""

    def __init__(self, segment_grace=SEGMENT_GRACE, max_segment_age=MAX_SEGMENT_AGE,
                 mer_switch_dwell_secs=10.0, mer_switch_margin_db=1.0):
        self.segment_grace = segment_grace
        self.max_segment_age = max_segment_age
        self.mer_switch_dwell_secs = mer_switch_dwell_secs  # how long a challenger source
                                        # must be consistently, meaningfully better before
                                        # it's allowed to become the new preferred source
        self.mer_switch_margin_db = mer_switch_margin_db  # how much better (dB) the
                                        # challenger's MER must be, not just marginally
                                        # ahead - guards against flip-flopping on MER noise
                                        # when the two sources are nearly tied
        self.completed = {'A': {}, 'B': {}}  # pcr -> (packets, has_error, arrival_time)
        self.pending_heap = []                # min-heap of PCRs not yet decided
        self.pending_set = set()               # dedup guard for the heap
        self.last_decided_pcr = None
        self.last_sane_pcr = {'A': None, 'B': None}  # per-source, most recent PCR that
                                        # passed the sanity check below - confirmed live as
                                        # necessary: a transient RF disruption (e.g. a
                                        # modcod re-acquisition moment) can corrupt a
                                        # packet's PCR field specifically while its TEI bit
                                        # still reads clean, meaning has_error alone never
                                        # catches it. That garbage PCR then gets trusted at
                                        # face value by the ordering/discontinuity logic,
                                        # producing real, confirmed output corruption.
        self._pending_reset_pcr = {'A': None, 'B': None}  # (pcr, arrival_time) of an
                                        # out-of-range PCR awaiting confirmation - see
                                        # add_segment() for how confirmation is judged
        self.stall_pcr = None
        self.stall_since = None
        self._mer_a = None
        self._mer_b = None
        self._mer_last_read = 0.0
        self.last_used_source = None  # 'A' or 'B' - the source actually chosen for the most
                                        # recently decided segment. Kept purely for stats/
                                        # diagnostics now - no longer drives the tie-break
                                        # decision itself (see preferred_source below).
        self.preferred_source = None  # 'A' or 'B' - which source the tie-break actually
                                        # uses when both are clean. Confirmed live as a
                                        # necessary distinction from last_used_source: a
                                        # single brief error on the OTHER source forces an
                                        # immediate, correct switch to whichever is clean
                                        # (unchanged, still instant) but that alone should
                                        # never be allowed to change which source is
                                        # PREFERRED going forward - otherwise one rare,
                                        # brief B blip can knock the preference onto a
                                        # genuinely marginal, unstable A, which then keeps
                                        # re-triggering switches back and forth as A's own
                                        # instability continues. preferred_source only ever
                                        # changes via sustained, evidence-based MER
                                        # superiority (see _update_preferred_source), never
                                        # as a side effect of a single segment's outcome.
        self._challenger_better_since = None  # timestamp since the non-preferred source's
                                        # MER has been continuously >= margin_db ahead of
                                        # the preferred source's, or None if not currently
                                        # in such a streak (any dip below the margin resets
                                        # this - the dwell must be continuous, not cumulative)
        self.lock = threading.Lock()  # Confirmed live as a real, serious bug: add_segment() is
                                        # called concurrently from two separate reader threads (one
                                        # per source) while tick() runs from the main loop thread,
                                        # all touching completed/pending_heap/pending_set with no
                                        # synchronization at all. heapq push/pop are not atomic as
                                        # whole operations - concurrent access can corrupt the heap's
                                        # internal list, silently losing track of specific entries
                                        # that remain in completed[] and pending_set (so a legitimate
                                        # retry can never re-add them) but become unreachable through
                                        # the heap itself, staying stuck forever. Confirmed via live
                                        # evidence: completed[A] permanently stuck at a fixed count
                                        # across many minutes, [stall] messages citing wildly
                                        # unrelated PCR values, and fresh mpv instances immediately
                                        # hitting HEVC parameter-set errors on every restart - the
                                        # combiner's own output stream had genuine, persistent gaps
                                        # regardless of which mpv instance was reading it.

    MER_REFRESH_INTERVAL = 1.0   # matches lynx_app.py's own publish interval - no point reading more often
    MER_PUBLISH_PATH = "/tmp/lynx_tuner_mer.json"

    def _refresh_mer(self, now):
        """Re-reads the MER file at most once per MER_REFRESH_INTERVAL.
        On any failure (file missing, stale process, malformed JSON),
        falls back to whatever was last known rather than raising -
        the tie-break itself already has its own fallback (arrival
        order) for when no MER data is available at all, so a
        transient read failure here is never fatal to the decision
        loop."""
        if now - self._mer_last_read < self.MER_REFRESH_INTERVAL:
            return
        self._mer_last_read = now
        try:
            with open(self.MER_PUBLISH_PATH) as f:
                data = json.load(f)
            self._mer_a = data.get("mer_a")
            self._mer_b = data.get("mer_b")
        except Exception:
            pass  # keep whatever was last known

    def _update_preferred_source(self, now):
        """Updates self.preferred_source based on sustained MER
        superiority, independent of whether there's an actual tie to
        break on this particular tick - MER should be tracked
        continuously, not just sampled at decision time. Only changes
        preferred_source when the challenger has been continuously
        ahead by mer_switch_margin_db for mer_switch_dwell_secs; any
        dip below the margin resets the streak rather than merely
        pausing it, so this requires sustained, not cumulative,
        superiority. Falls back to leaving preferred_source untouched
        if MER data isn't available - the tie-break itself has its own
        further fallback for that case."""
        if self._mer_a is None or self._mer_b is None:
            return

        if self.preferred_source is None:
            self.preferred_source = 'A'  # first decision, no preference established yet -
                                           # matches the previous default behaviour
            return

        preferred_mer = self._mer_a if self.preferred_source == 'A' else self._mer_b
        challenger = 'B' if self.preferred_source == 'A' else 'A'
        challenger_mer = self._mer_b if challenger == 'B' else self._mer_a

        if challenger_mer - preferred_mer >= self.mer_switch_margin_db:
            if self._challenger_better_since is None:
                self._challenger_better_since = now
            elif now - self._challenger_better_since >= self.mer_switch_dwell_secs:
                log(f"[preferred_source] Switching preference {self.preferred_source} -> "
                      f"{challenger} after {self.mer_switch_dwell_secs:.0f}s of consistently "
                      f"better MER ({challenger_mer:.1f}dB vs {preferred_mer:.1f}dB)")
                self.preferred_source = challenger
                self._challenger_better_since = None
        else:
            self._challenger_better_since = None

    def add_segment(self, source: str, pcr, packets, has_error: bool, now: float):
        with self.lock:
            last_sane = self.last_sane_pcr[source]
            if last_sane is not None and abs(pcr - last_sane) > PCR_SANITY_TOLERANCE_TICKS:
                pending = self._pending_reset_pcr[source]
                confirmed = False
                if pending is not None:
                    pending_pcr, pending_time = pending
                    if abs(pcr - pending_pcr) <= PCR_SANITY_TOLERANCE_TICKS:
                        # Numerically close - but a genuine, continuing PCR
                        # sequence should ALSO advance at the correct,
                        # fixed 27MHz rate relative to how much real time
                        # actually passed between the two arrivals.
                        # Unrelated garbage has no reason to happen to
                        # match that rate too - confirmed live as a real,
                        # necessary distinction under sustained interference.
                        real_elapsed = now - pending_time
                        expected_pcr_delta = real_elapsed * 27_000_000
                        actual_pcr_delta = pcr - pending_pcr
                        rate_mismatch = abs(actual_pcr_delta - expected_pcr_delta)
                        if rate_mismatch <= PCR_RESET_RATE_TOLERANCE_TICKS:
                            confirmed = True
                if confirmed:
                    log(f"[pcr] Confirmed genuine PCR reset on source {source} - two "
                          f"consecutive values consistent with each other AND with the real "
                          f"time elapsed between them")
                    self.last_sane_pcr[source] = pcr
                    self._pending_reset_pcr[source] = None
                else:
                    deviation = abs(pcr - last_sane)
                    log(f"[pcr] Implausible PCR from source {source}: {pcr} vs last sane "
                          f"{last_sane} (deviation {deviation} ticks / {deviation/27_000_000:.1f}s) - "
                          f"discarding this segment entirely rather than trusting it for ordering, "
                          f"regardless of TEI. Awaiting a confirming value before treating this as "
                          f"a genuine reset rather than transient corruption.")
                    self._pending_reset_pcr[source] = (pcr, now)
                    return  # never trust this PCR for ordering - discard the segment entirely
            else:
                self.last_sane_pcr[source] = pcr
                self._pending_reset_pcr[source] = None

            if self.last_decided_pcr is not None and pcr <= self.last_decided_pcr:
                behind = self.last_decided_pcr - pcr
                if behind < STRAGGLER_TOLERANCE_TICKS:
                    return  # genuine late straggler for an already-decided PCR - discard
                log(f"[pcr] Large backward jump detected (behind by {behind} ticks / {behind/27_000_000:.1f}s) — "
                      f"treating as a stream discontinuity (wraparound or reset), not a late straggler")
                self.last_decided_pcr = None
            self.completed[source][pcr] = (packets, has_error, now)
            if pcr not in self.pending_set:
                self.pending_set.add(pcr)
                heapq.heappush(self.pending_heap, pcr)

    def tick(self, now: float):
        with self.lock:
            return self._tick_impl(now)

    def _tick_impl(self, now: float):
        events = []
        while self.pending_heap:
            pcr = self.pending_heap[0]  # peek smallest pending PCR - never skip ahead of it
            a_entry = self.completed['A'].get(pcr)
            b_entry = self.completed['B'].get(pcr)

            arrival_times = [e[2] for e in (a_entry, b_entry) if e is not None]
            first_time = min(arrival_times) if arrival_times else now
            age = now - first_time
            forced = age >= self.max_segment_age

            both_arrived = a_entry is not None and b_entry is not None

            # Wait for the grace period before deciding whenever only
            # one source has arrived so far - regardless of whether
            # that lone arrival is clean or errored. Previously this
            # wait only applied when the sole arrival was errored,
            # since a clean lone arrival fell straight through to an
            # immediate decision below without ever reaching this
            # check. That silently bypassed the tie-break (and
            # therefore preferred_source) entirely for whichever
            # source happens to have even a small, systematic latency
            # edge - confirmed live as a persistent ~10% leak to the
            # non-preferred source despite both sources being 100%
            # clean at the packet level.
            if not both_arrived and age < self.segment_grace and not forced:
                self._note_stall(pcr, now)
                break

            chosen, source, corrupted = None, None, False
            candidates = []
            if a_entry:
                candidates.append(('A', a_entry))
            if b_entry:
                candidates.append(('B', b_entry))

            if a_entry and b_entry and not a_entry[1] and not b_entry[1]:
                # Genuine tie: both sources clean for this exact segment.
                self._refresh_mer(now)
                self._update_preferred_source(now)
                if self.preferred_source == 'B':
                    chosen, source = b_entry[0], 'B'
                elif self.preferred_source == 'A':
                    chosen, source = a_entry[0], 'A'
                elif self.last_used_source == 'B':
                    # No MER data available at all yet (preferred_source
                    # never got established) - fall back to the simple
                    # sticky behaviour rather than blocking on MER.
                    chosen, source = b_entry[0], 'B'
                else:
                    chosen, source = a_entry[0], 'A'
            else:
                candidates.sort(key=lambda c: c[1][2])
                for src, (packets, has_error, arrival) in candidates:
                    if not has_error:
                        chosen, source = packets, src
                        break

            if chosen is None and candidates and not forced:
                # The "wait for the other source" case is now handled
                # above, before this point is ever reached - what's
                # left here is specifically "the only candidate(s)
                # present are all errored", not "only one has arrived
                # yet". Force a decision using whatever's available.
                best = candidates[0][1]
                if a_entry and b_entry:
                    chosen, source = None, None
                else:
                    chosen, source = best[0], candidates[0][0]
                    corrupted = True

            if chosen is None and not candidates and not forced:
                self._note_stall(pcr, now)
                break

            if chosen is not None:
                events.append(('output', pcr, chosen, source, corrupted, age))
            else:
                events.append(('gap', pcr, None, None, False, age))

            heapq.heappop(self.pending_heap)
            self.pending_set.discard(pcr)
            self.completed['A'].pop(pcr, None)
            self.completed['B'].pop(pcr, None)
            self.last_decided_pcr = pcr
            if source is not None:
                self.last_used_source = source
            if self.stall_pcr == pcr:
                self.stall_pcr = None

        return events

    def _note_stall(self, pcr, now):
        if pcr != self.stall_pcr:
            self.stall_pcr, self.stall_since = pcr, now

    def backlog_size(self):
        return len(self.pending_heap)

    def memory_footprint(self):
        return len(self.completed['A']) + len(self.completed['B']) + len(self.pending_heap) + len(self.pending_set)


def combine(port_a: int, port_b: int, out_ip: str, out_port: int, stats: Stats, stop_event: threading.Event,
            mer_switch_dwell_secs: float = 10.0, mer_switch_margin_db: float = 1.0, decider_holder: dict = None):
    sock_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_a.bind(('0.0.0.0', port_a))
    sock_a.settimeout(0.5)

    sock_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_b.bind(('0.0.0.0', port_b))
    sock_b.settimeout(0.5)

    out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    lock = threading.Lock()
    decider = SegmentDecider(mer_switch_dwell_secs=mer_switch_dwell_secs,
                              mer_switch_margin_db=mer_switch_margin_db)
    if decider_holder is not None:
        decider_holder['decider'] = decider

    def reader(sock, source_label):
        segmenter = Segmenter()
        while not stop_event.is_set():
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            now = time.monotonic()
            for offset in range(0, len(data) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
                packet = data[offset:offset + TS_PACKET_SIZE]
                parsed = parse_ts_packet(packet)
                if parsed is None:
                    continue
                pid, tei, pcr = parsed
                if pid == 8191:
                    continue  # null/stuffing packet, no real content
                stats.record_input_packet(source_label, not tei)
                closed = segmenter.add_packet(packet, tei, pcr)
                if closed is not None:
                    closed_pcr, closed_packets, closed_has_error = closed
                    with lock:
                        decider.add_segment(source_label, closed_pcr, closed_packets, closed_has_error, now)

    thread_a = threading.Thread(target=reader, args=(sock_a, 'A'), daemon=True)
    thread_b = threading.Thread(target=reader, args=(sock_b, 'B'), daemon=True)
    thread_a.start()
    thread_b.start()

    print(f"Combining A (port {port_a}) + B (port {port_b}) -> output {out_ip}:{out_port}")
    print(f"PCR-segment based — earliest clean arrival wins, {SEGMENT_GRACE*1000:.0f}ms grace "
          f"for the other source if the first arrival had an error, {MAX_SEGMENT_AGE*1000:.0f}ms "
          f"absolute cap before giving up on any single segment")

    stall_last_logged = 0
    last_backlog_log = 0

    while not stop_event.is_set():
        time.sleep(0.005)
        out_buf = bytearray()
        now = time.monotonic()

        with lock:
            events = decider.tick(now)

            for event_type, pcr, packets, source, corrupted, age in events:
                if event_type == 'output':
                    for packet, _ in packets:
                        out_buf += packet
                    stats.record_segment(source, corrupted)
                else:
                    stats.record_segment('gap')
                    if DEBUG:
                        log(f"[debug] GAP: pcr={pcr} — no clean segment from either source (age={age*1000:.0f}ms)")

            if decider.stall_pcr is not None and now - stall_last_logged > 1.0:
                backlog = decider.backlog_size()
                log(f"[stall] pcr={decider.stall_pcr} stuck {now-decider.stall_since:.2f}s — {backlog} segment(s) queued behind it")
                stall_last_logged = now

            if now - last_backlog_log > 10.0:
                log(f"[backlog] {decider.backlog_size()} segment(s) queued, "
                      f"completed[A]={len(decider.completed['A'])} completed[B]={len(decider.completed['B'])}")
                last_backlog_log = now

        if out_buf:
            CHUNK = TS_PACKET_SIZE * 7
            for i in range(0, len(out_buf), CHUNK):
                out_sock.sendto(bytes(out_buf[i:i + CHUNK]), (out_ip, out_port))

    sock_a.close()
    sock_b.close()
    out_sock.close()


def memory_diagnostics(interval: float, stop_event: threading.Event):
    """Periodically compares tracemalloc snapshots to identify
    exactly which line of code is accumulating memory over time.
    Two confirmed leak sources have already been found and fixed
    this way (the main loop's own segment bookkeeping, and the
    per-source segment buffer growing unbounded when PCR stopped
    arriving) — but memory was still confirmed climbing on a live,
    multi-hour run with neither of those recurring (the "No PCR
    seen" safety warning never fired), meaning there's a third,
    different source not yet found. Rather than guess again from
    code inspection alone, this gets real, direct evidence: which
    specific allocation site is actually growing, not a hypothesis
    about which one might be."""
    tracemalloc.start(10)  # keep 10 frames of traceback per allocation, enough to identify the real source
    previous_snapshot = None
    while not stop_event.is_set():
        time.sleep(interval)
        snapshot = tracemalloc.take_snapshot()
        current, peak = tracemalloc.get_traced_memory()
        log(f"[memory] current={current/1024/1024:.1f}MB peak={peak/1024/1024:.1f}MB (tracemalloc-tracked only, not full RSS)")
        if previous_snapshot is not None:
            top_diffs = snapshot.compare_to(previous_snapshot, 'lineno')
            print("[memory] Top 5 growing allocations since last check:")
            for stat in top_diffs[:5]:
                if stat.size_diff > 0:
                    log(f"[memory]   +{stat.size_diff/1024:.1f}KB ({stat.count_diff:+d} objects)  {stat.traceback.format()[-1].strip()}")
        previous_snapshot = snapshot


def main():
    global DEBUG
    parser = argparse.ArgumentParser(description="PCR-segment-based diversity combiner")
    parser.add_argument('--port-a', type=int, default=9941)
    parser.add_argument('--port-b', type=int, default=9942)
    parser.add_argument('--out-ip', default='127.0.0.1', help='Destination IP for the combined output (e.g. your Mac\'s IP for direct VLC testing)')
    parser.add_argument('--out-port', type=int, default=9943)
    parser.add_argument('--stats-interval', type=float, default=1.0)
    parser.add_argument('--window', type=float, default=10.0, help='Rolling window size in seconds for the live view (default 10s)')
    parser.add_argument('--stats-file', default='diversity_stats_pcr.jsonl', help='Append-only JSONL diagnostic log')
    parser.add_argument('--live-stats-file', default=None, help='Always-overwritten single JSON snapshot, for a live consumer like Lynx itself')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--memory-diag-interval', type=float, default=20.0,
                         help='Seconds between memory diagnostic snapshots, showing which allocation is actually growing (default 300s/5min)')
    parser.add_argument('--mer-switch-dwell-secs', type=float, default=10.0,
                         help='How long (seconds) a challenger source must be consistently, meaningfully better in MER before it becomes the new preferred source (default 10s)')
    parser.add_argument('--mer-switch-margin-db', type=float, default=1.0,
                         help='How much better (dB) the challenger source\'s MER must be before it counts as genuinely better, not just marginally ahead / MER noise (default 1.0dB)')
    args = parser.parse_args()

    DEBUG = args.debug
    stats = Stats(window_seconds=args.window)
    stop_event = threading.Event()

    decider_holder = {}  # populated by combine() once it creates the actual SegmentDecider -
                           # lets stats_reporter (already running by then) pick up a live
                           # reference to it without needing to restructure startup order
    reporter = threading.Thread(target=stats_reporter, args=(stats, args.stats_interval, args.stats_file, args.live_stats_file, stop_event, decider_holder), daemon=True)
    reporter.start()

    memdiag = threading.Thread(target=memory_diagnostics, args=(args.memory_diag_interval, stop_event), daemon=True)
    memdiag.start()

    try:
        combine(args.port_a, args.port_b, args.out_ip, args.out_port, stats, stop_event,
                mer_switch_dwell_secs=args.mer_switch_dwell_secs,
                mer_switch_margin_db=args.mer_switch_margin_db,
                decider_holder=decider_holder)
    except KeyboardInterrupt:
        print("\nStopping...")
        stop_event.set()
        final = stats.snapshot()
        print(f"\n=== Final summary ===")
        print(f"  Input quality — A: {final['a_quality_pct']:.1f}% clean ({final['a_pkts']} pkts)   "
              f"B: {final['b_quality_pct']:.1f}% clean ({final['b_pkts']} pkts)")
        print(f"  Segments — A: {final['pct_a']:.1f}% (corrupted: {final['pct_a_corrupted']:.1f}%)  "
              f"B: {final['pct_b']:.1f}% (corrupted: {final['pct_b_corrupted']:.1f}%)  Gaps: {final['pct_gap']:.1f}%")
        print(f"  Total segments: {final['total_segments']}")


if __name__ == '__main__':
    main()
