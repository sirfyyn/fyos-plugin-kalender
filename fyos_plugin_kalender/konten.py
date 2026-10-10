"""Konten: was der Besitzer einträgt und wie das Plugin es liest.

**Ein Konto aus der Umgebung** (der einfache Weg, auch das Formular des Installers füllt diese Felder):

=========================  ===============================================================================================
``FYOS_KALENDER_URL``      Adresse des CalDAV-Servers (oder des Kalenders) oder eines ICS-Abonnements (``webcal://`` geht)
``FYOS_KALENDER_NUTZER``   Nutzername (CalDAV; bei ICS nur, wenn die Adresse einen Zugang verlangt)
``FYOS_KALENDER_PASSWORT`` App-Passwort (nie das Haupt-Passwort des Anbieters)
``FYOS_KALENDER_ART``      ``caldav`` oder ``ics``; ohne Angabe: ``webcal://`` und Adressen auf ``.ics`` ohne Nutzer sind ICS
``FYOS_KALENDER_NAME``     Anzeigename des Kontos; ohne Angabe der Host
=========================  ===============================================================================================

**Weitere Konten** stehen als JSON-Liste in ``FYOS_KALENDER_KONTEN`` (oder in der ``config:`` des Plugins unter
``konten:``): je Konto ``name``, ``art``, ``url``, ``nutzer`` und ``passwort_env``, der **Name einer Umgebungsvariablen**,
die das App-Passwort enthält. Ein Passwort im JSON selbst lehnt das Plugin ab (es stünde in Konfigurationsdateien und
Sicherungen).

Ein unbrauchbarer Eintrag bricht nichts ab: er wird ein Konto mit ``fehler`` und zeigt seinen Grund in jeder Antwort.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

KONTEN_MAX = 20
NAME_MAX = 60
URL_MAX = 2000
ARTEN = ("caldav", "ics")
_JSON_FELDER = {"name", "art", "url", "nutzer", "passwort_env"}
_UMGEBUNGSNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
_NAME_VERBOTEN = re.compile(r"[:#/\\\x00-\x1f\x7f]")
_VORSATZ = ("www.", "caldav.", "dav.", "cal.", "calendar.", "webcal.")


@dataclass(frozen=True)
class Konto:
    """Ein Kalender-Konto. Das Passwort steht in keiner Darstellung (``repr``, ``als_dict``)."""

    name: str
    art: str  # caldav | ics
    url: str  # http:// oder https://; webcal:// ist schon umgeschrieben
    nutzer: str = ""
    passwort: str = field(default="", repr=False)
    fehler: str = ""  # gesetzt: der Eintrag ist unbrauchbar, das ist der Grund

    @property
    def adresse(self) -> str:
        """Schema und Host (mit Port) der Adresse. Der Pfad bleibt weg: bei ICS-Abonnements ist er das Geheimnis."""
        teile = urllib.parse.urlsplit(self.url)
        host = teile.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        try:
            port = teile.port
        except ValueError:
            port = None
        return f"{teile.scheme}://{host}" + (f":{port}" if port else "") if host else ""

    def als_dict(self) -> dict[str, Any]:
        """Für Antworten: Name, Art, Host und Nutzer; nie das Passwort, nie der Pfad der Adresse."""
        aus: dict[str, Any] = {"name": self.name, "art": self.art}
        if self.adresse:
            aus["adresse"] = self.adresse
        if self.nutzer:
            aus["nutzer"] = self.nutzer
        if self.fehler:
            aus["fehler"] = self.fehler
        return aus


def _name_aus(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for vorsatz in _VORSATZ:
        if host.startswith(vorsatz) and host.count(".") >= 2:
            host = host[len(vorsatz):]
            break
    return host[:NAME_MAX] or "kalender"


def _name_pruefen(roh: Any) -> str:
    if not isinstance(roh, str):
        raise ValueError("name muss ein Text sein")
    name = " ".join(roh.split())
    if not name:
        raise ValueError("name ist leer")
    if len(name) > NAME_MAX:
        raise ValueError(f"name ist länger als {NAME_MAX} Zeichen")
    if _NAME_VERBOTEN.search(name):
        raise ValueError("name enthält ein unzulässiges Zeichen (: # / \\)")
    return name


def _adresse_pruefen(roh: Any) -> tuple[str, str]:
    """(Adresse mit http/https, ursprüngliches Schema). ``ValueError`` mit einem Text ohne die Adresse selbst."""
    if not isinstance(roh, str) or not roh.strip():
        raise ValueError("url fehlt")
    url = roh.strip()
    if len(url) > URL_MAX or re.search(r"[\s\x00-\x1f\x7f]", url):
        raise ValueError("url ist ungültig (zu lang oder mit Leerzeichen)")
    teile = urllib.parse.urlsplit(url)
    schema = teile.scheme.lower()
    if schema in ("webcal", "webcals"):
        teile = teile._replace(scheme="https")
    elif schema not in ("http", "https"):
        raise ValueError("url muss mit https://, http:// oder webcal:// beginnen")
    try:
        host, _ = teile.hostname, teile.port
    except ValueError:
        raise ValueError("url hat einen ungültigen Port") from None
    if not host:
        raise ValueError("url nennt keinen Host")
    if "@" in teile.netloc:
        raise ValueError("url darf keinen Nutzer und kein Passwort enthalten (dafür gibt es eigene Felder)")
    return urllib.parse.urlunsplit(teile._replace(fragment="")), schema


def _art_waehlen(art: Any, schema: str, url: str, nutzer: str) -> str:
    if art not in (None, ""):
        gewaehlt = str(art).strip().lower()
        if gewaehlt not in ARTEN:
            raise ValueError("art muss caldav oder ics sein")
        return gewaehlt
    if schema in ("webcal", "webcals"):
        return "ics"
    if urllib.parse.urlsplit(url).path.lower().endswith(".ics") and not nutzer:
        return "ics"
    return "caldav"


def _konto_bauen(name: str | None, art: Any, url: Any, nutzer: Any, passwort: str, passwort_hinweis: str) -> Konto:
    """Prüft einen Eintrag; ``ValueError`` mit dem Grund. ``passwort_hinweis``: woher das Passwort kommen müsste."""
    adresse, schema = _adresse_pruefen(url)
    nutzer_text = "" if nutzer is None else " ".join(str(nutzer).split())
    if len(nutzer_text) > 200:
        raise ValueError("nutzer ist zu lang")
    gewaehlt = _art_waehlen(art, schema, adresse, nutzer_text)
    if gewaehlt == "caldav" and (not nutzer_text or not passwort):
        raise ValueError(f"CalDAV braucht Nutzer und App-Passwort ({passwort_hinweis})")
    if gewaehlt == "ics" and bool(nutzer_text) != bool(passwort):
        raise ValueError(f"für einen geschützten ICS-Zugang gehören Nutzer und Passwort zusammen ({passwort_hinweis})")
    return Konto(name=_name_pruefen(name) if name else _name_aus(adresse), art=gewaehlt, url=adresse,
                 nutzer=nutzer_text, passwort=passwort)


def _eintrag_aus_json(roh: Any, umgebung: Mapping[str, str]) -> Konto:
    if not isinstance(roh, dict):
        raise ValueError("ein Konto muss ein Objekt mit name, art, url, nutzer und passwort_env sein")
    if "passwort" in roh or "password" in roh:
        raise ValueError("Passwörter gehören nicht in die Liste: passwort_env nennt stattdessen eine Umgebungsvariable")
    fremd = sorted(str(k) for k in set(roh) - _JSON_FELDER)
    if fremd:
        raise ValueError(f"unbekannte Felder: {', '.join(fremd)}")
    passwort = ""
    ref = roh.get("passwort_env")
    if ref not in (None, ""):
        if not isinstance(ref, str) or not _UMGEBUNGSNAME.fullmatch(ref):
            raise ValueError("passwort_env muss der Name einer Umgebungsvariablen sein")
        passwort = umgebung.get(ref, "")
        if not passwort:
            raise ValueError(f"die Umgebungsvariable {ref} ist nicht gesetzt oder leer")
    return _konto_bauen(roh.get("name"), roh.get("art"), roh.get("url"), roh.get("nutzer"), passwort,
                        "passwort_env und nutzer")


def _liste(roh: Any, quelle: str) -> list[Any]:
    if not isinstance(roh, list):
        raise ValueError(f"{quelle} muss eine Liste von Konten sein")
    return roh


def konten_lesen(umgebung: Mapping[str, str], config: Mapping[str, Any] | None = None) -> list[Konto]:
    """Alle Konten: das aus ``FYOS_KALENDER_*``, dann die aus ``config: konten:``, dann die aus ``FYOS_KALENDER_KONTEN``.
    Jeder unbrauchbare Eintrag wird ein Konto mit ``fehler``."""
    konten: list[Konto] = []
    namen: set[str] = set()

    def aufnehmen(konto: Konto) -> None:
        if len(konten) >= KONTEN_MAX:
            return
        if konto.name.casefold() in namen:
            konto = Konto(name=f"{konto.name} ({len(konten) + 1})", art=konto.art, url=konto.url,
                          fehler="der Name ist schon vergeben")
        namen.add(konto.name.casefold())
        konten.append(konto)

    def fehlerhaft(bezeichnung: str, grund: str) -> None:
        name = bezeichnung
        while name.casefold() in namen:
            name += "+"
        aufnehmen(Konto(name=name, art="caldav", url="", fehler=grund))

    url = (umgebung.get("FYOS_KALENDER_URL") or "").strip()
    if url:
        try:
            name = (umgebung.get("FYOS_KALENDER_NAME") or "").strip() or None
            aufnehmen(_konto_bauen(
                name, umgebung.get("FYOS_KALENDER_ART"), url, umgebung.get("FYOS_KALENDER_NUTZER"),
                umgebung.get("FYOS_KALENDER_PASSWORT") or "", "FYOS_KALENDER_NUTZER und FYOS_KALENDER_PASSWORT",
            ))
        except ValueError as e:
            fehlerhaft("FYOS_KALENDER_URL", str(e))

    eintraege: list[tuple[str, Any]] = []
    quellen: list[tuple[str, Any]] = []
    if config and config.get("konten") not in (None, ""):
        quellen.append(("konten in der Plugin-Konfiguration", config.get("konten")))
    json_text = (umgebung.get("FYOS_KALENDER_KONTEN") or "").strip()
    if json_text:
        try:
            quellen.append(("FYOS_KALENDER_KONTEN", json.loads(json_text)))
        except ValueError:
            fehlerhaft("FYOS_KALENDER_KONTEN", "ist kein gültiges JSON (erwartet: eine Liste von Konten)")
    for bezeichnung, roh in quellen:
        try:
            liste = _liste(roh, bezeichnung)
        except ValueError as e:
            fehlerhaft(bezeichnung, str(e))
            continue
        eintraege += [(f"{bezeichnung}, Eintrag {i}", e) for i, e in enumerate(liste, 1)]
    for bezeichnung, roh in eintraege:
        try:
            aufnehmen(_eintrag_aus_json(roh, umgebung))
        except ValueError as e:
            name = roh.get("name") if isinstance(roh, dict) and isinstance(roh.get("name"), str) else None
            fehlerhaft(" ".join(name.split())[:NAME_MAX] if name and name.strip() else bezeichnung, str(e))
    return konten
