#!/usr/bin/env python3
"""Auto-Squeak - Lindos sequence analyser for Lynx.

Listens to received audio, syncs on the FSK segment headers, and
measures whatever the sequence contains. Measurements are made against
alignment level (PPM4, -18 dBFS peak) rather than being purely
relative, because the sender's alignment is a known standard - so a
tone arriving low is a real gain error in the chain rather than an
arbitrary number.

Physical layer, measured empirically from a Lindos WAV rather than
taken from documentation:
    two-tone FSK, space 1655 Hz / mark 1872 Hz
    bit period 9 ms (about 110 baud)
    segment header about 224 ms, sequence-start header about 1 s

The bit pattern is treated as an OPAQUE FINGERPRINT, not decoded.
Lindos's own encoding is undocumented publicly, but the payload for a
given segment is identical between runs, so matching the pattern is
enough to identify a segment - and where a pattern is unrecognised the
audio that follows is classified directly instead. Nothing here needs
Lindos's format to be reverse-engineered.
"""
import numpy as np

SR_DEFAULT = 48000

FSK_SPACE = 1654.8
FSK_MARK  = 1871.8
FSK_BIT_S = 0.009

# Alignment level. PPM4 = -18 dBFS PEAK; a sine sits 3.01 dB below its
# peak in RMS, so an on-level tone reads -21.01 dBFS RMS. Both are kept
# because levels are quoted as peak (broadcast practice) but measured
# as RMS (what the maths gives).
ALIGN_PEAK_DBFS = -18.0
SINE_RMS_OFFSET = 20 * np.log10(1 / np.sqrt(2))   # -3.01


def db(x):
    return 20 * np.log10(np.maximum(np.asarray(x, dtype=float), 1e-12))


def ppm_from_peak_dbfs(peak_dbfs):
    """BBC/EBU PPM, 4 dB per mark, PPM4 at alignment."""
    return 4.0 + (peak_dbfs - ALIGN_PEAK_DBFS) / 4.0


# ----------------------------------------------------------------- FSK

def fsk_energy(mono, sr, win_s=0.004, hop_s=0.0005):
    """Two Goertzel-style detectors, returned as (mark - space) and the
    fraction of total energy in the FSK band. The second is what
    detects a header at all; the first slices the bits."""
    n = int(sr * win_s)
    hop = int(sr * hop_s)
    t = np.arange(n) / sr
    cs = np.exp(-2j * np.pi * FSK_SPACE * t)
    cm = np.exp(-2j * np.pi * FSK_MARK * t)
    idx = np.arange(0, len(mono) - n, hop)
    d = np.empty(len(idx))
    frac = np.empty(len(idx))
    for j, i in enumerate(idx):
        fr = mono[i:i + n]
        s = abs((fr * cs).sum())
        m = abs((fr * cm).sum())
        tot = np.sqrt((fr ** 2).sum() * n) + 1e-12
        d[j] = m - s
        frac[j] = (s + m) / tot
    return idx / sr, d, frac


def find_headers(mono, sr, min_ms=90):
    """Return (start_s, end_s) for every FSK burst."""
    t, _d, frac = fsk_energy(mono, sr, win_s=0.008, hop_s=0.002)
    on = frac > 0.55
    out = []
    s = None
    for i, v in enumerate(on):
        if v and s is None:
            s = i
        elif not v and s is not None:
            if (t[i] - t[s]) * 1000 >= min_ms:
                out.append((t[s], t[i]))
            s = None
    if s is not None and (t[-1] - t[s]) * 1000 >= min_ms:
        out.append((t[s], t[-1]))
    return out


def header_bits(mono, sr, a, b):
    """Slice a header into bits. Returned as a string and used only as
    a fingerprint - see module docstring."""
    seg = mono[int(a * sr):int(b * sr)]
    if len(seg) < int(sr * FSK_BIT_S * 2):
        return ''
    n = int(sr * FSK_BIT_S * 0.8)
    hop = max(1, int(sr * 0.0005))
    t = np.arange(n) / sr
    cs = np.exp(-2j * np.pi * FSK_SPACE * t)
    cm = np.exp(-2j * np.pi * FSK_MARK * t)
    d = []
    for i in range(0, len(seg) - n, hop):
        fr = seg[i:i + n]
        d.append(abs((fr * cm).sum()) - abs((fr * cs).sum()))
    d = np.array(d)
    sl = (d > 0).astype(int)
    spb = FSK_BIT_S / 0.0005
    bits, k = [], spb / 2
    while k < len(sl):
        bits.append(sl[int(k)])
        k += spb
    return ''.join(map(str, bits))


# --------------------------------------------------------- measurement

def seg_levels(L, R, sr, a, b):
    l = L[int(a * sr):int(b * sr)]
    r = R[int(a * sr):int(b * sr)]
    if len(l) < 64:
        return None
    return {
        'l_rms': db(np.sqrt((l ** 2).mean())), 'r_rms': db(np.sqrt((r ** 2).mean())),
        'l_pk':  db(np.abs(l).max()),          'r_pk':  db(np.abs(r).max()),
    }


