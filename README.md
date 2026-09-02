# EnergyLab

EnergyLab ist ein schlankes, lokal betriebenes Dashboard für Energie-, Wasser- und Fahrzeugdaten. Die Anwendung läuft als einzelner Docker-Container, speichert ihre Daten in SQLite und kann Zählerstände aus Home Assistant übernehmen.

## Release 0.6.0

Die Detailansichten von Strom, PV-Eigenverbrauch, Gas und Wasser lassen sich jetzt monatsweise durchblättern:

- vorherige Monate direkt in der jeweiligen Energieansicht öffnen
- bis zum aktuellen Monat wieder vorwärts navigieren
- Verbrauch, Kosten, Messwerte und Diagramme passend zum gewählten Monat berechnen
- zukünftige Monate automatisch sperren
- Abrechnungshochrechnung nur dort anzeigen, wo sie fachlich sinnvoll ist

Die Version 0.6.0 ist ein vollständiges Standalone-Release und benötigt keinen Patch sowie keine ältere Programmversion. Beim Update bleibt das vorhandene Docker-Volume erhalten.

## Funktionen

- Dashboard für Strom, PV-Eigenverbrauch, Gas und Wasser
- Tages-, Monats- und Jahresauswertungen mit Detailtabellen und Diagrammen
- Plausibilitätsprüfung für Nullwerte, Zählerrücksprünge und unrealistische Sprünge
- Home-Assistant-Synchronisierung und Import vorhandener Langzeitstatistiken
- automatische Synchronisierung täglich um 23:30 Uhr in `Europe/Berlin`
- sichtbarer Datenstand mit Datum und Uhrzeit
- Tarifzeiträume mit Anbieter, Verbrauchspreis, Grundpreis und Abschlag
- Abrechnungsvorschau mit Erstattung oder Nachzahlung
- PV-Ersparnis als separate negative Position, ohne die tatsächlichen Stromkosten zu reduzieren
- Wasserzähler mit manuellen, auch rückwirkenden Ablesungen
- Zeitraums- und Vorjahresvergleich
- Excel-Export unter Einstellungen
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

