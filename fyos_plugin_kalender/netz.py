"""HTTP zu Kalender-Servern: Anmeldung mit Nutzer und App-Passwort, Fristen, Größengrenzen, Umleitungen von Hand.

Das Passwort geht nur in die Kopfzeile ``Authorization`` und nie in einen Text, ein Log oder ein Ergebnis. Es geht
nur an den Server, den der Besitzer eingetragen hat, und an dessen Geschwister unter derselben Domain, solange alles
über https läuft (Apples CalDAV leitet von ``caldav.…`` auf ``pNN-caldav.…`` weiter). Jedes andere Ziel einer Umleitung
oder eines ``href`` lehnt das Plugin ab, statt das Passwort hinzuschicken.

Fehler sind ``NetzFehler`` mit einem Text, den man dem Besitzer zeigen darf.
"""

from __future__ import annotations

import base64
import http.client
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any

from . import __version__

#: Frist für jeden einzelnen Netzschritt (Verbinden, auf Daten warten)
FRIST_S = 10.0
#: Frist für eine ganze Antwort, auch wenn der Server sie tropfenweise liefert
GESAMT_S = 25.0
ANTWORT_MAX = 8 * 1024 * 1024
UMLEITUNGEN_MAX = 5

#: Unter diesen Endungen sind die Namen auf der zweiten Ebene vergeben (co.uk, com.au): die Basis hat drei Teile.
_ZWEITE_EBENE = {"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne"}
#: Dienste, bei denen fremde Kunden Geschwister-Hosts unter derselben Domain haben: dort gilt „gleiche Domain“ nicht.
_GETEILT = {
    "github.io", "gitlab.io", "herokuapp.com", "appspot.com", "blogspot.com", "azurewebsites.net", "cloudfront.net",
    "amazonaws.com", "netlify.app", "vercel.app", "pages.dev", "workers.dev", "web.app", "firebaseapp.com",
    "onrender.com", "fly.dev", "glitch.me", "repl.co", "ngrok.io", "ngrok-free.app", "trycloudflare.com",
    "duckdns.org", "no-ip.org", "dyndns.org", "myfritz.net", "ddns.net", "hopto.org",
}


class NetzFehler(Exception):
    """Ein Fehler auf dem Weg zum Kalender-Server. ``text`` darf gezeigt werden (keine Zugangsdaten).

    ``art``: ``auth`` (Anmeldung abgelehnt), ``verboten`` (403), ``nicht_gefunden`` (404/410), ``anfrage`` (Server lehnt
    die Anfrage ab), ``server`` (5xx, 429), ``zeit``, ``verbindung``, ``tls``, ``antwort`` (unlesbar), ``gross``,
    ``umleitung``."""

    def __init__(self, art: str, text: str, status: int = 0) -> None:
        super().__init__(text)
        self.art = art
        self.text = text
        self.status = status


@dataclass
class Antwort:
    status: int
    kopf: dict[str, str]  # Namen kleingeschrieben
    koerper: bytes
    url: str


