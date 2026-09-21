"""Gemeinsame Fixtures. Setzt das Datenverzeichnis auf tmp, bevor web_app importiert wird."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

RESOURCES = Path(__file__).resolve().parents[1] / "Inbox-Assistent Web.app" / "Contents" / "Resources"
sys.path.insert(0, str(RESOURCES))

# web_app liest DATA_DIR beim Import aus der Umgebung.
_DATA_DIR = Path(tempfile.mkdtemp(prefix="inbox-test-data-"))
os.environ["INBOX_APP_DATA_DIR"] = str(_DATA_DIR)

import core  # noqa: E402
import web_app  # noqa: E402

from PIL import Image  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Jeder Test bekommt ein eigenes Anwendungsdatenverzeichnis."""
    data = tmp_path / "appdata"
    data.mkdir()
    monkeypatch.setattr(web_app, "DATA_DIR", data)
    return data


@pytest.fixture
def photo_folder(tmp_path):
    folder = tmp_path / "Fotos"
    folder.mkdir()
    return folder


def write_photo(folder: Path, name: str, colour=(230, 230, 230), size=(600, 800)) -> Path:
    """Legt ein echtes, lesbares JPEG/PNG im Ordner ab."""
    path = folder / name
    image = Image.new("RGB", size, colour)
    # Ein paar dunkle Balken, damit Heuristiken etwas zu sehen bekommen.
    for y in range(80, size[1] - 80, 40):
        for x in range(60, size[0] - 60):
            image.putpixel((x, y), (20, 20, 20))
    image.save(path)
    return path


@pytest.fixture
def make_photo(photo_folder):
    def _make(name: str, **kwargs) -> Path:
        return write_photo(photo_folder, name, **kwargs)
    return _make


@pytest.fixture
def app(photo_folder):
    instance = web_app.App()
    instance.select_folder(photo_folder)
    return instance


class FakeStream:
    """Ersetzt self.rfile beim Import: liefert den Dateiinhalt in Häppchen."""

    def __init__(self, payload: bytes):
        self._data = payload
        self._pos = 0

    def read(self, count: int) -> bytes:
        chunk = self._data[self._pos:self._pos + count]
        self._pos += len(chunk)
        return chunk


@pytest.fixture
def importer(app):
    """Importiert eine Datei so, wie es der HTTP-Handler täte."""
    def _import(path: Path, payload: bytes | None = None, size: int | None = None):
        data = path.read_bytes() if payload is None else payload
        return app.import_file(path.name, path.stat().st_size if size is None else size, FakeStream(data))
    return _import
