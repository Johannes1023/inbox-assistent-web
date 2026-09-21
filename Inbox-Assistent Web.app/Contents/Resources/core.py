"""Local file helpers for the Inbox Assistant. No network calls live here."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps


PLACEHOLDER = "Unbekannt"


@dataclass(frozen=True)
class InboxItem:
    id: str
    path: Path
    kind: str
    fallback_date: str
    date_origin: str


def _exif_date(path: Path) -> str | None:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            for tag in (36867, 36868, 306):
                value = exif.get(tag)
                if value:
                    return datetime.strptime(str(value)[:19], "%Y:%m:%d %H:%M:%S").date().isoformat()
    except (OSError, ValueError, TypeError):
        pass
    # ImageIO/Spotlight often preserve the original capture date for HEIC files.
    try:
        result = subprocess.run(
            ["/usr/bin/mdls", "-raw", "-name", "kMDItemContentCreationDate", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", result.stdout)
        if result.returncode == 0 and match:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date().isoformat()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def fallback_date(path: Path) -> tuple[str, str]:
    captured = _exif_date(path)
    if captured:
        return captured, "Aufnahme-/Scandatum"
    stat = path.stat()
    created = getattr(stat, "st_birthtime", None) or stat.st_mtime
    return datetime.fromtimestamp(created).date().isoformat(), "Dateierstellungsdatum"


def _clean_component(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value)
    return re.sub(r"\s+", " ", value).strip(" .")


def sanitize_component(value: str, max_length: int = 90) -> str:
    return (_clean_component(value) or PLACEHOLDER)[:max_length].rstrip(" .")


def is_blank_component(value: str) -> bool:
    """True, wenn nach der Bereinigung nichts übrig bleibt.

    Nicht dasselbe wie "Ergebnis ist PLACEHOLDER": ein Absender, der wirklich
    "Unbekannt" heißt, ist gültig.
    """
    return not _clean_component(value)


def validate_date(value: str) -> str:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_image(path: Path) -> Image.Image:
    try:
        with Image.open(path) as image:
            return ImageOps.exif_transpose(image).convert("RGB")
    except (OSError, ValueError):
        with tempfile.TemporaryDirectory(prefix="inbox-image-") as temp_dir:
            converted = Path(temp_dir) / "converted.jpg"
            result = subprocess.run(["/usr/bin/sips", "-s", "format", "jpeg", str(path), "--out", str(converted)], capture_output=True, text=True)
            if result.returncode != 0:
                raise ValueError(f"Foto kann nicht gelesen werden: {path.name}")
            with Image.open(converted) as image:
                return ImageOps.exif_transpose(image).convert("RGB")
