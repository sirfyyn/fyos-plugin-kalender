English: [README.en.md](README.en.md)

# fyos-plugin-kalender

Termine für **FYOS — For Your Own Sake**: das Plugin liest Kalender aus beliebigen CalDAV-Konten (Nextcloud, iCloud,
Radicale, Baïkal, Fastmail …) und aus ICS-Abonnements (zum Beispiel die geheime Adresse eines Google-Kalenders), löst
Wiederholungen richtig auf und legt auf Wunsch neue Termine an. Außerdem ist es die **Quelle der Termine** für alle
anderen Plugins: Leitstand, Tagesbrief und Startseite fragen im Kern (`mcp_hub.termine`) und bekommen die Termine
dieser Konten, ohne CalDAV zu können und ohne eine Adresse zu kennen.

Wer FYOS nachbaut, braucht dafür keinen eigenen Kalender-Dienst: ein Konto mit App-Passwort genügt.

## Einrichten

Im Installer den Baustein **Kalender** wählen (Felder: Adresse, Nutzer, App-Passwort). Von Hand: das Paket in die
Python-Umgebung des Hubs installieren (`pip install fyos-plugin-kalender`, aus dem Paketindex oder aus dem
Git-Repository) und die Umgebungsvariablen setzen. Der Hub lädt das Plugin dann von selbst über die Entry-Point-Gruppe
`mcp_hub.plugins`. Die Termin-Quelle im Kern gibt es ab `fyos-hub` 0.1.1; mit einem älteren Kern laufen die Werkzeuge
`kalender.*` trotzdem, nur die Anmeldung als Quelle für Leitstand und Tagesbrief entfällt.

### Ein Konto

| Variable | Bedeutung |
|---|---|
| `FYOS_KALENDER_URL` | Adresse des CalDAV-Servers (oder eines einzelnen Kalenders) oder eines ICS-Abonnements; `webcal://` geht |
| `FYOS_KALENDER_NUTZER` | Nutzername (CalDAV); bei ICS nur, wenn die Adresse einen Zugang verlangt |
| `FYOS_KALENDER_PASSWORT` | **App-Passwort**, nie das Haupt-Passwort des Anbieters |
| `FYOS_KALENDER_ART` | `caldav` oder `ics`. Ohne Angabe sind `webcal://`-Adressen und Adressen auf `.ics` ohne Nutzer ICS, alles andere CalDAV |
| `FYOS_KALENDER_NAME` | Anzeigename des Kontos; ohne Angabe der Host |

Das Plugin findet die Kalender selbst (`/.well-known/caldav`, dann `current-user-principal` und `calendar-home-set`).
Es genügt meist die Adresse des Servers; wer die Adresse eines einzelnen Kalenders einträgt, bekommt nur diesen.

Typische Adressen (die genaue nennt der Anbieter): Nextcloud `https://cloud.example.com/remote.php/dav`, iCloud
`https://caldav.icloud.com` (Apple-ID als Nutzer, App-spezifisches Passwort unter appleid.apple.com), Radicale
`https://radicale.example.com/<nutzer>/`, Baïkal `https://baikal.example.com/dav.php`, Fastmail
`https://caldav.fastmail.com`.

### Mehrere Konten

`FYOS_KALENDER_KONTEN` ist eine JSON-Liste (oder `konten:` in der `config:` des Plugins, siehe
[`hub-config.beispiel.yaml`](hub-config.beispiel.yaml)). Je Konto `name`, `art`, `url`, `nutzer` und `passwort_env`, der
**Name einer Umgebungsvariablen** mit dem App-Passwort:

```sh
export FYOS_KALENDER_KONTEN='[
  {"name": "Arbeit", "url": "https://dav.example.org", "nutzer": "bob", "passwort_env": "KALENDER_ARBEIT_PW"},
  {"name": "Feiertage", "art": "ics", "url": "webcal://calendars.example.org/feiertage.ics"}
]'
export KALENDER_ARBEIT_PW='…'
```

Ein Passwort im JSON selbst lehnt das Plugin ab: es stünde in Konfigurationsdateien und Sicherungen. Das Konto aus
`FYOS_KALENDER_URL` und die der Liste gelten zusammen (höchstens 20). Ein unbrauchbarer Eintrag bricht nichts ab; er
erscheint als Konto mit Fehler in `kalender.konten` und in jeder Antwort.

