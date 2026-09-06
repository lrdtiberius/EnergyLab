import importlib.util
import tempfile
import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("energylab_app", APP_PATH)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class EnergyFinanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA_DIR = Path(self.temp.name)
        app.DB_PATH = app.DATA_DIR / "test.sqlite3"
        app.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def add_tariff(self, metric="grid_import", price=1.0, base=10.0, advance=500 / 12,
                   valid_from="2025-01-01", valid_to="2025-12-31", provider="Test"):
        with app.connect() as db:
            db.execute(
                """INSERT INTO energy_tariffs
                   (metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,base_fee_monthly,advance_monthly)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (metric, provider, valid_from, valid_to, price, 1.0, base, advance),
            )

    def add_reading(self, day, total, delta=None, metric="grid_import"):
        with app.connect() as db:
            db.execute(
                """INSERT INTO energy_readings
                   (metric,read_on,total_value,delta_value,unit,entity_id,source,is_valid)
                   VALUES(?,?,?,?,?,?,?,1)""",
                (metric, day, total, delta, "kWh", "test", "test"),
            )

    def test_maximum_credit_excludes_base_fee_from_credit(self):
        self.add_tariff(price=1.0, base=10.0, advance=500 / 12)
        self.add_reading("2025-01-01", 1000, None)
        with app.connect() as db:
            values = app.energy_finances(db, "2025-01-01", "2025-12-31")["grid_import"]
        self.assertAlmostEqual(values["variable"], 0.0, places=6)
        self.assertAlmostEqual(values["base_fee"], 120.0, places=6)
        self.assertAlmostEqual(values["cost"], 120.0, places=6)
        self.assertAlmostEqual(values["advance"], 500.0, places=6)
        self.assertAlmostEqual(values["balance"], 380.0, places=6)

    def test_consumption_and_base_fee_are_both_costs(self):
        self.add_tariff(price=1.0, base=10.0, advance=500 / 12)
        self.add_reading("2025-01-01", 1000, None)
        self.add_reading("2025-12-31", 1250, 250)
        with app.connect() as db:
            values = app.energy_finances(db, "2025-01-01", "2025-12-31")["grid_import"]
        self.assertAlmostEqual(values["variable"], 250.0)
        self.assertAlmostEqual(values["cost"], 370.0)
        self.assertAlmostEqual(values["balance"], 130.0)

    def test_end_date_prevents_later_readings_leaking_into_month(self):
        self.add_tariff(price=1.0, base=10.0, advance=50.0)
        self.add_reading("2025-01-01", 1000, None)
        self.add_reading("2025-01-31", 1010, 10)
        self.add_reading("2025-02-01", 1110, 100)
        with app.connect() as db:
            january = app.energy_finances(db, "2025-01-01", "2025-01-31")["grid_import"]
        self.assertAlmostEqual(january["variable"], 10.0)
        self.assertAlmostEqual(january["base_fee"], 10.0)
        self.assertAlmostEqual(january["cost"], 20.0)
        self.assertAlmostEqual(january["balance"], 30.0)

    def test_contract_change_splits_consumption_fees_and_advances(self):
        self.add_tariff(price=1.0, base=31.0, advance=310.0,
                        valid_from="2025-01-01", valid_to="2025-01-16", provider="Alt")
        self.add_tariff(price=2.0, base=62.0, advance=620.0,
                        valid_from="2025-01-17", valid_to="2025-01-31", provider="Neu")
        self.add_reading("2025-01-01", 1000, None)
        self.add_reading("2025-01-31", 1300, 300)
        with app.connect() as db:
            january = app.energy_finances(db, "2025-01-01", "2025-01-31")["grid_import"]
        # 15 Verbrauchstage alt zu 1 € + 15 Tage neu zu 2 €.
        self.assertAlmostEqual(january["variable"], 450.0)
        # Jeder berührte Kalendermonat trägt je Vertrag den vollen Monatsgrundpreis.
        self.assertAlmostEqual(january["base_fee"], 93.0)
        # Auch Abschläge werden je berührtem Vertragsmonat vollständig angesetzt.
        self.assertAlmostEqual(january["advance"], 930.0)
        self.assertAlmostEqual(january["cost"], 543.0)
        self.assertAlmostEqual(january["balance"], 387.0)

        with app.connect() as db:
            forecast = app.settlement_forecast(db, "grid_import")
        self.assertAlmostEqual(forecast["current"]["consumption"], 150.0)
        self.assertAlmostEqual(forecast["current"]["variable"], 300.0)
        self.assertAlmostEqual(forecast["current"]["base_fee"], 62.0)
        self.assertAlmostEqual(forecast["current"]["advance"], 620.0)
        self.assertAlmostEqual(forecast["current"]["balance"], 258.0)

    def test_xlsx_contains_auditable_cost_columns(self):
        data = app.build_xlsx([("Übersicht", [["Verbrauchskosten", "Grundpreis", "Gesamtkosten", "Abschläge", "Saldo"], [250, 120, 370, 500, 130]])])
        self.assertTrue(data.startswith(b"PK"))
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("Verbrauchskosten", sheet)
        self.assertIn("Grundpreis", sheet)
        self.assertIn("Gesamtkosten", sheet)

    def test_comparison_difference_reports_more_less_and_zero_baseline(self):
        less = app.comparison_difference(52, 43)
        self.assertEqual(less["label"], "weniger")
        self.assertAlmostEqual(less["difference"], -9.0)
        self.assertAlmostEqual(less["percent"], -17.3076923)
        more = app.comparison_difference(40, 50)
        self.assertEqual(more["label"], "mehr")
        self.assertAlmostEqual(more["percent"], 25.0)
        self.assertIsNone(app.comparison_difference(0, 5)["percent"])
        self.assertEqual(app.comparison_difference(0, 0)["label"], "gleich")

    def test_manual_historical_electricity_and_gas_readings_recalculate_deltas(self):
        self.add_reading("2025-12-31", 1000, None, "grid_import")
        app.save_manual_energy_reading("grid_import", "2025-01-01", "900")
        self.add_reading("2025-12-31", 500, None, "gas")
        app.save_manual_energy_reading("gas", "2025-01-01", "450")
        with app.connect() as db:
            electricity = db.execute(
                "SELECT read_on,total_value,delta_value,source FROM energy_readings WHERE metric='grid_import' ORDER BY read_on"
            ).fetchall()
            gas = db.execute(
                "SELECT read_on,total_value,delta_value,source FROM energy_readings WHERE metric='gas' ORDER BY read_on"
            ).fetchall()
        self.assertEqual(electricity[0]["source"], "manual")
        self.assertAlmostEqual(electricity[1]["delta_value"], 100.0)
        self.assertEqual(gas[0]["source"], "manual")
        self.assertAlmostEqual(gas[1]["delta_value"], 50.0)

    def test_manual_historical_reading_must_fit_between_neighbors(self):
        self.add_reading("2025-01-01", 100, None, "grid_import")
        self.add_reading("2025-12-31", 200, 100, "grid_import")
        with self.assertRaisesRegex(ValueError, "zwischen"):
            app.save_manual_energy_reading("grid_import", "2025-06-01", "250")
        with app.connect() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM energy_readings WHERE metric='grid_import' AND read_on='2025-06-01'"
            ).fetchone())

    def test_old_historical_baseline_does_not_flow_into_current_tariff(self):
        self.add_tariff(price=0.2726, base=0, advance=0,
                        valid_from="2025-09-20", valid_to="2026-09-20", provider="Octopus")
        self.add_reading("2023-06-02", 1961, None, "grid_import")
        self.add_reading("2026-04-01", 10127, 8166, "grid_import")
        self.add_reading("2026-04-03", 10128, 1, "grid_import")
        with app.connect() as db:
            consumption, cost = app.allocated_usage(
                db, "grid_import", "2025-09-20", "2026-04-03"
            )
            finances = app.energy_finances(db, "2025-09-20", "2026-04-03")["grid_import"]
        self.assertAlmostEqual(consumption, 1.0)
        self.assertAlmostEqual(cost, 0.2726)
        self.assertAlmostEqual(finances["variable"], 0.2726)

    def test_short_interval_still_splits_across_tariff_change(self):
        self.add_tariff(price=1.0, base=0, advance=0,
                        valid_from="2025-01-01", valid_to="2025-01-16", provider="Alt")
        self.add_tariff(price=2.0, base=0, advance=0,
                        valid_from="2025-01-17", valid_to="2025-01-31", provider="Neu")
        self.add_reading("2025-01-01", 1000, None)
        self.add_reading("2025-01-31", 1300, 300)
        with app.connect() as db:
            values = app.energy_finances(db, "2025-01-01", "2025-01-31")["grid_import"]
        self.assertAlmostEqual(values["variable"], 450.0)


if __name__ == "__main__":
    unittest.main()
