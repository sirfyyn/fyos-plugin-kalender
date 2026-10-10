"""Gemeinsame Vorgaben: die Tests laufen ohne Redis, Qdrant und Anmeldung, ohne Proxy und ohne die Umgebung des Rechners.

Echte Kalender-Zugänge kommen nie vor; die Server sind Attrappen auf 127.0.0.1 (``fakes.py``).
"""

from __future__ import annotations

import os

import pytest


def pytest_configure(config):
    os.environ.setdefault("MCP_HUB_AUDIT_SKIP_QDRANT", "1")
    # vor dem Import von netz.py: der Öffner liest die Proxy-Umgebung beim Bauen
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    for name in ("REDIS_URL", "QDRANT_URL", "FYOS_EMBED_URL", "HUB_SUCHE_EMBED_URL", "FYOS_OIDC_ISSUER"):
        os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def umgebung(monkeypatch):
    """Keine Kalender-Umgebung des Rechners; feste Zeitzone; keine Reste in der Termin-Quelle des Kerns."""
    for name in list(os.environ):
        if name.startswith(("FYOS_KALENDER_", "FYOS_TERMINE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FYOS_ZEITZONE", "Europe/Berlin")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    from fyos_plugin_kalender import kern, zeit

    zeit._gewarnt.clear()
    termine = kern.modul()
    if termine is not None:
        termine.ablage_anmelden(None)
        termine.dienst_anmelden(None)
    yield
    if termine is not None:
        termine.ablage_anmelden(None)
        termine.dienst_anmelden(None)


@pytest.fixture
def server_liste():
    """Sammelt Attrappen-Server und hält sie am Ende an."""
    server = []
    yield server
    for s in server:
        s.stop()


@pytest.fixture
def caldav(server_liste):
    """``caldav([Kalender, …], **Einstellungen)`` startet einen CalDAV-Server (Attrappe) mit einem Kalender „Privat“ oder den genannten."""
    from fakes import FakeCalDav, FakeKalender

    def neu(kalender=None, **kw):
        s = FakeCalDav(kalender if kalender is not None else [FakeKalender("/dav/calendars/alice/privat/", "Privat")], **kw).start()
        server_liste.append(s)
        return s

    return neu


@pytest.fixture
def http(server_liste):
    """``http(antworter)`` startet einen einfachen HTTP-Server (Attrappe); ``antworter(Anfrage) -> Antwort``."""
    from fakes import Server

    def neu(antworter=None):
        s = Server(antworter).start()
        server_liste.append(s)
        return s

    return neu