def sweep_response(sig, sr, a, b, win_s=0.050, hop_s=0.005):
    """Track a glide sweep: returns (freq_Hz, level_dBFS_rms) per frame.

    Level comes from the RMS of each short window, NOT from the FFT bin
    magnitude. That distinction matters: a logarithmic sweep spends
    progressively less time per bin as it rises, so the FFT peak falls
    at roughly 3 dB per octave even through a perfectly flat system.
    Measuring the envelope instead is immune to sweep rate, because at
    any instant the signal is a single tone and its RMS is its
    amplitude. The FFT is used only to say WHICH frequency, never how
    loud."""
    s_ = sig[int(a * sr):int(b * sr)]
    n = int(sr * win_s)
    hop = int(sr * hop_s)
    if len(s_) < n * 2:
        return np.array([]), np.array([])
    win = np.hanning(n)
    fs, ls = [], []
    for i in range(0, len(s_) - n, hop):
        fr = s_[i:i + n]
        rms = np.sqrt((fr ** 2).mean())
        if rms < 1e-6:
            continue
        # (level is measured coherently further down, once the
        # frequency is known - see the note there)
        sp = np.abs(np.fft.rfft(fr * win))
        k = sp.argmax()
        if k >= 2 and k < len(sp) - 1:
            al, be, ga = sp[k - 1], sp[k], sp[k + 1]
            d = 0.5 * (al - ga) / (al - 2 * be + ga + 1e-20)
            f_est = (k + d) * sr / n
        else:
            f_est = 0.0
        # Below a couple of bins the FFT cannot resolve the tone at all,
        # and the sweep starts at 20 Hz where a 50 ms window is only one
        # bin wide. Zero crossings give an exact answer for a single
        # tone at any frequency, so they take over at the bottom end.
        if f_est < 4 * sr / n:
            fr0 = fr - fr.mean()
            zc = np.where(np.diff(np.signbit(fr0)))[0]
            if len(zc) >= 3:
                f_est = (len(zc) - 1) * sr / (2.0 * (zc[-1] - zc[0]))
            else:
                continue
        if not (10.0 <= f_est <= sr / 2):
            continue
        # Level by HANN-WEIGHTED RMS, not plain RMS of the window.
        #
        # Plain RMS is wrong by a few tenths of a dB when the window
        # holds a non-integer number of cycles - a 50 ms window is
        # exactly one cycle at 20 Hz, so the bottom two octaves droop
        # on a source known to be flat. Tapering with the same Hann
        # window used for the FFT kills that, because the partial cycle
        # sits where the window is near zero.
        #
        # Coherent detection against the estimated frequency would be
        # exact for a steady tone and was tried first, but it is wrong
        # here: the signal is a chirp that moves during the window, so
        # correlating against a single frequency loses amplitude - and
        # loses more at HF where the sweep covers more Hz per window.
        # Weighted RMS makes no assumption about frequency at all,
        # which is what a swept measurement needs.
        wrms = np.sqrt((win * fr * fr).sum() / win.sum())
        if wrms < 1e-6:
            continue
        fs.append(f_est)
        ls.append(db(wrms))
    return np.array(fs), np.array(ls)


def _merge_response(curves, ref_hz=1000.0, nbins=180, min_pts=3,
                    dropout_db=30.0):
    """Combine sweeps onto log-spaced bins, taking the median where they
    overlap, then normalise to the level at ref_hz.

    Three defences, all learned from a real off-air capture that came
    back with 60 dB notches the transmitter had never produced:

    min_pts - a logarithmic sweep dwells about the same time in every
    log bin, which at a 5 ms hop is only a handful of frames. Accepting
    a bin built from ONE frame means a single glitched frame becomes a
    deep narrow notch, and because the channels are binned separately
    they notch at different frequencies - which is what gave the game
    away.

    dropout_db - a frame far below the sweep's own median level is a
    dropout in the path or the capture, not a response feature. No
    repeater audio chain has a 60 dB notch a few hundred Hz wide; a
    buffer underrun looks exactly like one.

    A three-bin median at the end removes any single-bin spike that
    still survives both."""
    if not curves:
        return np.array([]), np.array([])
    f = np.concatenate([c[0] for c in curves])
    l = np.concatenate([c[1] for c in curves])
    keep = (f >= 18) & (f <= 21000) & np.isfinite(l) & np.isfinite(f)
    f, l = f[keep], l[keep]
    if len(f) < 12:
        return np.array([]), np.array([])

    med = np.median(l)
    ok = l > med - dropout_db
    f, l = f[ok], l[ok]
    if len(f) < 12:
        return np.array([]), np.array([])

    edges = np.logspace(np.log10(18), np.log10(21000), nbins + 1)
    idx = np.digitize(f, edges) - 1
    fo, lo = [], []
    for b in range(nbins):
        m = idx == b
        if m.sum() >= min_pts:
            fo.append(np.sqrt(edges[b] * edges[b + 1]))
            lo.append(np.median(l[m]))
    if len(fo) < 8:
        return np.array([]), np.array([])
    fo, lo = np.array(fo), np.array(lo)

    if len(lo) >= 3:
        pad = np.concatenate([lo[:1], lo, lo[-1:]])
        lo = np.array([np.median(pad[i:i + 3]) for i in range(len(lo))])

    ref = np.interp(ref_hz, fo, lo)
    return fo, lo - ref


