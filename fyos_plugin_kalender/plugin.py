"""kalender — Termine aus CalDAV-Konten und ICS-Abonnements, und die Quelle der Termine für die anderen Plugins.

Werkzeuge:
    kalender.konten    lesend      welche Konten eingerichtet sind und welche Kalender sie haben (nie Passwörter)
    kalender.termine   lesend      Termine der nächsten Tage oder eines Fensters, Wiederholungen aufgelöst
    kalender.anlegen   schreibend  einen neuen Termin in einem CalDAV-Kalender anlegen; mit Trockenlauf, der echte Lauf
                                   nur mit ``bestaetigt: true``. Ändern und Löschen gibt es nicht.

Konten (siehe ``konten``): ``FYOS_KALENDER_URL``, ``FYOS_KALENDER_NUTZER`` und ``FYOS_KALENDER_PASSWORT`` für ein Konto,
``FYOS_KALENDER_KONTEN`` (JSON-Liste, Passwörter nur über ``passwort_env``) für mehrere.

**Quelle der Termine.** Das Plugin meldet sich beim Start in ``mcp_hub.termine`` als Ablage an. Leitstand, Tagesbrief und
Startseite fragen dort ``quelle()`` und bekommen die Termine dieser Konten, ohne CalDAV zu können und ohne eine Adresse zu
kennen. Dafür gilt eine strengere Regel als für das Werkzeug: antwortet **ein** Konto nicht, wirft die Ablage einen
``QuelleFehler`` (mit dem Namen des Kontos), statt eine unvollständige Liste zu liefern; sonst sähe ein Tag frei aus, an dem
in Wahrheit ein Konto fehlt. Das Werkzeug ``kalender.termine`` liefert dagegen die Termine der erreichbaren Konten und nennt
die anderen in ``konten_fehler``.

Fehlt ``mcp_hub.termine`` (fyos-hub 0.1.0), laufen die Werkzeuge trotzdem; nur die Anmeldung als Quelle entfällt.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from mcp_hub.plugin import HubPlugin, ToolDescriptor, build_dry_run_result, is_dry_run

from . import __version__, eingabe, kern, zeit
from .konten import Konto, konten_lesen
from .netz import NetzFehler
from .quellen import Kalender, Quelle, auswahl, instanz_dict, quelle_fuer, sortiert, termin_dict

_log = logging.getLogger("fyos_plugin_kalender")

#: So viele Abfragen laufen höchstens zugleich
FAEDEN_MAX = 6
#: So viele Termine gibt das Werkzeug ``kalender.termine`` höchstens zurück
AUSGABE_MAX = 200
HINWEIS_EINRICHTEN = (
    "Kalender nicht eingerichtet: im Installer den Baustein „Kalender“ wählen oder FYOS_KALENDER_URL, "
    "FYOS_KALENDER_NUTZER und FYOS_KALENDER_PASSWORT (App-Passwort) setzen; mehrere Konten stehen in "
    "FYOS_KALENDER_KONTEN."
)


@dataclass
class Abfrage:
    """Ergebnis einer Abfrage über alle gewählten Kalender."""

    termine: list[dict[str, Any]] = field(default_factory=list)
    fehler: dict[str, str] = field(default_factory=dict)  # Konto -> Grund
    erfolge: int = 0  # so viele Kalender haben geantwortet


def _parallel(aufgaben: list[tuple[Any, Callable[[], Any]]]) -> list[tuple[Any, Any, Exception | None]]:
    """Führt die Aufgaben zugleich aus; liefert je Aufgabe (Schlüssel, Ergebnis, Fehler) in der Reihenfolge der Eingabe."""

    def lauf(f: Callable[[], Any]) -> tuple[Any, Exception | None]:
        try:
            return f(), None
        except Exception as fehler:  # noqa: BLE001 — jede Aufgabe meldet ihren Fehler einzeln
            return None, fehler

    if len(aufgaben) <= 1:
        return [(k, *lauf(f)) for k, f in aufgaben]
    with ThreadPoolExecutor(max_workers=min(FAEDEN_MAX, len(aufgaben)), thread_name_prefix="kalender") as pool:
        laeufe = [(k, pool.submit(lauf, f)) for k, f in aufgaben]
        return [(k, *lauf_.result()) for k, lauf_ in laeufe]


class KalenderPlugin(HubPlugin):
    name = "kalender"
    description = (
        "Kalender: Termine aus CalDAV-Konten (Nextcloud, iCloud, Radicale …) und ICS-Abonnements lesen, Termine "
        "anlegen; Quelle der Termine für Leitstand und Tagesbrief."
    )
    version = __version__
    argumente_vertraulich = True  # Titel und Orte sind persönlich

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.config = dict(config or {})
        uhr = self.config.get("uhr")
        self._jetzt: Callable[[], datetime] = self.config.get("jetzt") or (lambda: datetime.now(timezone.utc))
        self._quellen: list[Quelle] = []
        self._unbrauchbar: list[Konto] = []
        for konto in konten_lesen(os.environ, self.config):
            if konto.fehler:
                self._unbrauchbar.append(konto)
            else:
                self._quellen.append(quelle_fuer(konto, uhr=uhr))
        self._registry: Any = None
        self._angemeldet = False

    # ── Lebenslauf ────────────────────────────────────────────────────────────────────────────────────
    @property
    def eingerichtet(self) -> bool:
        """Ist mindestens ein Konto eingetragen (auch ein unbrauchbares: dann zeigt der Leser den Fehler)?"""
        return bool(self._quellen or self._unbrauchbar)

    def register(self) -> None:
        # Nur Leichtes und ohne Netz: der Hub startet nicht mit, wenn ein Kalender-Server gerade fehlt.
        if not self.eingerichtet:
            _log.info("kalender: kein Konto eingerichtet (FYOS_KALENDER_URL …)")
        for q in self._quellen:
            _log.info("kalender: Konto %s (%s, %s)", q.konto.name, q.konto.art, q.host)
        for k in self._unbrauchbar:
            _log.warning("kalender: Konto %s ist unbrauchbar: %s", k.name, k.fehler)

    def registry_anbinden(self, registry: Any) -> None:
        """Vom Server gerufen: meldet das Plugin als Quelle der Termine an (``mcp_hub.termine``), wenn Konten eingetragen
        sind. Auch bei unbrauchbaren Einträgen, damit die Leser den Fehler sehen und nicht „nicht eingerichtet“."""
        self._registry = registry
        if self.eingerichtet:
            self._angemeldet = kern.anmelden(self)
            if not self._angemeldet:
                _log.info("kalender: der Kern hat keine Termin-Quelle (fyos-hub 0.1.1 oder neuer); nur die Werkzeuge laufen")

    def shutdown(self) -> None:
        if self._angemeldet:
            kern.abmelden(self)
            self._angemeldet = False

    def health(self) -> dict[str, Any]:
        # Ohne Netz: der Hub fragt oft. Ob ein Server antwortet, zeigen kalender.konten und kalender.termine.
        if not self.eingerichtet:
            return {"ok": True, "status": "aus", "reason": "kein Konto eingerichtet (FYOS_KALENDER_URL …)"}
        stand: dict[str, Any] = {"konten": len(self._quellen), "quelle_der_termine": self._angemeldet}
        if self._unbrauchbar:
            namen = ", ".join(k.name for k in self._unbrauchbar)
            return {"ok": False, "status": "unhealthy", "reason": f"{len(self._unbrauchbar)} Konto-Eintrag unbrauchbar: {namen}", **stand}
        return {"ok": True, "status": "healthy", **stand}

    # ── Beschreibung ──────────────────────────────────────────────────────────────────────────────────
    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="konten",
                title="Kalender-Konten",
                description=(
                    "Zeigt, welche Kalender-Konten eingerichtet sind (CalDAV oder ICS-Abonnement) und welche Kalender sie "
                    "haben: Name, Kennung und ob man dort Termine anlegen kann. Zeigt nie Passwörter. Nur lesend; "
                    "`neu_laden: true` sucht die Kalender neu."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "neu_laden": {"type": "boolean", "description": "Kalender neu suchen statt aus dem Zwischenspeicher (eine Stunde)"},
                    },
                    "additionalProperties": False,
                },
                tags=["kalender", "read"],
            ),
            ToolDescriptor(
                name="termine",
                title="Termine",
                description=(
                    "Termine aus den eingerichteten Kalendern: die nächsten `tage` Tage ab heute 0 Uhr (Vorgabe 7, höchstens 31) "
                    "oder das Fenster `von` bis `bis`. Wiederholungen sind aufgelöst. `kalender` wählt einen Kalender nach Namen "
                    "oder Kennung. Je Termin: id, titel, start, ende, ganztaegig, ort, kalender, konto; bei ganztägigen Terminen "
                    "ist `ende` der Tag nach dem letzten. Beschreibung und Teilnehmer gehören nicht dazu. Nur lesend. Antwortet "
                    "ein Konto nicht, steht es in `konten_fehler`; das heißt nie „keine Termine“."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "tage": {"type": "integer", "minimum": 1, "maximum": eingabe.WERKZEUG_TAGE_MAX,
                                 "description": "So viele Kalendertage ab `von` (Vorgabe: heute 0 Uhr); nicht zusammen mit `bis`"},
                        "von": {"type": "string", "description": "Beginn, ISO 8601: 2026-10-12 (ab 0 Uhr) oder 2026-10-12T09:00"},
                        "bis": {"type": "string", "description": "Ende (ausgeschlossen): von 2026-10-12 bis 2026-10-14 liefert den 12. und 13.; braucht `von`; höchstens 31 Tage danach"},
                        "kalender": {"type": "string", "description": "Name oder Kennung eines Kalenders (siehe kalender.konten); ohne Angabe alle"},
                    },
                    "additionalProperties": False,
                },
                tags=["kalender", "read"],
            ),
            ToolDescriptor(
                name="anlegen",
                title="Termin anlegen",
                description=(
                    "Legt einen neuen Termin in einem CalDAV-Kalender an. Ändern und Löschen gibt es nicht; ein Abonnement ist "
                    "schreibgeschützt. Nur auf ausdrücklichen Wunsch. Zuerst mit `__dry_run__: true` den Plan (Kalender, Zeit) "
                    "zeigen und bestätigen lassen; ein echter Lauf braucht `bestaetigt: true`. `beschreibung` nur setzen, wenn "
                    "der Mensch einen Text dafür nennt. Ohne `kalender` genügt es, wenn es genau einen beschreibbaren gibt."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "titel": {"type": "string", "minLength": 1, "maxLength": eingabe.TITEL_MAX, "description": "Titel des Termins"},
                        "start": {"type": "string", "description": "Beginn, ISO 8601: 2026-10-15T09:00 (Zeit des Hubs, wenn ohne Versatz) oder bei ganztägigen 2026-10-15"},
                        "ende": {"type": "string", "description": "Ende; ohne Angabe eine Stunde (ganztägig: ein Tag). Ganztägig: der Tag nach dem letzten"},
                        "ganztaegig": {"type": "boolean", "description": "Ganztägig; ohne Angabe ganztägig, wenn `start` nur ein Datum ist"},
                        "ort": {"type": "string", "maxLength": eingabe.ORT_MAX, "description": "Ort (optional)"},
                        "beschreibung": {"type": "string", "maxLength": eingabe.BESCHREIBUNG_MAX, "description": "Beschreibung, nur auf ausdrücklichen Wunsch"},
                        "kalender": {"type": "string", "description": "Name oder Kennung des Kalenders (optional, wenn es nur einen beschreibbaren gibt)"},
                        "bestaetigt": {"type": "boolean", "description": "Echter Lauf: vorher den Plan (`__dry_run__`) zeigen und bestätigen lassen"},
                    },
                    "required": ["titel", "start"],
                    "additionalProperties": False,
                },
                tags=["kalender", "write"],
            ),
        ]

    # ── Aufrufe ───────────────────────────────────────────────────────────────────────────────────────
    def call(self, method: str, args: dict[str, Any]) -> Any:
        args = args or {}
        if method == "konten":
            return self.konten(args.get("neu_laden"))
        if method == "termine":
            return self.termine(args.get("tage"), args.get("von"), args.get("bis"), args.get("kalender"))
        if method == "anlegen":
            return self.anlegen(args)
        raise ValueError(f"kalender hat kein Werkzeug {method!r}")

    # ── Hilfen ────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _text(quelle: Quelle | None, fehler: BaseException) -> str:
        """Der Grund eines Fehlers zum Zeigen: bei Netzfehlern ihr Text, sonst nur der Typ (nie ein fremder Text)."""
        name = quelle.konto.name if quelle is not None else "?"
        if isinstance(fehler, NetzFehler):
            _log.info("kalender: Konto %s: %s", name, fehler.text)
            return fehler.text
        _log.warning("kalender: Konto %s: unerwarteter Fehler (%s)", name, type(fehler).__name__,
                     exc_info=_log.isEnabledFor(logging.DEBUG))
        return f"Unerwarteter Fehler ({type(fehler).__name__})"

    def _kalender_je_konto(self, neu_laden: bool = False) -> list[tuple[Quelle, list[Kalender] | None, str | None]]:
        aufgaben = [(q, (lambda q=q: q.kalender(neu_laden))) for q in self._quellen]
        return [(q, liste, self._text(q, f) if f is not None else None) for q, liste, f in _parallel(aufgaben)]

    def _abfragen(self, von: datetime, bis: datetime, filter_: str | None) -> Abfrage:
        """Die Termine aller Kalender (oder der zum Filter passenden) im Fenster ``[von, bis)``, nach Beginn sortiert.
        Fehler stehen je Konto in ``fehler``. Passt der Filter auf Kalender erreichbarer Konten, zählen Fehler beim Suchen
        der Kalender anderer Konten nicht (sie sagen nichts über die gewählten). ``ValueError``, wenn nichts passt und
        alle Konten antworteten."""
        ergebnis = Abfrage()
        for k in self._unbrauchbar:
            ergebnis.fehler[k.name] = f"Konfiguration fehlerhaft: {k.fehler}"
        paare: list[tuple[Quelle, Kalender]] = []
        for quelle, liste, fehler in self._kalender_je_konto():
            if fehler is not None:
                ergebnis.fehler.setdefault(quelle.konto.name, fehler)
            else:
                paare += [(quelle, k) for k in liste or []]
        if filter_:
            gewaehlt = {k.id for k in auswahl([k for _, k in paare], filter_)}
            if not gewaehlt:
                if ergebnis.fehler:
                    return ergebnis
                raise ValueError(f"kein Kalender {filter_!r}; vorhanden: {self._namen(paare)}")
            paare = [(q, k) for q, k in paare if k.id in gewaehlt]
            ergebnis.fehler = {}
        aufgaben = [
            ((q, k), (lambda q=q, k=k: [instanz_dict(k, i) for i in q.instanzen(k, von, bis)])) for q, k in paare
        ]
        for (quelle, _), liste, fehler in _parallel(aufgaben):
            if fehler is not None:
                ergebnis.fehler.setdefault(quelle.konto.name, self._text(quelle, fehler))
            else:
                ergebnis.termine += liste
                ergebnis.erfolge += 1
        ergebnis.termine = sortiert(ergebnis.termine)
        return ergebnis

    @staticmethod
    def _namen(paare: list[tuple[Quelle, Kalender]]) -> str:
        namen = [k.name for _, k in paare]
        return ", ".join(repr(n) for n in namen[:30]) + (" …" if len(namen) > 30 else "") if namen else "keine"

    def _ziel(self, name: str) -> tuple[Quelle, Kalender]:
        """Der Kalender für einen neuen Termin. ``ValueError``, wenn die Wahl nicht eindeutig ist; ``NetzFehler``, wenn
        ein Konto bei der Suche nicht antwortet und deshalb nichts Sicheres zu sagen ist (ohne Namen wäre „der einzige
        beschreibbare Kalender“ womöglich nur der einzige, den man gerade sieht)."""
        alle: list[tuple[Quelle, Kalender]] = []
        probleme: list[str] = []
        for quelle, liste, fehler in self._kalender_je_konto():
            if fehler is not None:
                probleme.append(f"{quelle.konto.name}: {fehler}")
            else:
                alle += [(quelle, k) for k in liste or []]
        probleme += [f"{k.name}: Konfiguration fehlerhaft ({k.fehler})" for k in self._unbrauchbar]
        if name:
            gewaehlt = {k.id for k in auswahl([k for _, k in alle], name)}
            treffer = [(q, k) for q, k in alle if k.id in gewaehlt]
            if not treffer:
                if probleme:
                    raise NetzFehler("verbindung", "Kalender nicht gefunden, weil ein Konto nicht lesbar ist: " + "; ".join(probleme))
                raise ValueError(f"kein Kalender {name!r}; vorhanden: {self._namen(alle)}")
        else:
            if probleme:
                raise NetzFehler(
                    "verbindung",
                    "Der Kalender lässt sich nicht sicher wählen, weil ein Konto nicht lesbar ist (" + "; ".join(probleme)
                    + "); mit `kalender` einen nennen",
                )
            treffer = alle
        schreibbar = [(q, k) for q, k in treffer if k.schreibbar and q.schreibbar()]
        if not schreibbar:
            if treffer:
                raise ValueError(
                    "Dieser Kalender ist schreibgeschützt (ICS-Abonnement oder keine Berechtigung); Termine lassen sich nur "
                    "in CalDAV-Kalendern anlegen"
                )
            raise ValueError("kein Kalender, in dem sich Termine anlegen lassen (ein CalDAV-Konto ist nötig)")
        if len(schreibbar) > 1:
            auch = ", ".join(f"{k.name!r} ({k.id})" for _, k in schreibbar[:12])
            raise ValueError(
                (f"dieser Name passt auf mehrere Kalender: {auch}" if name else f"es gibt mehrere beschreibbare Kalender: {auch}")
                + "; mit `kalender` (Name oder Kennung) einen genau wählen"
            )
        return schreibbar[0]

    def _schreiben(self, quelle: Quelle, kalender: Kalender, neu: dict[str, Any]) -> dict[str, Any]:
        typ = eingabe.typisiert(neu)
        uid = quelle.anlegen(kalender, typ, self._jetzt())
        return termin_dict(kalender, uid, "", neu["titel"], neu["ort"], typ["start"], typ["ende"], neu["ganztaegig"])

    # ── kalender.konten ───────────────────────────────────────────────────────────────────────────────
    def konten(self, neu_laden: Any = None) -> dict[str, Any]:
        if neu_laden is not None and not isinstance(neu_laden, bool):
            raise ValueError("neu_laden muss true oder false sein")
        if not self.eingerichtet:
            return {"ok": True, "eingerichtet": False, "konten": [], "hinweis": HINWEIS_EINRICHTEN}
        zeilen: list[dict[str, Any]] = []
        for quelle, liste, fehler in self._kalender_je_konto(bool(neu_laden)):
            zeile = quelle.konto.als_dict()
            if fehler is not None:
                zeile.update({"status": "fehler", "fehler": fehler})
            else:
                zeile["status"] = "ok"
                zeile["kalender"] = [{"id": k.id, "name": k.name, "schreibbar": k.schreibbar} for k in liste or []]
            zeilen.append(zeile)
        for konto in self._unbrauchbar:
            zeilen.append({**konto.als_dict(), "status": "fehler"})
        schlecht = [z for z in zeilen if z["status"] == "fehler"]
        antwort: dict[str, Any] = {"ok": True, "eingerichtet": True, "konten": zeilen}
        if schlecht:
            antwort["hinweis"] = f"{len(schlecht)} von {len(zeilen)} Konten sind nicht nutzbar; ihre Termine fehlen."
        return antwort

    # ── kalender.termine ──────────────────────────────────────────────────────────────────────────────
    def termine(self, tage: Any = None, von: Any = None, bis: Any = None, kalender: Any = None) -> dict[str, Any]:
        a, b = eingabe.werkzeug_fenster(tage, von, bis, self._jetzt())
        filter_ = eingabe.kalender_filter(kalender)
        if not self.eingerichtet:
            return {"ok": False, "eingerichtet": False, "error": HINWEIS_EINRICHTEN}
        ergebnis = self._abfragen(a, b, filter_)
        fehler = [{"konto": konto, "fehler": text} for konto, text in ergebnis.fehler.items()]
        if ergebnis.fehler and not ergebnis.erfolge:
            gruende = "; ".join(f"{f['konto']}: {f['fehler']}" for f in fehler)
            return {"ok": False, "error": f"Kein Kalender antwortet ({gruende})", "konten_fehler": fehler}
        gesamt = len(ergebnis.termine)
        antwort: dict[str, Any] = {
            "ok": True, "von": zeit.iso(a), "bis": zeit.iso(b), "anzahl": gesamt, "termine": ergebnis.termine[:AUSGABE_MAX],
        }
        if gesamt > AUSGABE_MAX:
            antwort["gekuerzt"] = True
            antwort["hinweis"] = f"Nur die ersten {AUSGABE_MAX} von {gesamt} Terminen; das Fenster verkleinern."
        elif not gesamt and not fehler:
            antwort["hinweis"] = "Keine Termine in diesem Zeitraum."
        if fehler:
            antwort["konten_fehler"] = fehler
            antwort["hinweis"] = "Unvollständig: " + ", ".join(f["konto"] for f in fehler) + " antwortet nicht."
        return antwort

    # ── kalender.anlegen ──────────────────────────────────────────────────────────────────────────────
    def anlegen(self, args: dict[str, Any]) -> dict[str, Any]:
        trocken = is_dry_run(args)
        bestaetigt = args.get("bestaetigt")
        if bestaetigt is not None and not isinstance(bestaetigt, bool):
            raise ValueError("bestaetigt muss true oder false sein")
        termin = {k: v for k, v in args.items() if k not in ("bestaetigt", "__dry_run__")}
        neu = eingabe.neu_pruefen(termin)
        if not self.eingerichtet:
            return {"ok": False, "eingerichtet": False, "error": HINWEIS_EINRICHTEN}
        try:
            quelle, kalender = self._ziel(neu["kalender"])
        except NetzFehler as e:
            return {"ok": False, "error": e.text}
        if trocken:
            return build_dry_run_result(
                would_execute=f"Termin {neu['titel']!r} ({self._wann(neu)}) im Kalender {kalender.name!r} ({kalender.konto}) anlegen",
                expected_side_effects=[f"{kalender.konto} / {kalender.name}: neuer Termin"],
                estimated_cost=1,
                plugin=self.name,
                method="anlegen",
                args_preview={
                    "titel": neu["titel"], "start": self._zeit_text(neu, "start"), "ende": self._zeit_text(neu, "ende"),
                    "ganztaegig": neu["ganztaegig"], "ort": neu["ort"], "kalender": kalender.name,
                    "beschreibung": f"{len(neu['beschreibung'])} Zeichen" if neu["beschreibung"] else "",
                },
            )
        if bestaetigt is not True:
            raise ValueError(
                "Nichts angelegt: ein echter Lauf braucht bestaetigt: true — erst mit __dry_run__ den Plan zeigen und "
                "bestätigen lassen."
            )
        try:
            angelegt = self._schreiben(quelle, kalender, neu)
        except NetzFehler as e:
            _log.info("kalender: Konto %s: Anlegen gescheitert: %s", quelle.konto.name, e.text)
            return {"ok": False, "error": e.text}
        return {
            "ok": True,
            "angelegt": angelegt,
            "satz": f"Termin {angelegt['titel']!r} angelegt ({self._wann(neu)}, Kalender {kalender.name!r}).",
        }

    @staticmethod
    def _zeit_text(neu: dict[str, Any], feld: str) -> str:
        """Beginn oder Ende eines geprüften Termins, wie er angelegt würde: ein Tag, sonst mit dem Versatz der Hub-Zeit."""
        if neu["ganztaegig"]:
            return str(neu[feld])
        return zeit.iso(zeit.lokalisiere(datetime.fromisoformat(neu[feld])))

    @staticmethod
    def _wann(neu: dict[str, Any]) -> str:
        """Der Zeitraum eines geprüften Termins in einem Satz (für Plan und Bestätigung)."""
        if neu["ganztaegig"]:
            erster = date.fromisoformat(neu["start"])
            letzter = date.fromisoformat(neu["ende"]) - timedelta(days=1)
            return f"ganztägig {erster.isoformat()}" + (f" bis {letzter.isoformat()}" if letzter > erster else "")
        anfang = zeit.lokalisiere(datetime.fromisoformat(neu["start"]))
        ende = zeit.lokalisiere(datetime.fromisoformat(neu["ende"]))
        return f"{zeit.kurz(anfang)} bis {zeit.kurz(ende)}"

    # ── Ablage: was die Termin-Quelle des Kerns aufruft ──────────────────────────────────────────────
    def _streng(self, fehler: dict[str, str]) -> None:
        """Wirft einen ``QuelleFehler``, wenn ein Konto fehlt (eine unvollständige Liste wäre ein falscher freier Tag)."""
        if not fehler:
            return
        gruende = "; ".join(f"Kalender-Konto {konto!r}: {text}" for konto, text in fehler.items())
        raise kern.QuelleFehler(gruende, 503)

    def ablage_kalender(self) -> list[dict[str, Any]]:
        if not self.eingerichtet:
            raise kern.NichtEingerichtet(HINWEIS_EINRICHTEN)
        fehler = {k.name: f"Konfiguration fehlerhaft: {k.fehler}" for k in self._unbrauchbar}
        zeilen: list[dict[str, Any]] = []
        for quelle, liste, f in self._kalender_je_konto():
            if f is not None:
                fehler.setdefault(quelle.konto.name, f)
            else:
                zeilen += [k.als_dict() for k in liste or []]
        self._streng(fehler)
        return zeilen

    def ablage_termine(self, von: datetime, bis: datetime, kalender: str | None = None) -> list[dict[str, Any]]:
        if not self.eingerichtet:
            raise kern.NichtEingerichtet(HINWEIS_EINRICHTEN)
        a, b = eingabe.fenster(von, bis)
        ergebnis = self._abfragen(a, b, eingabe.kalender_filter(kalender))
        self._streng(ergebnis.fehler)
        return ergebnis.termine

    def ablage_anlegen(self, termin: dict[str, Any]) -> dict[str, Any]:
        if not self.eingerichtet:
            raise kern.NichtEingerichtet(HINWEIS_EINRICHTEN)
        neu = eingabe.neu_pruefen(termin)
        try:
            quelle, kalender = self._ziel(neu["kalender"])
            return self._schreiben(quelle, kalender, neu)
        except NetzFehler as e:
            raise kern.QuelleFehler(e.text, 503 if e.art in ("zeit", "verbindung", "tls") else 502) from None
