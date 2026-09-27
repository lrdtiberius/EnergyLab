# EnergyLab-Handbuch

Gültig für Version **1.2.2**.

## Überblick

EnergyLab verwaltet Strom, PV-Eigenverbrauch, Gas, Wasser, Abwasser sowie Fahrzeuge. Messwerte können manuell oder aus Home Assistant übernommen werden. Tarifzeiträume, Grundpreise und Zahlungen bleiben historisch getrennt.

## Energie und Verträge

Für jede Energieart lassen sich Zählerstände, Tarife und Zahlungsrhythmen pflegen. Ein Tarif besitzt einen Gültigkeitszeitraum, Anbieter, Verbrauchspreis, Grundpreis, Zahlungsbetrag, Rhythmus, Zahlungstag und optional das Datum der ersten Zahlung. Spätere Betragsänderungen werden mit eigenem Wirksamkeitsdatum gespeichert und verändern die Vergangenheit nicht.

Abwasser verwendet den Wasserverbrauch als Messbasis, besitzt aber eigene Tarife und Zahlungen. Gas kann mit einem Umrechnungsfaktor vom Zählerstand in kWh ausgewertet werden.

## Ist-Zahlungen und FinanzLab

Bestätigte Zahlungen können aus FinanzLab übernommen oder manuell erfasst werden. Unterstützt werden normale Zahlungen, Erstattungen, Rücklastschriften, Korrekturen und Zahlungspausen. Stornierungen entfernen den Prüfverlauf nicht.

Die Verbindung wird unter **Einstellungen → FinanzLab-Zahlungen** eingerichtet. Erforderlich sind die aus dem EnergyLab-Container erreichbare Adresse und der Haushalt als Name oder ID. Ein Zugriffsschlüssel ist optional und muss auf beiden Seiten übereinstimmen.

## Schlussabrechnungen

Eine Schlussabrechnung kann als Snapshot fixiert werden. Der Snapshot hält den damaligen Berechnungs- und Zahlungsstand fest und wird durch spätere Tarif- oder Zahlungsänderungen nicht nachträglich umgeschrieben.

## Fahrzeuge

Fahrzeuge besitzen einen Namen und zulässige Betriebsstoffe. Tankungen erfassen Datum, Kilometerstand, Menge, Literpreis, Tankstelle und Voll- oder Teiltankung. Weitere Kosten wie Versicherung, Steuer, Wartung, Reparatur oder Reifen werden separat erfasst und gemeinsam mit den Tankkosten pro Kilometer ausgewertet.

Der Spritmonitor-Import liest CSV-Dateien zunächst nur in eine Vorschau. Auffällige Cent-, Literpreis- oder Mengenwerte werden markiert; erst die ausdrückliche Bestätigung schreibt die geprüften Zeilen. Bereits importierte Zeilen werden nicht doppelt angelegt.

## Sicherungen

Unter **Einstellungen → Datenbanksicherungen** kann jederzeit eine konsistente SQLite-Sicherung erstellt, heruntergeladen oder wiederhergestellt werden. Vor Updates und Wiederherstellungen legt EnergyLab zusätzliche Sicherungen an. Die Dateien liegen unter `/data/backups` im Daten-Volume.

Sicherungen im selben Volume ersetzen kein externes Backup. Das gesamte Volume sollte regelmäßig auf einen anderen Datenträger gesichert werden.

## Bekannte Grenzen

- Das vorgefertigte Image enthält keine persönlichen Sensoren, Tokens oder Finanzdaten; diese werden erst über Umgebungsvariablen und das Daten-Volume bereitgestellt.
- Home-Assistant- und FinanzLab-Adressen müssen aus dem Container erreichbar sein; `localhost` bezeichnet den EnergyLab-Container selbst.
- Die Anwendung ist für ein vertrauenswürdiges lokales Netzwerk gedacht und sollte nicht ungeschützt aus dem Internet erreichbar sein.
