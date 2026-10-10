"""HTTP zu Kalender-Servern: wohin das Passwort geht, Umleitungen, Grenzen, Fehlertexte."""

from __future__ import annotations

import gzip
import socket
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fakes import Antwort, Server, basic

from fyos_plugin_kalender import netz
from fyos_plugin_kalender.netz import NetzFehler, Sitzung

PASSWORT = "streng-geheimes-app-passwort"


@pytest.fixture
def server(server_liste):
    def neu(antworter=None):
        s = Server(antworter).start()
        server_liste.append(s)
        return s
    return neu


def teile(url: str) -> urllib.parse.SplitResult:
    return urllib.parse.urlsplit(url)


# ── wohin darf das Passwort ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "start,ziel,erwartet",
    [
        ("https://cloud.example.com/dav", "https://cloud.example.com/remote.php/dav/", True),
        ("https://cloud.example.com", "https://cloud.example.com:443/x", True),  # 443 ist der Vorgabeport
        ("https://cloud.example.com", "https://cloud.example.com:8443/x", False),  # anderer Port
        ("http://cloud.example.com", "https://cloud.example.com/x", True),  # Aufwertung 80 → 443
        ("http://cloud.example.com:80", "https://cloud.example.com:443/x", True),
        ("https://cloud.example.com", "http://cloud.example.com/x", False),  # nie zurück auf http
        ("http://cloud.example.com:8080", "https://cloud.example.com:8080/x", False),  # nur die Vorgabeports
        ("http://cloud.example.com:8080", "https://cloud.example.com/x", False),
        ("http://cloud.example.com", "https://cloud.example.com:8443/x", False),
        ("http://cloud.example.com:8080", "http://cloud.example.com:8080/x", True),
        # Apple: caldav.… leitet auf pNN-caldav.… weiter
        ("https://caldav.icloud.com", "https://p12-caldav.icloud.com:443/1234/calendars/", True),
        ("http://caldav.icloud.com", "http://p12-caldav.icloud.com/1234/calendars/", False),  # Geschwister nur mit https
        ("https://caldav.example.co.uk", "https://p1.example.co.uk/x", True),  # Basis hat drei Teile
        ("https://a.co.uk", "https://b.co.uk/x", False),  # Namen unter co.uk gehören verschiedenen Leuten
        ("https://mein.github.io", "https://anderer.github.io/x", False),  # geteilte Domain
        ("https://cloud.example.com", "https://cloud.example.org/x", False),
        ("https://cloud.example.com", "https://evilexample.com/x", False),
        ("https://cloud.example.com", "https://cloud.example.com.evil.net/x", False),
        ("https://cloud.example.com", "ftp://cloud.example.com/x", False),
        ("https://cloud.example.com", "file:///etc/passwd", False),
        ("http://192.0.2.10", "http://192.0.2.10/x", True),
        ("https://192.0.2.10", "https://192.0.2.11/x", False),
        ("http://[2001:db8::1]:8080", "http://[2001:db8::1]:8080/x", True),
        ("http://[2001:db8::1]", "http://[2001:db8::2]/x", False),
        ("https://cloud.example.com", "https://CLOUD.example.com/x", True),
    ],
)
def test_gleiche_seite(start, ziel, erwartet):
    assert netz.gleiche_seite(teile(start), teile(ziel)) is erwartet


def test_host_text_zeigt_nie_pfad_oder_nutzer():
    assert netz.host_text(teile("https://cloud.example.com:8443/geheimer/pfad?x=1")) == "cloud.example.com:8443"
    assert netz.host_text(teile("https://cloud.example.com/pfad")) == "cloud.example.com"
    assert netz.host_text(teile("http://[2001:db8::1]:8080/")) == "[2001:db8::1]:8080"


def test_sitzung_zeigt_das_passwort_nicht():
    s = Sitzung("https://cloud.example.com/dav", "anna", PASSWORT)
    assert PASSWORT not in repr(s) and "anna" not in repr(s) and s.host == "cloud.example.com"
    assert s.mit_anmeldung and not Sitzung("https://x.example.com").mit_anmeldung


