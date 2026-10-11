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
                open(f, "wb").write(urllib.request.urlopen(url).read())
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


# Kuestenlinie aus OpenStreetMap (siehe oben, "Kueste")
KUESTE = None
if G.get("kueste"):
    KUESTE = [[(p["lon"], p["lat"]) for p in el["geometry"]]
              for el in json.load(open(os.path.join(CACHE, G["kueste"]), encoding="utf-8"))["elements"]
              if el.get("type") == "way" and len(el.get("geometry", [])) > 1]
    print("Kueste:", len(KUESTE), "Wege")


def bereich(name, z, box, form_px, relief_px):
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
        teile, anzahl = ndimage.label(~mauer)
        idx = np.arange(1, anzahl + 1)
        anteil = ndimage.mean(nass.astype(np.float32), labels=teile, index=idx)
        meer_id = np.zeros(anzahl + 1, bool); meer_id[1:] = anteil > G.get("meerAnteil", 0.12)
        land = ~meer_id[teile]
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
    # Kueste als Abstand (km, im Land positiv): bilinear gelesen ergibt das
    # bei jeder Naehe eine glatte, scharfe Kante; leicht geglaettet, damit
    # die Treppen des Rasters nicht als Zacken stehen bleiben.
    sdf = (ndimage.distance_transform_edt(land) - ndimage.distance_transform_edt(~land)) * km_px
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
    Image.fromarray(form, "RGB").save(ZIEL + "-form-%s.png" % name, optimize=True)
    if G.get(name, {}).get("kueste"):
        # Kueste fein (fuer den Rechner): nur das Abstandsfeld, in eigener Groesse
        kp = G[name]["kueste"]
        kf = np.clip(128 + auf(sdf, kp, 1) / SDF_KM * 127 + 0.5, 0, 255).astype(np.uint8)
        Image.fromarray(kf, "L").save(ZIEL + "-kueste-%s.png" % name, optimize=True)
        print("  ", ZIEL + "-kueste-%s.png" % name, os.path.getsize(ZIEL + "-kueste-%s.png" % name), "Bytes")
    for f in ("-form-%s.png",):
        print("  ", ZIEL + f % name, os.path.getsize(ZIEL + f % name), "Bytes")
    return {"rand": [round(v, 3) for v in rand]}


aus = {"hmax": HMAX, "sdfKm": G.get("sdfKm", 1.5)}
aus["innen"] = bereich("innen", G["innen"]["zoom"], G["innen"]["box"], G["innen"]["form"], G["innen"]["relief"])
aus["aussen"] = bereich("aussen", G["aussen"]["zoom"], CFG["ausschnitt"][0:1] + [CFG["ausschnitt"][2], CFG["ausschnitt"][1], CFG["ausschnitt"][3]],
                        G["aussen"]["form"], G["aussen"]["relief"])
print(json.dumps(aus))
