"""Zeit im Plugin: Zeitzone des Hubs, Zeitzonen aus iCalendar-Dateien, Zeitangaben lesen, Fenster.

**Zeitzone des Hubs.** ``FYOS_ZEITZONE`` (IANA-Name wie ``Europe/Berlin`` oder ``UTC``) wie im Kern; ohne Angabe die
Zeitzone des Rechners. Alle Zeitpunkte, die das Plugin ausgibt, tragen deren Versatz. Ganztägige Termine gelten dort als
Kalendertage, Zeiten ohne Versatz („schwebend“) ebenso.

**Zeitzonen in Kalenderdateien.** ``TZID`` nennt meist einen IANA-Namen, bei Outlook und Exchange einen Windows-Namen,
manchmal einen eigenen Namen, der nur in der eingebetteten ``VTIMEZONE`` erklärt wird. Die Reihenfolge der Suche:
IANA (auch mit dem Vorsatz älterer Programme wie ``/softwarestudio.org/Olson_20011030_5/Europe/Berlin``), Windows-Name,
eingebettete ``VTIMEZONE``; sonst die Standardzone der Datei (``X-WR-TIMEZONE``) oder die des Hubs, einmal im Log
vermerkt.
"""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import tz as _dtz

from .zonen import ALIASE, WINDOWS_ZONEN_KLEIN

_log = logging.getLogger("fyos_plugin_kalender")
_gewarnt: set[str] = set()

#: Zeigt die Datei einen Tag nach dem letzten Tag an (iCalendar, Google): so weit liegt „ende“ bei ganztägigen Terminen.
EIN_TAG = timedelta(days=1)


def warne_einmal(schluessel: str, text: str, *args: object) -> None:
    """Eine Warnung höchstens einmal je Schlüssel; ein kaputter Kalender füllt das Log sonst bei jedem Abruf."""
    if schluessel in _gewarnt or len(_gewarnt) > 500:
        return
    _gewarnt.add(schluessel)
    _log.warning(text, *args)


# ── Zeitzone des Hubs ──────────────────────────────────────────────────────────────────────────────────
def hub_zone() -> tzinfo:
    """Die Zeitzone des Hubs: ``FYOS_ZEITZONE``, sonst die des Rechners (folgt deren Sommerzeit)."""
    name = (os.getenv("FYOS_ZEITZONE") or "").strip()
    if name:
        if name.upper() in ("UTC", "Z"):
            return timezone.utc
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — unbekannter Name oder keine Zeitzonen-Datenbank auf dem Rechner
            warne_einmal("hub:" + name, "FYOS_ZEITZONE=%s ist unbekannt; es gilt die Zeitzone des Rechners", name[:60])
    return _dtz.tzlocal()


def lokalisiere(zeit: datetime, zone: tzinfo | None = None) -> datetime:
    """Die Zeit mit Versatz; eine Zeit ohne Versatz gilt in ``zone`` (Vorgabe: Zeitzone des Hubs)."""
    if zeit.tzinfo is not None and zeit.utcoffset() is not None:
        return zeit
    return zeit.replace(tzinfo=zone if zone is not None else hub_zone())


def in_hub_zone(zeit: datetime) -> datetime:
    """Eine Zeit mit Versatz (oder ohne: dann gilt sie als Zeit des Hubs) in der Zeitzone des Hubs."""
    return lokalisiere(zeit).astimezone(hub_zone())


def tagesanfang(tag: date, zone: tzinfo | None = None) -> datetime:
    """Mitternacht des Tages in der Zeitzone des Hubs."""
    return lokalisiere(datetime.combine(tag, time.min), zone)


def heute(jetzt: datetime | None = None) -> date:
    """Das heutige Datum in der Zeitzone des Hubs."""
    return in_hub_zone(jetzt or datetime.now(timezone.utc)).date()


