"""Bilder der Zeitleiste ("Wer ich bin") fuer das Netz aufbereiten.

Quelle: Images/Timeline Bilder/  ->  Ziel: Images/web/timeline/<name>-<breite>.webp

- EXIF wird nicht uebernommen (Aufnahmeort, Geraet).
- Gedreht wird nach der EXIF-Ausrichtung -- ausser bei IMG_8077: dort steht
  im EXIF "90 Grad", die Pixel liegen aber schon richtig (Luis: "muss korrekt
  gedreht werden"; quer ist die Fernbedienung waagerecht).
- IMG_7612: Kennzeichen unkenntlich (verpixelt und weichgezeichnet).
- Die Standbilder als PNG haben schwarze Balken vom Video: abgeschnitten.
- Videos (.MOV): stumme Schleife als MP4 (H.264, ohne Ton) plus Standbild,
  Images/web/timeline/<name>.mp4 und <name>-standbild.webp.

Aufruf: python werkzeug/timeline_bau.py
"""
import os
import json
import subprocess
import imageio_ffmpeg
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
# Dateinamen, die nichts ueber den Inhalt sagen (oder irrefuehren: das
# Venedig-Bild heisst "Sommer2025", stammt aber aus dem Video vom 16.08.2024)
NAMEN = {
    "Sommer2025.jpg": "ranui-2025",
    "Sommer2025(bitte eher klein reinmachen).png": "alm-drohne-2025",
    "Sommer2025(bitte eher klein machen).png": "venedig-2024",
    "molli_mai2025.png": "molli-mai-2025",
    "IMG_3940.MOV": "molli-zug-2025",
    "IMG_1287.MOV": "venedig-2024-film",
}
# Video: Breite der langen Seite, Sekunde fuer das Standbild
FILME = {"IMG_3940.MOV": (720, 1.0), "IMG_1287.MOV": (720, 3.0)}
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def name_fuer(datei):
    if datei in NAMEN:
        return NAMEN[datei]
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


def film(datei, masse):
    """Stumme Schleife (ffmpeg dreht nach der Ausrichtung im Video selbst)."""
    lang, sek = FILME[datei]
    name = name_fuer(datei)
    quelle = os.path.join(QUELLE, datei)
    skal = f"scale='if(gt(iw,ih),{lang},-2)':'if(gt(iw,ih),-2,{lang})'"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", quelle, "-an", "-map_metadata", "-1",
                    "-vf", skal + ",fps=30", "-c:v", "libx264", "-preset", "slow", "-crf", "29",
                    "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart",
                    os.path.join(ZIEL, name + ".mp4")], check=True)
    bild = os.path.join(ZIEL, name + "-standbild.png")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(sek), "-i", quelle, "-frames:v", "1",
                    "-vf", skal, bild], check=True)
    im = Image.open(bild).convert("RGB")
    im.save(os.path.join(ZIEL, name + "-standbild.webp"), "WEBP", quality=QUALITAET, method=6)
    os.remove(bild)
    r, g, b = im.resize((1, 1), Image.BOX).getpixel((0, 0))
    masse[name] = [im.width, im.height, "#%02x%02x%02x" % (r, g, b)]
    print(name, im.size, "Film")


def main():
    os.makedirs(ZIEL, exist_ok=True)
    masse = {}
    for datei in sorted(os.listdir(QUELLE)):
        pfad = os.path.join(QUELLE, datei)
        if datei in FILME:
            film(datei, masse)
            continue
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
