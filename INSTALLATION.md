# EnergyLab 1.2.2 installieren und einrichten

Diese Anleitung beschreibt die vollständige Erstinstallation und das Update einer bestehenden EnergyLab-Installation. Version 1.2.2 ist ein Standalone-Release und benötigt keinen Patch und keine frühere Programmversion.

## 1. Voraussetzungen

- ein Docker-System oder Portainer
- ein dauerhaftes Docker-Volume für die SQLite-Datenbank
- für die automatische Energieübernahme: Home Assistant im selben Netzwerk
- drei fortlaufende Home-Assistant-Zähler sind möglich: Strombezug, PV-Eigenverbrauch und Gas
- Wasserstände werden manuell erfasst; derselbe Zähler liefert automatisch die Verbrauchsmenge für Abwasser

EnergyLab besitzt keine Benutzeranmeldung. Verwende es im vertrauenswürdigen Heimnetz. Für einen Zugriff aus dem Internet sollte ein vorgeschalteter Reverse Proxy mit Anmeldung oder ein VPN eingesetzt werden; Port 8090 sollte nicht ungeschützt ins Internet freigegeben werden.

## 2. Installation mit Portainer-Buildarchiv

### 2.1 Image bauen

1. Lade `energylab-portainer-build-v1.2.2.tar.gz` herunter.
2. Öffne in Portainer **Images**.
3. Wähle **Build a new image** und anschließend den Upload eines Archivs.
4. Vergib den Namen `energylab:1.2.2`.
5. Lade das Archiv hoch und starte den Build.

Das Archiv enthält den vollständigen Stand von EnergyLab 1.2.2 sowie den Docker-Buildkontext. Es ist kein älteres Image erforderlich.

### 2.2 Stack anlegen

Lege unter **Stacks → Add stack** beispielsweise den Stack `energylab` mit folgender Vorlage an:

```yaml
services:
  energylab:
    image: energylab:1.2.2
    container_name: energylab
    restart: unless-stopped
    ports:
      - "8090:8090"
    environment:
      TZ: "Europe/Berlin"
      HA_URL: "http://HOME_ASSISTANT_HOST:8123"
      HA_TOKEN: ""
      HA_GRID_IMPORT_ENTITY: ""
      HA_PV_SELF_ENTITY: ""
      HA_GAS_ENTITY: ""
      ENERGYLAB_SYNC_HOUR: "23"
      ENERGYLAB_SYNC_MINUTE: "30"
      BUY_ME_A_COFFEE_URL: "https://www.paypal.com/paypalme/SebastianM207"
    volumes:
      - energylab_data:/data

volumes:
  energylab_data:
```

Ersetze Hostnamen, Token und Sensor-IDs durch deine eigenen Werte. Nicht verwendete Sensorvariablen dürfen leer bleiben. Veröffentliche deinen Token niemals in GitHub, Screenshots oder Supportanfragen.

### 2.3 Stack starten

1. Klicke auf **Deploy the stack**.
2. Warte, bis der Container den Status **running** beziehungsweise **healthy** zeigt.
3. Öffne `http://IP-DEINES-DOCKER-HOSTS:8090`.

## 3. Installation mit dem GitHub-Container

Alternativ kann das von GitHub Actions gebaute Image verwendet werden:

```yaml
image: ghcr.io/lrdtiberius/energylab:latest
```

Die übrigen Einstellungen und das Volume entsprechen der Portainer-Vorlage. Mit `docker compose` wird der Stack so gestartet:

```bash
docker compose pull
docker compose up -d
```

## 4. Home Assistant vorbereiten

### 4.1 Langlebigen Zugriffstoken erstellen

1. Öffne in Home Assistant dein Benutzerprofil.
2. Öffne **Sicherheit**.
3. Scrolle zu **Langlebige Zugriffstoken**.
4. Erstelle einen Token, zum Beispiel mit dem Namen `EnergyLab`.
5. Kopiere ihn sofort und trage ihn lokal als `HA_TOKEN` ein.

Home Assistant zeigt diesen Token nur einmal vollständig an. Bei einem versehentlich veröffentlichten Token muss der alte Token in Home Assistant widerrufen und ein neuer erzeugt werden.

### 4.2 Erreichbare Home-Assistant-Adresse wählen

`HA_URL` muss aus dem Docker-Container erreichbar sein. Eine feste LAN-Adresse ist meist zuverlässiger als mDNS:

```yaml
HA_URL: "http://HOME_ASSISTANT_HOST:8123"
```

`localhost` verweist innerhalb des Containers auf EnergyLab selbst und ist normalerweise falsch. Falls `homeassistant.local` im Container nicht aufgelöst wird, verwende die LAN-IP des Home-Assistant-Systems.

### 4.3 Sensoren auswählen

Die Sensor-IDs findest du in Home Assistant unter **Entwicklerwerkzeuge → Zustände**. Verwende nach Möglichkeit fortlaufende Gesamtzähler mit Langzeitstatistiken.

