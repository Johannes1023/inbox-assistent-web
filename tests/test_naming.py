"""Namensbildung und Pfadsicherheit."""
from __future__ import annotations

import pytest

import core
import web_app


class TestSanitizeComponent:
    def test_entfernt_pfadtrenner(self):
        assert "/" not in core.sanitize_component("Stadt/Werke")
        assert "\\" not in core.sanitize_component("A\\B")

    def test_entfernt_steuerzeichen(self):
        assert core.sanitize_component("A\x00\x1fB") == "A B"

    def test_faltet_mehrfache_leerzeichen(self):
        assert core.sanitize_component("Viele    Leer   zeichen") == "Viele Leer zeichen"

    def test_schneidet_auf_maximallaenge(self):
        assert len(core.sanitize_component("x" * 500, 90)) == 90

    def test_endet_nie_auf_punkt_oder_leerzeichen(self):
        for value in ("Name.", "Name ", "Name . ", "x" * 89 + " y"):
            result = core.sanitize_component(value)
            assert result == result.rstrip(" .")

    def test_leere_eingabe_wird_unbekannt(self):
        assert core.sanitize_component("   ") == "Unbekannt"
        assert core.sanitize_component("...") == "Unbekannt"


class TestValidateDate:
    def test_akzeptiert_iso(self):
        assert core.validate_date("2024-03-12") == "2024-03-12"
        assert core.validate_date("  2024-03-12  ") == "2024-03-12"

    @pytest.mark.parametrize("value", ["12.03.2024", "2024/03/12", "", "morgen", "2024-13-01"])
    def test_lehnt_nicht_iso_ab(self, value):
        with pytest.raises(ValueError):
            core.validate_date(value)


class TestFilenameBase:
    def test_baut_erwartetes_muster(self):
        group = {"date": "2024-03-12", "sender": "Stadtwerke", "title": "Jahresabrechnung"}
        assert web_app._filename_base(group) == "2024-03-12_Stadtwerke_Jahresabrechnung"

    def test_bereinigt_pfadtrenner_im_absender(self):
        group = {"date": "2024-03-12", "sender": "A/B", "title": "Titel"}
        assert "/" not in web_app._filename_base(group)

    @pytest.mark.parametrize("group", [
        {"date": "", "sender": "A", "title": "B"},
        {"date": "2024-03-12", "sender": "", "title": "B"},
        {"date": "2024-03-12", "sender": "A", "title": ""},
        {"date": "2024-03-12", "sender": "   ", "title": "B"},
    ])
    def test_lehnt_unvollstaendige_gruppen_ab(self, group):
        with pytest.raises(ValueError):
            web_app._filename_base(group)


class TestPhotoPath:
    def test_liefert_pfad_im_ordner(self, photo_folder):
        draft = {"folder": str(photo_folder)}
        assert web_app._photo_path(draft, {"name": "a.jpg"}) == photo_folder / "a.jpg"

    @pytest.mark.parametrize("name", ["../aussen.jpg", "unter/a.jpg", "../../etc/passwd"])
    def test_wehrt_pfadausbruch_ab(self, photo_folder, name):
        draft = {"folder": str(photo_folder)}
        with pytest.raises(ValueError):
            web_app._photo_path(draft, {"name": name})
