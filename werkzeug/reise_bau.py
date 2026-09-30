# -*- coding: utf-8 -*-
"""Baut die Daten der versteckten Reisekarte (sg-reise-7q4m2x/).

Aus den Fotos (fotos.js, von geheime_karte_bau.py) entstehen Stationen:
ein Ort, an dem Fotos entstanden sind, und bei vielen Fotos am selben Ort
mehrere Stationen nach der Uhrzeit. Dazu:

  daten/basis.bin.gz   Grundkarte der ganzen Reise (Hamburg bis Skye)
  daten/nah-NN.bin.gz  Detailstuecke um die Fotostellen: feine Hoehenstufen,
                       Seen, Strassen, Wege, Fluesse, Namen (OpenStreetMap)
  reise.js             Stationen, Kamerapunkte, Routen, Detailstuecke

Alle Dateien teilen ein Koordinatensystem (km, Mercator, Ursprung in der
Mitte der Grundkarte). Aufruf:  python werkzeug/reise_bau.py
Braucht: numpy pillow scipy contourpy shapely mapbox_earcut"""
import gzip, io, json, math, os, sys, tempfile, time, datetime, urllib.parse, urllib.request
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
import contourpy
from shapely.geometry import Polygon, LineString, Point
from shapely.validation import make_valid
from shapely.ops import polygonize, linemerge
import mapbox_earcut as earcut

WURZEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEITE = os.path.join(WURZEL, "sg-reise-7q4m2x")
DATEN = os.path.join(SEITE, "daten")
CACHE = os.path.join(tempfile.gettempdir(), "lxr_dem_cache")
os.makedirs(CACHE, exist_ok=True); os.makedirs(DATEN, exist_ok=True)

# ---------- Stationen ----------
R_KM = 0.4          # Fotos naeher beieinander: derselbe Ort
LUECKE_MIN = 75     # laengere Pause am selben Ort: neue Station
MAX_FOTOS = 6       # mehr Fotos am selben Ort: nach der Uhrzeit aufgeteilt
GRUPPE_KM = 7       # Orte in diesem Umkreis teilen ein Detailstueck
NAH_RAND_KM = 5.5   # Rand des Detailstuecks um die Orte
NAH_HALB_MIN = 7.0  # halbe Mindestgroesse eines Detailstuecks

BASIS_ZOOM, NAH_ZOOM = 8, 12
BASIS_STUFEN = [-200, -100, -40, -10, 0, 60, 150, 280, 450, 650, 900]
NAH_STUFEN = [-60, -30, -12, -4, 0, 12, 25, 40, 60, 80, 100, 125, 150, 180, 210, 250, 290, 330,
              380, 430, 490, 550, 620, 700, 780, 870, 960]


def km_ll(a, b):
    dy = (a[0] - b[0]) * 111.2
    dx = (a[1] - b[1]) * 111.2 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


fotos = json.loads(open(os.path.join(SEITE, "fotos.js"), encoding="utf-8").read().split("= ", 1)[1].rstrip().rstrip(";"))
mit = sorted([f for f in fotos if "ort" in f and "u" in f], key=lambda f: f["u"])
ohne = [f["id"] for f in fotos if not ("ort" in f and "u" in f)]
stationen = []
for f in mit:
    s = stationen[-1] if stationen else None
    if s and km_ll(s["ll"], f["ort"]) <= R_KM and f["u"] - s["bis"] <= LUECKE_MIN and len(s["f"]) < MAX_FOTOS:
        s["f"].append(f); s["bis"] = f["u"]
        n = len(s["f"]); s["ll"] = [sum(x["ort"][0] for x in s["f"]) / n, sum(x["ort"][1] for x in s["f"]) / n]
    else:
        stationen.append({"f": [f], "ll": list(f["ort"]), "von": f["u"], "bis": f["u"]})
# Kamerapunkte: aufeinanderfolgende Stationen am selben Ort teilen einen.
punkte = []
for s in stationen:
    if punkte and km_ll(punkte[-1]["ll"], s["ll"]) <= R_KM:
        p = punkte[-1]; p["st"].append(s)
        alle = [x for st in p["st"] for x in st["f"]]
        p["ll"] = [sum(x["ort"][0] for x in alle) / len(alle), sum(x["ort"][1] for x in alle) / len(alle)]
    else:
        punkte.append({"ll": list(s["ll"]), "st": [s]})
for i, p in enumerate(punkte):
    for s in p["st"]: s["punkt"] = i
