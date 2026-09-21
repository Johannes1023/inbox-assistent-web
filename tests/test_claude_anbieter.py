"""Zweiter KI-Anbieter: Claude über die lokale Claude-Code-CLI (Abo statt API)."""
from __future__ import annotations

import base64
import http.client
import json
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import ai
import web_app
from core import InboxItem

ANTWORT = {"documents": [{"file_ids": ["F1"], "date": "2024-03-12", "sender": "Stadtwerke",
                          "title": "Abrechnung", "confidence": 0.93, "needs_review": False,
                          "reason": "", "evidence": "Briefkopf"}]}


def stream(*nachrichten) -> str:
    return "\n".join(json.dumps(n) for n in nachrichten) + "\n"


def ergebnis(**felder):
    return {"type": "result", "subtype": "success", "is_error": False, "result": "", **felder}


class Aufruf:
    """Ersetzt subprocess.run in ai und merkt sich, womit Claude aufgerufen wurde."""

    def __init__(self, stdout="", returncode=0, stderr="", fehler=None):
        self.stdout, self.returncode, self.stderr, self.fehler = stdout, returncode, stderr, fehler
        self.args = self.kwargs = None

    def __call__(self, args, **kwargs):
        if args and str(args[0]).endswith("tesseract"):
            return subprocess.CompletedProcess(args, 0, stdout="Stadtwerke Musterstadt", stderr="")
        self.args, self.kwargs = args, kwargs
        if self.fehler:
            raise self.fehler
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr=self.stderr)

    @property
    def eingabe(self) -> dict:
        return json.loads(self.kwargs["input"].strip().splitlines()[0])


@pytest.fixture
def foto(make_photo):
    pfad = make_photo("brief.jpg", size=(600, 800))
    return InboxItem("F1", pfad, "Foto", "2024-03-01", "Aufnahmedatum")


@pytest.fixture
def claude(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ai, "_claude_binary", lambda: fake)

    def _setzen(**kwargs):
        aufruf = Aufruf(**kwargs)
        monkeypatch.setattr(ai.subprocess, "run", aufruf)
        return aufruf
    return _setzen


class TestAuswahl:
    def test_claude_ruft_nicht_codex(self, foto, monkeypatch):
        gerufen = []
        monkeypatch.setattr(ai, "_ocr_image", lambda path: "")
        monkeypatch.setattr(ai, "_run_codex", lambda *a, **k: gerufen.append("codex") or ANTWORT)
        monkeypatch.setattr(ai, "_run_claude", lambda *a, **k: gerufen.append("claude") or ANTWORT)
        ai.classify([foto], [], provider="claude")
        assert gerufen == ["claude"]

    def test_standard_bleibt_chatgpt(self, foto, monkeypatch):
        gerufen = []
        monkeypatch.setattr(ai, "_ocr_image", lambda path: "")
        monkeypatch.setattr(ai, "_run_codex", lambda *a, **k: gerufen.append("codex") or ANTWORT)
        monkeypatch.setattr(ai, "_run_claude", lambda *a, **k: gerufen.append("claude") or ANTWORT)
        ai.classify([foto], [])
        assert gerufen == ["codex"]

    def test_unbekannter_anbieter(self, foto):
        with pytest.raises(ValueError):
            ai.classify([foto], [], provider="gemini")


