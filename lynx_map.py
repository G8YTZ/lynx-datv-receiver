#!/usr/bin/env python3
"""
Lynx station map — the end-of-contact card.

Draws a full-screen 16:9 card showing where a station was, the path
back to this receiver, and the signal figures from the contact that
just ended. It replaces the idle logo screen for a configurable window
after the station stops transmitting, so it never covers live video.

WHY pyshp AND CAIRO, NOT geopandas/matplotlib
---------------------------------------------
The prototype for this was written with geopandas and matplotlib, which
between them drag in GEOS, PROJ, pandas and NumPy. That is a lot of
install for a Pi that is already doing software video decode, and it is
not how anything else in Lynx draws. pyshp is 74KB of pure Python and
Cairo is already a hard dependency of the overlay, so this module adds
essentially nothing to the install.

DATA
----
Everything is pre-clipped to a 1200km radius around the UK and stored
in geo/ — see geo/README.md. That radius comfortably covers northern
Italy (~865km), northern Spain (~925km), Berlin, Copenhagen and Vienna,
while keeping the whole dataset under 6MB. Natural Earth is public
domain; the towns list is GeoNames, CC BY, hence the attribution line
drawn on the card.

RESOLUTION NOTE
---------------
Shaded relief was considered and rejected. Natural Earth's best raster
manages about 0.9 pixels per km at our latitude; a 180km card needs
roughly 10. Real terrain would mean SRTM or Copernicus DEM data —
gigabytes, for something on screen for thirty seconds. Vector data
stays sharp at every span instead, and label density does the work of
conveying scale: close cards name market towns, distant ones name only
capitals.
"""

import csv
import math
import os
import struct
import time

import cairo

try:
    import shapefile  # pyshp
except ImportError:  # pragma: no cover - surfaced clearly at startup instead
    shapefile = None

GEO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geo")

# ---------------------------------------------------------------- palette
# Deliberately dark and desaturated. This sits under a magenta path and
# cyan/amber station markers, and has to stay legible when the display
# is being streamed out at a couple of Mb/s.

SEA = (0.039, 0.078, 0.125)
COAST = (0.306, 0.486, 0.600)
BORDER = (0.357, 0.431, 0.522)
RIVER = (0.165, 0.361, 0.478)

CYAN = (0.000, 0.863, 0.922)
AMBER = (1.000, 0.647, 0.157)
PATH = (1.000, 0.314, 0.745)

TEXT_BRIGHT = (1.000, 1.000, 1.000)
TEXT_DIM = (0.561, 0.651, 0.737)
TEXT_LABEL = (0.431, 0.514, 0.600)

# Per-country tints, ISO A2. Anything not listed falls back to
# DEFAULT_LAND, so an unexpected country still renders sensibly rather
# than disappearing into the sea colour.
COUNTRY_COLOURS = {
    'GB': '#1B2C3C', 'IE': '#17322C', 'FR': '#2C2439', 'BE': '#18353A',
    'NL': '#3A2E1E', 'DE': '#1D3224', 'DK': '#35262E', 'LU': '#2F361E',
    'CH': '#372323', 'AT': '#232E3E', 'ES': '#3A2A1C', 'IT': '#1E362C',
    'NO': '#212A40', 'SE': '#283A42', 'PL': '#312335', 'CZ': '#273A36',
    'PT': '#3A2432', 'SK': '#2A3A2A', 'SI': '#243A3A', 'HR': '#332A40',
    'HU': '#3A3220', 'NONE': '#222A35',
}
DEFAULT_LAND = '#222A35'
URBAN_LIGHTEN = 0.10

# Height of the header and footer text bands, as a fraction of the
# frame. Both the framing maths and _draw_text() use these, so they
# cannot drift apart - an earlier version had the fit target the whole
# frame while the bands covered nearly 30% of it, and a station near
# the edge ended up hidden under the header.
BAND_TOP = 0.155
BAND_BOTTOM = 0.135


# ---------------------------------------------------------------- QO-100
#
# QO-100 / Es'hail-2 sits at 25.9 degrees East on the geostationary
# belt. A contact through it did not travel the great-circle path
# between the two stations, so drawing one is actively misleading - it
# went up 36,000 km and back down. The globe view draws what actually
# happened.
SAT_LON = 25.9
SAT_LAT = 0.0

RE_KM   = 6378.137            # Earth equatorial radius
RGEO_KM = 42164.0             # geostationary orbit radius, from Earth's centre
C_KMS   = 299792.458
GEO_R   = RGEO_KM / RE_KM     # 6.61 Earth radii

# Viewing tilt for the globe. Looking from 12 degrees south of the
# equator puts the sub-satellite point slightly above the disc centre,
# and GEO_R times that offset lands the satellite about 1.38 R above
# centre - just clear of the limb. That means the bird can be drawn at
# its TRUE distance, to scale against the Earth below it, with no
# compression or fudging. Centred exactly on the sub-satellite point
# it would sit dead in front of the planet and be undrawable.
VIEW_LAT = -12.0

# The satellite-only segment of 3cm, from the IARU Region 1 / RSGB
# bandplan. Deliberately the bandplan boundary rather than the
# transponder edges, because that is what makes a frequency test
# sufficient on its own rather than a heuristic: 10475-10500 MHz is
# Amateur Satellite Service ONLY, while terrestrial 10 GHz ATV
# repeaters sit far below at 10065, 10240 and 10425 MHz. Nothing
# terrestrial can legitimately appear in this window. It also covers
# both QO-100 transponders - narrowband at 10489.5-10490.0 and
# wideband (DATV) at 10490.5-10499.5 - without needing to tell them
# apart.
SAT_ONLY_MIN_KHZ = 10_475_000
SAT_ONLY_MAX_KHZ = 10_500_000