print("Fotos %d, Stationen %d, Kamerapunkte %d, ohne Ort/Zeit %d" % (len(mit), len(stationen), len(punkte), len(ohne)))

# ---------- Rahmen: gemeinsames Koordinatensystem ----------
lat_alle = [p["ll"][0] for p in punkte]; lon_alle = [p["ll"][1] for p in punkte]
B_W, B_E = min(lon_alle) - 1.6, max(lon_alle) + 1.2
B_S, B_N = min(lat_alle) - 0.7, max(lat_alle) + 0.6
C = 40075.0 * math.cos(math.radians((B_S + B_N) / 2))     # km je Mercator-Einheit


def merc(lon, lat):
    s = math.sin(math.radians(lat))
    return (lon + 180) / 360, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def welt(lon, lat):
    x, y = merc(lon, lat)
    return x * C, -y * C


mx0, my0 = merc(B_W, B_N); mx1, my1 = merc(B_E, B_S)
OX, OY = (mx0 + mx1) / 2 * C, -(my0 + my1) / 2 * C      # Ursprung: Mitte der Grundkarte


def lokal(lon, lat):
    x, y = welt(lon, lat)
    return x - OX, y - OY


# ---------- Netz ----------
def holen(url, datei, versuche=4):
    if os.path.exists(datei) and os.path.getsize(datei) > 0:
        return open(datei, "rb").read()
    for v in range(versuche):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lxr-films-reisekarte/1.0"})
            d = urllib.request.urlopen(req, timeout=120).read()
            open(datei, "wb").write(d)
            return d
        except Exception as e:
            print("   ", url[:90], "->", e); time.sleep(3 + 5 * v)
    raise SystemExit("nicht abrufbar: " + url)


def overpass(q, name):
    cf = os.path.join(CACHE, "reise_%s.json" % name)
    if os.path.exists(cf) and os.path.getsize(cf) > 0:
        return json.load(open(cf, encoding="utf-8"))
    for v, server in enumerate(["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
                                "https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                                "https://overpass-api.de/api/interpreter"]):
        try:
            req = urllib.request.Request(server, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": "lxr-films-reisekarte/1.0"})
            a = urllib.request.urlopen(req, timeout=300).read()
            j = json.loads(a)
            if "remark" in j and "error" in j["remark"].lower(): raise RuntimeError(j["remark"][:120])
            open(cf, "wb").write(a)
            time.sleep(1.5)
            return j
        except Exception as e:
            print("   overpass", name, server, "->", e); time.sleep(8 + 6 * v)
    raise SystemExit("Overpass nicht abrufbar: " + name)


# ---------- Gelaende ----------
def chaikin(r, runden=2):
    for _ in range(runden):
        nxt = np.roll(r, -1, axis=0)
        neu = np.empty((2 * len(r), 2))
        neu[0::2] = 0.75 * r + 0.25 * nxt
        neu[1::2] = 0.25 * r + 0.75 * nxt
        r = neu
    return r


def ringe_aus(el):
    """Aussenringe (lon, lat) eines OSM-Wegs oder einer Relation."""
    if el["type"] == "way":
        g = el.get("geometry", [])
        return [[(p["lon"], p["lat"]) for p in g]] if len(g) > 3 and g[0] == g[-1] else []
    st = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]]) for m in el.get("members", [])
          if m.get("role") == "outer" and m.get("geometry") and len(m["geometry"]) > 1]
    return [list(x.exterior.coords) for x in polygonize(linemerge(st))] if st else []


