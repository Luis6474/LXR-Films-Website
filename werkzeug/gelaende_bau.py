# -*- coding: utf-8 -*-
"""Baut das Gelaende einer Reisekarte als echte Landschaft statt gestapelter
Hoehenplatten: je Bereich ein Formbild (Hoehe und Abstand zur Kueste) und ein
Schattenbild (Licht von Nordwesten, Taeler dunkler), beides fuer index.html.

Zwei Bereiche: "innen" (wo die Reise spielt, feine Hoehendaten) und "aussen"
(die ganze Karte, grober). Die Weltkoordinaten passen zur gebauten Karte
(gleicher Ursprung), deshalb wird deren Datei gelesen.

Aufruf:  python werkzeug/gelaende_bau.py werkzeug/karten/schottland.json daten/schottland-karte.bin.gz daten/schottland
         -> daten/schottland-form-innen.png, -relief-innen(-gross).webp, -kueste-innen.png,
            -form-aussen.png, -relief-aussen.webp, -abstand.png
Braucht: numpy pillow scipy

Kueste (Einstellung "kueste"): Datei im Zwischenspeicher %TEMP%/lxr_dem_cache im
Format einer Overpass-Antwort ({"elements": [{"type": "way", "geometry": [{lat, lon}, ...]}]}),
alle Wege mit natural=coastline. Overpass schafft das ganze Gebiet nicht (Zeitueberschreitung
bei den dichten Kuesten am Clyde); verlaesslich ist der Geofabrik-Auszug
(download.geofabrik.de/europe/united-kingdom/scotland-latest.osm.pbf), ausgelesen mit
pyosmium (Wege mit natural=coastline, locations=True). Nordirland fehlt darin, seine
Kueste kommt aus Overpass dazu."""
import gzip, json, math, os, struct, sys, tempfile, urllib.request
import numpy as np
from PIL import Image
from scipy import ndimage
from shapely.geometry import LineString
from shapely.ops import polygonize, linemerge

CFG = json.load(open(sys.argv[1], encoding="utf-8"))
roh = gzip.decompress(open(sys.argv[2], "rb").read())
KOPF = json.loads(roh[16:16 + struct.unpack("<I", roh[:4])[0]].decode("utf-8"))
ZIEL = sys.argv[3]
G = CFG["gelaende"]
CACHE = os.path.join(tempfile.gettempdir(), "lxr_dem_cache"); os.makedirs(CACHE, exist_ok=True)
C = KOPF["c"]; OX, OY = KOPF["ursprung"]
HMAX = G["hmax"]


def merc(lon, lat):
    s = math.sin(math.radians(lat))
    return (lon + 180) / 360, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def dem(z, w, s, e, n):
    """Hoehen (m) im Mercator-Raster der Zoomstufe z, dazu die Weltkoordinaten
    der Raender (x links, y oben, x rechts, y unten)."""
    N = 2 ** z
    x0, y0 = merc(w, n); x1, y1 = merc(e, s)
    tx0, ty0, tx1, ty1 = int(x0 * N), int(y0 * N), int(x1 * N), int(y1 * N)
    zeilen = []
    for ty in range(ty0, ty1 + 1):
        z_ = []
        for tx in range(tx0, tx1 + 1):
            f = os.path.join(CACHE, "%d_%d_%d.png" % (z, tx, ty))
            if not os.path.exists(f):
                url = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/%d/%d/%d.png" % (z, tx, ty)
                open(f + ".teil", "wb").write(urllib.request.urlopen(url).read())
                os.replace(f + ".teil", f)                   # nie eine halbe Kachel im Speicher
            a = np.asarray(Image.open(f).convert("RGB")).astype(np.float32)
            z_.append(a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768)
        zeilen.append(np.hstack(z_))
    h = np.vstack(zeilen)
    p0x = int((x0 * N - tx0) * 256); p0y = int((y0 * N - ty0) * 256)
    p1x = int((x1 * N - tx0) * 256); p1y = int((y1 * N - ty0) * 256)
    h = h[p0y:p1y, p0x:p1x]
    mx0 = (tx0 * 256 + p0x) / (N * 256); my0 = (ty0 * 256 + p0y) / (N * 256)
    mx1 = (tx0 * 256 + p1x) / (N * 256); my1 = (ty0 * 256 + p1y) / (N * 256)
    def pix(lon, lat):                       # Laenge/Breite -> Punkt in diesem Raster
        mx, my = merc(lon, lat)
        return mx * N * 256 - tx0 * 256 - p0x, my * N * 256 - ty0 * 256 - p0y
    return h, [mx0 * C - OX, -my0 * C - OY, mx1 * C - OX, -my1 * C - OY], pix