def iso(zeit: datetime) -> str:
    """Zeitstempel ISO 8601 in der Zeitzone des Hubs, ohne Bruchteile."""
    return in_hub_zone(zeit).replace(microsecond=0).isoformat()


def kurz(zeit: datetime | date) -> str:
    """``2026-10-12 09:00`` (Zeitpunkt in der Zeitzone des Hubs) oder ``2026-10-12`` (Tag)."""
    if isinstance(zeit, datetime):
        return in_hub_zone(zeit).strftime("%Y-%m-%d %H:%M")
    return zeit.isoformat()


def utc_text(zeit: datetime) -> str:
    """``20261012T070000Z`` für iCalendar und CalDAV."""
    return lokalisiere(zeit).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── Zeitzonen finden ────────────────────────────────────────────────────────────────────────────────────
_ZONENNAME = re.compile(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,3}")


@lru_cache(maxsize=512)
def _iana_roh(name: str) -> tzinfo | None:
    if not _ZONENNAME.fullmatch(name):
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unbekannter Schlüssel, keine Datenbank, kaputte Datei
        pass
    if "/" in name:  # dateutil bringt eine eigene Datenbank mit; ohne „/“ liefert gettz auch POSIX-Texte wie GMT+1
        zone = _dtz.gettz(name)
        if zone is not None:
            return zone
    return None


def _kandidaten(name: str) -> list[str]:
    text = name.strip().strip('"').strip()
    if not text:
        return []
    if text.startswith("/"):  # /softwarestudio.org/Olson_20011030_5/Europe/Berlin, /mozilla.org/20050126_1/Europe/Berlin
        teile = [t for t in text.split("/") if t]
        return ["/".join(teile[-3:]), "/".join(teile[-2:]), teile[-1]] if teile else []
    return [text]


def iana(name: str) -> tzinfo | None:
    """Die Zone zu einem IANA-Namen (auch mit dem Vorsatz älterer Programme); ``None``, wenn es sie nicht gibt."""
    for kandidat in _kandidaten(name):
        zone = _iana_roh(kandidat)
        if zone is None and kandidat in ALIASE:
            zone = _iana_roh(ALIASE[kandidat])
        if zone is not None:
            return zone
    return None


class Zonen:
    """Die Zeitzonen einer Kalenderdatei: eingebettete ``VTIMEZONE``-Blöcke und die Standardzone der Datei."""

    def __init__(self, vtimezone: str = "", standard: tzinfo | None = None) -> None:
        self._vtimezone = vtimezone
        #: Zone für Zeiten ohne Versatz und ohne TZID (``X-WR-TIMEZONE``); ``None``: Zeit des Hubs
        self.standard = standard
        self._gemerkt: dict[str, tzinfo | None] = {}
        self._ical: Any = False  # False: noch nicht gelesen, None: nicht lesbar

    def _eingebettet(self, tzid: str) -> tzinfo | None:
        if self._ical is False:
            self._ical = None
            if self._vtimezone.strip():
                try:
                    self._ical = _dtz.tzical(io.StringIO(self._vtimezone))
                except Exception:  # noqa: BLE001 — dateutil wirft bei ungewohnten VTIMEZONE-Blöcken verschiedenes
                    warne_einmal("vtimezone", "Eine eingebettete VTIMEZONE ist nicht lesbar; es gelten Ersatzzonen")
        if self._ical is None:
            return None
        try:
            return self._ical.get(tzid)
        except Exception:  # noqa: BLE001
            return None

    def finde(self, tzid: str) -> tzinfo | None:
        """Die Zone zu ``tzid``; ``None``, wenn keine der Quellen sie kennt."""
        name = tzid.strip().strip('"').strip()
        if name in self._gemerkt:
            return self._gemerkt[name]
        zone = iana(name)
        if zone is None:
            windows = WINDOWS_ZONEN_KLEIN.get(name.lower())
            if windows is not None:
                zone = iana(windows)
        if zone is None:
            zone = self._eingebettet(name)
        if len(self._gemerkt) < 200:
            self._gemerkt[name] = zone
        return zone

    def zone(self, tzid: str | None) -> tzinfo | None:
        """Die Zone für eine Zeitangabe mit ``TZID``; kennt niemand sie, gilt die Standardzone (``None``: Hub)."""
        if not tzid or not tzid.strip():
            return self.standard
        gefunden = self.finde(tzid)
        if gefunden is None:
            warne_einmal("tzid:" + tzid[:60], "Unbekannte Zeitzone %r in einem Kalender; es gilt die Standardzone", tzid[:60])
            return self.standard
        return gefunden


