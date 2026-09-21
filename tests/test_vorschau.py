"""Vorschaubilder: einmal rendern, danach aus dem Cache; Browser darf zwischenspeichern."""
from __future__ import annotations

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

import web_app


@pytest.fixture
def server(app):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), web_app.make_handler(app))
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    yield instance, app
    instance.shutdown()


@pytest.fixture
def zaehler(monkeypatch):
    aufrufe = {"n": 0}
    echt = web_app._render_image

    def gezaehlt(*args, **kwargs):
        aufrufe["n"] += 1
        return echt(*args, **kwargs)
    monkeypatch.setattr(web_app, "_render_image", gezaehlt)
    web_app._RENDER_CACHE.clear()
    return aufrufe


def hole(server, pfad):
    instance, app = server
    connection = http.client.HTTPConnection("127.0.0.1", instance.server_port, timeout=15)
    try:
        connection.request("GET", f"{pfad}?token={app.token}")
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.fixture
def bild(app, make_photo, importer):
    return importer(make_photo("a.jpg", size=(600, 800)))["images"][0]["id"]


class TestRenderCache:
    def test_zweiter_abruf_rendert_nicht_erneut(self, server, bild, zaehler):
        status1, _, daten1 = hole(server, f"/api/thumb/{bild}")
        status2, _, daten2 = hole(server, f"/api/thumb/{bild}")
        assert status1 == status2 == 200
        assert daten1 == daten2
        assert zaehler["n"] == 1

    def test_drehung_erzeugt_neues_bild(self, server, app, bild, zaehler):
        hole(server, f"/api/thumb/{bild}")
        app.rotate(bild, 90)
        hole(server, f"/api/thumb/{bild}")
        assert zaehler["n"] == 2

    def test_vorschau_und_grossbild_getrennt(self, server, bild, zaehler):
        hole(server, f"/api/thumb/{bild}")
        hole(server, f"/api/image/{bild}")
        assert zaehler["n"] == 2

    def test_ersetzte_datei_erzeugt_neues_bild(self, server, app, bild, make_photo, importer, zaehler):
        hole(server, f"/api/thumb/{bild}")
        importer(make_photo("a.jpg", colour=(100, 80, 60), size=(600, 800)))
        hole(server, f"/api/thumb/{bild}")
        assert zaehler["n"] == 2


class TestBrowserCache:
    def test_bilder_sind_zwischenspeicherbar(self, server, bild):
        _, kopf, _ = hole(server, f"/api/thumb/{bild}")
        assert "immutable" in kopf["Cache-Control"]
        assert "private" in kopf["Cache-Control"]

    def test_zustand_bleibt_ungecacht(self, server):
        _, kopf, _ = hole(server, "/api/state")
        assert kopf["Cache-Control"] == "no-store"

    def test_snapshot_liefert_bildversion(self, app, bild):
        vorher = app.snapshot()["images"][0]["version"]
        app.rotate(bild, 90)
        assert app.snapshot()["images"][0]["version"] != vorher