class TestClaudeAufruf:
    def test_liefert_vorschlaege_aus_strukturierter_antwort(self, foto, claude):
        claude(stdout=stream({"type": "system", "subtype": "init"}, ergebnis(structured_output=ANTWORT)))
        vorschlaege = ai.classify([foto], [], provider="claude")
        assert [(v.file_ids, v.sender) for v in vorschlaege] == [(["F1"], "Stadtwerke")]

    def test_liest_notfalls_json_aus_dem_text(self, foto, claude):
        claude(stdout=stream(ergebnis(result="```json\n" + json.dumps(ANTWORT) + "\n```")))
        assert ai.classify([foto], [], provider="claude")[0].title == "Abrechnung"

    def test_ohne_werkzeuge_plugins_und_speicherung(self, foto, claude):
        aufruf = claude(stdout=stream(ergebnis(structured_output=ANTWORT)))
        ai.classify([foto], [], provider="claude")
        args = aufruf.args
        assert args[args.index("--tools") + 1] == ""
        assert args[args.index("--setting-sources") + 1] == ""
        for flag in ("-p", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
            assert flag in args
        assert "--bare" not in args  # --bare liest keine Abo-Anmeldung
        schema = json.loads(args[args.index("--json-schema") + 1])
        assert schema == ai.SCHEMA

    def test_nutzt_das_abo_nicht_die_api(self, foto, claude, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.example")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "abo-token")
        aufruf = claude(stdout=stream(ergebnis(structured_output=ANTWORT)))
        ai.classify([foto], [], provider="claude")
        umgebung = aufruf.kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in umgebung and "ANTHROPIC_BASE_URL" not in umgebung
        assert umgebung["CLAUDE_CODE_OAUTH_TOKEN"] == "abo-token"

    def test_startet_in_leerem_arbeitsordner(self, foto, claude):
        aufruf = claude(stdout=stream(ergebnis(structured_output=ANTWORT)))
        ai.classify([foto], [], provider="claude")
        assert aufruf.kwargs["cwd"] != str(Path.cwd())

    def test_schickt_fotos_als_bilder_in_reihenfolge(self, make_photo, claude):
        a = InboxItem("F1", make_photo("a.jpg", size=(600, 800)), "Foto", "2024-03-01", "x")
        b = InboxItem("F2", make_photo("b.jpg", colour=(200, 180, 160), size=(3000, 4000)), "Foto", "2024-03-01", "x")
        antwort = {"documents": [dict(ANTWORT["documents"][0], file_ids=["F1", "F2"])]}
        aufruf = claude(stdout=stream(ergebnis(structured_output=antwort)))
        ai.classify([a, b], [], provider="claude")
        inhalt = aufruf.eingabe["message"]["content"]
        assert inhalt[0]["type"] == "text" and '"photo_image_order": ["F1", "F2"]' in inhalt[0]["text"]
        bilder = [block for block in inhalt if block["type"] == "image"]
        assert len(bilder) == 2
        assert all(b_["source"]["media_type"] == "image/jpeg" for b_ in bilder)
        from io import BytesIO
        from PIL import Image
        groesse = Image.open(BytesIO(base64.b64decode(bilder[1]["source"]["data"]))).size
        assert max(groesse) <= ai.CLAUDE_IMAGE_EDGE


class TestClaudeFehler:
    def test_nicht_angemeldet(self, foto, claude):
        claude(stdout=stream(ergebnis(is_error=True, result="Not logged in · Please run /login")))
        with pytest.raises(RuntimeError, match="claude auth login"):
            ai.classify([foto], [], provider="claude")

    def test_sonstiger_fehler_nennt_den_grund(self, foto, claude):
        claude(stdout=stream(ergebnis(is_error=True, result="Usage limit reached")))
        with pytest.raises(RuntimeError, match="Usage limit reached"):
            ai.classify([foto], [], provider="claude")

    def test_programm_bricht_ab(self, foto, claude):
        claude(returncode=1, stderr="kaputt")
        with pytest.raises(RuntimeError, match="Claude-Auswertung fehlgeschlagen"):
            ai.classify([foto], [], provider="claude")

    def test_zeitueberschreitung(self, foto, claude):
        claude(fehler=subprocess.TimeoutExpired("claude", 1))
        with pytest.raises(RuntimeError, match="zu lange"):
            ai.classify([foto], [], provider="claude")

    def test_ungueltige_antwort(self, foto, claude):
        claude(stdout=stream(ergebnis(result="Hier ist meine Einschätzung ohne JSON")))
        with pytest.raises(RuntimeError, match="keine gültige Antwort"):
            ai.classify([foto], [], provider="claude")

    def test_nicht_installiert(self, foto, monkeypatch):
        monkeypatch.setattr(ai, "_claude_binary", lambda: None)
        monkeypatch.setattr(ai, "_ocr_image", lambda path: "")
        with pytest.raises(RuntimeError, match="nicht gefunden"):
            ai.classify([foto], [], provider="claude")

    def test_fremde_datei_id_wird_abgelehnt(self, foto, claude):
        falsch = {"documents": [dict(ANTWORT["documents"][0], file_ids=["F9"])]}
        claude(stdout=stream(ergebnis(structured_output=falsch)))
        with pytest.raises(ValueError, match="Claude"):
            ai.classify([foto], [], provider="claude")


# ---------------------------------------------------------------- App-Ebene

@pytest.fixture
def server(app):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), web_app.make_handler(app))
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    yield instance, app
    instance.shutdown()


