"""iCalendar (RFC 5545) lesen, Wiederholungen auflösen und Termine zum Anlegen schreiben.

**Lesen.** ``lies(text)`` zerlegt eine Kalenderdatei in ``Ereignis``-Objekte (je ein ``VEVENT``). Zeiten bekommen ihren
Versatz aus ``Z``, ``TZID`` (siehe ``zeit.Zonen``) oder der Standardzone der Datei; ``VALUE=DATE`` bleibt ein Datum
(ganztägig). Alarme, Teilnehmer, Beschreibungen und alles, was die Terminliste nicht zeigt, werden gar nicht erst
gelesen.

**Wiederholungen.** ``instanzen(ereignisse, von, bis)`` liefert die einzelnen Termine im Fenster ``[von, bis)``:

* ``RRULE`` mit python-dateutil, in der Ortszeit des Termins (ein Termin „täglich 9 Uhr“ bleibt über die Zeitumstellung
  bei 9 Uhr). ``DTSTART`` ist immer die erste Instanz, ``RDATE`` fügt Instanzen hinzu, ``EXDATE`` nimmt welche heraus
  (``COUNT`` zählt auch die herausgenommenen, wie im Standard).
* ``RECURRENCE-ID`` ersetzt die Instanz, auf die sie zeigt (verschoben, umbenannt oder abgesagt); ein verschobener
  Termin erscheint dort, wo er jetzt liegt. ``RANGE=THISANDFUTURE`` wird wie ein einzelner Ausnahmetermin behandelt.
* Hat der Server die Wiederholungen schon aufgelöst (CalDAV ``expand``), kommen Einzeltermine mit ``RECURRENCE-ID``
  und ohne Regel; sie laufen durch denselben Weg.
* ``UNTIL`` wird passend zu ``DTSTART`` umgeschrieben (dateutil verlangt UTC bei Zeiten mit Zone) und nie hinter das
  Fenster gelegt. Feste Grenzen schützen vor Regeln, die nie enden oder nie zutreffen (Sekunden- und Minutentakt,
  Zahlen außerhalb ihres Bereichs und der 30. Februar werden abgelehnt: dann gilt nur der erste Termin).

**Schreiben.** ``baue_ics`` erzeugt für ``kalender.anlegen`` ein ``VEVENT`` in UTC (ohne ``VTIMEZONE``), mit
Maskierung und Zeilenfaltung nach dem Standard.
"""

from __future__ import annotations

import hashlib
import logging
import re
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterator

from dateutil import rrule as _rrule

from . import zeit
from .zeit import EIN_TAG, Zonen

_log = logging.getLogger("fyos_plugin_kalender")

#: So viele ``VEVENT`` liest das Plugin höchstens aus einer Datei
EREIGNISSE_MAX = 20000
#: So viele Schritte einer Wiederholungsregel laufen höchstens bis zum Fenster
SCHRITTE_MAX = 200_000
#: So viele Instanzen liefert ein Termin höchstens je Fenster
INSTANZEN_MAX = 2000
#: Längere Zeilen gibt es in Kalendern nicht; sie gelten als Müll
ZEILE_MAX = 20000
_HAEUFIGKEITEN = {"YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY"}
_ERLAUBT = {"UID", "SUMMARY", "LOCATION", "DTSTART", "DTEND", "DURATION", "RRULE", "EXDATE", "RDATE", "RECURRENCE-ID", "STATUS"}
_TZ_ERLAUBT = {"TZID", "DTSTART", "TZOFFSETFROM", "TZOFFSETTO", "TZNAME", "RRULE", "RDATE", "EXDATE"}


# ── Daten ───────────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Ereignis:
    """Ein ``VEVENT``, soweit die Terminliste es braucht."""

    uid: str
    titel: str
    ort: str
    start: date | datetime  # ein Datum: ganztägig
    ende: date | datetime | None
    dauer: timedelta | None  # aus DURATION, wenn DTEND fehlt
    regel: str = ""  # Wert von RRULE
    ausgenommen: list[date | datetime] = field(default_factory=list)  # EXDATE
    zusaetzlich: list[tuple[date | datetime, date | datetime | None]] = field(default_factory=list)  # RDATE
    ursprung: date | datetime | None = None  # RECURRENCE-ID
    abgesagt: bool = False

    @property
    def ganztaegig(self) -> bool:
        return not isinstance(self.start, datetime)