class _KeineUmleitung(urllib.request.HTTPRedirectHandler):
    """Folgt keiner Umleitung von selbst: ``Sitzung.anfrage`` entscheidet, wohin das Passwort darf."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_OFFEN = urllib.request.build_opener(_KeineUmleitung)


def host_text(ziel: urllib.parse.SplitResult) -> str:
    """``host`` oder ``host:port`` für Meldungen (nie der Pfad: er kann einen Nutzernamen oder ein Geheimnis tragen)."""
    host = ziel.hostname or "?"
    if ":" in host:
        host = f"[{host}]"
    try:
        port = ziel.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port else host


def _port(ziel: urllib.parse.SplitResult) -> int:
    try:
        if ziel.port:
            return ziel.port
    except ValueError:
        return -1
    return 443 if ziel.scheme == "https" else 80


def _basisdomain(host: str) -> str | None:
    teile = host.lower().rstrip(".").split(".")
    if len(teile) < 2 or ":" in host or all(t.isdigit() for t in teile):
        return None
    if len(teile) >= 3 and len(teile[-1]) == 2 and teile[-2] in _ZWEITE_EBENE:
        return ".".join(teile[-3:])
    return ".".join(teile[-2:])


def gleiche_seite(start: urllib.parse.SplitResult, ziel: urllib.parse.SplitResult) -> bool:
    """Darf das Passwort, das für ``start`` gilt, an ``ziel`` gehen? Ja bei demselben Host mit demselben Schema und
    Port, bei der Aufwertung von http (Port 80) auf https (Port 443) und bei Geschwistern unter derselben Domain, wenn
    beide https auf demselben Port sprechen. Alles andere nicht (wie bei curl und requests)."""
    if ziel.scheme not in ("http", "https"):
        return False
    h_start, h_ziel = (start.hostname or "").lower(), (ziel.hostname or "").lower()
    if not h_start or not h_ziel:
        return False
    if start.scheme == "https" and ziel.scheme != "https":
        return False
    p_start, p_ziel = _port(start), _port(ziel)
    if h_start == h_ziel:
        if start.scheme == ziel.scheme:
            return p_start == p_ziel
        return p_start == 80 and p_ziel == 443
    if start.scheme != "https" or ziel.scheme != "https" or p_start != p_ziel:
        return False
    basis = _basisdomain(h_start)
    return basis is not None and basis == _basisdomain(h_ziel) and basis not in _GETEILT


def _netz_fehler(e: BaseException, host: str) -> NetzFehler:
    ursache = e.reason if isinstance(e, urllib.error.URLError) and not isinstance(e.reason, str) else e
    if isinstance(ursache, TimeoutError):
        return NetzFehler("zeit", f"Zeitüberschreitung bei {host}")
    if isinstance(ursache, ssl.SSLCertVerificationError):
        return NetzFehler(
            "tls",
            f"Das Zertifikat von {host} wird nicht akzeptiert (abgelaufen, falscher Name oder eigene Zertifizierungsstelle: "
            "dann deren Zertifikat über SSL_CERT_FILE eintragen)",
        )
    if isinstance(ursache, ssl.SSLError):
        return NetzFehler("tls", f"TLS-Fehler bei {host}")
    if isinstance(ursache, ConnectionRefusedError):
        return NetzFehler("verbindung", f"Verbindung zu {host} abgelehnt")
    if isinstance(ursache, socket.gaierror):
        return NetzFehler("verbindung", f"Der Name {host} lässt sich nicht auflösen")
    if isinstance(ursache, (ConnectionError, http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return NetzFehler("verbindung", f"Die Verbindung zu {host} wurde unterbrochen")
    name = type(ursache).__name__ if not isinstance(ursache, str) else "Fehler"
    return NetzFehler("verbindung", f"{host} nicht erreichbar ({name})")


class Sitzung:
    """Der Zugang zu einem Kalender-Server. ``start`` ist die eingetragene Adresse; Nutzer und Passwort gelten für sie."""

    def __init__(
        self,
        start: str,
        nutzer: str = "",
        passwort: str = "",
        *,
        frist_s: float = FRIST_S,
        gesamt_s: float = GESAMT_S,
        antwort_max: int = ANTWORT_MAX,
    ) -> None:
        self._start = urllib.parse.urlsplit(start)
        self._auth = (
            "Basic " + base64.b64encode(f"{nutzer}:{passwort}".encode("utf-8")).decode("ascii") if nutzer or passwort else ""
        )
        self.frist_s = frist_s
        self.gesamt_s = gesamt_s
        self.antwort_max = antwort_max
        self.host = host_text(self._start)

    def __repr__(self) -> str:  # nie das Passwort
        return f"Sitzung({self.host})"

    @property
    def mit_anmeldung(self) -> bool:
        return bool(self._auth)

    # ── eine Anfrage, mit Umleitungen ────────────────────────────────────────────────────────────────
    def anfrage(
        self,
        methode: str,
        url: str,
        *,
        koerper: bytes | None = None,
        kopf: dict[str, str] | None = None,
        erlaubt: tuple[int, ...] = (200, 207),
    ) -> Antwort:
        """Schickt die Anfrage und folgt Umleitungen (301, 302, 307, 308 mit derselben Methode und demselben Rumpf; 303
        als GET). Gibt die Antwort zurück, wenn ihr Status in ``erlaubt`` steht; sonst ``NetzFehler``."""
        aktuell, meth, rumpf = url, methode, koerper
        antwort: Antwort | None = None
        for _ in range(UMLEITUNGEN_MAX + 1):
            ziel = urllib.parse.urlsplit(aktuell)
            if ziel.scheme not in ("http", "https") or not ziel.hostname:
                raise NetzFehler("umleitung", "Ungültige Adresse (erlaubt sind http:// und https://)")
            if self._auth and not gleiche_seite(self._start, ziel):
                raise NetzFehler(
                    "umleitung",
                    f"Der Server verweist auf ein anderes Ziel ({host_text(ziel)}); dorthin sendet das Plugin kein Passwort "
                    "(die Adresse des Kontos dann direkt eintragen)",
                )
            antwort = self._einmal(meth, aktuell, rumpf, kopf, bool(self._auth))
            if antwort.status in (301, 302, 303, 307, 308):
                ort = antwort.kopf.get("location", "").strip()
                if not ort:
                    raise NetzFehler("umleitung", f"{self.host} leitet um, nennt aber kein Ziel", antwort.status)
                aktuell = urllib.parse.urljoin(aktuell, ort)
                if antwort.status == 303 and meth not in ("GET", "HEAD"):
                    meth, rumpf = "GET", None
                antwort = None
                continue
            break
        if antwort is None:
            raise NetzFehler("umleitung", f"Zu viele Umleitungen bei {self.host}")
        if antwort.status in erlaubt:
            return antwort
        raise self._abgelehnt(antwort, methode)

    def _abgelehnt(self, antwort: Antwort, methode: str) -> NetzFehler:
        s = antwort.status
        if s == 401:
            herausforderung = antwort.kopf.get("www-authenticate", "").lower()
            if not self._auth:
                return NetzFehler("auth", f"{self.host} verlangt eine Anmeldung (Nutzer und App-Passwort fehlen)", s)
            if "digest" in herausforderung and "basic" not in herausforderung:
                return NetzFehler("auth", f"{self.host} verlangt eine Digest-Anmeldung, die das Plugin nicht kann", s)
            return NetzFehler("auth", "Anmeldung abgelehnt (App-Passwort prüfen)", s)
        if s == 403:
            return NetzFehler("verboten", "Zugriff verweigert (HTTP 403)", s)
        if s in (404, 410):
            return NetzFehler("nicht_gefunden", f"Adresse nicht gefunden (HTTP {s})", s)
        if s in (405, 501):
            return NetzFehler("anfrage", f"{self.host} lehnt {methode} ab (HTTP {s}); ist das ein CalDAV-Server?", s)
        if s == 429:
            return NetzFehler("server", f"{self.host} bremst (HTTP 429); später wieder versuchen", s)
        if s >= 500:
            return NetzFehler("server", f"{self.host} meldet einen Fehler (HTTP {s})", s)
        return NetzFehler("anfrage", f"Unerwartete Antwort von {self.host} (HTTP {s})", s)

    # ── ein Schritt ───────────────────────────────────────────────────────────────────────────────────
    def _einmal(self, methode: str, url: str, rumpf: bytes | None, kopf: dict[str, str] | None, mit_auth: bool) -> Antwort:
        zeilen = {"User-Agent": f"fyos-plugin-kalender/{__version__}", "Accept-Encoding": "gzip", **(kopf or {})}
        if mit_auth:
            zeilen["Authorization"] = self._auth
        try:
            req = urllib.request.Request(url, data=rumpf, method=methode, headers=zeilen)
        except ValueError:
            raise NetzFehler("anfrage", "Ungültige Adresse") from None
        host = host_text(urllib.parse.urlsplit(url))
        frist_ende = time.monotonic() + self.gesamt_s
        try:
            with _OFFEN.open(req, timeout=self.frist_s) as r:  # noqa: S310 — Adresse aus der Konfiguration, Schema geprüft
                status, kopfzeilen = r.status, {k.lower(): v for k, v in r.headers.items()}
                roh = self._lies(r, frist_ende, host)
        except urllib.error.HTTPError as e:
            status, kopfzeilen = e.code, {k.lower(): v for k, v in (e.headers or {}).items()}
            try:
                roh = self._lies(e, frist_ende, host, grenze=65536) if e.fp else b""
            except (NetzFehler, OSError, http.client.HTTPException):
                roh = b""  # der Rumpf einer Fehlerantwort ist nur Beiwerk
        except NetzFehler:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise _netz_fehler(e, host) from None
        if kopfzeilen.get("content-encoding", "").lower() == "gzip" and roh:
            roh = self._entpacken(roh, host)
        return Antwort(status, kopfzeilen, roh, url)

    def _lies(self, r: Any, frist_ende: float, host: str, grenze: int | None = None) -> bytes:
        grenze = grenze or self.antwort_max
        stuecke: list[bytes] = []
        summe = 0
        while True:
            if time.monotonic() > frist_ende:
                raise NetzFehler("zeit", f"Zeitüberschreitung bei {host}")
            try:
                teil = r.read(65536)
            except (OSError, http.client.HTTPException) as e:
                raise _netz_fehler(e, host) from None
            if not teil:
                return b"".join(stuecke)
            summe += len(teil)
            if summe > grenze:
                raise NetzFehler("gross", f"Die Antwort von {host} ist zu groß (über {grenze // (1024 * 1024) or 1} MiB)")
            stuecke.append(teil)

    def _entpacken(self, roh: bytes, host: str) -> bytes:
        try:
            auspacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
            aus = auspacker.decompress(roh, self.antwort_max + 1)
        except zlib.error:
            raise NetzFehler("antwort", f"Die Antwort von {host} ist nicht lesbar (gzip)") from None
        if len(aus) > self.antwort_max or auspacker.unconsumed_tail:
            raise NetzFehler("gross", f"Die Antwort von {host} ist zu groß (über {self.antwort_max // (1024 * 1024)} MiB)")
        return aus
