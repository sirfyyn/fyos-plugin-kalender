"""Quellen gegen Attrappen-Server: CalDAV (Entdecken, Termine, Anlegen) und ICS-Abonnement."""

from __future__ import annotations

import gzip
import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fakes import Antwort, FakeCalDav, FakeKalender, Server, Uhr, basic, ics, lokal, res, tag, termin, vevent
from fakes import utc as u

from fyos_plugin_kalender import eingabe, ical, zeit
from fyos_plugin_kalender.konten import Konto
from fyos_plugin_kalender.netz import NetzFehler
from fyos_plugin_kalender.quellen import (
    CalDavQuelle, IcsQuelle, Kalender, KontoFehler, Speicher, auswahl, instanz_dict, quelle_fuer, sortiert, tagesfenster,
    termin_dict, termin_id,
)

B = "Europe/Berlin"
BERLIN = ZoneInfo(B)
PASSWORT = "geheim-app-passwort"


def tag12(stunde: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 12, stunde, minute, tzinfo=BERLIN)


def konto(s: Server | str, art: str = "caldav", name: str = "Nextcloud", nutzer: str = "alice", passwort: str = PASSWORT) -> Konto:
    url = s if isinstance(s, str) else s.url
    return Konto(name=name, art=art, url=url, nutzer=nutzer, passwort=passwort)


# ── Zwischenspeicher, Fenster, Ids ───────────────────────────────────────────────────────────────────────
def test_speicher_haelt_begrenzt_und_vergisst_nach_der_frist():
    uhr = Uhr()
    s = Speicher(60, uhr, maximum=2)
    s.setze("a", [])
    assert s.hole("a") == [] and s.hole("b") is None  # eine leere Liste ist ein Treffer
    s.setze("b", 1)
    s.setze("c", 2)  # "a" fliegt raus
    assert s.hole("a") is None and s.hole("b") == 1 and len(s) == 2
    uhr.weiter(59)
    assert s.hole("b") == 1
    uhr.weiter(2)
    assert s.hole("b") is None and s.hole("c") is None
    s.setze("d", 3)
    s.leere()
    assert s.hole("d") is None


def test_tagesfenster_erweitert_auf_ganze_tage_der_hub_zeit():
    a, b = tagesfenster(tag12(9, 30), tag12(17))
    assert (a.isoformat(), b.isoformat()) == ("2026-10-12T00:00:00+02:00", "2026-10-13T00:00:00+02:00")
    a, b = tagesfenster(tag12(0), tag12(0) + timedelta(days=2))  # genau Mitternacht: nicht noch einen Tag mehr
    assert (a.isoformat(), b.isoformat()) == ("2026-10-12T00:00:00+02:00", "2026-10-14T00:00:00+02:00")
    a, b = tagesfenster(datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc), datetime(2026, 10, 12, 23, 0, tzinfo=timezone.utc))
    assert a.isoformat() == "2026-10-13T00:00:00+02:00" and b.isoformat() == "2026-10-14T00:00:00+02:00"  # schon der 13. in Berlin
    a, b = tagesfenster(tag12(10), tag12(10))  # leeres Fenster wird ein Tag
    assert b - a >= timedelta(hours=23)


def test_termin_ids_sind_kurz_stabil_und_ohne_leerraum():
    k = Kalender("Konto:abcd1234", "Privat", "Konto", True)
    assert termin_id(k, "uid-1") == "Konto:abcd1234:uid-1"
    assert termin_id(k, "uid-1", "20261012T070000Z") == "Konto:abcd1234:uid-1#20261012T070000Z"
    assert " " not in termin_id(k, "mit leerzeichen\tund tab")
    lang = termin_id(k, "u" * 500)
    assert len(lang) < 60 and lang == termin_id(k, "u" * 500) and lang != termin_id(k, "u" * 501)
    assert len(termin_id(k, "x" * 100, "20261012T070000Z")) <= 200