def anfrage(server, methode, pfad, daten=None):
    instance, app = server
    verbindung = http.client.HTTPConnection("127.0.0.1", instance.server_port, timeout=15)
    try:
        verbindung.request(methode, pfad, body=json.dumps(daten or {}).encode() if methode == "POST" else None,
                           headers={"X-App-Token": app.token, "Content-Type": "application/json"})
        antwort = verbindung.getresponse()
        return antwort.status, json.loads(antwort.read())
    finally:
        verbindung.close()


@pytest.fixture
def stub_klassifikation(monkeypatch):
    gesehen = {}

    def fake(items, existing_senders, feedback="", provider="chatgpt"):
        gesehen["provider"] = provider
        return [ai.Suggestion(file_ids=[items[0].id], date="2024-03-12", sender="Stadtwerke", title="Abrechnung",
                              confidence=0.95, needs_review=False, reason="", evidence="Briefkopf")]
    monkeypatch.setattr(web_app, "classify", fake)
    monkeypatch.setattr(web_app, "_auto_orientation", lambda image: (0, False))
    monkeypatch.setattr(web_app, "_page_quad", lambda image: None)
    monkeypatch.setattr(web_app, "_auto_deskew", lambda image: (0.0, False))
    return gesehen


class TestEinstellung:
    def test_standard_ist_chatgpt(self, app):
        assert app.snapshot()["ai_provider"] == "chatgpt"

    def test_auswahl_wird_gespeichert(self, server, app):
        status, zustand = anfrage(server, "POST", "/api/settings", {"ai_provider": "claude"})
        assert status == 200 and zustand["ai_provider"] == "claude"
        neu = web_app.App()
        assert neu.snapshot()["ai_provider"] == "claude"

    def test_unbekannter_anbieter_abgelehnt(self, server):
        status, antwort = anfrage(server, "POST", "/api/settings", {"ai_provider": "gemini"})
        assert status == 400 and "error" in antwort

    def test_alter_entwurf_ohne_einstellung(self, isolated_data_dir):
        (isolated_data_dir / "drafts.json").write_text(json.dumps({"version": 2, "drafts": {}}))
        assert web_app.App().snapshot()["ai_provider"] == "chatgpt"


class TestAnalyseMitClaude:
    def _warten(self, server, job):
        ende = time.monotonic() + 10
        while time.monotonic() < ende:
            _, stand = anfrage(server, "GET", f"/api/job?id={job}")
            if stand["status"] != "running":
                return stand
            time.sleep(0.05)
        raise AssertionError("Job hängt")

    def test_anbieter_aus_der_anfrage_wird_genutzt(self, server, app, make_photo, importer, stub_klassifikation):
        bild = importer(make_photo("a.jpg", size=(300, 400)))["images"][0]["id"]
        _, antwort = anfrage(server, "POST", "/api/ai", {"ids": [bild], "consent": True, "provider": "claude"})
        stand = self._warten(server, antwort["job"])
        assert stand["status"] == "done", stand
        assert stub_klassifikation["provider"] == "claude"
        gruppe = app.snapshot()["groups"][0]
        assert gruppe["provider"] == "claude"

    def test_unbekannter_anbieter_sofort_abgelehnt(self, server, app, make_photo, importer):
        bild = importer(make_photo("a.jpg", size=(300, 400)))["images"][0]["id"]
        status, _ = anfrage(server, "POST", "/api/ai", {"ids": [bild], "consent": True, "provider": "gemini"})
        assert status == 400

    def test_ohne_angabe_gilt_die_einstellung(self, server, app, make_photo, importer, stub_klassifikation):
        anfrage(server, "POST", "/api/settings", {"ai_provider": "claude"})
        bild = importer(make_photo("a.jpg", size=(300, 400)))["images"][0]["id"]
        _, antwort = anfrage(server, "POST", "/api/ai", {"ids": [bild], "consent": True})
        self._warten(server, antwort["job"])
        assert stub_klassifikation["provider"] == "claude"
