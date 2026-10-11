# -*- coding: utf-8 -*-
"""Baut zu einer Reisekarte ein Reliefbild: die Hangschattierung aus dem
Hoehenmodell, auf denselben Ausschnitt wie die Hoehenstufen. Die Karte legt
es ueber jede Stufe; so zeigen die flachen Terrassen die Hangform darunter,
statt wie gestapelte Platten zu wirken.

Aufruf:  python werkzeug/relief_bau.py werkzeug/karten/schottland.json daten/schottland-relief.webp
Braucht die Hoehenkacheln, die karte_bau.py zwischengespeichert hat."""
import json, math, os, sys, tempfile, urllib.request
import numpy as np
from PIL import Image
from scipy import ndimage

CFG = json.load(open(sys.argv[1], encoding="utf-8"))
ZIEL = sys.argv[2]
CACHE = os.path.join(tempfile.gettempdir(), "lxr_dem_cache"); os.makedirs(CACHE, exist_ok=True)
Z = CFG.get("zoom", 9)
W_LON, E_LON, S_LAT, N_LAT = CFG["ausschnitt"]
C = 40075.0 * math.cos(math.radians((S_LAT + N_LAT) / 2))


def merc(lon, lat):
    s = math.sin(math.radians(lat))
    return (lon + 180) / 360, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


# Dieselben Kacheln und derselbe Zuschnitt wie in karte_bau.py
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
h = ndimage.median_filter(h, size=3)
# Meer flach: dort zeigt die Karte Wasser, kein Relief
h = np.maximum(h, 0)
R = CFG.get("relief", {})
h = ndimage.gaussian_filter(h, R.get("glaettung", 0.8))

# Hangschattierung, Licht aus Nordwesten wie der Papierschatten
px_m = 1000 * C / (n * 256)                      # Meter je Bildpunkt
gy, gx = np.gradient(h * R.get("ueberhoehung", 2.0), px_m)
# Bildzeilen laufen nach Sueden: Nordrichtung ist -y
nx, ny, nz = -gx, gy, np.ones_like(h)
laenge = np.sqrt(nx * nx + ny * ny + nz * nz)
az, el = math.radians(315), math.radians(40)
L = (math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el))
licht = (nx * L[0] + ny * L[1] + nz * L[2]) / laenge
# 128 = ebener Grund; heller zum Licht, dunkler davon weg
wert = 128 + (licht - L[2]) * R.get("kontrast", 160)
bild = np.clip(wert + 0.5, 0, 255).astype(np.uint8)
# Kantenlaenge als Zweierpotenz: sonst gibt es unter WebGL 1 keine
# Mipmaps, und aus der Ferne flimmerte das Relief. Die Karte liest es
# ueber den Anteil, nicht ueber Bildpunkte -- das Strecken stoert nicht.
g = R.get("groesse", 2048)
bild = Image.fromarray(bild, "L").resize((g, g), Image.LANCZOS)
bild.save(ZIEL, quality=R.get("qualitaet", 82), method=6)
print("Relief", bild.size, os.path.getsize(ZIEL), "Bytes")