def test_termin_dict_hat_das_format_des_kerns():
    k = Kalender("K:1", "Privat", "Konto", True)
    t = termin_dict(k, "u1", "", "  Zahn   arzt ", "Praxis\nOst", datetime(2026, 10, 15, 7, tzinfo=timezone.utc),
                    datetime(2026, 10, 15, 8, tzinfo=timezone.utc), False)
    assert t == {"id": "K:1:u1", "titel": "Zahn arzt", "start": "2026-10-15T09:00:00+02:00", "ende": "2026-10-15T10:00:00+02:00",
                 "ganztaegig": False, "ort": "Praxis Ost", "kalender": "Privat", "konto": "Konto"}
    t = termin_dict(k, "u2", "", "", "", date(2026, 12, 24), date(2026, 12, 27), True)
    assert (t["start"], t["ende"], t["ganztaegig"], t["titel"]) == ("2026-12-24", "2026-12-27", True, "(ohne Titel)")


def test_sortiert_nach_beginn_ganztaegige_zuerst():
    k = Kalender("K:1", "Privat", "Konto", True)
    def t(uid, titel, start, ende, ganz):
        return termin_dict(k, uid, "", titel, "", start, ende, ganz)
    liste = [
        t("c", "Spät", datetime(2026, 10, 12, 16, tzinfo=BERLIN), datetime(2026, 10, 12, 17, tzinfo=BERLIN), False),
        t("b", "Morgens", datetime(2026, 10, 12, 8, tzinfo=BERLIN), datetime(2026, 10, 12, 9, tzinfo=BERLIN), False),
        t("a", "Ganztags", date(2026, 10, 12), date(2026, 10, 13), True),
        t("d", "Vorher", datetime(2026, 10, 11, 8, tzinfo=BERLIN), datetime(2026, 10, 11, 9, tzinfo=BERLIN), False),
    ]
    assert [x["titel"] for x in sortiert(liste)] == ["Vorher", "Ganztags", "Morgens", "Spät"]


def test_kalender_auswahl_genau_vor_teilstring():
    liste = [Kalender("K:1", "Privat", "A", True), Kalender("K:2", "Privat Kinder", "A", True), Kalender("K:3", "Arbeit", "B", True)]
    assert [k.id for k in auswahl(liste, "privat")] == ["K:1"]  # genau gewinnt
    assert [k.id for k in auswahl(liste, "PRIVAT KINDER")] == ["K:2"]
    assert [k.id for k in auswahl(liste, "kinder")] == ["K:2"]  # sonst ein Teil des Namens
    assert [k.id for k in auswahl(liste, "k:3")] == ["K:3"]  # oder die Kennung
    assert auswahl(liste, "gibt es nicht") == []
    assert "url" not in Kalender("K:1", "x", "y", True, url="https://x.example.com/geheim/").als_dict()
    assert "geheim" not in repr(Kalender("K:1", "x", "y", True, url="https://x.example.com/geheim/"))


# ── CalDAV: Kalender finden ──────────────────────────────────────────────────────────────────────────────
def zwei_kalender():
    return [
        FakeKalender("/dav/calendars/alice/privat/", "Privat", rechte=("read", "write")),
        FakeKalender("/dav/calendars/alice/abo/", "Feiertage", rechte=("read",)),
        FakeKalender("/dav/calendars/alice/ohne-recht/", "Ohne Rechteangabe", rechte=None),
        FakeKalender("/dav/calendars/alice/aufgaben/", "Aufgaben", komponenten=("VTODO",)),
    ]


