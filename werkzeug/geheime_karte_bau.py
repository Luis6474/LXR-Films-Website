# -*- coding: utf-8 -*-
"""Baut die Fotos der versteckten Reisekarte (The Same Gray).

Liest die Handyfotos aus Images/Bilder geheime Karte/ (bleibt lokal, siehe
.git/info/exclude), rechnet jedes in drei WebP-Groessen um und schreibt
fotos.js mit Zeit und Ort. Die Web-Fassungen tragen keine EXIF-Daten mehr.

    python werkzeug/geheime_karte_bau.py

Ein zweiter Lauf rechnet nur neue Fotos; Web-Fassungen von Fotos, die aus
dem Ordner geloescht wurden, werden entfernt.
"""
import os, sys, json, glob, datetime
from multiprocessing import Pool
import pillow_heif
from PIL import Image, ImageOps
pillow_heif.register_heif_opener()

WURZEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUELLE = os.path.join(WURZEL, "Images", "Bilder geheime Karte")
ZIEL = os.path.join(WURZEL, "sg-reise-7q4m2x")
BILDER = os.path.join(ZIEL, "bilder")
GROESSEN = (400, 1000, 2400)          # lange Kante in Pixeln
ENDUNGEN = (".heic", ".jpg", ".jpeg")  # PNG sind Bildschirmfotos
# Nicht veroeffentlichen: Strafzettel mit Kennzeichen und Aktenzeichen.
AUSSCHLUSS = {"IMG_7348.HEIC"}
# Fotos in Hamburg nur auf die Stadt genau zeigen (Wohnort).
HAMBURG, HAMBURG_R = (53.55, 10.0), 0.3


def grad(w, ref):
    g = float(w[0]) + float(w[1]) / 60 + float(w[2]) / 3600
    return -g if ref in ("S", "W") else g


def lesen(pfad):
    im = Image.open(pfad)
    ex = im.getexif()
    gps, sub = ex.get_ifd(0x8825), ex.get_ifd(0x8769)
    zeit = sub.get(0x9003) or ex.get(0x0132)
    versatz = sub.get(0x9011)                  # z. B. "+01:00": Zeitzone des Telefons
    lat = lon = None
    if gps and 2 in gps and 4 in gps:
        lat, lon = grad(gps[2], gps.get(1, "N")), grad(gps[4], gps.get(3, "E"))
    return im, zeit, versatz, lat, lon


def bearbeiten(pfad):
    name = os.path.splitext(os.path.basename(pfad))[0]
    ziele = [os.path.join(BILDER, "%s-%d.webp" % (name, g)) for g in GROESSEN]
    im, zeit, versatz, lat, lon = lesen(pfad)
    im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size
    if not all(os.path.exists(z) and os.path.getmtime(z) >= os.path.getmtime(pfad) for z in ziele):
        for g, z in zip(GROESSEN, ziele):
            k = im.copy()
            k.thumbnail((g, g), Image.LANCZOS)
            k.save(z, "WEBP", quality=80 if g > 400 else 72, method=6)
    return dict(id=name, zeit=zeit, versatz=versatz, lat=lat, lon=lon, w=w, h=h)


def main():
    ordner = [os.path.join(QUELLE, n) for n in os.listdir(QUELLE) if os.path.isdir(os.path.join(QUELLE, n))]
    dateien = sorted(p for o in ordner + [QUELLE] for p in glob.glob(os.path.join(o, "*"))
                     if os.path.splitext(p)[1].lower() in ENDUNGEN and os.path.basename(p) not in AUSSCHLUSS)
    os.makedirs(BILDER, exist_ok=True)
    with Pool(max(1, os.cpu_count() - 1)) as pool:
        fotos = []
        for i, f in enumerate(pool.imap(bearbeiten, dateien)):
            fotos.append(f)
            if i % 20 == 0: print(i, "/", len(dateien), flush=True)

    # verwaiste Web-Fassungen entfernen
    soll = {"%s-%d.webp" % (f["id"], g) for f in fotos for g in GROESSEN}
    for z in glob.glob(os.path.join(BILDER, "*.webp")):
        if os.path.basename(z) not in soll: os.remove(z)

    def versatz(f):
        v = f.get("versatz")
        if not v: return datetime.timedelta(hours=1)
        s = -1 if v[0] == "-" else 1
        return s * datetime.timedelta(hours=int(v[1:3]), minutes=int(v[4:6]))

    def zeitpunkt(f):
        # Weltzeit (UTC): das Telefon stellt beim Grenzuebertritt die Uhr um,
        # nach Ortszeit sortiert liefe die Reise an der Faehre rueckwaerts.
        if not f["zeit"]: return None
        return datetime.datetime.strptime(f["zeit"], "%Y:%m:%d %H:%M:%S") - versatz(f)

    mit_ort = [f for f in fotos if f["lat"] is not None and f["zeit"]]
    aus = []
    for f in fotos:
        t, ungefaehr = zeitpunkt(f), False
        lat, lon = f["lat"], f["lon"]
        if lat is None and t is not None and mit_ort:
            # ohne GPS: Ort des zeitlich naechsten Fotos
            n = min(mit_ort, key=lambda g: abs((zeitpunkt(g) - t).total_seconds()))
            lat, lon, ungefaehr = n["lat"], n["lon"], True
        if lat is not None and abs(lat - HAMBURG[0]) < HAMBURG_R and abs(lon - HAMBURG[1]) < HAMBURG_R * 2:
            lat, lon, ungefaehr = HAMBURG[0], HAMBURG[1], True
        e = dict(id=f["id"], w=f["w"], h=f["h"])
        if t:
            # Angezeigt wird die Ortszeit des Landes, in dem das Foto entstand:
            # Grossbritannien UTC+1 (Sommerzeit), das Festland UTC+2.
            gb = lon is not None and lon < 1.6
            e["t"] = (t + datetime.timedelta(hours=1 if gb else 2)).strftime("%Y-%m-%dT%H:%M")
            e["u"] = int(t.replace(tzinfo=datetime.timezone.utc).timestamp() // 60)
        if lat is not None: e["ort"] = [round(lat, 4), round(lon, 4)]
        if ungefaehr: e["ca"] = 1
        aus.append(e)
    aus.sort(key=lambda e: e.get("u", 1e12))
    with open(os.path.join(ZIEL, "fotos.js"), "w", encoding="utf-8") as fh:
        fh.write("/* erzeugt von werkzeug/geheime_karte_bau.py */\nwindow.FOTOS = ")
        json.dump(aus, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    groesse = sum(os.path.getsize(z) for z in glob.glob(os.path.join(BILDER, "*.webp")))
    print("Fotos", len(aus), "mit Ort", sum(1 for e in aus if "ort" in e), "Web-Bilder %.1f MB" % (groesse / 1e6))


if __name__ == "__main__":
    sys.exit(main())
