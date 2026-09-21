"""Import prüft Herkunft, Integrität und Dateityp."""
from __future__ import annotations

import pytest

from conftest import FakeStream


class TestImportAkzeptanz:
    def test_importiert_foto_aus_quellordner(self, app, make_photo, importer):
        photo = make_photo("brief.jpg")
        state = importer(photo)
        assert [image["name"] for image in state["images"]] == ["brief.jpg"]

    def test_merkt_sich_rueckfalldatum(self, app, make_photo, importer):
        state = importer(make_photo("brief.jpg"))
        image = state["images"][0]
        assert image["fallback_date"] and image["date_origin"]

    def test_gleiche_datei_zweimal_bleibt_ein_eintrag(self, app, make_photo, importer):
        photo = make_photo("brief.jpg")
        importer(photo)
        state = importer(photo)
        assert len(state["images"]) == 1

    def test_zwei_verschiedene_fotos(self, app, make_photo, importer):
        importer(make_photo("a.jpg"))
        state = importer(make_photo("b.jpg", colour=(200, 190, 180)))
        assert len(state["images"]) == 2


class TestImportAbwehr:
    @pytest.mark.parametrize("name", ["../aussen.jpg", "unter/a.jpg", ".versteckt.jpg", ""])
    def test_lehnt_unzulaessige_namen_ab(self, app, name):
        with pytest.raises(ValueError):
            app.import_file(name, 10, FakeStream(b"x" * 10))

    @pytest.mark.parametrize("name", ["dokument.pdf", "notiz.txt", "bild.gif", "film.mov"])
    def test_lehnt_fremde_endungen_ab(self, app, name):
        with pytest.raises(ValueError):
            app.import_file(name, 10, FakeStream(b"x" * 10))

    def test_lehnt_leere_datei_ab(self, app):
        with pytest.raises(ValueError):
            app.import_file("a.jpg", 0, FakeStream(b""))

    def test_lehnt_zu_grosse_datei_ab(self, app):
        with pytest.raises(ValueError):
            app.import_file("a.jpg", 200 * 1024 * 1024, FakeStream(b""))

    def test_lehnt_datei_ausserhalb_des_ordners_ab(self, app, tmp_path):
        fremd = tmp_path / "fremd.jpg"
        fremd.write_bytes(b"x" * 20)
        with pytest.raises(ValueError):
            app.import_file("fremd.jpg", 20, FakeStream(b"x" * 20))

    def test_lehnt_abweichenden_inhalt_ab(self, app, make_photo, importer):
        """Hochgeladene Bytes müssen zur Datei im Quellordner passen."""
        photo = make_photo("brief.jpg")
        falsch = b"y" * photo.stat().st_size
        with pytest.raises(ValueError):
            importer(photo, payload=falsch)

    def test_lehnt_abgebrochenen_upload_ab(self, app, make_photo, importer):
        photo = make_photo("brief.jpg")
        with pytest.raises(ValueError):
            importer(photo, payload=photo.read_bytes()[:10])

    def test_lehnt_nichtbild_mit_bildendung_ab(self, app, photo_folder):
        fake = photo_folder / "kein-bild.png"
        fake.write_bytes(b"das ist kein png" * 4)
        data = fake.read_bytes()
        with pytest.raises(ValueError):
            app.import_file("kein-bild.png", len(data), FakeStream(data))

    def test_verlangt_ausgewaehlten_ordner(self, make_photo):
        import web_app
        leer = web_app.App()
        with pytest.raises(ValueError):
            leer.import_file("a.jpg", 10, FakeStream(b"x" * 10))
