# -*- coding: utf-8 -*-
"""Baut eine gezeichnete Reisekarte fuer index.html: gestapelte Hoehen-
schichten als Dreiecksnetz, Strassenrouten (OSRM, ueber Land), Seen aus
OpenStreetMap und ein Abstandsbild fuer die Wasserlinien.

Jede Karte hat eine Einstellungsdatei in werkzeug/karten/:
  Ausschnitt, Hoehenstufen, Glaettung, Orte (mit Zwischenpunkten "ueber",
  damit die Route auf der gewuenschten Strecke bleibt), Seen ja/nein.

Aufruf:  python werkzeug/karte_bau.py werkzeug/karten/schottland.json daten/schottland-karte.bin.gz
Braucht: numpy pillow scipy contourpy shapely mapbox_earcut"""
import gzip, io, json, math, os, struct, sys, tempfile, time, urllib.parse, urllib.request
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
import contourpy
from shapely.geometry import Polygon, LineString
from shapely.validation import make_valid
import mapbox_earcut as earcut

CFG = json.load(open(sys.argv[1], encoding="utf-8"))
ZIEL = sys.argv[2]
CACHE = os.path.join(tempfile.gettempdir(), "lxr_dem_cache"); os.makedirs(CACHE, exist_ok=True)
Z = CFG.get("zoom", 9)
W_LON, E_LON, S_LAT, N_LAT = CFG["ausschnitt"]
LEVELS = CFG["stufen"]                       # unter null: Tiefenlinien im Meer
C = 40075.0 * math.cos(math.radians((S_LAT + N_LAT) / 2))   # km je Mercator-Einheit
HOCH = max(LEVELS) + 400


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
px0 = int((x0 * n - tx0) * 256); py0 = int((y0 * n - ty0) * 256)
px1 = int((x1 * n - tx0) * 256); py1 = int((y1 * n - ty0) * 256)
h = h[py0:py1, px0:px1]
print("Raster", h.shape)

# Fehlpunkte weg (Median), Meer flach -- die Glaettung kommt nach den Seen.
h = ndimage.median_filter(h, size=3)
h = np.clip(h, -400, HOCH)


def lonlat2pix(lon, lat):
    mx, my = merc(lon, lat)
    return mx * n * 256 - tx0 * 256 - px0, my * n * 256 - ty0 * 256 - py0


def pix2welt(px, py):
    mx = (tx0 * 256 + px0 + px) / (n * 256)
    my = (ty0 * 256 + py0 + py) / (n * 256)
    return mx * C, -my * C


ox, oy = pix2welt(h.shape[1] / 2, h.shape[0] / 2)      # Ursprung: Mitte
Q = 100.0                                              # 1 Einheit = 10 m
px_km = (pix2welt(h.shape[1], 0)[0] - pix2welt(0, 0)[0]) / h.shape[1]


def overpass(q, art):
    """OpenStreetMap abfragen; die Antwort wird zwischengespeichert, und ist
    ein Server ueberlastet, wird der naechste gefragt."""
    cf = os.path.join(CACHE, "%s_%s" % (art, os.path.basename(sys.argv[1])))
    if not os.path.exists(cf) or os.path.getsize(cf) == 0:
        antwort = None
        for server in ("https://overpass-api.de/api/interpreter",
                       "https://overpass-api.de/api/interpreter",
                       "https://overpass.private.coffee/api/interpreter",
                       "https://overpass-api.de/api/interpreter",
                       "https://overpass.kumi.systems/api/interpreter"):
            try:
                req = urllib.request.Request(server, data=urllib.parse.urlencode({"data": q}).encode(),
                                             headers={"User-Agent": "lxr-films-karte/1.0"})
                antwort = urllib.request.urlopen(req, timeout=300).read()
                json.loads(antwort)            # nur gueltige Antworten merken
                break
            except Exception as e:
                print(art, server, "->", e); antwort = None; time.sleep(5)
        if antwort is None: raise SystemExit("%s nicht abrufbar" % art)
        open(cf, "wb").write(antwort)
    return json.load(open(cf, encoding="utf-8"))


