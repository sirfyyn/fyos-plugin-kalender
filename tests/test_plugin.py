"""Das Plugin als Ganzes: Konten, die drei Werkzeuge, die drei Zustände, Trockenlauf und die Ablage für den Kern."""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fakes import Antwort, FakeCalDav, FakeKalender, Uhr, basic, ganztags, ics, termin, vevent

from fyos_plugin_kalender import kern
from fyos_plugin_kalender import plugin as plugin_modul
from fyos_plugin_kalender.plugin import AUSGABE_MAX, HINWEIS_EINRICHTEN, KalenderPlugin
from mcp_hub import registry as hub_registry

PASSWORT = "geheim-app-passwort"
ZWEITES = "anderes-passwort-bob"
JETZT = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)  # 8 Uhr in Berlin, Montag
BERLIN = ZoneInfo("Europe/Berlin")
ID = r"Nextcloud:[0-9a-f]{8}"


# ── Aufbau ───────────────────────────────────────────────────────────────────────────────────────────────
def privat() -> FakeKalender:
    k = FakeKalender("/dav/calendars/alice/privat/", "Privat")
    k.ressourcen.update(dict([
        termin("fruehstueck", "Frühstück", "20261012T080000", "20261012T090000"),
        termin("zahnarzt", "Zahnarzt", "20261013T100000", "20261013T110000", ort="Praxis"),
        ganztags("urlaub", "Urlaub", "20261016", "20261018"),
        termin("spaeter", "Später im Jahr", "20261130T100000", "20261130T110000"),
    ]))
    return k


def feiertage() -> FakeKalender:
    k = FakeKalender("/dav/calendars/alice/feiertage/", "Feiertage", rechte=("read",))
    k.ressourcen.update(dict([ganztags("reformation", "Reformationstag", "20261031", "20261101")]))
    return k


def arbeit() -> FakeKalender:
    k = FakeKalender("/dav/calendars/alice/projekt/", "Projekt")
    k.ressourcen.update(dict([termin("sprint", "Sprint-Planung", "20261012T130000", "20261012T140000", ort="Raum 2")]))
    return k


def konto_eintragen(monkeypatch, s, name: str = "Nextcloud", nutzer: str = "alice", passwort: str = PASSWORT) -> None:
    monkeypatch.setenv("FYOS_KALENDER_URL", s.url)
    monkeypatch.setenv("FYOS_KALENDER_NUTZER", nutzer)
    monkeypatch.setenv("FYOS_KALENDER_PASSWORT", passwort)
    monkeypatch.setenv("FYOS_KALENDER_NAME", name)


def weiteres_konto(monkeypatch, s, name: str = "Arbeit", nutzer: str = "bob", passwort: str = ZWEITES, **mehr) -> None:
    monkeypatch.setenv("FYOS_KALENDER_ZWEITES_PW", passwort)
    eintrag = {"name": name, "url": s.url, "nutzer": nutzer, "passwort_env": "FYOS_KALENDER_ZWEITES_PW", **mehr}
    monkeypatch.setenv("FYOS_KALENDER_KONTEN", json.dumps([eintrag]))


@pytest.fixture
def bauen(monkeypatch):
    """``bauen(server)``: ein Plugin mit dem Konto „Nextcloud“ auf dem Attrappen-Server; Montag, 12.10.2026, 8 Uhr."""

    def neu(s=None, **config) -> KalenderPlugin:
        if s is not None:
            konto_eintragen(monkeypatch, s)
        config.setdefault("jetzt", lambda: JETZT)
        return KalenderPlugin(config)

    return neu


def titel(antwort: dict) -> list[str]:
    return [t["titel"] for t in antwort["termine"]]


# ── Lebenslauf, Beschreibung ─────────────────────────────────────────────────────────────────────────────
def test_ohne_konto_ist_das_plugin_aus_und_harmlos():
    p = KalenderPlugin()
    assert p.eingerichtet is False and p.name == "kalender"
    p.register()
    h = p.health()
    assert h == {"ok": True, "status": "aus", "reason": "kein Konto eingerichtet (FYOS_KALENDER_URL …)"}
    assert hub_registry.plugin_aus(h) and hub_registry.plugin_healthy(h)  # absichtlich aus, nicht kaputt
    p.registry_anbinden(object())
    if kern.modul() is not None:
        assert kern.modul().angemeldete_ablage() is None  # ohne Konto meldet es sich nicht als Quelle an
    p.shutdown()


def test_werkzeuge_wirkung_und_schemas():
    p = KalenderPlugin()
    werkzeuge = {t.name: t for t in p.describe_tools()}
    assert set(werkzeuge) == {"konten", "termine", "anlegen"}
    assert {n: hub_registry.scope_from_tags(t.tags) for n, t in werkzeuge.items()} == {
        "konten": "READ", "termine": "READ", "anlegen": "WRITE"}
    for t in werkzeuge.values():
        assert t.title and t.description and t.input_schema["type"] == "object"
        assert t.input_schema["additionalProperties"] is False  # der Hub lehnt unbekannte Argumente ab
        assert "kalender" in t.tags
    assert werkzeuge["anlegen"].input_schema["required"] == ["titel", "start"]
    assert "bestaetigt" in werkzeuge["anlegen"].input_schema["properties"]
    assert werkzeuge["termine"].input_schema["properties"]["tage"]["maximum"] == 31
    assert KalenderPlugin.argumente_vertraulich is True  # Titel und Orte stehen nie im Klartext auf dem Ereignisbus
    with pytest.raises(ValueError):
        p.call("loeschen", {})


def test_konto_aus_der_umgebung_wird_beim_start_nicht_befragt(bauen, caldav):
    s = caldav([privat()])
    p = bauen(s)
    p.register()
    assert p.eingerichtet and p.health()["konten"] == 1
    assert s.anfragen == []  # register() und health() gehen nie ans Netz


def test_unbrauchbarer_eintrag_ist_sichtbar_und_nicht_aus(monkeypatch):
    monkeypatch.setenv("FYOS_KALENDER_URL", "ftp://kalender.example.com/")
    p = KalenderPlugin()
    assert p.eingerichtet  # es gibt einen Eintrag, nur einen kaputten
    p.register()  # wirft nicht
    h = p.health()
    assert h["ok"] is False and h["status"] == "unhealthy" and "FYOS_KALENDER_URL" in h["reason"]
    assert not hub_registry.plugin_aus(h)


# ── Hub: so ruft der Hub die Werkzeuge ───────────────────────────────────────────────────────────────────
@pytest.fixture
def reg(bauen, caldav):
    s = caldav([privat(), feiertage()])
    r = hub_registry.PluginRegistry()
    hub_registry._register_one(r, "kalender", bauen(s))
    r.server = s
    return r