**ICS-Abonnements sind schreibgeschützt.** Die Adresse ist oft das Geheimnis (Google: „Geheime Adresse im iCal-Format“;
Outlook.com: veröffentlichter Kalender), das Plugin zeigt von ihr nie mehr als den Host. **Google-CalDAV** verlangt OAuth
und kein App-Passwort; das unterstützt das Plugin nicht, nimm dafür die geheime ICS-Adresse.

## Werkzeuge

| Werkzeug | Wirkung | Zweck |
|---|---|---|
| `kalender.konten` | lesend | welche Konten eingerichtet sind, welche Kalender sie haben (Name, Kennung, beschreibbar), welches Konto nicht antwortet. Nie Passwörter. `neu_laden: true` sucht die Kalender neu |
| `kalender.termine` | lesend | Termine der nächsten `tage` Tage ab heute 0 Uhr (Vorgabe 7, höchstens 31) oder des Fensters `von`/`bis`; `kalender` wählt einen Kalender nach Name oder Kennung |
| `kalender.anlegen` | schreibend | neuer Termin in einem CalDAV-Kalender. Zuerst `__dry_run__: true` (Plan), ein echter Lauf braucht `bestaetigt: true`. Ändern und Löschen gibt es nicht |

Jeder Termin: `id`, `titel`, `start`, `ende`, `ganztaegig`, `ort`, `kalender`, `konto`. Zeiten tragen den Versatz der
Zeitzone des Hubs (`FYOS_ZEITZONE`, sonst die des Rechners); ein ganztägiger Termin hat Daten, und sein `ende` ist der Tag
**nach** dem letzten. Beschreibung und Teilnehmer liest das Plugin nicht.

```sh
curl -s localhost:8080/hub/call -H 'Content-Type: application/json' \
  -d '{"tool":"kalender.termine","arguments":{"tage":3}}'
curl -s localhost:8080/hub/call -H 'Content-Type: application/json' \
  -d '{"tool":"kalender.anlegen","arguments":{"titel":"Zahnarzt","start":"2026-10-15T09:00","__dry_run__":true}}'
```

### Drei Zustände, die nie verwechselt werden

| Zustand | Antwort |
|---|---|
| Termine, auch keine | `ok: true`, `termine` (die leere Liste ist eine Antwort: „keine Termine“) |
| Ein Konto antwortet nicht | die Termine der anderen und `konten_fehler` mit dem Grund; antwortet **keines**, `ok: false` |
| Nichts eingerichtet | `ok: false`, `eingerichtet: false` und der Weg zur Einrichtung |

Ein Ausfall zeigt nie einen freien Tag. Antwortet ein Konto nicht, ist die Liste **unvollständig**, und das steht in der
Antwort.

## Quelle der Termine für andere Plugins

Beim Start meldet sich das Plugin im Kern als Ablage an (`mcp_hub.termine.ablage_anmelden`), sobald mindestens ein Konto
eingetragen ist. Andere Plugins rufen `mcp_hub.termine.quelle()` und bekommen `kalender()`, `termine(von, bis, kalender)`
und `anlegen(termin)` im Terminformat des Kerns. Für diesen Weg gilt die **strengere Regel**: antwortet ein Konto nicht,
wirft die Ablage einen `QuelleFehler` mit dem Namen des Kontos, statt eine unvollständige Liste zu liefern. Die
Kachel auf der Startseite zeigt dann einen Fehler und keinen leeren Tag. Die Ablage geht erst nach einem Termin-Dienst
(`FYOS_TERMINE_URL`, die Ausbaustufe mit eigenem Dienst) an die Reihe; wer einen eigenen Dienst betreibt, setzt nur diese
Variable.

## Sicherheit

* Das Passwort steht nie in einer Antwort, einem Log, einer Fehlermeldung oder `repr()`; Fehlertexte nennen nur den Host.
* Das Passwort geht nur an den Host des Kontos oder an einen Geschwister-Host derselben Anbieter-Domain über https und
  denselben Port (iCloud verweist auf `pNN-caldav.icloud.com`). Eine Umleitung auf einen fremden Host folgt das Plugin nicht;
  der Fehlertext sagt, dass dorthin kein Passwort geht.
* Zugangsdaten über `http://` melden einen Hinweis im Log (das Passwort ginge unverschlüsselt).
* XML mit `DOCTYPE` oder `ENTITY` lehnt das Plugin ab; Antworten sind auf 8 MiB begrenzt (auch entpackt), jeder Schritt
  auf 10 s und eine Anfrage auf 25 s. Wiederholungsregeln, die nie treffen oder Millionen Schritte bräuchten, werden
  verworfen statt gerechnet.