# ---------- Seen (OpenStreetMap) ----------
# Grosse Seen werden in die Hoehen "eingeebnet": flach auf ihrer Wasser-
# hoehe, damit die Terrassen des Ufers nicht ueber sie wachsen. Ausserdem
# werden sie als eigene Flaechen gespeichert, auf denen der Browser das
# Wasser mit seinen Linien zeichnet.
seen = []
seemaske = np.zeros(h.shape, bool)
if CFG.get("seen"):
    # Nur benannte Seen: in Ebenen wie der Po-Ebene gibt es sonst tausende
    # Teiche, und der Server bricht die Abfrage ab.
    # Nur Seen mit Wikidata-Eintrag: das sind die bekannten; Teiche und
    # Becken (in der Po-Ebene zu Tausenden) fallen weg, und die Abfrage ist
    # klein genug fuer einen einzigen Durchgang.
    q = ('[out:json][timeout:240];(way["natural"="water"]["water"="lake"]["wikidata"](%f,%f,%f,%f);'
         'relation["natural"="water"]["water"="lake"]["wikidata"](%f,%f,%f,%f););out geom;'
         % (S_LAT, W_LON, N_LAT, E_LON, S_LAT, W_LON, N_LAT, E_LON))
    osm = overpass(q, "seenwd")
    for el in osm["elements"]:
        if el["type"] == "way":
            ringe_ll = [[(p["lon"], p["lat"]) for p in el.get("geometry", [])]]
        else:
            # Grosse Seen sind aus mehreren Uferstuecken zusammengesetzt.
            # Frueher wurde jedes Stueck als eigener See genommen -- aus
            # halben Ufern entstanden falsche Flaechen (bei The South als
            # Streifen und "Meer" auf dem Festland). Jetzt werden die Stuecke
            # zu geschlossenen Ringen verbunden.
            from shapely.ops import polygonize, linemerge
            stuecke = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
                       for m in el.get("members", [])
                       if m.get("role") == "outer" and m.get("geometry") and len(m["geometry"]) > 1]
            ringe_ll = [list(poly.exterior.coords) for poly in polygonize(linemerge(stuecke))] if stuecke else []
        for rl in ringe_ll:
            if len(rl) < 4: continue
            pw = Polygon([welt(*p) for p in rl])
            if not pw.is_valid: pw = make_valid(pw)
            if pw.area < CFG.get("seeMinKm2", 2.0) or pw.geom_type != "Polygon": continue
            name = el.get("tags", {}).get("name", "")
            pix = [lonlat2pix(*p) for p in rl]
            img = Image.new("L", (h.shape[1], h.shape[0]), 0)
            ImageDraw.Draw(img).polygon(pix, fill=1)
            m = np.asarray(img).astype(bool)
            if m.sum() < 3: continue
            pegel = float(np.median(h[m]))
            seen.append({"name": name, "poly": pw, "maske": m, "pegel": pegel})
            seemaske |= m
    print("Seen:", ", ".join("%s (%.0f km², %.0f m)" % (s["name"] or "?", s["poly"].area, s["pegel"]) for s in seen))

for s in seen:
    h[s["maske"]] = s["pegel"]

