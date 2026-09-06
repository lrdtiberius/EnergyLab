# EnergyLab

EnergyLab ist ein schlankes, lokal betriebenes Dashboard für Energie-, Wasser-, Abwasser- und Fahrzeugdaten. Die Anwendung läuft als einzelner Docker-Container, speichert ihre Daten in SQLite und kann Zählerstände aus Home Assistant übernehmen.

## Release 0.6.6

EnergyLab 0.6.6 erweitert die Verbrauchs- und Vertragsverwaltung:

- eigene Bereiche für Strom, Gas, Wasser und Abwasser
- Abwasserverbrauch automatisch aus dem Wasserzähler, aber mit eigenem Tarif und eigenen Zahlungen
- monatliche, quartalsweise, halbjährliche oder jährliche Zahlungen mit frei wählbarem Zahlungstag und optionalem Datum der ersten Zahlung
- spätere Zahlungsänderungen mit Gültigkeitsmonat, ohne alte Zahlungen oder Verträge umzuschreiben
- Kontoübergabe und Zahlungstermine für die automatische Übernahme durch FinanzLab
- manuelle, auch rückwirkende Zählerstände für Strom, Gas und Wasser
- Verbrauchsdifferenz im Zeitraumvergleich und vollständiger Zählerstände-Export
- Grundpreis und Zahlungsbetrag in Monatsansichten als volle Monatswerte

Die Version 0.6.6 ist ein vollständiges Standalone-Release und benötigt keinen Patch sowie keine ältere Programmversion. Beim Update bleibt das vorhandene Docker-Volume erhalten.

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

## Update

Baue das neue Image, ändere im Portainer-Stack den Image-Namen und aktualisiere den Stack. Das Volume `energylab_data` darf dabei nicht gelöscht werden. EnergyLab führt erforderliche Datenbankanpassungen beim Start selbst aus.

## Datenschutz

Das Repository enthält keine privaten Home-Assistant-Adressen, Tokens, Sensor-IDs, Zählerstände, Verbrauchsdaten, Tarife oder Vertragsdaten. Alle Nutzdaten bleiben in der lokalen SQLite-Datenbank.

## Urheber und Unterstützung

Idee und Umsetzung: **Lrd.Tiberius**

[Buy me a coffee](https://www.paypal.com/paypalme/SebastianM207)
