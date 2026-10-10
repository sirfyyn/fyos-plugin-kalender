"""CalDAV (RFC 4791) in Bausteinen: die Anfragen und das Lesen der Antworten.

Der Weg zu den Terminen eines Kontos:

1. **Erkennen.** ``PROPFIND`` auf die eingetragene Adresse (bei einer reinen Server-Adresse zuerst auf
   ``/.well-known/caldav``, dann auf ``/``) fragt, was dort liegt. Ein Kalender (``resourcetype`` mit ``calendar``) ist
   das Ziel selbst; sonst führt ``current-user-principal`` zum Principal und dessen ``calendar-home-set`` zum Ordner,
   in dem die Kalender liegen.
2. **Kalender auflisten.** ``PROPFIND`` mit Tiefe 1 auf den Ordner; behalten werden Sammlungen mit ``calendar`` im
   ``resourcetype``, die Termine (``VEVENT``) aufnehmen.
3. **Termine holen.** ``REPORT`` ``calendar-query`` mit Zeitraum, wenn der Server es kann mit ``expand`` (er löst
   Wiederholungen selbst auf), sonst ohne; was als Wiederholungsregel zurückkommt, löst ``ical`` auf.
4. **Anlegen.** ``PUT`` auf ``<Kalender>/<UID>.ics`` mit ``If-None-Match: *``: ein vorhandener Termin wird nie
   überschrieben.

Dieses Modul kennt kein Netz; ``quellen.CalDavQuelle`` setzt es mit ``netz.Sitzung`` zusammen.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

from . import zeit
from .netz import NetzFehler

DAV = "{DAV:}"
CALDAV = "{urn:ietf:params:xml:ns:caldav}"
KALENDER_MAX = 100

_KOPF_XML = {"Content-Type": "application/xml; charset=utf-8"}

ERKENNEN = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
    "<d:resourcetype/><d:displayname/><d:current-user-principal/><c:calendar-home-set/>"
    "<c:supported-calendar-component-set/><d:current-user-privilege-set/>"
    "</d:prop></d:propfind>"
).encode("utf-8")

ZUHAUSE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
    "<c:calendar-home-set/>"
    "</d:prop></d:propfind>"
).encode("utf-8")

KALENDER_LISTE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
    "<d:resourcetype/><d:displayname/><c:supported-calendar-component-set/><d:current-user-privilege-set/>"
    "</d:prop></d:propfind>"
).encode("utf-8")


def kopf_xml(tiefe: str | None = None) -> dict[str, str]:
    """Kopfzeilen für Anfragen mit XML-Rumpf (``Depth`` bei ``PROPFIND`` und ``REPORT``)."""
    return {**_KOPF_XML, **({"Depth": tiefe} if tiefe is not None else {})}


def report_koerper(von: datetime, bis: datetime, aufloesen: bool) -> bytes:
    """``calendar-query`` für ``[von, bis)``; mit ``aufloesen`` bittet er den Server, Wiederholungen in einzelne Termine
    aufzulösen (``expand``)."""
    a, b = zeit.utc_text(von), zeit.utc_text(bis)
    daten = f'<c:calendar-data><c:expand start="{a}" end="{b}"/></c:calendar-data>' if aufloesen else "<c:calendar-data/>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        f"<d:prop>{daten}</d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        f'<c:time-range start="{a}" end="{b}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    ).encode("utf-8")


# ── XML lesen ───────────────────────────────────────────────────────────────────────────────────────────
_GEFAEHRLICH = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


def lies_xml(roh: bytes) -> ET.Element:
    """Eine Antwort als XML. Dokumenttyp-Angaben und Entitäten gibt es in CalDAV-Antworten nicht; wer sie schickt, will
    den Parser angreifen (Entitäten-Vervielfachung), und wird abgewiesen."""
    if _GEFAEHRLICH.search(roh):
        raise NetzFehler("antwort", "Der Server schickt XML mit Dokumenttyp oder Entitäten; das Plugin liest es nicht")
    try:
        return ET.fromstring(roh)
    except ET.ParseError:
        raise NetzFehler("antwort", "Der Server antwortet mit unlesbarem XML (ist das ein CalDAV-Server?)") from None


def _status_ok(text: str | None) -> bool:
    """Ist die Statuszeile ``HTTP/1.1 200 OK`` eine Erfolgsmeldung? Ohne Angabe gilt sie als Erfolg."""
    teile = (text or "").split()
    if len(teile) >= 2 and teile[1].isdigit():
        return 200 <= int(teile[1]) < 300
    return True


@dataclass
class Eintrag:
    """Eine ``response`` eines ``PROPFIND``: was die Adresse ist und was sie über sich verrät."""

    href: str
    kalender: bool = False  # resourcetype enthält calendar
    sammlung: bool = False  # resourcetype enthält collection
    name: str = ""
    komponenten: set[str] | None = None  # supported-calendar-component-set; None: nicht angegeben
    rechte: set[str] | None = None  # current-user-privilege-set; None: nicht angegeben
    principal: str = ""
    zuhause: list[str] = field(default_factory=list)  # calendar-home-set
    nicht_angemeldet: bool = False  # current-user-principal meldet „unauthenticated“

    @property
    def nimmt_termine(self) -> bool:
        """Nimmt die Sammlung Termine auf? Ohne Angabe: ja."""
        return self.komponenten is None or not self.komponenten or "VEVENT" in self.komponenten

    @property
    def schreibbar(self) -> bool | None:
        """Darf der Nutzer Termine anlegen? ``None``: der Server sagt es nicht."""
        if self.rechte is None:
            return None
        return bool(self.rechte & {"all", "write", "bind"})


def propfind_eintraege(roh: bytes) -> list[Eintrag]:
    """Die Einträge einer ``207``-Antwort auf ``PROPFIND``. Eigenschaften mit Fehlerstatus zählen als nicht vorhanden."""
    wurzel = lies_xml(roh)
    eintraege: list[Eintrag] = []
    for antwort in wurzel.iter(DAV + "response"):
        href = (antwort.findtext(DAV + "href") or "").strip()
        eigenschaften: dict[str, ET.Element] = {}
        for satz in antwort.findall(DAV + "propstat"):
            if not _status_ok(satz.findtext(DAV + "status")):
                continue
            prop = satz.find(DAV + "prop")
            if prop is not None:
                for kind in prop:
                    eigenschaften[kind.tag] = kind
        e = Eintrag(href=href)
        art = eigenschaften.get(DAV + "resourcetype")
        if art is not None:
            e.kalender = art.find(CALDAV + "calendar") is not None
            e.sammlung = art.find(DAV + "collection") is not None
        name = eigenschaften.get(DAV + "displayname")
        if name is not None:
            e.name = " ".join((name.text or "").split())[:200]
        komponenten = eigenschaften.get(CALDAV + "supported-calendar-component-set")
        if komponenten is not None:
            e.komponenten = {(c.get("name") or "").upper() for c in komponenten.findall(CALDAV + "comp")}
        rechte = eigenschaften.get(DAV + "current-user-privilege-set")
        if rechte is not None:
            e.rechte = {
                kind.tag.rsplit("}", 1)[-1] for privileg in rechte.findall(DAV + "privilege") for kind in privileg
            }
        principal = eigenschaften.get(DAV + "current-user-principal")
        if principal is not None:
            e.principal = (principal.findtext(DAV + "href") or "").strip()
            e.nicht_angemeldet = principal.find(DAV + "unauthenticated") is not None
        zuhause = eigenschaften.get(CALDAV + "calendar-home-set")
        if zuhause is not None:
            e.zuhause = [(h.text or "").strip() for h in zuhause.findall(DAV + "href") if (h.text or "").strip()]
        eintraege.append(e)
    return eintraege


def report_daten(roh: bytes) -> tuple[list[str], list[int]]:
    """Die Kalenderdaten (iCalendar-Texte) einer ``207``-Antwort auf ``REPORT`` und die Fehlerstatus der Einträge, die der
    Server nicht liefern konnte (zum Beispiel 403, weil er ``expand`` nicht erlaubt)."""
    wurzel = lies_xml(roh)
    daten: list[str] = []
    fehler: list[int] = []
    for antwort in wurzel.iter(DAV + "response"):
        gefunden = False
        for satz in antwort.findall(DAV + "propstat"):
            status = (satz.findtext(DAV + "status") or "").split()
            prop = satz.find(DAV + "prop")
            kalender_daten = prop.find(CALDAV + "calendar-data") if prop is not None else None
            if _status_ok(satz.findtext(DAV + "status")):
                if kalender_daten is not None and (kalender_daten.text or "").strip():
                    daten.append(kalender_daten.text or "")
                    gefunden = True
            elif len(status) >= 2 and status[1].isdigit() and kalender_daten is not None:
                fehler.append(int(status[1]))
        if not gefunden:
            antwort_status = (antwort.findtext(DAV + "status") or "").split()
            if len(antwort_status) >= 2 and antwort_status[1].isdigit() and not _status_ok(antwort.findtext(DAV + "status")):
                fehler.append(int(antwort_status[1]))
    return daten, fehler


def aufloesen(basis: str, href: str) -> str:
    """Die absolute Adresse zu einem ``href`` (absolut, mit Pfad oder relativ) relativ zur Adresse der Anfrage."""
    return urllib.parse.urljoin(basis, href)


def name_aus_pfad(url: str) -> str:
    """Letzter Teil des Pfades als Ersatz für einen fehlenden Anzeigenamen."""
    pfad = urllib.parse.urlsplit(url).path.rstrip("/")
    return urllib.parse.unquote(pfad.rsplit("/", 1)[-1])[:200] if pfad else ""
