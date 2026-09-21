"""Kleine Härtungen: Host-Prüfung, Referrer, fehlende Texterkennung, Entwurfsgröße."""
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


def hole(server, pfad, host=None):
    instance, app = server
    connection = http.client.HTTPConnection("127.0.0.1", instance.server_port, timeout=10)
    try:
        connection.putrequest("GET", pfad, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{instance.server_port}")
        connection.putheader("X-App-Token", app.token)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, dict(response.getheaders())
    finally:
        connection.close()


class TestHostPruefung:
    """Schutz gegen DNS-Rebinding: fremde Hostnamen werden abgewiesen."""

    def test_akzeptiert_loopback(self, server):
        status, _ = hole(server, "/api/state")
        assert status == 200

    @pytest.mark.parametrize("host", ["boese.example:80", "localhost.boese.example", "127.0.0.1.nip.io"])
    def test_lehnt_fremden_host_ab(self, server, host):
        status, _ = hole(server, "/api/state", host=host)
        assert status == 403

    def test_prueft_auch_statische_dateien(self, server):
        status, _ = hole(server, "/app.js", host="boese.example")
        assert status == 403


class TestKopfzeilen:
    def test_kein_referrer(self, server):
        _, kopf = hole(server, "/api/state")
        assert kopf["Referrer-Policy"] == "no-referrer"


class TestOhneTexterkennung:
    """Fehlt Tesseract, ist Drehungserkennung nicht verfügbar — aber keine Seite 'unsicher'."""

    def test_markiert_nicht_jede_seite_als_unsicher(self, monkeypatch):
        from PIL import Image
        monkeypatch.setattr(web_app, "TESSERACT", "/gibt/es/nicht/tesseract")
        web_app._osd_available.cache_clear()
        drehung, unsicher = web_app._auto_orientation(Image.new("RGB", (200, 300), "white"))
        web_app._osd_available.cache_clear()
        assert (drehung, unsicher) == (0, False)


class TestEntwurfsgroesse:
    def test_begrenzt_erledigte_eintraege(self, app):
        draft = app.draft()
        draft["completed"] = [{"id": str(n), "pdf": f"{n}.pdf", "photos": [], "warnings": [], "at": ""}
                              for n in range(500)]
        app._save()
        assert len(app.draft()["completed"]) == web_app.MAX_COMPLETED
        assert app.draft()["completed"][-1]["id"] == "499"
