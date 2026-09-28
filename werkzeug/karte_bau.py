# -*- coding: utf-8 -*-
"""Baut die gezeichnete Schottlandkarte fuer die Reisekarte in index.html:
gestapelte Hoehenschichten als Dreiecksnetz + Kanten, dazu die Strassen-
routen (OSRM, ueber Land). Orte und Zwischenpunkte stehen in reise.json.

Aufruf:  python werkzeug/karte_bau.py daten/schottland-karte.bin.gz
Braucht: numpy pillow scipy contourpy shapely mapbox_earcut"""
import gzip, io, json, math, os, struct, sys, tempfile, urllib.request
import numpy as np
from PIL import Image
from scipy import ndimage
import contourpy
from shapely.geometry import Polygon, LineString
from shapely.validation import make_valid
import mapbox_earcut as earcut

S = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(tempfile.gettempdir(), "lxr_dem_cache"); os.makedirs(CACHE, exist_ok=True)
Z = 9
W_LON, E_LON, S_LAT, N_LAT = -7.35, -3.95, 55.75, 58.25
# Unter null: Tiefenlinien im Meer, wie auf einer Seekarte.
LEVELS = [-150, -100, -60, -30, -10, 0, 45, 110, 190, 280, 380, 490, 610, 740, 880, 1030]
C = 40075.0 * math.cos(math.radians(57.0))       # km je Mercator-Einheit


def merc(lon, lat):
    x = (lon + 180) / 360
    s = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x, y


def welt(lon, lat):          # km, x nach Osten, y nach Norden
    x, y = merc(lon, lat)
    return x * C, -y * C


# ---------- Hoehen ----------
n = 2 ** Z
x0, y0 = merc(W_LON, N_LAT); x1, y1 = merc(E_LON, S_LAT)
tx0, ty0, tx1, ty1 = int(x0 * n), int(y0 * n), int(x1 * n), int(y1 * n)
rows = []
for ty in range(ty0, ty1 + 1):
    row = []
    for tx in range(tx0, tx1 + 1):
        f = os.path.join(CACHE, "%d_%d_%d.png" % (Z, tx, ty))
        if not os.path.exists(f):
            url = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/%d/%d/%d.png" % (Z, tx, ty)
            open(f, "wb").write(urllib.request.urlopen(url).read())
        a = np.asarray(Image.open(f).convert("RGB")).astype(np.float64)
        row.append(a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768)
    rows.append(np.hstack(row))
h = np.vstack(rows)
# Ausschnitt in Pixeln
px0 = int((x0 * n - tx0) * 256); py0 = int((y0 * n - ty0) * 256)
px1 = int((x1 * n - tx0) * 256); py1 = int((y1 * n - ty0) * 256)
h = h[py0:py1, px0:px1]
print("Raster", h.shape)

# Fehlpunkte weg (Median), Meer flach, dann weich -- kuenstlerisch, nicht exakt
h = ndimage.median_filter(h, size=3)
h = np.clip(h, -400, 1340)
h = ndimage.gaussian_filter(h, 1.6)


def pix2welt(px, py):
    mx = (tx0 * 256 + px0 + px) / (n * 256)
    my = (ty0 * 256 + py0 + py) / (n * 256)
    return mx * C, -my * C


# Ursprung der Welt: Mitte des Ausschnitts
ox, oy = pix2welt(h.shape[1] / 2, h.shape[0] / 2)
Q = 100.0                   # 1 Einheit = 10 m

verts, tris, ringe = [], [], []


def chaikin(r, runden=2):
    """Ecken abrunden (Chaikin): aus jeder Kante werden zwei, die Form wird
    weich und organisch statt eckig."""
    for _ in range(runden):
        nxt = np.roll(r, -1, axis=0)
        neu = np.empty((2 * len(r), 2))
        neu[0::2] = 0.75 * r + 0.25 * nxt
        neu[1::2] = 0.25 * r + 0.75 * nxt
        r = neu
    return r

layers = []
gen = contourpy.contour_generator(z=h, fill_type=contourpy.FillType.OuterOffset, line_type=contourpy.LineType.Separate)
TOL = 0.9                   # Vereinfachung in Pixeln (~150 m)
for li, lev in enumerate(LEVELS):
    polys, offsets = gen.filled(lev + 0.5, 1e9)
    v0, t0, r0 = len(verts), len(tris), len(ringe)
    for pts, offs in zip(polys, offsets):
        rings = [pts[offs[k]:offs[k + 1]] for k in range(len(offs) - 1)]
        try:
            poly = Polygon(rings[0], rings[1:])
        except Exception:
            continue
        if poly.area < (6 if lev <= 0 else 4):
            continue
        poly = poly.simplify(TOL, preserve_topology=True)
        poly = make_valid(poly)
        geoms = [poly] if poly.geom_type == "Polygon" else [g for g in getattr(poly, "geoms", []) if g.geom_type == "Polygon"]
        for g in geoms:
            if g.is_empty or g.area < 3:
                continue
            ringlist = [chaikin(np.asarray(g.exterior.coords)[:-1])] + \
                       [chaikin(np.asarray(r.coords)[:-1]) for r in g.interiors if Polygon(r).area > 3]
            allp = np.vstack(ringlist)
            ends = np.cumsum([len(r) for r in ringlist]).astype(np.uint32)
            idx = earcut.triangulate_float64(allp, ends)
            base = len(verts) - v0          # Indizes je Schicht, ab ihrem ersten Punkt
            for p in allp:
                wx, wy = pix2welt(p[0], p[1])
                verts.append((round((wx - ox) * Q), round((wy - oy) * Q)))
            tris.extend(int(i) + base for i in idx)
            # Kanten werden nicht gespeichert: die Punkte liegen Ring fuer
            # Ring hintereinander, der Browser verbindet sie selbst.
            ringe.extend(len(r) for r in ringlist)
    layers.append({"lev": lev, "v": [v0, len(verts)], "t": [t0, len(tris)], "r": [r0, len(ringe)]})
    print("Schicht %4d m: %6d Punkte %7d Dreiecke/3 %5d Ringe" % (lev, len(verts) - v0, len(tris) - t0, len(ringe) - r0))

