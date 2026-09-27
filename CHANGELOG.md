# Änderungsverlauf

## 1.2.2 – geprüfter Produktionsstand

- Anwendungscode auf den am 24. September 2026 gebauten und auf dem AM06 laufenden Stand synchronisiert
- Ist-Zahlungen, Rückerstattungen, Rücklastschriften, Korrekturen und Zahlungspausen revisionssicher ergänzt
- Schlussabrechnungen können als unveränderliche Snapshots festgehalten werden
- optionaler Zahlungsabgleich mit FinanzLab einschließlich Zugriffsschlüssel und manuellem Sofortabgleich
- automatische Sicherung vor Versionsmigrationen sowie manuelle, herunterladbare und geprüfte Wiederherstellung von Datenbanksicherungen
- Spritmonitor-CSV-Import mit Vorschau, Normalisierung auffälliger Werte und Dublettenschutz
- zusätzliche Fahrzeugkosten und gemeinsame Kosten-pro-Kilometer-Auswertung
- Web-App-Manifest und Symbole in das Standalone-Image aufgenommen
- Basisimage auf Python 3.13.15 `slim-trixie` vereinheitlicht

## 0.6.6 – Verträge, Zahlungen und Abwasser

### Neu

- eigener Abwasserbereich mit vom Wasserzähler abgeleiteter Verbrauchsmenge sowie getrenntem Tarif, Grundpreis und Zahlungen
- monatliche, quartalsweise, halbjährliche und jährliche Zahlungsrhythmen
- optionales Datum **„Erste Zahlung“** als Anker für abweichende Fälligkeiten
- FinanzLab-Konto je Vertrag und erweiterte Schnittstelle für fällige Vertragszahlungen
- historisierte Änderungen des Zahlungsbetrags mit **„Neuer Betrag ab“**
- rückwirkende manuelle Zählerstände für Strom, Gas und Wasser
- Verbrauchsdifferenz im Zeitraumvergleich
- Excel-Export aller automatisch und manuell erfassten Zählerstände

### Korrigiert

- Guthaben/Nachzahlung wird durchgängig als Zahlungen minus Verbrauchskosten und Grundpreis berechnet
- Grundpreis und Zahlungsbetrag werden in der Monatsansicht als volle Monatswerte angezeigt
- Tarifwechsel und Betragsänderungen werden exakt ab ihrem Gültigkeitszeitpunkt berücksichtigt
- historische Zählerstände vor dem gewählten Jahr oder Vertragszeitraum fließen nicht in dessen Verbrauch ein
- Abwasser nutzt dieselbe Messreihe wie Wasser, ohne Wasser- und Abwasserkosten zu vermischen

### Update

Version 0.6.6 aktualisiert die bestehende Datenbank beim Start automatisch. Das persistente Volume muss unverändert weiterverwendet werden.

## 0.6.1 – Standalone-Release

### Neu

- direkter Button **„Zum aktuellen Monat“** in allen Energie-Monatsansichten
- deaktivierter, klar erkennbarer Zustand, wenn bereits der aktuelle Monat geöffnet ist
- weiterhin vollständige Standalone-Installation ohne Patch oder vorherige Version

## 0.6.0 – Standalone-Release

### Neu

- Monatsnavigation in allen Energie-Detailansichten
- Auswahl zurückliegender Monate anhand der gespeicherten Quelldaten
- Vorwärtsnavigation nur bis zum aktuellen Monat
- monatsbezogene Verbrauchs-, Kosten-, Messwert- und Diagrammauswertung
- verständliche Monatsüberschrift und deaktivierte Navigation an den Zeitgrenzen

### Enthaltene Funktionen

- Strom-, PV-, Gas- und Wasserübersichten
- Plausibilitätsfilter für Sensorausfälle und unplausible Zählerwerte
- tägliche Home-Assistant-Synchronisierung um 23:30 Uhr
- Datenstand mit Datum und Uhrzeit
- Tarif-, Grundpreis- und Abschlagsberechnung
- Abrechnungsvorschau und Zeitraumvergleich
- Excel-Export
- Fahrzeug- und Tankkostenverwaltung

### Installation

Version 0.6.0 ist vollständig und benötigt weder einen Patch noch eine vorherige Version. Bestehende Installationen werden durch Austausch des Images aktualisiert; das persistente Docker-Volume bleibt erhalten.

### Datenschutz

Der veröffentlichte Stand enthält keine persönlichen Home-Assistant-Konfigurationen, Sensor-IDs, Zugangsdaten, Messwerte, Tarife oder Verträge.
