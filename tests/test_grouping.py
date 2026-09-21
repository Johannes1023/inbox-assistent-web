"""Gruppieren, Umsortieren und Freigabelogik."""
from __future__ import annotations

import pytest

import web_app


@pytest.fixture
def drei_fotos(app, make_photo, importer):
    for index, name in enumerate(["a.jpg", "b.jpg", "c.jpg"]):
        importer(make_photo(name, colour=(240 - index * 20, 230, 220)))
    return [image["id"] for image in app.snapshot()["images"]]


def gruppe(app, index=0):
    return app.snapshot()["groups"][index]


class TestGruppieren:
    def test_bildet_gruppe_in_angegebener_reihenfolge(self, app, drei_fotos):
        app.group(drei_fotos[:2])
        assert gruppe(app)["pages"] == drei_fotos[:2]

    def test_uebernimmt_rueckfalldatum_der_ersten_seite(self, app, drei_fotos):
        erwartet = app.snapshot()["images"][0]["fallback_date"]
        app.group(drei_fotos[:1])
        assert gruppe(app)["date"] == erwartet

    def test_manuelle_gruppe_gilt_als_bestaetigt(self, app, drei_fotos):
        app.group(drei_fotos[:1])
        assert gruppe(app)["source"] == "manual" and gruppe(app)["confirmed"] is True

    def test_entfernt_doppelte_ids(self, app, drei_fotos):
        app.group([drei_fotos[0], drei_fotos[0]])
        assert gruppe(app)["pages"] == [drei_fotos[0]]

    def test_lehnt_leere_auswahl_ab(self, app, drei_fotos):
        with pytest.raises(ValueError):
            app.group([])

    def test_lehnt_unbekannte_id_ab(self, app, drei_fotos):
        with pytest.raises(ValueError):
            app.group(["gibtsnicht"])

    def test_lehnt_bereits_gruppierte_seite_ab(self, app, drei_fotos):
        app.group(drei_fotos[:1])
        with pytest.raises(ValueError):
            app.group(drei_fotos[:1])


class TestVerschieben:
    def test_haengt_seite_ans_ende_an(self, app, drei_fotos):
        app.group(drei_fotos[:2])
        app.move(drei_fotos[2], gruppe(app)["id"], None)
        assert gruppe(app)["pages"] == drei_fotos

    def test_fuegt_an_position_ein(self, app, drei_fotos):
        app.group(drei_fotos[:2])
        app.move(drei_fotos[2], gruppe(app)["id"], 0)
        assert gruppe(app)["pages"][0] == drei_fotos[2]

    def test_loest_seite_aus_gruppe(self, app, drei_fotos):
        app.group(drei_fotos[:2])
        app.move(drei_fotos[0], None, None)
        assert gruppe(app)["pages"] == [drei_fotos[1]]

    def test_entfernt_leer_gewordene_gruppe(self, app, drei_fotos):
        app.group(drei_fotos[:1])
        app.move(drei_fotos[0], None, None)
        assert app.snapshot()["groups"] == []

    def test_tauscht_nachbarn_beim_sortieren(self, app, drei_fotos):
        app.group(drei_fotos)
        gid = gruppe(app)["id"]
        app.move(drei_fotos[0], gid, 1)
        assert gruppe(app)["pages"] == [drei_fotos[1], drei_fotos[0], drei_fotos[2]]

    def test_begrenzt_zu_grossen_index(self, app, drei_fotos):
        app.group(drei_fotos[:2])
        app.move(drei_fotos[2], gruppe(app)["id"], 99)
        assert gruppe(app)["pages"][-1] == drei_fotos[2]

    def test_lehnt_unbekannte_zielgruppe_ab(self, app, drei_fotos):
        with pytest.raises(ValueError):
            app.move(drei_fotos[0], "gibtsnicht", None)


class TestAufloesen:
    def test_gibt_seiten_frei(self, app, drei_fotos):
        app.group(drei_fotos[:2])
        app.ungroup(gruppe(app)["id"])
        assert app.snapshot()["groups"] == []
        assert len(app.snapshot()["images"]) == 3

    def test_lehnt_unbekannte_gruppe_ab(self, app, drei_fotos):
        with pytest.raises(ValueError):
            app.ungroup("gibtsnicht")


