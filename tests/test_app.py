import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["ENERGYLAB_DATA_DIR"] = TEST_DATA.name

import app  # noqa: E402


CSV_SAMPLE = """Datum;Km-Stand;Teil-Km;Spritmenge;Kosten;Währung;Tankart;Reifen;Strecken;Fahrweise;Kraftstoff;Bemerkung;Verbrauch;BC-Verbrauch;BC-Spritmenge;BC-Geschwindigkeit;Tankstelle;Land;Großraum;Ort
03.03.2030;10000,00;500,00;50,00;8750,00;EUR;1;;;0;1;;;;;;;D;;
02.02.2030;9500,00;500,00;40,00;1,75;EUR;1;;;0;1;;;;;;;D;;
01.01.2030;9000,00;500,00;60,00;105,00;EUR;1;;;0;1;;;;;;;D;;
01.12.2029;8500,00;500,00;55,00;;EUR;1;;;0;1;;;;;;;D;;
"""


class EnergyLabTests(unittest.TestCase):
    def setUp(self):
        db = Path(TEST_DATA.name) / "energylab.sqlite3"
        if db.exists():
            db.unlink()
        app.init_db()

    def test_seed_vehicle_and_fluids(self):
        with app.connect() as db:
            vehicle = db.execute("SELECT * FROM vehicles").fetchone()
            self.assertEqual(vehicle["name"], "VW T6.1")
            self.assertEqual(set(app.vehicle_fluids(db, vehicle["id"])), {"Diesel", "AdBlue"})

    def test_spritmonitor_cost_normalization(self):
        rows = app.parse_spritmonitor_csv(CSV_SAMPLE.encode(), "Diesel")
        self.assertEqual(len(rows), 4)
        by_date = {row["fueled_on"]: row for row in rows}
        self.assertEqual(by_date["2030-03-03"]["total_price"], 87.50)
        self.assertEqual(by_date["2030-02-02"]["total_price"], 70.00)
        self.assertEqual(by_date["2030-01-01"]["total_price"], 105.00)
        self.assertIn("Centwert", by_date["2030-03-03"]["warning"])
        self.assertIn("Literpreis", by_date["2030-02-02"]["warning"])
        self.assertEqual(by_date["2029-12-01"]["total_price"], 0)
        self.assertEqual(by_date["2029-12-01"]["warning"], "Preis fehlt")

    def test_weighted_full_to_full_consumption(self):
        with app.connect() as db:
            vehicle_id = db.execute("SELECT id FROM vehicles").fetchone()["id"]
            data = [
                ("2026-01-01", 1000, 50, "first"),
                ("2026-01-10", 1300, 20, "partial"),
                ("2026-01-20", 1600, 30, "full"),
                ("2026-02-01", 2100, 40, "full"),
            ]
            for day, odo, liters, fill_type in data:
                db.execute(
                    "INSERT INTO fuelings(vehicle_id,fueled_on,odometer,fluid,liters,unit_price,total_price,fill_type) VALUES(?,?,?,?,?,?,?,?)",
                    (vehicle_id, day, odo, "Diesel", liters, 1.7, liters * 1.7, fill_type),
                )
            summary = app.consumption_summary(db, vehicle_id)
        self.assertAlmostEqual(summary["average"], 90 / 1100 * 100, places=6)
        self.assertEqual(len(summary["cycles"]), 2)

    def test_signed_blob_rejects_tampering(self):
        token = app.sign_blob({"ok": True})
        self.assertEqual(app.verify_blob(token), {"ok": True})
        self.assertIsNone(app.verify_blob(token + "x"))

    def test_historical_statistics_are_idempotent(self):
        start = int(datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
        metrics = {
            "grid_import": {"entity": "sensor.example_total", "unit": "kWh"},
        }
        result = {
            "sensor.example_total": [
                {"start": start, "state": 1000.0, "change": 5.5},
                {"start": start + 86_400_000, "state": 1007.0, "change": 7.0},
            ]
        }
        self.assertEqual(app.import_statistics_rows(result, metrics)["grid_import"], 2)
        self.assertEqual(app.import_statistics_rows(result, metrics)["grid_import"], 2)
        with app.connect() as db:
            rows = db.execute("SELECT * FROM energy_readings ORDER BY read_on").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["delta_value"], 5.5)
        self.assertEqual(rows[1]["total_value"], 1007.0)
        self.assertEqual(rows[1]["source"], "home_assistant_history")

    def test_energy_tariffs_calculate_period_costs(self):
        self.assertAlmostEqual(app.tariff_price_to_eur("grid_import", "32,90"), 0.329)
        self.assertAlmostEqual(app.tariff_price_to_eur("gas", "10,90"), 0.109)
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("grid_import", "2025-01-02", 1000, 10, "kWh", "sensor.example_grid"),
            )
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("gas", "2025-01-02", 500, 2, "m³", "sensor.example_gas"),
            )
            db.execute(
                "INSERT INTO energy_tariffs(metric,valid_from,valid_to,price_per_kwh,kwh_per_unit) VALUES(?,?,?,?,?)",
                ("grid_import", "2025-01-01", "2025-12-31", 0.329, 1.0),
            )
            db.execute(
                "INSERT INTO energy_tariffs(metric,valid_from,valid_to,price_per_kwh,kwh_per_unit) VALUES(?,?,?,?,?)",
                ("gas", "2025-01-01", "2025-12-31", 0.10, 10.5),
            )
            costs = app.energy_costs(db)
        self.assertAlmostEqual(costs["grid_import"], 3.29)
        self.assertAlmostEqual(costs["gas"], 2.1)

    def test_base_fee_advances_and_balance(self):
        self.assertAlmostEqual(app.prorated_months("2025-01-01", "2025-01-31"), 1.0)
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("grid_import", "2025-01-31", 1000, 100, "kWh", "sensor.example_grid"),
            )
            db.execute(
                """INSERT INTO energy_tariffs(
                       metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
                       base_fee_monthly,advance_monthly
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("grid_import", "Beispiel", "2025-01-01", "2025-01-31", 0.329, 1.0, 12.0, 100.0),
            )
            values = app.energy_finances(db)["grid_import"]
        self.assertAlmostEqual(values["variable"], 32.9)
        self.assertAlmostEqual(values["base_fee"], 12.0)
        self.assertAlmostEqual(values["cost"], 44.9)
        self.assertAlmostEqual(values["advance"], 100.0)
        self.assertAlmostEqual(values["balance"], 55.1)

    def test_existing_tariffs_gain_provider_column(self):
        with app.connect() as db:
            db.execute("DROP TABLE energy_tariffs")
            db.execute(
                """CREATE TABLE energy_tariffs (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       metric TEXT NOT NULL,
                       valid_from TEXT NOT NULL,
                       valid_to TEXT,
                       price_per_kwh REAL NOT NULL,
                       kwh_per_unit REAL NOT NULL,
                       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            db.execute(
                "INSERT INTO energy_tariffs(metric,valid_from,valid_to,price_per_kwh,kwh_per_unit) VALUES(?,?,?,?,?)",
                ("grid_import", "2025-01-01", "2025-12-31", 0.329, 1.0),
            )
        app.init_db()
        with app.connect() as db:
            row = db.execute("SELECT provider,base_fee_monthly,advance_monthly FROM energy_tariffs").fetchone()
        self.assertEqual(row["provider"], "")
        self.assertEqual(row["base_fee_monthly"], 0)
        self.assertEqual(row["advance_monthly"], 0)

    def test_legacy_cent_prices_are_corrected_once(self):
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_tariffs(metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit) VALUES(?,?,?,?,?,?)",
                ("grid_import", "Altstrom", "2024-01-01", "2024-12-31", 32.9, 1.0),
            )
            db.execute(
                "INSERT INTO energy_tariffs(metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit) VALUES(?,?,?,?,?,?)",
                ("gas", "Altgas", "2024-01-01", "2024-12-31", 10.9, 10.5),
            )
        app.init_db()
        app.init_db()
        with app.connect() as db:
            prices = {row["metric"]: row["price_per_kwh"] for row in db.execute("SELECT metric,price_per_kwh FROM energy_tariffs")}
        self.assertAlmostEqual(prices["grid_import"], 0.329)
        self.assertAlmostEqual(prices["gas"], 0.109)


if __name__ == "__main__":
    unittest.main()