| EnergyLab-Variable | Erwarteter Wert | Einheit |
|---|---|---|
| `HA_GRID_IMPORT_ENTITY` | gesamter Netzbezug | kWh |
| `HA_PV_SELF_ENTITY` | gesamter selbst verbrauchter PV-Strom | kWh |
| `HA_GAS_ENTITY` | Gas-Gesamtzähler | m³ |

Der PV-Sensor muss den **Eigenverbrauch** liefern. Ein reiner PV-Erzeugungs- oder Einspeisezähler bildet nicht dieselbe Größe ab. Wasser benötigt keine Sensorvariable.

## 5. Erster Start und Prüfung

1. Öffne in EnergyLab die Seite **Energie**.
2. Prüfe, ob Home Assistant als eingerichtet angezeigt wird.
3. Klicke auf **Jetzt synchronisieren**.
4. Kontrolliere das Importprotokoll auf derselben Seite.
5. Prüfe die angezeigten Einheiten und Zählerstände.

Beim ersten gültigen Stand legt EnergyLab die Ausgangsbasis an. Ein Verbrauch entsteht aus der Differenz zum nächsten gültigen Zählerstand.

## 6. Historische Home-Assistant-Daten importieren

1. Öffne **Energie → Historie einmalig importieren**.
2. Wähle das gewünschte Startdatum.
3. Starte den Import.

EnergyLab übernimmt tägliche Home-Assistant-Langzeitstatistiken. Ein erneuter Lauf aktualisiert vorhandene Tage und erzeugt keine Duplikate. Importierbar sind nur Zeiträume, die Home Assistant im Recorder beziehungsweise in seinen Langzeitstatistiken noch gespeichert hat.

Nullwerte während eines Sensorausfalls, Zählerrücksprünge und unplausibel große Sprünge werden markiert und nicht in Verbrauch oder Kosten eingerechnet. Die ursprünglichen Messwerte bleiben zur Kontrolle sichtbar.

## 7. Automatische Synchronisierung

Standardmäßig synchronisiert EnergyLab täglich um 23:30 Uhr in der mit `TZ` festgelegten Zeitzone:

```yaml
TZ: "Europe/Berlin"
ENERGYLAB_SYNC_HOUR: "23"
ENERGYLAB_SYNC_MINUTE: "30"
```

Der laufende Container prüft den Termin einmal pro Minute und führt pro Kalendertag höchstens einen automatischen Versuch aus. Wird der Container erst nach 23:30 Uhr gestartet, wird der Versuch an diesem Abend nachgeholt. Der manuelle Button bleibt jederzeit nutzbar.

## 8. Tarife und Verträge einrichten

EnergyLab unterstützt jeweils bis zu fünf Strom-, Gas-, Wasser- und Abwassertarife. Für jeden Vertrag werden Anbieter, Gültigkeitszeitraum, Verbrauchspreis, Grundpreis und Zahlungsbetrag hinterlegt. Tarifzeiträume dürfen sich innerhalb derselben Energieart nicht überschneiden.

Zusätzlich kannst du den Zahlungsrhythmus, den Zahlungstag, das optionale Datum **Erste Zahlung** und den exakten Kontonamen aus FinanzLab speichern. Die erste Zahlung dient als Rhythmusanker, wenn die tatsächliche erste Fälligkeit nicht dem rechnerischen Termin ab Vertragsbeginn entspricht. Ändert sich später nur der Betrag, verwende im bestehenden Vertrag **Neuer Betrag gültig ab Monat**. So bleiben frühere Zahlungen historisch korrekt erhalten.

### Strom

- Arbeitspreis in **Cent/kWh**, zum Beispiel `32,90`
- Grundpreis in **€/Monat**
- Betrag je Zahlung und Zahlungsrhythmus

### Gas

- Arbeitspreis in **Cent/kWh**
- Umrechnungsfaktor in **kWh/m³** aus der Gasabrechnung
- Grundpreis in **€/Monat** sowie Betrag je Zahlung und Zahlungsrhythmus

EnergyLab rechnet den gemessenen m³-Verbrauch mit dem zum jeweiligen Vertragszeitraum gehörenden Faktor in kWh um.

### Wasser

- Verbrauchspreis in **€/m³**
- Grundpreis in **€/Monat** sowie Betrag je Zahlung und Zahlungsrhythmus

### Abwasser

- Verbrauchspreis in **€/m³**
- eigener Grundpreis, eigener Zahlungsbetrag und eigener Zahlungsrhythmus
- kein zusätzlicher Sensor: Die Verbrauchsmenge wird automatisch aus dem Wasserzähler übernommen

Wasser- und Abwasserkosten bleiben getrennt. Der Wasserpreis darf deshalb nur die Trinkwasserkosten enthalten.

Die Abrechnungsvorschau berechnet:

```text
Verbrauchskosten + Grundpreis der berührten Kalendermonate = Gesamtkosten
Tatsächlich angesetzte Zahlungen − Gesamtkosten = Guthaben oder Nachzahlung
```

