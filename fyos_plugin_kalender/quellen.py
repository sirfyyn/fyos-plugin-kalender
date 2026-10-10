"""Quellen: ein Konto, das Termine liefert. ``CalDavQuelle`` spricht mit einem CalDAV-Server, ``IcsQuelle`` liest ein
Abonnement (eine Adresse, die eine iCalendar-Datei liefert).

Beide haben dieselbe Form:

* ``kalender(neu_laden)``: die Kalender des Kontos (``Kalender``). CalDAV findet sie selbst (siehe ``caldav``) und merkt
  sie sich eine Stunde; ein ICS-Abonnement ist ein Kalender mit dem Namen des Kontos (der Abruf prüft die Adresse).
* ``instanzen(kalender, von, bis)``: die einzelnen Termine im Fenster ``[von, bis)``, Wiederholungen aufgelöst. Gefragt
  wird tageweise ausgerichtet (so trifft derselbe Abruf, der sekündlich anders beginnt, den Zwischenspeicher: 60 s); das
  Fenster des Aufrufers schneidet erst danach zu.
* ``anlegen(kalender, termin, jetzt)``: nur CalDAV. ``PUT`` mit ``If-None-Match: *``; ein vorhandener Termin wird nie
  überschrieben.

Fehler sind ``NetzFehler`` (oder ``KontoFehler``) mit einem Text ohne Zugangsdaten.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import caldav, ical, zeit
from .konten import Konto
from .netz import Antwort, NetzFehler, Sitzung

_log = logging.getLogger("fyos_plugin_kalender")

#: So lange gilt die Liste der Kalender eines CalDAV-Kontos
KALENDER_TTL_S = 3600.0
#: So lange gelten abgerufene Termine (je Kalender und Tagesfenster)
TERMINE_TTL_S = 60.0
#: So lange gilt ein abgerufenes ICS-Abonnement
FEED_TTL_S = 600.0
#: Gesamtfrist für das Finden der Kalender (mehrere Schritte nacheinander)
ENTDECKEN_S = 45.0
#: So viele Zwischenspeicher-Einträge je Konto
SPEICHER_MAX = 64
#: So viele einzelne Termine liefert ein Kalender höchstens je Abruf
INSTANZEN_MAX = 5000
OHNE_TITEL = "(ohne Titel)"
#: Antworten, nach denen der Abruf ohne ``expand`` wiederholt wird (der Server mag es nicht)
_OHNE_AUFLOESEN = (400, 403, 415, 422, 500, 501)

Uhr = Callable[[], float]


class KontoFehler(NetzFehler):
    """Ein Konto lässt sich nicht lesen, ohne dass das Netz schuld ist (kein Kalender gefunden, kein iCalendar)."""


@dataclass(frozen=True)
class Kalender:
    """Ein Kalender eines Kontos. ``url`` (CalDAV) erscheint in keiner Antwort."""

    id: str
    name: str
    konto: str
    schreibbar: bool
    url: str = field(default="", repr=False, compare=False)

    def als_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "konto": self.konto, "schreibbar": self.schreibbar}


# ── Termine als Terminformat ─────────────────────────────────────────────────────────────────────────────
def termin_id(kalender: Kalender, uid: str, kennung: str = "") -> str:
    """``<Kalender-Kennung>:<UID>[#<Kennung der Instanz>]``; sehr lange oder unsaubere UIDs werden gekürzt."""
    sauber = re.sub(r"\s+", "_", uid.strip())
    if not sauber or len(sauber) > 100:
        sauber = hashlib.sha1(uid.encode("utf-8", "replace")).hexdigest()[:16]  # noqa: S324 — nur eine Kennung
    return f"{kalender.id}:{sauber}" + (f"#{kennung}" if kennung else "")


def _zeitpunkt(wert: date | datetime) -> datetime:
    return zeit.lokalisiere(wert) if isinstance(wert, datetime) else zeit.tagesanfang(wert)


def termin_dict(
    kalender: Kalender, uid: str, kennung: str, titel: str, ort: str, start: date | datetime, ende: date | datetime,
    ganztaegig: bool,
) -> dict[str, Any]:
    """Ein Termin im Terminformat des Kerns: ``id titel start ende ganztaegig ort kalender konto``."""
    if ganztaegig and not isinstance(start, datetime):
        s = start.isoformat()
        e = (ende.date() if isinstance(ende, datetime) else ende).isoformat()
    else:
        s, e = zeit.iso(_zeitpunkt(start)), zeit.iso(_zeitpunkt(ende))
    return {
        "id": termin_id(kalender, uid, kennung),
        "titel": " ".join((titel or "").split())[:200] or OHNE_TITEL,
        "start": s,
        "ende": e,
        "ganztaegig": bool(ganztaegig),
        "ort": " ".join((ort or "").split())[:200],
        "kalender": kalender.name[:200],
        "konto": kalender.konto[:200],
    }


def instanz_dict(kalender: Kalender, i: ical.Instanz) -> dict[str, Any]:
    return termin_dict(kalender, i.uid, i.kennung, i.titel, i.ort, i.start, i.ende, i.ganztaegig)


def sortiert(termine: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nach Beginn; ganztägige eines Tages zuerst (wie die Termin-Quelle des Kerns)."""

    def schluessel(t: dict[str, Any]) -> tuple[Any, ...]:
        try:
            s = datetime.fromisoformat(t["start"]) if not t["ganztaegig"] else datetime.combine(
                date.fromisoformat(t["start"]), datetime.min.time())
            anfang = zeit.lokalisiere(s).astimezone(timezone.utc)
        except (ValueError, OverflowError, OSError):
            anfang = datetime.max.replace(tzinfo=timezone.utc)
        return anfang, 0 if t["ganztaegig"] else 1, str(t["titel"]).casefold(), str(t["id"])

    return sorted(termine, key=schluessel)


def auswahl(kalender: list[Kalender], text: str) -> list[Kalender]:
    """Die Kalender, die zu ``text`` passen: erst Name oder Kennung genau (Groß-/Kleinschreibung egal), sonst ein Teil
    des Namens."""
    gesucht = text.casefold()
    genau = [k for k in kalender if gesucht in (k.name.casefold(), k.id.casefold())]
    if genau:
        return genau
    return [k for k in kalender if gesucht in k.name.casefold()]


# ── Zwischenspeicher ─────────────────────────────────────────────────────────────────────────────────────
class Speicher:
    """Kleiner Zwischenspeicher mit Haltedauer; sicher für mehrere Fäden."""

    def __init__(self, ttl_s: float, uhr: Uhr | None = None, maximum: int = SPEICHER_MAX) -> None:
        self._ttl = ttl_s
        self._uhr = uhr or time.monotonic
        self._max = maximum
        self._daten: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._sperre = threading.Lock()

    def hole(self, schluessel: Any) -> Any | None:
        with self._sperre:
            eintrag = self._daten.get(schluessel)
            if eintrag is None:
                return None
            if self._uhr() - eintrag[0] >= self._ttl:
                del self._daten[schluessel]
                return None
            return eintrag[1]

    def setze(self, schluessel: Any, wert: Any) -> None:
        with self._sperre:
            self._daten[schluessel] = (self._uhr(), wert)
            self._daten.move_to_end(schluessel)
            while len(self._daten) > self._max:
                self._daten.popitem(last=False)

    def leere(self) -> None:
        with self._sperre:
            self._daten.clear()

    def __len__(self) -> int:
        with self._sperre:
            return len(self._daten)


def tagesfenster(von: datetime, bis: datetime) -> tuple[datetime, datetime]:
    """Das Fenster auf ganze Tage der Zeitzone des Hubs erweitert (Mitternacht vor ``von`` bis Mitternacht nach ``bis``)."""
    a, b = zeit.in_hub_zone(von), zeit.in_hub_zone(bis)
    tag_b = b.date() if b.time() == datetime.min.time() else b.date() + timedelta(days=1)
    tag_a = a.date()
    if tag_b <= tag_a:
        tag_b = tag_a + timedelta(days=1)
    return zeit.tagesanfang(tag_a), zeit.tagesanfang(tag_b)


# ── Grundform ────────────────────────────────────────────────────────────────────────────────────────────
class Quelle:
    """Gemeinsame Form der Quellen. Unterklassen setzen ``art`` und ``_lade``."""

    art = ""

    def __init__(self, konto: Konto, *, sitzung: Sitzung | None = None, uhr: Uhr | None = None) -> None:
        self.konto = konto
        self._uhr: Uhr = uhr or time.monotonic
        self._sitzung = sitzung or Sitzung(konto.url, konto.nutzer, konto.passwort)
        self._termine = Speicher(TERMINE_TTL_S, self._uhr)
        if konto.url.lower().startswith("http://") and konto.passwort:
            zeit.warne_einmal(
                "http:" + konto.name, "Konto %s: das Passwort geht über http unverschlüsselt an den Server", konto.name[:60])

    @property
    def host(self) -> str:
        return self._sitzung.host

    def kalender(self, neu_laden: bool = False) -> list[Kalender]:
        raise NotImplementedError

    def schreibbar(self) -> bool:
        return False

    def instanzen(self, kalender: Kalender, von: datetime, bis: datetime) -> list[ical.Instanz]:
        """Die Termine von ``kalender`` im Fenster ``[von, bis)`` (aufgelöst, nach Beginn sortiert)."""
        a, b = tagesfenster(von, bis)
        schluessel = (kalender.id, a, b)
        gespeichert = self._termine.hole(schluessel)
        if gespeichert is None:
            geladen = self._lade(kalender, a, b)
            geladen.sort(key=lambda i: (zeit.spanne(i.start, i.ende)[0].astimezone(timezone.utc), i.titel.casefold(), i.uid))
            gespeichert = geladen[:INSTANZEN_MAX]
            if len(geladen) > INSTANZEN_MAX:
                zeit.warne_einmal("viele:" + kalender.id, "Kalender %s liefert mehr als %d Termine im Fenster", kalender.name[:60], INSTANZEN_MAX)
            self._termine.setze(schluessel, gespeichert)
        treffer = []
        for i in gespeichert:
            anfang, ende = zeit.spanne(i.start, i.ende)
            if zeit.im_fenster(anfang, ende, von, bis):
                treffer.append(i)
        return treffer

    def _lade(self, kalender: Kalender, von: datetime, bis: datetime) -> list[ical.Instanz]:
        raise NotImplementedError

    def anlegen(self, kalender: Kalender, termin: dict[str, Any], jetzt: datetime | None = None) -> str:
        raise KontoFehler("anfrage", f"Das Konto {self.konto.name} ist schreibgeschützt (Abonnement)")


def _instanzen_aus(daten: list[str], von: datetime, bis: datetime) -> list[ical.Instanz]:
    """Die Termine aus iCalendar-Texten (ein Text je Ressource)."""
    aus: list[ical.Instanz] = []
    for text in daten:
        datei = ical.lies(text)
        aus += ical.instanzen(datei.ereignisse, von, bis)
    return aus


# ── CalDAV ───────────────────────────────────────────────────────────────────────────────────────────────
class CalDavQuelle(Quelle):
    art = "caldav"

    def __init__(self, konto: Konto, *, sitzung: Sitzung | None = None, uhr: Uhr | None = None) -> None:
        super().__init__(konto, sitzung=sitzung, uhr=uhr)
        self._liste: tuple[float, list[Kalender]] | None = None
        self._entdeckung = threading.Lock()  # das Finden der Kalender läuft nur einmal zugleich
        self._expand: bool | None = None  # None: noch nicht versucht; True: der Server löst Wiederholungen auf

    # ── Kalender finden ──────────────────────────────────────────────────────────────────────────────
    def kalender(self, neu_laden: bool = False) -> list[Kalender]:
        with self._entdeckung:
            if not neu_laden and self._liste is not None and self._uhr() - self._liste[0] < KALENDER_TTL_S:
                return list(self._liste[1])
            liste = self._entdecken()
            self._liste = (self._uhr(), liste)
            self._termine.leere()
            return list(liste)

    def schreibbar(self) -> bool:
        return True

    def _kandidaten(self) -> list[str]:
        teile = urllib.parse.urlsplit(self.konto.url)
        wurzel = f"{teile.scheme}://{teile.netloc}"
        kandidaten: list[str] = []
        if teile.path not in ("", "/") or teile.query:
            kandidaten.append(self.konto.url)
        kandidaten += [wurzel + "/.well-known/caldav", wurzel + "/"]
        return list(dict.fromkeys(kandidaten))

    def _entdecken(self) -> list[Kalender]:
        frist = self._uhr() + ENTDECKEN_S
        fehler: list[NetzFehler] = []
        for adresse in self._kandidaten():
            if self._uhr() > frist:
                raise KontoFehler("zeit", f"Zeitüberschreitung bei {self.host} (Kalender suchen)")
            try:
                gefunden = self._aus_adresse(adresse, frist)
            except NetzFehler as e:
                if e.art in ("auth", "zeit", "verbindung", "tls"):  # mit einem anderen Pfad wird es nicht besser
                    raise
                fehler.append(e)
                continue
            if gefunden:
                return gefunden
        if fehler:
            wichtig = [e for e in fehler if e.art != "nicht_gefunden"]
            raise (wichtig or fehler)[0]
        raise KontoFehler(
            "nicht_gefunden",
            f"Auf {self.host} wurde kein Kalender mit Terminen gefunden (ist die Adresse die des CalDAV-Servers?)",
        )

    def _propfind(self, adresse: str, koerper: bytes, tiefe: str, frist: float) -> Antwort:
        if self._uhr() > frist:
            raise KontoFehler("zeit", f"Zeitüberschreitung bei {self.host} (Kalender suchen)")
        return self._sitzung.anfrage("PROPFIND", adresse, koerper=koerper, kopf=caldav.kopf_xml(tiefe), erlaubt=(207,))

    def _aus_adresse(self, adresse: str, frist: float) -> list[Kalender]:
        antwort = self._propfind(adresse, caldav.ERKENNEN, "0", frist)
        eintraege = caldav.propfind_eintraege(antwort.koerper)
        if not eintraege:
            return []
        basis = antwort.url
        selbst = self._selbst(eintraege, basis)
        if selbst.nicht_angemeldet:
            raise KontoFehler("auth", "Anmeldung abgelehnt (App-Passwort prüfen)", 401)
        if selbst.kalender:
            return [self._kalender_aus(selbst, basis)] if selbst.nimmt_termine else []
        zuhause = [caldav.aufloesen(basis, h) for h in selbst.zuhause]
        if not zuhause and selbst.principal:
            principal = caldav.aufloesen(basis, selbst.principal)
            antwort_p = self._propfind(principal, caldav.ZUHAUSE, "0", frist)
            for e in caldav.propfind_eintraege(antwort_p.koerper):
                zuhause += [caldav.aufloesen(antwort_p.url, h) for h in e.zuhause]
        if not zuhause:
            zuhause = [basis]  # vielleicht ist die Adresse selbst der Ordner mit den Kalendern
        liste: list[Kalender] = []
        gesehen: set[str] = set()
        for ordner in list(dict.fromkeys(zuhause))[:5]:
            antwort_l = self._propfind(ordner, caldav.KALENDER_LISTE, "1", frist)
            for e in caldav.propfind_eintraege(antwort_l.koerper):
                if not e.kalender or not e.nimmt_termine:
                    continue
                k = self._kalender_aus(e, antwort_l.url)
                if k.url.rstrip("/") not in gesehen:
                    gesehen.add(k.url.rstrip("/"))
                    liste.append(k)
        if len(liste) > caldav.KALENDER_MAX:
            zeit.warne_einmal("viele-kalender:" + self.konto.name, "Konto %s hat mehr als %d Kalender", self.konto.name[:60], caldav.KALENDER_MAX)
        return liste[: caldav.KALENDER_MAX]

    @staticmethod
    def _selbst(eintraege: list[caldav.Eintrag], basis: str) -> caldav.Eintrag:
        """Der Eintrag zur angefragten Adresse selbst (bei Tiefe 0 der einzige, manche Server liefern trotzdem Kinder)."""
        ziel = urllib.parse.urlsplit(basis).path.rstrip("/")
        for e in eintraege:
            if urllib.parse.urlsplit(caldav.aufloesen(basis, e.href)).path.rstrip("/") == ziel:
                return e
        return eintraege[0]

    def _kalender_aus(self, e: caldav.Eintrag, basis: str) -> Kalender:
        url = caldav.aufloesen(basis, e.href)
        schreibbar = e.schreibbar
        kennung = hashlib.sha1(url.rstrip("/").encode("utf-8")).hexdigest()[:8]  # noqa: S324 — nur eine Kennung
        return Kalender(
            id=f"{self.konto.name}:{kennung}",
            name=e.name or caldav.name_aus_pfad(url) or self.konto.name,
            konto=self.konto.name,
            schreibbar=True if schreibbar is None else schreibbar,  # sagt der Server es nicht, gilt: vielleicht
            url=url,
        )

    # ── Termine holen ────────────────────────────────────────────────────────────────────────────────
    def _lade(self, kalender: Kalender, von: datetime, bis: datetime) -> list[ical.Instanz]:
        return _instanzen_aus(self._report(kalender, von, bis), von, bis)

    def _report(self, kalender: Kalender, von: datetime, bis: datetime) -> list[str]:
        """Die iCalendar-Texte der Termine im Fenster. Mit ``expand``, wenn der Server es kann; sonst ohne (das Plugin
        löst Wiederholungen dann selbst auf)."""
        versuche = [True, False] if self._expand is not False else [False]
        for nr, aufloesen in enumerate(versuche):
            gibt_ersatz = nr + 1 < len(versuche)  # nach einem Fehlschlag mit expand bleibt der Abruf ohne
            try:
                antwort = self._sitzung.anfrage(
                    "REPORT", kalender.url, koerper=caldav.report_koerper(von, bis, aufloesen), kopf=caldav.kopf_xml("1"),
                    erlaubt=(207,),
                )
            except NetzFehler as e:
                if gibt_ersatz and e.status in _OHNE_AUFLOESEN:
                    continue
                raise
            daten, fehler = caldav.report_daten(antwort.koerper)
            if fehler and not daten:
                if gibt_ersatz:
                    continue
                raise KontoFehler("server", f"{self.host} konnte die Termine nicht liefern (HTTP {fehler[0]})", fehler[0])
            if fehler:
                zeit.warne_einmal("teil:" + kalender.id, "Kalender %s: einige Termine liefert der Server nicht (HTTP %s)", kalender.name[:60], fehler[0])
            if aufloesen:
                self._expand = True
            elif len(versuche) == 2:
                self._expand = False  # expand ging nicht, ohne ging: dabei bleibt es
            return daten
        raise KontoFehler("server", f"{self.host} konnte die Termine nicht liefern")  # nicht erreichbar: die Schleife endet vorher

    # ── Anlegen ──────────────────────────────────────────────────────────────────────────────────────
    def anlegen(self, kalender: Kalender, termin: dict[str, Any], jetzt: datetime | None = None) -> str:
        """Legt den Termin mit ``PUT`` an und liefert seine UID. Ein bestehender Termin gleichen Namens wird nie
        überschrieben (``If-None-Match: *``); bei einer Kollision gibt es einmal eine neue UID."""
        if not kalender.url:
            raise KontoFehler("anfrage", "Dieser Kalender hat keine Adresse")
        stand = jetzt or datetime.now(timezone.utc)
        for versuch in (1, 2):
            kennung = uuid.uuid4()
            uid = f"{kennung}@fyos-kalender"
            ziel = kalender.url.rstrip("/") + f"/fyos-{kennung.hex}.ics"
            text = ical.baue_ics(uid, termin, stand)
            try:
                self._sitzung.anfrage(
                    "PUT", ziel, koerper=text.encode("utf-8"),
                    kopf={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"}, erlaubt=(200, 201, 204),
                )
            except NetzFehler as e:
                if e.status == 412 and versuch == 1:
                    continue
                raise self._schreibfehler(e) from None
            self._termine.leere()
            return uid
        raise KontoFehler("anfrage", f"{self.host} meldet, der Termin gebe es schon (HTTP 412)", 412)

    def _schreibfehler(self, e: NetzFehler) -> NetzFehler:
        if e.art == "verboten":
            return KontoFehler("verboten", f"Der Kalender nimmt keine Termine an (schreibgeschützt oder keine Berechtigung, {self.host})", e.status)
        if e.art == "anfrage" and e.status:
            return KontoFehler("anfrage", f"{self.host} lehnt den Termin ab (HTTP {e.status})", e.status)
        return e


# ── ICS-Abonnement ───────────────────────────────────────────────────────────────────────────────────────
def _dekodiere(antwort: Antwort) -> str:
    roh = antwort.koerper
    try:
        return roh.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    m = re.search(r"charset=\"?([\w.-]+)", antwort.kopf.get("content-type", ""), re.I)
    if m:
        try:
            return roh.decode(m[1])
        except (LookupError, UnicodeDecodeError):
            pass
    return roh.decode("latin-1")


class IcsQuelle(Quelle):
    art = "ics"

    def __init__(self, konto: Konto, *, sitzung: Sitzung | None = None, uhr: Uhr | None = None) -> None:
        super().__init__(konto, sitzung=sitzung, uhr=uhr)
        self._datei: tuple[float, ical.Datei] | None = None
        self._sperre = threading.Lock()

    def kalender(self, neu_laden: bool = False) -> list[Kalender]:
        # Das Abonnement ist ein einziger Kalender. Der Abruf prüft, dass die Adresse wirklich einen liefert (und wird
        # zehn Minuten gemerkt, die Termine brauchen ihn ohnehin): so zeigt `kalender.konten` ein totes Konto als tot.
        self._holen(erzwingen=neu_laden)
        return [self._der_kalender()]

    def _der_kalender(self) -> Kalender:
        return Kalender(id=self.konto.name, name=self.konto.name, konto=self.konto.name, schreibbar=False)

    def _holen(self, erzwingen: bool = False) -> ical.Datei:
        with self._sperre:
            if not erzwingen and self._datei is not None and self._uhr() - self._datei[0] < FEED_TTL_S:
                return self._datei[1]
            antwort = self._sitzung.anfrage(
                "GET", self.konto.url, kopf={"Accept": "text/calendar, text/plain;q=0.5, */*;q=0.1"}, erlaubt=(200,))
            text = _dekodiere(antwort)
            if "BEGIN:VCALENDAR" not in text[:4096].upper():
                raise KontoFehler("antwort", f"{self.host} liefert keinen Kalender (kein iCalendar); die Adresse prüfen")
            datei = ical.lies(text)
            self._datei = (self._uhr(), datei)
            self._termine.leere()
            return datei

    def _lade(self, kalender: Kalender, von: datetime, bis: datetime) -> list[ical.Instanz]:
        return ical.instanzen(self._holen().ereignisse, von, bis)


def quelle_fuer(konto: Konto, *, sitzung: Sitzung | None = None, uhr: Uhr | None = None) -> Quelle:
    """Die Quelle zu einem gültigen Konto."""
    klasse = IcsQuelle if konto.art == "ics" else CalDavQuelle
    return klasse(konto, sitzung=sitzung, uhr=uhr)
