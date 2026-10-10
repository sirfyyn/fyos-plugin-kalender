"""Die Verbindung zur Termin-Quelle des Kerns (``mcp_hub.termine``, ab fyos-hub 0.1.1).

Das Plugin meldet sich dort als Ablage an, damit Leitstand, Tagesbrief und Startseite seine Termine lesen, ohne CalDAV
zu können oder eine Adresse zu kennen. Fehlt das Modul (ein älterer Kern), läuft das Plugin trotzdem: die Werkzeuge
``kalender.*`` funktionieren, nur die Anmeldung als Quelle entfällt.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

try:
    from mcp_hub import termine as _termine
except ImportError:  # fyos-hub 0.1.0 und älter
    _termine = None

if _termine is not None:
    QuelleFehler = _termine.QuelleFehler
    NichtEingerichtet = _termine.NichtEingerichtet
else:

    class QuelleFehler(RuntimeError):  # type: ignore[no-redef]
        """Die Quelle antwortet nicht wie vereinbart (Ersatz für ``mcp_hub.termine.QuelleFehler``)."""

        def __init__(self, text: str, status: int = 502) -> None:
            super().__init__(text)
            self.status = status

    class NichtEingerichtet(QuelleFehler):  # type: ignore[no-redef]
        """Es gibt keine Quelle für Termine (Ersatz für ``mcp_hub.termine.NichtEingerichtet``)."""

        def __init__(self, text: str = "") -> None:
            super().__init__(text or "Kein Kalender eingerichtet.", 501)


def modul() -> ModuleType | None:
    """Das Modul ``mcp_hub.termine`` oder ``None``, wenn der Kern es nicht hat."""
    return _termine


def verfuegbar() -> bool:
    return _termine is not None


def anmelden(ablage: Any) -> bool:
    """Meldet ``ablage`` als Quelle der Termine an. ``False``, wenn der Kern keine Termin-Quelle hat."""
    if _termine is None:
        return False
    _termine.ablage_anmelden(ablage)
    return True


def abmelden(ablage: Any) -> None:
    """Nimmt ``ablage`` zurück, aber nur, wenn sie noch die angemeldete ist (eine andere bleibt)."""
    if _termine is not None and _termine.angemeldete_ablage() is ablage:
        _termine.ablage_anmelden(None)