def test_hub_ruft_termine_und_konten(reg):
    r = hub_registry.dispatch(reg, "kalender.termine", {})
    assert r["ok"] is True and titel(r["result"]) == ["Frühstück", "Zahnarzt", "Urlaub"]
    r = hub_registry.dispatch(reg, "kalender.konten", {})
    assert r["ok"] is True and r["result"]["konten"][0]["name"] == "Nextcloud"


def test_eingabefehler_sind_bad_arguments_und_sperren_nicht(reg):
    for _ in range(8):  # mehr als die Schwelle des Schutzschalters
        for tool, args in (
            ("kalender.termine", {"tage": 0}),
            ("kalender.termine", {"tage": 99}),
            ("kalender.termine", {"kalender": "gibt es nicht"}),
            ("kalender.anlegen", {"titel": "x", "start": "2026-10-15T09:00"}),  # ohne Bestätigung
            ("kalender.anlegen", {"start": "2026-10-15T09:00"}),
            ("kalender.konten", {"neu_laden": "ja"}),
        ):
            r = hub_registry.dispatch(reg, tool, args)
            assert r["ok"] is False and r["kind"] == "bad_arguments", (tool, args, r)
    assert hub_registry.dispatch(reg, "kalender.termine", {})["ok"] is True
    assert reg.server.mit("PUT") == []


def test_unbekanntes_argument_wird_vom_hub_abgelehnt(reg):
    r = hub_registry.dispatch(reg, "kalender.termine", {"foo": 1})
    assert r["ok"] is False and r["kind"] == "bad_arguments" and "foo" in r["error"]


def test_fehlschlag_ist_tool_failed_nie_ein_leeres_ergebnis(bauen, caldav):
    s = caldav([privat()])
    p = bauen(s)
    r = hub_registry.PluginRegistry()
    hub_registry._register_one(r, "kalender", p)
    s.passwort = "inzwischen-geaendert"  # der Server lehnt das App-Passwort jetzt ab
    antwort = hub_registry.dispatch(r, "kalender.termine", {})
    assert antwort["ok"] is False and antwort["kind"] == "tool_failed"
    assert "Anmeldung abgelehnt" in antwort["error"] and "Keine Termine" not in antwort["error"]


def test_hub_laedt_das_plugin_mit_konfiguration_aus_der_config(caldav, monkeypatch):
    from mcp_hub.config import HubConfig, PluginConfig

    s = caldav([privat()])
    monkeypatch.setenv("FAMILIE_PW", PASSWORT)
    konto = {"name": "Familie", "url": s.url, "nutzer": "alice", "passwort_env": "FAMILIE_PW"}
    cfg = HubConfig(plugins=[PluginConfig(name="kalender", module="fyos_plugin_kalender.plugin", config={"konten": [konto]})])
    reg = hub_registry.build_registry(cfg)
    assert not reg.nicht_geladen
    assert sorted(t for t in reg.tools if t.startswith("kalender.")) == ["kalender.anlegen", "kalender.konten", "kalender.termine"]
    r = hub_registry.dispatch(reg, "kalender.konten", {})
    assert r["ok"] is True and [z["name"] for z in r["result"]["konten"]] == ["Familie"]


# ── kalender.konten ──────────────────────────────────────────────────────────────────────────────────────
def test_konten_zeigt_konten_und_kalender_nie_das_passwort(bauen, caldav):
    s = caldav([privat(), feiertage()])
    p = bauen(s)
    r = p.konten()
    assert r["ok"] is True and r["eingerichtet"] is True and "hinweis" not in r
    (z,) = r["konten"]
    assert (z["name"], z["art"], z["nutzer"], z["status"], z["adresse"]) == ("Nextcloud", "caldav", "alice", "ok", s.url)
    assert [(k["name"], k["schreibbar"]) for k in z["kalender"]] == [("Privat", True), ("Feiertage", False)]
    assert all(re.fullmatch(ID, k["id"]) for k in z["kalender"])
    text = json.dumps(r, ensure_ascii=False)
    assert PASSWORT not in text and "/dav/" not in text and basic("alice", PASSWORT) not in text


def test_konten_neu_laden_sucht_die_kalender_neu(bauen, caldav):
    s = caldav([privat()])
    p = bauen(s)
    p.konten()
    n = len(s.mit("PROPFIND"))
    p.konten()
    assert len(s.mit("PROPFIND")) == n  # aus dem Zwischenspeicher
    s.kalender.append(feiertage())
    assert [k["name"] for k in p.konten()["konten"][0]["kalender"]] == ["Privat"]
    assert [k["name"] for k in p.konten(True)["konten"][0]["kalender"]] == ["Privat", "Feiertage"]
    with pytest.raises(ValueError):
        p.konten("ja")


def test_konten_ohne_einrichtung_zeigt_den_weg(bauen):
    r = bauen().konten()
    assert r == {"ok": True, "eingerichtet": False, "konten": [], "hinweis": HINWEIS_EINRICHTEN}
    assert "FYOS_KALENDER_URL" in r["hinweis"] and "FYOS_KALENDER_PASSWORT" in r["hinweis"]


def test_konten_mit_einem_toten_konto_sagen_es(bauen, caldav, monkeypatch):
    s, s2 = caldav([privat()]), caldav([arbeit()], nutzer="bob", passwort=ZWEITES)
    konto_eintragen(monkeypatch, s)
    weiteres_konto(monkeypatch, s2, passwort="falsches-passwort")
    r = bauen().konten()
    assert [z["status"] for z in r["konten"]] == ["ok", "fehler"]
    assert r["konten"][1]["fehler"] == "Anmeldung abgelehnt (App-Passwort prüfen)" and "kalender" not in r["konten"][1]
    assert r["hinweis"] == "1 von 2 Konten sind nicht nutzbar; ihre Termine fehlen."
    assert "falsches-passwort" not in json.dumps(r)


# ── kalender.termine ─────────────────────────────────────────────────────────────────────────────────────
def test_termine_vorgabe_sieben_tage_ab_heute_im_terminformat(bauen, caldav):
    s = caldav([privat(), feiertage()])
    r = bauen(s).termine()
    assert r["ok"] is True and r["anzahl"] == 3 and "gekuerzt" not in r and "hinweis" not in r
    assert (r["von"], r["bis"]) == ("2026-10-12T00:00:00+02:00", "2026-10-19T00:00:00+02:00")  # heute 0 Uhr, 7 Tage
    fruehstueck, zahnarzt, urlaub = r["termine"]
    assert re.fullmatch(ID + ":fruehstueck", fruehstueck["id"])
    assert {k: v for k, v in fruehstueck.items() if k != "id"} == {
        "titel": "Frühstück", "start": "2026-10-12T08:00:00+02:00", "ende": "2026-10-12T09:00:00+02:00",
        "ganztaegig": False, "ort": "", "kalender": "Privat", "konto": "Nextcloud"}
    assert (zahnarzt["titel"], zahnarzt["ort"]) == ("Zahnarzt", "Praxis")
    assert {k: urlaub[k] for k in ("titel", "start", "ende", "ganztaegig")} == {
        "titel": "Urlaub", "start": "2026-10-16", "ende": "2026-10-18", "ganztaegig": True}  # ende: der Tag nach dem letzten
    assert set(fruehstueck) == {"id", "titel", "start", "ende", "ganztaegig", "ort", "kalender", "konto"}  # keine Beschreibung


