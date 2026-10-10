---
name: kalender
description: Laden bei Fragen zu Terminen (was heute oder diese Woche ansteht, wann etwas stattfindet, ob jemand Zeit hat) und wenn ein Termin eingetragen werden soll. Wann welches Werkzeug, wie ein Termin sicher angelegt wird und was das Plugin nie tut.
ausloeser: Termine, Kalender, was steht an, heute, morgen, diese Woche, Zeit haben, Termin eintragen, Termin anlegen, Treffen, Zahnarzt
fuer: [agent]
---

# Kalender: Termine lesen und anlegen

Dieser Skill gehört zum Plugin `fyos-plugin-kalender`. Das Plugin liest die Kalender, die der Besitzer eingerichtet hat
(CalDAV-Konten wie Nextcloud, iCloud, Radicale und ICS-Abonnements wie ein Google-Kalender über seine geheime
Adresse). Es zeigt Titel, Zeit und Ort; Beschreibungen und Teilnehmer liest es nicht.

## Wann

- Der Besitzer fragt nach Terminen („Was steht heute an?“, „Wann ist der Zahnarzt?“, „Habe ich Freitag Zeit?“):
  `kalender.termine`.
- Du weißt nicht, welche Kalender es gibt, oder ein Name passt nicht: `kalender.konten`.
- Der Besitzer will einen Termin eintragen: `kalender.anlegen`, nach dem Ablauf unten.

## Termine lesen

1. Ohne Angaben liefert `kalender.termine` die nächsten 7 Tage ab heute 0 Uhr. Mit `tage` (höchstens 31) oder mit `von`
   und `bis` (ISO 8601, `bis` ausgeschlossen: von 2026-10-12 bis 2026-10-14 sind der 12. und der 13.) wählst du das Fenster.
2. Ein ganztägiger Termin hat Daten statt Uhrzeiten; sein `ende` ist der Tag **nach** dem letzten Tag. Alle Zeiten sind
   in der Zeitzone des Hubs.
3. Antwortet ein Konto nicht, steht es in `konten_fehler`, und die Liste ist **unvollständig**. Sage das dem Besitzer
   („Der Kalender Privat antwortet gerade nicht“), statt zu behaupten, der Tag sei frei. Eine leere Liste ohne
   `konten_fehler` heißt wirklich: keine Termine.
4. Antwortet `kalender.termine` mit „Kalender nicht eingerichtet“, gibt es noch kein Konto. Sage, wie man eines einrichtet
   (Baustein „Kalender“ im Installer oder die Umgebung des Hubs, siehe README des Plugins); versuche nichts Eigenes.

## Termin anlegen

1. Nur auf ausdrücklichen Wunsch. Ändern und Löschen kann das Plugin nicht; sage das, wenn der Besitzer es verlangt.
2. Kläre Titel, Beginn (Datum und Uhrzeit, oder ganztägig) und bei Bedarf Ende und Ort. Fehlt das Ende, dauert der Termin
   eine Stunde. Rate keine Uhrzeit.
3. Mehrere beschreibbare Kalender: frage, in welchen der Termin soll (Name aus `kalender.konten`). Abonnements (ICS)
   sind schreibgeschützt.
4. Rufe `kalender.anlegen` zuerst mit `__dry_run__: true` auf. Du bekommst den Plan: welcher Kalender, welche Zeit.
   Zeige ihn dem Besitzer in einem Satz und lasse ihn bestätigen.
5. Erst danach der echte Lauf mit `bestaetigt: true`. Ohne `bestaetigt` legt das Werkzeug nichts an.
6. Melde knapp, was angelegt wurde (Titel, Zeit, Kalender). Lege einen Termin nie ein zweites Mal an, wenn die Antwort
   unklar war: lies stattdessen mit `kalender.termine` nach.

## Regeln

- `beschreibung` nur setzen, wenn der Besitzer einen Text dafür nennt. Schreibe keine Zugangsdaten, Passwörter oder
  Gesundheitsdaten in einen Termin; er liegt im Kalender-Server, den vielleicht andere sehen.
- Zugangsdaten richtet der Besitzer außerhalb des Chats ein (Installer oder Umgebung des Hubs). Bitte nie darum, ein
  Passwort im Gespräch zu nennen, und wiederhole keines.
- Der Titel ist ein Datum für andere: kurz und sachlich, ohne vertrauliche Einzelheiten.