# ── Anfragen ─────────────────────────────────────────────────────────────────────────────────────────────
def test_anfrage_sendet_basic_auth_methode_rumpf_und_kopf(server):
    s1 = server(lambda a: Antwort(207, {"Content-Type": "application/xml"}, b"<ok/>"))
    sitzung = Sitzung(s1.url + "/dav", "anna.müller", "pässwort:mit:doppelpunkt")
    antwort = sitzung.anfrage("PROPFIND", s1.url + "/dav", koerper=b"<x/>", kopf={"Depth": "0"}, erlaubt=(207,))
    assert (antwort.status, antwort.koerper, antwort.url) == (207, b"<ok/>", s1.url + "/dav")
    (a,) = s1.anfragen
    assert (a.methode, a.pfad, a.koerper, a.kopf["depth"]) == ("PROPFIND", "/dav", b"<x/>", "0")
    assert a.kopf["authorization"] == basic("anna.müller", "pässwort:mit:doppelpunkt")
    assert a.kopf["user-agent"].startswith("fyos-plugin-kalender/") and "gzip" in a.kopf["accept-encoding"]


def test_ohne_anmeldung_kein_authorization_kopf(server):
    s1 = server(lambda a: Antwort(200, {}, b"x"))
    Sitzung(s1.url).anfrage("GET", s1.url + "/x", erlaubt=(200,))
    assert "authorization" not in s1.anfragen[0].kopf


@pytest.mark.parametrize(
    "status,kopf,art,enthaelt",
    [
        (401, {"WWW-Authenticate": 'Basic realm="x"'}, "auth", "Anmeldung abgelehnt (App-Passwort prüfen)"),
        (401, {"WWW-Authenticate": 'Digest realm="x", nonce="1"'}, "auth", "Digest"),
        (403, {}, "verboten", "HTTP 403"),
        (404, {}, "nicht_gefunden", "nicht gefunden"),
        (410, {}, "nicht_gefunden", "nicht gefunden"),
        (405, {}, "anfrage", "lehnt PROPFIND ab"),
        (501, {}, "anfrage", "lehnt PROPFIND ab"),
        (429, {}, "server", "bremst"),
        (500, {}, "server", "HTTP 500"),
        (503, {}, "server", "HTTP 503"),
        (418, {}, "anfrage", "HTTP 418"),
    ],
)
def test_fehlerantworten_haben_verstaendliche_texte_ohne_zugangsdaten(server, status, kopf, art, enthaelt):
    s1 = server(lambda a: Antwort(status, kopf, b"Fehlerseite mit " + PASSWORT.encode()))
    with pytest.raises(NetzFehler) as e:
        Sitzung(s1.url, "anna", PASSWORT).anfrage("PROPFIND", s1.url + "/pfad/mit/geheimnis")
    assert e.value.art == art and e.value.status == status and enthaelt in e.value.text
    for text in (e.value.text, str(e.value), repr(e.value)):
        assert PASSWORT not in text and "geheimnis" not in text and "Fehlerseite" not in text
    assert "127.0.0.1" in e.value.text or art in ("auth", "verboten", "nicht_gefunden")


def test_401_ohne_anmeldung_sagt_dass_nutzer_und_passwort_fehlen(server):
    s1 = server(lambda a: Antwort(401, {"WWW-Authenticate": 'Basic realm="x"'}))
    with pytest.raises(NetzFehler) as e:
        Sitzung(s1.url).anfrage("GET", s1.url + "/")
    assert e.value.art == "auth" and "fehlen" in e.value.text


def test_erlaubte_status_werden_durchgereicht(server):
    s1 = server(lambda a: Antwort(204))
    assert Sitzung(s1.url).anfrage("PUT", s1.url + "/x", koerper=b"a", erlaubt=(201, 204)).status == 204
    with pytest.raises(NetzFehler):
        Sitzung(s1.url).anfrage("PUT", s1.url + "/x", koerper=b"a", erlaubt=(201,))