def is_qo100(downlink_khz):
    """True if this DOWNLINK frequency is in the satellite-only part of
    3cm, i.e. the contact came via QO-100.

    Tested on the downlink, never on the LNB local oscillator, so a
    9750 Universal LNB, a 9000 QO-100 PLL LNB or anything else all give
    the same answer and there is no list of known LOs to maintain.

    Note the caller must pass the DOWNLINK, not the frequency the
    Picotuner reports - those differ by the LO, and the Picotuner has
    no idea an LNB is in front of it."""
    try:
        f = float(downlink_khz)
    except (TypeError, ValueError):
        return False
    return SAT_ONLY_MIN_KHZ <= f <= SAT_ONLY_MAX_KHZ


def slant_range_km(lat, lon):
    """Straight-line distance from a ground station up to QO-100.

    Cosine rule on the triangle Earth-centre / station / satellite,
    where the central angle is the angle subtended at Earth's centre
    between the station and the sub-satellite point."""
    g = math.acos(max(-1.0, min(1.0,
        math.cos(math.radians(lat)) * math.cos(math.radians(lon - SAT_LON)))))
    return math.sqrt(RGEO_KM ** 2 + RE_KM ** 2
                     - 2 * RGEO_KM * RE_KM * math.cos(g))


def sat_path_delay_ms(lat_h, lon_h, lat_s, lon_s):
    """Propagation delay for the whole path: one station up to the
    satellite and back down to the other. Around 253 ms for a European
    contact - the quarter second everyone notices on QO-100. Computed
    per contact rather than quoted as a constant, because a station in
    South Africa or Brazil is meaningfully different."""
    total = slant_range_km(lat_h, lon_h) + slant_range_km(lat_s, lon_s)
    return total, total / C_KMS * 1000.0


def ortho(lat, lon):
    """Orthographic projection, unit Earth radius, centred on the
    sub-satellite longitude and tilted by VIEW_LAT. Returns
    (x, y, visible) with y already flipped for screen use.

    Far-side points are pushed out onto the limb rather than dropped.
    Breaking the path instead fragments any polygon that spans the
    horizon and loses whole continents - Africa disappeared entirely in
    the first version of this."""
    la, lo = math.radians(lat), math.radians(lon)
    cla, clo = math.radians(VIEW_LAT), math.radians(SAT_LON)
    cosc = (math.sin(cla) * math.sin(la)
            + math.cos(cla) * math.cos(la) * math.cos(lo - clo))
    x = math.cos(la) * math.sin(lo - clo)
    y = (math.cos(cla) * math.sin(la)
         - math.sin(cla) * math.cos(la) * math.cos(lo - clo))
    if cosc < 0.0:
        m = math.hypot(x, y) or 1.0
        return x / m, -y / m, False
    return x, -y, True


def country_of(lat, lon):
    """Country name for a position, or ''.

    Used to label the two ends of a QO-100 path. The bundled towns.csv
    and populated-places layer are both clipped to a 1200 km window
    around the UK, so a station in Brazil or Thailand has no city to
    name - and at globe scale a country is the honest granularity
    anyway."""
    parts_list = _read_shp('ne_110m_admin_0_countries', want_fields=('NAME',))
    for parts, rec in parts_list:
        for ring in parts:
            if not ring:
                continue
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            if not (min(xs) <= lon <= max(xs) and min(ys) <= lat <= max(ys)):
                continue
            inside = False
            j = len(ring) - 1
            for k in range(len(ring)):
                xi, yi = ring[k]
                xj, yj = ring[j]
                if ((yi > lat) != (yj > lat)) and \
                   (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
                    inside = not inside
                j = k
            if inside:
                return str(rec.get('NAME', '') if isinstance(rec, dict) else '')
    return ''


def _hex_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _lighten(rgb, amount):
    return tuple(c + (1.0 - c) * amount for c in rgb)


# ---------------------------------------------------------------- maths

def locator_to_latlon(loc):
    """Maidenhead locator to (lat, lon) at the centre of the square.

    Accepts 4 or 6 characters. Returns None for anything it cannot
    parse rather than raising — a malformed locator from QRZ must skip
    the card, never take the overlay down.
    """
    if not loc:
        return None
    loc = str(loc).strip().upper()
    if len(loc) < 4:
        return None
    try:
        if not ('A' <= loc[0] <= 'R' and 'A' <= loc[1] <= 'R'):
            return None
        if not (loc[2].isdigit() and loc[3].isdigit()):
            return None
        lon = (ord(loc[0]) - 65) * 20 - 180
        lat = (ord(loc[1]) - 65) * 10 - 90
        lon += int(loc[2]) * 2
        lat += int(loc[3]) * 1
        if len(loc) >= 6 and 'A' <= loc[4] <= 'X' and 'A' <= loc[5] <= 'X':
            lon += (ord(loc[4]) - 65) * (2.0 / 24) + (1.0 / 24)
            lat += (ord(loc[5]) - 65) * (1.0 / 24) + (0.5 / 24)
        else:
            lon += 1.0
            lat += 0.5
        return lat, lon
    except (ValueError, IndexError, TypeError):
        return None


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass_point(deg):
    pts = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
           'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    return pts[int((deg + 11.25) % 360 // 22.5)]


def great_circle_points(lat1, lon1, lat2, lon2, n=90):
    """Points along the great circle between two positions, so the path
    bows the way a real signal path does instead of drawing a straight
    line across a projected map."""
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    d = 2 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2 +
        math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d == 0:
        return [(lat1, lon1)]
    out = []
    for i in range(n + 1):
        f = i / n
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
        y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
        z = a * math.sin(p1) + b * math.sin(p2)
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))),
                    math.degrees(math.atan2(y, x))))
    return out