@pytest.mark.parametrize(
    "args,erwartet",
    [
        ({"tage": 1}, ["Frühstück"]),
        ({"tage": 2}, ["Frühstück", "Zahnarzt"]),
        ({"tage": 3.0}, ["Frühstück", "Zahnarzt"]),
        ({"von": "2026-10-13", "tage": 1}, ["Zahnarzt"]),
        ({"von": "2026-10-13T10:30", "tage": 1}, ["Zahnarzt"]),  # ein laufender Termin zählt
        ({"von": "2026-10-13T11:00", "tage": 1}, []),  # ende ausgeschlossen
        ({"von": "2026-10-30", "bis": "2026-11-02"}, ["Reformationstag"]),
        ({"von": "2026-10-12", "bis": "2026-10-14"}, ["Frühstück", "Zahnarzt"]),  # bis ausgeschlossen
        ({"von": "2026-10-12T00:00:00+02:00", "bis": "2026-10-12T08:30:00+02:00"}, ["Frühstück"]),
        ({"von": "2026-10-12T06:00:00+00:00", "bis": "2026-10-12T06:30:00+00:00"}, ["Frühstück"]),  # UTC: 8 Uhr in Berlin
        ({"kalender": "feiertage", "von": "2026-10-12", "bis": "2026-11-05"}, ["Reformationstag"]),
        ({"kalender": "Privat", "tage": 31}, ["Frühstück", "Zahnarzt", "Urlaub"]),
    ],
)
def test_termine_fenster(bauen, caldav, args, erwartet):
    s = caldav([privat(), feiertage()])
    r = bauen(s).call("termine", args)
    assert r["ok"] is True and titel(r) == erwartet, r
    assert ("hinweis" in r) == (not erwartet)  # nur die leere Antwort trägt den Hinweis „Keine Termine“


def test_termine_filter_fragt_nur_den_gewaehlten_kalender(bauen, caldav):
    s = caldav([privat(), feiertage()])
    p = bauen(s)
    r = p.termine(von="2026-10-12", bis="2026-11-05", kalender="Feiertage")
    assert titel(r) == ["Reformationstag"] and r["termine"][0]["kalender"] == "Feiertage"
    assert [a.pfad for a in s.mit("REPORT")] == ["/dav/calendars/alice/feiertage/"]
    # Teil des Namens und Kennung gehen auch
    assert titel(p.termine(von="2026-10-12", bis="2026-11-05", kalender="feier")) == ["Reformationstag"]
    kennung = next(k["id"] for k in p.konten()["konten"][0]["kalender"] if k["name"] == "Feiertage")
    assert titel(p.termine(von="2026-10-12", bis="2026-11-05", kalender=kennung)) == ["Reformationstag"]


def test_termine_unbekannter_kalender_nennt_die_vorhandenen(bauen, caldav):
    s = caldav([privat(), feiertage()])
    with pytest.raises(ValueError, match=r"kein Kalender 'Gibt es nicht'; vorhanden: 'Privat', 'Feiertage'"):
        bauen(s).termine(kalender="Gibt es nicht")


@pytest.mark.parametrize(
    "args",
    [
        {"tage": 0}, {"tage": 32}, {"tage": -1}, {"tage": "x"}, {"tage": True}, {"tage": 1.5},
        {"bis": "2026-10-20"},  # bis braucht von
        {"tage": 2, "von": "2026-10-12", "bis": "2026-10-20"},  # tage und bis schließen sich aus
        {"von": "kein Datum"}, {"von": "2026-10-12", "bis": "morgen"},
        {"von": "2026-10-12", "bis": "2026-10-12"}, {"von": "2026-10-12", "bis": "2026-10-11"},
        {"von": "2026-10-01", "bis": "2026-11-05"},  # mehr als 31 Tage
        {"kalender": 5}, {"kalender": "x" * 201},
    ],
)
def test_termine_falsche_eingaben_sind_value_error(bauen, caldav, args):
    s = caldav([privat()])
    with pytest.raises(ValueError):
        bauen(s).call("termine", args)
    assert s.anfragen == []  # geprüft, bevor das Netz gefragt wird


def test_termine_ueber_die_zeitumstellung_sind_31_tage_erlaubt(bauen, caldav):
    s = caldav([privat()])
    r = bauen(s).termine(von="2026-10-12", bis="2026-11-12")  # 31 Tage, dazwischen die Umstellung (25.10.)
    assert r["ok"] is True and r["von"] == "2026-10-12T00:00:00+02:00" and r["bis"] == "2026-11-12T00:00:00+01:00"


def test_termine_aus_mehreren_konten_gemischt_und_sortiert(bauen, caldav, http, monkeypatch):
    s = caldav([privat()])
    feed = http(lambda a: Antwort(200, {"Content-Type": "text/calendar"}, ics(
        vevent("m1", "Müllabfuhr", ";VALUE=DATE:20261014", ";VALUE=DATE:20261015"), name="Abfall").encode()))
    konto_eintragen(monkeypatch, s)
    monkeypatch.setenv("FYOS_KALENDER_KONTEN", json.dumps([{"name": "Abfall", "art": "ics", "url": feed.url + "/abfall.ics"}]))
    r = KalenderPlugin({"jetzt": lambda: JETZT}).termine()
    assert titel(r) == ["Frühstück", "Zahnarzt", "Müllabfuhr", "Urlaub"]
    assert [t["konto"] for t in r["termine"]] == ["Nextcloud", "Nextcloud", "Abfall", "Nextcloud"]
    assert r["termine"][2]["id"] == "Abfall:m1" and r["termine"][2]["kalender"] == "Abfall"


def test_termine_kuerzt_die_ausgabe(bauen, caldav):
    k = FakeKalender("/dav/calendars/alice/privat/", "Privat")
    for i in range(AUSGABE_MAX + 50):
        k.ressourcen.update(dict([termin(f"t{i:03d}", f"Termin {i:03d}", "20261012T100000", "20261012T110000")]))
    r = bauen(caldav([k])).termine(tage=1)
    assert r["ok"] is True and r["anzahl"] == AUSGABE_MAX + 50 and len(r["termine"]) == AUSGABE_MAX
    assert r["gekuerzt"] is True and "Nur die ersten 200 von 250" in r["hinweis"]