Für eine Hochrechnung werden ein Tarif-Enddatum und mindestens zwei gültige Zählerstände benötigt. Der monatliche Grundpreis wird nicht tageweise gekürzt. Auch in einem noch nicht abgeschlossenen Monat zeigt die Monatsansicht den vollständigen Monatsgrundpreis und den vollständigen fälligen Zahlungsbetrag. Quartals- oder Jahreszahlungen werden nur in den nach Rhythmus und erster Zahlung fälligen Monaten angesetzt.

## 9. Zählerstände manuell erfassen

1. Öffne **Strom**, **Gas** oder **Wasser**.
2. Trage Ablesedatum und Zählerstand ein.
3. Speichere den Stand.

Rückwirkende Werte sind erlaubt. Ein älterer Stand darf niedriger als der heutige Startwert sein, muss aber zeitlich und wertmäßig zwischen einem eventuell vorhandenen früheren und späteren Zählerstand liegen. Nach einer rückwirkenden Eingabe berechnet EnergyLab alle betroffenen Verbrauchsdifferenzen neu.

Eine Eingabe für ein bereits gespeichertes Datum korrigiert genau diesen Tageswert.

## 10. Monatsansichten und direkter Rücksprung

Klicke im Dashboard auf Strom, PV, Gas, Wasser oder Abwasser und wähle **Monat**. Mit den Pfeilen wechselst du durch die vorhandenen Monate. Aus einem historischen Monat führt **Zum aktuellen Monat** mit einem Klick zurück zur aktuellen Übersicht. Zukünftige Monate sind gesperrt.

## 11. Vergleich und Export

Unter **Vergleich** lassen sich zwei frei wählbare Zeiträume gegenüberstellen. Angezeigt werden Verbrauch und dessen Differenz, Verbrauchskosten, Grundpreis, Gesamtkosten sowie Guthaben oder Nachzahlung.

Unter **Einstellungen** steht ein Excel-Export mit Übersicht, Tarifen sowie allen automatisch und manuell erfassten Zählerständen bereit. Unter **Unterstützung** können zusätzlich ein vollständiges JSON-Backup und die Tankungen als CSV exportiert werden.

## 12. Datensicherung

Die wichtigste Sicherung ist das Docker-Volume `energylab_data`. Zusätzlich empfiehlt sich regelmäßig:

1. **Unterstützung → JSON-Backup** herunterladen.
2. **Einstellungen → XLSX herunterladen** für eine lesbare Auswertung.
3. Das Docker-Volume mit der Sicherungsmethode des eigenen Servers sichern.

Das Volume enthält die SQLite-Datenbank unter `/data/energylab.sqlite3`.

## 13. Update einer bestehenden Installation

1. Erstelle zuerst ein JSON-Backup.
2. Baue das neue Standalone-Archiv unter einem neuen Image-Namen, zum Beispiel `energylab:1.2.2`, oder ziehe `ghcr.io/lrdtiberius/energylab:latest` neu.
3. Ändere ausschließlich den Image-Namen im Stack.
4. Aktualisiere den Stack.
5. Behalte das vorhandene Volume `energylab_data` unverändert bei.
6. Öffne EnergyLab und kontrolliere Version, Datenstand und Importprotokoll.

EnergyLab führt erforderliche Datenbankanpassungen beim Start automatisch durch.

## 14. Fehlerbehebung

### Home Assistant ist nicht erreichbar

- `HA_URL` aus Sicht des Containers prüfen
- statt `homeassistant.local` eine LAN-IP verwenden
- Port `8123` und Firewall prüfen
- keinen abschließenden API-Pfad an die URL hängen

### Nicht autorisiert oder Tokenfehler

- langlebigen Token ohne zusätzliche Anführungszeichen oder Leerzeichen neu eintragen
- widerrufenen oder veröffentlichten Token ersetzen
- Stack nach Änderung neu bereitstellen

### Keine historischen Daten

- prüfen, ob der Sensor Home-Assistant-Langzeitstatistiken besitzt
- richtigen fortlaufenden Gesamtzähler verwenden
- Startdatum innerhalb der gespeicherten Recorder-Historie wählen

### Ein Wert wird ausgeschlossen

Die Detailansicht zeigt den Ausschlussgrund. Die optionalen Plausibilitätsgrenzen können im Stack angepasst werden:

| Variable | Standard |
|---|---:|
| `ENERGYLAB_MAX_GRID_KWH_DAY` | 250 kWh/Tag |
| `ENERGYLAB_MAX_PV_KWH_DAY` | 250 kWh/Tag |
| `ENERGYLAB_MAX_GAS_M3_DAY` | 100 m³/Tag |

Die Grenzwerte werden bei echten Datenlücken mit der Anzahl der vergangenen Tage multipliziert. Sie sollten nur geändert werden, wenn der reale Maximalverbrauch sicher bekannt ist.

### Gesundheitsprüfung des Containers

EnergyLab stellt den Endpunkt `/api/health` bereit. Ein lokaler Test lautet beispielsweise:

```bash
curl http://127.0.0.1:8090/api/health
```

Eine erfolgreiche Antwort enthält den Status und die installierte Versionsnummer.
