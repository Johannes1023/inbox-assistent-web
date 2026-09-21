"""Befunde aus dem Code-Review nach der ersten Reparatur."""
from __future__ import annotations

import fcntl
import http.client
import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import web_app


@pytest.fixture
def server(app):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), web_app.make_handler(app))
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    yield instance, app
    instance.shutdown()


def anfrage(server, methode, pfad, body=None, kopf=None):
    instance, app = server
    verbindung = http.client.HTTPConnection("127.0.0.1", instance.server_port, timeout=60)
    try:
        headers = {"X-App-Token": app.token, **(kopf or {})}
        if methode == "POST" and body is None:
            body = b"{}"
            headers.setdefault("Content-Type", "application/json")
        verbindung.request(methode, pfad, body=body, headers=headers)
        antwort = verbindung.getresponse()
        daten = antwort.read()
        return antwort.status, dict(antwort.getheaders()), daten
    finally:
        verbindung.close()


@pytest.fixture
def laufender_job(app):
    """Hält einen Job künstlich offen, bis der Test ihn freigibt."""
    frei = threading.Event()
    job_id = app.start_job("test", lambda melden: frei.wait(10))
    yield job_id
    frei.set()
    ende = time.monotonic() + 5
    while app.job_status(job_id)["status"] == "running" and time.monotonic() < ende:
        time.sleep(0.02)


class TestQuitWaehrendJob:
    def test_beenden_wird_abgelehnt(self, server, laufender_job):
        status, _, daten = anfrage(server, "POST", "/api/quit")
        assert status == 400
        assert "läuft" in json.loads(daten)["error"]

    def test_server_laeuft_danach_weiter(self, server, laufender_job):
        anfrage(server, "POST", "/api/quit")
        status, _, _ = anfrage(server, "GET", "/api/state")
        assert status == 200


class TestOrdnerdialogWaehrendJob:
    def test_dialog_oeffnet_sich_nicht(self, server, laufender_job, monkeypatch):
        geoeffnet = []
        monkeypatch.setattr(web_app, "_choose_folder", lambda: geoeffnet.append(1))
        status, _, daten = anfrage(server, "POST", "/api/select-folder")
        assert status == 400 and "läuft" in json.loads(daten)["error"]
        assert geoeffnet == []


class TestBekannteAbsender:
    def test_mehrdeutiger_dateiname_liefert_keinen_verstuemmelten_absender(self, photo_folder):
        (photo_folder / "2024-01-05_Müller_GmbH_Rechnung.pdf").write_bytes(b"%PDF")
        assert "Müller" not in web_app._known_senders(photo_folder, [])

    def test_eindeutiger_dateiname_zaehlt(self, photo_folder):
        (photo_folder / "2023-11-02_Finanzamt Nord_Bescheid.pdf").write_bytes(b"%PDF")
        assert web_app._known_senders(photo_folder, []) == ["Finanzamt Nord"]

    def test_absender_aus_exportliste_ist_exakt(self, photo_folder):
        erledigt = [{"pdf": "2024-01-05_Müller_GmbH_Rechnung.pdf", "sender": "Müller_GmbH"}]
        assert web_app._known_senders(photo_folder, erledigt) == ["Müller_GmbH"]

    @pytest.mark.slow
    def test_export_merkt_sich_den_absender(self, app, make_photo, importer):
        state = importer(make_photo("a.jpg", size=(300, 400)))
        app.group([state["images"][0]["id"]])
        gid = app.snapshot()["groups"][0]["id"]
        app.update_group(gid, {"date": "2024-03-12", "sender": "Müller_GmbH", "title": "Rechnung"})
        app.export()
        assert app.draft()["completed"][-1]["sender"] == "Müller_GmbH"


class TestUebergrosserUpload:
    def test_meldet_zu_gross_statt_verbindungsabbruch(self, server):
        rumpf = b"x" * (web_app.MAX_IMPORT_SIZE + 5 * 1024 * 1024)
        status, _, daten = anfrage(server, "POST", "/api/import", body=rumpf,
                                   kopf={"X-Filename": "riesig.jpg", "Content-Type": "application/octet-stream"})
        assert status == 400
        assert "zu groß" in json.loads(daten)["error"]

    def test_haengender_client_blockiert_nicht_ewig(self, app):
        handler = web_app.make_handler(app)
        assert handler.timeout and handler.timeout <= 120


class TestUnbekanntAlsEchterName:
    def test_absender_unbekannt_ist_erlaubt(self):
        name = web_app._filename_base({"date": "2024-03-12", "sender": "Unbekannt", "title": "Brief"})
        assert name == "2024-03-12_Unbekannt_Brief"

    def test_titel_unbekannt_ist_erlaubt(self):
        name = web_app._filename_base({"date": "2024-03-12", "sender": "Amt", "title": "Unbekannt"})
        assert name == "2024-03-12_Amt_Unbekannt"


