"""Attrappen für die Tests: ein kleiner HTTP-Server (in einem Faden) und darauf aufbauend ein CalDAV-Server.

Nichts hier spricht mit einem echten Kalender-Dienst. Der Server hört nur auf 127.0.0.1 und merkt sich jede Anfrage,
damit Tests prüfen können, was das Plugin geschickt hat (und was nicht: etwa ein Passwort an einen fremden Host).
"""

from __future__ import annotations

import base64
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from xml.sax.saxutils import escape


@dataclass
class Anfrage:
    methode: str
    pfad: str
    kopf: dict[str, str]  # Namen kleingeschrieben
    koerper: bytes = b""


@dataclass
class Antwort:
    status: int = 200
    kopf: dict[str, str] = field(default_factory=dict)
    koerper: bytes = b""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: Any) -> None:  # still
        return

    def _bearbeite(self) -> None:
        laenge = int(self.headers.get("Content-Length") or 0)
        koerper = self.rfile.read(laenge) if laenge else b""
        anfrage = Anfrage(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, koerper)
        server: Server = self.server.fake  # type: ignore[attr-defined]
        antwort = server.verarbeite(anfrage)
        self.send_response(antwort.status)
        kopf = {"Content-Length": str(len(antwort.koerper)), **antwort.kopf}
        for name, wert in kopf.items():
            self.send_header(name, wert)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(antwort.koerper)

    do_GET = do_PUT = do_POST = do_PROPFIND = do_REPORT = do_HEAD = do_DELETE = do_OPTIONS = _bearbeite


class Server:
    """Ein HTTP-Server, der jede Anfrage an ``verarbeite`` gibt. Unterklassen oder ``antworter`` bestimmen die Antwort."""

    def __init__(self, antworter: Callable[[Anfrage], Antwort] | None = None) -> None:
        self.antworter = antworter
        self.anfragen: list[Anfrage] = []
        self._sperre = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.fake = self  # type: ignore[attr-defined]
        self._faden = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> "Server":
        self._faden.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._faden.join(timeout=5)

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def verarbeite(self, anfrage: Anfrage) -> Antwort:
        with self._sperre:
            self.anfragen.append(anfrage)
        if self.antworter is None:
            return Antwort(404)
        return self.antworter(anfrage)

    def mit(self, methode: str) -> list[Anfrage]:
        with self._sperre:
            return [a for a in self.anfragen if a.methode == methode]


def basic(nutzer: str, passwort: str) -> str:
    return "Basic " + base64.b64encode(f"{nutzer}:{passwort}".encode()).decode()


# ── CalDAV ───────────────────────────────────────────────────────────────────────────────────────────────
NS = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/"'


def ics(*ereignisse: str, name: str = "", zonen: str = "") -> str:
    """Eine Kalenderdatei aus den Zeilen der ``VEVENT``-Blöcke."""
    kopf = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//DE\r\n" + (f"X-WR-CALNAME:{name}\r\n" if name else "")
    return kopf + zonen + "".join(ereignisse) + "END:VCALENDAR\r\n"


def utc(zeit: str) -> str:
    """``20261012T090000`` als UTC-Wert einer Eigenschaft: ``:20261012T090000Z``."""
    return f":{zeit}Z"


def lokal(zone: str, zeit: str) -> str:
    """Wert mit Zeitzone: ``;TZID=Europe/Berlin:20261012T090000``."""
    return f";TZID={zone}:{zeit}"


def tag(datum: str) -> str:
    """Ganztägiger Wert: ``;VALUE=DATE:20261012``."""
    return f";VALUE=DATE:{datum}"


def vevent(uid: str, titel: str, start: str, ende: str = "", ort: str = "", extra: str = "") -> str:
    """Ein ``VEVENT``. ``start`` und ``ende`` sind Werte mit Parametern, wie ``utc``, ``lokal`` und ``tag`` sie liefern;
    ``extra`` sind weitere Zeilen (``RRULE:…``)."""
    zeilen = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{titel}", f"DTSTART{start}"]
    if ende:
        zeilen.append(f"DTEND{ende}")
    if ort:
        zeilen.append(f"LOCATION:{ort}")
    if extra:
        zeilen += [z for z in extra.splitlines() if z.strip()]
    zeilen.append("END:VEVENT")
    return "\r\n".join(zeilen) + "\r\n"