# ---------- Kueste aus OpenStreetMap (wo die Hoehen allein nicht reichen) ----------
# An flachen Kuesten liegt Land unter dem Meeresspiegel (bei Venedig die
# trockengelegten Flaechen von Polesine und Po-Delta, bis -3 m), und ueber
# nassem Gelaende hat der Radarsatellit Messfehler als Streifen. Die Hoehen
# koennen Land und Lagune dort nicht trennen. Fuer diesen Abschnitt wird
# deshalb die Kuestenlinie aus OpenStreetMap genommen: dahinter Land, davor
# Meer (die Lagune liegt in OSM ausserhalb der Kueste und bleibt Wasser).
landmaske = meermaske = None
if CFG.get("kueste"):
    from shapely.geometry import box
    from shapely.ops import polygonize, unary_union
    kw, ke, ks, kn = CFG["kueste"]
    ko = overpass('[out:json][timeout:240];way["natural"="coastline"](%f,%f,%f,%f);out geom;' % (ks, kw, kn, ke), "kueste")
    # Die Kuestenlinie wird als Mauer ins Raster gezeichnet; Meer ist, was
    # vom offenen Meer (tiefer als -10 m) aus erreichbar ist, ohne sie zu
    # ueberqueren. Alles andere im Abschnitt ist Land -- auch Ebenen unter
    # null. (Frueher: Flaechen aus abgeschnittenen Kuestenstuecken, dabei
    # wurden Teile der tiefen Ebene faelschlich zu Meer.)
    mauer = Image.new("L", (h.shape[1], h.shape[0]), 0)
    zeichner = ImageDraw.Draw(mauer)
    for el in ko["elements"]:
        if el["type"] == "way" and len(el.get("geometry", [])) > 1:
            zeichner.line([lonlat2pix(p["lon"], p["lat"]) for p in el["geometry"]], fill=1, width=2)
    mauer = np.asarray(mauer).astype(bool)
    x0p, y0p = lonlat2pix(kw, kn); x1p, y1p = lonlat2pix(ke, ks)
    abschnitt = np.zeros(h.shape, bool)
    abschnitt[max(0, int(y0p)):int(y1p) + 1, max(0, int(x0p)):int(x1p) + 1] = True
    teile, anzahl = ndimage.label(abschnitt & ~mauer)
    # Meer: zusammenhaengende Teile, die ueberwiegend tief sind. Einzelne
    # tiefe Messfehler in der Ebene machen sie nicht zu Meer.
    anteil_tief = ndimage.mean((h < -5).astype(float), labels=teile, index=np.arange(1, anzahl + 1))
    groesse = ndimage.sum(np.ones(h.shape), labels=teile, index=np.arange(1, anzahl + 1))
    offen = [i + 1 for i in range(anzahl) if anteil_tief[i] > 0.5 and groesse[i] > 200]
    meermaske = np.isin(teile, offen)
    # Lagunen (Venedig) sind in OSM eigene Wasserflaechen hinter der Kueste:
    # ebenfalls Wasser.
    lo = overpass('[out:json][timeout:240];(way["water"="lagoon"](%f,%f,%f,%f);relation["water"="lagoon"](%f,%f,%f,%f););out geom;'
                  % (ks, kw, kn, ke, ks, kw, kn, ke), "lagune")
    from shapely.ops import linemerge
    lagbild = Image.new("L", (h.shape[1], h.shape[0]), 0)
    for el in lo["elements"]:
        if el["type"] == "way":
            ringe_l = [[(p["lon"], p["lat"]) for p in el.get("geometry", [])]]
        else:
            st = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]]) for m in el.get("members", [])
                  if m.get("role") == "outer" and m.get("geometry") and len(m["geometry"]) > 1]
            ringe_l = [list(x.exterior.coords) for x in polygonize(linemerge(st))] if st else []
        for rl in ringe_l:
            if len(rl) > 3: ImageDraw.Draw(lagbild).polygon([lonlat2pix(*q) for q in rl], fill=1)
    lagune = np.asarray(lagbild).astype(bool)
    print("Lagunen: %d Punkte" % lagune.sum())
    meermaske |= lagune & abschnitt
    landmaske = abschnitt & ~meermaske & ~mauer
    print("Kueste: Meer %.0f %%, Land %.0f %% des Abschnitts"
          % (100 * meermaske.sum() / max(1, abschnitt.sum()), 100 * landmaske.sum() / max(1, abschnitt.sum())))


def kueste_setzen():
    if meermaske is None: return
    h[meermaske & (h > -0.6)] = -0.6
    h[landmaske & (h < 1.0)] = 1.0


kueste_setzen()
h = ndimage.gaussian_filter(h, CFG.get("glaettung", 1.6))
kueste_setzen()
for s in seen:                                       # nach dem Weichzeichnen wieder flach
    h[s["maske"]] = s["pegel"]


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