# ── Umleitungen ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_umleitung_behaelt_methode_rumpf_und_anmeldung(server, code):
    def antwort(a):
        if a.pfad == "/alt":
            return Antwort(code, {"Location": "/neu/"})
        return Antwort(207, {}, b"<ok/>")
    s1 = server(antwort)
    r = Sitzung(s1.url, "anna", PASSWORT).anfrage("PROPFIND", s1.url + "/alt", koerper=b"<body/>", kopf={"Depth": "1"}, erlaubt=(207,))
    assert r.url == s1.url + "/neu/"
    erste, zweite = s1.anfragen
    assert (zweite.methode, zweite.pfad, zweite.koerper, zweite.kopf["depth"]) == ("PROPFIND", "/neu/", b"<body/>", "1")
    assert zweite.kopf["authorization"] == erste.kopf["authorization"]


def test_303_wird_zu_get_ohne_rumpf(server):
    s1 = server(lambda a: Antwort(303, {"Location": "/danach"}) if a.pfad == "/vorher" else Antwort(200, {}, b"hier"))
    r = Sitzung(s1.url).anfrage("PUT", s1.url + "/vorher", koerper=b"daten", erlaubt=(200,))
    assert r.koerper == b"hier"
    assert [(a.methode, a.koerper) for a in s1.anfragen] == [("PUT", b"daten"), ("GET", b"")]


def test_umleitung_auf_anderen_host_oder_port_bekommt_kein_passwort(server):
    fremd = server(lambda a: Antwort(200, {}, b"abgegriffen"))
    s1 = server(lambda a: Antwort(307, {"Location": fremd.url + "/sammeln"}))
    with pytest.raises(NetzFehler) as e:
        Sitzung(s1.url, "anna", PASSWORT).anfrage("PROPFIND", s1.url + "/", koerper=b"<x/>")
    assert e.value.art == "umleitung" and f"127.0.0.1:{fremd.port}" in e.value.text and "kein Passwort" in e.value.text
    assert fremd.anfragen == []  # nichts, auch keine Anfrage ohne Passwort, ging an den fremden Host
    assert PASSWORT not in e.value.text


def test_umleitung_ohne_anmeldung_darf_ueberall_hin(server):
    ziel = server(lambda a: Antwort(200, {}, b"da"))
    s1 = server(lambda a: Antwort(302, {"Location": ziel.url + "/feed.ics"}))
    assert Sitzung(s1.url).anfrage("GET", s1.url + "/").koerper == b"da"
    assert "authorization" not in ziel.anfragen[0].kopf


@pytest.mark.parametrize("ort", ["file:///etc/passwd", "ftp://127.0.0.1/x", "gopher://x/", "javascript:alert(1)"])
def test_umleitung_auf_andere_schemata_wird_abgelehnt(server, ort):
    s1 = server(lambda a: Antwort(302, {"Location": ort}))
    with pytest.raises(NetzFehler) as e:
        Sitzung(s1.url).anfrage("GET", s1.url + "/")
    assert e.value.art == "umleitung"


def test_umleitungsschleife_und_umleitung_ohne_ziel(server):
    s1 = server(lambda a: Antwort(302, {"Location": "/schleife"}))
    with pytest.raises(NetzFehler) as e:
        Sitzung(s1.url).anfrage("GET", s1.url + "/schleife")
    assert e.value.art == "umleitung" and "Zu viele" in e.value.text
    assert len(s1.anfragen) == netz.UMLEITUNGEN_MAX + 1
    s2 = server(lambda a: Antwort(302))
    with pytest.raises(NetzFehler) as e:
        Sitzung(s2.url).anfrage("GET", s2.url + "/")
    assert e.value.art == "umleitung" and "kein Ziel" in e.value.text


# ── Grenzen ──────────────────────────────────────────────────────────────────────────────────────────────
def test_zu_grosse_antwort(server):
    s1 = server(lambda a: Antwort(200, {}, b"x" * 5000))
    with pytest.raises(NetzFehler) as e:
        Sitzung(s1.url, antwort_max=1000).anfrage("GET", s1.url + "/")
    assert e.value.art == "gross"
    assert Sitzung(s1.url, antwort_max=6000).anfrage("GET", s1.url + "/").koerper == b"x" * 5000