# ---------------------------------------------------------------- data

_CACHE = {}


def _read_shp(name, want_fields=()):
    """Reads a shapefile once and caches the parts we need.

    Returns a list of (parts, field_dict) where parts is a list of
    coordinate rings. Deliberately keeps only what's drawn — the full
    Natural Earth attribute tables are large and none of it is used.
    """
    key = ('shp', name)
    if key in _CACHE:
        return _CACHE[key]
    path = os.path.join(GEO_DIR, name + '.shp')
    out = []
    if shapefile is None or not os.path.exists(path):
        _CACHE[key] = out
        return out
    try:
        sf = shapefile.Reader(path)
        names = [f[0] for f in sf.fields[1:]]
        for sr in sf.iterShapeRecords():
            shp = sr.shape
            pts = shp.points
            if not pts:
                continue
            # Point shapefiles (populated places) carry an empty parts
            # array, so the ring-splitting below yields nothing for them
            # and every major label silently vanished. Treat "no parts"
            # as one part covering all the points.
            raw_parts = list(shp.parts) or [0]
            idx = raw_parts + [len(pts)]
            parts = [pts[idx[i]:idx[i + 1]] for i in range(len(idx) - 1)]
            parts = [p for p in parts if p]
            rec = {}
            for f in want_fields:
                if f in names:
                    rec[f] = sr.record[names.index(f)]
            out.append((parts, rec))
    except Exception as e:
        print(f"[map] could not read {name}: {e}")
        out = []
    _CACHE[key] = out
    return out


def _read_towns():
    """GeoNames towns above 15,000 population.

    Natural Earth's own populated-places layer carries only 13 entries
    across the whole of southern England, which is why an early version
    of this card looked so bare — this adds roughly 360 in the same
    window. There is no population column (the file is already filtered
    by it), so which towns actually get drawn is decided by spatial
    thinning at render time rather than by rank.
    """
    key = ('towns',)
    if key in _CACHE:
        return _CACHE[key]
    path = os.path.join(GEO_DIR, 'towns.csv')
    out = []
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as fh:
                for r in csv.DictReader(fh):
                    try:
                        out.append((r['name'], float(r['lat']), float(r['lng'])))
                    except (ValueError, KeyError):
                        continue
        except Exception as e:
            print(f"[map] could not read towns.csv: {e}")
    _CACHE[key] = out
    return out


def _fix_name(raw):
    """Natural Earth's DBF is UTF-8 but historically shipped without a
    .cpg, so some readers decode it as latin-1 and 'Munster' arrives
    mojibaked. The clipped data in geo/ has a .cpg and should be clean,
    but this repairs it either way rather than putting a mangled name
    on screen."""
    if not isinstance(raw, str):
        return str(raw or '')
    if 'Ã' not in raw and 'Â' not in raw:
        return raw
    try:
        return raw.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


# ---------------------------------------------------------------- render

