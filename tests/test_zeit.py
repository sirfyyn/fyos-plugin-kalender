"""Zeit: Zeitzone des Hubs, Zeitzonen aus Kalenderdateien, Zeitangaben, Fenster."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from fyos_plugin_kalender import zeit
from fyos_plugin_kalender.zonen import ALIASE, WINDOWS_ZONEN, WINDOWS_ZONEN_KLEIN


def utc(*teile: int) -> datetime:
    return datetime(*teile, tzinfo=timezone.utc)


# ── Zeitzone des Hubs ────────────────────────────────────────────────────────────────────────────────────
def test_hub_zone_aus_fyos_zeitzone(monkeypatch):
    assert str(zeit.hub_zone()) == "Europe/Berlin"
    monkeypatch.setenv("FYOS_ZEITZONE", "UTC")
    assert zeit.hub_zone() is timezone.utc
    monkeypatch.setenv("FYOS_ZEITZONE", "America/New_York")
    assert zeit.iso(utc(2026, 10, 12, 12)) == "2026-10-12T08:00:00-04:00"


def test_unbekannte_zeitzone_faellt_auf_die_des_rechners_zurueck_und_warnt_einmal(monkeypatch, caplog):
    monkeypatch.setenv("FYOS_ZEITZONE", "Mars/Olympus")
    with caplog.at_level("WARNING", logger="fyos_plugin_kalender"):
        zone = zeit.hub_zone()
        zeit.hub_zone()
    assert zone.utcoffset(datetime(2026, 10, 12)) is not None
    assert len([r for r in caplog.records if "Mars/Olympus" in r.getMessage()]) == 1


def test_lokalisiere_und_tagesanfang():
    assert zeit.lokalisiere(datetime(2026, 10, 12, 9, 0)).isoformat() == "2026-10-12T09:00:00+02:00"
    assert zeit.lokalisiere(utc(2026, 10, 12, 9)).isoformat() == "2026-10-12T09:00:00+00:00"  # Versatz bleibt
    assert zeit.tagesanfang(date(2026, 10, 25)).isoformat() == "2026-10-25T00:00:00+02:00"
    assert zeit.tagesanfang(date(2026, 10, 26)).isoformat() == "2026-10-26T00:00:00+01:00"  # nach der Zeitumstellung


def test_heute_folgt_der_zeitzone_des_hubs():
    # 23:30 UTC am 12.10. ist in Berlin schon der 13.10.
    assert zeit.heute(utc(2026, 10, 12, 23, 30)) == date(2026, 10, 13)
    assert zeit.heute(utc(2026, 10, 12, 21, 59)) == date(2026, 10, 12)


def test_ausgabeformate():
    z = utc(2026, 10, 12, 7, 0, 5)
    assert zeit.iso(z) == "2026-10-12T09:00:05+02:00"
    assert zeit.kurz(z) == "2026-10-12 09:00"
    assert zeit.kurz(date(2026, 10, 12)) == "2026-10-12"
    assert zeit.utc_text(datetime(2026, 10, 12, 9, 0, tzinfo=zeit.hub_zone())) == "20261012T070000Z"


# ── Zeitzonen finden ─────────────────────────────────────────────────────────────────────────────────────
def test_alle_iana_ziele_der_windows_tabelle_gibt_es():
    fehlend = sorted({ziel for ziel in WINDOWS_ZONEN.values() if zeit.iana(ziel) is None})
    assert fehlend == []


def test_windows_namen_werden_ohne_beachtung_der_schreibweise_gefunden():
    assert len(WINDOWS_ZONEN_KLEIN) >= len(WINDOWS_ZONEN) - 5
    z = zeit.Zonen()
    sommer = datetime(2026, 7, 1, 12)
    erwartet = {
        "W. Europe Standard Time": timedelta(hours=2),
        "w. europe standard time": timedelta(hours=2),
        "Romance Standard Time": timedelta(hours=2),
        "GMT Standard Time": timedelta(hours=1),
        "Pacific Standard Time": timedelta(hours=-7),
        "Eastern Standard Time": timedelta(hours=-4),
        "India Standard Time": timedelta(hours=5, minutes=30),
        "Tokyo Standard Time": timedelta(hours=9),
        "UTC": timedelta(0),
    }
    for name, versatz in erwartet.items():
        zone = z.finde(name)
        assert zone is not None, name
        assert sommer.replace(tzinfo=zone).utcoffset() == versatz, name


def test_iana_mit_vorsatz_aelterer_programme_und_alte_namen():
    assert str(zeit.iana("/softwarestudio.org/Olson_20011030_5/Europe/Berlin")) == "Europe/Berlin"
    assert str(zeit.iana("/mozilla.org/20050126_1/America/New_York")) == "America/New_York"
    for alt, neu in ALIASE.items():  # beide Schreibweisen führen zu einer Zone
        assert zeit.iana(alt) is not None and zeit.iana(neu) is not None


@pytest.mark.parametrize("name", ["", "   ", "Nirgendwo/Stadt", "../../etc/passwd", "Europe/Berlin\nX", "a" * 300, "GMT+1"])
def test_unbekannte_oder_gefaehrliche_namen_sind_keine_zone(name):
    assert zeit.iana(name) is None


def test_zone_ohne_tzid_ist_die_standardzone_und_eine_unbekannte_faellt_darauf_zurueck(caplog):
    berlin = zeit.iana("Europe/Berlin")
    tokio = zeit.iana("Asia/Tokyo")
    z = zeit.Zonen(standard=tokio)
    assert z.zone(None) is tokio and z.zone("") is tokio
    with caplog.at_level("WARNING", logger="fyos_plugin_kalender"):
        assert z.zone("Phantasie Standard Time") is tokio
        assert z.zone("Phantasie Standard Time") is tokio
    assert len([r for r in caplog.records if "Phantasie" in r.getMessage()]) == 1  # einmal im Log
    assert zeit.Zonen().zone("Phantasie Standard Time") is None and berlin is not None


EINGEBETTET = """BEGIN:VTIMEZONE
TZID:Eigene Zone
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE"""


def test_eingebettete_vtimezone_wird_gelesen():
    z = zeit.Zonen(EINGEBETTET)
    zone = z.finde("Eigene Zone")
    assert zone is not None
    assert datetime(2026, 7, 1, 12, tzinfo=zone).utcoffset() == timedelta(hours=2)
    assert datetime(2026, 12, 1, 12, tzinfo=zone).utcoffset() == timedelta(hours=1)
    assert z.finde("Gibt es nicht") is None


# ── Zeitangaben lesen ────────────────────────────────────────────────────────────────────────────────────
def test_lies_zeit_formen():
    z = zeit.Zonen()
    assert zeit.lies_zeit("20261012T090000Z", {}, z) == utc(2026, 10, 12, 9)
    assert zeit.lies_zeit("20261012T090000", {"TZID": "Europe/Berlin"}, z).isoformat() == "2026-10-12T09:00:00+02:00"
    assert zeit.lies_zeit("20261012T090000", {}, z).isoformat() == "2026-10-12T09:00:00+02:00"  # schwebend: Zeit des Hubs
    assert zeit.lies_zeit("20261012", {"VALUE": "DATE"}, z) == date(2026, 10, 12)
    assert zeit.lies_zeit("20261012", {}, z) == date(2026, 10, 12)
    assert zeit.lies_zeit("20261012T0900", {}, z).isoformat() == "2026-10-12T09:00:00+02:00"
    assert zeit.lies_zeit("20261012T090060Z", {}, z) == utc(2026, 10, 12, 9, 0, 59)  # Schaltsekunde


@pytest.mark.parametrize("text", ["", "morgen", "2026-10-12", "20261312T090000Z", "20261012T250000Z", "20261012T0900000000"])
def test_lies_zeit_lehnt_unsinn_ab(text):
    with pytest.raises(ValueError):
        zeit.lies_zeit(text, {}, zeit.Zonen())


def test_lies_zeit_zum_dst_sprung():
    """02:30 gibt es am 25.10. zweimal (Ende der Sommerzeit) und am 29.03. nicht: es kommt eine Zeit, kein Fehler."""
    z = zeit.Zonen()
    assert zeit.lies_zeit("20261025T023000", {}, z).utcoffset() in (timedelta(hours=1), timedelta(hours=2))
    assert zeit.lies_zeit("20260329T023000", {}, z).utcoffset() in (timedelta(hours=1), timedelta(hours=2))


@pytest.mark.parametrize(
    "text,erwartet",
    [
        ("PT1H", timedelta(hours=1)),
        ("PT1H30M", timedelta(hours=1, minutes=30)),
        ("P1D", timedelta(days=1)),
        ("P2W", timedelta(weeks=2)),
        ("P1DT2H", timedelta(days=1, hours=2)),
        ("-PT15M", timedelta(minutes=-15)),
        ("PT45S", timedelta(seconds=45)),
    ],
)
def test_lies_dauer(text, erwartet):
    assert zeit.lies_dauer(text) == erwartet


@pytest.mark.parametrize("text", ["", "P", "PT", "1H", "eine Stunde", "P1.5D"])
def test_lies_dauer_lehnt_unsinn_ab(text):
    with pytest.raises(ValueError):
        zeit.lies_dauer(text)


# ── Fenster ──────────────────────────────────────────────────────────────────────────────────────────────
def test_spanne_ganztaegig_und_mit_uhrzeit():
    a, b = zeit.spanne(date(2026, 10, 12), date(2026, 10, 13))
    assert (a.isoformat(), b.isoformat()) == ("2026-10-12T00:00:00+02:00", "2026-10-13T00:00:00+02:00")
    a, b = zeit.spanne(utc(2026, 10, 12, 9), utc(2026, 10, 12, 8))  # Ende vor Beginn: Länge null
    assert a == b


def test_im_fenster_beruehrung_zaehlt():
    von, bis = utc(2026, 10, 12, 8), utc(2026, 10, 12, 12)
    assert zeit.im_fenster(utc(2026, 10, 12, 9), utc(2026, 10, 12, 10), von, bis)
    assert zeit.im_fenster(utc(2026, 10, 12, 7), utc(2026, 10, 12, 9), von, bis)  # ragt hinein
    assert zeit.im_fenster(utc(2026, 10, 12, 11), utc(2026, 10, 12, 13), von, bis)
    assert zeit.im_fenster(utc(2026, 10, 12, 6), utc(2026, 10, 12, 14), von, bis)  # umschließt
    assert not zeit.im_fenster(utc(2026, 10, 12, 7), utc(2026, 10, 12, 8), von, bis)  # endet genau am Beginn
    assert not zeit.im_fenster(utc(2026, 10, 12, 12), utc(2026, 10, 12, 13), von, bis)  # beginnt genau am Ende
    # ohne Dauer nur, wenn der Beginn im Fenster liegt
    assert zeit.im_fenster(von, von, von, bis)
    assert not zeit.im_fenster(bis, bis, von, bis)