def ablegen(ringlist_welt, v0):
    """Ringe (Weltkoordinaten in km) triangulieren und anhaengen; Indizes
    zaehlen ab v0 (dem ersten Punkt der Schicht)."""
    allp = np.vstack(ringlist_welt)
    ends = np.cumsum([len(r) for r in ringlist_welt]).astype(np.uint32)
    idx = earcut.triangulate_float64(allp, ends)
    base = len(verts) - v0
    for p in allp:
        verts.append((round((p[0] - ox) * Q), round((p[1] - oy) * Q)))
    tris.extend(int(i) + base for i in idx)
    # Kanten werden nicht gespeichert: die Punkte liegen Ring fuer Ring
    # hintereinander, der Browser verbindet sie selbst.
    ringe.extend(len(r) for r in ringlist_welt)


def in_welt(r):
    return np.array([pix2welt(p[0], p[1]) for p in r])


FEHLT = [0.0, 0.0]
def pruefen(poly, rl):
    """Deckt die Zerlegung die Flaeche? (Summe der Dreiecke gegen Flaeche)"""
    allp = np.vstack(rl)
    ends = np.cumsum([len(r) for r in rl]).astype(np.uint32)
    idx = np.asarray(earcut.triangulate_float64(allp, ends)).reshape(-1, 3)
    a, b, c = allp[idx[:, 0]], allp[idx[:, 1]], allp[idx[:, 2]]
    flaeche = np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])).sum() / 2
    FEHLT[0] += abs(poly.area - flaeche); FEHLT[1] += poly.area


layers = []
gen = contourpy.contour_generator(z=h, fill_type=contourpy.FillType.OuterOffset, line_type=contourpy.LineType.Separate)
TOL = CFG.get("vereinfachung", 0.9)       # in Pixeln
for lev in LEVELS:
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
        poly = make_valid(poly.simplify(TOL, preserve_topology=True))
        geoms = [poly] if poly.geom_type == "Polygon" else [g for g in getattr(poly, "geoms", []) if g.geom_type == "Polygon"]
        for g in geoms:
            if g.is_empty or g.area < 3:
                continue
            ringlist = [chaikin(np.asarray(g.exterior.coords)[:-1])] + \
                       [chaikin(np.asarray(r.coords)[:-1]) for r in g.interiors if Polygon(r).area > 3]
            # Das Abrunden kann an engen Stellen Kanten erzeugen, die sich
            # kreuzen -- dann fehlen beim Zerlegen in Dreiecke ganze Stuecke
            # (bei The South stand die Ebene um Treviso deshalb als "Meer" da).
            # Solche Formen werden repariert und in gueltige Teile zerlegt.
            rund = Polygon(ringlist[0], ringlist[1:])
            if rund.is_valid:
                teile = [(rund, ringlist)]
            else:
                rep = make_valid(rund)
                teile = []
                for x in ([rep] if rep.geom_type == "Polygon" else getattr(rep, "geoms", [])):
                    if x.geom_type != "Polygon" or x.area < 1: continue
                    teile.append((x, [np.asarray(x.exterior.coords)[:-1]] + [np.asarray(q.coords)[:-1] for q in x.interiors]))
            for form, rl in teile:
                neu = sum(len(r) for r in rl)
                # Eine Schicht darf hoechstens 65 000 Punkte haben (16-Bit-Indizes);
                # grosse Ausschnitte bekommen deshalb mehrere Teile je Hoehe.
                if len(verts) - v0 + neu > 65000 and len(verts) > v0:
                    layers.append({"lev": lev, "v": [v0, len(verts)], "t": [t0, len(tris)], "r": [r0, len(ringe)]})
                    v0, t0, r0 = len(verts), len(tris), len(ringe)
                ablegen([in_welt(r) for r in rl], v0)
                pruefen(form, rl)
    layers.append({"lev": lev, "v": [v0, len(verts)], "t": [t0, len(tris)], "r": [r0, len(ringe)]})
    print("Schicht %5d m: bis %6d Punkte" % (lev, len(verts)))


