"""
Erzeugt aus den Originalbildern in Images/ optimierte Web-Versionen in Images/web/.

Aufruf:  python optimize-images.py

Pro Quellbild entstehen mehrere Breiten (800/1600/2400 px) jeweils als WebP und als
JPEG-Fallback. Die Originale in Images/ werden nicht verändert -- sie bleiben das Archiv.
Neue Bilder einfach in Images/ legen und das Skript erneut ausführen.

Projektunterordner werden mitgenommen und in Images/web/ gespiegelt, z. B.
    Images/Gärtnerei Finder/5.5_5.5.1.jpg  ->  Images/web/gaertnerei-finder/5-5-5-5-1-800.webp
"""

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


def process(path: Path) -> tuple[int, int]:
    """Gibt (Quellgröße, Summe der erzeugten Größen) zurück."""
    out_dir = target_dir(path)
    slug = slugify(path.stem)
    written = 0

    with Image.open(path) as img:
        img = img.convert("RGB")

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

    total_src = total_out = 0
    for path in sources:
        print(f"\n{path.relative_to(SRC_DIR)}  ({human(path.stat().st_size)})")
        src_size, out_size = process(path)
        total_src += src_size
        total_out += out_size

    print(f"\n{len(sources)} Bilder verarbeitet.")
    print(f"Originale:  {human(total_src)}")
    print(f"Web-Größen: {human(total_out)}  (alle Breiten und Formate zusammen)")
    print(f"Ausgabe in: {OUT_DIR}")


if __name__ == "__main__":
    main()
