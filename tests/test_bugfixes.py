"""Ein Test je verifiziertem Befund. Rot vor dem Fix, grün danach."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import web_app
from conftest import FakeStream


class TestBefund10NamenPruefungNachBereinigung:
    """Geprüft werden muss der bereinigte Wert, nicht die Rohgabe."""

    @pytest.mark.parametrize("sender", ["...", "///", "   .   ", "\x01\x02", ":*?"])
    def test_lehnt_absender_ohne_verwertbare_zeichen_ab(self, sender):
        with pytest.raises(ValueError):
            web_app._filename_base({"date": "2024-03-12", "sender": sender, "title": "Titel"})

    @pytest.mark.parametrize("title", ["...", "///", "\x01"])
    def test_lehnt_titel_ohne_verwertbare_zeichen_ab(self, title):
        with pytest.raises(ValueError):
            web_app._filename_base({"date": "2024-03-12", "sender": "Stadtwerke", "title": title})

    def test_erzeugt_nie_platzhalter_im_dateinamen(self):
        with pytest.raises(ValueError):
            web_app._filename_base({"date": "2024-03-12", "sender": "...", "title": "..."})


class TestBefund11GleicherNameAndererInhalt:
    """Wird die Datei im Ordner ersetzt, darf kein zweiter Geistereintrag entstehen."""

    def test_ersetzt_eintrag_statt_ihn_zu_verdoppeln(self, app, make_photo, importer, photo_folder):
        photo = make_photo("brief.jpg", size=(300, 400))
        importer(photo)
        make_photo("brief.jpg", colour=(120, 90, 60), size=(300, 400))
        state = importer(photo)
        namen = [image["name"] for image in state["images"]]
        assert namen == ["brief.jpg"], f"Doppelter Eintrag: {namen}"

    def test_gruppe_bleibt_nach_ersetzen_exportierbar(self, app, make_photo, importer, photo_folder):
        photo = make_photo("brief.jpg", size=(300, 400))
        importer(photo)
        make_photo("brief.jpg", colour=(120, 90, 60), size=(300, 400))
        state = importer(photo)
        app.group([image["id"] for image in state["images"]])
        gid = app.snapshot()["groups"][0]["id"]
        app.update_group(gid, {"date": "2024-03-12", "sender": "Test", "title": "Ersetzt"})
        assert app.snapshot()["groups"][0]["ready"] is True


@pytest.fixture
def server(app):
    """Echter HTTP-Server auf Loopback, wie im Betrieb."""
    instance = ThreadingHTTPServer(("127.0.0.1", 0), web_app.make_handler(app))
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    yield instance, app
    instance.shutdown()


def post_import(server, filename: str, body: bytes):
    import http.client
    instance, app = server
    connection = http.client.HTTPConnection("127.0.0.1", instance.server_port, timeout=15)
    try:
        connection.request("POST", "/api/import", body=body, headers={
            "X-App-Token": app.token, "X-Filename": filename,
            "Content-Type": "application/octet-stream", "Content-Length": str(len(body))})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class TestBefund15AbgelehnterUpload:
    """Der Client muss den Grund erfahren — auch bei großem Rumpf."""

    @pytest.mark.parametrize("groesse_kb", [1, 2000, 8000])
    def test_meldet_falsche_endung_statt_verbindungsabbruch(self, server, groesse_kb):
        status, body = post_import(server, "notiz.txt", b"x" * (groesse_kb * 1024))
        assert status == 400
        assert "JPG" in body["error"]

    def test_meldet_fehlende_datei_statt_verbindungsabbruch(self, server):
        status, body = post_import(server, "fehlt.jpg", b"x" * (4000 * 1024))
        assert status == 400
        assert "Quellordner" in body["error"]

    def test_meldet_zu_grosse_datei(self, server):
        status, body = post_import(server, "riesig.jpg", b"x" * (3000 * 1024))
        assert status == 400


class TestBefund9DatumAusKiVorschlag:
    """Nicht-ISO-Daten dürfen nicht stumm im Entwurf landen."""

    def _vorschlag(self, monkeypatch, datum):
        import ai

        def fake_classify(items, existing_senders, feedback="", provider="chatgpt"):
            return [ai.Suggestion(file_ids=[items[0].id], date=datum, sender="Stadtwerke",
                                  title="Abrechnung", confidence=0.95, needs_review=False,
                                  reason="", evidence="Briefkopf")]
        monkeypatch.setattr(web_app, "classify", fake_classify)
        monkeypatch.setattr(web_app, "_auto_orientation", lambda image: (0, False))
        monkeypatch.setattr(web_app, "_page_quad", lambda image: None)
        monkeypatch.setattr(web_app, "_auto_deskew", lambda image: (0.0, False))

    @pytest.mark.parametrize("datum", ["12.03.2024", "2024/03/12", "März 2024", ""])
    def test_faellt_auf_rueckfalldatum_zurueck(self, app, make_photo, importer, monkeypatch, datum):
        self._vorschlag(monkeypatch, datum)
        state = importer(make_photo("a.jpg", size=(300, 400)))
        bild = state["images"][0]
        app.ai_group([bild["id"]], consent=True)
        gruppe = app.snapshot()["groups"][0]
        assert gruppe["date"] == bild["fallback_date"]
        assert gruppe["needs_review"] is True

    def test_uebernimmt_gueltiges_datum(self, app, make_photo, importer, monkeypatch):
        self._vorschlag(monkeypatch, "2024-03-12")
        state = importer(make_photo("a.jpg", size=(300, 400)))
        app.ai_group([state["images"][0]["id"]], consent=True)
        assert app.snapshot()["groups"][0]["date"] == "2024-03-12"


class TestBefund8VorhandeneAbsender:
    """Der Prompt bittet um Wiederverwendung bestehender Ordnernamen — die müssen ankommen."""

    def test_reicht_bereits_exportierte_absender_an_die_ki(self, app, make_photo, importer, monkeypatch, photo_folder):
        (photo_folder / "2024-01-05_Stadtwerke Musterstadt_Abschlag.pdf").write_bytes(b"%PDF")
        (photo_folder / "2023-11-02_Finanzamt Nord_Bescheid.pdf").write_bytes(b"%PDF")
        gesehen = {}

        import ai

        def fake_classify(items, existing_senders, feedback="", provider="chatgpt"):
            gesehen["senders"] = existing_senders
            return [ai.Suggestion(file_ids=[items[0].id], date="2024-03-12", sender="Stadtwerke Musterstadt",
                                  title="Abrechnung", confidence=0.95, needs_review=False,
                                  reason="", evidence="Briefkopf")]
        monkeypatch.setattr(web_app, "classify", fake_classify)
        monkeypatch.setattr(web_app, "_auto_orientation", lambda image: (0, False))
        monkeypatch.setattr(web_app, "_page_quad", lambda image: None)
        monkeypatch.setattr(web_app, "_auto_deskew", lambda image: (0.0, False))

        state = importer(make_photo("a.jpg", size=(300, 400)))
        app.ai_group([state["images"][0]["id"]], consent=True)
        assert sorted(gesehen["senders"]) == ["Finanzamt Nord", "Stadtwerke Musterstadt"]


class TestBefund17AutokorrekturProBild:
    """Ein kaputtes Foto darf nicht das ganze KI-Ergebnis vernichten."""

    def test_behaelt_vorschlag_wenn_eine_korrektur_scheitert(self, app, make_photo, importer, monkeypatch):
        import ai
        state = importer(make_photo("a.jpg", size=(300, 400)))
        state = importer(make_photo("b.jpg", colour=(200, 180, 160), size=(300, 400)))
        ids = [image["id"] for image in state["images"]]

        def fake_classify(items, existing_senders, feedback="", provider="chatgpt"):
            return [ai.Suggestion(file_ids=[item.id], date="2024-03-12", sender="Stadtwerke",
                                  title=f"Brief {item.id[:4]}", confidence=0.95, needs_review=False,
                                  reason="", evidence="Briefkopf") for item in items]

        aufrufe = {"n": 0}

        def kaputt(image):
            aufrufe["n"] += 1
            if aufrufe["n"] == 1:
                raise OSError("Foto unlesbar")
            return (0, False)

        monkeypatch.setattr(web_app, "classify", fake_classify)
        monkeypatch.setattr(web_app, "_auto_orientation", kaputt)
        monkeypatch.setattr(web_app, "_page_quad", lambda image: None)
        monkeypatch.setattr(web_app, "_auto_deskew", lambda image: (0.0, False))

        app.ai_group(ids, consent=True)
        assert len(app.snapshot()["groups"]) == 2


class TestBefund12Speicherplatz:
    """Vor dem Export muss geprüft werden, ob der Platz reicht."""

    def test_bricht_bei_zu_wenig_platz_ab(self, app, make_photo, importer, monkeypatch):
        import shutil as shutil_modul
        state = importer(make_photo("a.jpg", size=(300, 400)))
        app.group([state["images"][0]["id"]])
        gid = app.snapshot()["groups"][0]["id"]
        app.update_group(gid, {"date": "2024-03-12", "sender": "Test", "title": "Eng"})

        class Wenig:
            free = 1024

        monkeypatch.setattr(web_app.shutil, "disk_usage", lambda path: Wenig)
        ergebnis = app.export()
        assert ergebnis["exported"] == []
        assert "platz" in ergebnis["skipped"][0]["reason"].lower()