def terrasse(v):
    lev = LEVELS[0]
    for L in LEVELS:
        if v >= L + 0.5: lev = L
    return lev


see_kopf = []
for s in seen:
    v0, t0, r0 = len(verts), len(tris), len(ringe)
    g = s["poly"].simplify(0.05)
    if g.geom_type != "Polygon" or g.is_empty: continue
    ablegen([chaikin(np.asarray(g.exterior.coords)[:-1], 1)], v0)
    see_kopf.append({"name": s["name"], "lev": terrasse(s["pegel"]), "v": [v0, len(verts)],
                     "t": [t0, len(tris)], "r": [r0, len(ringe)]})

print("Zerlegung: %.3f %% der Flaeche nicht gedeckt" % (100 * FEHLT[0] / max(1, FEHLT[1])))
assert max(L["v"][1] - L["v"][0] for L in layers + see_kopf) < 65536
vx = np.array(verts, dtype=np.int32)
assert np.abs(vx).max() < 32767, np.abs(vx).max()


# ---------- Hoehe nachschlagen (fuer Route und Orte) ----------
def hoehe_bei(wx, wy):
    mx = wx / C; my = -wy / C
    px = mx * n * 256 - tx0 * 256 - px0; py = my * n * 256 - ty0 * 256 - py0
    ix = min(max(int(px), 0), h.shape[1] - 1); iy = min(max(int(py), 0), h.shape[0] - 1)
    return float(max(0.0, h[iy, ix]))


def schicht_bei(wx, wy):
    return max(0, terrasse(hoehe_bei(wx, wy)))


# ---------- Routen ----------
def osrm(pkte):
    q = ";".join("%.5f,%.5f" % (p[0], p[1]) for p in pkte)
    url = "https://router.project-osrm.org/route/v1/driving/%s?overview=full&geometries=geojson&steps=true" % q
    d = json.load(urllib.request.urlopen(url))
    r = d["routes"][0]
    modi = set(s["mode"] for leg in r["legs"] for s in leg["steps"])
    return r["geometry"]["coordinates"], r["distance"] / 1000, modi


def weg(coords):
    pts = [welt(c[0], c[1]) for c in coords]
    ls = LineString(pts).simplify(CFG.get("routeVereinfachung", 0.06))
    return [[round(x - ox, 3), round(y - oy, 3), round(schicht_bei(x, y) / 1000, 3)] for x, y in ls.coords]


