"""iCalendar lesen, Wiederholungen auflösen, Termine schreiben."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest
from fakes import ics, lokal, tag, vevent
from fakes import utc as u

from fyos_plugin_kalender import eingabe, ical, zeit

B = "Europe/Berlin"


def tage(von: str, bis: str) -> tuple[datetime, datetime]:
    """Fenster von Mitternacht (Zeit des Hubs) bis Mitternacht."""
    return zeit.tagesanfang(date.fromisoformat(von)), zeit.tagesanfang(date.fromisoformat(bis))


def lies(text: str, von: str = "2026-10-01", bis: str = "2026-11-03") -> list[tuple[str, str, str]]:
    """(Titel, Beginn, Ende) aller Termine im Fenster, nach Beginn."""
    a, b = tage(von, bis)
    datei = ical.lies(text)
    instanzen = ical.instanzen(datei.ereignisse, a, b)
    instanzen.sort(key=lambda i: (zeit.spanne(i.start, i.ende)[0], i.titel))
    return [
        (i.titel, i.start.isoformat(), i.ende.isoformat()) if i.ganztaegig else (i.titel, zeit.iso(i.start), zeit.iso(i.ende))
        for i in instanzen
    ]


# ── einzelne Termine ─────────────────────────────────────────────────────────────────────────────────────
def test_termin_in_utc():
    text = ics(vevent("a1", "Treffen", u("20261012T070000"), u("20261012T080000"), ort="Café"))
    assert lies(text) == [("Treffen", "2026-10-12T09:00:00+02:00", "2026-10-12T10:00:00+02:00")]
    (e,) = ical.lies(text).ereignisse
    assert e.ort == "Café" and e.uid == "a1" and not e.ganztaegig


def test_termin_mit_tzid_in_anderer_zone():
    text = ics(vevent("a1", "Anruf", lokal("America/New_York", "20261012T090000"), lokal("America/New_York", "20261012T100000")))
    assert lies(text) == [("Anruf", "2026-10-12T15:00:00+02:00", "2026-10-12T16:00:00+02:00")]


def test_schwebende_zeit_gilt_in_der_zeit_des_hubs_oder_der_standardzone_der_datei():
    text = ics(vevent("a1", "Frei", ":20261012T090000", ":20261012T100000"))
    assert lies(text)[0][1] == "2026-10-12T09:00:00+02:00"
    mit_standard = text.replace("PRODID", "X-WR-TIMEZONE:Asia/Tokyo\r\nPRODID")
    assert lies(mit_standard)[0][1] == "2026-10-12T02:00:00+02:00"  # 09:00 in Tokio


def test_ganztaegig_ohne_ende_ist_ein_tag_und_das_ende_ist_ausgeschlossen():
    (t,) = lies(ics(vevent("g1", "Urlaub", tag("20261012"))))
    assert t == ("Urlaub", "2026-10-12", "2026-10-13")


def test_ganztaegig_mehrere_tage():
    text = ics(vevent("g1", "Reise", tag("20261026"), tag("20261029")))
    assert lies(text) == [("Reise", "2026-10-26", "2026-10-29")]
    assert lies(text, "2026-10-28", "2026-10-29") == [("Reise", "2026-10-26", "2026-10-29")]
    assert lies(text, "2026-10-29", "2026-10-30") == []  # das Ende ist ausgeschlossen
    assert lies(text, "2026-10-25", "2026-10-26") == []


def test_dauer_statt_ende_und_termin_ohne_ende():
    text = ics(vevent("d1", "Lang", u("20261012T070000"), extra="DURATION:PT90M"))
    assert lies(text) == [("Lang", "2026-10-12T09:00:00+02:00", "2026-10-12T10:30:00+02:00")]
    text = ics(vevent("d2", "Moment", u("20261012T070000")))
    assert lies(text) == [("Moment", "2026-10-12T09:00:00+02:00", "2026-10-12T09:00:00+02:00")]


def test_ganztaegig_mit_dauer_in_tagen():
    text = ics(vevent("g2", "Woche", tag("20261012"), extra="DURATION:P1W"))
    assert lies(text) == [("Woche", "2026-10-12", "2026-10-19")]


def test_fenster_beruehrung():
    text = ics(vevent("a1", "Treffen", u("20261012T070000"), u("20261012T080000")))  # 09:00-10:00 Ortszeit
    def im(von: str, bis: str) -> int:
        a = datetime.fromisoformat(von).replace(tzinfo=zeit.hub_zone())
        b = datetime.fromisoformat(bis).replace(tzinfo=zeit.hub_zone())
        return len(ical.instanzen(ical.lies(text).ereignisse, a, b))
    assert im("2026-10-12T10:00", "2026-10-12T11:00") == 0  # beginnt genau, wo der Termin endet
    assert im("2026-10-12T09:59", "2026-10-12T11:00") == 1
    assert im("2026-10-12T08:00", "2026-10-12T09:00") == 0
    assert im("2026-10-12T08:00", "2026-10-12T09:01") == 1


def test_text_wird_entmaskiert_und_gefaltet_gelesen():
    roh = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:t1\r\nDTSTART:20261012T070000Z\r\n"
        "SUMMARY:Teamrunde\\, woechentlich\\; mit Semikolon\\nzweite Zeile und\r\n  eine lange Fortsetzung\r\n"
        "LOCATION:Raum 1 \r\n\tzweiter Stock\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"  # Tab und Leerzeichen leiten die Fortsetzung ein
    )
    (e,) = ical.lies(roh).ereignisse
    assert e.titel == "Teamrunde, woechentlich; mit Semikolon zweite Zeile und eine lange Fortsetzung"
    assert e.ort == "Raum 1 zweiter Stock"


@pytest.mark.parametrize("ende", ["\r\n", "\n", "\r"])
def test_zeilenenden_und_bom(ende):
    roh = ende.join(["﻿BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:t1", "DTSTART:20261012T070000Z", "SUMMARY:Ein Termin", "END:VEVENT", "END:VCALENDAR", ""])
    assert [e.titel for e in ical.lies(roh).ereignisse] == ["Ein Termin"]


def test_kalendername_wird_gelesen():
    assert ical.lies(ics(vevent("a", "x", u("20261012T070000")), name="Feiertage")).name == "Feiertage"


def test_alarme_und_teilnehmer_aendern_den_titel_nicht():
    roh = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:t1\r\nDTSTART:20261012T070000Z\r\nSUMMARY:Echter Titel\r\n"
        "ATTENDEE;CN=Jemand:mailto:jemand@example.com\r\nDESCRIPTION:geheim\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nSUMMARY:Alarmtext\r\nDESCRIPTION:Alarm\r\nDTSTART:20000101T000000Z\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    (e,) = ical.lies(roh).ereignisse
    assert e.titel == "Echter Titel" and e.start == datetime(2026, 10, 12, 7, tzinfo=timezone.utc)
    assert not hasattr(e, "beschreibung") and not hasattr(e, "teilnehmer")


def test_abgesagte_termine_fehlen_vorlaeufige_nicht():
    text = ics(
        vevent("a", "Abgesagt", u("20261012T070000"), extra="STATUS:CANCELLED"),
        vevent("b", "Vorläufig", u("20261013T070000"), extra="STATUS:TENTATIVE"),
    )
    assert [t[0] for t in lies(text)] == ["Vorläufig"]


def test_unlesbares_wird_gezaehlt_und_uebersprungen():
    text = ics(
        vevent("a", "Gut", u("20261012T070000")),
        "BEGIN:VEVENT\r\nUID:b\r\nSUMMARY:Ohne Beginn\r\nEND:VEVENT\r\n",
        "BEGIN:VEVENT\r\nUID:c\r\nSUMMARY:Kaputt\r\nDTSTART:morgen\r\nEND:VEVENT\r\n",
    )
    datei = ical.lies(text)
    assert [e.titel for e in datei.ereignisse] == ["Gut"] and datei.uebersprungen == 2


def test_termin_ohne_titel_und_ohne_uid():
    (e,) = ical.lies("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20261012T070000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").ereignisse
    assert e.titel == "" and e.uid.startswith("x-")


def test_aufgaben_und_freibusy_sind_keine_termine():
    roh = ("BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:t\r\nSUMMARY:Aufgabe\r\nDTSTART:20261012T070000Z\r\nEND:VTODO\r\n"
           "BEGIN:VFREEBUSY\r\nUID:f\r\nDTSTART:20261012T070000Z\r\nEND:VFREEBUSY\r\nEND:VCALENDAR\r\n")
    assert ical.lies(roh).ereignisse == []


# ── Zeitzonen ────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "tzid,erwartet",
    [
        ("W. Europe Standard Time", "2026-10-12T09:00:00+02:00"),
        ("Romance Standard Time", "2026-10-12T09:00:00+02:00"),
        ("/softwarestudio.org/Olson_20011030_5/Europe/Berlin", "2026-10-12T09:00:00+02:00"),
        ("GMT Standard Time", "2026-10-12T10:00:00+02:00"),
        ("Pacific Standard Time", "2026-10-12T18:00:00+02:00"),
        ("Europe/Kiev", "2026-10-12T08:00:00+02:00"),  # alter Name von Europe/Kyiv
    ],
)
def test_zeitzonennamen_von_outlook_und_aelteren_programmen(tzid, erwartet):
    text = ics(vevent("z", "Zone", lokal(tzid, "20261012T090000")))
    assert lies(text)[0][1] == erwartet


def test_eigener_zonenname_aus_der_eingebetteten_vtimezone_auch_mit_x_lic_location():
    zonen = ("BEGIN:VTIMEZONE\r\nTZID:Customized Time Zone\r\nX-LIC-LOCATION:Europe/Berlin\r\n"
             "BEGIN:STANDARD\r\nDTSTART:19701025T030000\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nTZNAME:CET\r\n"
             "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\nEND:STANDARD\r\n"
             "BEGIN:DAYLIGHT\r\nDTSTART:19700329T020000\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\nTZNAME:CEST\r\n"
             "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n")
    text = ics(vevent("z", "Eigene", lokal("Customized Time Zone", "20261012T090000")),
               vevent("y", "Winter", lokal("Customized Time Zone", "20261101T090000")), zonen=zonen)
    assert lies(text) == [("Eigene", "2026-10-12T09:00:00+02:00", "2026-10-12T09:00:00+02:00"),
                          ("Winter", "2026-11-01T09:00:00+01:00", "2026-11-01T09:00:00+01:00")]


def test_unbekannte_zone_faellt_auf_die_zeit_des_hubs_zurueck_und_der_termin_bleibt(caplog):
    text = ics(vevent("z", "Irgendwo", lokal("Phantasie/Land", "20261012T090000")))
    with caplog.at_level("WARNING", logger="fyos_plugin_kalender"):
        assert lies(text) == [("Irgendwo", "2026-10-12T09:00:00+02:00", "2026-10-12T09:00:00+02:00")]
    assert any("Phantasie" in r.getMessage() for r in caplog.records)


# ── Wiederholungen ───────────────────────────────────────────────────────────────────────────────────────
def serie(extra: str = "", uid: str = "serie", titel: str = "Teamrunde", start: str = "20261005T090000", ende: str = "20261005T100000"):
    return vevent(uid, titel, lokal(B, start), lokal(B, ende), extra=extra)


def test_woechentlich_mit_ausnahme_verschiebung_und_zeitumstellung():
    text = ics(
        serie("RRULE:FREQ=WEEKLY;BYDAY=MO\nEXDATE;TZID=Europe/Berlin:20261012T090000"),
        vevent("serie", "Teamrunde (verschoben)", lokal(B, "20261020T140000"), lokal(B, "20261020T150000"),
               extra=f"RECURRENCE-ID;TZID={B}:20261019T090000"),
    )
    assert lies(text) == [
        ("Teamrunde", "2026-10-05T09:00:00+02:00", "2026-10-05T10:00:00+02:00"),
        ("Teamrunde (verschoben)", "2026-10-20T14:00:00+02:00", "2026-10-20T15:00:00+02:00"),
        ("Teamrunde", "2026-10-26T09:00:00+01:00", "2026-10-26T10:00:00+01:00"),  # nach der Zeitumstellung 9 Uhr Ortszeit
        ("Teamrunde", "2026-11-02T09:00:00+01:00", "2026-11-02T10:00:00+01:00"),
    ]


def test_verschobener_termin_erscheint_dort_wo_er_jetzt_liegt():
    text = ics(
        serie("RRULE:FREQ=WEEKLY;BYDAY=MO"),
        vevent("serie", "Teamrunde (verschoben)", lokal(B, "20261022T140000"), lokal(B, "20261022T150000"),
               extra=f"RECURRENCE-ID;TZID={B}:20261019T090000"),
    )
    assert [t[0] for t in lies(text, "2026-10-19", "2026-10-20")] == []  # an der alten Stelle nichts mehr
    assert [t[0] for t in lies(text, "2026-10-22", "2026-10-23")] == ["Teamrunde (verschoben)"]
    assert [t[0] for t in lies(text, "2026-10-26", "2026-10-27")] == ["Teamrunde"]


def test_abgesagte_instanz_einer_serie():
    text = ics(
        serie("RRULE:FREQ=WEEKLY;BYDAY=MO"),
        vevent("serie", "Teamrunde", lokal(B, "20261019T090000"), lokal(B, "20261019T100000"),
               extra=f"RECURRENCE-ID;TZID={B}:20261019T090000\nSTATUS:CANCELLED"),
    )
    tage_ = [t[1][:10] for t in lies(text)]
    assert tage_ == ["2026-10-05", "2026-10-12", "2026-10-26", "2026-11-02"]


def test_taeglich_ueber_die_zeitumstellung_bleibt_zur_selben_ortszeit():
    text = ics(serie("RRULE:FREQ=DAILY;COUNT=4", start="20261024T090000", ende="20261024T100000"))
    assert [t[1] for t in lies(text)] == [
        "2026-10-24T09:00:00+02:00", "2026-10-25T09:00:00+01:00", "2026-10-26T09:00:00+01:00", "2026-10-27T09:00:00+01:00"]


def test_serie_in_utc_bleibt_zur_selben_utc_zeit():
    text = ics(vevent("u", "UTC-Serie", u("20261019T070000"), u("20261019T080000"), extra="RRULE:FREQ=WEEKLY"))
    assert [t[1] for t in lies(text)] == [
        "2026-10-19T09:00:00+02:00", "2026-10-26T08:00:00+01:00", "2026-11-02T08:00:00+01:00"]


def test_count_zaehlt_auch_herausgenommene_termine():
    text = ics(serie("RRULE:FREQ=WEEKLY;COUNT=3\nEXDATE;TZID=Europe/Berlin:20261012T090000"))
    assert [t[1][:10] for t in lies(text, "2026-10-01", "2026-12-31")] == ["2026-10-05", "2026-10-19"]


def test_exdate_mit_mehreren_werten_und_als_utc():
    text = ics(serie("RRULE:FREQ=WEEKLY\nEXDATE;TZID=Europe/Berlin:20261012T090000,20261019T090000\nEXDATE:20261025T080000Z"))
    assert [t[1][:10] for t in lies(text)] == ["2026-10-05", "2026-10-26", "2026-11-02"]


def test_until_in_utc_ist_eingeschlossen():
    text = ics(serie("RRULE:FREQ=WEEKLY;UNTIL=20261019T070000Z"))  # genau der Beginn am 19.10. (09:00 Ortszeit)
    assert [t[1][:10] for t in lies(text)] == ["2026-10-05", "2026-10-12", "2026-10-19"]


def test_until_als_datum_bei_termin_mit_uhrzeit_gilt_bis_zum_ende_des_tages():
    text = ics(serie("RRULE:FREQ=WEEKLY;UNTIL=20261019"))
    assert [t[1][:10] for t in lies(text)] == ["2026-10-05", "2026-10-12", "2026-10-19"]


def test_until_bei_ganztaegigen_terminen_in_beiden_schreibweisen():
    for until in ("20261019", "20261019T000000Z", "20261018T220000Z"):
        text = ics(vevent("g", "Müll", tag("20261005"), extra=f"RRULE:FREQ=WEEKLY;UNTIL={until}"))
        assert [t[1] for t in lies(text)] == ["2026-10-05", "2026-10-12", "2026-10-19"], until


def test_monatlich_am_letzten_freitag():
    text = ics(serie("RRULE:FREQ=MONTHLY;BYDAY=-1FR", start="20260925T090000", ende="20260925T100000"))
    assert [t[1][:10] for t in lies(text, "2026-10-01", "2026-12-01")] == ["2026-10-30", "2026-11-27"]


def test_monatlich_am_31_ueberspringt_kurze_monate():
    text = ics(serie("RRULE:FREQ=MONTHLY;BYMONTHDAY=31", start="20260831T090000", ende="20260831T100000"))
    assert [t[1][:10] for t in lies(text, "2026-08-01", "2027-01-01")] == ["2026-08-31", "2026-10-31", "2026-12-31"]


def test_jaehrlicher_geburtstag_ganztaegig_aus_ferner_vergangenheit():
    text = ics(vevent("geb", "Geburtstag", tag("19900315"), extra="RRULE:FREQ=YEARLY"))
    assert lies(text, "2027-03-01", "2027-04-01") == [("Geburtstag", "2027-03-15", "2027-03-16")]
    assert lies(text, "2027-04-01", "2027-05-01") == []


def test_schalttag_nur_in_schaltjahren():
    text = ics(vevent("sch", "Schalttag", tag("20240229"), extra="RRULE:FREQ=YEARLY"))
    assert lies(text, "2027-02-01", "2027-04-01") == []
    assert lies(text, "2028-02-01", "2028-04-01") == [("Schalttag", "2028-02-29", "2028-03-01")]


def test_endlose_serie_weit_in_der_zukunft_ist_schnell():
    text = ics(serie("RRULE:FREQ=WEEKLY", start="20200106T090000", ende="20200106T100000"))
    start = time.perf_counter()
    gefunden = lies(text, "2030-01-01", "2031-01-01")
    assert 52 <= len(gefunden) <= 53 and time.perf_counter() - start < 3


def test_rdate_ergaenzt_und_period_traegt_eigenes_ende():
    text = ics(serie(f"RDATE;TZID={B}:20261014T180000", start="20261012T090000", ende="20261012T100000"))
    assert lies(text) == [
        ("Teamrunde", "2026-10-12T09:00:00+02:00", "2026-10-12T10:00:00+02:00"),
        ("Teamrunde", "2026-10-14T18:00:00+02:00", "2026-10-14T19:00:00+02:00"),
    ]
    text = ics(serie(f"RDATE;VALUE=PERIOD;TZID={B}:20261014T180000/20261014T203000", start="20261012T090000", ende="20261012T100000"))
    assert lies(text)[1] == ("Teamrunde", "2026-10-14T18:00:00+02:00", "2026-10-14T20:30:00+02:00")


def test_vom_server_aufgeloeste_instanzen_mit_recurrence_id_ohne_regel():
    text = ics(*(
        vevent("srv", "Aufgelöst", lokal(B, f"202610{d}T090000"), lokal(B, f"202610{d}T100000"), extra=f"RECURRENCE-ID;TZID={B}:202610{d}T090000")
        for d in ("05", "12", "19")
    ))
    datei = ical.lies(text)
    a, b = tage("2026-10-01", "2026-11-01")
    instanzen = ical.instanzen(datei.ereignisse, a, b)
    assert sorted(i.start.day for i in instanzen) == [5, 12, 19]
    assert len({i.kennung for i in instanzen}) == 3 and all(i.kennung for i in instanzen)


def test_aufgeloeste_instanzen_ohne_recurrence_id_bekommen_verschiedene_kennungen():
    text = ics(*(vevent("srv", "Doppelt", lokal(B, f"202610{d}T090000"), lokal(B, f"202610{d}T100000")) for d in ("05", "12")))
    a, b = tage("2026-10-01", "2026-11-01")
    instanzen = ical.instanzen(ical.lies(text).ereignisse, a, b)
    assert len(instanzen) == 2 and len({i.kennung for i in instanzen}) == 2


def test_nicht_unterstuetzte_regeln_zeigen_nur_den_ersten_termin(caplog):
    for regel in (
        "FREQ=SECONDLY",
        "FREQ=MINUTELY;INTERVAL=5",
        "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30",  # gibt es nie
        "FREQ=DAILY;BYMONTHDAY=40",
        "FREQ=MONTHLY;BYMONTH=13",
        "FREQ=DAILY;BYWEEKNO=3",
        "FREQ=WEEKLY;INTERVAL=0",
        "FREQ=DAILY;COUNT=abc",
        "BYDAY=MO",
        "FREQ=DAILY;BYDAY=XX",
    ):
        with caplog.at_level("WARNING", logger="fyos_plugin_kalender"):
            start = time.perf_counter()
            gefunden = lies(ics(serie(f"RRULE:{regel}")), "2026-10-01", "2026-12-01")
        assert [t[1][:10] for t in gefunden] == ["2026-10-05"], regel
        assert time.perf_counter() - start < 2, regel


def test_stuendlich_seit_ueber_einem_jahrzehnt_wird_abgelehnt_aber_juengere_laufen():
    text = ics(serie("RRULE:FREQ=HOURLY;INTERVAL=6", start="20150105T090000", ende="20150105T091500"))
    assert len(lies(text, "2026-10-12", "2026-10-13")) == 4
    text = ics(serie("RRULE:FREQ=HOURLY", start="20120105T090000", ende="20120105T091500"))
    assert len(lies(text, "2026-10-12", "2026-10-13")) == 24
    text = ics(serie("RRULE:FREQ=HOURLY", start="20050105T090000", ende="20050105T091500"))
    assert lies(text, "2026-10-12", "2026-10-13") == []  # über 17 Jahre: die Regel fällt weg, der erste Termin liegt nicht im Fenster


def test_sehr_viele_instanzen_werden_gekappt():
    text = ics(serie("RRULE:FREQ=HOURLY", start="20261001T000000", ende="20261001T000500"))
    assert len(lies(text, "2026-10-01", "2026-12-31")) == ical.INSTANZEN_MAX


def test_viele_ereignisse_werden_gekappt_und_gezaehlt(monkeypatch):
    monkeypatch.setattr(ical, "EREIGNISSE_MAX", 3)
    text = ics(*(vevent(f"e{i}", f"T{i}", u("20261012T070000")) for i in range(5)))
    datei = ical.lies(text)
    assert len(datei.ereignisse) == 3 and datei.uebersprungen == 2


def test_ausnahmen_ohne_meister_erscheinen_trotzdem():
    text = ics(vevent("allein", "Nur die Ausnahme", lokal(B, "20261015T090000"), lokal(B, "20261015T100000"), extra=f"RECURRENCE-ID;TZID={B}:20261012T090000"))
    assert [t[0] for t in lies(text)] == ["Nur die Ausnahme"]


def test_ganztaegige_serie_mit_herausgenommenem_tag_und_verschobener_instanz():
    text = ics(
        vevent("g", "Müllabfuhr", tag("20261005"), extra="RRULE:FREQ=WEEKLY\nEXDATE;VALUE=DATE:20261012"),
        vevent("g", "Müllabfuhr (später)", tag("20261021"), extra="RECURRENCE-ID;VALUE=DATE:20261019"),
    )
    assert [(t[0], t[1]) for t in lies(text)] == [
        ("Müllabfuhr", "2026-10-05"), ("Müllabfuhr (später)", "2026-10-21"), ("Müllabfuhr", "2026-10-26"), ("Müllabfuhr", "2026-11-02")]


# ── Schreiben ────────────────────────────────────────────────────────────────────────────────────────────
JETZT = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def neu(**felder):
    return eingabe.typisiert(eingabe.neu_pruefen({"titel": "Zahnarzt", "start": "2026-10-15T09:00", **felder}))


def test_ics_zum_anlegen_mit_uhrzeit_in_utc():
    text = ical.baue_ics("abc@fyos-kalender", neu(ort="Praxis, 2. Stock"), JETZT)
    assert text.endswith("END:VCALENDAR\r\n") and "\n" not in text.replace("\r\n", "")
    zeilen = text.split("\r\n")
    assert "UID:abc@fyos-kalender" in zeilen and "DTSTAMP:20261010T120000Z" in zeilen
    assert "DTSTART:20261015T070000Z" in zeilen and "DTEND:20261015T080000Z" in zeilen  # 09:00 Ortszeit, eine Stunde
    assert "SUMMARY:Zahnarzt" in zeilen and "LOCATION:Praxis\\, 2. Stock" in zeilen
    assert not any(z.startswith("DESCRIPTION") for z in zeilen)
    (e,) = ical.lies(text).ereignisse
    assert (e.titel, e.ort, e.start) == ("Zahnarzt", "Praxis, 2. Stock", datetime(2026, 10, 15, 7, tzinfo=timezone.utc))


def test_ics_zum_anlegen_ganztaegig_und_mit_beschreibung():
    text = ical.baue_ics("g@fyos-kalender", neu(start="2026-12-24", ende="2026-12-27", beschreibung="Zeile 1\nZeile 2; mit, Zeichen"), JETZT)
    zeilen = text.split("\r\n")
    assert "DTSTART;VALUE=DATE:20261224" in zeilen and "DTEND;VALUE=DATE:20261227" in zeilen
    assert "DESCRIPTION:Zeile 1\\nZeile 2\\; mit\\, Zeichen" in zeilen
    (e,) = ical.lies(text).ereignisse
    assert e.ganztaegig and e.start == date(2026, 12, 24) and e.ende == date(2026, 12, 27)


def test_ics_zum_anlegen_faltet_nach_75_oktetten_ohne_ein_zeichen_zu_teilen():
    titel = "Überprüfung für größere Änderungen " * 6
    text = ical.baue_ics("l@fyos-kalender", neu(titel=titel[:200], ort="ü" * 150), JETZT)
    for zeile in text.split("\r\n"):
        assert len(zeile.encode("utf-8")) <= 75, zeile
        zeile.encode("utf-8").decode("utf-8")
    (e,) = ical.lies(text).ereignisse
    assert e.titel == " ".join(titel[:200].split()) and e.ort == "ü" * 150


@pytest.mark.parametrize(
    "titel",
    [
        "Hallo\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Fremd",
        "Hallo\nATTENDEE:mailto:fremd@example.com",
        "Hallo\rLOCATION:Woanders",
        "x\x00y\x07z\x1b[2J",
        "\\; , \\n",
    ],
)
def test_ics_zum_anlegen_laesst_keine_zeilen_einschleusen(titel):
    # direkt an baue_ics (ohne die Prüfung), damit die Maskierung für sich steht
    roh = {"titel": titel, "start": datetime(2026, 10, 15, 7, tzinfo=timezone.utc), "ende": datetime(2026, 10, 15, 8, tzinfo=timezone.utc),
           "ganztaegig": False, "ort": titel, "beschreibung": titel}
    text = ical.baue_ics("i@fyos-kalender", roh, JETZT)
    zeilen = text.split("\r\n")
    assert zeilen.count("BEGIN:VEVENT") == 1 and zeilen.count("END:VEVENT") == 1  # ganze Zeilen, nicht Teile maskierten Textes
    assert not any(z.startswith(("ATTENDEE", "LOCATION:Woanders", "SUMMARY:Fremd")) for z in zeilen)
    assert not any(ord(c) < 32 and c not in "\r\n" for c in text)
    datei = ical.lies(text)
    assert len(datei.ereignisse) == 1
