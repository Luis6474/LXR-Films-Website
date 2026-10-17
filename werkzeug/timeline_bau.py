"""Bilder der Zeitleiste ("Wer ich bin") fuer das Netz aufbereiten.

Quelle: Images/Timeline Bilder/  ->  Ziel: Images/web/timeline/<name>-<breite>.webp

- EXIF wird nicht uebernommen (Aufnahmeort, Geraet).
- Gedreht wird nach der EXIF-Ausrichtung -- ausser bei IMG_8077: dort steht
  im EXIF "90 Grad", die Pixel liegen aber schon richtig (Luis: "muss korrekt
  gedreht werden"; quer ist die Fernbedienung waagerecht).
- IMG_7612: Kennzeichen unkenntlich (verpixelt und weichgezeichnet).
- Die Standbilder als PNG haben schwarze Balken vom Video: abgeschnitten.

Aufruf: python werkzeug/timeline_bau.py
"""
import os
import json
import pillow_heif
from PIL import Image, ImageOps, ImageFilter

pillow_heif.register_heif_opener()

WURZEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUELLE = os.path.join(WURZEL, "Images", "Timeline Bilder")
ZIEL = os.path.join(WURZEL, "Images", "web", "timeline")
BREITEN = (800, 1600)
QUALITAET = 92

ROH_LASSEN = {"IMG_8077.JPEG"}                       # EXIF-Drehung ignorieren
ZENSUR = {"IMG_7612.JPEG": [(880, 795, 978, 840)]}   # Kennzeichen (x0, y0, x1, y1)


def name_fuer(datei):
    stamm = os.path.splitext(datei)[0].lower().replace(" ", "-")
    if stamm.startswith("dji_fly_"):
        return "dji-2023-05-19"
    return stamm.replace("_", "-")


def balken_weg(im):
    """Schwarze Raender (Videobalken) abschneiden."""
    grau = im.convert("L").point(lambda v: 255 if v > 18 else 0)
    box = grau.getbbox()
    return im.crop(box) if box else im


def zensieren(im, boxen):
    for box in boxen:
        teil = im.crop(box)
        w, h = teil.size
        teil = teil.resize((max(1, w // 14), max(1, h // 14)), Image.BILINEAR).resize((w, h), Image.NEAREST)
        teil = teil.filter(ImageFilter.GaussianBlur(3))
        im.paste(teil, box[:2])
    return im


def main():
    os.makedirs(ZIEL, exist_ok=True)
    masse = {}
    for datei in sorted(os.listdir(QUELLE)):
        pfad = os.path.join(QUELLE, datei)
        try:
            im = Image.open(pfad)
        except Exception:
            continue
        if datei not in ROH_LASSEN:
            im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        if datei in ZENSUR:
            im = zensieren(im, ZENSUR[datei])
        if datei.lower().endswith(".png"):
            im = balken_weg(im)
        name = name_fuer(datei)
        for b in BREITEN:
            kopie = im.copy()
            if kopie.width > b:
                kopie = kopie.resize((b, round(kopie.height * b / kopie.width)), Image.LANCZOS)
            kopie.save(os.path.join(ZIEL, f"{name}-{b}.webp"), "WEBP", quality=QUALITAET, method=6)
        # Mittlere Farbe (fuer das Leuchten hinter der Station)
        r, g, b = im.resize((1, 1), Image.BOX).getpixel((0, 0))
        masse[name] = [im.width, im.height, "#%02x%02x%02x" % (r, g, b)]
        print(name, im.size)
    with open(os.path.join(ZIEL, "masse.json"), "w", encoding="utf-8") as f:
        json.dump(masse, f, indent=1)


if __name__ == "__main__":
    main()
