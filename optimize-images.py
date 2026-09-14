"""
Erzeugt aus den Originalbildern in Images/ optimierte Web-Versionen in Images/web/.

Aufruf:  python optimize-images.py

Pro Quellbild entstehen mehrere Breiten (800/1600/2400 px) jeweils als WebP und als
JPEG-Fallback. Die Originale in Images/ werden nicht verändert -- sie bleiben das Archiv.
Neue Bilder einfach in Images/ legen und das Skript erneut ausführen.

Projektunterordner werden mitgenommen und in Images/web/ gespiegelt, z. B.
    Images/Gärtnerei Finder/5.5_5.5.1.jpg  ->  Images/web/gaertnerei-finder/5-5-5-5-1-800.webp
"""

import json
import re
import unicodedata
from pathlib import Path

from PIL import Image

SRC_DIR = Path(__file__).parent / "Images"
OUT_DIR = SRC_DIR / "web"

WIDTHS = [800, 1600, 2400]

# Die Vorschaubilder der Projektliste werden nur 96-132 px breit dargestellt.
# Mit der 800er-Fassung laedt ein Handy dort das Achtfache dessen, was es zeigt
# -- gemessen 280 KB fuer fuenf Kacheln. 400 px decken auch Bildschirme mit
# dreifacher Punktdichte ab (132 px x 1,05 Vergroesserung beim Ueberfahren x 3).
THUMB_WIDTH = 400
THUMB_SLUGS = {
    "still-2025-09-02-204030-6-3-1",
    "stephen-am-strand",
    "still-2025-08-22-215508-4-2-1",
    "2-19-2-19-1",
    "img-2100",
}
# Hoch angesetzt: das Material lebt von weichen Verlaeufen (Nebel, Himmel,
# Gegenlicht). Genau dort erzeugt staerkere Kompression sichtbare Stufen.
WEBP_QUALITY = 92
JPEG_QUALITY = 90
SOURCE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}

# Die Lichthoefe hinter Bildern und Videos nehmen die Farbe des jeweiligen
# Bildes an. Diese Farbe wurde bisher im Browser ermittelt: Bild auf ein 8x8
# grosses Feld zeichnen, mitteln, saettigen. Das Mitteln selbst dauert 0,2 ms --
# aber das erste Zeichnen eines Bildes auf ein Canvas zwingt den Browser, das
# entpackte Bild umzurechnen, und das kostete auf einem vierfach gedrosselten
# Prozessor 40 ms im Mittel und bis zu 100 ms je Bild. Genau das waren die
# Haenger beim Scrollen, sobald neue Bilder nachluden.
# Die Farbe aendert sich nie. Einmal hier ausgerechnet und als glow.json
# mitgeliefert, kostet sie den Besucher nichts.
GLOW_DATEI = "glow.json"
GLOW_SAETTIGUNG = 2.15   # muss zu "kraft" in index.html passen
GLOW_HELLIGKEIT = 1.5    # muss zu "hell"  in index.html passen


def glow_farbe(img: "Image.Image") -> str:
    """Mittlere Bildfarbe, angehoben -- dieselbe Rechnung wie im Browser."""
    # BOX auf 8x8 ist der Flaechenmittelwert, also genau das, was das
    # Gegenstueck im Browser annaehert.
    klein = img.resize((8, 8), Image.BOX)
    pixel = list(klein.getdata())
    n = len(pixel)
    r = sum(p[0] for p in pixel) / n
    g = sum(p[1] for p in pixel) / n
    b = sum(p[2] for p in pixel) / n
    mitte = (r + g + b) / 3
    werte = []
    for v in (r, g, b):
        x = (mitte + (v - mitte) * GLOW_SAETTIGUNG) * GLOW_HELLIGKEIT
        werte.append(max(0, min(255, round(x))))
    return ",".join(str(v) for v in werte)



def slugify(name: str) -> str:
    """'Aufnahme Stephen im Cottage' -> 'aufnahme-stephen-im-cottage'"""
    text = name.lower()
    for umlaut, replacement in UMLAUTS.items():
        text = text.replace(umlaut, replacement)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def human(num_bytes: int) -> str:
    return f"{num_bytes / 1024 / 1024:.1f} MB" if num_bytes >= 1024 * 1024 else f"{num_bytes / 1024:.0f} KB"