KARTE = os.path.splitext(os.path.basename(sys.argv[1]))[0]


def overpass(q, art):
    """OpenStreetMap abfragen (wie in karte_bau.py), die Antwort zwischenspeichern."""
    import time, urllib.parse
    cf = os.path.join(CACHE, "%s_gelaende_%s.json" % (art, KARTE))
    if not os.path.exists(cf) or os.path.getsize(cf) == 0:
        antwort = None
        for server in ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
                       "https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
            try:
                req = urllib.request.Request(server, data=urllib.parse.urlencode({"data": q}).encode(),
                                             headers={"User-Agent": "lxr-films-karte/1.0"})
                antwort = urllib.request.urlopen(req, timeout=400).read()
                json.loads(antwort)
                break
            except Exception as e:
                print(art, server, "->", e); antwort = None; time.sleep(5)
        if antwort is None: raise SystemExit("%s nicht abrufbar" % art)
        open(cf, "wb").write(antwort)
    return json.load(open(cf, encoding="utf-8"))


# Kuestenlinie aus OpenStreetMap (siehe oben, "Kueste"): eine Datei im
# Zwischenspeicher, oder ein Ausschnitt [West, Ost, Sued, Nord], der bei
# Overpass abgefragt wird (klein genug, etwa Sylt).
KUESTE = None
if G.get("kueste"):
    if isinstance(G["kueste"], list):
        kw, ke, ks, kn = G["kueste"]
        quelle = overpass('[out:json][timeout:120];way["natural"="coastline"](%f,%f,%f,%f);out geom;' % (ks, kw, kn, ke), "kueste")
    else:
        quelle = json.load(open(os.path.join(CACHE, G["kueste"]), encoding="utf-8"))
    KUESTE = [[(p["lon"], p["lat"]) for p in el["geometry"]]
              for el in quelle["elements"] if el.get("type") == "way" and len(el.get("geometry", [])) > 1]
    print("Kueste:", len(KUESTE), "Wege")


def seen_holen(name, box, nur_bekannte):
    """Seen aus OpenStreetMap als Ringe (Laenge, Breite): je See ein aeusserer
    Ring und seine Inseln. Im Reisegebiet alle stehenden Gewaesser (auch
    kleine Bergseen wie der Karersee), in der Uebersicht nur bekannte
    (mit Wikidata-Eintrag)."""
    w, s_, e, n = box
    f = '["natural"="water"]' + ('["wikidata"]' if nur_bekannte else '')
    osm = overpass('[out:json][timeout:180];(way%s(%f,%f,%f,%f);relation%s(%f,%f,%f,%f););out geom;'
                   % (f, s_, w, n, e, f, s_, w, n, e), "seen_" + name)
    aus = []
    for el in osm["elements"]:
        t = el.get("tags", {})
        if t.get("water") in ("river", "stream", "canal", "ditch", "wastewater", "moat", "lock", "stream_pool") or t.get("waterway"):
            continue
        if el["type"] == "way":
            g = el.get("geometry", [])
            if len(g) > 3: aus.append(([(p["lon"], p["lat"]) for p in g], []))
        else:
            ringe = {}
            for rolle in ("outer", "inner"):
                st = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]]) for m in el.get("members", [])
                      if m.get("role") == rolle and m.get("geometry") and len(m["geometry"]) > 1]
                ringe[rolle] = [list(x.exterior.coords) for x in polygonize(linemerge(st))] if st else []
            for r in ringe["outer"]:
                aus.append((r, ringe["inner"]))
    print("  Seen (%s): %d Flaechen in OpenStreetMap" % (name, len(aus)))
    return aus