class MapRenderer:
    """Renders the end-of-contact card to a Cairo ImageSurface.

    Rendering takes a noticeable fraction of a second, so the overlay
    renders once when the card first appears and reuses the surface for
    the rest of its display window rather than redrawing every frame.
    """

    def __init__(self, home_locator, min_span_km=15.0, max_span_km=1000.0,
                 span_factor=1.45):
        self.home_locator = (home_locator or '').upper()
        self.home = locator_to_latlon(self.home_locator)
        self.min_span_km = float(min_span_km)
        self.max_span_km = float(max_span_km)
        self.span_factor = float(span_factor)

    # -- geometry helpers -------------------------------------------------

    def _window(self, lat_s, lon_s, dist_km, W, H):
        """Frame centred on the midpoint of the path, spanning enough to
        hold both stations with margin.

        The span is driven by whichever axis is LIMITING, not by the
        contact distance. Distance alone is wrong for any path that
        isn't roughly east-west: a 275km contact due south spans about
        246km vertically, but a 16:9 frame 275km wide is only ~155km
        tall, so both stations ended up off the top and bottom of the
        map with an empty sea between them. Converting the north-south
        extent into its equivalent width (multiplying by the aspect
        ratio) and taking the larger of the two makes the frame fit the
        path in either orientation.

        The longitude correction matters too: at 51N a degree of
        longitude is only about 0.63 of a degree of latitude, so without
        it the map comes out visibly stretched east-west.
        """
        lat_h, lon_h = self.home
        clat = (lat_h + lat_s) / 2.0
        clon = (lon_h + lon_s) / 2.0

        km_per_deg_lat = 111.32
        km_per_deg_lon = 111.32 * math.cos(math.radians(clat))
        if km_per_deg_lon < 1e-6:
            km_per_deg_lon = 1e-6

        aspect = W / float(H)
        dx_km = abs(lon_s - lon_h) * km_per_deg_lon
        dy_km = abs(lat_s - lat_h) * km_per_deg_lat

        # Only the band between the header and footer is actually
        # visible map, so the vertical fit has to be against that, not
        # against the full frame height.
        usable = 1.0 - BAND_TOP - BAND_BOTTOM

        # Both expressed as the frame WIDTH each would require
        needed = max(dx_km, dy_km * aspect / usable)
        span = needed * self.span_factor

        # min_span_km is a HARD floor - it stops a very local contact
        # zooming into a featureless square. max_span_km is a SOFT
        # ceiling: it stops the frame growing further than necessary,
        # but the fit always wins, because a card with a station off the
        # edge defeats the entire point of drawing one. A 524km path due
        # north needs a ~1900km frame in 16:9 once the text bands are
        # accounted for, and clamping that to 1000km put both markers
        # outside the visible area.
        span = max(self.min_span_km, span)
        if span > self.max_span_km and needed <= self.max_span_km:
            span = self.max_span_km

        half_w = (span / 2.0) / km_per_deg_lon
        half_h = (span / 2.0) / km_per_deg_lat / aspect

        # Nudge the frame so the MIDPOINT of the path sits in the middle
        # of the visible band rather than the middle of the frame - the
        # bands are not equal, so centring on the frame pushes everything
        # slightly too high.
        centre_frac_from_bottom = 1.0 - (BAND_TOP + usable / 2.0)
        clat += (0.5 - centre_frac_from_bottom) * (half_h * 2.0)

        return (clon - half_w, clon + half_w,
                clat - half_h, clat + half_h, span)

    # -- main -------------------------------------------------------------

    def render(self, W, H, callsign, locator, name=None, mer=None,
               modcod=None, symbol_rate=None, frequency=None,
               site_name=None, via_qo100=False):
        """Returns a cairo.ImageSurface, or None if the card cannot be
        drawn (no home locator, unparseable station locator, missing
        data files). Callers must handle None — a missing map should
        simply mean the normal idle screen stays up.

        via_qo100 switches to the globe view. It is passed in rather
        than worked out here because the decision needs the LNB local
        oscillator, and the only frequency this module ever sees is the
        one the Picotuner reports - which is the IF, several GHz below
        the actual downlink. lynx_app.py knows the LO; this does not."""
        if not self.home:
            return None
        if via_qo100:
            return self._render_globe(W, H, callsign, locator, name, mer,
                                      modcod, symbol_rate, frequency,
                                      site_name)
        pos = locator_to_latlon(locator)
        if not pos:
            return None
        lat_s, lon_s = pos
        lat_h, lon_h = self.home

        dist = haversine_km(lat_h, lon_h, lat_s, lon_s)
        brg = bearing_deg(lat_h, lon_h, lat_s, lon_s)
        x0, x1, y0, y1, span = self._window(lat_s, lon_s, dist, W, H)

        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        cr = cairo.Context(surf)

        def sx(lon):
            return (lon - x0) / (x1 - x0) * W

        def sy(lat):
            return H - (lat - y0) / (y1 - y0) * H

        def visible(parts):
            """Does this shape's bounding box overlap the window?

            Deliberately a bounding-box test, not a "does any vertex fall
            inside the window" test. The latter is the obvious thing to
            write and it fails badly at close range: for a local contact
            the window may be only 20-30km across and sit entirely INSIDE
            a country's outline, so not one vertex of that polygon is in
            view and the whole country gets skipped as invisible. The
            result is a card showing town names floating on an empty sea,
            with no land at all - reported from Nordenham at 19km.

            A bounding box that merely overlaps is cheap to compute and
            cannot make that mistake: a polygon enclosing the window has
            a box that contains it.
            """
            pad = 0.5
            for ring in parts:
                if not ring:
                    continue
                xs = [pt[0] for pt in ring]
                ys = [pt[1] for pt in ring]
                if (min(xs) <= x1 + pad and max(xs) >= x0 - pad and
                        min(ys) <= y1 + pad and max(ys) >= y0 - pad):
                    return True
            return False

        def path(parts, close):
            cr.new_path()
            for ring in parts:
                if len(ring) < 2:
                    continue
                cr.move_to(sx(ring[0][0]), sy(ring[0][1]))
                for px, py in ring[1:]:
                    cr.line_to(sx(px), sy(py))
                if close:
                    cr.close_path()

        # sea
        cr.set_source_rgb(*SEA)
        cr.paint()

        # ---- countries, each in its own tint ----
        countries = _read_shp('ne_10m_admin_0_countries', ('ISO_A2_EH', 'ISO_A2'))
        urban = _read_shp('ne_10m_urban_areas')

        drawn_iso = []
        for parts, rec in countries:
            if not visible(parts):
                continue
            iso = str(rec.get('ISO_A2_EH') or rec.get('ISO_A2') or '').strip()
            base = _hex_rgb(COUNTRY_COLOURS.get(iso, DEFAULT_LAND))
            path(parts, True)
            cr.set_source_rgb(*base)
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.fill()
            drawn_iso.append(base)

        # If no country polygons landed in frame (shouldn't happen inside
        # the clipped region, but a badly wrong locator could do it),
        # fall back to the plain land layer so the card isn't all sea.
        if not drawn_iso:
            for parts, _ in _read_shp('ne_10m_land'):
                if not visible(parts):
                    continue
                path(parts, True)
                cr.set_source_rgb(*_hex_rgb(DEFAULT_LAND))
                cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
                cr.fill()

        # Urban areas, as a lighter shade so built-up land reads as part
        # of the country rather than a separate grey layer. One tint for
        # all of them here (working out which country each polygon falls
        # in would mean point-in-polygon tests we have no geometry
        # library for) — the average of what's on screen is close enough.
        if drawn_iso:
            avg = tuple(sum(c[i] for c in drawn_iso) / len(drawn_iso)
                        for i in range(3))
        else:
            avg = _hex_rgb(DEFAULT_LAND)
        urban_rgb = _lighten(avg, URBAN_LIGHTEN)
        cr.set_source_rgb(*urban_rgb)
        for parts, _ in urban:
            if not visible(parts):
                continue
            path(parts, True)
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.fill()

        # rivers
        cr.set_source_rgb(*RIVER)
        cr.set_line_width(1.2)
        for parts, _ in _read_shp('ne_10m_rivers_lake_centerlines'):
            if not visible(parts):
                continue
            path(parts, False)
            cr.stroke()

        # lakes punched back to sea colour
        cr.set_source_rgb(*SEA)
        for parts, _ in _read_shp('ne_10m_lakes'):
            if not visible(parts):
                continue
            path(parts, True)
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.fill()

        # coastline
        cr.set_source_rgb(*COAST)
        cr.set_line_width(1.6)
        for parts, _ in _read_shp('ne_10m_coastline'):
            if not visible(parts):
                continue
            path(parts, False)
            cr.stroke()

        # national borders, dashed
        cr.set_source_rgb(*BORDER)
        cr.set_line_width(1.3)
        cr.set_dash([8, 5])
        for parts, _ in _read_shp('ne_10m_admin_0_boundary_lines_land'):
            if not visible(parts):
                continue
            path(parts, False)
            cr.stroke()
        cr.set_dash([])

        # ---- place labels, two tiers ----
        self._draw_places(cr, W, H, x0, x1, y0, y1, span)

        # ---- great-circle path ----
        gc = great_circle_points(lat_h, lon_h, lat_s, lon_s)
        cr.new_path()
        cr.move_to(sx(gc[0][1]), sy(gc[0][0]))
        for plat, plon in gc[1:]:
            cr.line_to(sx(plon), sy(plat))
        cr.set_source_rgba(*PATH, 0.18)
        cr.set_line_width(9)
        cr.stroke_preserve()
        cr.set_source_rgba(*PATH, 0.9)
        cr.set_line_width(2.6)
        cr.stroke()

        # ---- station markers ----
        for lon_m, lat_m, colour in ((lon_h, lat_h, AMBER), (lon_s, lat_s, CYAN)):
            mx, my = sx(lon_m), sy(lat_m)
            cr.set_source_rgba(*colour, 0.22)
            cr.arc(mx, my, 17, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(*colour)
            cr.arc(mx, my, 8, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(0.02, 0.05, 0.09, 0.9)
            cr.set_line_width(1.8)
            cr.arc(mx, my, 8, 0, 2 * math.pi)
            cr.stroke()

        # ---- text ----
        self._draw_text(cr, W, H, callsign, locator, name, dist, brg,
                        mer, modcod, symbol_rate, frequency, site_name)

        surf.flush()
        return surf

    # -- labels -----------------------------------------------------------

    def _draw_places(self, cr, W, H, x0, x1, y0, y1, span):
        """Two tiers, both spatially thinned.

        Majors come from Natural Earth (ranked, so London and Brussels
        always win their space); minors from the GeoNames set, which is
        far denser. Majors are placed first so the big names claim their
        positions and smaller towns fill whatever is left. Labels are
        kept out of the top and bottom text bands.
        """
        placed = []
        major_size = 21 if span < 350 else (19 if span < 700 else 17)
        minor_size = major_size - 4

        def sx(lon):
            return (lon - x0) / (x1 - x0) * W

        def sy(lat):
            return H - (lat - y0) / (y1 - y0) * H

        def try_place(lon, lat, label, major):
            fx = (lon - x0) / (x1 - x0)
            fy = (lat - y0) / (y1 - y0)
            if not (0.012 < fx < 0.93 and 0.155 < fy < 0.775):
                return False
            gx = 0.090 if major else 0.065
            gy = gx * (W / float(H)) * 0.60
            for ox, oy in placed:
                if abs(fx - ox) < gx and abs(fy - oy) < gy:
                    return False
            placed.append((fx, fy))

            px, py = sx(lon), sy(lat)
            r = 4.4 if major else 2.6
            cr.set_source_rgb(0.95, 0.97, 0.99) if major else \
                cr.set_source_rgb(0.72, 0.80, 0.86)
            cr.arc(px, py, r, 0, 2 * math.pi)
            cr.fill()
            if major:
                cr.set_source_rgba(0.03, 0.06, 0.10, 0.9)
                cr.set_line_width(1.4)
                cr.arc(px, py, r, 0, 2 * math.pi)
                cr.stroke()

            cr.select_font_face(
                "monospace", cairo.FONT_SLANT_NORMAL,
                cairo.FONT_WEIGHT_BOLD if major else cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(major_size if major else minor_size)
            tx, ty = px + 11, py - 8
            # dark halo, so labels hold up over both land and sea
            cr.set_source_rgba(0.02, 0.035, 0.06, 0.95)
            cr.set_line_width(3.4)
            cr.move_to(tx, ty)
            cr.text_path(label)
            cr.stroke()
            cr.set_source_rgb(*(TEXT_BRIGHT if major else (0.80, 0.86, 0.91)))
            cr.move_to(tx, ty)
            cr.show_text(label)
            cr.new_path()
            return True

        majors = _read_shp('ne_10m_populated_places',
                           ('NAME', 'NAMEASCII', 'SCALERANK'))
        majors = sorted(majors, key=lambda mr: mr[1].get('SCALERANK', 99))
        n = 0
        for parts, rec in majors:
            if n >= 14:
                break
            if not parts or not parts[0]:
                continue
            lon, lat = parts[0][0]
            label = _fix_name(rec.get('NAME') or rec.get('NAMEASCII') or '')
            if label and try_place(lon, lat, label, True):
                n += 1

        # NB: town_name, not name — reusing `name` here would clobber the
        # operator's name in the caller and print the last town drawn
        # under the callsign instead. Cost an hour during development.
        n = 0
        cap = 30 if span < 350 else (26 if span < 700 else 20)
        for town_name, tlat, tlon in _read_towns():
            if n >= cap:
                break
            if not (x0 < tlon < x1 and y0 < tlat < y1):
                continue
            if try_place(tlon, tlat, town_name, False):
                n += 1

    # -- QO-100 globe -----------------------------------------------------

    def _render_globe(self, W, H, callsign, locator, name, mer, modcod,
                      symbol_rate, frequency, site_name):
        """The end-of-contact card for a QO-100 contact.

        Deliberately a different picture rather than the terrestrial map
        with a different line on it: the signal genuinely did not travel
        between the two stations, and a great-circle path would be a
        confident lie. Orthographic globe, both ends marked, and the
        satellite drawn where it really is.

        Uses the 110m Natural Earth data rather than the 10m set the
        terrestrial card uses. That is not a compromise - at this scale
        10m is invisible detail that merely costs time to draw, and the
        bundled 10m set is clipped to 1200 km around the UK anyway, so
        it contains no Africa or Middle East to draw."""
        pos = locator_to_latlon(locator)
        if not pos or not self.home:
            return None
        lat_s, lon_s = pos
        lat_h, lon_h = self.home

        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        cr = cairo.Context(surf)
        cr.set_source_rgb(*_hex_rgb('#0B0D14'))
        cr.paint()

        top = H * BAND_TOP
        bot = H * (1 - BAND_BOTTOM)

        # Size the globe so the satellite AND its label clear the header
        # band. sat_off is how far above the disc centre the bird lands,
        # in globe radii - see VIEW_LAT for why this works out.
        _, sat_unit_y, _ = ortho(SAT_LAT, SAT_LON)
        sat_off = abs(sat_unit_y) * GEO_R
        R = (bot - top - H * 0.10) / (sat_off + 1.0)
        cx = W * 0.5
        cy = top + H * 0.081 + sat_off * R

        def P(lat, lon, r=1.0):
            x, y, vis = ortho(lat, lon)
            return cx + x * R * r, cy + y * R * r, vis

        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.set_source_rgb(*_hex_rgb('#212B3B'))
        cr.fill()

        cr.save()
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.clip()

        land = _read_shp('ne_110m_land')
        if not land:
            # Loud, because the alternative is a bare blue disc with two
            # correct markers on it, which reads as a drawing bug rather
            # than a missing file. A shapefile is three files - .shp,
            # .shx and .dbf - and pyshp needs all of them; copying only
            # the .shp leaves _read_shp catching the error and returning
            # nothing at all.
            print("[map] ne_110m_land not readable - the QO-100 globe will "
                  "have no land. Check geo/ contains ne_110m_land.shp, .shx "
                  "AND .dbf (all three are required).")
        for parts, _rec in land:
            for ring in parts:
                for i, (lon, lat) in enumerate(ring):
                    x, y, _ = P(lat, lon)
                    cr.move_to(x, y) if i == 0 else cr.line_to(x, y)
                cr.close_path()
        cr.set_source_rgb(*_hex_rgb('#313D51'))
        cr.fill()

        # Graticule - without it the disc reads as a flat circle
        cr.set_source_rgba(*COAST, 0.22)
        cr.set_line_width(max(0.8, H * 0.0009))
        for lat in range(-60, 61, 30):
            first = True
            for lon in range(-180, 181, 2):
                x, y, vis = P(lat, lon)
                if not vis:
                    first = True
                    continue
                cr.move_to(x, y) if first else cr.line_to(x, y)
                first = False
            cr.stroke()
        for lon in range(-180, 181, 30):
            first = True
            for lat in range(-88, 89, 2):
                x, y, vis = P(lat, lon)
                if not vis:
                    first = True
                    continue
                cr.move_to(x, y) if first else cr.line_to(x, y)
                first = False
            cr.stroke()

        # The equator, picked out - it is the line the satellite sits above
        cr.set_source_rgba(*COAST, 0.55)
        cr.set_line_width(max(1.2, H * 0.0013))
        first = True
        for lon in range(-180, 181, 2):
            x, y, vis = P(0, lon)
            if not vis:
                first = True
                continue
            cr.move_to(x, y) if first else cr.line_to(x, y)
            first = False
        cr.stroke()

        for parts, _rec in land:
            for ring in parts:
                first = True
                for (lon, lat) in ring:
                    x, y, vis = P(lat, lon)
                    if not vis:
                        first = True
                        continue
                    cr.move_to(x, y) if first else cr.line_to(x, y)
                    first = False
                cr.set_source_rgb(*COAST)
                cr.set_line_width(max(0.9, H * 0.001))
                cr.stroke()
        cr.restore()

        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.set_source_rgba(*COAST, 0.75)
        cr.set_line_width(max(1.4, H * 0.0016))
        cr.stroke()

        # -- the satellite, at its true distance ---------------------------
        sub_x, sub_y, _ = P(SAT_LAT, SAT_LON)
        sx, sy, _ = P(SAT_LAT, SAT_LON, r=GEO_R)

        cr.set_source_rgba(*COAST, 0.5)
        cr.set_line_width(1.0)
        cr.set_dash([4, 6])
        cr.move_to(sub_x, sub_y)
        cr.line_to(sx, sy)
        cr.stroke()
        cr.set_dash([])
        cr.set_source_rgba(*COAST, 0.9)
        cr.arc(sub_x, sub_y, max(2.5, H * 0.003), 0, 2 * math.pi)
        cr.fill()

        hx, hy, _ = P(lat_h, lon_h)
        rx, ry, _ = P(lat_s, lon_s)

        cr.set_line_width(max(2.0, H * 0.0024))
        cr.set_source_rgb(*PATH)
        for (px, py) in ((hx, hy), (rx, ry)):
            cr.move_to(px, py)
            cr.line_to(sx, sy)
            cr.stroke()

        wing_w, wing_h = W * 0.0167, H * 0.013
        body = H * 0.024
        cr.set_source_rgb(*_hex_rgb('#8CB8F2'))
        cr.rectangle(sx - body / 2 - wing_w - 3, sy - wing_h / 2, wing_w, wing_h)
        cr.fill()
        cr.rectangle(sx + body / 2 + 3, sy - wing_h / 2, wing_w, wing_h)
        cr.fill()
        cr.set_source_rgb(*AMBER)
        cr.rectangle(sx - body / 2, sy - body / 2, body, body)
        cr.fill()

        def gtext(x, y, t, size, colour, bold=False, centre=False):
            cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL,
                                cairo.FONT_WEIGHT_BOLD if bold
                                else cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(size)
            if centre:
                x -= cr.text_extents(t).width / 2
            cr.set_source_rgb(*colour)
            cr.move_to(x, y)
            cr.show_text(t)
            cr.new_path()

        gtext(sx, sy - body / 2 - H * 0.030, 'QO-100', H * 0.021,
              TEXT_BRIGHT, bold=True, centre=True)
        gtext(sx, sy - body / 2 - H * 0.012, '25.9\u00b0E  GEO', H * 0.014,
              TEXT_LABEL, centre=True)

        # -- the two ends --------------------------------------------------
        for (px, py, col, lat_p, lon_p) in ((hx, hy, CYAN, lat_h, lon_h),
                                            (rx, ry, AMBER, lat_s, lon_s)):
            cr.set_source_rgba(*col, 0.28)
            cr.arc(px, py, max(10, H * 0.014), 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(*col)
            cr.arc(px, py, max(4.5, H * 0.006), 0, 2 * math.pi)
            cr.fill()
            lbl = country_of(lat_p, lon_p)
            if lbl:
                cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL,
                                    cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(H * 0.0195)
                cr.set_source_rgb(*TEXT_BRIGHT)
                cr.move_to(px + H * 0.018, py + H * 0.007)
                cr.show_text(lbl)
                cr.new_path()

        dist = haversine_km(lat_h, lon_h, lat_s, lon_s)
        _total, delay_ms = sat_path_delay_ms(lat_h, lon_h, lat_s, lon_s)
        self._draw_text(cr, W, H, callsign, locator, name, dist, None,
                        mer, modcod, symbol_rate, frequency, site_name,
                        delay_ms=delay_ms)
        return surf

    # -- text card --------------------------------------------------------

    def _draw_text(self, cr, W, H, callsign, locator, name, dist, brg,
                   mer, modcod, symbol_rate, frequency, site_name,
                   delay_ms=None):
        """Shared between the terrestrial card and the QO-100 globe, so
        the two always agree on layout - same bands, same column slots,
        same attribution. A viewer should never feel they are looking at
        two different products.

        delay_ms, when given, replaces the BEARING column with DELAY.
        A bearing is meaningless for a satellite contact - both stations
        are pointing at the same bird, so it would be near enough the
        same number every time - whereas the quarter-second delay is the
        thing everyone remarks on."""
        top_h = int(H * BAND_TOP)
        bot_h = int(H * BAND_BOTTOM)

        # Slightly transparent rather than solid: the map stays faintly
        # visible behind the bands, so a marker that lands close to the
        # edge still reads instead of vanishing. The framing above is
        # the real fix for that - this is belt and braces.
        cr.set_source_rgba(0.016, 0.027, 0.047, 0.80)
        cr.rectangle(0, 0, W, top_h)
        cr.fill()
        cr.rectangle(0, H - bot_h, W, bot_h)
        cr.fill()

        def text(x, y, s, size, colour, bold=False, right=False, mono=True):
            cr.select_font_face(
                "monospace" if mono else "sans-serif",
                cairo.FONT_SLANT_NORMAL,
                cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(size)
            if right:
                x -= cr.text_extents(s).width
            cr.set_source_rgb(*colour)
            cr.move_to(x, y)
            cr.show_text(s)
            cr.new_path()

        m = int(W * 0.028)

        text(m, top_h * 0.52, (callsign or '').upper(), H * 0.062,
             TEXT_BRIGHT, bold=True, mono=False)
        if name:
            text(m, top_h * 0.82, name, H * 0.026, TEXT_DIM, mono=False)

        text(W - m, top_h * 0.34, (self.home_locator or ''), H * 0.030,
             CYAN, bold=True, right=True)
        home_line = site_name or ''
        if home_line:
            text(W - m, top_h * 0.62, home_line.upper(), H * 0.020,
                 TEXT_LABEL, right=True)
        text(W - m, top_h * 0.88, 'END OF CONTACT', H * 0.018,
             TEXT_LABEL, right=True)

        # bottom data strip
        cols = [('LOCATOR', (locator or '').upper()),
                ('DISTANCE', f'{dist:,.0f} km')]
        if delay_ms is not None:
            cols.append(('DELAY', f'{delay_ms:.0f} ms'))
        elif brg is not None:
            cols.append(('BEARING', f'{brg:.0f}\u00b0 {compass_point(brg)}'))
        if mer:
            cols.append(('MER', f'{mer} dB'))
        if modcod:
            cols.append(('MODCOD', str(modcod)))
        if symbol_rate:
            cols.append(('SR', f'{symbol_rate} kS'))
        if frequency:
            cols.append(('FREQ', f'{frequency} MHz'))

        usable = W - 2 * m
        step = usable / max(len(cols), 1)
        for i, (label, value) in enumerate(cols):
            x = m + i * step
            text(x, H - bot_h * 0.60, label, H * 0.0165, TEXT_LABEL)
            text(x, H - bot_h * 0.22, value, H * 0.030, (0.91, 0.94, 0.97),
                 bold=True)

        text(W - 6, 14, 'Natural Earth (public domain) \u00b7 towns GeoNames (CC BY)',
             H * 0.0115, (0.24, 0.30, 0.38), right=True)


# ---------------------------------------------------------------- tracker

class PathfinderTracker:
    """Decides when a card should be showing, and for which station.

    Deliberately holds no timers. Lynx's existing notification code
    stores a timestamp and computes expiry when asked (see
    TriWatchArbitrator.get_notification), and this follows the same
    pattern: a stale timer firing a card over a station that has since
    come back simply cannot happen, because nothing is ever scheduled.

    station_unlocked() is called when a station stops transmitting;
    station_locked() whenever one starts, which clears any pending or
    showing card immediately.
    """

    def __init__(self, delay_secs=2.0, duration_secs=30.0,
                 max_distance_km=1200.0, enabled=True):
        self.delay_secs = float(delay_secs)
        self.duration_secs = float(duration_secs)
        self.max_distance_km = float(max_distance_km)
        self.enabled = bool(enabled)
        self.pending = None

    # How long the card may be held after a station locks, waiting for a
    # picture that never comes. Without a cap, a source that locks but
    # never renders would leave the map up indefinitely.
    HOLD_AFTER_LOCK_MAX_SECS = 12.0

    def station_locked(self):
        """A station is transmitting.

        Note this does NOT clear the card. A lock is not a picture: the
        lock has to be confirmed, mpv has to start, and mpv has to
        confirm it is rendering - several seconds during which the card
        was previously already gone and the viewer saw the idle screen
        instead. The map was covering that gap perfectly well.

        So the card is held until a picture is genuinely up, which
        get_card() is told about, or until HOLD_AFTER_LOCK_MAX_SECS has
        passed in case no picture ever arrives. Audio is not held back
        with it - sound arriving slightly before the picture is normal
        for a vision cut and better than silence.
        """
        if self.pending is not None:
            self.pending['locked_at'] = time.time()

    def station_unlocked(self, callsign, locator, name=None, mer=None,
                         modcod=None, symbol_rate=None, frequency=None,
                         via_qo100=False):
        """A station has stopped. Arms the card if we have enough to draw
        one; otherwise does nothing at all, which leaves the normal idle
        screen in place."""
        if not self.enabled or not callsign or not locator:
            return
        if locator_to_latlon(locator) is None:
            print(f"[map] {callsign}: unusable locator {locator!r} - no card")
            return
        self.pending = {
            'callsign': callsign,
            'locator': locator,
            'name': name,
            'mer': mer,
            'modcod': modcod,
            'symbol_rate': symbol_rate,
            'frequency': frequency,
            'via_qo100': via_qo100,
            'unlocked_at': time.time(),
        }

    def get_card(self, picture_ready=False):
        """Returns the card to display now, or None. One comparison
        covers both the delay before it appears and the window it stays
        up for."""
        if not self.enabled or self.pending is None:
            return None
        age = time.time() - self.pending['unlocked_at']
        if age < self.delay_secs:
            return None
        if age > self.delay_secs + self.duration_secs:
            return None
        # A station has locked since this card was armed: hold the card
        # until there is actually a picture to cut to, rather than
        # dropping straight to the idle screen while the new source
        # acquires. See station_locked().
        locked_at = self.pending.get('locked_at')
        if locked_at is not None:
            if picture_ready:
                self.pending = None
                return None
            if time.time() - locked_at > self.HOLD_AFTER_LOCK_MAX_SECS:
                self.pending = None
                return None
        return self.pending