def test_kalender_werden_ueber_well_known_principal_und_home_set_gefunden(caldav):
    s = caldav(zwei_kalender())
    q = CalDavQuelle(konto(s.url))
    liste = q.kalender()
    assert [(k.name, k.schreibbar) for k in liste] == [("Privat", True), ("Feiertage", False), ("Ohne Rechteangabe", True)]
    assert all(re.fullmatch(r"Nextcloud:[0-9a-f]{8}", k.id) for k in liste) and len({k.id for k in liste}) == 3
    assert all(k.konto == "Nextcloud" and k.url.startswith(s.url + "/dav/calendars/alice/") for k in liste)
    assert [(a.methode, a.pfad, a.kopf.get("depth")) for a in s.anfragen] == [
        ("PROPFIND", "/.well-known/caldav", "0"),
        ("PROPFIND", "/dav/", "0"),  # nach der Umleitung dieselbe Anfrage
        ("PROPFIND", "/dav/principals/alice/", "0"),
        ("PROPFIND", "/dav/calendars/alice/", "1"),
    ]
    assert s.abgelehnt == [] and all(a.kopf["authorization"] == basic("alice", PASSWORT) for a in s.anfragen)
    assert q.schreibbar() and IcsQuelle(konto(s.url, "ics")).schreibbar() is False


def test_liste_wird_eine_stunde_gemerkt_und_auf_wunsch_neu_geladen(caldav):
    s = caldav(zwei_kalender())
    uhr = Uhr()
    q = CalDavQuelle(konto(s.url), uhr=uhr)
    erste = q.kalender()
    n = len(s.anfragen)
    assert q.kalender() == erste and len(s.anfragen) == n  # aus dem Zwischenspeicher
    uhr.weiter(3599)
    q.kalender()
    assert len(s.anfragen) == n
    q.kalender(neu_laden=True)
    assert len(s.anfragen) == 2 * n
    uhr.weiter(3601)
    q.kalender()
    assert len(s.anfragen) == 3 * n
    assert [k.id for k in q.kalender()] == [k.id for k in erste]  # die Kennungen bleiben gleich


@pytest.mark.parametrize("pfad", ["", "/", "/dav/", "/dav", "/dav/principals/alice/", "/dav/calendars/alice/", "/dav/calendars/alice/privat/"])
def test_jede_sinnvolle_adresse_fuehrt_zu_den_kalendern(caldav, pfad):
    s = caldav([FakeKalender("/dav/calendars/alice/privat/", "Privat")])
    liste = CalDavQuelle(konto(s.url + pfad)).kalender()
    assert [k.name for k in liste] == ["Privat"], pfad


def test_ohne_well_known_geht_es_ueber_die_wurzel(caldav):
    s = caldav(wellknown=False, wurzel="/")
    assert [k.name for k in CalDavQuelle(konto(s.url)).kalender()] == ["Privat"]


def test_kalender_ohne_anzeigenamen_heisst_wie_sein_ordner(caldav):
    s = caldav([FakeKalender("/dav/calendars/alice/mein-kalender/", "")])
    assert [k.name for k in CalDavQuelle(konto(s.url)).kalender()] == ["mein-kalender"]


def test_falsches_passwort_ist_ein_klarer_fehler_und_die_suche_hoert_auf(caldav):
    s = caldav()
    with pytest.raises(NetzFehler) as e:
        CalDavQuelle(konto(s.url, passwort="falsches-passwort")).kalender()
    assert e.value.art == "auth" and e.value.text == "Anmeldung abgelehnt (App-Passwort prüfen)"
    assert "falsches-passwort" not in e.value.text
    assert len(s.anfragen) == 1  # kein Durchprobieren anderer Pfade mit demselben Passwort