def bereich(name, z, box, form_px, relief_px, kueste_px=None, seen=None, see_min_km2=0.02, inseln=False):
    h, rand, pix = dem(z, *box)
    print(name, "Raster", h.shape, "Zoom", z)
    h = ndimage.median_filter(h.astype(np.float32), size=3)
    km_px = (rand[2] - rand[0]) / h.shape[1]
    # Meer ist nur, was mit echtem Meeresgrund (unter -2 m) zusammenhaengt.
    # Die Hoehendaten haben im Land eckige Loecher mit Hoehe 0 -- sie waren
    # als kleine Kaestchen mit Kuestenlinie zu sehen. Sie werden aus den
    # Nachbarn gefuellt.
    nass = h <= 0.5
    teile, anzahl = ndimage.label(nass)
    tief = ndimage.minimum(h, labels=teile, index=np.arange(1, anzahl + 1))
    groesse = ndimage.sum(np.ones(h.shape), labels=teile, index=np.arange(1, anzahl + 1))
    meer_id = np.zeros(anzahl + 1, bool)
    # Meer: mit Meeresgrund, oder gross (in Zoom 12 haben Meeresarme wie
    # Loch Fyne oft keine Tiefe, nur 0). Die Loecher sind kleine Kaestchen.
    km2 = groesse * km_px * km_px
    meer_id[1:] = ((tief < -2) & (km2 > G.get("meerMinKm2", 30.0))) | (km2 > G.get("lochMaxKm2", 4.0))
    land = ~meer_id[teile]
    if KUESTE:
        # Die Hoehendaten kennen die Meeresarme nicht sicher: in Zoom 12 hat
        # Loch Fyne Hoehen von 10 bis 50 m, nur ein Uferstreifen liegt auf 0.
        # Deshalb die Kuestenlinie aus OpenStreetMap als Mauer ins Raster;
        # Meer ist jedes Gebiet zwischen den Mauern, das zu einem guten Teil
        # (Anteil "meerAnteil") auf Meereshoehe liegt. Die Mauer zaehlt zum Land.
        from PIL import ImageDraw
        bild = Image.new("L", (h.shape[1], h.shape[0]), 0); zeichner = ImageDraw.Draw(bild)
        for w in KUESTE:
            zeichner.line([pix(*q) for q in w], fill=1, width=2)
        mauer = np.asarray(bild).astype(bool)
        # Nur im Ausschnitt der Kuestenlinie (bei einem Ausschnitt wie Sylt);
        # ausserhalb bleibt Land und Meer aus den Hoehen -- sonst hinge dort
        # alles ohne Mauer zusammen und wuerde gemeinsam Land oder Meer.
        abschnitt = np.ones(h.shape, bool)
        if isinstance(G["kueste"], list):
            kw, ke, ks, kn = G["kueste"]
            (x0, y0), (x1, y1) = pix(kw, kn), pix(ke, ks)
            abschnitt[:] = False
            abschnitt[max(0, int(y0)):max(0, int(y1) + 1), max(0, int(x0)):max(0, int(x1) + 1)] = True
        if abschnitt.any():
            teile, anzahl = ndimage.label(abschnitt & ~mauer)
            idx = np.arange(1, anzahl + 1)
            anteil = ndimage.mean(nass.astype(np.float32), labels=teile, index=idx)
            meer_id = np.zeros(anzahl + 1, bool); meer_id[1:] = anteil > G.get("meerAnteil", 0.12)
            if isinstance(G["kueste"], list):
                # An flachen Kuesten (Wattenmeer: Sylt und die Marsch liegen
                # auf 0 bis 5 m) taugt der Anteil auf Meereshoehe nicht. Dort
                # stimmen die Gebiete ab: OpenStreetMap zeichnet jede Kueste
                # mit dem Land zur Linken. Ein Punkt knapp links jeder Strecke
                # zaehlt fuer Land, einer knapp rechts fuer Meer.
                stimme = np.zeros((anzahl + 1, 2))
                ab_grad = 3 * km_px / 111.0
                for w in KUESTE:
                    for (l0, b0), (l1, b1) in zip(w[:-1], w[1:]):
                        c = math.cos(math.radians(b0))
                        dx, dy = (l1 - l0) * c, b1 - b0
                        lg = math.hypot(dx, dy)
                        if lg == 0: continue
                        nx, ny = -dy / lg, dx / lg               # links der Richtung
                        lm, bm = (l0 + l1) / 2, (b0 + b1) / 2
                        for seite, vz in ((0, 1), (1, -1)):
                            px_, py_ = pix(lm + vz * nx * ab_grad / c, bm + vz * ny * ab_grad)
                            ix, iy = int(px_), int(py_)
                            if 0 <= iy < h.shape[0] and 0 <= ix < h.shape[1]:
                                stimme[teile[iy, ix], seite] += 1
                hat = stimme[1:].sum(1) > 0
                meer_id[1:] = np.where(hat, stimme[1:, 1] > stimme[1:, 0], meer_id[1:])
            land = np.where(abschnitt, ~meer_id[teile], land)
        h[~land] = np.minimum(h[~land], 0)
    loch = land & nass
    if name == "aussen" and G.get("abstand"):
        # Abstand zum Ufer fuer die Wasserlinien des Meeres, in der Groesse
        # des Abstandsbilds der Karte -- passend zu dieser Kueste statt der
        # groben aus der Karte.
        tw, th, tkm = KOPF["tex"][0], KOPF["tex"][1], KOPF["texKm"]
        ab = ndimage.distance_transform_edt(~land) * km_px
        ab = ndimage.zoom(ab, (th / ab.shape[0], tw / ab.shape[1]), order=1, grid_mode=True, mode="nearest")
        Image.fromarray(np.clip(ab / tkm * 255 + 0.5, 0, 255).astype(np.uint8), "L").save(ZIEL + "-abstand.png", optimize=True)
        print("  ", ZIEL + "-abstand.png", os.path.getsize(ZIEL + "-abstand.png"), "Bytes")
    if os.environ.get("LOCHBILD"):              # zur Kontrolle: gefuellte Loecher rot, Meer blau
        b = np.zeros(h.shape + (3,), np.uint8); b[land] = 90; b[~land, 2] = 160; b[loch] = (255, 0, 0)
        Image.fromarray(b).resize((1600, int(1600 * h.shape[0] / h.shape[1])), Image.NEAREST).save(os.environ["LOCHBILD"] + name + ".png")
    if loch.any():
        _, (iy, ix) = ndimage.distance_transform_edt(loch, return_indices=True)
        h[loch] = h[iy[loch], ix[loch]]
        h[loch] = np.maximum(ndimage.gaussian_filter(h, 2)[loch], 1.0)
    print("  Loecher gefuellt: %d Punkte (%.1f km2)" % (loch.sum(), loch.sum() * km_px * km_px))
    # Seen: flach auf ihrem Pegel (Median der Hoehen darin) und Wasser wie
    # das Meer -- nur malt der Browser sie selbst als Wasserflaeche (Blau im
    # Formbild sagt "hier liegt ein See"), statt sie wegzulassen.
    see = np.zeros(h.shape, bool)
    if seen:
        from PIL import ImageDraw
        for aussen_ring, inseln in seen:
            # nur im Rechteck um den See zeichnen (es sind tausende Flaechen)
            pa = [pix(*q) for q in aussen_ring]
            xs, ys = [q[0] for q in pa], [q[1] for q in pa]
            x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
            x1, y1 = min(h.shape[1], int(max(xs)) + 2), min(h.shape[0], int(max(ys)) + 2)
            if x1 <= x0 or y1 <= y0 or (x1 - x0) * (y1 - y0) * km_px * km_px < see_min_km2: continue
            bild = Image.new("L", (x1 - x0, y1 - y0), 0); zz = ImageDraw.Draw(bild)
            zz.polygon([(q[0] - x0, q[1] - y0) for q in pa], fill=1)
            for r in inseln: zz.polygon([(q[0] - x0, q[1] - y0) for q in (pix(*p) for p in r)], fill=0)
            m = np.asarray(bild).astype(bool) & land[y0:y1, x0:x1]
            if m.sum() * km_px * km_px < see_min_km2: continue
            ausschnitt = h[y0:y1, x0:x1]
            ausschnitt[m] = float(np.median(ausschnitt[m]))
            see[y0:y1, x0:x1] |= m
        print("  Seen: %.1f km2" % (see.sum() * km_px * km_px))
    trocken = land & ~see
    # Kueste als Abstand (km, im Land positiv): bilinear gelesen ergibt das
    # bei jeder Naehe eine glatte, scharfe Kante; leicht geglaettet, damit
    # die Treppen des Rasters nicht als Zacken stehen bleiben.
    sdf = (ndimage.distance_transform_edt(trocken) - ndimage.distance_transform_edt(~trocken)) * km_px
    sdf = ndimage.gaussian_filter(sdf, G.get("kuesteWeich", 1.2))

    # Licht: von Nordwesten, dazu ein weiches Himmelslicht. Die Hoehen
    # werden fuer die Schattierung so ueberhoeht wie auf der Karte.
    zf = G.get("ueberhoehung", 3.2) * G.get("relief", 0.7)
    hs = np.where(land, np.maximum(h, 0), 0)
    # An der Kueste weicher: die Stufe Land/Meer stand sonst als treppige Linie im Licht.
    nah = np.clip(1 - np.abs(sdf) / (3 * km_px), 0, 1)
    hs = ndimage.gaussian_filter(hs, G.get("weich", 0.6)) * (1 - nah) + ndimage.gaussian_filter(hs, 2.5) * nah
    gy, gx = np.gradient(hs / 1000 * zf, km_px)          # Norden ist oben (Zeile 0)
    nz = 1 / np.sqrt(1 + gx * gx + gy * gy)
    nx, ny = -gx * nz, gy * nz                           # Normale, y nach Norden
    def licht(az, alt):
        a, b = math.radians(az), math.radians(alt)
        lx, ly, lz = math.sin(a) * math.cos(b), math.cos(a) * math.cos(b), math.sin(b)
        return np.clip(nx * lx + ny * ly + nz * lz, 0, 1)
    sonne = licht(315, G.get("sonne", 38))
    fuell = licht(260, 55) * 0.5 + licht(10, 55) * 0.5
    # Nur ein feiner Rest Umgebungslicht (enge Kerben dunkler) -- grosse
    # Radien machten aus Huegeln weiche Kissen (Knete). Die Form kommt
    # aus dem gerichteten Licht.
    kerbe = np.tanh((hs - ndimage.gaussian_filter(hs, 0.4 / km_px)) / G.get("kerbe", 60.0))
    s = (0.72 * sonne + 0.13 * fuell + 0.15) * (1 + 0.18 * kerbe)
    s[~land] = np.median(s[land])                         # Meer: neutral (wird nicht gezeigt)
    s = np.clip(s, 0, 1.6) / 1.6

    def auf(a, px, ordnung=1):
        return ndimage.zoom(a, (px / a.shape[0], px / a.shape[1]), order=ordnung, grid_mode=True, mode="nearest")

    # Schattenbild in einer oder zwei Groessen (die grosse fuer den Rechner, "-gross")
    for i, px in enumerate(relief_px if isinstance(relief_px, list) else [relief_px]):
        rel = np.clip(auf(s, px, 3) * 255 + 0.5, 0, 255).astype(np.uint8)
        Image.fromarray(rel, "L").save(ZIEL + "-relief-%s%s.webp" % (name, "-gross" if i else ""), quality=G.get("qualitaet", 82), method=6)
        print("  ", ZIEL + "-relief-%s%s.webp" % (name, "-gross" if i else ""), os.path.getsize(ZIEL + "-relief-%s%s.webp" % (name, "-gross" if i else "")), "Bytes")
    hf = auf(np.clip(h, 0, HMAX), form_px, 1)
    sf = auf(sdf, form_px, 1)
    SDF_KM = G.get("sdfKm", 1.5)
    form = np.zeros((form_px, form_px, 3), np.uint8)
    form[..., 0] = np.clip(hf / HMAX * 255 + 0.5, 0, 255)
    form[..., 1] = np.clip(128 + sf / SDF_KM * 127 + 0.5, 0, 255)
    if inseln:
        # Halbes Blau (128): Inseln -- Land, das den Rand des Bereichs nicht
        # beruehrt. Schmale Landbruecken (Hindenburgdamm) zaehlen dafuer
        # nicht, sonst hinge Sylt am Festland. Der Browser malt dort Sand.
        offen = ndimage.binary_opening(trocken, iterations=max(2, int(0.08 / km_px)))
        teile_i, _ = ndimage.label(offen)
        rand_ids = np.unique(np.concatenate([teile_i[0], teile_i[-1], teile_i[:, 0], teile_i[:, -1]]))
        insel = (teile_i > 0) & ~np.isin(teile_i, rand_ids)
        insel = ndimage.binary_dilation(insel, iterations=max(2, int(0.08 / km_px)) + 2) & trocken
        ib = auf(insel.astype(np.float32), form_px, 1) > 0.5
        form[..., 2] = np.where(ndimage.binary_dilation(ib, iterations=2), 128, 0)
        print("  Inseln: %.0f km2" % (insel.sum() * km_px * km_px))
    if see.any():
        # Blau: See in der Naehe (etwas breiter als der See, damit sein Ufer
        # sicher darin liegt); dort laeuft das Netz nicht zum Meer hinab.
        sb = auf(see.astype(np.float32), form_px, 1) > 0.02
        form[..., 2] = np.where(ndimage.binary_dilation(sb, iterations=3), 255, form[..., 2])
    Image.fromarray(form, "RGB").save(ZIEL + "-form-%s.png" % name, optimize=True)
    if kueste_px:
        # Kueste fein (fuer den Rechner): nur das Abstandsfeld, in eigener Groesse
        kp = kueste_px
        kf = np.clip(128 + auf(sdf, kp, 1) / SDF_KM * 127 + 0.5, 0, 255).astype(np.uint8)
        Image.fromarray(kf, "L").save(ZIEL + "-kueste-%s.png" % name, optimize=True)
        print("  ", ZIEL + "-kueste-%s.png" % name, os.path.getsize(ZIEL + "-kueste-%s.png" % name), "Bytes")
    for f in ("-form-%s.png",):
        print("  ", ZIEL + f % name, os.path.getsize(ZIEL + f % name), "Bytes")
    return {"rand": [round(v, 3) for v in rand]}