def test_termine_wiederholungen_werden_aufgeloest(bauen, caldav):
    k = FakeKalender("/dav/calendars/alice/privat/", "Privat")
    serie = ics(
        vevent("serie", "Teamrunde", ";TZID=Europe/Berlin:20260921T090000", ";TZID=Europe/Berlin:20260921T100000",
               extra="RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEXDATE;TZID=Europe/Berlin:20261019T090000"),
        vevent("serie", "Teamrunde (verschoben)", ";TZID=Europe/Berlin:20261026T150000", ";TZID=Europe/Berlin:20261026T160000",
               extra="RECURRENCE-ID;TZID=Europe/Berlin:20261026T090000"),
    )
    k.ressourcen["serie.ics"] = (serie, serie)
    s = caldav([k], expand="http501")  # der Server kann es nicht: das Plugin löst auf
    r = bauen(s).termine(von="2026-10-12", bis="2026-11-03")
    assert [(t["titel"], t["start"]) for t in r["termine"]] == [
        ("Teamrunde", "2026-10-12T09:00:00+02:00"),  # 19.10. ist ausgenommen
        ("Teamrunde (verschoben)", "2026-10-26T15:00:00+01:00"),
        ("Teamrunde", "2026-11-02T09:00:00+01:00"),
    ]
    assert len({t["id"] for t in r["termine"]}) == 3  # jede Instanz hat ihre eigene Kennung


# ── Die drei Zustände ────────────────────────────────────────────────────────────────────────────────────
def test_zustand_nicht_eingerichtet_ist_weder_leer_noch_ein_fehler_des_kalenders(bauen):
    r = bauen().termine()
    assert r == {"ok": False, "eingerichtet": False, "error": HINWEIS_EINRICHTEN}
    assert "termine" not in r and "Keine Termine" not in r["error"]


def test_zustand_keine_termine_ist_eine_antwort(bauen, caldav):
    s = caldav([FakeKalender("/dav/calendars/alice/privat/", "Privat")])
    r = bauen(s).termine()
    assert r["ok"] is True and r["anzahl"] == 0 and r["termine"] == [] and r["hinweis"] == "Keine Termine in diesem Zeitraum."
    assert "konten_fehler" not in r


def test_zustand_fehler_ist_nie_keine_termine(bauen, caldav):
    s = caldav([privat()], passwort="ein-anderes-passwort")
    r = bauen(s).termine()
    assert r["ok"] is False and "termine" not in r and "Keine Termine" not in r["error"]
    assert r["error"].startswith("Kein Kalender antwortet (Nextcloud: Anmeldung abgelehnt")
    assert r["konten_fehler"] == [{"konto": "Nextcloud", "fehler": "Anmeldung abgelehnt (App-Passwort prüfen)"}]
    assert PASSWORT not in json.dumps(r) and "ein-anderes-passwort" not in json.dumps(r)


def test_zustand_server_nicht_erreichbar(bauen, caldav):
    s = caldav([privat()])
    p = bauen(s)
    s.stop()
    r = p.termine()
    assert r["ok"] is False and r["konten_fehler"][0]["konto"] == "Nextcloud" and "Keine Termine" not in r["error"]


def test_ein_konto_antwortet_nicht_die_anderen_liefern_und_der_ausfall_steht_drin(bauen, caldav, monkeypatch):
    s, s2 = caldav([privat()]), caldav([arbeit()], nutzer="bob", passwort=ZWEITES)
    konto_eintragen(monkeypatch, s)
    weiteres_konto(monkeypatch, s2, passwort="falsches-passwort")
    r = bauen().termine(tage=1)
    assert r["ok"] is True and titel(r) == ["Frühstück"]
    assert r["konten_fehler"] == [{"konto": "Arbeit", "fehler": "Anmeldung abgelehnt (App-Passwort prüfen)"}]
    assert r["hinweis"] == "Unvollständig: Arbeit antwortet nicht."  # kein freier Tag, wo in Wahrheit ein Konto fehlt


def test_filter_auf_erreichbaren_kalender_ignoriert_ausfall_anderer_konten(bauen, caldav, monkeypatch):
    s, s2 = caldav([privat()]), caldav([arbeit()], nutzer="bob", passwort=ZWEITES)
    konto_eintragen(monkeypatch, s)
    weiteres_konto(monkeypatch, s2, passwort="falsches-passwort")
    r = bauen().termine(tage=1, kalender="Privat")
    assert r["ok"] is True and titel(r) == ["Frühstück"] and "konten_fehler" not in r


def test_filter_auf_kalender_im_ausgefallenen_konto_ist_ein_fehler_kein_value_error(bauen, caldav, monkeypatch):
    s, s2 = caldav([privat()]), caldav([arbeit()], nutzer="bob", passwort=ZWEITES)
    konto_eintragen(monkeypatch, s)
    weiteres_konto(monkeypatch, s2, passwort="falsches-passwort")
    r = bauen().termine(tage=1, kalender="Projekt")  # den gibt es nur im Konto, das nicht antwortet
    assert r["ok"] is False and r["konten_fehler"][0]["konto"] == "Arbeit"


def test_zustand_unbrauchbarer_eintrag_ist_ein_fehler_mit_grund(bauen, monkeypatch):
    monkeypatch.setenv("FYOS_KALENDER_URL", "ftp://kalender.example.com/")
    r = bauen().termine()
    assert r["ok"] is False and "Konfiguration fehlerhaft" in r["error"] and "FYOS_KALENDER_URL" in r["error"]
    k = bauen().konten()
    assert k["eingerichtet"] is True and k["konten"][0]["status"] == "fehler" and "url muss mit https://" in k["konten"][0]["fehler"]


def test_unerwarteter_fehler_zeigt_nur_den_typ(bauen, caldav, monkeypatch):
    s = caldav([privat()])
    p = bauen(s)

    def kaputt(*a, **k):
        raise RuntimeError("Text aus einer Bibliothek mit geheim-app-passwort")

    monkeypatch.setattr(p._quellen[0], "instanzen", kaputt)
    r = p.termine()
    assert r["ok"] is False and r["konten_fehler"] == [{"konto": "Nextcloud", "fehler": "Unerwarteter Fehler (RuntimeError)"}]
    assert PASSWORT not in json.dumps(r)


# ── kalender.anlegen ─────────────────────────────────────────────────────────────────────────────────────
NEU = {"titel": "Zahnreinigung", "start": "2026-10-15T09:00", "ort": "Praxis Süd"}


