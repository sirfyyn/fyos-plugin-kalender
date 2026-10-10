"""CalDAV-Bausteine: Anfragekörper und das Lesen der Antworten (ohne Netz)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from fyos_plugin_kalender import caldav
from fyos_plugin_kalender.netz import NetzFehler

KOPF = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/">'
ENDE = "</d:multistatus>"


def antwort(*teile: str) -> bytes:
    return (KOPF + "".join(teile) + ENDE).encode()


def test_anfragekoerper_sind_wohlgeformt_und_fragen_das_richtige():
    for koerper in (caldav.ERKENNEN, caldav.ZUHAUSE, caldav.KALENDER_LISTE):
        ET.fromstring(koerper)
    erkennen = caldav.ERKENNEN.decode()
    for name in ("resourcetype", "displayname", "current-user-principal", "calendar-home-set",
                 "supported-calendar-component-set", "current-user-privilege-set"):
        assert name in erkennen
    assert "calendar-home-set" in caldav.ZUHAUSE.decode() and "displayname" not in caldav.ZUHAUSE.decode()


def test_report_koerper_mit_und_ohne_expand():
    von = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)
    bis = datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc)
    ohne = caldav.report_koerper(von, bis, False)
    mit = caldav.report_koerper(von, bis, True)
    for koerper in (ohne, mit):
        wurzel = ET.fromstring(koerper)
        zeitraum = wurzel.find(".//{urn:ietf:params:xml:ns:caldav}time-range")
        assert zeitraum.get("start") == "20261011T220000Z" and zeitraum.get("end") == "20261018T220000Z"
        assert wurzel.find(".//{urn:ietf:params:xml:ns:caldav}comp-filter[@name='VEVENT']") is not None
    assert ET.fromstring(ohne).find(".//{urn:ietf:params:xml:ns:caldav}expand") is None
    expand = ET.fromstring(mit).find(".//{urn:ietf:params:xml:ns:caldav}expand")
    assert expand.get("start") == "20261011T220000Z" and expand.get("end") == "20261018T220000Z"


def test_report_koerper_rechnet_ortszeit_nach_utc():
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    koerper = caldav.report_koerper(datetime(2026, 10, 12, tzinfo=berlin), datetime(2026, 10, 13, tzinfo=berlin), False).decode()
    assert 'start="20261011T220000Z"' in koerper and 'end="20261012T220000Z"' in koerper


def test_kopf_xml():
    assert caldav.kopf_xml("1")["Depth"] == "1" and "xml" in caldav.kopf_xml()["Content-Type"]
    assert "Depth" not in caldav.kopf_xml()


# ── PROPFIND lesen ───────────────────────────────────────────────────────────────────────────────────────
KALENDER = """
<d:response><d:href>/dav/calendars/anna/privat/</d:href>
 <d:propstat><d:prop>
  <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
  <d:displayname>  Privat
     und mehr  </d:displayname>
  <c:supported-calendar-component-set><c:comp name="VEVENT"/><c:comp name="VTODO"/></c:supported-calendar-component-set>
  <d:current-user-privilege-set><d:privilege><d:read/></d:privilege><d:privilege><d:write/></d:privilege></d:current-user-privilege-set>
 </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 <d:propstat><d:prop><d:current-user-principal/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
</d:response>
"""


def test_kalender_eintrag():
    (e,) = caldav.propfind_eintraege(antwort(KALENDER))
    assert (e.href, e.kalender, e.sammlung, e.name) == ("/dav/calendars/anna/privat/", True, True, "Privat und mehr")
    assert e.komponenten == {"VEVENT", "VTODO"} and e.nimmt_termine
    assert e.rechte == {"read", "write"} and e.schreibbar is True
    assert e.principal == "" and not e.nicht_angemeldet


def test_rechte_alle_schreiben_oder_binden_gelten_als_schreibbar_nur_lesen_nicht():
    def rechte(*namen: str):
        privilegien = "".join(f"<d:privilege><d:{n}/></d:privilege>" for n in namen)
        return caldav.propfind_eintraege(antwort(
            f"<d:response><d:href>/k/</d:href><d:propstat><d:prop><d:current-user-privilege-set>{privilegien}"
            "</d:current-user-privilege-set></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"))[0].schreibbar
    assert rechte("all") is True and rechte("write") is True and rechte("bind") is True
    assert rechte("read") is False and rechte("read", "read-acl") is False
    ohne = caldav.propfind_eintraege(antwort("<d:response><d:href>/k/</d:href></d:response>"))[0]
    assert ohne.schreibbar is None and ohne.komponenten is None and ohne.nimmt_termine  # der Server sagt nichts: vielleicht


def test_aufgabenkalender_nimmt_keine_termine():
    xml = ("<d:response><d:href>/a/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
           '<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set></d:prop>'
           "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
    (e,) = caldav.propfind_eintraege(antwort(xml))
    assert e.kalender and not e.nimmt_termine


def test_principal_und_calendar_home_set():
    xml = ("<d:response><d:href>/</d:href><d:propstat><d:prop>"
           "<d:current-user-principal><d:href>/dav/principals/anna/</d:href></d:current-user-principal>"
           "<c:calendar-home-set><d:href>/dav/calendars/anna/</d:href><d:href> https://p1.example.com/anna/ </d:href></c:calendar-home-set>"
           "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
    (e,) = caldav.propfind_eintraege(antwort(xml))
    assert e.principal == "/dav/principals/anna/" and e.zuhause == ["/dav/calendars/anna/", "https://p1.example.com/anna/"]


def test_nicht_angemeldet_wird_erkannt():
    xml = ("<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:unauthenticated/></d:current-user-principal>"
           "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
    (e,) = caldav.propfind_eintraege(antwort(xml))
    assert e.nicht_angemeldet and e.principal == ""


def test_lange_namen_werden_gekuerzt():
    xml = (f"<d:response><d:href>/k/</d:href><d:propstat><d:prop><d:displayname>{'x' * 500}</d:displayname></d:prop>"
           "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
    assert len(caldav.propfind_eintraege(antwort(xml))[0].name) == 200


def test_mehrere_eintraege_in_reihenfolge():
    eintraege = caldav.propfind_eintraege(antwort(
        "<d:response><d:href>/a/</d:href></d:response>", "<d:response><d:href>/b/</d:href></d:response>"))
    assert [e.href for e in eintraege] == ["/a/", "/b/"]


# ── XML-Sicherheit ───────────────────────────────────────────────────────────────────────────────────────
BOMBE = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<d:multistatus xmlns:d="DAV:"><d:response><d:href>&lol2;</d:href></d:response></d:multistatus>"""