# Bereiche: "bereiche" (Liste, je mit "name"), oder wie bei Schottland ein
# "innen". Dazu immer "aussen" ueber die ganze Karte. "seen": Seen aus
# OpenStreetMap, in den Bereichen alle ab "seeMinKm2", aussen nur bekannte
# ab "seeAussenKm2" (im Ausschnitt "seenBox" des Aussenbereichs).
BEREICHE = G.get("bereiche") or [dict(G["innen"], name="innen")]
aus = {"hmax": HMAX, "sdfKm": G.get("sdfKm", 1.5)}
NUR = os.environ.get("NUR", "").split(",") if os.environ.get("NUR") else None   # z.B. NUR=sylt: nur diese Bereiche neu
for b in BEREICHE:
    if NUR and b["name"] not in NUR: continue
    sn = seen_holen(b["name"], b["box"], False) if G.get("seen") else None
    aus[b["name"]] = bereich(b["name"], b["zoom"], b["box"], b["form"], b["relief"], b.get("kueste"),
                             sn, G.get("seeMinKm2", 0.02), b.get("inseln", False))
A = G["aussen"]
if NUR and "aussen" not in NUR: print(json.dumps(aus)); sys.exit()
box_a = CFG["ausschnitt"][0:1] + [CFG["ausschnitt"][2], CFG["ausschnitt"][1], CFG["ausschnitt"][3]]
sn = seen_holen("aussen", A.get("seenBox", box_a), True) if G.get("seen") else None
aus["aussen"] = bereich("aussen", A["zoom"], box_a, A["form"], A["relief"], A.get("kueste"), sn, G.get("seeAussenKm2", 2.0))
print(json.dumps(aus))