def test_trockenlauf_zeigt_den_plan_und_sendet_nichts(bauen, caldav):
    s = caldav([privat(), feiertage()])  # genau ein beschreibbarer Kalender: Privat
    r = bauen(s).call("anlegen", {**NEU, "beschreibung": "Bitte Versichertenkarte mitnehmen", "__dry_run__": True})
    assert r["ok"] is True and r["dry_run"] is True and r["plugin"] == "kalender" and r["method"] == "anlegen"
    assert "Zahnreinigung" in r["would_execute"] and "'Privat'" in r["would_execute"] and "09:00" in r["would_execute"]
    assert r["expected_side_effects"] == ["Nextcloud / Privat: neuer Termin"]
    assert r["args_preview"] == {"titel": "Zahnreinigung", "start": "2026-10-15T09:00:00+02:00", "ende": "2026-10-15T10:00:00+02:00",
                                 "ganztaegig": False, "ort": "Praxis Süd", "kalender": "Privat", "beschreibung": "33 Zeichen"}
    assert "Versichertenkarte" not in json.dumps(r)  # die Beschreibung steht nur als Länge im Plan
    assert s.mit("PUT") == []
    # auch eine Bestätigung macht aus dem Trockenlauf keinen echten Lauf
    r = bauen(s).call("anlegen", {**NEU, "__dry_run__": True, "bestaetigt": True})
    assert r["dry_run"] is True and s.mit("PUT") == []


@pytest.mark.parametrize("bestaetigt", [None, False, "ja", "true", 1])
def test_echter_lauf_braucht_bestaetigt_true(bauen, caldav, bestaetigt):
    s = caldav([privat()])
    args = dict(NEU) if bestaetigt is None else {**NEU, "bestaetigt": bestaetigt}
    with pytest.raises(ValueError, match="bestaetigt"):
        bauen(s).call("anlegen", args)
    assert s.mit("PUT") == []


def test_anlegen_legt_den_termin_an_und_er_ist_gleich_sichtbar(bauen, caldav):
    s = caldav([privat(), feiertage()])
    p = bauen(s)
    assert titel(p.termine(von="2026-10-15", tage=1)) == []
    r = p.call("anlegen", {**NEU, "bestaetigt": True})
    assert r["ok"] is True
    a = r["angelegt"]
    assert re.fullmatch(ID + r":[0-9a-f-]{36}@fyos-kalender", a["id"])
    assert {k: v for k, v in a.items() if k != "id"} == {
        "titel": "Zahnreinigung", "start": "2026-10-15T09:00:00+02:00", "ende": "2026-10-15T10:00:00+02:00", "ganztaegig": False,
        "ort": "Praxis Süd", "kalender": "Privat", "konto": "Nextcloud"}
    assert r["satz"].startswith("Termin 'Zahnreinigung' angelegt (") and "Kalender 'Privat'" in r["satz"]
    (put,) = s.mit("PUT")
    assert put.kopf["if-none-match"] == "*"  # nie einen vorhandenen Termin überschreiben
    assert "DESCRIPTION" not in put.koerper.decode()  # ohne ausdrücklichen Text keine Beschreibung
    gefunden = p.termine(von="2026-10-15", tage=1)  # der Zwischenspeicher ist geleert
    assert titel(gefunden) == ["Zahnreinigung"] and gefunden["termine"][0]["ort"] == "Praxis Süd"


def test_anlegen_ganztaegig_und_mit_beschreibung(bauen, caldav):
    s = caldav([privat()])
    r = bauen(s).call("anlegen", {"titel": "Urlaub", "start": "2026-12-24", "ende": "2026-12-27", "beschreibung": "Skihütte",
                                  "bestaetigt": True})
    assert r["ok"] is True
    assert {k: r["angelegt"][k] for k in ("start", "ende", "ganztaegig")} == {"start": "2026-12-24", "ende": "2026-12-27", "ganztaegig": True}
    assert "DESCRIPTION:Skihütte" in s.mit("PUT")[0].koerper.decode()
    assert "ganztägig 2026-12-24 bis 2026-12-26" in r["satz"]


def test_anlegen_wahl_des_kalenders(bauen, caldav):
    s = caldav([privat(), arbeit(), feiertage()])
    p = bauen(s)
    with pytest.raises(ValueError, match=r"es gibt mehrere beschreibbare Kalender: 'Privat' \(Nextcloud:[0-9a-f]{8}\), 'Projekt' "):
        p.call("anlegen", {**NEU, "bestaetigt": True})
    with pytest.raises(ValueError, match="kein Kalender 'Gibt es nicht'; vorhanden: 'Privat', 'Projekt', 'Feiertage'"):
        p.call("anlegen", {**NEU, "kalender": "Gibt es nicht", "bestaetigt": True})
    with pytest.raises(ValueError, match="dieser Name passt auf mehrere Kalender: 'Privat'"):
        p.call("anlegen", {**NEU, "kalender": "r", "bestaetigt": True})  # Privat und Projekt
    with pytest.raises(ValueError, match="schreibgeschützt"):
        p.call("anlegen", {**NEU, "kalender": "Feiertage", "bestaetigt": True})
    assert s.mit("PUT") == []
    r = p.call("anlegen", {**NEU, "kalender": "projekt", "bestaetigt": True})
    assert r["ok"] is True and r["angelegt"]["kalender"] == "Projekt"
    assert s.mit("PUT")[0].pfad.startswith("/dav/calendars/alice/projekt/")


def test_anlegen_in_ein_abonnement_ist_nicht_moeglich(bauen, http, monkeypatch):
    feed = http(lambda a: Antwort(200, {"Content-Type": "text/calendar"}, ics(name="Abo").encode()))
    monkeypatch.setenv("FYOS_KALENDER_URL", feed.url + "/abo.ics")
    p = bauen()
    assert p.konten()["konten"][0]["art"] == "ics"
    with pytest.raises(ValueError, match="schreibgeschützt"):
        p.call("anlegen", {**NEU, "bestaetigt": True})
    assert [a.methode for a in feed.anfragen] == ["GET"]  # nichts geschrieben


def test_anlegen_ohne_kalender_fuer_termine(bauen, caldav):
    k = FakeKalender("/dav/calendars/alice/aufgaben/", "Aufgaben", komponenten=("VTODO",))  # nur Aufgaben, keine Termine
    s = caldav([k])
    p = bauen(s)
    r = p.call("anlegen", {**NEU, "bestaetigt": True})
    assert r["ok"] is False and "kein Kalender mit Terminen gefunden" in r["error"] and s.mit("PUT") == []
    assert p.konten()["konten"][0]["status"] == "fehler"  # und kalender.konten sagt es auch


def test_anlegen_ohne_einrichtung(bauen):
    assert bauen().call("anlegen", {**NEU, "bestaetigt": True}) == {"ok": False, "eingerichtet": False, "error": HINWEIS_EINRICHTEN}
    with pytest.raises(ValueError):  # Eingabefehler zuerst
        bauen().call("anlegen", {"titel": "", "start": "2026-10-15T09:00"})