def thd_at(sig, sr, a, b, f0, nharm=5):
    """THD from a spot tone. Not meaningful through a perceptual codec,
    where noise is deliberately added under the masking threshold, but
    entirely meaningful on an analogue path."""
    s = sig[int(a * sr):int(b * sr)]
    n = 1 << 15
    if len(s) < n:
        return None
    s = s[:n] * np.hanning(n)
    sp = np.abs(np.fft.rfft(s))
    fr = np.fft.rfftfreq(n, 1 / sr)
    def peak(f):
        k = np.argmin(np.abs(fr - f))
        return sp[max(0, k - 3):k + 4].max()
    fund = peak(f0)
    if fund <= 0:
        return None
    h = np.sqrt(sum(peak(f0 * i) ** 2 for i in range(2, nharm + 1)))
    return 100.0 * h / fund


def analyse(L, R, sr):
    """Run the whole thing. Returns a dict of results plus the response
    curves, ready to hand to the renderer."""
    mono = (L + R) / 2
    hdrs = find_headers(mono, sr)
    res = {'headers': len(hdrs), 'segments': [], 'notes': []}
    if not hdrs:
        res['notes'].append('No Lindos header found')
        return res

    # A segment's content is the audio between its own header and the
    # NEXT header. The final header has no successor to bound it, and
    # running it to the end of the buffer is wrong in a way that is hard
    # to spot: capture deliberately continues for END_GAP_S after the
    # last header, so the tail is a dozen seconds of dead air. Measured
    # as a segment it lands under -60 dBFS, is classified 'noise', and
    # then wins the min() in the noise calculation - so the S/N figure
    # ends up describing the silence after the sequence rather than the
    # sequence's own noise segments. A lone header with nothing after it
    # produces a card reading PASS off the back of it.
    #
    # Bound the last segment by how long the other segments actually ran
    # instead, and if there are no others to learn that from, there is no
    # trustworthy content and it is dropped.
    bounds = []
    spans = [hdrs[i + 1][0] - hdrs[i][1] for i in range(len(hdrs) - 1)]
    typical = float(np.median(spans)) if spans else None
    for i, (a, b) in enumerate(hdrs):
        if i + 1 < len(hdrs):
            nxt = hdrs[i + 1][0]
        elif typical is not None:
            nxt = min(b + typical, len(mono) / sr)
        else:
            continue
        if nxt - b < 0.10:
            continue
        bounds.append((a, b, b + 0.03, nxt - 0.03))

    # ---- classify each segment from the audio that follows it -------
    for a, b, ca, cb in bounds:
        lv = seg_levels(L, R, sr, ca, cb)
        if lv is None:
            continue
        kind = 'unknown'
        span = cb - ca
        loud = max(lv['l_rms'], lv['r_rms'])
        imbal = abs(lv['l_rms'] - lv['r_rms'])
        if loud < -60:
            kind = 'noise'
        elif imbal > 25:
            kind = 'crosstalk'
        elif span > 1.2:
            kind = 'sweep'
        else:
            kind = 'tone'
        res['segments'].append({'hdr': (a, b), 'content': (ca, cb),
                                'kind': kind, **lv,
                                'fingerprint': header_bits(mono, sr, a, b)})

    S = res['segments']

    # ---- level, from the first steady tone --------------------------
    tone = next((s for s in S if s['kind'] == 'tone'), None)
    if tone:
        res['level_l_ppm'] = ppm_from_peak_dbfs(tone['l_pk'])
        res['level_r_ppm'] = ppm_from_peak_dbfs(tone['r_pk'])
        res['level_err_l'] = tone['l_pk'] - ALIGN_PEAK_DBFS
        res['level_err_r'] = tone['r_pk'] - ALIGN_PEAK_DBFS
        res['balance'] = tone['l_rms'] - tone['r_rms']
        t = tone['content']
        res['thd_l'] = thd_at(L, sr, t[0], t[1], 1000.0)
        res['thd_r'] = thd_at(R, sr, t[0], t[1], 1000.0)

    # ---- frequency response ------------------------------------------
    # Only sweeps at REFERENCE level are used. The sequence also
    # contains a sweep 8 dB hot for distortion measurement (PPM6) and
    # others for phase; mixing those in produces a curve that is part
    # response and part level difference, which is worse than useless
    # because it looks plausible.
    ref_rms = tone['l_rms'] if tone else None
    cl, crr = [], []
    for s_ in S:
        if s_['kind'] != 'sweep':
            continue
        if ref_rms is not None and abs(s_['l_rms'] - ref_rms) > 2.0:
            continue
        ca, cb = s_['content']
        cl.append(sweep_response(L, sr, ca, cb))
        crr.append(sweep_response(R, sr, ca, cb))
    fl, ll = _merge_response(cl)
    fr_, lr = _merge_response(crr)
    if len(fl): res['resp_l'] = (fl, ll)
    if len(fr_): res['resp_r'] = (fr_, lr)

    # ---- noise ------------------------------------------------------
    ns = [s for s in S if s['kind'] == 'noise']
    if ns:
        res['noise_l'] = min(s['l_rms'] for s in ns)
        res['noise_r'] = min(s['r_rms'] for s in ns)

    # ---- crosstalk --------------------------------------------------
    xs = [s for s in S if s['kind'] == 'crosstalk']
    if xs:
        seps = []
        for s in xs:
            hi, lo = max(s['l_rms'], s['r_rms']), min(s['l_rms'], s['r_rms'])
            seps.append(hi - lo)
        res['separation'] = min(seps)
        res['separation_n'] = len(seps)

    return res