class Gelaende:
    def __init__(self, name, ausschnitt, zoom, stufen, glaettung, toleranz, q, seen_osm=None, see_min_km2=0.03,
                 min_flaeche=(6, 4), tex_km=6.0, tex_schritt=1):
        self.name, self.stufen, self.q = name, stufen, q
        W, E, S, N = ausschnitt
        n = 2 ** zoom
        x0, y0 = merc(W, N); x1, y1 = merc(E, S)
        tx0, ty0, tx1, ty1 = int(x0 * n), int(y0 * n), int(x1 * n), int(y1 * n)
        zeilen = []
        for ty in range(ty0, ty1 + 1):
            z = []
            for tx in range(tx0, tx1 + 1):
                f = os.path.join(CACHE, "%d_%d_%d.png" % (zoom, tx, ty))
                holen("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/%d/%d/%d.png" % (zoom, tx, ty), f)
                a = np.asarray(Image.open(f).convert("RGB")).astype(np.float64)
                z.append(a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768)
            zeilen.append(np.hstack(z))
        h = np.vstack(zeilen)
        px0 = int((x0 * n - tx0) * 256); py0 = int((y0 * n - ty0) * 256)
        px1 = int((x1 * n - tx0) * 256); py1 = int((y1 * n - ty0) * 256)
        h = h[py0:py1, px0:px1]
        self.n, self.tx0, self.ty0, self.px0, self.py0 = n, tx0, ty0, px0, py0
        h = ndimage.median_filter(h, size=3)
        h = np.clip(h, min(stufen) - 200, max(stufen) + 400)

        # Mitte des Stuecks: Ursprung seiner Punkte
        cx, cy = self.pix2welt(h.shape[1] / 2, h.shape[0] / 2)
        self.mitte = (cx - OX, cy - OY)
        self.groesse = [self.pix2welt(h.shape[1], 0)[0] - self.pix2welt(0, 0)[0],
                        self.pix2welt(0, 0)[1] - self.pix2welt(0, h.shape[0])[1]]
        px_km = self.groesse[0] / h.shape[1]

        # Seen: flach auf ihre Wasserhoehe, eigene Flaechen
        self.seen = []
        seemaske = np.zeros(h.shape, bool)
        for el in (seen_osm or []):
            tags = el.get("tags", {})
            if tags.get("water") in ("river", "stream", "canal", "riverbank", "tidal", "lagoon"): continue
            for rl in ringe_aus(el):
                if len(rl) < 4: continue
                pw = Polygon([lokal(*p) for p in rl])
                if not pw.is_valid: pw = make_valid(pw)
                if pw.geom_type != "Polygon" or pw.area < see_min_km2: continue
                img = Image.new("L", (h.shape[1], h.shape[0]), 0)
                ImageDraw.Draw(img).polygon([self.ll2pix(*p) for p in rl], fill=1)
                m = np.asarray(img).astype(bool)
                if m.sum() < 3: continue
                pegel = float(np.median(h[m]))
                if pegel < 1: continue                     # Meeresbucht, kein See
                self.seen.append({"name": tags.get("name", ""), "poly": pw, "maske": m, "pegel": pegel})
                seemaske |= m
        for s in self.seen: h[s["maske"]] = s["pegel"]
        h = ndimage.gaussian_filter(h, glaettung)
        for s in self.seen: h[s["maske"]] = s["pegel"]
        self.h = h

        self.verts, self.tris, self.ringe, self.schichten, self.seen_kopf = [], [], [], [], []
        gen = contourpy.contour_generator(z=h, fill_type=contourpy.FillType.OuterOffset, line_type=contourpy.LineType.Separate)
        for lev in stufen:
            polys, offsets = gen.filled(lev + 0.5, 1e9)
            v0, t0, r0 = len(self.verts), len(self.tris), len(self.ringe)
            for pts, offs in zip(polys, offsets):
                rings = [pts[offs[k]:offs[k + 1]] for k in range(len(offs) - 1)]
                try: poly = Polygon(rings[0], rings[1:])
                except Exception: continue
                if poly.area < (min_flaeche[0] if lev <= 0 else min_flaeche[1]): continue
                poly = make_valid(poly.simplify(toleranz, preserve_topology=True))
                geoms = [poly] if poly.geom_type == "Polygon" else [g for g in getattr(poly, "geoms", []) if g.geom_type == "Polygon"]
                for g in geoms:
                    if g.is_empty or g.area < 3: continue
                    rl = [chaikin(np.asarray(g.exterior.coords)[:-1])] + \
                         [chaikin(np.asarray(r.coords)[:-1]) for r in g.interiors if Polygon(r).area > 3]
                    rund = Polygon(rl[0], rl[1:])
                    if rund.is_valid: teile = [rl]
                    else:
                        rep = make_valid(rund); teile = []
                        for x in ([rep] if rep.geom_type == "Polygon" else getattr(rep, "geoms", [])):
                            if x.geom_type != "Polygon" or x.area < 1: continue
                            teile.append([np.asarray(x.exterior.coords)[:-1]] + [np.asarray(q_.coords)[:-1] for q_ in x.interiors])
                    for rl in teile:
                        neu = sum(len(r) for r in rl)
                        if len(self.verts) - v0 + neu > 65000 and len(self.verts) > v0:
                            self.schichten.append({"lev": lev, "v": [v0, len(self.verts)], "t": [t0, len(self.tris)], "r": [r0, len(self.ringe)]})
                            v0, t0, r0 = len(self.verts), len(self.tris), len(self.ringe)
                        if neu >= 65000: continue
                        self.ablegen([np.array([self.pix2lokal(p[0], p[1]) for p in r]) for r in rl], v0)
            self.schichten.append({"lev": lev, "v": [v0, len(self.verts)], "t": [t0, len(self.tris)], "r": [r0, len(self.ringe)]})
        for s in self.seen:
            v0, t0, r0 = len(self.verts), len(self.tris), len(self.ringe)
            g = s["poly"].simplify(0.01)
            if g.geom_type != "Polygon" or g.is_empty: continue
            self.ablegen([chaikin(np.asarray(g.exterior.coords)[:-1], 1)], v0)
            self.seen_kopf.append({"name": s["name"], "lev": self.terrasse(s["pegel"]), "v": [v0, len(self.verts)],
                                   "t": [t0, len(self.tris)], "r": [r0, len(self.ringe)]})
        abst = ndimage.distance_transform_edt((h <= 0.5) | seemaske) * px_km
        self.tex_km = tex_km
        self.tex = np.clip(abst[::tex_schritt, ::tex_schritt] / tex_km * 255 + 0.5, 0, 255).astype(np.uint8)
        self.linien, self.namen = [], []
        print("  %s: Raster %s, %d Punkte, %d Dreiecke, %d Seen" % (name, h.shape, len(self.verts), len(self.tris) // 3, len(self.seen_kopf)))

    # Pixel <-> Welt
    def ll2pix(self, lon, lat):
        mx, my = merc(lon, lat)
        return mx * self.n * 256 - self.tx0 * 256 - self.px0, my * self.n * 256 - self.ty0 * 256 - self.py0

    def pix2welt(self, px, py):
        mx = (self.tx0 * 256 + self.px0 + px) / (self.n * 256)
        my = (self.ty0 * 256 + self.py0 + py) / (self.n * 256)
        return mx * C, -my * C

    def pix2lokal(self, px, py):
        x, y = self.pix2welt(px, py)
        return x - OX, y - OY

    def ablegen(self, ringe_km, v0):
        allp = np.vstack(ringe_km)
        ends = np.cumsum([len(r) for r in ringe_km]).astype(np.uint32)
        idx = np.atleast_1d(np.asarray(earcut.triangulate_float64(allp, ends))).ravel()
        base = len(self.verts) - v0
        for p in allp:
            self.verts.append((round((p[0] - self.mitte[0]) * self.q), round((p[1] - self.mitte[1]) * self.q)))
        self.tris.extend(int(i) + base for i in idx)
        self.ringe.extend(len(r) for r in ringe_km)

    def terrasse(self, v):
        lev = self.stufen[0]
        for L in self.stufen:
            if v >= L + 0.5: lev = L
        return lev

    def enthaelt(self, x, y, rand=0.0):
        return abs(x - self.mitte[0]) < self.groesse[0] / 2 - rand and abs(y - self.mitte[1]) < self.groesse[1] / 2 - rand

    def hoehe(self, x, y):
        """Hoehe (m) am Punkt (km, gemeinsamer Rahmen), auf die Stufe gerundet."""
        mx = (x + OX) / C; my = -(y + OY) / C
        px = mx * self.n * 256 - self.tx0 * 256 - self.px0; py = my * self.n * 256 - self.ty0 * 256 - self.py0
        ix = min(max(int(px), 0), self.h.shape[1] - 1); iy = min(max(int(py), 0), self.h.shape[0] - 1)
        return max(0.0, self.terrasse(float(self.h[iy, ix])))

    def schreiben(self, datei, zusatz=None):
        vx = np.array(self.verts, dtype=np.int32).reshape(-1, 2)
        assert np.abs(vx).max() < 32767, (self.name, np.abs(vx).max())
        assert max([L["v"][1] - L["v"][0] for L in self.schichten + self.seen_kopf] + [0]) < 65536
        dx = vx.copy(); s = 0
        for m in self.ringe:
            dx[s + 1:s + m] = vx[s + 1:s + m] - vx[s:s + m - 1]; s += m
        teile, kopf_teile = [], {}

        def teil(name, arr):
            b = arr.tobytes()
            kopf_teile[name] = [sum(len(x) for x in teile), len(arr.ravel()), arr.dtype.str]
            teile.append(b + b"\0" * ((4 - len(b) % 4) % 4))

        teil("pos", dx.astype("<i2"))
        teil("tri", np.array(self.tris, dtype="<u2"))
        teil("ringe", np.array(self.ringe, dtype="<u4"))
        teil("tex", self.tex)
        # Linien (Strassen, Wege, Fluesse): Punkte x, y, Hoehe (m), je Linie die Anzahl
        lp, ln, arten = [], [], []
        for art, pk in self.linien:
            if len(pk) < 2: continue
            for x, y, z in pk: lp.append((round((x - self.mitte[0]) * self.q), round((y - self.mitte[1]) * self.q), round(z)))
            ln.append(len(pk)); arten.append(art)
        lpa = np.array(lp or [(0, 0, 0)], dtype=np.int32)
        ok = np.all(np.abs(lpa[:, :2]) < 32767, axis=1)
        if not ok.all():   # Linien, die ueber das Stueck hinausreichen: dort kappen
            lpa[:, 0] = np.clip(lpa[:, 0], -32766, 32766); lpa[:, 1] = np.clip(lpa[:, 1], -32766, 32766)
        teil("lpos", lpa.astype("<i2"))
        teil("lzahl", np.array(ln or [0], dtype="<u4"))
        teil("lart", np.array(arten or [0], dtype="<u1"))
        kopf = {"name": self.name, "q": self.q, "mitte": [round(self.mitte[0], 4), round(self.mitte[1], 4)],
                "groesse": [round(self.groesse[0], 4), round(self.groesse[1], 4)], "stufen": self.stufen,
                "schichten": self.schichten, "seen": self.seen_kopf, "tex": [int(self.tex.shape[1]), int(self.tex.shape[0])],
                "texKm": self.tex_km, "teile": kopf_teile, "namen": self.namen, "nLinien": len(ln)}
        if zusatz: kopf.update(zusatz)
        kj = json.dumps(kopf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        kj += b" " * ((4 - len(kj) % 4) % 4)
        roh = io.BytesIO()
        roh.write(np.array([len(kj)], dtype="<u4").tobytes()); roh.write(kj)
        for b in teile: roh.write(b)
        with open(datei, "wb") as fh: fh.write(gzip.compress(roh.getvalue(), 9, mtime=0))
        print("  ->", os.path.basename(datei), "%.0f kB" % (os.path.getsize(datei) / 1024))


# ---------- Namen und Linien aus OpenStreetMap ----------
RANG = {"city": 1, "town": 1, "village": 2, "island": 2, "suburb": 3, "hamlet": 3, "islet": 3, "peak": 3, "hill": 4,
        "cape": 3, "bay": 3, "beach": 3, "lighthouse": 2, "museum": 3, "water": 3, "isolated_dwelling": 4,
        "locality": 4, "farm": 5, "neighbourhood": 4, "viewpoint": 4, "attraction": 4, "camp_site": 4, "caravan_site": 4,
        "historic": 4, "cliff": 4, "saddle": 5, "sea": 1}
LINIEN_ART = {"motorway": 0, "trunk": 0, "motorway_link": 0, "trunk_link": 0, "primary": 0, "primary_link": 0,
              "secondary": 1, "secondary_link": 1, "tertiary": 1, "tertiary_link": 1,
              "unclassified": 2, "residential": 2, "living_street": 2,
              "track": 3, "path": 3, "footway": 3, "bridleway": 3, "cycleway": 3,
              "river": 4, "canal": 4, "stream": 5, "ferry": 6, "rail": 7}


def namen_aus(elemente, g, kasten):
    W, E, S, N = kasten
    aus, gesehen = [], set()
    for el in elemente:
        t = el.get("tags", {})
        name = t.get("name:en") if t.get("name", "").startswith("Eilean") and t.get("name:en") else t.get("name")
        if not name: continue
        art = (t.get("place") or t.get("natural") or ("lighthouse" if t.get("man_made") == "lighthouse" else None)
               or t.get("tourism") or ("historic" if t.get("historic") else None))
        if art not in RANG: continue
        if el["type"] == "node": lon, lat = el["lon"], el["lat"]
        elif "center" in el: lon, lat = el["center"]["lon"], el["center"]["lat"]
        elif "bounds" in el: b = el["bounds"]; lon, lat = (b["minlon"] + b["maxlon"]) / 2, (b["minlat"] + b["maxlat"]) / 2
        elif art == "water" and el.get("geometry") or el.get("members"):
            rl = ringe_aus(el)
            if not rl: continue
            pw = Polygon([lokal(*p) for p in rl[0]])
            if not pw.is_valid: pw = make_valid(pw)
            if pw.area < 0.08: continue
            rp = pw.representative_point(); lon = lat = None
            x, y = rp.x, rp.y
        else: continue
        if lon is not None:
            if not (W <= lon <= E and S <= lat <= N): continue
            x, y = lokal(lon, lat)
        if not g.enthaelt(x, y, 0.3): continue
        k = (name, art)
        if k in gesehen: continue
        gesehen.add(k)
        ele = t.get("ele", "")
        try: ele = int(float(ele.replace("m", "").strip()))
        except Exception: ele = None
        rang = RANG[art]
        if art == "peak" and ele and ele > 600: rang = 2
        aus.append([round(x, 3), round(y, 3), round(g.hoehe(x, y)), name, art, rang] + ([ele] if ele else []))
    aus.sort(key=lambda n: n[5])
    return aus


def linien_aus(elemente, g):
    aus = []
    for el in elemente:
        if el["type"] != "way" or not el.get("geometry"): continue
        t = el.get("tags", {})
        art = LINIEN_ART.get(t.get("highway")) if "highway" in t else \
            LINIEN_ART.get(t.get("waterway")) if "waterway" in t else \
            LINIEN_ART["ferry"] if t.get("route") == "ferry" else \
            LINIEN_ART["rail"] if t.get("railway") == "rail" else None
        if art is None: continue
        pk = [lokal(p["lon"], p["lat"]) for p in el["geometry"]]
        ls = LineString(pk).simplify(0.006)
        # nur der Teil im Stueck (mit etwas Rand)
        stueck = []
        for x, y in ls.coords:
            if g.enthaelt(x, y, -0.2):
                stueck.append((x, y, g.hoehe(x, y) if art not in (6,) else 0.0))
            else:
                if len(stueck) > 1: aus.append((art, stueck))
                stueck = []
        if len(stueck) > 1: aus.append((art, stueck))
    return aus


def osm_nah(kasten, name):
    W, E, S, N = kasten
    bb = "(%f,%f,%f,%f)" % (S, W, N, E)
    q = ('[out:json][timeout:240];('
         'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|'
         'motorway_link|trunk_link|primary_link|secondary_link|tertiary_link|track|path|footway|bridleway)$"]%s;'
         'way["waterway"~"^(river|stream|canal)$"]%s;way["route"="ferry"]%s;way["railway"="rail"]%s;);out geom;'
         '(node["place"~"^(city|town|village|hamlet|isolated_dwelling|locality|island|islet|farm|suburb|neighbourhood)$"]["name"]%s;'
         'node["natural"~"^(peak|cape|bay|beach|hill|cliff|saddle)$"]["name"]%s;'
         'node["man_made"="lighthouse"]["name"]%s;node["tourism"~"^(museum|attraction|viewpoint|camp_site|caravan_site)$"]["name"]%s;'
         'node["historic"]["name"]%s;);out;'
         '(way["natural"="water"]%s;relation["natural"="water"]%s;);out geom;'
         '(way["place"~"^(island|islet)$"]["name"]%s;relation["place"~"^(island|islet)$"]["name"]%s;'
         'way["natural"~"^(bay|beach|cape)$"]["name"]%s;relation["natural"="bay"]["name"]%s;'
         'way["tourism"~"^(museum|attraction|camp_site|caravan_site)$"]["name"]%s;way["man_made"="lighthouse"]["name"]%s;);out center;'
         % ((bb,) * 17))
    return overpass(q, name)["elements"]


def main():
    # ---------- Gruppen fuer die Detailstuecke ----------
    gruppen = []
    for i, p in enumerate(punkte):
        for g in gruppen:
            if km_ll(g["ll"], p["ll"]) < GRUPPE_KM:
                g["pk"].append(i); break
        else:
            gruppen.append({"ll": p["ll"], "pk": [i]})
    print("Detailstuecke:", len(gruppen))

    # ---------- Grundkarte ----------
    print("Grundkarte", [round(x, 2) for x in (B_W, B_E, B_S, B_N)])
    basis = Gelaende("basis", (B_W, B_E, B_S, B_N), BASIS_ZOOM, BASIS_STUFEN, 1.4, 0.8, 50,
                     min_flaeche=(8, 5), tex_km=40.0, tex_schritt=2)
    # In drei kleinen Abfragen -- eine grosse laeuft den Servern in die Zeitgrenze.
    bb = "(%f,%f,%f,%f)" % (B_S, B_W, B_N, B_E)
    bel = (overpass('[out:json][timeout:180];node["place"~"^(city|town)$"]%s;out;' % bb, "basis_orte")["elements"] +
           overpass('[out:json][timeout:180];node["place"="sea"]%s;out;' % bb, "basis_meere")["elements"] +
           # grosse Inseln gibt es auf der Strecke nur vor der schottischen Westkueste
           overpass('[out:json][timeout:300];(relation["place"="island"]["name"](55.3,%f,58.7,-4.5);'
                    'way["place"="island"]["name"](55.3,%f,58.7,-4.5););out tags bb;' % (B_W - 0.8, B_W - 0.8), "basis_inseln")["elements"])
    bn = []
    for el in bel:
        t = el.get("tags", {})
        name = t.get("name:en") or t.get("name") if t.get("place") in ("sea", "island") else t.get("name")
        if not name: continue
        if t.get("place") == "town":
            try: pop = int(t.get("population", "0").replace(",", "").replace(".", ""))
            except Exception: pop = 0
            if pop < 40000: continue
        if t.get("place") == "island":
            b = el.get("bounds")
            if not b or km_ll((b["minlat"], b["minlon"]), (b["maxlat"], b["maxlon"])) < 25: continue
        if el["type"] == "node": lon, lat = el["lon"], el["lat"]
        elif "center" in el: lon, lat = el["center"]["lon"], el["center"]["lat"]
        elif "bounds" in el: b = el["bounds"]; lon, lat = (b["minlon"] + b["maxlon"]) / 2, (b["minlat"] + b["maxlat"]) / 2
        else: continue
        x, y = lokal(lon, lat)
        if not basis.enthaelt(x, y, 5): continue
        art = t.get("place")
        rang = 1 if art in ("city", "sea", "island") else 2
        bn.append([round(x, 3), round(y, 3), round(basis.hoehe(x, y)), name, art, rang])
    basis.namen = bn
    print("  Namen Grundkarte:", len(bn))

    # ---------- Detailstuecke ----------
    nah = []
    for gi, g in enumerate(gruppen):
        pk = [punkte[i]["ll"] for i in g["pk"]]
        # Kasten in km um die Orte, dann in Grad
        lat_m = sum(p[0] for p in pk) / len(pk)
        kx = 111.2 * math.cos(math.radians(lat_m))
        la0, la1 = min(p[0] for p in pk), max(p[0] for p in pk)
        lo0, lo1 = min(p[1] for p in pk), max(p[1] for p in pk)
        hy = max(NAH_HALB_MIN, (la1 - la0) * 111.2 / 2 + NAH_RAND_KM)
        hx = max(NAH_HALB_MIN * 1.25, (lo1 - lo0) * kx / 2 + NAH_RAND_KM)
        cla, clo = (la0 + la1) / 2, (lo0 + lo1) / 2
        kasten = (clo - hx / kx, clo + hx / kx, cla - hy / 111.2, cla + hy / 111.2)
        name = "nah-%02d" % gi
        print(name, "um %.3f %.3f" % (cla, clo), "%.0f x %.0f km" % (2 * hx, 2 * hy))
        el = osm_nah(kasten, name + "_%.3f_%.3f" % (cla, clo))
        wasser = [e for e in el if e.get("tags", {}).get("natural") == "water" and e["type"] in ("way", "relation") and e.get("geometry") or e.get("members")]
        wasser = [e for e in wasser if e.get("tags", {}).get("natural") == "water"]
        gl = Gelaende(name, kasten, NAH_ZOOM, NAH_STUFEN, 1.1, 0.6, 400, seen_osm=wasser, see_min_km2=0.01,
                      min_flaeche=(10, 6), tex_km=4.0, tex_schritt=1)
        gl.namen = namen_aus(el, gl, kasten)
        gl.linien = linien_aus(el, gl)
        datei = os.path.join(DATEN, name + ".bin.gz")
        gl.schreiben(datei)
        nah.append({"g": gl, "datei": "daten/" + name + ".bin.gz", "pk": g["pk"]})
        print("   Namen %d, Linien %d" % (len(gl.namen), len(gl.linien)))

    # ---------- Hoehe: im Detailstueck, sonst Grundkarte ----------
    def hoehe(x, y):
        for n in nah:
            if n["g"].enthaelt(x, y, 0.5): return n["g"].hoehe(x, y)
        return basis.hoehe(x, y)

    # ---------- Kamerapunkte und Routen ----------
    kp = []
    for p in punkte:
        x, y = lokal(p["ll"][1], p["ll"][0])
        kp.append([round(x, 3), round(y, 3), round(hoehe(x, y) / 1000, 4)])

    def osrm(a, b):
        url = "https://router.project-osrm.org/route/v1/driving/%.5f,%.5f;%.5f,%.5f?overview=full&geometries=geojson" % (a[1], a[0], b[1], b[0])
        f = os.path.join(CACHE, "osrm_%.5f_%.5f_%.5f_%.5f.json" % (a[1], a[0], b[1], b[0]))
        try:
            d = json.loads(holen(url, f))
            time.sleep(0.2)
            r = d["routes"][0]
            return r["geometry"]["coordinates"], r["distance"] / 1000
        except Exception as e:
            print("   osrm ->", e); return None, None

    wege, arten = [], []
    for i in range(len(punkte) - 1):
        a, b = punkte[i]["ll"], punkte[i + 1]["ll"]
        gerade = km_ll(a, b)
        co, lang = (None, None) if gerade < 0.6 else osrm(a, b)
        # Boot, Wanderung, Faehre ohne Strasse: der Router macht dann riesige
        # Umwege (oder findet nichts) -- dort die Luftlinie.
        if co is None or lang > 2.2 * gerade + 4:
            art = "luft"
            n = max(2, int(gerade / 0.2))
            pk = [lokal(a[1] + (b[1] - a[1]) * j / n, a[0] + (b[0] - a[0]) * j / n) for j in range(n + 1)]
        else:
            art = "strasse"
            pk = [lokal(c[0], c[1]) for c in co]
            pk = [kp[i][:2]] + pk + [kp[i + 1][:2]]
        ls = LineString(pk).simplify(0.015) if len(pk) > 2 else LineString(pk)
        wege.append([[round(x, 3), round(y, 3), round(hoehe(x, y) / 1000, 4)] for x, y in ls.coords])
        arten.append(art)
        print("  Weg %2d -> %2d: %6.1f km Luftlinie, %s, %d Punkte" % (i, i + 1, gerade, art, len(wege[-1])))

    # ---------- Ortsname je Station (naechster Name aus OSM) ----------
    def ortsname(x, y):
        beste = None
        for n in nah:
            if not n["g"].enthaelt(x, y): continue
            for nm in n["g"].namen:
                if nm[4] not in ("city", "town", "village", "hamlet", "suburb", "island", "islet", "lighthouse", "museum",
                                 "beach", "bay", "cape", "isolated_dwelling", "locality", "peak", "water", "neighbourhood"): continue
                d = math.hypot(nm[0] - x, nm[1] - y)
                gew = d * (0.7 if nm[4] in ("lighthouse", "museum", "beach", "cape") else 1.0) * (0.8 if nm[5] <= 2 else 1.0)
                if d < 3.0 and (beste is None or gew < beste[0]): beste = (gew, nm[3])
        return beste[1] if beste else ""

    st_aus = []
    for s in stationen:
        p = kp[s["punkt"]]
        st_aus.append({"p": s["punkt"], "f": [f["id"] for f in s["f"]], "name": ortsname(p[0], p[1])})
    reise = {
        "rahmen": {"c": C, "ursprung": [OX, OY]},
        "basis": "daten/basis.bin.gz",
        "nah": [{"datei": n["datei"], "mitte": [round(n["g"].mitte[0], 4), round(n["g"].mitte[1], 4)],
                 "groesse": [round(n["g"].groesse[0], 4), round(n["g"].groesse[1], 4)], "punkte": n["pk"]} for n in nah],
        "punkte": kp, "wege": wege, "wegArt": arten, "stationen": st_aus, "ohne": ohne,
    }
    basis.schreiben(os.path.join(DATEN, "basis.bin.gz"))
    with open(os.path.join(SEITE, "reise.js"), "w", encoding="utf-8") as fh:
        fh.write("/* erzeugt von werkzeug/reise_bau.py */\nwindow.REISE = ")
        json.dump(reise, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    groesse = sum(os.path.getsize(os.path.join(DATEN, f)) for f in os.listdir(DATEN))
    print("Fertig: %d Stationen, %d Kamerapunkte, Daten %.1f MB" % (len(st_aus), len(kp), groesse / 1e6))


if __name__ == "__main__":
    main()
