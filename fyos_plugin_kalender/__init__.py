"""fyos_plugin_kalender: Termine aus CalDAV-Konten und ICS-Abonnements für Leitstand und Agent.

Das Paket meldet sich über den Entry-Point ``mcp_hub.plugins`` beim Hub an (``plugin.py``) und über ``fyos.skills`` mit
seinem Skill. Zeit, iCalendar, Netz und die beiden Kontoarten stehen je in einem eigenen Modul; ``plugin.py`` fügt
sie zu den Werkzeugen ``kalender.konten``, ``kalender.termine`` und ``kalender.anlegen`` und zur Termin-Ablage zusammen.
"""

from __future__ import annotations

__version__ = "0.1.0"
