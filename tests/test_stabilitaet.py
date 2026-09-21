"""Start, Speicherung und Export unter widrigen Bedingungen."""
from __future__ import annotations

import errno
import json

import pytest

import web_app


class TestBefund3OhneHardlinks:
    """exFAT/FAT (USB-Sticks, SD-Karten) kennen keine Hardlinks."""

    @pytest.mark.slow
    def test_exportiert_auch_ohne_hardlink(self, app, make_photo, importer, photo_folder, monkeypatch):
        state = importer(make_photo("a.jpg", size=(300, 400)))
        app.group([state["images"][0]["id"]])
        gid = app.snapshot()["groups"][0]["id"]
        app.update_group(gid, {"date": "2024-03-12", "sender": "Test", "title": "Stick"})

        def kein_link(src, dst, *args, **kwargs):
            raise OSError(errno.ENOTSUP, "Operation not supported")
        monkeypatch.setattr(web_app.os, "link", kein_link)

        ergebnis = app.export()
        assert [x["pdf"] for x in ergebnis["exported"]] == ["2024-03-12_Test_Stick.pdf"]
        assert (photo_folder / "2024-03-12_Test_Stick.pdf").read_bytes().startswith(b"%PDF")
        assert not list(photo_folder.glob(".inbox-export-*"))

    @pytest.mark.slow
    def test_ueberschreibt_auch_ohne_hardlink_nichts(self, app, make_photo, importer, photo_folder, monkeypatch):
        state = importer(make_photo("a.jpg", size=(300, 400)))
        app.group([state["images"][0]["id"]])
        gid = app.snapshot()["groups"][0]["id"]
        app.update_group(gid, {"date": "2024-03-12", "sender": "Test", "title": "Stick"})
        monkeypatch.setattr(web_app.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EPERM, "nein")))

        # PDF taucht erst nach der Vorprüfung auf, genau im Moment des Anlegens.
        echtes_schreiben = web_app._write_pdf
        def schreiben_und_kollidieren(draft, group, target):
            warnungen = echtes_schreiben(draft, group, target)
            (photo_folder / "2024-03-12_Test_Stick.pdf").write_bytes(b"fremd")
            return warnungen
        monkeypatch.setattr(web_app, "_write_pdf", schreiben_und_kollidieren)

        ergebnis = app.export()
        assert ergebnis["exported"] == []
        assert (photo_folder / "2024-03-12_Test_Stick.pdf").read_bytes() == b"fremd"
        assert (photo_folder / "a.jpg").is_file()


class TestBefund4DefekterEntwurf:
    def test_startet_trotz_defekter_entwurfsdatei(self, isolated_data_dir):
        (isolated_data_dir / "drafts.json").write_text("{ kaputt", encoding="utf-8")
        app = web_app.App()
        assert app.snapshot()["folder"] is None

    def test_sichert_defekte_datei_statt_sie_zu_loeschen(self, isolated_data_dir):
        (isolated_data_dir / "drafts.json").write_text("{ kaputt", encoding="utf-8")
        web_app.App()
        sicherungen = list(isolated_data_dir.glob("drafts.defekt-*.json"))
        assert len(sicherungen) == 1
        assert sicherungen[0].read_text(encoding="utf-8") == "{ kaputt"

    def test_meldet_die_wiederherstellung(self, isolated_data_dir):
        (isolated_data_dir / "drafts.json").write_text('{"version": 99}', encoding="utf-8")
        app = web_app.App()
        assert "gesichert" in app.snapshot().get("notice", "")


class TestBefund5ZweiterStart:
    def test_veraltete_serverdatei_wird_ignoriert(self, isolated_data_dir):
        (isolated_data_dir / "server.json").write_text(json.dumps({"url": "http://127.0.0.1:9/?token=x"}))
        assert web_app._running_instance_url() is None
        assert not (isolated_data_dir / "server.json").exists()

    def test_kaputte_serverdatei_wird_ignoriert(self, isolated_data_dir):
        (isolated_data_dir / "server.json").write_text("kein json")
        assert web_app._running_instance_url() is None

    def test_fehlende_serverdatei(self, isolated_data_dir):
        assert web_app._running_instance_url() is None