legs = []
bahnhoefe = []
if CFG.get("bahn"):
    # Die Route folgt den Gleisen aus OpenStreetMap: aus allen Abschnitten
    # wird ein Netz gebaut, und von Ort zu Ort der kuerzeste Weg darauf
    # gesucht. Bahnhoefe und Halte, die direkt am Gleis liegen, werden mit
    # gespeichert (die Karte setzt dort kleine Striche wie auf einem
    # Streckenplan).
    import heapq
    B = CFG["bahn"]
    osm = overpass('[out:json][timeout:180];(way%s(%f,%f,%f,%f);node["railway"~"station|halt"](%f,%f,%f,%f););out geom;'
                   % (B["filter"], S_LAT, W_LON, N_LAT, E_LON, S_LAT, W_LON, N_LAT, E_LON), "bahn")
    nb = {}
    def kante(a, b):
        wa, wb = welt(*a), welt(*b)
        d = math.hypot(wa[0] - wb[0], wa[1] - wb[1])
        nb.setdefault(a, []).append((b, d)); nb.setdefault(b, []).append((a, d))
    gleise = [el for el in osm["elements"] if el["type"] == "way"]
    for w in gleise:
        g = [(round(p["lon"], 7), round(p["lat"], 7)) for p in w["geometry"]]
        for a, b in zip(g[:-1], g[1:]): kante(a, b)
    knoten = list(nb.keys())
    def naechster(ll):
        wx, wy = welt(*ll)
        return min(knoten, key=lambda k: (welt(*k)[0] - wx) ** 2 + (welt(*k)[1] - wy) ** 2)
    def kuerzester(a, b):
        dist, vor, q = {a: 0.0}, {}, [(0.0, a)]
        while q:
            d, k = heapq.heappop(q)
            if k == b: break
            if d > dist.get(k, 1e18): continue
            for m, l in nb[k]:
                nd = d + l
                if nd < dist.get(m, 1e18):
                    dist[m] = nd; vor[m] = k; heapq.heappush(q, (nd, m))
        pfad = [b]
        while pfad[-1] != a: pfad.append(vor[pfad[-1]])
        return pfad[::-1], dist[b]
    for a, b in zip(CFG["orte"][:-1], CFG["orte"][1:]):
        pfad, km = kuerzester(naechster(a["lngLat"]), naechster(b["lngLat"]))
        print("%s -> %s: %.1f km auf den Gleisen" % (a["ort"], b["ort"], km))
        legs.append(weg(pfad))
    # Halte am Gleis (bis B["bahnhofM"] Meter entfernt), entlang der Route
    gl = LineString([welt(*p) for leg in [[(p[0], p[1]) for p in kuerzester(naechster(CFG["orte"][0]["lngLat"]),
                                                                              naechster(CFG["orte"][-1]["lngLat"]))[0]]] for p in leg])
    for el in osm["elements"]:
        if el["type"] != "node" or not el.get("tags", {}).get("name"): continue
        wx, wy = welt(el["lon"], el["lat"])
        from shapely.geometry import Point
        if gl.distance(Point(wx, wy)) * 1000 <= B.get("bahnhofM", 80):
            bahnhoefe.append([round(wx - ox, 3), round(wy - oy, 3), round(schicht_bei(wx, wy) / 1000, 3), el["tags"]["name"]])
    bahnhoefe.sort(key=lambda b: gl.project(Point(b[0] + ox, b[1] + oy)))
    print("Halte:", ", ".join(b[3] for b in bahnhoefe))
else:
    for a, b in zip(CFG["orte"][:-1], CFG["orte"][1:]):
        pk = [a["lngLat"]] + b.get("ueber", []) + [b["lngLat"]]
        co, km, modi = osrm(pk)
        print("%s -> %s: %.0f km, %s" % (a["ort"], b["ort"], km, modi))
        legs.append(weg(co))

orte = []
for o in CFG["orte"]:
    wx, wy = welt(*o["lngLat"])
    orte.append([round(wx - ox, 3), round(wy - oy, 3), round(schicht_bei(wx, wy) / 1000, 3)])

# ---------- Abstand zum Ufer (fuer die Wasserlinien) ----------
# Fuer jeden Wasserpunkt (Meer oder See) die Entfernung zum naechsten Land,
# in km, als Graustufenbild (0..255 fuer 0..texKm km). Halbe Aufloesung
# genuegt fuers Meer; Seen sind schmal und bekommen die volle.
TEX_KM = CFG.get("texKm", 14.0)
schritt = CFG.get("texSchritt", 2)
abst = ndimage.distance_transform_edt((h <= 0.5) | seemaske) * px_km
tex = np.clip(abst[::schritt, ::schritt] / TEX_KM * 255 + 0.5, 0, 255).astype(np.uint8)

kopf = {
    "q": Q, "ursprung": [ox, oy], "c": C, "stufen": LEVELS, "schichten": layers, "seen": see_kopf,
    "groesse": [round((pix2welt(h.shape[1], 0)[0] - pix2welt(0, 0)[0]), 2),
                round((pix2welt(0, 0)[1] - pix2welt(0, h.shape[0])[1]), 2)],
    "orte": orte, "wege": legs, "bahnhoefe": bahnhoefe, "tex": [int(tex.shape[1]), int(tex.shape[0])], "texKm": TEX_KM
}
kj = json.dumps(kopf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
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
with open(ZIEL, "wb") as f:
    f.write(gzip.compress(roh.getvalue(), 9, mtime=0))
print("Datei", ZIEL, os.path.getsize(ZIEL), "Bytes (ungepackt %d);" % len(roh.getvalue()),
      len(verts), "Punkte", len(tris) // 3, "Dreiecke")