def res(text: str) -> tuple[str, str]:
    """Eine Ressource ohne Unterschied zwischen dem Text mit und ohne ``expand``."""
    return text, text


def termin(uid: str, titel: str, von: str, bis: str, zone: str = "Europe/Berlin", **kw: str) -> tuple[str, tuple[str, str]]:
    """Eine Ressource (Dateiname, Texte) mit einem Termin in Ortszeit: ``termin("a", "Zahnarzt", "20261012T080000", "20261012T090000")``."""
    return f"{uid}.ics", res(ics(vevent(uid, titel, lokal(zone, von), lokal(zone, bis), **kw)))


def ganztags(uid: str, titel: str, von: str, bis: str, **kw: str) -> tuple[str, tuple[str, str]]:
    """Ein ganztägiger Termin; ``bis`` ist der Tag nach dem letzten."""
    return f"{uid}.ics", res(ics(vevent(uid, titel, tag(von), tag(bis), **kw)))


class Uhr:
    """Eine Uhr, die der Test weiterstellt (für die Haltedauer der Zwischenspeicher)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def weiter(self, s: float) -> None:
        self.t += s


@dataclass
class FakeKalender:
    pfad: str  # /dav/calendars/alice/privat/
    name: str
    komponenten: tuple[str, ...] = ("VEVENT",)
    rechte: tuple[str, ...] | None = ("read", "write")  # None: der Server nennt keine Rechte
    #: Ressourcen: Dateiname -> (Text ohne expand, Text mit expand)
    ressourcen: dict[str, tuple[str, str]] = field(default_factory=dict)
    angelegt: list[tuple[str, str]] = field(default_factory=list)  # (Dateiname, Text) aus PUT


class FakeCalDav(Server):
    """Ein CalDAV-Server nach dem Muster von Nextcloud und Radicale: ``/.well-known/caldav`` → Wurzel →
    ``current-user-principal`` → ``calendar-home-set`` → Kalender."""

    def __init__(
        self,
        kalender: list[FakeKalender],
        *,
        nutzer: str | None = "alice",
        passwort: str = "geheim-app-passwort",
        wellknown: bool = True,
        expand: str = "ja",  # ja | http403 | http501 | multistatus403 | ignoriert
        wurzel: str = "/dav/",
        zuhause_host: str | None = None,  # absolute Adresse statt Pfad im calendar-home-set (fremder Host)
        put_status: int | None = None,
        put_412_zuerst: bool = False,
    ) -> None:
        super().__init__()
        self.kalender = kalender
        self.nutzer, self.passwort = nutzer, passwort
        self.wellknown = wellknown
        self.expand = expand
        self.wurzel = wurzel
        self.zuhause_host = zuhause_host
        self.put_status = put_status
        self.put_412_zuerst = put_412_zuerst
        self.principal = "/dav/principals/alice/"
        self.zuhause = "/dav/calendars/alice/"
        self.abgelehnt: list[Anfrage] = []

    # ── Antworten ─────────────────────────────────────────────────────────────────────────────────────
    def verarbeite(self, anfrage: Anfrage) -> Antwort:
        with self._sperre:
            self.anfragen.append(anfrage)
        if self.nutzer is not None and anfrage.kopf.get("authorization") != basic(self.nutzer, self.passwort):
            self.abgelehnt.append(anfrage)
            return Antwort(401, {"WWW-Authenticate": 'Basic realm="Kalender"'})
        pfad = anfrage.pfad.split("?")[0]
        if pfad == "/.well-known/caldav":
            if self.wellknown:
                return Antwort(301, {"Location": self.wurzel})
            return Antwort(404)
        if anfrage.methode == "PROPFIND":
            return self._propfind(pfad, anfrage)
        if anfrage.methode == "REPORT":
            return self._report(pfad, anfrage)
        if anfrage.methode == "PUT":
            return self._put(pfad, anfrage)
        return Antwort(405)

    @staticmethod
    def _multistatus(*antworten: str) -> Antwort:
        text = f'<?xml version="1.0" encoding="utf-8"?><d:multistatus {NS}>' + "".join(antworten) + "</d:multistatus>"
        return Antwort(207, {"Content-Type": "application/xml; charset=utf-8"}, text.encode())

    @staticmethod
    def _eintrag(href: str, gefunden: str, fehlend: str = "") -> str:
        ok = f"<d:propstat><d:prop>{gefunden}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        nein = f"<d:propstat><d:prop>{fehlend}</d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>" if fehlend else ""
        return f"<d:response><d:href>{escape(href)}</d:href>{ok}{nein}</d:response>"

    def _kalender_eintrag(self, k: FakeKalender) -> str:
        art = "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
        name = f"<d:displayname>{escape(k.name)}</d:displayname>"
        komp = "<c:supported-calendar-component-set>" + "".join(f'<c:comp name="{c}"/>' for c in k.komponenten) + "</c:supported-calendar-component-set>"
        rechte = ""
        if k.rechte is not None:
            rechte = "<d:current-user-privilege-set>" + "".join(f"<d:privilege><d:{r}/></d:privilege>" for r in k.rechte) + "</d:current-user-privilege-set>"
        return self._eintrag(k.pfad, art + name + komp + rechte)

    def _propfind(self, pfad: str, anfrage: Anfrage) -> Antwort:
        tiefe = anfrage.kopf.get("depth", "0")
        norm = pfad.rstrip("/") + "/"
        if norm in ("/", self.wurzel):
            haupt = ("<d:resourcetype><d:collection/></d:resourcetype>"
                     f"<d:current-user-principal><d:href>{self.principal}</d:href></d:current-user-principal>")
            return self._multistatus(self._eintrag(pfad, haupt, "<d:displayname/><c:calendar-home-set/>"))
        if norm == self.principal:
            ziel = self.zuhause if self.zuhause_host is None else f"{self.zuhause_host}{self.zuhause}"
            haupt = ("<d:resourcetype><d:collection/><d:principal/></d:resourcetype>"
                     f"<c:calendar-home-set><d:href>{ziel}</d:href></c:calendar-home-set>")
            return self._multistatus(self._eintrag(pfad, haupt))
        if norm == self.zuhause:
            eintraege = [self._eintrag(pfad, "<d:resourcetype><d:collection/></d:resourcetype>")]
            if tiefe == "1":
                eintraege += [self._kalender_eintrag(k) for k in self.kalender]
                # Dinge, die keine Kalender sind (Posteingang) oder keine Termine nehmen (Aufgaben):
                eintraege.append(self._eintrag(self.zuhause + "inbox/", "<d:resourcetype><d:collection/><c:schedule-inbox/></d:resourcetype>"))
            return self._multistatus(*eintraege)
        for k in self.kalender:
            if norm == k.pfad.rstrip("/") + "/":
                return self._multistatus(self._kalender_eintrag(k))
        return Antwort(404)

    def _report(self, pfad: str, anfrage: Anfrage) -> Antwort:
        kal = next((k for k in self.kalender if pfad.rstrip("/") + "/" == k.pfad.rstrip("/") + "/"), None)
        if kal is None:
            return Antwort(404)
        wurzel = ET.fromstring(anfrage.koerper)
        mit_expand = wurzel.find(".//{urn:ietf:params:xml:ns:caldav}expand") is not None
        if mit_expand and self.expand == "http403":
            return Antwort(403)
        if mit_expand and self.expand == "http501":
            return Antwort(501)
        antworten = []
        for name, (roh, expandiert) in kal.ressourcen.items():
            href = kal.pfad + name
            if mit_expand and self.expand == "multistatus403":
                antworten.append(
                    f"<d:response><d:href>{href}</d:href><d:propstat><d:prop><c:calendar-data/></d:prop>"
                    "<d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat></d:response>")
                continue
            text = expandiert if mit_expand and self.expand == "ja" else roh
            antworten.append(self._eintrag(href, f"<c:calendar-data>{escape(text)}</c:calendar-data>"))
        return self._multistatus(*antworten)

    def _put(self, pfad: str, anfrage: Anfrage) -> Antwort:
        kal = next((k for k in self.kalender if pfad.startswith(k.pfad)), None)
        if kal is None:
            return Antwort(404)
        if self.put_status is not None:
            return Antwort(self.put_status)
        if self.put_412_zuerst and len(self.mit("PUT")) == 1:
            return Antwort(412)
        name = pfad[len(kal.pfad):]
        if anfrage.kopf.get("if-none-match") == "*" and (name in kal.ressourcen or any(n == name for n, _ in kal.angelegt)):
            return Antwort(412)
        text = anfrage.koerper.decode("utf-8")
        kal.angelegt.append((name, text))
        kal.ressourcen[name] = res(text)  # ab jetzt liefert auch der REPORT den neuen Termin
        return Antwort(201, {"ETag": '"1"'})