# ------------------------------------------------------------- display

import math
try:
    import cairo
except ImportError:
    cairo = None

INK        = (0.043, 0.051, 0.078)
PANEL      = (0.078, 0.094, 0.137)
GRID       = (0.180, 0.220, 0.290)
GRID_MAJOR = (0.250, 0.330, 0.420)
COAST      = (0.306, 0.486, 0.600)
CYAN       = (0.000, 0.863, 0.922)
AMBER      = (1.000, 0.647, 0.157)
MAGENTA    = (1.000, 0.314, 0.745)
GREEN      = (0.180, 0.820, 0.500)
RED        = (0.949, 0.271, 0.376)
TEXT_BRIGHT= (1.000, 1.000, 1.000)
TEXT_DIM   = (0.561, 0.651, 0.737)
TEXT_LABEL = (0.431, 0.514, 0.600)

BAND_TOP, BAND_BOTTOM = 0.155, 0.205   # deeper footer than Pathfinder,
                                       # two rows of figures rather than one

F_MIN, F_MAX = 20.0, 20000.0

# Default tolerances. Deliberately generous - a repeater audio chain is
# not a mastering suite, and a card that cries wolf gets ignored.
TOL = {'level_db': 1.0, 'balance_db': 1.0, 'resp_db': 3.0,
       'noise_dbfs': -60.0, 'sep_db': 30.0, 'thd_pct': 1.0}


def _fx(f, x0, w):
    f = min(max(f, F_MIN), F_MAX)
    return x0 + w * (math.log10(f) - math.log10(F_MIN)) / \
        (math.log10(F_MAX) - math.log10(F_MIN))