class TestFelderUndFreigabe:
    def test_speichert_felder(self, app, drei_fotos):
        app.group(drei_fotos[:1])
        app.update_group(gruppe(app)["id"], {"sender": "Stadtwerke", "title": "Abrechnung"})
        assert gruppe(app)["sender"] == "Stadtwerke"

    def test_meldet_bereitschaft_erst_bei_vollstaendigkeit(self, app, drei_fotos):
        app.group(drei_fotos[:1])
        assert gruppe(app)["ready"] is False
        app.update_group(gruppe(app)["id"], {"sender": "Stadtwerke", "title": "Abrechnung"})
        assert gruppe(app)["ready"] is True
        assert gruppe(app)["filename"].endswith("_Stadtwerke_Abrechnung.pdf")

    def test_ki_gruppe_ist_bis_zur_bestaetigung_gesperrt(self, app, drei_fotos):
        app.group(drei_fotos[:1])
        roh = app.draft()["groups"][0]
        roh["source"] = "ai"
        roh["confirmed"] = False
        app.update_group(roh["id"], {"sender": "Stadtwerke", "title": "Abrechnung"})
        assert gruppe(app)["ready"] is False
        app.update_group(roh["id"], {"sender": "Stadtwerke", "title": "Abrechnung", "confirm": True})
        assert gruppe(app)["ready"] is True

    def test_aenderung_entzieht_bestaetigung_einer_ki_gruppe(self, app, drei_fotos):
        app.group(drei_fotos[:1])
        roh = app.draft()["groups"][0]
        roh["source"] = "ai"
        app.update_group(roh["id"], {"sender": "A", "title": "B", "confirm": True})
        assert gruppe(app)["confirmed"] is True
        app.update_group(roh["id"], {"title": "C"})
        assert gruppe(app)["confirmed"] is False

    def test_seitenwechsel_entzieht_bestaetigung(self, app, drei_fotos):
        app.group(drei_fotos[:2])
        roh = app.draft()["groups"][0]
        roh["source"] = "ai"
        app.update_group(roh["id"], {"sender": "A", "title": "B", "confirm": True})
        app.move(drei_fotos[2], roh["id"], None)
        assert gruppe(app)["confirmed"] is False
        assert gruppe(app)["needs_review"] is True

    def test_meldet_fehlende_datei(self, app, drei_fotos, photo_folder):
        app.group(drei_fotos[:1])
        app.update_group(gruppe(app)["id"], {"sender": "A", "title": "B"})
        (photo_folder / "a.jpg").unlink()
        assert gruppe(app)["ready"] is False
        assert "fehlt" in gruppe(app)["blocked_reason"].lower()

    def test_meldet_geaenderte_datei(self, app, drei_fotos, photo_folder):
        app.group(drei_fotos[:1])
        app.update_group(gruppe(app)["id"], {"sender": "A", "title": "B"})
        (photo_folder / "a.jpg").write_bytes(b"anders")
        assert gruppe(app)["ready"] is False


class TestDrehen:
    def test_addiert_drehung_modulo(self, app, drei_fotos):
        app.rotate(drei_fotos[0], 90)
        app.rotate(drei_fotos[0], 90)
        assert app.draft()["images"][drei_fotos[0]]["rotation"] == 180
        app.rotate(drei_fotos[0], 180)
        assert app.draft()["images"][drei_fotos[0]]["rotation"] == 0

    @pytest.mark.parametrize("grad", [45, 0, 360, -180, 1])
    def test_lehnt_ungueltige_winkel_ab(self, app, drei_fotos, grad):
        with pytest.raises(ValueError):
            app.rotate(drei_fotos[0], grad)

    def test_setzt_autokorrektur_zurueck(self, app, drei_fotos):
        bild = app.draft()["images"][drei_fotos[0]]
        bild.update({"auto_rotation": 90, "auto_angle": 1.5, "auto_quad": [0] * 8, "auto_review": True})
        app.reset_correction(drei_fotos[0])
        bild = app.draft()["images"][drei_fotos[0]]
        assert (bild["auto_rotation"], bild["auto_angle"], bild["auto_quad"], bild["auto_review"]) == (0, 0, None, False)