def test_gzip_wird_entpackt_und_eine_gzip_bombe_abgelehnt(server):
    inhalt = b"BEGIN:VCALENDAR\r\n" * 200
    s1 = server(lambda a: Antwort(200, {"Content-Encoding": "gzip"}, gzip.compress(inhalt)))
    assert Sitzung(s1.url).anfrage("GET", s1.url + "/").koerper == inhalt
    bombe = gzip.compress(b"\0" * (30 * 1024 * 1024), 9)
    assert len(bombe) < 100_000
    s2 = server(lambda a: Antwort(200, {"Content-Encoding": "gzip"}, bombe))
    with pytest.raises(NetzFehler) as e:
        Sitzung(s2.url, antwort_max=1024 * 1024).anfrage("GET", s2.url + "/")
    assert e.value.art == "gross"
    s3 = server(lambda a: Antwort(200, {"Content-Encoding": "gzip"}, b"das ist kein gzip"))
    with pytest.raises(NetzFehler) as e:
        Sitzung(s3.url).anfrage("GET", s3.url + "/")
    assert e.value.art == "antwort"


def test_zeitueberschreitung_eines_schritts(server):
    def langsam(a):
        time.sleep(0.8)
        return Antwort(200)
    s1 = server(langsam)
    start = time.perf_counter()
    with pytest.raises(NetzFehler) as e:
        Sitzung(s1.url, frist_s=0.2).anfrage("GET", s1.url + "/")
    assert e.value.art == "zeit" and "Zeitüberschreitung" in e.value.text and time.perf_counter() - start < 0.75


def test_gesamtfrist_gilt_auch_bei_tropfender_antwort():
    # Der Server liefert zuerst die Kopfzeilen und dann Stück für Stück Daten: jede Pause liegt unter der Frist je Schritt.
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            return

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b"x" * 10)
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        with pytest.raises(NetzFehler) as e:
            Sitzung(url, frist_s=1.0, gesamt_s=0.4).anfrage("GET", url)
        assert e.value.art == "zeit"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_verbindung_abgelehnt_und_name_nicht_aufloesbar(server, monkeypatch):
    s1 = server()
    url = s1.url
    s1.stop()
    with pytest.raises(NetzFehler) as e:
        Sitzung(url).anfrage("GET", url + "/")
    assert e.value.art == "verbindung" and "abgelehnt" in e.value.text

    def kein_name(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", kein_name)
    with pytest.raises(NetzFehler) as e:
        Sitzung("http://kalender.example.net/").anfrage("GET", "http://kalender.example.net/geheim/pfad")
    assert e.value.art == "verbindung" and "kalender.example.net" in e.value.text and "auflösen" in e.value.text
    assert "geheim" not in e.value.text


def test_fehlertexte_fuer_tls_und_abbrueche():
    zert = netz._netz_fehler(ssl.SSLCertVerificationError(1, "certificate verify failed"), "cloud.example.com")
    assert zert.art == "tls" and "SSL_CERT_FILE" in zert.text and "cloud.example.com" in zert.text
    assert netz._netz_fehler(ssl.SSLError("handshake"), "h").art == "tls"
    assert netz._netz_fehler(ConnectionResetError(), "h").art == "verbindung"
    assert netz._netz_fehler(TimeoutError(), "h").art == "zeit"
    sonst = netz._netz_fehler(OSError("Text mit https://nutzer:passwort" + "@" + "host.example.com/pfad"), "h")
    assert "passwort" not in sonst.text and "OSError" in sonst.text  # nur der Typ, nie der fremde Text


def test_nur_http_und_https(server):
    for adresse in ("ftp://127.0.0.1/x", "file:///etc/passwd", "keineadresse", "http://"):
        with pytest.raises(NetzFehler) as e:
            Sitzung("http://127.0.0.1").anfrage("GET", adresse)
        assert e.value.art in ("umleitung", "anfrage")