def _txt(cr, x, y, s, size, col, bold=False, mono=True, centre=False, right=False):
    cr.select_font_face("monospace" if mono else "sans-serif",
                        cairo.FONT_SLANT_NORMAL,
                        cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    if centre or right:
        te = cr.text_extents(s)
        x -= te.width / 2 if centre else te.width
    cr.set_source_rgb(*col)
    cr.move_to(x, y)
    cr.show_text(s)
    cr.new_path()


def render_card(res, W=1920, H=1080, seq_name='', duration_s=None, tol=None):
    """Auto-Squeak results card, drawn in the Pathfinder idiom so the
    two feel like one product. No station identification along the top:
    this is a measurement of the path, not of a station, and the space
    is better spent on figures."""
    if cairo is None:
        raise RuntimeError('pycairo not available')
    t = dict(TOL); t.update(tol or {})

    # The card reaches the overlay via /api/status, so the response
    # curves arrive as plain lists rather than the numpy arrays the
    # analyser produced. Normalise here rather than making every reader
    # care which side of the JSON boundary it is on.
    res = dict(res)
    for k in ('resp_l', 'resp_r'):
        if k in res and res[k] is not None:
            f, l = res[k]
            res[k] = (np.asarray(f, dtype=float), np.asarray(l, dtype=float))

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    cr.set_source_rgb(*INK); cr.paint()

    top, bot = H * BAND_TOP, H * (1 - BAND_BOTTOM)

    # ---- plot area ---------------------------------------------------
    m_l, m_r = W * 0.055, W * 0.03
    x0, pw = m_l, W - m_l - m_r
    y0, ph = top + H * 0.055, (bot - top) - H * 0.085

    cr.set_source_rgb(*PANEL)
    cr.rectangle(x0, y0, pw, ph); cr.fill()

    span = t['resp_db'] * 2.5
    def yv(d):
        return y0 + ph / 2 - (d / span) * ph

    # tolerance band, drawn first so the traces sit over it
    cr.set_source_rgba(*GREEN, 0.09)
    cr.rectangle(x0, yv(t['resp_db']), pw, yv(-t['resp_db']) - yv(t['resp_db']))
    cr.fill()

    for d in range(-int(span // 2), int(span // 2) + 1):
        if d % 3 and abs(d) != int(t['resp_db']):
            continue
        major = (d == 0)
        cr.set_source_rgba(*(GRID_MAJOR if major else GRID), 1.0 if major else 0.75)
        cr.set_line_width(1.6 if major else 0.9)
        if abs(d) == int(t['resp_db']) and not major:
            cr.set_dash([5, 5])
        cr.move_to(x0, yv(d)); cr.line_to(x0 + pw, yv(d)); cr.stroke()
        cr.set_dash([])
        _txt(cr, x0 - 10, yv(d) + 5, f'{d:+d}' if d else '0',
             H * 0.016, TEXT_LABEL if not major else TEXT_DIM, right=True)

    for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
        x = _fx(f, x0, pw)
        cr.set_source_rgba(*GRID, 0.8); cr.set_line_width(0.9)
        cr.move_to(x, y0); cr.line_to(x, y0 + ph); cr.stroke()
        lab = f'{f // 1000}k' if f >= 1000 else str(f)
        _txt(cr, x, y0 + ph + H * 0.027, lab, H * 0.016, TEXT_LABEL, centre=True)

    _txt(cr, x0 - 10, y0 - H * 0.012, 'dB', H * 0.015, TEXT_LABEL, right=True)

    # ---- response traces --------------------------------------------
    def trace(key, col, dash=None, lw=2.2):
        if key not in res:
            return
        f, l = res[key]
        if not len(f):
            return
        pts = [(_fx(a, x0, pw), yv(max(-span / 2, min(span / 2, b))))
               for a, b in zip(f, l) if F_MIN <= a <= F_MAX]
        if len(pts) < 2:
            return
        cr.set_source_rgba(*col, 0.95)
        cr.set_line_width(max(lw, H * 0.0011 * lw))
        cr.set_dash(dash or [])
        cr.move_to(*pts[0])
        for p in pts[1:]:
            cr.line_to(*p)
        cr.stroke()
        cr.set_dash([])

    # Right is dashed and drawn over Left. On a healthy stereo path the
    # two curves lie on top of one another, and a solid trace would
    # simply hide the one beneath - leaving the viewer unable to tell
    # "both channels identical" from "one channel missing".
    trace('resp_l', CYAN, lw=3.0)
    trace('resp_r', AMBER, dash=[9, 7], lw=2.0)

    lx = x0 + pw - W * 0.075
    for i, (lab, col) in enumerate((('LEFT', CYAN), ('RIGHT', AMBER))):
        yy = y0 + H * 0.030 + i * H * 0.030
        cr.set_source_rgb(*col)
        if i:
            cr.set_dash([5, 4]); cr.set_line_width(H * 0.005)
            cr.move_to(lx, yy - H * 0.0055)
            cr.line_to(lx + W * 0.017, yy - H * 0.0055); cr.stroke()
            cr.set_dash([])
        else:
            cr.rectangle(lx, yy - H * 0.008, W * 0.017, H * 0.005); cr.fill()
        _txt(cr, lx + W * 0.022, yy, lab, H * 0.017, TEXT_DIM, bold=True)

    # ---- header ------------------------------------------------------
    cr.set_source_rgba(0.027, 0.035, 0.055, 0.96)
    cr.rectangle(0, 0, W, top); cr.fill()
    _txt(cr, W * 0.030, H * 0.062, 'AUTO-SQUEAK', H * 0.046,
         TEXT_BRIGHT, bold=True, mono=False)
    sub = seq_name or 'Lindos sequence'
    if duration_s:
        # Segments MEASURED, not headers seen. These differ - the final
        # header is dropped when nothing trustworthy follows it - and
        # this figure is what tells the viewer whether the card saw the
        # whole run, so it has to be the honest one.
        nseg = len(res.get('segments') or []) or res.get('headers', 0)
        sub += f'  \u00b7  {duration_s:.0f} s  \u00b7  {nseg} segments'
    _txt(cr, W * 0.031, H * 0.100, sub, H * 0.021, TEXT_DIM, mono=False)

    fails = _failures(res, t)
    ok = not fails
    _txt(cr, W - W * 0.030, H * 0.058, 'PASS' if ok else 'CHECK',
         H * 0.040, GREEN if ok else AMBER, bold=True, mono=False, right=True)
    _txt(cr, W - W * 0.031, H * 0.098,
         'all within tolerance' if ok else ', '.join(fails),
         H * 0.019, TEXT_DIM, mono=False, right=True)

    # ---- footer figures ---------------------------------------------
    cr.set_source_rgba(0.027, 0.035, 0.055, 0.96)
    cr.rectangle(0, H - H * BAND_BOTTOM, W, H * BAND_BOTTOM); cr.fill()

    # Two rows of five. One row crowded the figures into each other,
    # and a second row leaves space for further measurements - phase,
    # wow and flutter - without redesigning the card.
    cells = _cells(res, t)
    per_row = 5
    cw = (W - W * 0.06) / per_row
    for i, (lab, val, col) in enumerate(cells[:per_row * 2]):
        cx = W * 0.030 + (i % per_row) * cw
        cy = H - H * (0.125 if i < per_row else 0.055)
        _txt(cr, cx, cy, lab, H * 0.0155, TEXT_LABEL)
        _txt(cr, cx, cy + H * 0.036, val, H * 0.028, col, bold=True)

    _txt(cr, W - W * 0.030, H - H * 0.016,
         'Levels referenced to alignment, PPM4 = \u221218 dBFS peak',
         H * 0.015, TEXT_LABEL, right=True)
    return surf


def _failures(res, t):
    # An absence of faults is only a PASS if something was actually
    # measured. Every test below is conditional on its measurement being
    # present, so a result carrying none at all falls through the lot and
    # renders as PASS - confidently, over a blank card. Say so instead.
    measured = ('level_err_l', 'resp_l', 'resp_r', 'noise_l',
                'separation', 'thd_l')
    if not any(res.get(k) is not None for k in measured):
        return ['no measurements']

    f = []
    if 'level_err_l' in res and max(abs(res['level_err_l']),
                                    abs(res['level_err_r'])) > t['level_db']:
        f.append('level')
    if 'balance' in res and abs(res['balance']) > t['balance_db']:
        f.append('balance')
    for k in ('resp_l', 'resp_r'):
        if k in res and res[k] is not None and len(res[k][1]):
            fa = np.asarray(res[k][0], dtype=float)
            la = np.asarray(res[k][1], dtype=float)
            band = la[(fa >= 40) & (fa <= 15000)]
            if len(band) and np.abs(band).max() > t['resp_db']:
                f.append('response'); break
    if 'noise_l' in res and max(res['noise_l'], res['noise_r']) > t['noise_dbfs']:
        f.append('noise')
    if 'separation' in res and res['separation'] < t['sep_db']:
        f.append('separation')
    if res.get('thd_l') is not None and \
            max(res['thd_l'], res['thd_r']) > t['thd_pct']:
        f.append('THD')
    return f


def _cells(res, t):
    out = []
    def col(ok):
        return TEXT_BRIGHT if ok else AMBER

    if 'level_l_ppm' in res:
        e = max(abs(res['level_err_l']), abs(res['level_err_r']))
        out.append(('LEVEL  L / R  (PPM)',
                    f"{res['level_l_ppm']:.1f} / {res['level_r_ppm']:.1f}",
                    col(e <= t['level_db'])))
        out.append(('LEVEL ERROR', f"{res['level_err_l']:+.2f} dB",
                    col(e <= t['level_db'])))
        out.append(('BALANCE', f"{res['balance']:+.2f} dB",
                    col(abs(res['balance']) <= t['balance_db'])))
    if res.get('resp_l') is not None and len(res['resp_l'][1]):
        f_ = np.asarray(res['resp_l'][0], dtype=float)
        l_ = np.asarray(res['resp_l'][1], dtype=float)
        band = l_[(f_ >= 40) & (f_ <= 15000)]
        dev = np.abs(band).max() if len(band) else 0.0
        out.append(('RESPONSE 40Hz-15k', f'\u00b1{dev:.1f} dB',
                    col(dev <= t['resp_db'])))
    if 'noise_l' in res:
        wv = max(res['noise_l'], res['noise_r'])
        out.append(('NOISE', f'{wv:.0f} dBFS', col(wv <= t['noise_dbfs'])))
    if 'separation' in res:
        out.append(('SEPARATION', f"{res['separation']:.0f} dB",
                    col(res['separation'] >= t['sep_db'])))
    if res.get('thd_l') is not None:
        v = max(res['thd_l'], res['thd_r'])
        out.append(('THD @ 1kHz', f'{v:.3f} %', col(v <= t['thd_pct'])))
    return out


# ------------------------------------------------------------ listener

import os, subprocess, threading, time as _time, shutil

# Gap after the last header before a pass is considered finished.
# MUST exceed the longest silence WITHIN a sequence: the noise segments
# (N and L) are eight seconds of near-silence each, so anything based
# on "audio stopped" would cut the sequence in half. Header timing is
# the only reliable end marker.
END_GAP_S = 12.0
MAX_PASS_S = 200.0

# Two separate gates, because they answer different questions.
#
# MIN_HEADER_CHUNKS decides whether to OPEN a capture, and is measured in
# 100 ms frames of FSK-band energy - so four frames is 400 ms, not four
# headers. It is deliberately loose: it runs continuously and only has to
# be cheap, and being wrong here costs nothing but a discarded buffer.
#
# MIN_SEGMENTS decides whether to PUBLISH a result, and is measured in
# complete segments found by find_headers(). This is the gate that
# matters. Programme audio can easily hold enough energy in the FSK band
# to open a capture - music does it regularly - but it never survives
# find_headers(), so it arrives here with zero segments.
MIN_HEADER_CHUNKS = 4
MIN_SEGMENTS = 1

# A header should arrive at alignment (PPM4). Much below that and the
# path is broken or the sender is badly misaligned - worth saying so
# rather than silently failing to sync.
HDR_LOW_WARN_DB = 20.0


class SqueakListener(threading.Thread):
    """Listens continuously, captures a whole Lindos pass, measures it,
    and hands the result to a callback.

    Designed for repeated passes: the source plays the sequence over
    and over, and a fresh card appears after each one. That is what
    makes it useful for alignment - make an adjustment, wait for the
    next pass, see whether it helped, rather than re-running a file by
    hand every time.

    Audio is captured as int16 rather than float. A 200 second pass at
    48 kHz stereo is 38 MB that way and 77 MB as float32, and the
    source is 16-bit anyway, so the extra precision would be invented."""

    DEFAULT_SOURCE = '@DEFAULT_MONITOR@'

    def __init__(self, source=None, on_result=None, sr=48000,
                 chunk_s=0.10):
        super().__init__(daemon=True)
        # PulseAudio's own alias for "the monitor of whatever sink is
        # currently the default", which PipeWire's compatibility layer
        # honours. Better than a hard-coded node name: it needs no
        # configuration, and it follows automatically if the output
        # device is changed later. A real name can still be given for
        # a site with several sinks where the default is not the one
        # carrying programme audio.
        self.source = source or self.DEFAULT_SOURCE
        self.sr = sr
        self.on_result = on_result
        self.chunk = int(sr * chunk_s)
        self.running = False
        self._proc = None
        self.last_error = ''
        self.state = 'idle'
        self.headers_seen = 0

    # -- header presence on a short frame ------------------------------
    def _is_header(self, mono):
        n = len(mono)
        if n < 256:
            return False, 0.0
        w = np.hanning(n)
        sp = np.abs(np.fft.rfft(mono * w))
        fr = np.fft.rfftfreq(n, 1 / self.sr)
        band = (fr > 1580) & (fr < 1950)
        tot = (sp ** 2).sum() + 1e-20
        frac = (sp[band] ** 2).sum() / tot
        pk = db(np.abs(mono).max())
        return frac > 0.55, pk

    def stop(self):
        self.running = False
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _reap(self):
        """Make sure the previous pw-cat is genuinely gone.

        run() below restarts _capture() on ANY exception, and
        'audio stream ended' is one this class raises itself - a normal
        path, not an edge case. Without this, each restart simply
        overwrote self._proc and abandoned the old process: zombies at
        best, and at worst a second live capture competing for the same
        audio device every three seconds."""
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            pass
        for stream in (p.stdout, p.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass

    def run(self):
        self.running = True
        while self.running:
            try:
                self._capture()
            except Exception as e:
                self.last_error = str(e)
                print(f"[squeak] listener error: {e}")
            finally:
                self._reap()
            if self.running:
                _time.sleep(3.0)

    def _capture(self):
        if not shutil.which('pw-cat'):
            raise RuntimeError('pw-cat not found (is pipewire-utils installed?)')
        # -fragment_size and PIPEWIRE_LATENCY both ask for a RELAXED
        # capture, and they are the point of this whole block.
        #
        # A capture client's latency request drives the entire PipeWire
        # graph, not just its own stream: ask for small periods and the
        # server speeds everything up to serve them, mpv's playback
        # included. On a Pi already decoding video that is enough to
        # produce under-runs, which is heard as stuttering audio and
        # stalled video - confirmed in the field, where disabling
        # Auto-Squeak cured a receiver that had been stuttering for days
        # while its RF was provably perfect.
        #
        # Nothing here needs low latency. The sequence is measured after
        # the fact, from a buffer, and a card appears seconds later
        # regardless. Asking to be served in large lazy chunks costs
        # nothing and takes the capture out of the critical path.
        # Captured with pw-cat, IDENTICAL to how the PPM meter has read
        # this same monitor all along - same program, same flags, no
        # extra environment.
        #
        # Asking ffmpeg's "-f pulse" input for large fragments was the
        # obvious fix for the above and did not work, because it treats
        # the symptom from inside the problem: "-f pulse" is not a
        # PipeWire client at all, it connects through the PulseAudio
        # compatibility layer, and the negotiation that sets the graph
        # quantum happens there regardless of what is asked for further
        # up. pw-cat speaks to PipeWire natively and never enters that
        # negotiation - which is precisely why the PPM, doing exactly
        # this, has never disturbed anything.
        #
        # Deliberately no PIPEWIRE_LATENCY here either. Requesting a
        # large quantum is plausible and probably harmless, but it is
        # reasoning rather than evidence, and it would make this tap
        # subtly different from the one already proven on this monitor.
        # If a second capture ever does disturb the graph, that is worth
        # finding out against the known-good method, not a variant.
        cmd = ['pw-cat', '-r', f'--target={self.source}',
               '--format=s16', '--rate', str(self.sr),
               '--channels', '2', '-']
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,
                                      bufsize=self.chunk * 8)
        print(f"[squeak] listening on {self.source}")
        nbytes = self.chunk * 2 * 2
        buf = []              # captured chunks during a pass
        t = 0.0
        t_start = None
        t_last_hdr = None
        hdr_count = 0
        low_warned = False

        while self.running:
            raw = self._proc.stdout.read(nbytes)
            if not raw or len(raw) < nbytes:
                raise RuntimeError('audio stream ended')
            a = np.frombuffer(raw, dtype='<i2').reshape(-1, 2)
            mono = a.mean(1).astype(np.float64) / 32768.0
            is_hdr, pk = self._is_header(mono)
            t += self.chunk / self.sr

            if is_hdr:
                if t_start is None:
                    t_start = t
                    buf = []
                    hdr_count = 0
                    low_warned = False
                    self.state = 'capturing'
                    print("[squeak] pass started")
                t_last_hdr = t
                hdr_count += 1
                if not low_warned and pk < ALIGN_PEAK_DBFS - HDR_LOW_WARN_DB:
                    low_warned = True
                    print(f"[squeak] header is {ALIGN_PEAK_DBFS - pk:.0f} dB "
                          f"below alignment - path or sender level suspect")

            if t_start is not None:
                buf.append(a.copy())
                over = (t - t_start) > MAX_PASS_S
                done = t_last_hdr is not None and (t - t_last_hdr) > END_GAP_S
                if done or over:
                    self.state = 'measuring'
                    self._finish(buf, hdr_count, over)
                    buf = []
                    t_start = t_last_hdr = None
                    self.state = 'idle'

    def _finish(self, chunks, hdr_count, truncated):
        if hdr_count < MIN_HEADER_CHUNKS:
            print(f"[squeak] only {hdr_count} header frames - ignoring")
            return
        x = np.concatenate(chunks).astype(np.float64) / 32768.0
        dur = len(x) / self.sr
        print(f"[squeak] pass complete, {dur:.0f}s - measuring")
        try:
            res = analyse(x[:, 0], x[:, 1], self.sr)
        except Exception as e:
            print(f"[squeak] measurement failed: {e}")
            return

        # A capture that yields no segments is not a short run - it is
        # programme audio that happened to trip the opening detector.
        # Publishing it puts up an empty card, and an empty card reads
        # PASS, because _failures() finds no measurement to fault.
        # Count segments that were actually measured, not headers that
        # were detected. A header on its own yields no usable content.
        nseg = len(res.get('segments') or [])
        if nseg < MIN_SEGMENTS:
            print(f"[squeak] {nseg} segments, need {MIN_SEGMENTS} - "
                  f"not a Lindos pass, ignoring")
            return

        res['duration_s'] = dur
        res['truncated'] = truncated
        self.headers_seen = res.get('headers', 0)
        fails = _failures(res, TOL)
        print(f"[squeak] {res.get('headers',0)} segments, "
              f"{'PASS' if not fails else 'CHECK: ' + ', '.join(fails)}")
        if self.on_result:
            try:
                self.on_result(res)
            except Exception as e:
                print(f"[squeak] result callback failed: {e}")


class SqueakTracker:
    """Holds the most recent result for the OSD, in the same shape as
    PathfinderTracker so the overlay treats them alike. A card stays up
    for hold_secs, then clears; the next pass replaces it."""

    def __init__(self, hold_secs=30.0, enabled=True):
        self.hold_secs = float(hold_secs)
        self.enabled = bool(enabled)
        self._card = None
        self._at = 0.0
        self._lock = threading.Lock()
        self.listener = None      # set by the app, so busy() can see it

    def busy(self):
        """True while a card is showing OR a measurement is still on its
        way. The second half matters: Auto-Squeak only finalises about
        twelve seconds after the last header, whereas Pathfinder arms a
        few seconds after unlock - so at the end of a test transmission
        Pathfinder would otherwise appear first and then be shoved
        aside mid-display by the squeak card. Anything wanting to queue
        behind Auto-Squeak has to wait for the pending measurement, not
        just for a card that has not appeared yet."""
        if not self.enabled:
            return False
        if self.get_card() is not None:
            return True
        l = self.listener
        return bool(l and getattr(l, 'state', 'idle') in ('capturing', 'measuring'))

    def on_result(self, res):
        if not self.enabled:
            return
        with self._lock:
            self._card = res
            self._at = _time.time()

    def get_card(self):
        with self._lock:
            if not self._card:
                return None
            if _time.time() - self._at > self.hold_secs:
                self._card = None
                return None
            return self._card

    def clear(self):
        with self._lock:
            self._card = None


def analyse_wav(path):
    """Offline helper - run the analyser over a recording. Useful for
    checking a captured off-air pass without any of the live plumbing."""
    import scipy.io.wavfile as wav
    sr, x = wav.read(path)
    x = x.astype(np.float64)
    if x.ndim == 1:
        x = np.column_stack([x, x])
    if x.dtype.kind == 'i' or np.abs(x).max() > 1.5:
        x = x / 32768.0
    return analyse(x[:, 0], x[:, 1], sr), sr, len(x) / sr