@dataclass(frozen=True)
class Instanz:
    """Ein einzelner Termin: ein Einzeltermin oder eine Instanz einer Wiederholung."""

    uid: str
    kennung: str  # "" bei Einzelterminen, sonst die Kennung der Instanz (Beginn in UTC bzw. Tag)
    titel: str
    ort: str
    start: date | datetime
    ende: date | datetime
    ganztaegig: bool


@dataclass
class Datei:
    ereignisse: list[Ereignis] = field(default_factory=list)
    uebersprungen: int = 0  # Ereignisse, die nicht lesbar waren
    name: str = ""  # X-WR-CALNAME


# ── Zeilen ──────────────────────────────────────────────────────────────────────────────────────────────
_ENTFALTEN = re.compile(r"\r?\n[ \t]")
_ZEILENENDE = re.compile(r"\r\n|\n|\r")
_MASKE = re.compile(r"\\(.)", re.S)


def zeilen(text: str) -> list[str]:
    """Die logischen Zeilen: gefaltete Zeilen zusammengesetzt, Leerzeilen weg."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return [z for z in _ZEILENENDE.split(_ENTFALTEN.sub("", text)) if z.strip() and len(z) <= ZEILE_MAX]


def zerlege(zeile: str) -> tuple[str, dict[str, str], str] | None:
    """``NAME;PARAM=WERT:WERT`` als (NAME, {PARAM: WERT}, WERT); Anführungszeichen schützen ``:`` und ``;``.
    ``None`` bei Zeilen ohne Doppelpunkt."""
    angefuehrt = False
    trenner = -1
    for i, c in enumerate(zeile):
        if c == '"':
            angefuehrt = not angefuehrt
        elif c == ":" and not angefuehrt:
            trenner = i
            break
    if trenner < 0:
        return None
    kopf, wert = zeile[:trenner], zeile[trenner + 1:]
    teile: list[str] = []
    aktuell: list[str] = []
    angefuehrt = False
    for c in kopf:
        if c == '"':
            angefuehrt = not angefuehrt
            aktuell.append(c)
        elif c == ";" and not angefuehrt:
            teile.append("".join(aktuell))
            aktuell = []
        else:
            aktuell.append(c)
    teile.append("".join(aktuell))
    name = teile[0].strip().upper()
    if "." in name:  # Gruppenvorsatz („item1.DTSTART“)
        name = name.rsplit(".", 1)[1]
    params: dict[str, str] = {}
    for p in teile[1:]:
        schluessel, _, w = p.partition("=")
        if schluessel.strip():
            params[schluessel.strip().upper()] = w.strip().strip('"')
    return name, params, wert


def text_aus(wert: str) -> str:
    """Einen iCalendar-Text entmaskieren (``\\n``, ``\\,``, ``\\;``, ``\\\\``) und auf eine Zeile bringen."""
    entmaskiert = _MASKE.sub(lambda m: "\n" if m[1] in "nN" else m[1], wert)
    return " ".join(entmaskiert.split())


# ── Lesen ───────────────────────────────────────────────────────────────────────────────────────────────
def _vtimezone_bereinigen(zeilen_: list[str]) -> str:
    """Nur die Teile einer ``VTIMEZONE`` behalten, die dateutil versteht (es lehnt alles Unbekannte ab, auch das
    ``X-LIC-LOCATION`` von libical), und die Parameter weglassen."""
    aus: list[str] = []
    tiefe = 0  # 0: VTIMEZONE selbst, 1: STANDARD/DAYLIGHT
    ueberspringen = 0
    for zeile in zeilen_:
        z = zerlege(zeile)
        if z is None:
            continue
        name, _, wert = z
        art = wert.strip().upper()
        if ueberspringen:
            if name == "BEGIN":
                ueberspringen += 1
            elif name == "END":
                ueberspringen -= 1
            continue
        if name == "BEGIN":
            if art == "VTIMEZONE" and tiefe == 0 and not aus:
                aus.append("BEGIN:VTIMEZONE")
            elif art in ("STANDARD", "DAYLIGHT") and aus and tiefe == 0:
                tiefe = 1
                aus.append(f"BEGIN:{art}")
            else:
                ueberspringen = 1
        elif name == "END":
            if art in ("STANDARD", "DAYLIGHT") and tiefe:
                tiefe = 0
                aus.append(f"END:{art}")
            elif art == "VTIMEZONE":
                aus.append("END:VTIMEZONE")
        elif name in _TZ_ERLAUBT and aus:
            if (tiefe == 0) != (name == "TZID"):
                continue
            aus.append(f"{name}:{wert.strip()}")
    return "\r\n".join(aus)


def lies(text: str) -> Datei:
    """Eine Kalenderdatei lesen. Unlesbare Ereignisse werden gezählt (``uebersprungen``), nicht gemeldet."""
    datei = Datei()
    roh_ereignisse: list[list[tuple[str, dict[str, str], str]]] = []
    zonen_bloecke: list[str] = []
    zonen_zeilen: list[str] = []
    standard_name = ""
    modus = ""  # "" | VEVENT | VTIMEZONE | ANDERES
    tiefe = 0
    aktuell: list[tuple[str, dict[str, str], str]] = []
    for zeile in zeilen(text):
        z = zerlege(zeile)
        if z is None:
            continue
        name, params, wert = z
        if modus == "":
            if name == "BEGIN":
                art = wert.strip().upper()
                if art == "VEVENT":
                    modus, tiefe, aktuell = "VEVENT", 0, []
                elif art == "VTIMEZONE":
                    modus, tiefe, zonen_zeilen = "VTIMEZONE", 0, [zeile]
                elif art != "VCALENDAR":
                    modus, tiefe = "ANDERES", 0
            elif name == "X-WR-TIMEZONE":
                standard_name = wert.strip()
            elif name == "X-WR-CALNAME" and not datei.name:
                datei.name = text_aus(wert)[:200]
            continue
        if name == "BEGIN":
            tiefe += 1
            if modus == "VTIMEZONE":
                zonen_zeilen.append(zeile)
            continue
        if name == "END":
            if tiefe > 0:
                tiefe -= 1
                if modus == "VTIMEZONE":
                    zonen_zeilen.append(zeile)
                continue
            if modus == "VEVENT":
                if len(roh_ereignisse) < EREIGNISSE_MAX:
                    roh_ereignisse.append(aktuell)
                else:
                    datei.uebersprungen += 1
            elif modus == "VTIMEZONE":
                zonen_zeilen.append(zeile)
                if len(zonen_bloecke) < 50:
                    zonen_bloecke.append(_vtimezone_bereinigen(zonen_zeilen))
            modus = ""
            continue
        if modus == "VEVENT" and tiefe == 0 and name in _ERLAUBT:
            aktuell.append((name, params, wert))
        elif modus == "VTIMEZONE":
            zonen_zeilen.append(zeile)
    standard = zeit.iana(standard_name) if standard_name else None
    zonen = Zonen("\r\n".join(b for b in zonen_bloecke if b), standard)
    for eigenschaften in roh_ereignisse:
        try:
            ereignis = _ereignis(eigenschaften, zonen)
        except ValueError as fehler:
            datei.uebersprungen += 1
            zeit.warne_einmal("ereignis:" + str(fehler)[:40], "Ein Termin im Kalender ist nicht lesbar (%s)", str(fehler)[:80])
            continue
        if ereignis is not None:
            datei.ereignisse.append(ereignis)
        else:
            datei.uebersprungen += 1
    return datei


def _zeiten_liste(params: dict[str, str], wert: str, zonen: Zonen) -> list[date | datetime]:
    return [zeit.lies_zeit(t, params, zonen) for t in wert.split(",") if t.strip()]


def _perioden(params: dict[str, str], wert: str, zonen: Zonen) -> list[tuple[date | datetime, date | datetime | None]]:
    """``RDATE`` als (Beginn, Ende oder None); ``VALUE=PERIOD`` nennt Beginn und Ende oder Dauer."""
    aus: list[tuple[date | datetime, date | datetime | None]] = []
    zeitpunkt = {**params, "VALUE": "DATE-TIME"}
    for teil in wert.split(","):
        if not teil.strip():
            continue
        if (params.get("VALUE") or "").upper() == "PERIOD" or "/" in teil:
            anfang_text, _, rest = teil.partition("/")
            anfang = zeit.lies_zeit(anfang_text, zeitpunkt, zonen)
            ende: date | datetime | None = None
            if rest.strip().upper().lstrip("+-").startswith("P"):
                if isinstance(anfang, datetime):
                    ende = anfang + zeit.lies_dauer(rest)
            elif rest.strip():
                ende = zeit.lies_zeit(rest, zeitpunkt, zonen)
            aus.append((anfang, ende))
        else:
            aus.append((zeit.lies_zeit(teil, params, zonen), None))
    return aus


def _ereignis(eigenschaften: list[tuple[str, dict[str, str], str]], zonen: Zonen) -> Ereignis | None:
    erste: dict[str, tuple[dict[str, str], str]] = {}
    ausgenommen: list[date | datetime] = []
    zusaetzlich: list[tuple[date | datetime, date | datetime | None]] = []
    for name, params, wert in eigenschaften:
        if name == "EXDATE":
            ausgenommen += _zeiten_liste(params, wert, zonen)
        elif name == "RDATE":
            zusaetzlich += _perioden(params, wert, zonen)
        elif name not in erste:
            erste[name] = (params, wert)
    if "DTSTART" not in erste:
        return None
    start = zeit.lies_zeit(erste["DTSTART"][1], erste["DTSTART"][0], zonen)
    ende: date | datetime | None = None
    dauer: timedelta | None = None
    if "DTEND" in erste:
        ende = zeit.lies_zeit(erste["DTEND"][1], erste["DTEND"][0], zonen)
        # Ein Ende der anderen Art (Tag zu Zeitpunkt oder umgekehrt) wird auf die Art des Beginns gebracht.
        if isinstance(start, datetime) and not isinstance(ende, datetime):
            ende = zeit.tagesanfang(ende)
        elif not isinstance(start, datetime) and isinstance(ende, datetime):
            ende = zeit.in_hub_zone(ende).date()
    elif "DURATION" in erste:
        try:
            dauer = zeit.lies_dauer(erste["DURATION"][1])
        except ValueError:
            dauer = None
    titel = text_aus(erste["SUMMARY"][1]) if "SUMMARY" in erste else ""
    ort = text_aus(erste["LOCATION"][1]) if "LOCATION" in erste else ""
    uid = erste["UID"][1].strip() if "UID" in erste else ""
    if not uid:
        uid = "x-" + hashlib.sha1(f"{titel}|{start}|{ort}".encode()).hexdigest()[:16]  # noqa: S324 — nur eine Kennung
    ursprung = None
    if "RECURRENCE-ID" in erste:
        try:
            ursprung = zeit.lies_zeit(erste["RECURRENCE-ID"][1], erste["RECURRENCE-ID"][0], zonen)
        except ValueError:
            ursprung = None
    status = erste["STATUS"][1].strip().upper() if "STATUS" in erste else ""
    return Ereignis(
        uid=uid[:500],
        titel=titel,
        ort=ort,
        start=start,
        ende=ende,
        dauer=dauer,
        regel=erste["RRULE"][1].strip() if "RRULE" in erste else "",
        ausgenommen=ausgenommen,
        zusaetzlich=zusaetzlich,
        ursprung=ursprung,
        abgesagt=status == "CANCELLED",
    )


# ── Wiederholungen ──────────────────────────────────────────────────────────────────────────────────────
def _dauer(e: Ereignis) -> timedelta:
    """Länge des Termins: ganztägig mindestens ein Tag (in ganzen Tagen), sonst von Beginn bis Ende (nie negativ)."""
    if e.ganztaegig:
        if e.ende is not None and not isinstance(e.ende, datetime) and e.ende > e.start:
            return e.ende - e.start  # type: ignore[operator]
        if e.dauer is not None and e.dauer > timedelta(0):
            return timedelta(days=max(1, -(-int(e.dauer.total_seconds()) // 86400)))
        return EIN_TAG
    if isinstance(e.ende, datetime):
        return max(e.ende - e.start, timedelta(0))  # type: ignore[operator]
    if e.dauer is not None:
        return max(e.dauer, timedelta(0))
    return timedelta(0)


def _plus(beginn: date | datetime, dauer: timedelta) -> date | datetime:
    """Beginn plus Dauer: ein Zeitpunkt in der Zeit der Uhr (nicht der Wanduhr), ein Tag in ganzen Tagen."""
    if isinstance(beginn, datetime):
        return (beginn.astimezone(timezone.utc) + dauer).astimezone(beginn.tzinfo)
    return beginn + dauer


def _kennung(wert: date | datetime) -> str:
    return zeit.utc_text(wert) if isinstance(wert, datetime) else wert.strftime("%Y%m%d")


def _schluessel(wert: date | datetime, ganztaegig: bool) -> tuple[str, Any]:
    """Wie ein Termin eine Instanz wiedererkennt: ein ganztägiger am Tag, einer mit Uhrzeit am Zeitpunkt (ein Tag als
    Hinweis trifft alle Instanzen dieses Tages)."""
    if isinstance(wert, datetime):
        if ganztaegig:
            return "d", zeit.in_hub_zone(wert).date()
        return "t", wert.astimezone(timezone.utc)
    return "d", wert


#: Wertebereiche der BY-Teile (Beträge; ``negativ`` heißt: von hinten zählen ist erlaubt)
_BY_GRENZEN = {
    "BYMONTH": (1, 12, False), "BYMONTHDAY": (1, 31, True), "BYYEARDAY": (1, 366, True), "BYWEEKNO": (1, 53, True),
    "BYHOUR": (0, 23, False), "BYMINUTE": (0, 59, False), "BYSECOND": (0, 60, False), "BYSETPOS": (1, 366, True),
}
_MONATSTAGE = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
#: So viele Stunden darf eine stündliche Regel von ``DTSTART`` bis zum Fenster zählen (dateutil läuft Schritt für Schritt)
STUNDEN_SCHRITTE_MAX = 150_000


def _regel_pruefen(teile: dict[str, str], haeufigkeit: str) -> None:
    """Weist Regeln ab, die dateutil nie zu Ende rechnet: Zahlen außerhalb ihres Bereichs und Tage, die es in den
    gewählten Monaten nie gibt (30. Februar). ``ValueError`` mit dem Grund."""
    for name, (klein, gross, negativ) in _BY_GRENZEN.items():
        if name not in teile:
            continue
        for t in teile[name].split(","):
            try:
                n = int(t)
            except ValueError:
                raise ValueError(f"{name} nicht lesbar") from None
            if not klein <= abs(n) <= gross or (n < 0 and not negativ):
                raise ValueError(f"{name}={n} liegt außerhalb des erlaubten Bereichs")
    if ("BYWEEKNO" in teile or "BYYEARDAY" in teile) and haeufigkeit != "YEARLY":
        raise ValueError("BYWEEKNO und BYYEARDAY gibt es nur bei FREQ=YEARLY")
    if "BYEASTER" in teile:
        raise ValueError("BYEASTER ist kein Teil des Standards")
    if "BYMONTHDAY" in teile:
        monate = [int(m) for m in teile["BYMONTH"].split(",")] if "BYMONTH" in teile else list(_MONATSTAGE)
        laengster = max(_MONATSTAGE[m] for m in monate)
        if all(abs(int(t)) > laengster for t in teile["BYMONTHDAY"].split(",")):
            raise ValueError("BYMONTHDAY passt zu keinem der gewählten Monate")
    for name, grenze in (("INTERVAL", 1000), ("COUNT", 1_000_000)):
        if name in teile:
            try:
                n = int(teile[name])
            except ValueError:
                raise ValueError(f"{name} nicht lesbar") from None
            if not 1 <= n <= grenze:
                raise ValueError(f"{name}={n} liegt außerhalb des erlaubten Bereichs")


def _regel_normieren(e: Ereignis, bis: datetime) -> str:
    """Die Regel für dateutil: unbekannte Teile weg, ``UNTIL`` passend zu ``DTSTART`` und nicht hinter dem Fenster
    (dateutil hört dann am ersten Treffer dahinter auf). ``ValueError``, wenn sie nicht unterstützt wird."""
    teile: dict[str, str] = {}
    for t in e.regel.split(";"):
        schluessel, _, w = t.partition("=")
        schluessel = schluessel.strip().upper()
        if not schluessel or not w.strip() or schluessel == "SKIP" or schluessel.startswith("X-"):
            continue
        if schluessel == "RSCALE" and w.strip().upper() == "GREGORIAN":
            continue
        teile[schluessel] = w.strip()
    haeufigkeit = teile.get("FREQ", "").upper()
    if haeufigkeit not in _HAEUFIGKEITEN:
        raise ValueError(f"Wiederholung {haeufigkeit or 'ohne FREQ'} wird nicht unterstützt")
    _regel_pruefen(teile, haeufigkeit)
    if haeufigkeit == "HOURLY" and isinstance(e.start, datetime):
        schritte = (bis - e.start) / timedelta(hours=int(teile.get("INTERVAL", "1")))
        if schritte > STUNDEN_SCHRITTE_MAX:
            raise ValueError("stündliche Wiederholung seit zu langer Zeit")
    teile["UNTIL"] = _bis_begrenzen(_bis_normieren(teile["UNTIL"], e) if "UNTIL" in teile else "", e, bis)
    return ";".join(f"{k}={v}" for k, v in teile.items())


_UNTIL_TAG = re.compile(r"(\d{4})(\d{2})(\d{2})")
_UNTIL_ZEIT = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z?)")


def _bis_normieren(wert: str, e: Ereignis) -> str:
    """``UNTIL`` so schreiben, wie dateutil es zu ``DTSTART`` verlangt: bei einem Termin mit Zeitzone in UTC, bei einem
    ganztägigen als Tag."""
    text = wert.strip()
    tag = _UNTIL_TAG.fullmatch(text)
    zp = _UNTIL_ZEIT.fullmatch(text)
    if tag is None and zp is None:
        raise ValueError(f"UNTIL nicht lesbar: {text[:30]!r}")
    if isinstance(e.start, datetime):
        zone = e.start.tzinfo
        if tag is not None:  # nur ein Tag: bis zum Ende dieses Tages in der Zeitzone des Termins
            bis = datetime(int(tag[1]), int(tag[2]), int(tag[3]), 23, 59, 59, tzinfo=zone)
        else:
            bis = datetime(
                int(zp[1]), int(zp[2]), int(zp[3]), int(zp[4]), int(zp[5]), min(int(zp[6] or 0), 59),
                tzinfo=timezone.utc if zp[7] else zone,
            )
        return bis.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if tag is not None:
        return text
    bis_zeit = datetime(int(zp[1]), int(zp[2]), int(zp[3]), int(zp[4]), int(zp[5]), min(int(zp[6] or 0), 59))
    if zp[7]:
        bis_zeit = bis_zeit.replace(tzinfo=timezone.utc).astimezone(zeit.hub_zone())
    return bis_zeit.strftime("%Y%m%d")


def _bis_begrenzen(text: str, e: Ereignis, ende: datetime) -> str:
    """``UNTIL`` (schon passend zu ``DTSTART`` geschrieben, oder leer) höchstens bis zum Ende des Fensters."""
    if isinstance(e.start, datetime):
        grenze = ende.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    else:
        grenze = (zeit.in_hub_zone(ende).date() + EIN_TAG).strftime("%Y%m%d")  # ein Tag Luft
    return min(text, grenze) if text else grenze


def _beginne(e: Ereignis, bis: datetime) -> Iterator[date | datetime]:
    """Die Beginne aller Instanzen von ``e`` (aufsteigend) bis zum Ende des Fensters: Tage bei ganztägigen Terminen,
    sonst Zeitpunkte mit Versatz. Ein Termin ohne Wiederholung liefert nur seinen Beginn. Eine Regel, die das Plugin
    nicht unterstützt, fällt weg: dann gelten nur der erste Termin und die ``RDATE`` (mit Eintrag im Log)."""
    if not e.regel and not e.zusaetzlich:
        yield e.start
        return
    ganztaegig = e.ganztaegig
    anfang = datetime.combine(e.start, time.min) if ganztaegig else e.start  # type: ignore[arg-type]
    satz = _rrule.rruleset()
    if e.regel:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)  # COUNT und UNTIL zugleich: dateutil hält beide ein
                satz.rrule(_rrule.rrulestr(_regel_normieren(e, bis), dtstart=anfang))
        except (ValueError, OverflowError, TypeError, KeyError) as fehler:
            zeit.warne_einmal(
                "regel:" + e.uid[:40], "Wiederholungsregel wird nicht unterstützt (%s): %s; es gilt nur der erste Termin",
                e.uid[:40], str(fehler)[:100])
    satz.rdate(anfang)
    for beginn, _ in e.zusaetzlich:
        if ganztaegig:
            satz.rdate(datetime.combine(beginn.date() if isinstance(beginn, datetime) else beginn, time.min))
        elif isinstance(beginn, datetime):
            satz.rdate(beginn)
        else:
            satz.rdate(zeit.tagesanfang(beginn, anfang.tzinfo))
    grenze_tag = date.max
    if ganztaegig:
        lokal = zeit.in_hub_zone(bis)
        grenze_tag = lokal.date() + (timedelta(0) if lokal.time() == time.min else EIN_TAG)
    schritte = 0
    for s in satz:
        schritte += 1
        if schritte > SCHRITTE_MAX:
            zeit.warne_einmal("schritte:" + e.uid[:40], "Eine Wiederholungsregel ist zu lang zum Abzählen (%s)", e.uid[:40])
            return
        if ganztaegig:
            if s.date() >= grenze_tag:
                return
            yield s.date()
        else:
            if s >= bis:
                return
            yield s


def _ausnahmen(e: Ereignis) -> tuple[set[datetime], set[date]]:
    """``EXDATE`` als Zeitpunkte (UTC) und als Tage."""
    zeitpunkte: set[datetime] = set()
    tage: set[date] = set()
    for x in e.ausgenommen:
        if isinstance(x, datetime):
            if e.ganztaegig:
                tage.add(zeit.in_hub_zone(x).date())
            else:
                zeitpunkte.add(x.astimezone(timezone.utc))
        else:
            tage.add(x)
    return zeitpunkte, tage


def _instanz(e: Ereignis, start: date | datetime, kennung: str, dauer: timedelta) -> Instanz:
    return Instanz(e.uid, kennung, e.titel, e.ort, start, _plus(start, dauer), e.ganztaegig)


def _aus_meister(
    m: Ereignis, ausnahmen: list[Ereignis], von: datetime, bis: datetime, unterscheiden: bool = False
) -> list[Instanz]:
    """Die Instanzen eines Meisters im Fenster, ohne die herausgenommenen und die durch Ausnahmen ersetzten.
    ``unterscheiden``: dieselbe Kennung gehört mehreren Terminen (ein Server hat Wiederholungen ohne ``RECURRENCE-ID``
    aufgelöst); dann trägt jeder seinen Beginn in der Kennung."""
    dauer = _dauer(m)
    ex_zeit, ex_tage = _ausnahmen(m)
    ersetzt = {_schluessel(o.ursprung, m.ganztaegig) for o in ausnahmen if o.ursprung is not None}
    wiederholt = bool(m.regel or m.zusaetzlich) or unterscheiden
    zusatz_dauer: dict[datetime, timedelta] = {}
    for beginn, ende in m.zusaetzlich:
        if isinstance(beginn, datetime) and isinstance(ende, datetime):
            zusatz_dauer[beginn.astimezone(timezone.utc)] = max(ende - beginn, timedelta(0))
    aus: list[Instanz] = []
    try:
        for s in _beginne(m, bis):
            if len(aus) >= INSTANZEN_MAX:
                zeit.warne_einmal("instanzen:" + m.uid[:40], "Ein Termin hat im Fenster sehr viele Wiederholungen (%s)", m.uid[:40])
                break
            if isinstance(s, datetime):
                utc = s.astimezone(timezone.utc)
                if utc in ex_zeit or s.date() in ex_tage:
                    continue
                if ("t", utc) in ersetzt or ("d", s.date()) in ersetzt:
                    continue
                d = zusatz_dauer.get(utc, dauer)
            else:
                if s in ex_tage or ("d", s) in ersetzt:
                    continue
                d = dauer
            a, b = zeit.spanne(s, _plus(s, d))
            if zeit.im_fenster(a, b, von, bis):
                aus.append(_instanz(m, s, _kennung(s) if wiederholt else "", d))
    except (ValueError, OverflowError, TypeError) as fehler:
        zeit.warne_einmal("regel:" + m.uid[:40], "Wiederholung eines Termins nicht auflösbar (%s): %s", m.uid[:40], str(fehler)[:100])
    return aus


def instanzen(ereignisse: list[Ereignis], von: datetime, bis: datetime) -> list[Instanz]:
    """Alle Termine im Fenster ``[von, bis)``: Einzeltermine, aufgelöste Wiederholungen und Ausnahmen. Abgesagte
    fehlen. Eine Wiederholungsregel, die das Plugin nicht unterstützt, fällt weg (Eintrag im Log): es gelten dann nur
    der erste Termin und die ``RDATE``."""
    gruppen: dict[str, list[Ereignis]] = {}
    for e in ereignisse:
        gruppen.setdefault(e.uid, []).append(e)
    aus: list[Instanz] = []
    for gruppe in gruppen.values():
        ausnahmen = [e for e in gruppe if e.ursprung is not None]
        meister = [e for e in gruppe if e.ursprung is None]
        for m in (e for e in meister if not e.abgesagt):
            aus += _aus_meister(m, ausnahmen, von, bis, unterscheiden=len(meister) > 1)
        for o in ausnahmen:
            if o.abgesagt:
                continue
            d = _dauer(o)
            a, b = zeit.spanne(o.start, _plus(o.start, d))
            if zeit.im_fenster(a, b, von, bis):
                aus.append(_instanz(o, o.start, _kennung(o.ursprung), d))  # type: ignore[arg-type]
    return aus


# ── Schreiben ───────────────────────────────────────────────────────────────────────────────────────────
_STEUERZEICHEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def maskiere(text: str) -> str:
    """Einen Text für einen iCalendar-Wert maskieren (Umbruch, Komma, Semikolon, Rückstrich)."""
    sauber = _STEUERZEICHEN.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return sauber.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def falte(zeile: str) -> str:
    """Eine Zeile nach 75 Oktetten brechen (Fortsetzung mit Leerzeichen), ohne ein UTF-8-Zeichen zu teilen."""
    roh = zeile.encode("utf-8")
    if len(roh) <= 75:
        return zeile
    teile: list[str] = []
    start = 0
    breite = 75
    while start < len(roh):
        ende = min(start + breite, len(roh))
        while ende < len(roh) and (roh[ende] & 0xC0) == 0x80:
            ende -= 1
        teile.append(roh[start:ende].decode("utf-8"))
        start = ende
        breite = 74
    return "\r\n ".join(teile)


def baue_ics(uid: str, termin: dict[str, Any], jetzt: datetime) -> str:
    """Ein ``VCALENDAR`` mit einem ``VEVENT`` für ``kalender.anlegen``. ``termin`` ist geprüft (siehe
    ``plugin.neu_pruefen``): ``titel``, ``start``, ``ende`` (Tage oder Zeitpunkte mit Versatz), ``ganztaegig``, ``ort``,
    ``beschreibung``. Zeiten gehen als UTC hinaus, ganztägige Termine als Datum."""
    zeilen_ = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FYOS//fyos-plugin-kalender//DE",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{maskiere(uid)}",
        f"DTSTAMP:{zeit.utc_text(jetzt)}",
    ]
    if termin["ganztaegig"]:
        zeilen_ += [f"DTSTART;VALUE=DATE:{termin['start'].strftime('%Y%m%d')}",
                    f"DTEND;VALUE=DATE:{termin['ende'].strftime('%Y%m%d')}"]
    else:
        zeilen_ += [f"DTSTART:{zeit.utc_text(termin['start'])}", f"DTEND:{zeit.utc_text(termin['ende'])}"]
    zeilen_.append(f"SUMMARY:{maskiere(termin['titel'])}")
    if termin.get("ort"):
        zeilen_.append(f"LOCATION:{maskiere(termin['ort'])}")
    if termin.get("beschreibung"):
        zeilen_.append(f"DESCRIPTION:{maskiere(termin['beschreibung'])}")
    zeilen_ += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(falte(z) for z in zeilen_) + "\r\n"