def test_kein_caldav_server_dahinter(http):
    s404 = http(lambda a: Antwort(404))
    with pytest.raises(NetzFehler) as e:
        CalDavQuelle(konto(s404.url + "/irgendwas")).kalender()
    assert e.value.art == "nicht_gefunden"
    assert [a.pfad for a in s404.anfragen] == ["/irgendwas", "/.well-known/caldav", "/"]
    seite = http(lambda a: Antwort(200, {"Content-Type": "text/html"}, b"<html>Willkommen</html>"))
    with pytest.raises(NetzFehler) as e:
        CalDavQuelle(konto(seite.url)).kalender()
    assert e.value.art in ("antwort", "nicht_gefunden", "anfrage") and PASSWORT not in e.value.text
    leer = http(lambda a: Antwort(207, {"Content-Type": "application/xml"},
                                  b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>'))
    with pytest.raises(KontoFehler) as e:
        CalDavQuelle(konto(leer.url)).kalender()
    assert e.value.art == "nicht_gefunden" and "kein Kalender" in e.value.text


def test_home_set_auf_fremdem_host_bekommt_kein_passwort(caldav, http):
    fremd = http(lambda a: Antwort(207, {}, b"<x/>"))
    s = caldav(zuhause_host=fremd.url)
    with pytest.raises(NetzFehler) as e:
        CalDavQuelle(konto(s.url)).kalender()
    assert e.value.art == "umleitung" and "kein Passwort" in e.value.text and PASSWORT not in e.value.text
    assert fremd.anfragen == []


def test_unbrauchbares_xml_vom_server(http):
    s = http(lambda a: Antwort(207, {}, b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY y 'z'>]><d:multistatus xmlns:d='DAV:'/>"))
    with pytest.raises(NetzFehler) as e:
        CalDavQuelle(konto(s.url)).kalender()
    assert e.value.art == "antwort"


# ── CalDAV: Termine ──────────────────────────────────────────────────────────────────────────────────────
def kalender_mit_terminen() -> FakeKalender:
    k = FakeKalender("/dav/calendars/alice/privat/", "Privat")
    for name, inhalt in (
        termin("fruehstueck", "Frühstück", "20261012T080000", "20261012T090000"),
        termin("meeting", "Meeting", "20261012T100000", "20261012T110000", ort="Raum 1"),
        termin("abend", "Abend", "20261012T190000", "20261012T200000"),
        termin("naechster", "Nächster Tag", "20261013T100000", "20261013T110000"),
    ):
        k.ressourcen[name] = inhalt
    return k


def test_termine_im_fenster_und_der_report_ist_auf_ganze_tage_ausgerichtet(caldav):
    s = caldav([kalender_mit_terminen()])
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    gefunden = q.instanzen(kal, tag12(9, 30), tag12(17))
    assert [(i.titel, i.ort) for i in gefunden] == [("Meeting", "Raum 1")]
    (report,) = s.mit("REPORT")
    assert report.pfad == "/dav/calendars/alice/privat/" and report.kopf["depth"] == "1"
    assert 'start="20261011T220000Z"' in report.koerper.decode() and 'end="20261012T220000Z"' in report.koerper.decode()
    # ein anderes Fenster desselben Tages: aus dem Zwischenspeicher, kein zweiter Abruf
    assert [i.titel for i in q.instanzen(kal, tag12(7), tag12(9, 30))] == ["Frühstück"]
    assert [i.titel for i in q.instanzen(kal, tag12(0), tag12(23, 59))] == ["Frühstück", "Meeting", "Abend"]
    assert len(s.mit("REPORT")) == 1
    # ein anderer Tag: neuer Abruf
    assert [i.titel for i in q.instanzen(kal, tag12(0) + timedelta(days=1), tag12(23) + timedelta(days=1))] == ["Nächster Tag"]
    assert len(s.mit("REPORT")) == 2


def test_abgerufene_termine_gelten_60_sekunden(caldav):
    s = caldav([kalender_mit_terminen()])
    uhr = Uhr()
    q = CalDavQuelle(konto(s.url), uhr=uhr)
    (kal,) = q.kalender()
    q.instanzen(kal, tag12(0), tag12(23))
    uhr.weiter(59)
    q.instanzen(kal, tag12(0), tag12(23))
    assert len(s.mit("REPORT")) == 1
    uhr.weiter(2)
    q.instanzen(kal, tag12(0), tag12(23))
    assert len(s.mit("REPORT")) == 2


def test_leerer_kalender_ist_eine_leere_liste_kein_fehler(caldav):
    s = caldav()
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    assert q.instanzen(kal, tag12(0), tag12(23)) == []


def test_server_loest_wiederholungen_auf_wenn_er_expand_kann(caldav):
    k = FakeKalender("/dav/calendars/alice/privat/", "Privat")
    master = ics(vevent("serie", "Teamrunde", lokal(B, "20261005T090000"), lokal(B, "20261005T100000"), extra="RRULE:FREQ=WEEKLY;BYDAY=MO"))
    vom_server = ics(vevent("serie", "Teamrunde (vom Server)", lokal(B, "20261012T090000"), lokal(B, "20261012T100000"),
                            extra=f"RECURRENCE-ID;TZID={B}:20261012T090000"))
    k.ressourcen["serie.ics"] = (master, vom_server)
    s = caldav([k], expand="ja")
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    assert [(i.titel, i.kennung) for i in q.instanzen(kal, tag12(0), tag12(23))] == [("Teamrunde (vom Server)", "20261012T070000Z")]
    assert "expand" in s.mit("REPORT")[0].koerper.decode()


@pytest.mark.parametrize("verhalten", ["http403", "http501", "multistatus403"])
def test_server_ohne_expand_wird_einmal_ohne_wiederholt_und_das_gemerkt(caldav, verhalten):
    k = FakeKalender("/dav/calendars/alice/privat/", "Privat")
    master = ics(vevent("serie", "Teamrunde", lokal(B, "20261005T090000"), lokal(B, "20261005T100000"), extra="RRULE:FREQ=WEEKLY;BYDAY=MO"))
    k.ressourcen["serie.ics"] = res(master)
    s = caldav([k], expand=verhalten)
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    gefunden = q.instanzen(kal, tag12(0), tag12(23))  # Montag, 12.10.: das Plugin löst die Serie selbst auf
    assert [(i.titel, zeit.iso(i.start)) for i in gefunden] == [("Teamrunde", "2026-10-12T09:00:00+02:00")]
    berichte = [("expand" in r.koerper.decode()) for r in s.mit("REPORT")]
    assert berichte == [True, False]
    q.instanzen(kal, tag12(0) + timedelta(days=7), tag12(23) + timedelta(days=7))  # anderes Fenster: gleich ohne expand
    assert [("expand" in r.koerper.decode()) for r in s.mit("REPORT")] == [True, False, False]


def test_report_fehler_werden_gemeldet(caldav, http):
    s = caldav(zwei_kalender())
    q = CalDavQuelle(konto(s.url))
    (kal, *_) = q.kalender()
    s.kalender.clear()  # der Kalender ist weg
    with pytest.raises(NetzFehler) as e:
        q.instanzen(kal, tag12(0), tag12(23))
    assert e.value.art == "nicht_gefunden"
    k = FakeKalender("/dav/calendars/alice/privat/", "Privat")
    k.ressourcen["x.ics"] = res(ics(vevent("x", "X", u("20261012T070000"))))
    s2 = caldav([k], expand="multistatus403")
    q2 = CalDavQuelle(konto(s2.url))
    (kal2,) = q2.kalender()
    q2._expand = False  # schon bekannt: ohne expand; die Ressource wird trotzdem geliefert
    assert [i.titel for i in q2.instanzen(kal2, tag12(0), tag12(23))] == ["X"]


def test_defekte_ressourcen_stoeren_die_anderen_nicht(caldav):
    k = FakeKalender("/dav/calendars/alice/privat/", "Privat")
    k.ressourcen["kaputt.ics"] = res("das ist kein kalender")
    k.ressourcen["leer.ics"] = res("")
    k.ressourcen["gut.ics"] = res(ics(vevent("gut", "Gut", u("20261012T070000"))))
    s = caldav([k])
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    assert [i.titel for i in q.instanzen(kal, tag12(0), tag12(23))] == ["Gut"]


# ── CalDAV: Anlegen ──────────────────────────────────────────────────────────────────────────────────────
def neu(**felder):
    return eingabe.typisiert(eingabe.neu_pruefen({"titel": "Zahnarzt", "start": "2026-10-15T09:00", "ort": "Praxis", **felder}))


JETZT = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def test_anlegen_sendet_put_mit_if_none_match_und_gueltigem_ics(caldav):
    s = caldav()
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    uid = q.anlegen(kal, neu(), JETZT)
    assert re.fullmatch(r"[0-9a-f-]{36}@fyos-kalender", uid)
    (put,) = s.mit("PUT")
    assert re.fullmatch(r"/dav/calendars/alice/privat/fyos-[0-9a-f]{32}\.ics", put.pfad)
    assert put.kopf["if-none-match"] == "*" and put.kopf["content-type"] == "text/calendar; charset=utf-8"
    assert put.kopf["authorization"] == basic("alice", PASSWORT)
    (e,) = ical.lies(put.koerper.decode()).ereignisse
    assert (e.uid, e.titel, e.ort) == (uid, "Zahnarzt", "Praxis")
    assert e.start == datetime(2026, 10, 15, 7, tzinfo=timezone.utc)
    (name, text), = s.kalender[0].angelegt
    assert name in put.pfad and "SUMMARY:Zahnarzt" in text


def test_nach_dem_anlegen_wird_neu_abgerufen(caldav):
    s = caldav([kalender_mit_terminen()])
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    q.instanzen(kal, tag12(0), tag12(23))
    q.instanzen(kal, tag12(0), tag12(23))
    assert len(s.mit("REPORT")) == 1
    q.anlegen(kal, neu(), JETZT)
    q.instanzen(kal, tag12(0), tag12(23))
    assert len(s.mit("REPORT")) == 2  # der Zwischenspeicher ist geleert


def test_anlegen_bei_kollision_einmal_mit_neuer_uid(caldav):
    s = caldav(put_412_zuerst=True)
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    uid = q.anlegen(kal, neu(), JETZT)
    puts = s.mit("PUT")
    assert len(puts) == 2 and puts[0].pfad != puts[1].pfad
    assert uid in puts[1].koerper.decode() and uid not in puts[0].koerper.decode()


def test_anlegen_zweimal_kollision_ist_ein_fehler(caldav):
    s = caldav(put_status=412)
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    with pytest.raises(NetzFehler) as e:
        q.anlegen(kal, neu(), JETZT)
    assert e.value.status == 412 and len(s.mit("PUT")) == 2


@pytest.mark.parametrize(
    "status,art,text",
    [(403, "verboten", "keine Termine an"), (401, "auth", "Anmeldung abgelehnt"), (409, "anfrage", "lehnt den Termin ab (HTTP 409)"),
     (415, "anfrage", "lehnt den Termin ab (HTTP 415)"), (500, "server", "HTTP 500"), (507, "server", "HTTP 507")],
)
def test_anlegen_fehler_haben_verstaendliche_texte(caldav, status, art, text):
    s = caldav(put_status=status)
    q = CalDavQuelle(konto(s.url))
    (kal,) = q.kalender()
    with pytest.raises(NetzFehler) as e:
        q.anlegen(kal, neu(), JETZT)
    assert e.value.art == art and text in e.value.text and PASSWORT not in e.value.text
    assert len(s.mit("PUT")) == 1


# ── ICS-Abonnement ───────────────────────────────────────────────────────────────────────────────────────
FEIERTAGE = ics(
    vevent("f1", "Tag der Deutschen Einheit", tag("20261003")),
    vevent("f2", "Reformationstag", tag("20261031")),
    vevent("f3", "Weihnachten", tag("20261225"), tag("20261227")),
    vevent("w1", "Wöchentlich", lokal(B, "20261005T090000"), lokal(B, "20261005T100000"), extra="RRULE:FREQ=WEEKLY;BYDAY=MO"),
    name="Feiertage",
)


def feed(http, inhalt: str | bytes = FEIERTAGE, **kopf):
    roh = inhalt.encode("utf-8") if isinstance(inhalt, str) else inhalt
    return http(lambda a: Antwort(200, {"Content-Type": "text/calendar; charset=utf-8", **kopf}, roh))


def ics_konto(s: Server, pfad: str = "/ical/geheim/basic.ics", **kw) -> Konto:
    return Konto(name="Feiertage", art="ics", url=s.url + pfad, **kw)


def test_ics_ein_kalender_mit_dem_namen_des_kontos_der_abruf_prueft_die_adresse(http):
    s = feed(http)
    q = IcsQuelle(ics_konto(s))
    (k,) = q.kalender()
    assert (k.id, k.name, k.konto, k.schreibbar) == ("Feiertage", "Feiertage", "Feiertage", False)
    assert len(s.mit("GET")) == 1  # ein totes Konto zeigt sich schon hier, nicht erst bei den Terminen
    q.kalender()
    assert len(s.mit("GET")) == 1  # gemerkt


def test_ics_termine_im_fenster_mit_aufgeloester_serie(http):
    s = feed(http)
    q = IcsQuelle(ics_konto(s))
    (k,) = q.kalender()
    fenster = (datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 11, 3, tzinfo=BERLIN))
    gefunden = [(i.titel, i.start.isoformat() if i.ganztaegig else zeit.iso(i.start)) for i in q.instanzen(k, *fenster)]
    assert gefunden == [
        ("Tag der Deutschen Einheit", "2026-10-03"),
        ("Wöchentlich", "2026-10-05T09:00:00+02:00"),
        ("Wöchentlich", "2026-10-12T09:00:00+02:00"),
        ("Wöchentlich", "2026-10-19T09:00:00+02:00"),
        ("Wöchentlich", "2026-10-26T09:00:00+01:00"),
        ("Reformationstag", "2026-10-31"),
        ("Wöchentlich", "2026-11-02T09:00:00+01:00"),
    ]
    assert len(s.mit("GET")) == 1


def test_ics_feed_wird_zehn_minuten_gemerkt(http):
    s = feed(http)
    uhr = Uhr()
    q = IcsQuelle(ics_konto(s), uhr=uhr)
    (k,) = q.kalender()
    q.instanzen(k, tag12(0), tag12(23))
    q.instanzen(k, tag12(0) + timedelta(days=40), tag12(23) + timedelta(days=40))  # anderes Fenster, derselbe Abruf
    assert len(s.mit("GET")) == 1
    uhr.weiter(599)
    q.instanzen(k, tag12(0) + timedelta(days=80), tag12(23) + timedelta(days=80))
    assert len(s.mit("GET")) == 1
    uhr.weiter(2)
    q.instanzen(k, tag12(0) + timedelta(days=120), tag12(23) + timedelta(days=120))
    assert len(s.mit("GET")) == 2
    q.kalender(neu_laden=True)
    assert len(s.mit("GET")) == 3


def test_ics_mit_zugang_sendet_basic_ohne_zugang_nichts(http):
    s = feed(http)
    q = IcsQuelle(ics_konto(s, nutzer="anna", passwort=PASSWORT))
    (k,) = q.kalender()
    q.instanzen(k, tag12(0), tag12(23))
    assert s.anfragen[0].kopf["authorization"] == basic("anna", PASSWORT)
    s2 = feed(http)
    IcsQuelle(ics_konto(s2)).instanzen(Kalender("Feiertage", "Feiertage", "Feiertage", False), tag12(0), tag12(23))
    assert "authorization" not in s2.anfragen[0].kopf


def test_ics_login_seite_statt_kalender_ist_ein_fehler_nie_keine_termine(http):
    s = http(lambda a: Antwort(200, {"Content-Type": "text/html"}, b"<html><body>Bitte anmelden</body></html>"))
    q = IcsQuelle(ics_konto(s))
    with pytest.raises(KontoFehler) as e:
        q.kalender(neu_laden=True)
    assert "liefert keinen Kalender" in e.value.text and e.value.art == "antwort"
    with pytest.raises(KontoFehler):
        q.instanzen(Kalender("Feiertage", "Feiertage", "Feiertage", False), tag12(0), tag12(23))


@pytest.mark.parametrize("status,art", [(404, "nicht_gefunden"), (401, "auth"), (403, "verboten"), (500, "server")])
def test_ics_fehlerstatus(http, status, art):
    s = http(lambda a: Antwort(status))
    for aufruf in (lambda q: q.kalender(), lambda q: q.kalender(neu_laden=True),
                   lambda q: q.instanzen(Kalender("Feiertage", "Feiertage", "Feiertage", False), tag12(0), tag12(23))):
        with pytest.raises(NetzFehler) as e:
            aufruf(IcsQuelle(ics_konto(s)))
        assert e.value.art == art and "geheim" not in e.value.text  # der Pfad der Adresse steht nie im Text


def test_ics_zeichensaetze_und_gzip(http):
    text = ics(vevent("u", "Äpfel & Öl", u("20261012T070000")))
    for roh, kopf in (
        (text.encode("utf-8"), {}),
        (b"\xef\xbb\xbf" + text.encode("utf-8"), {}),
        (text.encode("latin-1"), {"Content-Type": "text/calendar; charset=iso-8859-1"}),
        (text.encode("latin-1"), {"Content-Type": "text/calendar"}),  # ohne Angabe: Ersatz
        (gzip.compress(text.encode("utf-8")), {"Content-Encoding": "gzip"}),
    ):
        s = http(lambda a, roh=roh, kopf=kopf: Antwort(200, {"Content-Type": "text/calendar", **kopf}, roh))
        q = IcsQuelle(ics_konto(s))
        (k,) = q.kalender()
        assert [i.titel for i in q.instanzen(k, tag12(0), tag12(23))] == ["Äpfel & Öl"], kopf


def test_ics_ist_schreibgeschuetzt(http):
    s = feed(http)
    q = IcsQuelle(ics_konto(s))
    (k,) = q.kalender()
    with pytest.raises(KontoFehler) as e:
        q.anlegen(k, neu(), JETZT)
    assert "schreibgeschützt" in e.value.text and s.mit("PUT") == [] and len(s.anfragen) == 1


def test_ics_adresse_mit_pfad_geheimnis_steht_in_keinem_fehler(http):
    s = http(lambda a: Antwort(200, {}, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR"))
    q = IcsQuelle(ics_konto(s, "/ical/SEHR-GEHEIM/basic.ics"))
    assert q.instanzen(Kalender("Feiertage", "Feiertage", "Feiertage", False), tag12(0), tag12(23)) == []  # leerer Kalender: gültig
    assert "SEHR-GEHEIM" not in json.dumps(q.konto.als_dict()) and "SEHR-GEHEIM" not in repr(q)


def test_quelle_fuer_waehlt_nach_art(http):
    s = feed(http)
    assert isinstance(quelle_fuer(ics_konto(s)), IcsQuelle)
    assert isinstance(quelle_fuer(konto(s.url)), CalDavQuelle)


def test_instanz_dict_ist_das_terminformat_des_kerns(http):
    s = feed(http)
    q = IcsQuelle(ics_konto(s))
    (k,) = q.kalender()
    (i, *_) = [i for i in q.instanzen(k, datetime(2026, 12, 20, tzinfo=BERLIN), datetime(2026, 12, 31, tzinfo=BERLIN)) if i.titel == "Weihnachten"]
    t = instanz_dict(k, i)
    assert t == {"id": "Feiertage:f3", "titel": "Weihnachten", "start": "2026-12-25", "ende": "2026-12-27", "ganztaegig": True,
                 "ort": "", "kalender": "Feiertage", "konto": "Feiertage"}
