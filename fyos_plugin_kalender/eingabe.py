"""Eingaben prüfen: Zeitfenster, Kalender-Auswahl und Termine zum Anlegen.

``neu_pruefen`` ist das Gegenstück zu ``mcp_hub.termine.pruefe_neu`` des Kerns (gleiche Regeln, gleiche Fehlertexte): das
Plugin vertraut dem Aufrufer nicht, auch wenn der Kern schon geprüft hat. ``ValueError`` heißt für den Hub „falsches
Argument“ und zählt nicht gegen den Schutzschalter.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from . import zeit

#: Längstes Fenster für die Termin-Quelle des Kerns und längster einzelner Termin
FENSTER_MAX_TAGE = 366
#: Längstes Fenster des Werkzeugs ``kalender.termine``
WERKZEUG_TAGE_MAX = 31
TAGE_VORGABE = 7
TITEL_MAX = 200
ORT_MAX = 200
KALENDER_MAX = 200
BESCHREIBUNG_MAX = 2000
NEU_FELDER = ("titel", "start", "ende", "ganztaegig", "ort", "beschreibung", "kalender")


def zeile(wert: Any, grenze: int | None = None) -> str:
    """Ein Wert als Text in einer Zeile (Leerraum zusammengezogen)."""
    text = " ".join(str(wert).split()) if wert is not None else ""
    return text[:grenze] if grenze else text


def lies(wert: Any) -> date | datetime | None:
    """Datum (``JJJJ-MM-TT``) oder Zeitstempel (ISO 8601) aus einem Wert; sonst ``None``."""
    if isinstance(wert, (datetime, date)):
        return wert
    text = str(wert or "").strip()
    if not text:
        return None
    try:
        if "T" not in text and " " not in text:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def zeit_eingabe(wert: Any, name: str) -> datetime:
    """Eine Zeitangabe als Zeit mit Versatz; ein Tag gilt ab Mitternacht, Angaben ohne Versatz in der Zeit des Hubs."""
    gelesen = lies(wert)
    if gelesen is None:
        raise ValueError(f"{name}: keine gültige Zeit (ISO 8601, etwa 2026-10-12 oder 2026-10-12T09:00:00+02:00)")
    if not isinstance(gelesen, datetime):
        gelesen = datetime.combine(gelesen, time.min)
    try:
        return zeit.lokalisiere(gelesen)
    except (ValueError, OverflowError, OSError):
        raise ValueError(f"{name}: keine gültige Zeit") from None


def fenster(von: Any, bis: Any) -> tuple[datetime, datetime]:
    """Das Fenster ``[von, bis)`` der Termin-Quelle: ``bis`` nach ``von``, höchstens 366 Tage."""
    a, b = zeit_eingabe(von, "von"), zeit_eingabe(bis, "bis")
    if b <= a:
        raise ValueError("bis muss nach von liegen")
    if b - a > timedelta(days=FENSTER_MAX_TAGE):
        raise ValueError(f"das Fenster ist länger als {FENSTER_MAX_TAGE} Tage")
    return a, b


def _leer(wert: Any) -> bool:
    return wert is None or (isinstance(wert, str) and not wert.strip())


def tage_eingabe(wert: Any, grenze: int = WERKZEUG_TAGE_MAX) -> int:
    if isinstance(wert, bool):
        raise ValueError("tage muss eine ganze Zahl sein")
    try:
        n = int(wert)
        if isinstance(wert, float) and n != wert:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("tage muss eine ganze Zahl sein") from None
    if not 1 <= n <= grenze:
        raise ValueError(f"tage muss zwischen 1 und {grenze} liegen")
    return n


def werkzeug_fenster(tage: Any, von: Any, bis: Any, jetzt: datetime | None = None) -> tuple[datetime, datetime]:
    """Das Fenster für ``kalender.termine``: ``tage`` ganze Kalendertage ab ``von`` (Vorgabe: heute 0 Uhr, 7 Tage) oder
    von ``von`` bis ``bis`` (ausgeschlossen); höchstens 31 Tage."""
    if not _leer(tage) and not _leer(bis):
        raise ValueError("tage und bis schließen sich aus: entweder tage (ab von, sonst ab heute) oder von und bis")
    if not _leer(bis) and _leer(von):
        raise ValueError("bis braucht von")
    anfang = zeit.tagesanfang(zeit.heute(jetzt)) if _leer(von) else zeit_eingabe(von, "von")
    if not _leer(bis):
        ende = zeit_eingabe(bis, "bis")
    else:
        ende = anfang + timedelta(days=TAGE_VORGABE if _leer(tage) else tage_eingabe(tage))
    if ende <= anfang:
        raise ValueError("bis muss nach von liegen")
    if ende - anfang > timedelta(days=WERKZEUG_TAGE_MAX) + timedelta(hours=1):  # eine Stunde Luft für die Zeitumstellung
        raise ValueError(f"das Fenster ist länger als {WERKZEUG_TAGE_MAX} Tage")
    return anfang, ende


def kalender_filter(wert: Any) -> str | None:
    """Name oder Kennung eines Kalenders aus einer Eingabe; ``None``: alle."""
    if wert is not None and not isinstance(wert, str):
        raise ValueError("kalender muss ein Text sein (Name oder Kennung)")
    text = zeile(wert)
    if len(text) > KALENDER_MAX:
        raise ValueError(f"kalender ist länger als {KALENDER_MAX} Zeichen")
    return text or None


def neu_pruefen(termin: Any) -> dict[str, Any]:
    """Prüft die Eingabe für ``anlegen`` und liefert sie bereinigt: ``titel start ende ganztaegig ort beschreibung
    kalender``. Ganztägig sind ``start`` und ``ende`` Tage, ``ende`` der Tag nach dem letzten (fehlt er, ist es ein Tag).
    Sonst haben beide eine Uhrzeit; fehlt ``ende``, dauert der Termin eine Stunde. Zeiten ohne Versatz gelten in der
    Zeitzone des Hubs. ``ValueError`` bei falscher Eingabe."""
    if not isinstance(termin, dict):
        raise ValueError("termin: ein Objekt mit titel, start, ende … erwartet")
    fremd = sorted(str(k) for k in set(termin) - set(NEU_FELDER))
    if fremd:
        raise ValueError(f"termin: unbekannte Felder {', '.join(fremd)} (erlaubt: {', '.join(NEU_FELDER)})")
    for name in ("titel", "ort", "beschreibung", "kalender"):
        if termin.get(name) is not None and not isinstance(termin.get(name), str):
            raise ValueError(f"{name} muss ein Text sein")
    titel = zeile(termin.get("titel"))
    if not titel:
        raise ValueError("titel fehlt")
    if len(titel) > TITEL_MAX:
        raise ValueError(f"titel ist länger als {TITEL_MAX} Zeichen")
    start = lies(termin.get("start"))
    if start is None:
        raise ValueError("start fehlt oder ist keine gültige Zeit (ISO 8601: JJJJ-MM-TT oder JJJJ-MM-TTThh:mm)")
    roh_ende = termin.get("ende")
    ende = lies(roh_ende) if roh_ende not in (None, "") else None
    if roh_ende not in (None, "") and ende is None:
        raise ValueError("ende ist keine gültige Zeit (ISO 8601: JJJJ-MM-TT oder JJJJ-MM-TTThh:mm)")
    ganztaegig = termin.get("ganztaegig")
    if ganztaegig is None:
        ganztaegig = not isinstance(start, datetime)
    elif not isinstance(ganztaegig, bool):
        raise ValueError("ganztaegig muss true oder false sein")
    if ganztaegig:
        s = start.date() if isinstance(start, datetime) else start
        e = (ende.date() if isinstance(ende, datetime) else ende) if ende is not None else s
        if e < s:
            raise ValueError("ende liegt vor start")
        if e == s:
            e = s + timedelta(days=1)
        if e - s > timedelta(days=FENSTER_MAX_TAGE):
            raise ValueError(f"der Termin ist länger als {FENSTER_MAX_TAGE} Tage")
        start_text, ende_text = s.isoformat(), e.isoformat()
    else:
        if not isinstance(start, datetime):
            raise ValueError("start braucht eine Uhrzeit (oder ganztaegig: true)")
        if ende is not None and not isinstance(ende, datetime):
            raise ValueError("ende braucht eine Uhrzeit (oder ganztaegig: true)")
        e = ende if ende is not None else start + timedelta(hours=1)
        try:
            dauer = zeit.lokalisiere(e) - zeit.lokalisiere(start)
        except (ValueError, OverflowError, OSError):
            raise ValueError("start oder ende ist keine gültige Zeit") from None
        if dauer <= timedelta(0):
            raise ValueError("ende muss nach start liegen")
        if dauer > timedelta(days=FENSTER_MAX_TAGE):
            raise ValueError(f"der Termin ist länger als {FENSTER_MAX_TAGE} Tage")
        start_text, ende_text = start.isoformat(), e.isoformat()
    ort = zeile(termin.get("ort"))
    if len(ort) > ORT_MAX:
        raise ValueError(f"ort ist länger als {ORT_MAX} Zeichen")
    kalender = zeile(termin.get("kalender"))
    if len(kalender) > KALENDER_MAX:
        raise ValueError(f"kalender ist länger als {KALENDER_MAX} Zeichen")
    beschreibung = "".join(
        c for c in str(termin.get("beschreibung") or "").replace("\r\n", "\n").replace("\r", "\n") if c in "\n\t" or c >= " "
    ).strip()
    if len(beschreibung) > BESCHREIBUNG_MAX:
        raise ValueError(f"beschreibung ist länger als {BESCHREIBUNG_MAX} Zeichen")
    return {"titel": titel, "start": start_text, "ende": ende_text, "ganztaegig": ganztaegig, "ort": ort,
            "beschreibung": beschreibung, "kalender": kalender}


def typisiert(neu: dict[str, Any]) -> dict[str, Any]:
    """Ein geprüfter Termin (``neu_pruefen``) mit echten Werten: Tage als ``date``, Zeitpunkte als ``datetime`` mit Versatz."""
    if neu["ganztaegig"]:
        start: date | datetime = date.fromisoformat(neu["start"])
        ende: date | datetime = date.fromisoformat(neu["ende"])
    else:
        start = zeit.lokalisiere(datetime.fromisoformat(neu["start"]))
        ende = zeit.lokalisiere(datetime.fromisoformat(neu["ende"]))
    return {**neu, "start": start, "ende": ende}
