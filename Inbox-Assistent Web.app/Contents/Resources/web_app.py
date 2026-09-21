"""Local, single-user browser UI for sorting photographed letters.

The HTTP server listens only on loopback. A random per-run token protects every
API request; browser clients never get arbitrary filesystem paths to operate on.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import uuid
import webbrowser
from collections import OrderedDict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen

import pymupdf
from PIL import Image, ImageFilter

from ai import classify
from core import (PLACEHOLDER, InboxItem, _load_image, fallback_date, hash_file,
                  sanitize_component, validate_date)


HERE = Path(__file__).resolve().parent
STATIC = HERE / "web_static"
EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}
MAX_IMPORT_SIZE = 120 * 1024 * 1024
MAX_JSON_SIZE = 1024 * 1024
DATA_DIR = Path(os.environ.get("INBOX_APP_DATA_DIR", Path.home() / "Library/Application Support/Inbox-Assistent"))
TESSERACT = shutil.which("tesseract") or "/opt/homebrew/bin/tesseract"


def _new_draft(folder: Path) -> dict:
    return {"folder": str(folder), "images": {}, "groups": [], "completed": []}


def _store_file() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "drafts.json"


def _load_store() -> dict:
    target = _store_file()
    if not target.exists():
        return {"version": 2, "drafts": {}}
    data = json.loads(target.read_text(encoding="utf-8"))
    if data.get("version") != 2 or not isinstance(data.get("drafts"), dict):
        raise ValueError("Der lokale Entwurf hat ein unbekanntes Format.")
    return data


def _quarantine_store() -> Path:
    """Verschiebt eine unlesbare Entwurfsdatei beiseite, ohne sie zu löschen."""
    source = _store_file()
    backup = source.with_name(f"drafts.defekt-{datetime.now():%Y%m%d-%H%M%S}.json")
    os.replace(source, backup)
    return backup


def _save_store(data: dict) -> None:
    target = _store_file()
    fd, name = tempfile.mkstemp(prefix=".draft-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _choose_folder() -> Path | None:
    script = 'POSIX path of (choose folder with prompt "Ordner mit Fotoseiten auswählen")'
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=120)
    if result.returncode:
        if "User canceled" in result.stderr or "-128" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or "Ordnerauswahl fehlgeschlagen.")
    folder = Path(result.stdout.strip()).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("Der ausgewählte Ordner ist nicht verfügbar.")
    return folder


def _photo_path(draft: dict, image: dict) -> Path:
    folder = Path(draft["folder"]).resolve()
    path = folder / image["name"]
    if path.parent.resolve() != folder:
        raise ValueError("Ungültiger Dateiname im Entwurf.")
    return path


def _known_senders(folder: Path) -> list[str]:
    """Absender aus bereits exportierten PDFs ({datum}_{absender}_{titel}.pdf).

    Gibt der Klassifikation die vorhandenen Schreibweisen an die Hand, damit
    dieselbe Organisation nicht jedes Mal anders benannt wird.
    """
    senders: dict[str, str] = {}
    try:
        entries = list(folder.glob("*.pdf"))
    except OSError:
        return []
    for entry in entries:
        parts = entry.stem.split("_")
        if len(parts) >= 3 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]):
            name = parts[1].strip()
            if name and name != PLACEHOLDER:
                senders.setdefault(name.casefold(), name)
    return sorted(senders.values())


def _all_grouped(draft: dict) -> set[str]:
    return {page for group in draft["groups"] for page in group["pages"]}


def _filename_base(group: dict) -> str:
    date = validate_date(group.get("date", ""))
    sender = sanitize_component(group.get("sender", "").strip(), 70)
    title = sanitize_component(group.get("title", "").strip(), 100)
    # Geprüft wird das Ergebnis der Bereinigung, nicht die Rohgabe: "..." oder "///"
    # werden sonst still zum Platzhalter und landen unbemerkt im Dateinamen.
    if PLACEHOLDER in (sender, title):
        raise ValueError("Datum, Organisation und Titel müssen ausgefüllt sein.")
    return f"{date}_{sender}_{title}"


def _readiness(group: dict, draft: dict) -> tuple[bool, str]:
    if not group["pages"]:
        return False, "Gruppe enthält keine Seiten."
    try:
        _filename_base(group)
    except ValueError as exc:
        return False, str(exc)
    if group.get("source") == "ai" and not group.get("confirmed"):
        return False, "KI-Vorschlag muss bestätigt werden."
    for image_id in group["pages"]:
        image = draft["images"].get(image_id)
        if not image:
            return False, "Eine Seite fehlt im Entwurf."
        path = _photo_path(draft, image)
        if not path.is_file():
            return False, f"Datei fehlt: {image['name']}"
        if path.stat().st_size != image["size"]:
            return False, f"Datei wurde geändert: {image['name']}"
    return True, ""


def _auto_deskew(image: Image.Image) -> tuple[float, bool]:
    """Conservative local text-line deskew; leaves uncertain pages unchanged."""
    sample = image.copy()
    sample.thumbnail((900, 900))
    grey = sample.convert("L").filter(ImageFilter.GaussianBlur(0.5))
    # A page needs enough dark marks to infer horizontal text lines.
    dark = grey.point(lambda p: 255 if p < 160 else 0)
    count = sum(1 for value in dark.getdata() if value)
    if count < grey.width * grey.height * 0.006:
        return 0.0, True
    best_angle, best_score = 0.0, -1.0
    runner_up = -1.0
    for step in range(-12, 13):
        angle = step * 0.5
        rotated = dark.rotate(angle, expand=False, fillcolor=0)
        rows = []
        pixels = rotated.load()
        stride = 3
        for y in range(0, rotated.height, stride):
            rows.append(sum(1 for x in range(0, rotated.width, stride) if pixels[x, y]))
        mean = sum(rows) / max(1, len(rows))
        score = sum((row - mean) ** 2 for row in rows) / max(1, len(rows))
        if score > best_score:
            runner_up = best_score
            best_angle, best_score = angle, score
        elif score > runner_up:
            runner_up = score
    if abs(best_angle) < 0.6:
        return 0.0, False
    # Low separation or a maximum at the search boundary is not trustworthy.
    if abs(best_angle) >= 6 or best_score < runner_up * 1.015:
        return 0.0, True
    return best_angle, False


def _auto_orientation(image: Image.Image) -> tuple[int, bool]:
    """Ask local Tesseract OSD for a quarter turn; never guess on weak evidence."""
    sample = image.copy()
    sample.thumbnail((1900, 1900))
    with tempfile.TemporaryDirectory(prefix="inbox-orient-") as folder:
        source = Path(folder) / "photo.png"
        sample.save(source, "PNG")
        try:
            result = subprocess.run([TESSERACT, str(source), "stdout", "--psm", "0", "-l", "osd"],
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return 0, True
    match = re.search(r"Rotate:\s*(0|90|180|270)\b", result.stdout)
    confidence = re.search(r"Orientation confidence:\s*([0-9.]+)", result.stdout)
    if result.returncode or not match or not confidence or float(confidence.group(1)) < 3:
        return 0, True
    # Tesseract's "Rotate" is clockwise; Pillow rotates counter-clockwise.
    return -int(match.group(1)), False


def _page_quad(image: Image.Image) -> list[float] | None:
    """Find a bright sheet on a darker background, only when its outline is clear."""
    sample = image.copy()
    sample.thumbnail((850, 850))
    grey = sample.convert("L")
    width, height = grey.size
    if min(width, height) < 200:
        return None
    pixels = grey.load()
    border = [pixels[x, y] for y in (2, height - 3) for x in range(0, width, 8)]
    border += [pixels[x, y] for x in (2, width - 3) for y in range(0, height, 8)]
    center = [pixels[x, y] for y in range(height // 3, 2 * height // 3, 9)
              for x in range(width // 3, 2 * width // 3, 9)]
    border.sort()
    center.sort()
    border_level = border[len(border) // 2]
    center_level = center[len(center) // 2]
    if center_level < 175 or center_level - border_level < 50:
        return None
    threshold = max(140, min(240, (border_level + center_level) // 2))
    mask = grey.point(lambda value: 255 if value > threshold else 0).filter(ImageFilter.MinFilter(5))
    marks = mask.load()
    points = [(x, y) for y in range(0, height, 2) for x in range(0, width, 2) if marks[x, y]]
    if len(points) < width * height * 0.08:
        return None
    tl = min(points, key=lambda p: p[0] + p[1])
    tr = max(points, key=lambda p: p[0] - p[1])
    br = max(points, key=lambda p: p[0] + p[1])
    bl = min(points, key=lambda p: p[0] - p[1])
    corners = [tl, tr, br, bl]
    area = abs(sum(corners[i][0] * corners[(i + 1) % 4][1] -
                   corners[(i + 1) % 4][0] * corners[i][1] for i in range(4))) / 2
    if not width * height * 0.35 <= area <= width * height * 0.93:
        return None
    if any(x < width * 0.025 or x > width * 0.975 or y < height * 0.025 or y > height * 0.975 for x, y in corners):
        return None
    # Include a small margin rather than clipping handwriting at the sheet edge.
    cx = sum(x for x, _ in corners) / 4
    cy = sum(y for _, y in corners) / 4
    expanded = []
    for x, y in corners:
        expanded.extend((max(0, min(1, (cx + (x - cx) * 1.015) / width)),
                         max(0, min(1, (cy + (y - cy) * 1.015) / height))))
    return expanded


def _apply_quad(image: Image.Image, quad: list[float]) -> Image.Image:
    if not quad or len(quad) != 8:
        return image
    tl, tr, br, bl = [(quad[i] * image.width, quad[i + 1] * image.height) for i in range(0, 8, 2)]
    distance = lambda a, b: ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    width = max(1, round((distance(tl, tr) + distance(bl, br)) / 2))
    height = max(1, round((distance(tl, bl) + distance(tr, br)) / 2))
    return image.transform((width, height), Image.Transform.QUAD,
                           (*tl, *bl, *br, *tr), resample=Image.Resampling.BICUBIC)


def _check_space(paths: list[Path], folder: Path) -> None:
    """PDF, OCR-Zwischenbild und umbenannte Fotos brauchen Platz im selben Ordner."""
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    required = sum(path.stat().st_size for path in paths if path.is_file()) * 3 + 100 * 1024 * 1024
    if free < required:
        raise OSError("Zu wenig freier Speicherplatz für eine sichere Verarbeitung.")


def _publish_new(temp: Path, target: Path) -> None:
    """Legt target mit dem Inhalt von temp an und überschreibt dabei niemals etwas.

    Bevorzugt ein Hardlink (atomar, schlägt bei vorhandenem Ziel fehl). exFAT/FAT
    auf USB-Sticks und SD-Karten kennen keine Hardlinks; dort wird exklusiv
    angelegt (O_EXCL) und kopiert.
    """
    try:
        os.link(temp, target)
        return
    except FileExistsError:
        raise
    except OSError:
        pass
    with open(target, "xb") as out:
        try:
            with open(temp, "rb") as source:
                shutil.copyfileobj(source, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            out.close()
            target.unlink(missing_ok=True)
            raise


def _render_image(path: Path, image_state: dict, thumbnail: bool = False) -> Image.Image:
    image = _load_image(path)
    auto_turn = int(image_state.get("auto_rotation", 0)) % 360
    if auto_turn:
        image = image.rotate(auto_turn, expand=True)
    if image_state.get("auto_quad"):
        image = _apply_quad(image, image_state["auto_quad"])
    angle = float(image_state.get("auto_angle", 0) or 0)
    if angle:
        image = image.rotate(angle, expand=True, fillcolor="white")
    turn = int(image_state.get("rotation", 0)) % 360
    if turn:
        image = image.rotate(turn, expand=True)
    if thumbnail:
        image.thumbnail((460, 580))
    return image


def _image_version(image: dict) -> str:
    """Kurzer Schlüssel für alles, was das gerenderte Bild beeinflusst."""
    merkmale = [image.get("sha256", ""), image.get("rotation", 0), image.get("auto_rotation", 0),
                image.get("auto_angle", 0), image.get("auto_quad")]
    return hashlib.sha256(json.dumps(merkmale).encode()).hexdigest()[:16]


# Gerenderte Vorschauen. Begradigen und Entzerren kosten pro Foto Hunderte
# Millisekunden; ohne Cache wurde bei jedem Neuzeichnen alles neu gerechnet.
_RENDER_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_RENDER_CACHE_LIMIT = 64 * 1024 * 1024
_render_cache_lock = threading.Lock()


def _render_jpeg(image_id: str, path: Path, photo: dict, thumbnail: bool) -> bytes:
    key = (image_id, _image_version(photo), thumbnail)
    with _render_cache_lock:
        if key in _RENDER_CACHE:
            _RENDER_CACHE.move_to_end(key)
            return _RENDER_CACHE[key]
    image = _render_image(path, photo, thumbnail=thumbnail)
    if not thumbnail:
        image.thumbnail((2200, 3000))
    stream = BytesIO()
    image.save(stream, "JPEG", quality=84)
    data = stream.getvalue()
    with _render_cache_lock:
        _RENDER_CACHE[key] = data
        while sum(len(value) for value in _RENDER_CACHE.values()) > _RENDER_CACHE_LIMIT and len(_RENDER_CACHE) > 1:
            _RENDER_CACHE.popitem(last=False)
    return data


def _write_pdf(draft: dict, group: dict, target: Path) -> list[str]:
    """Baut die PDF seitenweise und speichert sie direkt nach target.

    Früher wurde das gesamte Dokument als bytes zurückgegeben; bei vielen Seiten
    lagen PDF und OCR-Pixmaps gleichzeitig im Speicher.
    """
    output = pymupdf.open()
    warnings = []
    try:
        for image_id in group["pages"]:
            photo = draft["images"][image_id]
            image = _render_image(_photo_path(draft, photo), photo)
            page_pdf = pymupdf.open()
            try:
                portrait = image.height >= image.width
                width, height = (595, 842) if portrait else (842, 595)
                page = page_pdf.new_page(width=width, height=height)
                image_bytes = BytesIO()
                image.save(image_bytes, format="JPEG", quality=92, optimize=True)
                page.insert_image(pymupdf.Rect(18, 18, width - 18, height - 18), stream=image_bytes.getvalue(), keep_proportion=True)
                # OCR is local. Its invisible text layer must not change the photo.
                try:
                    scale = 2.5
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                    pix.set_dpi(round(72 * scale), round(72 * scale))
                    ocr_pdf = pymupdf.open(stream=pix.pdfocr_tobytes(language="deu+eng"), filetype="pdf")
                    try:
                        output.insert_pdf(ocr_pdf)
                    finally:
                        ocr_pdf.close()
                except Exception:
                    output.insert_pdf(page_pdf)
                    warnings.append(f"Texterkennung für {photo['name']} war nicht möglich.")
            finally:
                page_pdf.close()
        output.set_metadata({"creator": "Inbox-Assistent"})
        output.save(str(target), garbage=3, deflate=True)
        return warnings
    finally:
        output.close()


class App:
    def __init__(self, initial_folder: Path | None = None):
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.notice = ""
        try:
            self.store = _load_store()
        except ValueError:
            # Ein beschädigter Entwurf darf den Start nicht verhindern. Die Datei
            # bleibt als Sicherung erhalten; die Fotos im Ordner sind unberührt.
            backup = _quarantine_store()
            self.store = {"version": 2, "drafts": {}}
            self.notice = (f"Der gespeicherte Entwurf war beschädigt und wurde als {backup.name} gesichert. "
                           "Die Fotos im Ordner sind unverändert; bitte erneut importieren.")
        self.folder: Path | None = None
        # Ein laufender Vorgang (Analyse oder Export). Der Lock schützt nur kurze
        # Zustandsänderungen; die eigentliche Arbeit läuft ohne ihn, damit die
        # Oberfläche währenddessen bedienbar bleibt.
        self.job: dict | None = None
        if initial_folder:
            self.select_folder(initial_folder)

    def _ensure_idle(self) -> None:
        if self.job and self.job["status"] == "running":
            raise ValueError("Es läuft bereits ein Vorgang. Bitte abwarten.")

    def start_job(self, kind: str, work) -> str:
        """Startet einen langen Vorgang im Hintergrund und liefert seine Nummer."""
        with self.lock:
            self._ensure_idle()
            job_id = uuid.uuid4().hex
            self.job = {"id": job_id, "kind": kind, "status": "running",
                        "progress": {"done": 0, "total": 0, "label": ""},
                        "result": None, "error": ""}

        def melden(done: int, total: int, label: str = "") -> None:
            with self.lock:
                if self.job and self.job["id"] == job_id:
                    self.job["progress"] = {"done": done, "total": total, "label": label}

        def lauf() -> None:
            try:
                ergebnis = work(melden)
                with self.lock:
                    if self.job and self.job["id"] == job_id:
                        self.job.update(status="done", result=ergebnis)
            except Exception as exc:
                with self.lock:
                    if self.job and self.job["id"] == job_id:
                        self.job.update(status="error", error=str(exc) or exc.__class__.__name__)

        threading.Thread(target=lauf, daemon=True, name=f"inbox-{kind}").start()
        return job_id

    def job_status(self, job_id: str) -> dict:
        with self.lock:
            if not self.job or self.job["id"] != job_id:
                raise ValueError("Unbekannter Vorgang.")
            return dict(self.job)

    def select_folder(self, folder: Path) -> dict:
        folder = folder.expanduser().resolve()
        if not folder.is_dir():
            raise ValueError("Ordner nicht gefunden.")
        with self.lock:
            self._ensure_idle()
            key = str(folder)
            self.store["drafts"].setdefault(key, _new_draft(folder))
            self.folder = folder
            _save_store(self.store)
            return self.snapshot()

    def draft(self) -> dict:
        if not self.folder:
            raise ValueError("Bitte zuerst einen Fotoordner auswählen.")
        return self.store["drafts"][str(self.folder)]

    def snapshot(self) -> dict:
        if not self.folder:
            return {"folder": None, "images": [], "groups": [], "completed": [],
                    "recent": list(self.store["drafts"])[-5:], "notice": self.notice}
        draft = self.draft()
        images = []
        for image in draft["images"].values():
            current = dict(image)
            current["missing"] = not _photo_path(draft, image).is_file()
            current["version"] = _image_version(image)
            images.append(current)
        groups = []
        for group in draft["groups"]:
            ready, reason = _readiness(group, draft)
            groups.append({**group, "ready": ready, "blocked_reason": reason, "filename": f"{_filename_base(group)}.pdf" if ready else ""})
        return {"folder": str(self.folder), "images": images, "groups": groups,
                "completed": draft["completed"], "recent": list(self.store["drafts"])[-5:],
                "notice": self.notice}

    def _save(self) -> dict:
        _save_store(self.store)
        return self.snapshot()

    def import_file(self, filename: str, size: int, stream) -> dict:
        if not filename or Path(filename).name != filename or filename.startswith("."):
            raise ValueError("Ungültiger Fotodateiname.")
        if Path(filename).suffix.lower() not in EXTENSIONS:
            raise ValueError("Nur JPG, JPEG, HEIC und PNG sind erlaubt.")
        if size <= 0 or size > MAX_IMPORT_SIZE:
            raise ValueError("Das Foto ist leer oder zu groß (maximal 120 MB).")
        with self.lock:
            self._ensure_idle()
            draft = self.draft()
            path = self.folder / filename
            if not path.is_file() or path.stat().st_size != size:
                raise ValueError(f"{filename} liegt nicht im gewählten Quellordner oder hat sich geändert.")
            sent = hashlib.sha256()
            left = size
            while left:
                chunk = stream.read(min(left, 1024 * 1024))
                if not chunk:
                    raise ValueError("Der Foto-Upload wurde unterbrochen.")
                sent.update(chunk)
                left -= len(chunk)
            digest = sent.hexdigest()
            if digest != hash_file(path):
                raise ValueError(f"{filename} stimmt nicht mit der Datei im Quellordner überein.")
            existing = next((image for image in draft["images"].values() if image["name"] == filename), None)
            if existing is not None and existing["sha256"] == digest:
                return self.snapshot()
            try:
                with Image.open(path) as image:
                    image.verify()
            except Exception:
                # macOS can open HEIC through sips even without a Pillow plugin.
                if path.suffix.lower() != ".heic":
                    raise ValueError(f"{filename} kann nicht als Foto gelesen werden.")
                _load_image(path)
            date, origin = fallback_date(path)
            if existing is not None:
                # Die Datei im Ordner wurde durch eine andere mit gleichem Namen ersetzt.
                # Ein zweiter Eintrag würde die Gruppe dauerhaft mit "Datei wurde
                # geändert" blockieren, ohne Weg zurück. Also Eintrag aktualisieren.
                existing.update({"sha256": digest, "size": size, "fallback_date": date,
                                 "date_origin": origin, "rotation": 0, "auto_rotation": 0,
                                 "auto_quad": None, "auto_angle": 0, "auto_review": False})
                for group in draft["groups"]:
                    if existing["id"] in group["pages"] and group["source"] == "ai":
                        group["confirmed"] = False
                        group["needs_review"] = True
                        group["reason"] = f"{filename} wurde ersetzt. Bitte erneut prüfen."
                return self._save()
            image_id = uuid.uuid4().hex
            draft["images"][image_id] = {"id": image_id, "name": filename, "sha256": digest,
                "size": size, "fallback_date": date, "date_origin": origin,
                "rotation": 0, "auto_rotation": 0, "auto_quad": None, "auto_angle": 0, "auto_review": False}
            return self._save()

    def group(self, ids: list[str]) -> dict:
        with self.lock:
            self._ensure_idle()
            draft = self.draft()
            ids = list(dict.fromkeys(ids))
            if not ids or any(i not in draft["images"] for i in ids):
                raise ValueError("Bitte vorhandene Fotos auswählen.")
            if any(i in _all_grouped(draft) for i in ids):
                raise ValueError("Eine ausgewählte Seite gehört bereits zu einer Gruppe.")
            draft["groups"].append({"id": uuid.uuid4().hex, "pages": ids,
                "date": draft["images"][ids[0]]["fallback_date"], "sender": "", "title": "",
                "source": "manual", "confirmed": True, "needs_review": False, "reason": ""})
            return self._save()

    def ai_prepare(self, ids: list[str], consent: bool, check_idle: bool = True) -> list[InboxItem]:
        """Prüft die Auswahl sofort, damit Fehleingaben nicht erst im Hintergrundjob auffallen."""
        if not consent:
            raise ValueError("Für die ChatGPT-Analyse ist eine ausdrückliche Freigabe nötig.")
        with self.lock:
            if check_idle:
                self._ensure_idle()
            draft = self.draft()
            ids = list(dict.fromkeys(ids))
            if not ids or any(i not in draft["images"] for i in ids) or any(i in _all_grouped(draft) for i in ids):
                raise ValueError("Bitte ungruppierte Fotos aus diesem Ordner auswählen.")
            return [InboxItem(i, _photo_path(draft, draft["images"][i]), "Foto",
                              draft["images"][i]["fallback_date"], draft["images"][i]["date_origin"]) for i in ids]

    def ai_group(self, ids: list[str], consent: bool = True, progress=None) -> dict:
        """Klassifiziert ausgewählte Fotos.

        OCR, der Codex-Aufruf und die Begradigung laufen bewusst ohne den Lock:
        sie dauern Minuten, und die Oberfläche muss in dieser Zeit antworten.
        Änderungen sind währenddessen über _ensure_idle gesperrt.
        """
        # Läuft selbst als Job; die Leerlaufprüfung würde sonst den eigenen Job abweisen.
        items = self.ai_prepare(ids, consent, check_idle=False)
        with self.lock:
            draft = self.draft()
            folder = self.folder
            erwartet = {item.id: draft["images"][item.id]["sha256"] for item in items}
            rueckfall = {item.id: draft["images"][item.id]["fallback_date"] for item in items}

        for item in items:
            if not item.path.is_file() or hash_file(item.path) != erwartet[item.id]:
                raise ValueError(f"{item.path.name} wurde verändert. Bitte neu importieren.")

        schritte = len(items) + 1
        if progress:
            progress(0, schritte, "ChatGPT wertet die Seiten aus")
        # The model sees exactly these files. It is never given folder write access.
        suggestions = classify(items, _known_senders(folder))
        prepared = []
        for suggestion in suggestions:
            hinweise = []
            try:
                datum = validate_date(suggestion.date)
            except ValueError:
                # Ein unlesbares Datum darf nicht stumm in den Entwurf: <input type="date">
                # zeigt es leer an, und der Nutzer sieht erst beim Export eine Formatmeldung.
                datum = rueckfall[suggestion.file_ids[0]]
                hinweise.append(f"Datum „{suggestion.date}“ war unlesbar; Aufnahmedatum eingesetzt.")
            if sanitize_component(suggestion.sender.strip(), 70) == PLACEHOLDER:
                hinweise.append("Absender fehlt.")
            if sanitize_component(suggestion.title.strip(), 100) == PLACEHOLDER:
                hinweise.append("Titel fehlt.")
            pruefen = bool(suggestion.needs_review) or bool(hinweise)
            prepared.append({"id": uuid.uuid4().hex, "pages": suggestion.file_ids,
                "date": datum, "sender": suggestion.sender, "title": suggestion.title,
                "source": "ai", "confirmed": False,
                "needs_review": pruefen,
                "reason": " ".join(hinweise) or suggestion.reason or ("KI-Vorschlag bitte bestätigen." if pruefen else ""),
                "evidence": suggestion.evidence})

        corrections = {}
        for nummer, item in enumerate(items, start=1):
            if progress:
                progress(nummer, schritte, f"Seite {nummer} von {len(items)} wird begradigt")
            try:
                photo = _load_image(item.path)
                turn, unsure_turn = _auto_orientation(photo)
                if turn:
                    photo = photo.rotate(turn, expand=True)
                quad = _page_quad(photo)
                if quad:
                    photo = _apply_quad(photo, quad)
                angle, unsure_angle = _auto_deskew(photo)
                corrections[item.id] = (turn, quad, angle, unsure_turn or unsure_angle)
            except Exception:
                # Eine misslungene Begradigung darf nicht das ganze Analyseergebnis
                # kosten. Die Seite bleibt unkorrigiert und wird zur Prüfung markiert.
                corrections[item.id] = (0, None, 0.0, True)

        with self.lock:
            draft = self.draft()
            for image_id, (turn, quad, angle, uncertain) in corrections.items():
                if image_id not in draft["images"]:
                    continue
                draft["images"][image_id]["auto_rotation"] = turn
                draft["images"][image_id]["auto_quad"] = quad
                draft["images"][image_id]["auto_angle"] = angle
                draft["images"][image_id]["auto_review"] = uncertain
            draft["groups"].extend(prepared)
            return self._save()

    def update_group(self, group_id: str, fields: dict) -> dict:
        with self.lock:
            self._ensure_idle()
            group = next((g for g in self.draft()["groups"] if g["id"] == group_id), None)
            if not group:
                raise ValueError("Gruppe nicht gefunden.")
            for field in ("date", "sender", "title"):
                if field in fields:
                    group[field] = str(fields[field]).strip()
            if fields.get("confirm"):
                _filename_base(group)
                group["confirmed"] = True
                group["needs_review"] = False
                group["reason"] = ""
            elif group["source"] == "ai" and any(k in fields for k in ("date", "sender", "title")):
                group["confirmed"] = False
            return self._save()

    def move(self, image_id: str, target_id: str | None, index: int | None) -> dict:
        with self.lock:
            self._ensure_idle()
            draft = self.draft()
            if image_id not in draft["images"]:
                raise ValueError("Seite nicht gefunden.")
            target = next((g for g in draft["groups"] if g["id"] == target_id), None) if target_id else None
            if target_id and not target:
                raise ValueError("Zielgruppe nicht gefunden.")
            for group in draft["groups"]:
                if image_id in group["pages"]:
                    group["pages"].remove(image_id)
                    if group["source"] == "ai":
                        group["confirmed"] = False
                        group["needs_review"] = True
                        group["reason"] = "Seitenzuordnung wurde geändert. Bitte erneut prüfen."
            draft["groups"] = [g for g in draft["groups"] if g["pages"]]
            if target:
                index = len(target["pages"]) if index is None else max(0, min(int(index), len(target["pages"])))
                target["pages"].insert(index, image_id)
                if target["source"] == "ai":
                    target["confirmed"] = False
                    target["needs_review"] = True
                    target["reason"] = "Seitenreihenfolge wurde geändert. Bitte erneut prüfen."
            return self._save()

    def ungroup(self, group_id: str) -> dict:
        with self.lock:
            self._ensure_idle()
            draft = self.draft()
            previous = len(draft["groups"])
            draft["groups"] = [g for g in draft["groups"] if g["id"] != group_id]
            if len(draft["groups"]) == previous:
                raise ValueError("Gruppe nicht gefunden.")
            return self._save()

    def rotate(self, image_id: str, degrees: int) -> dict:
        with self.lock:
            self._ensure_idle()
            draft = self.draft()
            if image_id not in draft["images"] or degrees not in (-90, 90, 180):
                raise ValueError("Ungültige Drehung.")
            image = draft["images"][image_id]
            image["rotation"] = (image["rotation"] + degrees) % 360
            return self._save()

    def reset_correction(self, image_id: str) -> dict:
        with self.lock:
            self._ensure_idle()
            image = self.draft()["images"].get(image_id)
            if not image:
                raise ValueError("Seite nicht gefunden.")
            image["auto_rotation"] = 0
            image["auto_quad"] = None
            image["auto_angle"] = 0
            image["auto_review"] = False
            return self._save()

    def export(self, progress=None) -> dict:
        """Erzeugt die PDFs und benennt die Fotos um.

        PDF-Bau und Texterkennung dauern pro Seite Sekunden und laufen deshalb
        ohne den Lock. Nur das Fortschreiben des Entwurfs ist gesperrt, damit
        /api/state währenddessen keinen halb geänderten Zustand sieht.
        """
        with self.lock:
            draft = self.draft()
            folder = Path(draft["folder"])
            gruppen = list(draft["groups"])
        exported, skipped = [], []
        for nummer, group in enumerate(gruppen):
            name = group.get("title") or "Unbenannte Gruppe"
            if progress:
                progress(nummer, len(gruppen), f"{name} wird exportiert")
            ready, reason = _readiness(group, draft)
            if not ready:
                skipped.append({"group": name, "reason": reason})
                continue
            try:
                base = _filename_base(group)
                pdf_target = folder / f"{base}.pdf"
                renames = []
                for seite, image_id in enumerate(group["pages"], 1):
                    image = draft["images"][image_id]
                    src = _photo_path(draft, image)
                    if hash_file(src) != image["sha256"]:
                        raise ValueError(f"{src.name} wurde verändert. Bitte neu importieren.")
                    dst = folder / f"{base}_Seite-{seite:02d}{src.suffix.lower()}"
                    if dst != src and dst.exists():
                        raise FileExistsError(f"Fotodatei existiert bereits: {dst.name}")
                    renames.append((src, dst))
                if pdf_target.exists():
                    raise FileExistsError(f"PDF existiert bereits: {pdf_target.name}")
                if len({str(dst).casefold() for _, dst in renames}) != len(renames):
                    raise ValueError("Zwei Seiten würden denselben Dateinamen erhalten.")
                _check_space([src for src, _ in renames], folder)
                fd, tmp_name = tempfile.mkstemp(prefix=".inbox-export-", suffix=".pdf", dir=folder)
                os.close(fd)
                renamed = []
                try:
                    warnings = _write_pdf(draft, group, Path(tmp_name))
                    for src, dst in renames:
                        if src != dst:
                            os.rename(src, dst)
                            renamed.append((dst, src))
                    # Schlägt fehl, falls inzwischen eine gleichnamige PDF entstanden ist.
                    _publish_new(Path(tmp_name), pdf_target)
                except Exception:
                    for dst, src in reversed(renamed):
                        if dst.exists() and not src.exists():
                            os.rename(dst, src)
                    raise
                finally:
                    Path(tmp_name).unlink(missing_ok=True)
                complete = {"id": group["id"], "pdf": pdf_target.name,
                    "photos": [dst.name for _, dst in renames], "warnings": warnings,
                    "at": datetime.now().isoformat(timespec="seconds")}
                with self.lock:
                    draft["completed"].append(complete)
                    draft["groups"].remove(group)
                    for image_id in group["pages"]:
                        draft["images"].pop(image_id, None)
                    _save_store(self.store)
                exported.append(complete)
            except Exception as exc:
                skipped.append({"group": name, "reason": str(exc)})
        if progress:
            progress(len(gruppen), len(gruppen), "Abgeschlossen")
        with self.lock:
            ungrouped = [image["name"] for image_id, image in draft["images"].items() if image_id not in _all_grouped(draft)]
            return {"exported": exported, "skipped": skipped, "ungrouped": ungrouped, "state": self.snapshot()}

    def reveal(self, completed_id: str) -> None:
        with self.lock:
            entry = next((x for x in self.draft()["completed"] if x["id"] == completed_id), None)
            if not entry:
                raise ValueError("Export nicht gefunden.")
            path = self.folder / entry["pdf"]
            if not path.is_file():
                raise FileNotFoundError("Die PDF wurde inzwischen verschoben.")
            subprocess.Popen(["/usr/bin/open", "-R", str(path)])


class _CountingReader:
    """Zählt mit, wie viel vom Anfragerumpf bereits gelesen wurde."""

    def __init__(self, stream):
        self._stream = stream
        self.consumed = 0

    def read(self, count: int) -> bytes:
        chunk = self._stream.read(count)
        self.consumed += len(chunk)
        return chunk


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "InboxAssistent/2"

        def log_message(self, format, *args):
            # Never log tokenized URLs or document names.
            return

        def _authorized(self) -> bool:
            parsed = urlparse(self.path)
            token = self.headers.get("X-App-Token") or parse_qs(parsed.query).get("token", [""])[0]
            return secrets.compare_digest(token, app.token)

        def _reply(self, status: int, content: bytes, content_type: str, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def _json(self, status: int, data: dict) -> None:
            self._reply(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _drain(self, remaining: int) -> None:
            """Nicht gelesenen Anfragerumpf verwerfen.

            Ohne das schließt der Server die Verbindung mitten im Upload; der Browser
            meldet dann einen Netzwerkfehler statt der eigentlichen Ursache.
            """
            while remaining > 0:
                try:
                    chunk = self.rfile.read(min(remaining, 1024 * 1024))
                except OSError:
                    return
                if not chunk:
                    return
                remaining -= len(chunk)

        def _input(self) -> dict:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > MAX_JSON_SIZE:
                raise ValueError("Ungültige Anfragegröße.")
            return json.loads(self.rfile.read(size) or b"{}")

        def do_GET(self):
            parsed = urlparse(self.path)
            static = {"/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if parsed.path in static:
                name, content_type = static[parsed.path]
                return self._reply(200, (STATIC / name).read_bytes(), content_type)
            if not self._authorized():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Zugriff verweigert."})
            try:
                if parsed.path == "/api/state":
                    return self._json(200, app.snapshot())
                if parsed.path == "/api/job":
                    return self._json(200, app.job_status(parse_qs(parsed.query).get("id", [""])[0]))
                if parsed.path.startswith("/api/thumb/") or parsed.path.startswith("/api/image/"):
                    image_id = parsed.path.rsplit("/", 1)[-1]
                    with app.lock:
                        draft = app.draft()
                        photo = draft["images"].get(image_id)
                        if not photo:
                            raise ValueError("Foto nicht gefunden.")
                        photo = dict(photo)
                        path = _photo_path(draft, photo)
                    # Rendern ohne Lock: mehrere Vorschauen entstehen parallel.
                    data = _render_jpeg(image_id, path, photo, parsed.path.startswith("/api/thumb/"))
                    # Die URL trägt die Bildversion; ändert sich das Bild, ändert sich die URL.
                    return self._reply(200, data, "image/jpeg", cache="private, max-age=31536000, immutable")
                if parsed.path == "/":
                    return self._reply(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                return self._json(404, {"error": "Nicht gefunden."})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})

        def do_POST(self):
            if not self._authorized():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Zugriff verweigert."})
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Ungültige Herkunft."})
            path = urlparse(self.path).path
            try:
                if path == "/api/import":
                    size = int(self.headers.get("Content-Length", "0"))
                    name = unquote(self.headers.get("X-Filename", ""))
                    reader = _CountingReader(self.rfile)
                    try:
                        return self._json(200, app.import_file(name, size, reader))
                    except Exception:
                        self._drain(min(size, MAX_IMPORT_SIZE) - reader.consumed)
                        raise
                data = self._input()
                if path == "/api/select-folder":
                    folder = _choose_folder()
                    return self._json(200, app.select_folder(folder) if folder else {"cancelled": True})
                if path == "/api/group":
                    return self._json(200, app.group(data.get("ids", [])))
                if path == "/api/ai":
                    ids = data.get("ids", [])
                    # Auswahl und Freigabe sofort prüfen, damit Fehleingaben 400 ergeben
                    # statt erst als Jobfehler aufzutauchen.
                    app.ai_prepare(ids, data.get("consent") is True)
                    return self._json(200, {"job": app.start_job("ai", lambda melden: app.ai_group(ids, True, melden))})
                if path == "/api/update-group":
                    return self._json(200, app.update_group(data.get("id", ""), data))
                if path == "/api/move":
                    return self._json(200, app.move(data.get("image_id", ""), data.get("target_id"), data.get("index")))
                if path == "/api/ungroup":
                    return self._json(200, app.ungroup(data.get("id", "")))
                if path == "/api/rotate":
                    return self._json(200, app.rotate(data.get("id", ""), int(data.get("degrees", 0))))
                if path == "/api/reset-correction":
                    return self._json(200, app.reset_correction(data.get("id", "")))
                if path == "/api/export":
                    return self._json(200, {"job": app.start_job("export", app.export)})
                if path == "/api/reveal":
                    app.reveal(data.get("id", ""))
                    return self._json(200, {"ok": True})
                if path == "/api/quit":
                    self._json(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                return self._json(404, {"error": "Nicht gefunden."})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})

    return Handler


def _running_instance_url() -> str | None:
    """URL einer laufenden Instanz, oder None. Veraltete Angaben werden entfernt."""
    info = DATA_DIR / "server.json"
    try:
        url = json.loads(info.read_text(encoding="utf-8"))["url"]
        if isinstance(url, str) and url.startswith("http://127.0.0.1:"):
            with urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return url
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    info.unlink(missing_ok=True)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--test-folder", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = (DATA_DIR / "server.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        url = _running_instance_url()
        if not url:
            raise RuntimeError("Die App startet bereits oder antwortet nicht. Bitte in wenigen Sekunden erneut öffnen.")
        if args.no_browser:
            print(url, flush=True)
        else:
            webbrowser.open(url)
        return
    app = App(args.test_folder)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    url = f"http://127.0.0.1:{server.server_port}/?token={app.token}"
    info = DATA_DIR / "server.json"
    fd = os.open(info, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"url": url}, handle)
    print(url, flush=True)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        info.unlink(missing_ok=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
