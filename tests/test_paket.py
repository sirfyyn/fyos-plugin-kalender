"""Das Paket: Entry-Points, Skill, Version, Beispiel-Konfiguration und dass die README-Dateien die Einstellungen nennen."""

from __future__ import annotations

import importlib
import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from fyos_plugin_kalender import __version__
from fyos_plugin_kalender.plugin import KalenderPlugin
from mcp_hub import registry as hub_registry
from mcp_hub.config import HubConfig, PluginConfig
from mcp_hub.plugin import HubPlugin

WURZEL = Path(__file__).resolve().parent.parent
PAKET = WURZEL / "fyos_plugin_kalender"


def pyproject() -> dict:
    return tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))


# ── Anmeldung beim Hub ───────────────────────────────────────────────────────────────────────────────────
def test_entry_point_steht_in_der_pyproject_und_ist_ladbar():
    ziel = pyproject()["project"]["entry-points"]["mcp_hub.plugins"]["kalender"]
    modul, klasse = ziel.split(":")
    cls = getattr(importlib.import_module(modul), klasse)
    assert issubclass(cls, HubPlugin) and cls.name == "kalender"


def test_hub_findet_das_installierte_paket_per_entry_point():
    eps = [ep for ep in importlib.metadata.entry_points(group="mcp_hub.plugins") if ep.name == "kalender"]
    if not eps:
        pytest.skip("Paket nicht installiert (uv pip install -e .)")
    assert eps[0].load() is KalenderPlugin
    reg = hub_registry.build_registry(HubConfig())
    assert "kalender.termine" in reg.tools


def test_hub_laedt_das_plugin_per_modul():
    cfg = HubConfig(plugins=[PluginConfig(name="kalender", module="fyos_plugin_kalender.plugin")])
    reg = hub_registry.build_registry(cfg)
    assert "kalender" not in reg.nicht_geladen
    assert sorted(t for t in reg.tools if t.startswith("kalender.")) == ["kalender.anlegen", "kalender.konten", "kalender.termine"]
    # ohne Konto: aus, nicht kaputt
    assert hub_registry.plugin_aus(reg.plugins["kalender"].health())


def test_version_in_pyproject_und_paket_ist_gleich():
    assert pyproject()["project"]["version"] == __version__ == KalenderPlugin.version


def test_abhaengigkeiten_sind_nach_unten_begrenzt_und_schlank():
    deps = {re.split(r"[<>=!~ ]", d, maxsplit=1)[0] for d in pyproject()["project"]["dependencies"]}
    assert deps == {"fyos-hub", "python-dateutil", "tzdata"}
    assert any(d.startswith("fyos-hub>=0.1.1") for d in pyproject()["project"]["dependencies"])  # mcp_hub.termine gibt es ab 0.1.1


# ── Skill ────────────────────────────────────────────────────────────────────────────────────────────────
def test_skill_liegt_im_paket_und_der_kopf_ist_gueltiges_yaml():
    daten = pyproject()
    assert daten["project"]["entry-points"]["fyos.skills"]["kalender"] == "fyos_plugin_kalender"
    assert daten["tool"]["setuptools"]["package-data"]["fyos_plugin_kalender"] == ["skills/*/*.md"]
    datei = PAKET / "skills" / "kalender" / "SKILL.md"
    assert datei in set(PAKET.glob("skills/*/*.md"))  # das Muster der Paketdaten trifft sie
    skills = pytest.importorskip("mcp_hub.plugins.skills")
    kopf, rumpf = skills.front_matter(datei.read_text(encoding="utf-8"))
    assert kopf.get("name") == "kalender" and kopf.get("fuer") == ["agent"]  # ein Doppelpunkt im Text bräche den Kopf
    assert len(kopf.get("description", "")) > 40 and kopf.get("ausloeser")
    eintrag = skills.eintrag_lesen(datei.parent, "kalender")
    assert eintrag["name"] == "kalender" and eintrag["beschreibung"] and rumpf.startswith("# Kalender")


def test_skill_nennt_nur_vorhandene_werkzeuge_und_alle():
    text = (PAKET / "skills" / "kalender" / "SKILL.md").read_text(encoding="utf-8")
    genannt = set(re.findall(r"`kalender\.(\w+)`", text))
    vorhanden = {t.name for t in KalenderPlugin().describe_tools()}
    assert genannt == vorhanden
    assert "__dry_run__" in text and "bestaetigt" in text


# ── Beispiel-Konfiguration, README ───────────────────────────────────────────────────────────────────────
def test_beispiel_konfiguration_ist_gueltig_und_das_plugin_liest_sie(monkeypatch):
    daten = yaml.safe_load((WURZEL / "hub-config.beispiel.yaml").read_text(encoding="utf-8"))
    (eintrag,) = daten["plugins"]
    assert eintrag["name"] == "kalender"
    assert eintrag["secrets"][0]["env"] == "FYOS_KALENDER_PRIVAT_PW"
    assert "passwort" not in {k for konto in eintrag["config"]["konten"] for k in konto}  # Passwörter stehen nie in der Liste
    monkeypatch.setenv("FYOS_KALENDER_PRIVAT_PW", "ein-app-passwort")
    p = KalenderPlugin(eintrag["config"])  # liest nur; fragt kein Netz
    assert p._unbrauchbar == []  # kein Konfigurationsfehler
    assert [(q.konto.name, q.konto.art, q.konto.nutzer) for q in p._quellen] == [
        ("Privat", "caldav", "alex"), ("Feiertage", "ics", "")]
    assert p._quellen[1].konto.url.startswith("https://")  # webcal:// wird https://


@pytest.mark.parametrize("datei", ["README.md", "README.en.md"])
def test_readme_nennt_jede_einstellung_und_jedes_werkzeug(datei):
    text = (WURZEL / datei).read_text(encoding="utf-8")
    quelltext = (PAKET / "konten.py").read_text(encoding="utf-8")
    variablen = set(re.findall(r"FYOS_KALENDER_[A-Z]+", quelltext))
    assert variablen >= {"FYOS_KALENDER_URL", "FYOS_KALENDER_NUTZER", "FYOS_KALENDER_PASSWORT", "FYOS_KALENDER_ART",
                         "FYOS_KALENDER_NAME", "FYOS_KALENDER_KONTEN"}
    for name in variablen:
        assert name in text, f"{datei} nennt {name} nicht"
    for werkzeug in ("kalender.konten", "kalender.termine", "kalender.anlegen"):
        assert werkzeug in text
    assert "FYOS_TERMINE_URL" in text and "mcp_hub.termine" in text and "Apache-2.0" in text


def test_readme_verweisen_auf_einander_und_vorhandene_dateien():
    for datei, andere in (("README.md", "README.en.md"), ("README.en.md", "README.md")):
        text = (WURZEL / datei).read_text(encoding="utf-8")
        assert f"({andere})" in text
        for ziel in re.findall(r"\]\(([\w./-]+)\)", text):
            assert (WURZEL / ziel).exists(), f"{datei}: {ziel} fehlt"