def test_anlegen_prueft_die_eingabe_vor_dem_netz(bauen, caldav):
    s = caldav([privat()])
    p = bauen(s)
    for args in (
        {"start": "2026-10-15T09:00"}, {"titel": "x"}, {"titel": "x", "start": "bald"}, {"titel": "x" * 201, "start": "2026-10-15"},
        {"titel": "x", "start": "2026-10-15T09:00", "ende": "2026-10-15T08:00"}, {"titel": "x", "start": "2026-10-15", "ganztaegig": "ja"},
        {"titel": "x", "start": "2026-10-15T09:00", "farbe": "rot"}, {"titel": 5, "start": "2026-10-15"},
        {"titel": "x", "start": "2026-10-15T09:00", "ort": "o" * 201}, {"titel": "x", "start": "2026-10-15T09:00", "beschreibung": "b" * 2001},
    ):
        with pytest.raises(ValueError):
            p.call("anlegen", {**args, "bestaetigt": True})
    assert s.anfragen == []


def test_anlegen_server_lehnt_ab(bauen, caldav):
    s = caldav([privat()], put_status=403)
    r = bauen(s).call("anlegen", {**NEU, "bestaetigt": True})
    assert r["ok"] is False and "nimmt keine Termine an" in r["error"] and PASSWORT not in r["error"]
    assert len(s.mit("PUT")) == 1


def test_anlegen_bei_ausgefallenem_konto_waehlt_nicht_blind(bauen, caldav, monkeypatch):
    s, s2 = caldav([privat()]), caldav([arbeit()], nutzer="bob", passwort=ZWEITES)
    konto_eintragen(monkeypatch, s)
    weiteres_konto(monkeypatch, s2, passwort="falsches-passwort")
    p = bauen()
    # ohne Namen: Privat wäre der einzige sichtbare beschreibbare Kalender, aber das Konto „Arbeit“ lässt sich nicht lesen
    r = p.call("anlegen", {**NEU, "bestaetigt": True})
    assert r["ok"] is False and "nicht sicher wählen" in r["error"] and "Arbeit: Anmeldung abgelehnt" in r["error"]
    assert s.mit("PUT") == []
    # mit Namen eines erreichbaren Kalenders geht es
    assert p.call("anlegen", {**NEU, "kalender": "Privat", "bestaetigt": True})["ok"] is True
    # der Name eines Kalenders im ausgefallenen Konto: Fehler, kein „gibt es nicht“
    r = p.call("anlegen", {**NEU, "kalender": "Projekt", "bestaetigt": True})
    assert r["ok"] is False and "ein Konto nicht lesbar ist" in r["error"]


def test_anlegen_bei_unbrauchbarem_eintrag_waehlt_nicht_blind(bauen, caldav, monkeypatch):
    s = caldav([privat()])
    konto_eintragen(monkeypatch, s)
    monkeypatch.setenv("FYOS_KALENDER_KONTEN", json.dumps([{"name": "Arbeit", "url": "https://arbeit.example.com", "nutzer": "bob",
                                                            "passwort_env": "GIBT_ES_NICHT"}]))
    r = bauen().call("anlegen", {**NEU, "bestaetigt": True})
    assert r["ok"] is False and "Arbeit: Konfiguration fehlerhaft" in r["error"] and s.mit("PUT") == []


# ── Zwei Konten, Konfiguration ───────────────────────────────────────────────────────────────────────────
def test_zwei_caldav_konten_mit_getrennten_passwoertern(bauen, caldav, monkeypatch):
    s, s2 = caldav([privat()]), caldav([arbeit()], nutzer="bob", passwort=ZWEITES)
    konto_eintragen(monkeypatch, s)
    weiteres_konto(monkeypatch, s2)
    p = bauen()
    r = p.termine(tage=1)
    assert titel(r) == ["Frühstück", "Sprint-Planung"] and [t["konto"] for t in r["termine"]] == ["Nextcloud", "Arbeit"]
    assert s.abgelehnt == [] and s2.abgelehnt == []  # jedes Konto bekam sein eigenes Passwort
    assert all(a.kopf["authorization"] == basic("alice", PASSWORT) for a in s.anfragen)
    assert all(a.kopf["authorization"] == basic("bob", ZWEITES) for a in s2.anfragen)
    assert p.health() == {"ok": True, "status": "healthy", "konten": 2, "quelle_der_termine": False}


def test_konten_aus_der_config_des_plugins(bauen, caldav, monkeypatch):
    s = caldav([privat()])
    monkeypatch.setenv("FAMILIE_PW", PASSWORT)
    p = KalenderPlugin({"jetzt": lambda: JETZT, "konten": [{"name": "Familie", "url": s.url, "nutzer": "alice", "passwort_env": "FAMILIE_PW"}]})
    assert titel(p.termine(tage=1)) == ["Frühstück"] and p.termine(tage=1)["termine"][0]["konto"] == "Familie"
    kaputt = KalenderPlugin({"konten": [{"name": "Familie", "url": s.url, "nutzer": "alice", "passwort": "nicht-im-json"}]})
    assert kaputt.eingerichtet and "Passwörter gehören nicht in die Liste" in kaputt.konten()["konten"][0]["fehler"]


def test_zwischenspeicher_der_termine_gilt_60_sekunden(caldav, monkeypatch):
    s = caldav([privat()])
    konto_eintragen(monkeypatch, s)
    uhr = Uhr()
    p = KalenderPlugin({"jetzt": lambda: JETZT, "uhr": uhr})
    p.termine(tage=1)
    p.termine(tage=1)
    assert len(s.mit("REPORT")) == 1
    uhr.weiter(61)
    p.termine(tage=1)
    assert len(s.mit("REPORT")) == 2