class TestKiHinweiseNurFuerDatum:
    def test_begruendung_der_ki_bleibt_erhalten(self, app, make_photo, importer, monkeypatch):
        import ai

        def fake_classify(items, existing_senders, feedback=""):
            return [ai.Suggestion(file_ids=[items[0].id], date="2024-03-12", sender="", title="Brief",
                                  confidence=0.4, needs_review=True, reason="Briefkopf unleserlich",
                                  evidence="")]
        monkeypatch.setattr(web_app, "classify", fake_classify)
        monkeypatch.setattr(web_app, "_auto_orientation", lambda image: (0, False))
        monkeypatch.setattr(web_app, "_page_quad", lambda image: None)
        monkeypatch.setattr(web_app, "_auto_deskew", lambda image: (0.0, False))
        state = importer(make_photo("a.jpg", size=(300, 400)))
        app.ai_group([state["images"][0]["id"]])
        assert app.snapshot()["groups"][0]["reason"] == "Briefkopf unleserlich"


class TestEtag:
    @pytest.fixture
    def bild(self, app, make_photo, importer):
        return importer(make_photo("a.jpg", size=(300, 400)))["images"][0]["id"]

    def test_liefert_etag(self, server, bild):
        _, kopf, _ = anfrage(server, "GET", f"/api/thumb/{bild}")
        assert kopf.get("ETag")

    def test_antwortet_304_bei_passendem_etag(self, server, bild):
        _, kopf, _ = anfrage(server, "GET", f"/api/thumb/{bild}")
        status, _, daten = anfrage(server, "GET", f"/api/thumb/{bild}", kopf={"If-None-Match": kopf["ETag"]})
        assert status == 304 and daten == b""

    def test_neues_bild_nach_drehung(self, server, app, bild):
        _, kopf, _ = anfrage(server, "GET", f"/api/thumb/{bild}")
        app.rotate(bild, 90)
        status, _, _ = anfrage(server, "GET", f"/api/thumb/{bild}", kopf={"If-None-Match": kopf["ETag"]})
        assert status == 200


class TestZweitstart:
    def test_klare_meldung_statt_traceback(self, isolated_data_dir, monkeypatch):
        sperre = (isolated_data_dir / "server.lock").open("a+")
        fcntl.flock(sperre, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(sys, "argv", ["web_app.py", "--no-browser"])
        try:
            with pytest.raises(SystemExit) as fehler:
                web_app.main()
            assert "startet bereits" in str(fehler.value.code)
        finally:
            fcntl.flock(sperre, fcntl.LOCK_UN)
            sperre.close()


class TestExportReihenfolge:
    """Erst die PDF anlegen, dann umbenennen: ein Abbruch dazwischen verliert keine Zuordnung."""

    @pytest.fixture
    def gruppe(self, app, make_photo, importer):
        for name in ("a.jpg", "b.jpg"):
            state = importer(make_photo(name, size=(300, 400)))
        app.group([image["id"] for image in state["images"]])
        gid = app.snapshot()["groups"][0]["id"]
        app.update_group(gid, {"date": "2024-03-12", "sender": "Amt", "title": "Brief"})

    @pytest.mark.slow
    def test_pdf_entsteht_vor_dem_umbenennen(self, app, gruppe, photo_folder, monkeypatch):
        beim_anlegen = {}
        echt = web_app._publish_new

        def beobachtet(temp, target):
            beim_anlegen["fotos_noch_original"] = (photo_folder / "a.jpg").exists() and (photo_folder / "b.jpg").exists()
            return echt(temp, target)
        monkeypatch.setattr(web_app, "_publish_new", beobachtet)
        app.export()
        assert beim_anlegen["fotos_noch_original"] is True

    @pytest.mark.slow
    def test_fehler_beim_umbenennen_raeumt_pdf_weg(self, app, gruppe, photo_folder, monkeypatch):
        echt = web_app.os.rename
        aufrufe = {"n": 0}

        def zweites_scheitert(src, dst):
            aufrufe["n"] += 1
            if aufrufe["n"] == 2:
                raise OSError("Datenträger voll")
            return echt(src, dst)
        monkeypatch.setattr(web_app.os, "rename", zweites_scheitert)
        ergebnis = app.export()
        assert ergebnis["exported"] == []
        assert not (photo_folder / "2024-03-12_Amt_Brief.pdf").exists()
        assert (photo_folder / "a.jpg").is_file() and (photo_folder / "b.jpg").is_file()
        assert not list(photo_folder.glob(".inbox-export-*"))
