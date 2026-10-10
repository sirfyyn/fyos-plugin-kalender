"""Konten aus Umgebung, JSON-Liste und Plugin-Konfiguration."""

from __future__ import annotations

import json

import pytest

from fyos_plugin_kalender.konten import KONTEN_MAX, Konto, konten_lesen

PASSWORT = "p4ss-w0rt-nur-im-test"


def lesen(**umgebung: str) -> list[Konto]:
    return konten_lesen(umgebung)


def test_ein_konto_aus_der_umgebung():
    (k,) = lesen(FYOS_KALENDER_URL="https://cloud.example.com/remote.php/dav", FYOS_KALENDER_NUTZER="anna.muster",
                 FYOS_KALENDER_PASSWORT=PASSWORT)
    assert (k.name, k.art, k.url, k.nutzer, k.passwort, k.fehler) == (
        "cloud.example.com", "caldav", "https://cloud.example.com/remote.php/dav", "anna.muster", PASSWORT, "")


def test_ohne_url_kein_konto():
    assert lesen() == []
    assert lesen(FYOS_KALENDER_URL="  ") == []
    assert lesen(FYOS_KALENDER_NUTZER="x", FYOS_KALENDER_PASSWORT="y") == []


@pytest.mark.parametrize(
    "url,erwartet",
    [
        ("https://caldav.example.org", "example.org"),
        ("https://www.example.org/dav/", "example.org"),
        ("https://dav.example.org:8443/", "example.org"),  # der Port steht nicht im Namen
        ("https://calendar.example.org", "example.org"),
        ("https://example.org", "example.org"),
        ("https://caldav.org", "caldav.org"),  # kein Vorsatz, wenn nichts übrig bliebe
    ],
)
def test_name_aus_dem_host(url, erwartet):
    (k,) = lesen(FYOS_KALENDER_URL=url, FYOS_KALENDER_NUTZER="n", FYOS_KALENDER_PASSWORT=PASSWORT)
    assert k.name == erwartet and not k.fehler


def test_eigener_name():
    (k,) = lesen(FYOS_KALENDER_URL="https://cloud.example.com/dav", FYOS_KALENDER_NUTZER="n",
                 FYOS_KALENDER_PASSWORT=PASSWORT, FYOS_KALENDER_NAME="  Privat  ")
    assert k.name == "Privat"


@pytest.mark.parametrize("name", ["a:b", "a#b", "a/b", "a\\b", "x" * 61])
def test_unzulaessiger_name_macht_das_konto_unbrauchbar(name):
    (k,) = lesen(FYOS_KALENDER_URL="https://cloud.example.com/dav", FYOS_KALENDER_NUTZER="n",
                 FYOS_KALENDER_PASSWORT=PASSWORT, FYOS_KALENDER_NAME=name)
    assert k.fehler and "name" in k.fehler


def test_webcal_ist_ein_abonnement_ueber_https():
    (k,) = lesen(FYOS_KALENDER_URL="webcal://calendar.example.net/feiertage.ics")
    assert (k.art, k.url, k.nutzer, k.passwort, k.fehler) == ("ics", "https://calendar.example.net/feiertage.ics", "", "", "")
    (k,) = lesen(FYOS_KALENDER_URL="webcals://calendar.example.net/x/basic.ics")
    assert k.art == "ics" and k.url.startswith("https://")


def test_ics_endung_ohne_nutzer_ist_ein_abonnement_mit_nutzer_caldav():
    (k,) = lesen(FYOS_KALENDER_URL="https://calendar.example.net/ical/abc/basic.ics")
    assert k.art == "ics"
    (k,) = lesen(FYOS_KALENDER_URL="https://calendar.example.net/ical/abc/basic.ics", FYOS_KALENDER_NUTZER="n",
                 FYOS_KALENDER_PASSWORT=PASSWORT)
    assert k.art == "caldav"
    (k,) = lesen(FYOS_KALENDER_URL="https://calendar.example.net/ical/abc/basic.ics", FYOS_KALENDER_NUTZER="n",
                 FYOS_KALENDER_PASSWORT=PASSWORT, FYOS_KALENDER_ART="ics")
    assert k.art == "ics" and k.nutzer == "n"


