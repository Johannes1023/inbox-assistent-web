"""Runde 2: vollständiger Durchgang durch Server und Oberfläche."""
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


def post(server, pfad, body: bytes, kopf=None):
    instance, app = server
    verbindung = http.client.HTTPConnection("127.0.0.1", instance.server_port, timeout=15)
    try:
        headers = {"X-App-Token": app.token, "Content-Type": "application/json", **(kopf or {})}
        verbindung.request("POST", pfad, body=body, headers=headers)
        antwort = verbindung.getresponse()
        return antwort.status, json.loads(antwort.read())
    finally:
        verbindung.close()


@pytest.fixture
def seiten(app, make_photo, importer):
    ids = []
    for index, name in enumerate(["a.jpg", "b.jpg"]):
        ids = [i["id"] for i in importer(make_photo(name, colour=(240 - index * 30, 230, 220), size=(300, 400)))["images"]]
    return ids


class TestEigeneGruppeAlsZiel:
    def test_einzige_seite_auf_eigene_gruppe_behaelt_gruppe(self, app, seiten):
        app.group(seiten[:1])
        gid = app.snapshot()["groups"][0]["id"]
        app.move(seiten[0], gid, 0)
        gruppen = app.snapshot()["groups"]
        assert [g["id"] for g in gruppen] == [gid]
        assert gruppen[0]["pages"] == [seiten[0]]


class LangsamerStrom:
    def __init__(self, daten: bytes, pause: float):
        self._daten, self._pos, self._pause = daten, 0, pause

    def read(self, anzahl):
        time.sleep(self._pause)
        stueck = self._daten[self._pos:self._pos + min(anzahl, 4096)]
        self._pos += len(stueck)
        return stueck


class TestImportOhneLock:
    def test_zustand_bleibt_waehrend_eines_langsamen_uploads_abrufbar(self, app, make_photo):
        foto = make_photo("gross.jpg", size=(900, 1200))
        daten = foto.read_bytes()
        strom = LangsamerStrom(daten, pause=1.5 / max(1, len(daten) // 4096))
        faden = threading.Thread(target=app.import_file, args=("gross.jpg", len(daten), strom))
        faden.start()
        time.sleep(0.2)
        start = time.monotonic()
        with app.lock:  # jede ändernde Aktion und jede Vorschau braucht diesen Lock
            pass
        dauer = time.monotonic() - start
        faden.join()
        assert dauer < 0.3, f"Lock war {dauer:.2f}s durch den Upload belegt"
        assert [i["name"] for i in app.snapshot()["images"]] == ["gross.jpg"]

    def test_ordnerwechsel_waehrend_upload_wird_erkannt(self, app, make_photo, tmp_path):
        foto = make_photo("a.jpg", size=(300, 400))
        daten = foto.read_bytes()
        anderer = tmp_path / "Anderer"
        anderer.mkdir()

        class WechselndStrom(LangsamerStrom):
            def read(inner, anzahl):
                if inner._pos == 0:
                    app.select_folder(anderer)
                return super().read(anzahl)

        with pytest.raises(ValueError, match="Ordner"):
            app.import_file("a.jpg", len(daten), WechselndStrom(daten, 0))
        assert app.snapshot()["images"] == []


class TestExportSpeicherfehler:
    @pytest.fixture
    def fertig(self, app, seiten):
        app.group(seiten)
        gid = app.snapshot()["groups"][0]["id"]
        app.update_group(gid, {"date": "2024-03-12", "sender": "Amt", "title": "Brief"})

    @pytest.mark.slow
    def test_gelungener_export_wird_trotz_speicherfehler_gemeldet(self, app, fertig, photo_folder, monkeypatch):
        def voll(daten):
            raise OSError("Datenträger voll")
        monkeypatch.setattr(web_app, "_save_store", voll)
        ergebnis = app.export()
        assert [x["pdf"] for x in ergebnis["exported"]] == ["2024-03-12_Amt_Brief.pdf"]
        assert any("nicht gespeichert" in w for w in ergebnis["exported"][0]["warnings"])
        assert (photo_folder / "2024-03-12_Amt_Brief.pdf").is_file()

    @pytest.mark.slow
    def test_fehlgeschlagenes_zurueckbenennen_wird_benannt(self, app, fertig, photo_folder, monkeypatch):
        echt = web_app.os.rename
        aufrufe = {"n": 0}

        def kaputt(src, dst):
            aufrufe["n"] += 1
            if aufrufe["n"] in (2, 3):  # zweites Umbenennen und erstes Zurückbenennen scheitern
                raise OSError("gesperrt")
            return echt(src, dst)
        monkeypatch.setattr(web_app.os, "rename", kaputt)
        ergebnis = app.export()
        assert ergebnis["exported"] == []
        assert "zurück" in ergebnis["skipped"][0]["reason"]


class TestSnapshotSynchronisiert:
    def test_snapshot_wartet_auf_laufende_aenderung(self, app, seiten):
        """Ohne Lock kann /api/state mitten in einer Änderung iterieren."""
        gesperrt = threading.Event()

        def halten():
            with app.lock:
                gesperrt.set()
                time.sleep(0.5)
        threading.Thread(target=halten).start()
        gesperrt.wait()
        start = time.monotonic()
        app.snapshot()
        assert time.monotonic() - start >= 0.4


class TestRobusterZustand:
    def test_ungueltiger_name_im_entwurf_legt_oberflaeche_nicht_lahm(self, app, seiten):
        app.group(seiten[:1])
        app.draft()["images"][seiten[0]]["name"] = "../fremd.jpg"
        zustand = app.snapshot()
        gruppe = zustand["groups"][0]
        assert gruppe["ready"] is False and gruppe["blocked_reason"]
        assert next(i for i in zustand["images"] if i["id"] == seiten[0])["missing"] is True


class TestJobEndetImmer:
    def test_auch_systemexit_beendet_den_job(self, app):
        def bricht_hart_ab(melden):
            raise SystemExit("hart")
        job = app.start_job("test", bricht_hart_ab)
        ende = time.monotonic() + 3
        while app.job_status(job)["status"] == "running" and time.monotonic() < ende:
            time.sleep(0.02)
        assert app.job_status(job)["status"] == "error"
        app.ensure_idle()


class TestAnfragen:
    def test_json_ohne_objekt_gibt_klare_meldung(self, server):
        status, antwort = post(server, "/api/group", b"[1, 2]")
        assert status == 400 and "Ungültige Anfrage" in antwort["error"]

    def test_localhost_als_herkunft_ist_erlaubt(self, server, seiten):
        instance, _ = server
        port = instance.server_port
        status, _ = post(server, "/api/group", json.dumps({"ids": seiten[:1]}).encode(),
                         kopf={"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"})
        assert status == 200

    def test_fremde_herkunft_bleibt_verboten(self, server, seiten):
        status, _ = post(server, "/api/group", json.dumps({"ids": seiten[:1]}).encode(),
                         kopf={"Origin": "http://boese.example"})
        assert status == 403
