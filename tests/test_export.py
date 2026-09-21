"""Export: erzeugt PDFs, benennt Fotos um und macht bei Fehlern sauber zurück."""
from __future__ import annotations

import pytest

import pymupdf

KLEIN = {"size": (300, 400)}


@pytest.fixture
def fertige_gruppe(app, make_photo, importer):
    """Eine exportbereite Gruppe aus zwei Seiten."""
    def _bauen(name_a="a.jpg", name_b="b.jpg", sender="Stadtwerke", title="Abrechnung", datum="2024-03-12"):
        vorher = {image["id"] for image in app.snapshot()["images"]}
        for name in (name_a, name_b):
            state = importer(make_photo(name, **KLEIN))
        ids = [image["id"] for image in state["images"] if image["id"] not in vorher]
        app.group(ids)
        gid = app.snapshot()["groups"][-1]["id"]
        app.update_group(gid, {"date": datum, "sender": sender, "title": title})
        return gid
    return _bauen


@pytest.mark.slow
class TestExportErfolg:
    def test_erzeugt_pdf_mit_einer_seite_je_foto(self, app, fertige_gruppe, photo_folder):
        fertige_gruppe()
        ergebnis = app.export()
        assert len(ergebnis["exported"]) == 1
        pdf = photo_folder / ergebnis["exported"][0]["pdf"]
        assert pdf.is_file()
        with pymupdf.open(pdf) as dokument:
            assert len(dokument) == 2

    def test_pdf_heisst_wie_die_gruppe(self, app, fertige_gruppe, photo_folder):
        fertige_gruppe()
        ergebnis = app.export()
        assert ergebnis["exported"][0]["pdf"] == "2024-03-12_Stadtwerke_Abrechnung.pdf"

    def test_benennt_fotos_um_statt_sie_zu_loeschen(self, app, fertige_gruppe, photo_folder):
        fertige_gruppe()
        ergebnis = app.export()
        namen = ergebnis["exported"][0]["photos"]
        assert namen == ["2024-03-12_Stadtwerke_Abrechnung_Seite-01.jpg",
                         "2024-03-12_Stadtwerke_Abrechnung_Seite-02.jpg"]
        for name in namen:
            assert (photo_folder / name).is_file()
        assert not (photo_folder / "a.jpg").exists()

    def test_raeumt_gruppe_und_seiten_aus_dem_entwurf(self, app, fertige_gruppe):
        fertige_gruppe()
        app.export()
        zustand = app.snapshot()
        assert zustand["groups"] == []
        assert zustand["images"] == []
        assert len(zustand["completed"]) == 1

    def test_exportiert_mehrere_gruppen(self, app, fertige_gruppe):
        fertige_gruppe("a.jpg", "b.jpg", title="Erster")
        fertige_gruppe("c.jpg", "d.jpg", title="Zweiter")
        ergebnis = app.export()
        assert len(ergebnis["exported"]) == 2

    def test_meldet_ungruppierte_seiten(self, app, fertige_gruppe, make_photo, importer):
        fertige_gruppe()
        importer(make_photo("offen.jpg", **KLEIN))
        ergebnis = app.export()
        assert ergebnis["ungrouped"] == ["offen.jpg"]


@pytest.mark.slow
class TestExportAbbruch:
    def test_ueberspringt_unvollstaendige_gruppe(self, app, make_photo, importer):
        state = importer(make_photo("a.jpg", **KLEIN))
        app.group([state["images"][0]["id"]])
        ergebnis = app.export()
        assert ergebnis["exported"] == []
        assert len(ergebnis["skipped"]) == 1
        assert (app.folder / "a.jpg").is_file()

    def test_ueberschreibt_vorhandene_pdf_nicht(self, app, fertige_gruppe, photo_folder):
        fertige_gruppe()
        vorhanden = photo_folder / "2024-03-12_Stadtwerke_Abrechnung.pdf"
        vorhanden.write_bytes(b"%PDF-1.4 bereits da")
        ergebnis = app.export()
        assert ergebnis["exported"] == []
        assert vorhanden.read_bytes() == b"%PDF-1.4 bereits da"

    def test_laesst_fotos_bei_pdf_kollision_unveraendert(self, app, fertige_gruppe, photo_folder):
        """Rollback: schlägt der Export fehl, behalten die Fotos ihre Originalnamen."""
        fertige_gruppe()
        (photo_folder / "2024-03-12_Stadtwerke_Abrechnung.pdf").write_bytes(b"%PDF belegt")
        app.export()
        assert (photo_folder / "a.jpg").is_file()
        assert (photo_folder / "b.jpg").is_file()

    def test_bricht_bei_belegtem_fotonamen_ab(self, app, fertige_gruppe, photo_folder):
        fertige_gruppe()
        (photo_folder / "2024-03-12_Stadtwerke_Abrechnung_Seite-01.jpg").write_bytes(b"belegt")
        ergebnis = app.export()
        assert ergebnis["exported"] == []
        assert (photo_folder / "a.jpg").is_file()

    def test_bricht_bei_geaenderter_datei_ab(self, app, fertige_gruppe, photo_folder):
        fertige_gruppe()
        (photo_folder / "a.jpg").write_bytes(b"inzwischen anders")
        ergebnis = app.export()
        assert ergebnis["exported"] == []
        assert len(ergebnis["skipped"]) == 1

    def test_eine_fehlerhafte_gruppe_stoppt_die_andere_nicht(self, app, fertige_gruppe, photo_folder):
        fertige_gruppe("a.jpg", "b.jpg", title="Kaputt")
        fertige_gruppe("c.jpg", "d.jpg", title="Heil")
        (photo_folder / "2024-03-12_Stadtwerke_Kaputt.pdf").write_bytes(b"%PDF belegt")
        ergebnis = app.export()
        assert [x["pdf"] for x in ergebnis["exported"]] == ["2024-03-12_Stadtwerke_Heil.pdf"]
        assert len(ergebnis["skipped"]) == 1

    def test_behaelt_entwurf_bei_fehlschlag(self, app, fertige_gruppe, photo_folder):
        fertige_gruppe()
        (photo_folder / "2024-03-12_Stadtwerke_Abrechnung.pdf").write_bytes(b"%PDF belegt")
        app.export()
        assert len(app.snapshot()["groups"]) == 1
        assert len(app.snapshot()["images"]) == 2
