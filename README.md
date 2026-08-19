# EnergyLab

EnergyLab bündelt Strom-, PV-, Gas- und Fahrzeugverbräuche lokal im eigenen Homelab.

## Funktionen

- tägliche Übernahme fortlaufender Zählerstände aus Home Assistant
- einmaliger Rückimport vorhandener täglicher Home-Assistant-Langzeitstatistiken
- bis zu fünf Strom- und fünf Gastarife mit frei wählbaren Gültigkeitszeiträumen
- periodengenaue Kostenberechnung; Gas mit kWh/m³-Faktor der Abrechnung
- Dashboard für Monat, Jahr und Gesamtzeitraum
- mehrere Fahrzeuge mit jeweils erlaubten Betriebsstoffen
- Voll- und Teiltankungen mit Voll-zu-Voll-Verbrauchsberechnung
- getrennte AdBlue-Auswertung in l/1.000 km
- Spritmonitor-CSV-Import mit Vorschau, Dublettenprüfung und Preisnormalisierung
- lokaler JSON- und CSV-Export
- responsive Weboberfläche für das lokale Homelab

## Installation mit Portainer

Es wird nur die Datei [`docker-compose.yml`](docker-compose.yml) benötigt.

1. Portainer öffnen und **Stacks → Add stack → Web editor** wählen.
2. Den Inhalt von `docker-compose.yml` einfügen.
3. Mindestens `HA_URL` und `HA_TOKEN` ersetzen.
4. Die eigenen Sensor-IDs nur lokal in den drei `HA_*_ENTITY`-Variablen ergänzen.
5. Stack bereitstellen und `http://<AM06Pro-IP>:8090` öffnen.

Die SQLite-Datenbank liegt dauerhaft im Docker-Volume `energylab_data`.

## Home Assistant

Die persönlichen Sensor-IDs werden nicht im Repository gespeichert. Sie werden ausschließlich lokal im Portainer-Stack eingetragen:

- `HA_GRID_IMPORT_ENTITY`: fortlaufender Strombezugszähler in kWh
- `HA_PV_SELF_ENTITY`: fortlaufender PV-Eigenverbrauchszähler in kWh
- `HA_GAS_ENTITY`: fortlaufender Gaszähler in m³

Die Anwendung liest einmal täglich den aktuellen Zählerstand. Der Tagesverbrauch entsteht aus der Differenz zum vorherigen Stand. Momentanleistungs-Sensoren in Watt sind dafür nicht geeignet.

Auf der Seite **Energie** kann ein Startdatum für einen einmaligen Historienimport gewählt werden. Verfügbar sind nur Zeiträume, für die Home Assistant Langzeitstatistiken des jeweiligen Sensors gespeichert hat. Wiederholte Importe aktualisieren dieselben Tage, statt Dubletten anzulegen.

Stromtarife werden mit Anbieter und Cent/kWh, Gastarife mit Anbieter und €/kWh eingegeben. Die gespeicherten Vertragszeiträume lassen sich auf der Energie-Seite aufklappen. Pro Tarifart können bis zu fünf Zeiträume hinterlegt werden. Da der Gaszähler m³ liefert, wird beim Gastarif außerdem der Umrechnungsfaktor in kWh/m³ aus der jeweiligen Gasabrechnung eingetragen.

Für den Zugriff wird in Home Assistant unter **Profil → Sicherheit → Langlebige Zugriffstoken** ein Token erzeugt und ausschließlich lokal als `HA_TOKEN` im Portainer-Stack eingetragen.

## Entwicklung

```bash
python -m unittest discover -s tests -v
ENERGYLAB_DATA_DIR=./data python app.py
```

Das Container-Image wird nach erfolgreichen Tests automatisch als `ghcr.io/lrdtiberius/energylab:latest` veröffentlicht.

## Unterstützung

Idea und umsetztung by Lrd.Tiberius

Der Bereich **Unterstützung** enthält einen einzelnen „Buy me a coffee“-Link auf die bereits in den anderen Lab-Projekten verwendete PayPal-Me-Seite. Das Ziel bleibt über `BUY_ME_A_COFFEE_URL` konfigurierbar.
