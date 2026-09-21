"""Local file handling for the Inbox Assistant. No network calls live here."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageOps


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp", ".bmp"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}
STATE_FILE = ".inbox-assistent-state.json"
PLACEHOLDER = "Unbekannt"
PHOTO_ARCHIVE = "Archivierte Fotos"


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


def _pdf_date(path: Path) -> str | None:
    try:
        with fitz.open(path) as pdf:
            value = pdf.metadata.get("creationDate", "") or ""
        match = re.search(r"(?:D:)?(\d{4})(\d{2})(\d{2})", value)
        if match:
            return datetime.strptime("".join(match.groups()), "%Y%m%d").date().isoformat()
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def fallback_date(path: Path) -> tuple[str, str]:
    captured = _pdf_date(path) if path.suffix.lower() == ".pdf" else _exif_date(path)
    if captured:
        return captured, "Aufnahme-/Scandatum"
    stat = path.stat()
    created = getattr(stat, "st_birthtime", None) or stat.st_mtime
    return datetime.fromtimestamp(created).date().isoformat(), "Dateierstellungsdatum"


def scan_inbox(root: Path) -> list[InboxItem]:
    files = sorted(
        (p for p in root.iterdir() if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in SUPPORTED_EXTENSIONS),
        key=lambda p: (p.stat().st_mtime, p.name.casefold()),
    )
    items = []
    for index, path in enumerate(files, start=1):
        date, origin = fallback_date(path)
        items.append(InboxItem(f"F{index:04d}", path, "PDF" if path.suffix.lower() == ".pdf" else "Foto", date, origin))
    return items


def sanitize_component(value: str, max_length: int = 90) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or PLACEHOLDER)[:max_length].rstrip(" .")


def validate_date(value: str) -> str:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()


def _canonical_sender(root: Path, sender: str) -> str:
    requested = sanitize_component(sender, 70)
    def key(text: str) -> str:
        text = unicodedata.normalize("NFKD", text).casefold()
        return "".join(ch for ch in text if ch.isalnum())
    for folder in root.iterdir():
        if folder.is_dir() and folder.name != PHOTO_ARCHIVE and not folder.name.endswith(".app"):
            if key(folder.name) == key(requested):
                return folder.name
    return requested


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def group_digest(items: list[InboxItem]) -> str:
    hashes = sorted(hash_file(item.path) for item in items)
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest()


def load_state(root: Path) -> dict:
    path = root / STATE_FILE
    if not path.exists():
        return {"version": 1, "processed": [], "source_hashes": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == 1 and isinstance(data.get("processed"), list):
            data.setdefault("source_hashes", [])
            return data
    except (OSError, ValueError):
        pass
    raise ValueError(f"Statusdatei ist beschädigt: {path.name}. Bitte zuerst prüfen; keine Dateien wurden verschoben.")


def save_state(root: Path, state: dict) -> None:
    target = root / STATE_FILE
    fd, temp_name = tempfile.mkstemp(prefix=".inbox-state-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def duplicate_items(items: list[InboxItem], state: dict) -> list[InboxItem]:
    known = set(state.get("source_hashes", []))
    return [item for item in items if hash_file(item.path) in known]


def _unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    for number in range(2, 10000):
        candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Zu viele gleichnamige Dateien im Zielordner.")


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


def image_preview(path: Path, size: tuple[int, int] = (480, 560)) -> Image.Image:
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as pdf:
            if not len(pdf):
                raise ValueError("PDF enthält keine Seiten")
            page = pdf[0]
            scale = min(size[0] / page.rect.width, size[1] / page.rect.height, 1.5)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    else:
        image = _load_image(path)
    image.thumbnail(size)
    return image


def _write_photo_pdf(items: list[InboxItem], target: Path) -> None:
    pdf = fitz.open()
    try:
        for item in items:
            image = _load_image(item.path)
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=94, optimize=True)
            portrait = image.height >= image.width
            width, height = (595, 842) if portrait else (842, 595)
            page = pdf.new_page(width=width, height=height)
            area = fitz.Rect(18, 18, width - 18, height - 18)
            page.insert_image(area, stream=buffer.getvalue(), keep_proportion=True)
        pdf.set_metadata({"creator": "Inbox-Assistent"})
        pdf.save(target, garbage=3, deflate=True)
    finally:
        pdf.close()


def _check_space(items: list[InboxItem], root: Path) -> None:
    free = shutil.disk_usage(root).free
    required = sum(item.path.stat().st_size for item in items) * 2 + 100 * 1024 * 1024
    if free < required:
        raise OSError("Zu wenig freier Speicherplatz für eine sichere Verarbeitung.")


def process_document(root: Path, items: list[InboxItem], date: str, sender: str, title: str, state: dict) -> dict:
    """Commit one document locally; return status and paths. Source files are never overwritten."""
    if not items:
        raise ValueError("Keine Dateien ausgewählt.")
    if any(not item.path.is_file() for item in items):
        raise FileNotFoundError("Eine Quelldatei wurde seit dem Start verschoben.")
    if any(item.kind == "PDF" for item in items) and (len(items) != 1 or items[0].kind != "PDF"):
        raise ValueError("Ein vorhandenes PDF muss einzeln verarbeitet werden.")
    date = validate_date(date)
    sender = _canonical_sender(root, sender.strip())
    title = sanitize_component(title.strip(), 100)
    if sender == "Unbekannt" or title == "Unbekannt":
        raise ValueError("Absender und Titel müssen ausgefüllt sein.")
    digest = group_digest(items)
    if digest in {entry.get("digest") for entry in state["processed"]}:
        return {"status": "duplicate", "files": [item.path.name for item in items]}
    _check_space(items, root)
    base = f"{date} - {sender} - {title}"
    sender_dir = root / sender
    sender_dir.mkdir(exist_ok=True)
    pdf_target = _unique_path(sender_dir / f"{base}.pdf")
    source_hashes = [hash_file(item.path) for item in items]
    moved: list[tuple[Path, Path]] = []
    created_pdf = False
    try:
        if items[0].kind == "PDF":
            shutil.move(str(items[0].path), str(pdf_target))
            moved.append((pdf_target, items[0].path))
        else:
            fd, temp_name = tempfile.mkstemp(prefix=".inbox-pdf-", suffix=".pdf", dir=sender_dir)
            os.close(fd)
            temp_pdf = Path(temp_name)
            try:
                _write_photo_pdf(items, temp_pdf)
                os.replace(temp_pdf, pdf_target)
                created_pdf = True
            finally:
                temp_pdf.unlink(missing_ok=True)
            archive = root / PHOTO_ARCHIVE
            archive.mkdir(exist_ok=True)
            for number, item in enumerate(items, start=1):
                photo_target = _unique_path(archive / f"{base} - Seite {number:02d}{item.path.suffix.lower()}")
                shutil.move(str(item.path), str(photo_target))
                moved.append((photo_target, item.path))
        # Keep only hashes in the state file; document contents and names are not logged.
        entry = {"digest": digest, "source_hashes": source_hashes}
        state["processed"].append(entry)
        state["source_hashes"].extend(source_hashes)
        save_state(root, state)
        return {"status": "processed", "output": pdf_target, "archived": [src for src, _ in moved] if created_pdf else []}
    except Exception:
        for destination, original in reversed(moved):
            if destination.exists() and not original.exists():
                shutil.move(str(destination), str(original))
        if created_pdf:
            pdf_target.unlink(missing_ok=True)
        raise
