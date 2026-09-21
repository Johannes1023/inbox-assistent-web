"""PDF-Export: Graustufen-JPEG statt verlustfreiem Rasterbild — deutlich kleinere Dateien."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

import web_app


def _testfoto(pfad: Path, groesse=(1200, 1600)) -> None:
    """Text/Kontrast für OCR, plus Kamerarauschen: echte Fotos komprimieren unter
    verlustfreier Flate-Kompression deutlich schlechter als ein glatter Testblock."""
    import random
    img = Image.new("RGB", groesse, (248, 246, 240))
    d = ImageDraw.Draw(img)
    d.text((60, 60), "Stadtwerke Musterstadt", fill=(20, 30, 60))
    for y in range(180, groesse[1] - 100, 40):
        d.rectangle([60, y, groesse[0] - 200, y + 12], fill=(30, 30, 30))
    pixel = img.load()
    zufall = random.Random(1)
    for _ in range(groesse[0] * groesse[1] // 6):
        x, y = zufall.randrange(groesse[0]), zufall.randrange(groesse[1])
        r, g, b = pixel[x, y]
        rauschen = zufall.randrange(-18, 19)
        pixel[x, y] = (max(0, min(255, r + rauschen)), max(0, min(255, g + rauschen)), max(0, min(255, b + rauschen)))
    img.save(pfad, quality=92)


@pytest.fixture
def gruppe(app, tmp_path, importer):
    foto = tmp_path / "brief.jpg"
    _testfoto(foto)
    photo_folder = app.folder
    ziel = photo_folder / "brief.jpg"
    _testfoto(ziel)
    state = importer(ziel)
    image_id = state["images"][0]["id"]
    app.group([image_id])
    gid = app.snapshot()["groups"][0]["id"]
    app.update_group(gid, {"date": "2024-03-12", "sender": "Test", "title": "Groesse"})
    return app.draft()["groups"][0]


@pytest.mark.slow
class TestPdfGroesse:
    def test_bild_ist_jpeg_nicht_verlustfrei(self, app, gruppe, tmp_path):
        ziel = tmp_path / "out.pdf"
        web_app._write_pdf(app.draft(), gruppe, ziel)
        dokument = pymupdf.open(ziel)
        xref = dokument[0].get_images()[0][0]
        assert dokument.xref_get_key(xref, "Filter") == ("name", "/DCTDecode")

    def test_bild_ist_graustufen(self, app, gruppe, tmp_path):
        """Ein Farbprofil kann als ICCBased auftauchen; entscheidend ist ein Kanal, keine drei."""
        from PIL import Image as PILImage
        ziel = tmp_path / "out.pdf"
        web_app._write_pdf(app.draft(), gruppe, ziel)
        dokument = pymupdf.open(ziel)
        xref = dokument[0].get_images()[0][0]
        eingebettet = PILImage.open(BytesIO(dokument.extract_image(xref)["image"]))
        assert eingebettet.mode in ("L", "LA")

    def test_deutlich_kleiner_als_verlustfreie_einbettung(self, app, gruppe, tmp_path):
        ziel = tmp_path / "out.pdf"
        web_app._write_pdf(app.draft(), gruppe, ziel)

        # Referenz: der alte Weg (verlustfreies Flate-Rasterbild), direkt nachgebaut.
        photo = app.draft()["images"][gruppe["pages"][0]]
        bild = web_app._render_image(web_app._photo_path(app.draft(), photo), photo)
        seite_pdf = pymupdf.open()
        seite = seite_pdf.new_page(width=595, height=842)
        puffer = BytesIO(); bild.save(puffer, format="JPEG", quality=92, optimize=True)
        seite.insert_image(pymupdf.Rect(18, 18, 577, 824), stream=puffer.getvalue(), keep_proportion=True)
        pix = seite.get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), alpha=False)
        referenz = pix.pdfocr_tobytes(language="deu+eng")

        assert ziel.stat().st_size < len(referenz) * 0.75

    def test_text_bleibt_durchsuchbar(self, app, gruppe, tmp_path):
        ziel = tmp_path / "out.pdf"
        web_app._write_pdf(app.draft(), gruppe, ziel)
        text = pymupdf.open(ziel)[0].get_text()
        assert "Stadtwerke Musterstadt" in text

    def test_keine_warnung_bei_erfolgreicher_ocr(self, app, gruppe, tmp_path):
        ziel = tmp_path / "out.pdf"
        warnungen = web_app._write_pdf(app.draft(), gruppe, ziel)
        assert warnungen == []

    def test_fallback_ohne_ocr_bleibt_lesbar(self, app, gruppe, tmp_path, monkeypatch):
        """Schlägt die Texterkennung fehl, wird trotzdem eine gültige PDF mit dem Foto erzeugt."""
        def kaputt(self, *a, **k):
            raise RuntimeError("OCR nicht verfügbar")
        monkeypatch.setattr(pymupdf.Pixmap, "pdfocr_tobytes", kaputt)
        ziel = tmp_path / "out.pdf"
        warnungen = web_app._write_pdf(app.draft(), gruppe, ziel)
        assert len(warnungen) == 1
        dokument = pymupdf.open(ziel)
        assert len(dokument) == 1
        assert len(dokument[0].get_images()) == 1