def test_art_wird_geprueft():
    (k,) = lesen(FYOS_KALENDER_URL="https://cloud.example.com/dav", FYOS_KALENDER_NUTZER="n",
                 FYOS_KALENDER_PASSWORT=PASSWORT, FYOS_KALENDER_ART="exchange")
    assert "caldav oder ics" in k.fehler


def test_caldav_braucht_nutzer_und_passwort_und_sagt_welche_felder():
    (k,) = lesen(FYOS_KALENDER_URL="https://cloud.example.com/dav", FYOS_KALENDER_NUTZER="n")
    assert "FYOS_KALENDER_PASSWORT" in k.fehler and "FYOS_KALENDER_NUTZER" in k.fehler
    (k,) = lesen(FYOS_KALENDER_URL="https://cloud.example.com/dav", FYOS_KALENDER_PASSWORT=PASSWORT)
    assert k.fehler


def test_ics_mit_nutzer_ohne_passwort_ist_unvollstaendig():
    (k,) = lesen(FYOS_KALENDER_URL="https://calendar.example.net/x.ics", FYOS_KALENDER_ART="ics", FYOS_KALENDER_NUTZER="n")
    assert "zusammen" in k.fehler


@pytest.mark.parametrize(
    "url",
    [
        "ftp://cloud.example.com/dav",
        "cloud.example.com/dav",
        "file:///etc/passwd",
        "https://",
        "https://cloud.example.com:99999/dav",
        "https://cloud.example.com/da v",
        "https://nutzer:geheim" + "@" + "cloud.example.com/dav",  # mit Zugang in der Adresse
        "https://" + "a" * 2100 + ".example.com",
    ],
)
def test_ungueltige_adressen_sind_unbrauchbar_und_stehen_nicht_im_fehlertext(url):
    (k,) = lesen(FYOS_KALENDER_URL=url, FYOS_KALENDER_NUTZER="n", FYOS_KALENDER_PASSWORT=PASSWORT)
    assert k.fehler
    assert "geheim" not in k.fehler and PASSWORT not in k.fehler and "etc/passwd" not in k.fehler


def test_passwort_steht_in_keiner_darstellung():
    (k,) = lesen(FYOS_KALENDER_URL="https://cloud.example.com/dav", FYOS_KALENDER_NUTZER="n", FYOS_KALENDER_PASSWORT=PASSWORT)
    for text in (repr(k), str(k), json.dumps(k.als_dict()), str(k.als_dict())):
        assert PASSWORT not in text
    assert k.als_dict() == {"name": "cloud.example.com", "art": "caldav", "adresse": "https://cloud.example.com", "nutzer": "n"}


def test_adresse_zeigt_nie_den_pfad():
    """Bei ICS-Abonnements ist der Pfad das Geheimnis (die „geheime Adresse“ von Google und Outlook)."""
    (k,) = lesen(FYOS_KALENDER_URL="https://calendar.example.net:8443/ical/GEHEIMER-TEIL/basic.ics")
    assert k.adresse == "https://calendar.example.net:8443"
    assert "GEHEIMER" not in json.dumps(k.als_dict())


# ── mehrere Konten ───────────────────────────────────────────────────────────────────────────────────────
def liste(*eintraege: dict) -> str:
    return json.dumps(list(eintraege))


def test_konten_aus_json_mit_passwort_aus_der_umgebung():
    konten = lesen(
        FYOS_KALENDER_URL="https://cloud.example.com/dav", FYOS_KALENDER_NUTZER="n", FYOS_KALENDER_PASSWORT=PASSWORT,
        FYOS_KALENDER_NAME="Privat",
        FYOS_KALENDER_KONTEN=liste(
            {"name": "Arbeit", "art": "caldav", "url": "https://dav.example.org/", "nutzer": "a", "passwort_env": "ARBEIT_PW"},
            {"name": "Feiertage", "url": "webcal://calendar.example.net/feiertage.ics"},
        ),
        ARBEIT_PW="arbeit-pw",
    )
    assert [k.name for k in konten] == ["Privat", "Arbeit", "Feiertage"]
    assert [k.art for k in konten] == ["caldav", "caldav", "ics"]
    assert konten[1].passwort == "arbeit-pw" and not any(k.fehler for k in konten)


