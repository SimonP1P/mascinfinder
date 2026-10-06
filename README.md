# mascinfinder

Automatisierte technische Recherche für CNC-/Portalfräsmaschinen.

## Pipeline

- machines_backlog.json bleibt die zentrale Aufgabenliste.
- Jeder Research-Worker bearbeitet exakt eine Maschine.
- GitHub Actions orchestriert den Lauf.
- Gemini 2.5 Flash-Lite recherchiert mit Google Search Grounding.
- Mehrere Maschinen können parallel laufen.
- Jede Ausgabe wird gegen machine.schema.json validiert.
- Ergebnisse werden unter dem im Backlog angegebenen data_file gespeichert.
- Nach erfolgreicher Recherche wird der Backlog automatisch aktualisiert.

## Einrichtung

1. Gemini API Key in Google AI Studio erstellen.
2. GitHub Settings -> Secrets and variables -> Actions -> New repository secret.
3. Secret Name: GEMINI_API_KEY
4. Secret Value: dein Gemini API Key.
5. Actions -> Machine Research -> Run workflow.

Der API Key wird nicht ins Repository geschrieben.

## Standardwerte

- 12 Maschinen pro Lauf
- 6 parallele Worker
- gemini-2.5-flash-lite

Beim manuellen Start können Batchgröße, Workerzahl und Modell geändert werden.

## Status

open -> researching -> completed

Bei fachlichen Widersprüchen wird eine Maschine auf needs_review gesetzt. Bei technischen Fehlern auf failed.

Keine API Keys oder andere Secrets committen.
