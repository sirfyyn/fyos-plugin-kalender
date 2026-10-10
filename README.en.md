Deutsch: [README.md](README.md)

# fyos-plugin-kalender

Appointments for **FYOS — For Your Own Sake**: the plugin reads calendars from any CalDAV account (Nextcloud, iCloud,
Radicale, Baïkal, Fastmail …) and from ICS subscriptions (for example the secret address of a Google calendar), resolves
recurring events correctly and creates new events on request. It is also the **source of appointments** for all other
plugins: the dashboard, the daily brief and the start page ask the core (`mcp_hub.termine`) and get the events of these
accounts without speaking CalDAV and without knowing an address.

Anyone rebuilding FYOS needs no calendar service of their own for this: one account with an app password is enough.

## Setup

Choose the **Kalender** (calendar) building block in the installer (fields: address, user, app password). By hand:
install the package into the Python environment of the hub (`pip install fyos-plugin-kalender`, from your package index
or from the Git repository) and set the environment variables. The hub then loads the plugin by itself through the entry
point group `mcp_hub.plugins`. The appointment source in the core exists from `fyos-hub` 0.1.1 on; with an older core the
`kalender.*` tools still work, only the registration as a source for the dashboard and the daily brief is skipped.

### One account

| Variable | Meaning |
|---|---|
| `FYOS_KALENDER_URL` | Address of the CalDAV server (or of a single calendar) or of an ICS subscription; `webcal://` works |
| `FYOS_KALENDER_NUTZER` | User name (CalDAV); for ICS only if the address requires a login |
| `FYOS_KALENDER_PASSWORT` | **App password**, never the main password of the provider |
| `FYOS_KALENDER_ART` | `caldav` or `ics`. Without it, `webcal://` addresses and addresses ending in `.ics` without a user are ICS, everything else is CalDAV |
| `FYOS_KALENDER_NAME` | Display name of the account; the host if omitted |

The plugin finds the calendars itself (`/.well-known/caldav`, then `current-user-principal` and `calendar-home-set`). The
address of the server is usually enough; if you enter the address of a single calendar, you get only that one.

Typical addresses (your provider names the exact one): Nextcloud `https://cloud.example.com/remote.php/dav`, iCloud
`https://caldav.icloud.com` (Apple ID as user, app-specific password from appleid.apple.com), Radicale
`https://radicale.example.com/<user>/`, Baïkal `https://baikal.example.com/dav.php`, Fastmail
`https://caldav.fastmail.com`.

### Several accounts

`FYOS_KALENDER_KONTEN` is a JSON list (or `konten:` in the `config:` of the plugin, see
[`hub-config.beispiel.yaml`](hub-config.beispiel.yaml)). Each account has `name`, `art`, `url`, `nutzer` and
`passwort_env`, the **name of an environment variable** holding the app password:

```sh
export FYOS_KALENDER_KONTEN='[
  {"name": "Work", "url": "https://dav.example.org", "nutzer": "bob", "passwort_env": "CALENDAR_WORK_PW"},
  {"name": "Holidays", "art": "ics", "url": "webcal://calendars.example.org/holidays.ics"}
]'
export CALENDAR_WORK_PW='…'
```

The plugin rejects a password inside the JSON itself: it would end up in configuration files and backups. The account from
`FYOS_KALENDER_URL` and those in the list count together (at most 20). An unusable entry breaks nothing; it appears as an
account with an error in `kalender.konten` and in every answer.

**ICS subscriptions are read-only.** The address is often the secret (Google: "Secret address in iCal format"; Outlook.com:
published calendar), and the plugin never shows more of it than the host. **Google CalDAV** requires OAuth rather than an
app password; the plugin does not support that, use the secret ICS address instead.

## Tools

| Tool | Effect | Purpose |
|---|---|---|
| `kalender.konten` | read | which accounts are set up, which calendars they have (name, id, writable), which account does not answer. Never passwords. `neu_laden: true` searches the calendars again |
| `kalender.termine` | read | events of the next `tage` days from today 00:00 (default 7, at most 31) or of the window `von`/`bis`; `kalender` picks one calendar by name or id |
| `kalender.anlegen` | write | new event in a CalDAV calendar. First `__dry_run__: true` (the plan), a real run needs `bestaetigt: true`. There is no change or delete |