# ── Passwort: nirgends ───────────────────────────────────────────────────────────────────────────────────
def test_das_passwort_steht_nirgends(bauen, caldav, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    s, s2 = caldav([privat(), feiertage()]), caldav([arbeit()], nutzer="bob", passwort=ZWEITES)
    konto_eintragen(monkeypatch, s)
    weiteres_konto(monkeypatch, s2, passwort="falsches-passwort")
    p = bauen()
    p.register()
    p.registry_anbinden(object())
    ausgaben = [
        p.konten(), p.konten(True), p.termine(), p.termine(kalender="Feiertage", von="2026-10-12", bis="2026-11-05"),
        p.call("anlegen", {**NEU, "kalender": "Privat", "__dry_run__": True}),
        p.call("anlegen", {**NEU, "kalender": "Privat", "bestaetigt": True}),
        p.call("anlegen", {**NEU, "kalender": "Projekt", "bestaetigt": True}),
        p.health(),
    ]
    try:
        p.ablage_termine(JETZT, JETZT + timedelta(days=1))
    except Exception as e:  # noqa: BLE001 — der Text des Fehlers ist hier der Gegenstand
        ausgaben.append(str(e))
    p.shutdown()
    alles = json.dumps(ausgaben, ensure_ascii=False, default=str) + caplog.text + repr(p) + repr(p._quellen) + str(p.config)
    for geheim in (PASSWORT, ZWEITES, "falsches-passwort", basic("alice", PASSWORT), basic("bob", ZWEITES)):
        assert geheim not in alles, geheim
    for q in p._quellen:
        assert PASSWORT not in repr(q.konto) and ZWEITES not in repr(q.konto) and PASSWORT not in repr(q._sitzung)


# ── Ablage: was der Kern aufruft ─────────────────────────────────────────────────────────────────────────
def fenster(tage: int = 7, start: datetime | None = None) -> tuple[datetime, datetime]:
    a = start or datetime(2026, 10, 12, tzinfo=BERLIN)
    return a, a + timedelta(days=tage)


def test_ablage_kalender_und_termine(bauen, caldav):
    s = caldav([privat(), feiertage()])
    p = bauen(s)
    kalender = p.ablage_kalender()
    assert [(k["name"], k["konto"], k["schreibbar"]) for k in kalender] == [("Privat", "Nextcloud", True), ("Feiertage", "Nextcloud", False)]
    assert all(set(k) == {"id", "name", "konto", "schreibbar"} for k in kalender)
    liste = p.ablage_termine(*fenster())
    assert [t["titel"] for t in liste] == ["Frühstück", "Zahnarzt", "Urlaub"]
    assert [t["titel"] for t in p.ablage_termine(*fenster(30), kalender="Feiertage")] == ["Reformationstag"]
    assert [t["titel"] for t in p.ablage_termine(*fenster(1))] == ["Frühstück"]


def test_ablage_fenster_bis_366_tage_und_ein_jahr_voraus(bauen, caldav):
    s = caldav([privat()])
    p = bauen(s)
    assert [t["titel"] for t in p.ablage_termine(*fenster(366))] == ["Frühstück", "Zahnarzt", "Urlaub", "Später im Jahr"]
    with pytest.raises(ValueError, match="366"):
        p.ablage_termine(*fenster(367))
    with pytest.raises(ValueError):
        p.ablage_termine(datetime(2026, 10, 13, tzinfo=BERLIN), datetime(2026, 10, 12, tzinfo=BERLIN))


def test_ablage_ist_streng_ein_fehlendes_konto_ist_ein_fehler_nie_eine_luecke(bauen, caldav, monkeypatch):
    s, s2 = caldav([privat()]), caldav([arbeit()], nutzer="bob", passwort=ZWEITES)
    konto_eintragen(monkeypatch, s)
    weiteres_konto(monkeypatch, s2, passwort="falsches-passwort")
    p = bauen()
    for aufruf in (p.ablage_kalender, lambda: p.ablage_termine(*fenster()), lambda: p.ablage_termine(*fenster(), kalender="Projekt")):
        with pytest.raises(kern.QuelleFehler) as e:
            aufruf()
        assert e.value.status == 503
        assert "Kalender-Konto 'Arbeit': Anmeldung abgelehnt" in str(e.value) and "falsches-passwort" not in str(e.value)
    # nennt der Aufrufer einen Kalender eines erreichbaren Kontos, ist die Antwort vollständig: der Ausfall betrifft ihn nicht
    assert [t["titel"] for t in p.ablage_termine(*fenster(), kalender="Privat")] == ["Frühstück", "Zahnarzt", "Urlaub"]


def test_ablage_unbrauchbarer_eintrag_ist_ein_fehler(bauen, monkeypatch):
    monkeypatch.setenv("FYOS_KALENDER_URL", "ftp://kalender.example.com/")
    p = bauen()
    for aufruf in (p.ablage_kalender, lambda: p.ablage_termine(*fenster())):
        with pytest.raises(kern.QuelleFehler, match="Konfiguration fehlerhaft"):
            aufruf()


def test_ablage_ohne_einrichtung_ist_nicht_eingerichtet(bauen):
    p = bauen()
    for aufruf in (p.ablage_kalender, lambda: p.ablage_termine(*fenster()), lambda: p.ablage_anlegen(dict(NEU))):
        with pytest.raises(kern.NichtEingerichtet) as e:
            aufruf()
        assert e.value.status == 501 and "FYOS_KALENDER_URL" in str(e.value)
    assert issubclass(kern.NichtEingerichtet, kern.QuelleFehler)


def test_ablage_anlegen(bauen, caldav):
    s = caldav([privat(), feiertage()])
    p = bauen(s)
    a = p.ablage_anlegen({"titel": "Zahnreinigung", "start": "2026-10-15T09:00", "ende": "2026-10-15T10:00", "ganztaegig": False,
                          "ort": "", "beschreibung": "", "kalender": ""})
    assert a["titel"] == "Zahnreinigung" and a["kalender"] == "Privat" and re.fullmatch(ID + r":[0-9a-f-]{36}@fyos-kalender", a["id"])
    assert [t["titel"] for t in p.ablage_termine(*fenster(1, datetime(2026, 10, 15, tzinfo=BERLIN)))] == ["Zahnreinigung"]
    with pytest.raises(ValueError):
        p.ablage_anlegen({"titel": "", "start": "2026-10-15T09:00"})
    with pytest.raises(ValueError, match="schreibgeschützt"):
        p.ablage_anlegen({"titel": "x", "start": "2026-10-15T09:00", "kalender": "Feiertage"})


@pytest.mark.parametrize("put_status,erwartet", [(403, 502), (409, 502), (500, 502)])
def test_ablage_anlegen_fehler_sind_quelle_fehler(bauen, caldav, put_status, erwartet):
    s = caldav([privat()], put_status=put_status)
    with pytest.raises(kern.QuelleFehler) as e:
        bauen(s).ablage_anlegen(dict(NEU))
    assert e.value.status == erwartet and PASSWORT not in str(e.value)


def test_ablage_anlegen_server_weg_ist_503(bauen, caldav):
    s = caldav([privat()])
    p = bauen(s)
    p.ablage_kalender()
    s.stop()
    with pytest.raises(kern.QuelleFehler) as e:
        p.ablage_anlegen(dict(NEU))
    assert e.value.status == 503


# ── Der echte Kern ───────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def termine_modul():
    return pytest.importorskip("mcp_hub.termine", reason="fyos-hub ohne Termin-Quelle (vor 0.1.1 mit termine.py)")


def test_kern_ohne_konto_bleibt_nicht_eingerichtet(bauen, termine_modul):
    p = bauen()
    p.registry_anbinden(object())
    q = termine_modul.quelle()
    assert q.art == "keine" and not q.eingerichtet
    with pytest.raises(termine_modul.NichtEingerichtet):
        q.termine(*fenster())


def test_kern_liest_die_termine_des_plugins(bauen, caldav, termine_modul):
    s = caldav([privat(), feiertage()])
    p = bauen(s)
    p.registry_anbinden(object())  # so ruft der Hub es beim Start
    assert termine_modul.angemeldete_ablage() is p
    q = termine_modul.quelle()
    assert q.art == "ablage" and q.eingerichtet and "Kalender-Plugin kalender" in q.beschreibung()
    assert [(k["name"], k["schreibbar"]) for k in q.kalender()] == [("Privat", True), ("Feiertage", False)]
    termine = q.termine(*fenster())
    assert [t["titel"] for t in termine] == ["Frühstück", "Zahnarzt", "Urlaub"]
    assert set(termine[0]) == {"id", "titel", "start", "ende", "ganztaegig", "ort", "kalender", "konto"}
    assert [t["titel"] for t in q.termine("2026-10-30", "2026-11-02", "Feiertage")] == ["Reformationstag"]  # ISO-Text geht auch
    assert q.termine(*fenster(1, datetime(2026, 12, 1, tzinfo=BERLIN))) == []  # „keine Termine“ ist die leere Liste


def test_kern_drei_zustaende_fehler_ist_nie_keine_termine(bauen, caldav, termine_modul):
    s = caldav([privat()], passwort="ein-anderes-passwort")
    p = bauen(s)
    p.registry_anbinden(object())
    q = termine_modul.quelle()
    with pytest.raises(termine_modul.QuelleFehler) as e:
        q.termine(*fenster())
    assert e.value.status == 503 and "Kalender-Konto 'Nextcloud'" in str(e.value) and not isinstance(e.value, termine_modul.NichtEingerichtet)
    with pytest.raises(termine_modul.QuelleFehler):
        q.kalender()


def test_kern_unbrauchbarer_eintrag_meldet_sich_trotzdem_als_quelle(bauen, termine_modul, monkeypatch):
    monkeypatch.setenv("FYOS_KALENDER_URL", "ftp://kalender.example.com/")
    p = bauen()
    p.registry_anbinden(object())
    q = termine_modul.quelle()
    assert q.art == "ablage"  # nicht „nicht eingerichtet“: der Eintrag ist da, nur kaputt
    with pytest.raises(termine_modul.QuelleFehler, match="Konfiguration fehlerhaft") as e:
        q.termine(*fenster())
    assert not isinstance(e.value, termine_modul.NichtEingerichtet)


def test_kern_legt_termine_an_und_prueft_vorher(bauen, caldav, termine_modul):
    s = caldav([privat()])
    p = bauen(s)
    p.registry_anbinden(object())
    q = termine_modul.quelle()
    a = q.anlegen({"titel": "Zahnreinigung", "start": "2026-10-15T09:00", "ort": "Praxis Süd"})
    assert a["titel"] == "Zahnreinigung" and a["ende"] == "2026-10-15T10:00:00+02:00" and a["kalender"] == "Privat"
    with pytest.raises(ValueError):
        q.anlegen({"titel": "", "start": "2026-10-15T09:00"})
    assert len(s.mit("PUT")) == 1


def test_kern_dienst_geht_der_ablage_vor_und_abmelden_raeumt_auf(bauen, caldav, termine_modul):
    s = caldav([privat()])
    p = bauen(s)
    p.registry_anbinden(object())
    termine_modul.dienst_anmelden("http://127.0.0.1:9")
    assert termine_modul.quelle().art == "dienst"  # die Ausbaustufe (eigener Dienst) hat Vorrang
    termine_modul.dienst_anmelden(None)
    assert termine_modul.quelle().art == "ablage"
    p.shutdown()
    assert termine_modul.angemeldete_ablage() is None and termine_modul.quelle().art == "keine"
    assert p.health()["quelle_der_termine"] is False


def test_shutdown_nimmt_eine_andere_ablage_nicht_weg(bauen, caldav, termine_modul):
    s = caldav([privat()])
    p = bauen(s)
    p.registry_anbinden(object())

    class Anderer:
        pass

    anderer = Anderer()
    termine_modul.ablage_anmelden(anderer)
    p.shutdown()
    assert termine_modul.angemeldete_ablage() is anderer


def test_health_nennt_ob_das_plugin_die_quelle_der_termine_ist(bauen, caldav, termine_modul):
    s = caldav([privat()])
    p = bauen(s)
    assert p.health()["quelle_der_termine"] is False
    p.registry_anbinden(object())
    assert p.health() == {"ok": True, "status": "healthy", "konten": 1, "quelle_der_termine": True}


# ── Ein Kern ohne Termin-Quelle (fyos-hub 0.1.0) ─────────────────────────────────────────────────────────
@pytest.fixture
def kern_ohne_termine(monkeypatch):
    """Eine Kopie von ``kern.py``, die ``mcp_hub.termine`` nicht findet: so sieht das Plugin einen älteren Kern."""
    import mcp_hub

    monkeypatch.setitem(sys.modules, "mcp_hub.termine", None)
    monkeypatch.delattr(mcp_hub, "termine", raising=False)
    spec = importlib.util.spec_from_file_location("kern_ohne_termine", kern.__file__)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    monkeypatch.setattr(plugin_modul, "kern", modul)
    return modul


def test_ersatzklassen_ohne_kern_modul(kern_ohne_termine):
    k = kern_ohne_termine
    assert k.modul() is None and k.verfuegbar() is False
    assert k.anmelden(object()) is False
    k.abmelden(object())  # wirft nicht
    fehler = k.QuelleFehler("kaputt", 503)
    assert isinstance(fehler, RuntimeError) and fehler.status == 503 and str(fehler) == "kaputt"
    assert k.QuelleFehler("x").status == 502
    nicht = k.NichtEingerichtet()
    assert isinstance(nicht, k.QuelleFehler) and nicht.status == 501 and "Kein Kalender" in str(nicht)


def test_werkzeuge_laufen_auch_ohne_termin_quelle(bauen, caldav, kern_ohne_termine):
    s = caldav([privat()])
    p = bauen(s)
    p.register()
    p.registry_anbinden(object())  # wirft nicht, meldet sich nirgends an
    assert p.health()["quelle_der_termine"] is False
    assert titel(p.termine(tage=1)) == ["Frühstück"] and p.konten()["konten"][0]["status"] == "ok"
    assert p.call("anlegen", {**NEU, "bestaetigt": True})["ok"] is True
    p.shutdown()


def test_ablage_methoden_werfen_dann_die_ersatzklassen(bauen, caldav, kern_ohne_termine, monkeypatch):
    s = caldav([privat()], passwort="ein-anderes-passwort")
    p = bauen(s)
    with pytest.raises(kern_ohne_termine.QuelleFehler) as e:
        p.ablage_termine(*fenster())
    assert e.value.status == 503
    monkeypatch.delenv("FYOS_KALENDER_URL")
    with pytest.raises(kern_ohne_termine.NichtEingerichtet) as e:
        bauen().ablage_kalender()
    assert e.value.status == 501