assert max(L["v"][1] - L["v"][0] for L in layers) < 65536
vx = np.array(verts, dtype=np.int32)
assert np.abs(vx).max() < 32767, np.abs(vx).max()


# ---------- Hoehe nachschlagen (fuer Route und Orte) ----------
def hoehe_bei(wx, wy):
    mx = wx / C; my = -wy / C
    px = mx * n * 256 - tx0 * 256 - px0; py = my * n * 256 - ty0 * 256 - py0
    ix = min(max(int(px), 0), h.shape[1] - 1); iy = min(max(int(py), 0), h.shape[0] - 1)
    return float(max(0.0, h[iy, ix]))


def schicht_bei(wx, wy):
    v = hoehe_bei(wx, wy)
    lev = 0
    for L in LEVELS:
        if v >= L + 0.5: lev = L
    return max(0, lev)


# ---------- Routen ----------
REISE = json.load(open(os.path.join(S, "reise.json"), encoding="utf-8"))


def osrm(pkte):
    q = ";".join("%.5f,%.5f" % (p[0], p[1]) for p in pkte)
    url = "https://router.project-osrm.org/route/v1/driving/%s?overview=full&geometries=geojson&steps=true" % q
    d = json.load(urllib.request.urlopen(url))
    r = d["routes"][0]
    modi = set(s["mode"] for leg in r["legs"] for s in leg["steps"])
    return r["geometry"]["coordinates"], r["distance"] / 1000, modi


def weg(coords):
    pts = [welt(c[0], c[1]) for c in coords]
    ls = LineString(pts).simplify(0.06)
    return [[round(x - ox, 3), round(y - oy, 3), round(schicht_bei(x, y) / 1000, 3)] for x, y in ls.coords]


legs = []
for a, b in zip(REISE["orte"][:-1], REISE["orte"][1:]):
    pk = [a["lngLat"]] + b.get("ueber", []) + [b["lngLat"]]
    co, km, modi = osrm(pk)
    print("%s -> %s: %.0f km, %s" % (a["ort"], b["ort"], km, modi))
    legs.append(weg(co))

orte = []
for o in REISE["orte"]:
    wx, wy = welt(*o["lngLat"])
    orte.append([round(wx - ox, 3), round(wy - oy, 3), round(schicht_bei(wx, wy) / 1000, 3)])

# ---------- Abstand zur Kueste (fuer die Wasserlinien) ----------
# Fuer jeden Meerespunkt die Entfernung zum naechsten Land, in km, als
# kleines Graustufenbild (halbe Aufloesung, 0..255 fuer 0..TEX_KM km).
# Daraus zeichnet der Browser die Wasserlinien um die Kuesten.
TEX_KM = 14.0
px_km = (pix2welt(h.shape[1], 0)[0] - pix2welt(0, 0)[0]) / h.shape[1]
abst = ndimage.distance_transform_edt(h <= 0.5) * px_km
tex = np.clip(abst[::2, ::2] / TEX_KM * 255 + 0.5, 0, 255).astype(np.uint8)

kopf = {
    "q": Q, "ursprung": [ox, oy], "c": C, "stufen": LEVELS, "schichten": layers,
    "groesse": [round((pix2welt(h.shape[1], 0)[0] - pix2welt(0, 0)[0]), 2),
                round((pix2welt(0, 0)[1] - pix2welt(0, h.shape[0])[1]), 2)],
    "orte": orte, "wege": legs, "tex": [int(tex.shape[1]), int(tex.shape[0])], "texKm": TEX_KM
}
kj = json.dumps(kopf, separators=(",", ":")).encode("utf-8")
kj += b" " * ((4 - len(kj) % 4) % 4)
# Punkte als Schritt zum vorigen Punkt desselben Rings: kleine Zahlen, die
# sich weit besser packen lassen. Der erste Punkt jedes Rings steht absolut.
dx = vx.copy()
s = 0
for m in ringe:
    dx[s + 1:s + m] = vx[s + 1:s + m] - vx[s:s + m - 1]
    s += m
assert np.abs(dx).max() < 32767
roh = io.BytesIO()
roh.write(struct.pack("<IIII", len(kj), len(verts), len(tris), len(ringe)))
roh.write(kj)
roh.write(dx.astype("<i2").tobytes())            # 4 Bytes je Punkt: bleibt ausgerichtet
# 16 Bit je Index reicht, weil jede Schicht ab ihrem eigenen ersten Punkt zaehlt
roh.write(np.array(tris, dtype="<u2").tobytes())
if len(tris) % 2: roh.write(b"\0\0")
roh.write(np.array(ringe, dtype="<u4").tobytes())
roh.write(tex.tobytes())
ziel = sys.argv[1]
with open(ziel, "wb") as f:
    f.write(gzip.compress(roh.getvalue(), 9, mtime=0))
print("Datei", ziel, os.path.getsize(ziel), "Bytes (ungepackt %d);" % len(roh.getvalue()),
      len(verts), "Punkte", len(tris) // 3, "Dreiecke")
