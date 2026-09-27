# EnergyLab

EnergyLab ist ein schlankes, lokal betriebenes Dashboard für Energie-, Wasser-, Abwasser- und Fahrzeugdaten. Die Anwendung läuft als einzelner Docker-Container, speichert ihre Daten in SQLite und kann Zählerstände aus Home Assistant übernehmen.

## Release 1.2.2

EnergyLab 1.2.2 entspricht dem auf dem AM06 geprüften Stand vom 24. September 2026. Gegenüber dem bisher veröffentlichten Stand kamen insbesondere hinzu:

- eigene Bereiche für Strom, Gas, Wasser und Abwasser
- Abwasserverbrauch automatisch aus dem Wasserzähler, aber mit eigenem Tarif und eigenen Zahlungen
- monatliche, quartalsweise, halbjährliche oder jährliche Zahlungen mit frei wählbarem Zahlungstag und optionalem Datum der ersten Zahlung
- spätere Zahlungsänderungen mit Gültigkeitsmonat, ohne alte Zahlungen oder Verträge umzuschreiben
- Kontoübergabe und Zahlungstermine für die automatische Übernahme durch FinanzLab
- manuelle, auch rückwirkende Zählerstände für Strom, Gas und Wasser
- Verbrauchsdifferenz im Zeitraumvergleich und vollständiger Zählerstände-Export
- Grundpreise werden bei Teilmonaten zeitanteilig angesetzt; Zahlungsbeträge folgen ihrem tatsächlichen Fälligkeitstakt
- revisionssichere Ist-Zahlungen, Rückerstattungen, Rücklastschriften, Korrekturen und Zahlungspausen
- Schlussabrechnungs-Snapshots und Zahlungsabgleich mit FinanzLab
- integrierte, herunterladbare Datenbanksicherungen mit automatischer Sicherung vor Updates und Wiederherstellungen
- Spritmonitor-CSV-Import mit Vorschau, Plausibilitätsprüfung und Dublettenschutz
- zusätzliche Fahrzeugkosten sowie kombinierte Kosten je Kilometer
- installierbare Web-App-Symbole und Manifest

Die Version 1.2.2 ist ein vollständiges Standalone-Release und benötigt keinen Patch sowie keine ältere Programmversion. Beim Update bleibt das vorhandene Docker-Volume erhalten; vor einer Datenbankmigration legt EnergyLab automatisch eine Sicherung an.

## Installation und Einrichtung

Die vollständige Schritt-für-Schritt-Anleitung für Portainer, Docker Compose, Home Assistant, Sensoren, Historienimport, Tarife, Wasser und Datensicherung steht in [INSTALLATION.md](INSTALLATION.md).

## Funktionen

- eigene Übersichten für Strom, PV-Eigenverbrauch, Gas, Wasser und Abwasser
- Tages-, Monats- und Jahresauswertungen mit Detailtabellen und Diagrammen
- Plausibilitätsprüfung für Nullwerte, Zählerrücksprünge und unrealistische Sprünge
- Home-Assistant-Synchronisierung und Import vorhandener Langzeitstatistiken
- automatische Synchronisierung täglich um 23:30 Uhr in `Europe/Berlin`
- sichtbarer Datenstand mit Datum und Uhrzeit
- historisch korrekte Tarifzeiträume mit Anbieter, Verbrauchspreis, monatlichem Grundpreis und Zahlungen
- Zahlungsrhythmus, Zahlungstag, erste Zahlung und FinanzLab-Konto je Vertrag
- Abrechnungsvorschau mit Erstattung oder Nachzahlung
- PV-Ersparnis als separate negative Position, ohne die tatsächlichen Stromkosten zu reduzieren
- manuelle, auch rückwirkende Zählerstände für Strom, Gas und Wasser
- Zeitraums- und Vorjahresvergleich einschließlich Verbrauchsdifferenz
- Excel-Export einschließlich aller automatischen und manuellen Zählerstände
- Fahrzeug-, Tank- und Betriebskostenverwaltung
- Spritmonitor-Import mit Prüf- und Bestätigungsschritt
- manuelle und automatische Datenbanksicherungen unter `/data/backups`
- optionaler Abgleich bestätigter Zahlungen mit FinanzLab

## Start mit Docker Compose

```bash
docker compose up -d
```

Danach ist EnergyLab unter `http://<docker-host>:8090` erreichbar. Die SQLite-Datenbank liegt im Volume `energylab_data` und bleibt bei Image-Updates erhalten.

Die mitgelieferte `docker-compose.yml` enthält ausschließlich neutrale Platzhalter. Trage Home-Assistant-Token und Sensoren nur in deiner lokalen Installation ein und veröffentliche sie nicht.

## Konfiguration

| Variable | Bedeutung | Standard |
|---|---|---|
| `TZ` | Zeitzone | `Europe/Berlin` |
| `HA_URL` | URL von Home Assistant | `http://homeassistant.local:8123` |
| `HA_TOKEN` | langlebiger Home-Assistant-Zugriffstoken | leer |
| `HA_GRID_IMPORT_ENTITY` | fortlaufender Strombezugszähler | leer |
| `HA_PV_SELF_ENTITY` | fortlaufender PV-Eigenverbrauchszähler | leer |
| `HA_GAS_ENTITY` | fortlaufender Gaszähler | leer |
| `ENERGYLAB_SYNC_HOUR` | Stunde der täglichen Synchronisierung | `23` |
| `ENERGYLAB_SYNC_MINUTE` | Minute der täglichen Synchronisierung | `30` |
| `BUY_ME_A_COFFEE_URL` | Ziel des Unterstützungslinks | siehe Beispiel-Stack |
| `FINANZLAB_BASE_URL` | optionale FinanzLab-Adresse für den Zahlungsabgleich | leer |
| `FINANZLAB_HOUSEHOLD_ID` | Haushalt als Name oder interne ID | leer |
| `FINANZLAB_TOKEN` | optionaler Zugriffsschlüssel | leer |

## Dokumentation

Das ausführliche Benutzerhandbuch steht in [HANDBUCH.md](HANDBUCH.md). Versionsänderungen sind im [CHANGELOG.md](CHANGELOG.md) dokumentiert.

## Update

Baue das neue Image, ändere im Portainer-Stack den Image-Namen und aktualisiere den Stack. Das Volume `energylab_data` darf dabei nicht gelöscht werden. EnergyLab führt erforderliche Datenbankanpassungen beim Start selbst aus.

## Datenschutz

Das Repository enthält keine privaten Home-Assistant-Adressen, Tokens, Sensor-IDs, Zählerstände, Verbrauchsdaten, Tarife oder Vertragsdaten. Alle Nutzdaten bleiben in der lokalen SQLite-Datenbank.

## Bekannte Einschränkungen

Fünf ältere Regressionstests bilden noch das frühere Verhalten von Monatsnavigation, Abrechnungshochrechnung und vollständigen Grundpreisen in Teilmonaten ab. Sie sind als erwartete Abweichungen markiert, bis die Tests fachlich auf das aktuelle 1.2.2-Modell umgestellt sind.

## Urheber und Unterstützung

Idee und Umsetzung: **Lrd.Tiberius**

[Buy me a coffee](https://www.paypal.com/paypalme/SebastianM207)