# ── Zeitangaben lesen ───────────────────────────────────────────────────────────────────────────────────
_TAG = re.compile(r"(\d{4})(\d{2})(\d{2})")
_ZEITPUNKT = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z?)")
_DAUER = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


def lies_zeit(wert: str, params: dict[str, str], zonen: Zonen) -> date | datetime:
    """Eine iCalendar-Zeitangabe: ein Datum (``VALUE=DATE``) oder ein Zeitpunkt mit Versatz (``Z``, ``TZID``, sonst
    die Standardzone). ``ValueError``, wenn der Text keins von beiden ist."""
    text = wert.strip()
    zeitpunkt = _ZEITPUNKT.fullmatch(text)
    if (params.get("VALUE") or "").upper() == "DATE" or (zeitpunkt is None and _TAG.fullmatch(text)):
        tag = _TAG.match(text)
        if tag is None:
            raise ValueError(f"kein Datum: {text[:40]!r}")
        return date(int(tag[1]), int(tag[2]), int(tag[3]))
    if zeitpunkt is None:
        raise ValueError(f"keine Zeitangabe: {text[:40]!r}")
    roh = datetime(
        int(zeitpunkt[1]), int(zeitpunkt[2]), int(zeitpunkt[3]), int(zeitpunkt[4]), int(zeitpunkt[5]),
        min(int(zeitpunkt[6] or 0), 59),
    )
    if zeitpunkt[7]:
        return roh.replace(tzinfo=timezone.utc)
    return lokalisiere(roh, zonen.zone(params.get("TZID")))


def lies_dauer(wert: str) -> timedelta:
    """Eine iCalendar-Dauer wie ``PT1H30M``, ``P1D`` oder ``P2W``. ``ValueError`` bei anderem Text."""
    m = _DAUER.fullmatch(wert.strip().upper())
    if m is None or not any(m.groups()[1:]):
        raise ValueError(f"keine Dauer: {wert.strip()[:40]!r}")
    wochen, tage, stunden, minuten, sekunden = (int(g or 0) for g in m.groups()[1:])
    summe = timedelta(weeks=wochen, days=tage, hours=stunden, minutes=minuten, seconds=sekunden)
    return -summe if m[1] == "-" else summe


# ── Fenster ─────────────────────────────────────────────────────────────────────────────────────────────
def spanne(start: date | datetime, ende: date | datetime) -> tuple[datetime, datetime]:
    """Beginn und Ende als Zeiten mit Versatz; ein ganztägiger Termin beginnt und endet um Mitternacht der Zeitzone
    des Hubs (``ende`` ist der Tag nach dem letzten)."""
    a = lokalisiere(start) if isinstance(start, datetime) else tagesanfang(start)
    b = lokalisiere(ende) if isinstance(ende, datetime) else tagesanfang(ende)
    return a, max(a, b)


def im_fenster(a: datetime, b: datetime, von: datetime, bis: datetime) -> bool:
    """Gehört der Termin ``[a, b)`` in das Fenster ``[von, bis)``? Er gehört hinein, wenn er es berührt; ein Termin ohne
    Dauer nur, wenn sein Beginn im Fenster liegt."""
    if a == b:
        return von <= a < bis
    return a < bis and b > von
