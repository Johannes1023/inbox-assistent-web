"""Lange Vorgänge laufen im Hintergrund; die Oberfläche bleibt bedienbar."""
from __future__ import annotations

import http.client
import json
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


def request(server, method, path, payload=None):
    instance, app = server
    connection = http.client.HTTPConnection("127.0.0.1", instance.server_port, timeout=30)
    try:
        body = json.dumps(payload or {}).encode() if method == "POST" else None
        headers = {"X-App-Token": app.token}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def warte_auf_job(server, job_id, grenze=30.0):
    ende = time.monotonic() + grenze
    while time.monotonic() < ende:
        status, body = request(server, "GET", f"/api/job?id={job_id}")
        assert status == 200, body
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError("Job wurde nicht fertig")


@pytest.fixture
def langsame_ki(monkeypatch):
    """Ersetzt die Klassifikation durch eine, die messbar Zeit braucht."""
    import ai

    def _setzen(dauer=1.5, fehler=None):
        def fake_classify(items, existing_senders, feedback="", provider="chatgpt"):
            time.sleep(dauer)
            if fehler:
                raise RuntimeError(fehler)
            return [ai.Suggestion(file_ids=[items[0].id], date="2024-03-12", sender="Stadtwerke",
                                  title="Abrechnung", confidence=0.95, needs_review=False,
                                  reason="", evidence="Briefkopf")]
        monkeypatch.setattr(web_app, "classify", fake_classify)
        monkeypatch.setattr(web_app, "_auto_orientation", lambda image: (0, False))
        monkeypatch.setattr(web_app, "_page_quad", lambda image: None)
        monkeypatch.setattr(web_app, "_auto_deskew", lambda image: (0.0, False))
    return _setzen


@pytest.fixture
def ein_foto(app, make_photo, importer):
    state = importer(make_photo("a.jpg", size=(300, 400)))
    return state["images"][0]["id"]


class TestAnalyseAlsJob:
    def test_liefert_sofort_eine_jobnummer(self, server, ein_foto, langsame_ki):
        langsame_ki(dauer=1.5)
        start = time.monotonic()
        status, body = request(server, "POST", "/api/ai", {"ids": [ein_foto], "consent": True})
        assert status == 200 and "job" in body
        assert time.monotonic() - start < 0.5, "Der Aufruf hat auf die Analyse gewartet"
        warte_auf_job(server, body["job"])

    def test_state_bleibt_waehrend_der_analyse_erreichbar(self, server, ein_foto, langsame_ki):
        langsame_ki(dauer=2.0)
        _, body = request(server, "POST", "/api/ai", {"ids": [ein_foto], "consent": True})
        time.sleep(0.3)
        start = time.monotonic()
        status, zustand = request(server, "GET", "/api/state")
        dauer = time.monotonic() - start
        assert status == 200 and zustand["folder"]
        assert dauer < 0.5, f"/api/state war {dauer:.2f}s blockiert"
        warte_auf_job(server, body["job"])

    def test_ergebnis_landet_im_entwurf(self, server, ein_foto, langsame_ki, app):
        langsame_ki(dauer=0.1)
        _, body = request(server, "POST", "/api/ai", {"ids": [ein_foto], "consent": True})
        ergebnis = warte_auf_job(server, body["job"])
        assert ergebnis["status"] == "done"
        assert len(app.snapshot()["groups"]) == 1

    def test_meldet_fehler_statt_ihn_zu_verschlucken(self, server, ein_foto, langsame_ki):
        langsame_ki(dauer=0.1, fehler="ChatGPT nicht erreichbar")
        _, body = request(server, "POST", "/api/ai", {"ids": [ein_foto], "consent": True})
        ergebnis = warte_auf_job(server, body["job"])
        assert ergebnis["status"] == "error"
        assert "ChatGPT nicht erreichbar" in ergebnis["error"]

    def test_verlangt_weiterhin_freigabe(self, server, ein_foto):
        status, body = request(server, "POST", "/api/ai", {"ids": [ein_foto], "consent": False})
        assert status == 400 and "Freigabe" in body["error"]


class TestExportAlsJob:
    @pytest.fixture
    def zwei_gruppen(self, app, make_photo, importer):
        for name, titel in (("a.jpg", "Erster"), ("b.jpg", "Zweiter")):
            vorher = {image["id"] for image in app.snapshot()["images"]}
            state = importer(make_photo(name, size=(300, 400)))
            neu = [image["id"] for image in state["images"] if image["id"] not in vorher]
            app.group(neu)
            gid = app.snapshot()["groups"][-1]["id"]
            app.update_group(gid, {"date": "2024-03-12", "sender": "Stadtwerke", "title": titel})

    @pytest.mark.slow
    def test_exportiert_im_hintergrund(self, server, zwei_gruppen):
        status, body = request(server, "POST", "/api/export")
        assert status == 200 and "job" in body
        ergebnis = warte_auf_job(server, body["job"])
        assert ergebnis["status"] == "done"
        assert len(ergebnis["result"]["exported"]) == 2

    @pytest.mark.slow
    def test_meldet_fortschritt(self, server, zwei_gruppen):
        _, body = request(server, "POST", "/api/export")
        gesehen = []
        ende = time.monotonic() + 30
        while time.monotonic() < ende:
            _, stand = request(server, "GET", f"/api/job?id={body['job']}")
            gesehen.append((stand["progress"]["done"], stand["progress"]["total"]))
            if stand["status"] in ("done", "error"):
                break
            time.sleep(0.02)
        assert any(total == 2 for _, total in gesehen), f"Kein Gesamtwert gemeldet: {gesehen}"


class TestSchutzWaehrendEinesJobs:
    def test_lehnt_aenderungen_ab_solange_ein_job_laeuft(self, server, ein_foto, langsame_ki):
        langsame_ki(dauer=1.5)
        _, body = request(server, "POST", "/api/ai", {"ids": [ein_foto], "consent": True})
        time.sleep(0.2)
        status, antwort = request(server, "POST", "/api/group", {"ids": [ein_foto]})
        assert status == 400
        assert "läuft" in antwort["error"].lower()
        warte_auf_job(server, body["job"])

    def test_erlaubt_aenderungen_wieder_nach_abschluss(self, server, app, make_photo, importer, langsame_ki):
        langsame_ki(dauer=0.1)
        erste = importer(make_photo("a.jpg", size=(300, 400)))["images"][0]["id"]
        zweite = importer(make_photo("b.jpg", colour=(200, 180, 160), size=(300, 400)))["images"][-1]["id"]
        _, body = request(server, "POST", "/api/ai", {"ids": [erste], "consent": True})
        warte_auf_job(server, body["job"])
        status, _ = request(server, "POST", "/api/group", {"ids": [zweite]})
        assert status == 200

    def test_unbekannte_jobnummer_wird_gemeldet(self, server):
        status, body = request(server, "GET", "/api/job?id=gibtsnicht")
        assert status == 400 and "error" in body