Every event: `id`, `titel`, `start`, `ende`, `ganztaegig`, `ort`, `kalender`, `konto` (title, start, end, all day, place,
calendar, account). Times carry the offset of the hub time zone (`FYOS_ZEITZONE`, otherwise the machine's); an all-day
event has dates and its `ende` is the day **after** the last one. The plugin does not read descriptions or attendees.

```sh
curl -s localhost:8080/hub/call -H 'Content-Type: application/json' \
  -d '{"tool":"kalender.termine","arguments":{"tage":3}}'
curl -s localhost:8080/hub/call -H 'Content-Type: application/json' \
  -d '{"tool":"kalender.anlegen","arguments":{"titel":"Dentist","start":"2026-10-15T09:00","__dry_run__":true}}'
```

### Three states that are never mixed up

| State | Answer |
|---|---|
| Events, including none | `ok: true`, `termine` (the empty list is an answer: "no events") |
| One account does not answer | the events of the others plus `konten_fehler` with the reason; if **none** answers, `ok: false` |
| Nothing set up | `ok: false`, `eingerichtet: false` and the way to set it up |

A failure never shows a free day. If an account does not answer, the list is **incomplete**, and the answer says so.

## Source of appointments for other plugins

At start the plugin registers with the core as a store (`mcp_hub.termine.ablage_anmelden`) as soon as at least one account
is set up. Other plugins call `mcp_hub.termine.quelle()` and get `kalender()`, `termine(von, bis, kalender)` and
`anlegen(termin)` in the core's event format. This path follows the **stricter rule**: if one account does not answer,
the store raises a `QuelleFehler` naming the account instead of returning an incomplete list. The tile on the start page
then shows an error and not an empty day. The store comes after an appointment service (`FYOS_TERMINE_URL`, the extended
tier with a service of your own); whoever runs their own service sets only that variable.

## Security

* The password never appears in an answer, a log, an error message or a `repr()`; error texts name only the host.
* The password goes only to the host of the account or to a sibling host of the same provider domain over https and the
  same port (iCloud points to `pNN-caldav.icloud.com`). The plugin does not follow a redirect to a foreign host; the error
  text says that no password goes there.
* Credentials over `http://` log a warning (the password would travel unencrypted).
* The plugin rejects XML with `DOCTYPE` or `ENTITY`; answers are limited to 8 MiB (also unpacked), every step to 10 s and
  a request to 25 s. Recurrence rules that never match or would need millions of steps are discarded instead of computed.
* Titles and places are personal: the plugin marks its arguments as confidential (never in plain text on the event bus).
* `kalender.anlegen` never overwrites an event (`PUT` with `If-None-Match: *`), sets a description only on explicit
  request and, without a name, creates only if exactly one writable calendar is certain (if an account does not answer,
  the plugin says so instead of writing into the only visible one).

## Limits

* Basic authentication only (app password). No OAuth, no Digest.
* There is no change or delete of events, on purpose.
* Recurrence: `RRULE` (daily, weekly, monthly, yearly, hourly) with `EXDATE`, `RDATE`, `RECURRENCE-ID`, time zones and
  all-day events. The server resolves them (`expand`) where it can; otherwise the plugin computes them. A rule the plugin
  does not understand is discarded: only the first event stays visible. `RANGE=THISANDFUTURE` applies to that one event only.
* Limits: 20 accounts, 100 calendars per account, at most 31 days for `kalender.termine` (366 for the store), 200 events in
  the answer of the tool, 2000 occurrences per event and window.
* Caches: calendar list one hour, events 60 seconds, ICS subscription 10 minutes. A newly created event is visible at once.
* The plugin checks TLS certificates with the system store; behind a certificate authority of your own set `SSL_CERT_FILE`.

## Development

```sh
uv venv && . .venv/bin/activate
uv pip install -e /path/to/fyos-os/hub     # the hub (package fyos-hub)
uv pip install -e ".[test]"
python3 -m pytest -q
python3 tools/sauber.py .                  # no private addresses, accounts or secrets in the repo
```

The tests run against fake servers on 127.0.0.1 (CalDAV and ICS), never against a real calendar. Tests that need the real
core (registration as the appointment source) are skipped when `mcp_hub.termine` is missing.

| Path | Purpose |
|---|---|
| `fyos_plugin_kalender/plugin.py` | `KalenderPlugin`: tools, three states, store for the core |
| `fyos_plugin_kalender/konten.py` | accounts from environment and configuration |
| `fyos_plugin_kalender/quellen.py` | `CalDavQuelle` and `IcsQuelle`: find calendars, fetch events, create, caches |
| `fyos_plugin_kalender/caldav.py`, `netz.py` | CalDAV requests and answers; network with redirect and password rules |
| `fyos_plugin_kalender/ical.py`, `zeit.py`, `zonen.py` | read and write iCalendar, recurrence, time zones |
| `fyos_plugin_kalender/kern.py` | the optional link to `mcp_hub.termine` |
| `fyos_plugin_kalender/skills/kalender/SKILL.md` | knowledge for the agent (which tool when, how to create an event safely) |
| `hub-config.beispiel.yaml` | entry in the hub configuration |
| `.github/workflows/tests.yml` | GitHub Actions: tests against the hub from the source tree of `fyos-os`, `tools/sauber.py` (tree and history), gitleaks |

## License

Apache License, Version 2.0 (SPDX identifier `Apache-2.0`); text in [`LICENSE`](LICENSE), copyright notice in
[`NOTICE`](NOTICE). Dependencies: `python-dateutil` (Apache-2.0 and BSD-3-Clause, dual-licensed) for recurrence rules and
embedded time zones, `tzdata` (Apache-2.0) as the time zone database.