* Titel und Orte sind persönlich: das Plugin kennzeichnet seine Argumente als vertraulich (nie im Klartext auf dem Ereignisbus).
* `kalender.anlegen` überschreibt nie einen Termin (`PUT` mit `If-None-Match: *`), setzt eine Beschreibung nur auf
  ausdrücklichen Wunsch und legt ohne Namen nur an, wenn genau ein beschreibbarer Kalender sicher feststeht (antwortet ein
  Konto nicht, sagt das Plugin es, statt in den einzigen sichtbaren zu schreiben).

## Grenzen

* Nur Basic-Authentifizierung (App-Passwort). Kein OAuth, kein Digest.
* Ändern und Löschen von Terminen gibt es nicht, absichtlich.
* Wiederholungen: `RRULE` (täglich, wöchentlich, monatlich, jährlich, stündlich) mit `EXDATE`, `RDATE`, `RECURRENCE-ID`,
  Zeitzonen und ganztägigen Terminen. Der Server löst sie auf (`expand`), wo er es kann; sonst rechnet das Plugin. Eine Regel,
  die das Plugin nicht versteht, wird verworfen: sichtbar bleibt dann nur der erste Termin. `RANGE=THISANDFUTURE` gilt nur für
  den einen Termin.
* Grenzen: 20 Konten, 100 Kalender je Konto, höchstens 31 Tage für `kalender.termine` (366 für die Ablage), 200 Termine in der
  Antwort des Werkzeugs, 2000 Wiederholungen je Termin und Fenster.
* Zwischenspeicher: Kalenderliste eine Stunde, Termine 60 Sekunden, ICS-Abonnement 10 Minuten. Ein neu angelegter Termin
  ist sofort zu sehen.
* TLS-Zertifikate prüft das Plugin mit dem System-Speicher; hinter einer eigenen Zertifizierungsstelle `SSL_CERT_FILE` setzen.

## Entwicklung

```sh
uv venv && . .venv/bin/activate
uv pip install -e /pfad/zu/fyos-os/hub     # der Hub (Paket fyos-hub)
uv pip install -e ".[test]"
python3 -m pytest -q
python3 tools/sauber.py .                  # keine privaten Adressen, Konten, Geheimnisse im Repo
```

Die Tests laufen gegen Attrappen-Server auf 127.0.0.1 (CalDAV und ICS), nie gegen einen echten Kalender. Tests, die den
echten Kern brauchen (Anmeldung als Termin-Quelle), werden übersprungen, wenn `mcp_hub.termine` fehlt.

| Pfad | Zweck |
|---|---|
| `fyos_plugin_kalender/plugin.py` | `KalenderPlugin`: Werkzeuge, drei Zustände, Ablage für den Kern |
| `fyos_plugin_kalender/konten.py` | Konten aus Umgebung und Konfiguration |
| `fyos_plugin_kalender/quellen.py` | `CalDavQuelle` und `IcsQuelle`: Kalender finden, Termine holen, anlegen, Zwischenspeicher |
| `fyos_plugin_kalender/caldav.py`, `netz.py` | CalDAV-Anfragen und -Antworten; Netz mit Umleitungs- und Passwort-Regeln |
| `fyos_plugin_kalender/ical.py`, `zeit.py`, `zonen.py` | iCalendar lesen und schreiben, Wiederholungen, Zeitzonen |
| `fyos_plugin_kalender/kern.py` | die optionale Verbindung zu `mcp_hub.termine` |
| `fyos_plugin_kalender/skills/kalender/SKILL.md` | Wissen für den Agenten (wann welches Werkzeug, wie ein Termin sicher angelegt wird) |
| `hub-config.beispiel.yaml` | Eintrag in der Hub-Konfiguration |
| `.github/workflows/tests.yml` | GitHub Actions: Tests gegen den Hub aus dem Quellbaum von `fyos-os`, `tools/sauber.py` (Baum und Historie), gitleaks |

## Lizenz

Apache License, Version 2.0 (SPDX-Kennung `Apache-2.0`); Text in [`LICENSE`](LICENSE), Urheberhinweis in
[`NOTICE`](NOTICE). Abhängigkeiten: `python-dateutil` (Apache-2.0 und BSD-3-Clause, doppelt lizenziert) für Wiederholungsregeln
und eingebettete Zeitzonen, `tzdata` (Apache-2.0) als Zeitzonen-Datenbank.