@pytest.mark.parametrize(
    "roh",
    [
        BOMBE,
        b'<?xml version="1.0"?><!DOCTYPE x SYSTEM "file:///etc/passwd"><d:multistatus xmlns:d="DAV:"/>',
        b'<!doctype x><d:multistatus xmlns:d="DAV:"/>',
        b'<d:multistatus xmlns:d="DAV:"><!ENTITY x "y"></d:multistatus>',
    ],
)
def test_dokumenttyp_und_entitaeten_werden_abgelehnt(roh):
    with pytest.raises(NetzFehler) as e:
        caldav.propfind_eintraege(roh)
    assert e.value.art == "antwort" and "Entitäten" in e.value.text


@pytest.mark.parametrize("roh", [b"", b"<html><body>Anmeldeseite", b"kein xml", b"<a><b></a>"])
def test_unlesbares_xml_ist_ein_verstaendlicher_fehler(roh):
    with pytest.raises(NetzFehler) as e:
        caldav.propfind_eintraege(roh)
    assert e.value.art == "antwort" and "CalDAV" in e.value.text


def test_gewoehnliche_entitaeten_im_text_sind_in_ordnung():
    xml = ("<d:response><d:href>/k/</d:href><d:propstat><d:prop><d:displayname>Tom &amp; Jerry &lt;3</d:displayname></d:prop>"
           "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
    assert caldav.propfind_eintraege(antwort(xml))[0].name == "Tom & Jerry <3"


# ── REPORT lesen ─────────────────────────────────────────────────────────────────────────────────────────
def test_report_daten_liefert_texte_und_fehlerstatus():
    daten, fehler = caldav.report_daten(antwort(
        "<d:response><d:href>/k/a.ics</d:href><d:propstat><d:prop><c:calendar-data>BEGIN:VCALENDAR\r\nEND:VCALENDAR</c:calendar-data></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
        "<d:response><d:href>/k/b.ics</d:href><d:propstat><d:prop><c:calendar-data/></d:prop>"
        "<d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat></d:response>",
        "<d:response><d:href>/k/c.ics</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>",
        "<d:response><d:href>/k/d.ics</d:href><d:propstat><d:prop><d:getetag>\"1\"</d:getetag></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
    ))
    assert daten == ["BEGIN:VCALENDAR\nEND:VCALENDAR"] and fehler == [403, 404]  # XML macht aus CRLF ein LF


def test_report_ohne_termine_ist_leer_und_kein_fehler():
    assert caldav.report_daten(antwort()) == ([], [])


# ── Adressen ─────────────────────────────────────────────────────────────────────────────────────────────
def test_aufloesen_und_namen():
    assert caldav.aufloesen("https://cloud.example.com/dav/", "/dav/calendars/a/") == "https://cloud.example.com/dav/calendars/a/"
    assert caldav.aufloesen("https://cloud.example.com/dav/", "calendars/a/") == "https://cloud.example.com/dav/calendars/a/"
    assert caldav.aufloesen("https://cloud.example.com/dav/", "https://p1.example.com:443/x/") == "https://p1.example.com:443/x/"
    assert caldav.name_aus_pfad("https://cloud.example.com/dav/calendars/a/mein%20kalender/") == "mein kalender"
    assert caldav.name_aus_pfad("https://cloud.example.com") == ""