def target_dir(path: Path) -> Path:
    """Spiegelt den Unterordner der Quelle nach Images/web/ (Dateien direkt in
    Images/ landen flach in Images/web/)."""
    relative_parent = path.parent.relative_to(SRC_DIR)
    out = OUT_DIR.joinpath(*(slugify(part) for part in relative_parent.parts))
    out.mkdir(parents=True, exist_ok=True)
    return out


def process(path: Path, glows: dict) -> tuple[int, int]:
    """Gibt (Quellgröße, Summe der erzeugten Größen) zurück."""
    out_dir = target_dir(path)
    slug = slugify(path.stem)
    written = 0

    with Image.open(path) as img:
        img = img.convert("RGB")

        # Schluessel ist der Web-Pfad ohne "-<Breite>.<Endung>", genau so, wie er
        # im src-Attribut steht -- so findet die Seite ihn unabhaengig davon,
        # welche Breite der Browser aus dem srcset gewaehlt hat. Bewusst aus den
        # Ordnernamen zusammengesetzt statt aus OUT_DIR abgeleitet: sonst haengt
        # der Schluessel daran, ob OUT_DIR absolut gesetzt ist.
        unterordner = [slugify(teil) for teil in path.parent.relative_to(SRC_DIR).parts]
        glows["/".join(["Images", "web"] + unterordner + [slug])] = glow_farbe(img)

        # Nie hochskalieren. Ist die Quelle schmaler als die groesste Zielbreite,
        # kommt zusaetzlich ihre native Breite dazu -- sonst haette z. B. ein
        # 1500-px-Export nur die 800er-Variante und wuerde gross unscharf wirken.
        widths = [w for w in WIDTHS if w <= img.width]
        if img.width < max(WIDTHS) and img.width not in widths:
            widths.append(img.width)
        if slug in THUMB_SLUGS and THUMB_WIDTH <= img.width:
            widths.insert(0, THUMB_WIDTH)

        for width in widths:
            height = round(img.height * width / img.width)
            resized = img.resize((width, height), Image.LANCZOS)

            webp = out_dir / f"{slug}-{width}.webp"
            resized.save(webp, "WEBP", quality=WEBP_QUALITY, method=6)

            jpeg = out_dir / f"{slug}-{width}.jpg"
            resized.save(jpeg, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)

            written += webp.stat().st_size + jpeg.stat().st_size
            print(f"    {width:>5}px  webp {human(webp.stat().st_size):>8}   jpg {human(jpeg.stat().st_size):>8}")

    return path.stat().st_size, written


def main() -> None:
    if not SRC_DIR.is_dir():
        raise SystemExit(f"Ordner nicht gefunden: {SRC_DIR}")

    sources = sorted(
        p for p in SRC_DIR.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SOURCE_SUFFIXES
        and OUT_DIR not in p.parents  # bereits erzeugte Web-Versionen überspringen
    )
    if not sources:
        raise SystemExit(f"Keine Bilder in {SRC_DIR} gefunden.")

    OUT_DIR.mkdir(exist_ok=True)

    glows: dict[str, str] = {}
    total_src = total_out = 0
    for path in sources:
        print(f"\n{path.relative_to(SRC_DIR)}  ({human(path.stat().st_size)})")
        src_size, out_size = process(path, glows)
        total_src += src_size
        total_out += out_size

    glow_pfad = OUT_DIR / GLOW_DATEI
    glow_pfad.write_text(
        json.dumps(dict(sorted(glows.items())), ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    print(f"Lichthof-Farben: {len(glows)} Eintraege -> {glow_pfad.relative_to(SRC_DIR.parent)}")

    print(f"\n{len(sources)} Bilder verarbeitet.")
    print(f"Originale:  {human(total_src)}")
    print(f"Web-Größen: {human(total_out)}  (alle Breiten und Formate zusammen)")
    print(f"Ausgabe in: {OUT_DIR}")


if __name__ == "__main__":
    main()