def test_passwort_im_json_wird_abgelehnt_ohne_es_zu_zeigen():
    (k,) = lesen(FYOS_KALENDER_KONTEN=liste(
        {"name": "Arbeit", "url": "https://dav.example.org/", "nutzer": "a", "passwort": "im-json-verboten"}))
    assert k.name == "Arbeit" and "passwort_env" in k.fehler
    assert "im-json-verboten" not in k.fehler and not k.passwort
    (k,) = lesen(FYOS_KALENDER_KONTEN=liste({"name": "A", "url": "https://dav.example.org/", "password": "x"}))
    assert k.fehler


def test_passwort_env_muss_gesetzt_und_ein_gueltiger_name_sein():
    (k,) = lesen(FYOS_KALENDER_KONTEN=liste({"name": "A", "url": "https://dav.example.org/", "nutzer": "a", "passwort_env": "FEHLT_PW"}))
    assert "FEHLT_PW" in k.fehler and "nicht gesetzt" in k.fehler
    (k,) = lesen(FYOS_KALENDER_KONTEN=liste({"name": "A", "url": "https://dav.example.org/", "nutzer": "a", "passwort_env": "a b; rm"}))
    assert "Umgebungsvariablen" in k.fehler
    (k,) = lesen(FYOS_KALENDER_KONTEN=liste({"name": "A", "url": "https://dav.example.org/", "nutzer": "a", "passwort_env": 5}))
    assert k.fehler


def test_unbekannte_felder_und_falsche_formen_im_json():
    (k,) = lesen(FYOS_KALENDER_KONTEN=liste({"name": "A", "url": "https://dav.example.org/", "nutzer": "a", "passwort_env": "X", "extra": 1}), X="p")
    assert "unbekannte Felder: extra" in k.fehler
    (k,) = lesen(FYOS_KALENDER_KONTEN="[1]")
    assert "Objekt" in k.fehler
    (k,) = lesen(FYOS_KALENDER_KONTEN="{nicht json")
    assert k.name == "FYOS_KALENDER_KONTEN" and "JSON" in k.fehler
    (k,) = lesen(FYOS_KALENDER_KONTEN='{"name": "A"}')
    assert "Liste" in k.fehler


def test_ein_kaputter_eintrag_stoert_die_anderen_nicht():
    konten = lesen(FYOS_KALENDER_KONTEN=liste(
        {"name": "Gut", "url": "webcal://calendar.example.net/feiertage.ics"},
        {"name": "Schlecht", "url": "ftp://x"},
        {"name": "Auch gut", "url": "https://calendar.example.net/b.ics"},
    ))
    assert [(k.name, bool(k.fehler)) for k in konten] == [("Gut", False), ("Schlecht", True), ("Auch gut", False)]


def test_doppelte_namen_ohne_beachtung_der_schreibweise():
    konten = lesen(FYOS_KALENDER_KONTEN=liste(
        {"name": "Privat", "url": "webcal://calendar.example.net/a.ics"},
        {"name": "privat", "url": "webcal://calendar.example.net/b.ics"},
    ))
    assert konten[0].fehler == "" and "vergeben" in konten[1].fehler
    assert konten[0].name.casefold() != konten[1].name.casefold()


def test_hoechstens_zwanzig_konten():
    konten = lesen(FYOS_KALENDER_KONTEN=liste(*({"name": f"K{i}", "url": f"webcal://calendar.example.net/{i}.ics"} for i in range(30))))
    assert len(konten) == KONTEN_MAX


def test_konten_aus_der_plugin_konfiguration():
    konten = konten_lesen({"X_PW": "pw"}, {"konten": [{"name": "Arbeit", "url": "https://dav.example.org/", "nutzer": "a", "passwort_env": "X_PW"}]})
    assert [(k.name, k.passwort) for k in konten] == [("Arbeit", "pw")]
    (k,) = konten_lesen({}, {"konten": "kein-Liste"})
    assert k.fehler
