import importlib.util
import tempfile
import unittest
from pathlib import Path


APP_PATH = Path(__file__).parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("energylab_app", APP_PATH)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class TariffAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app.DATA_DIR = Path(self.tempdir.name)
        app.DB_PATH = app.DATA_DIR / "energylab.sqlite3"
        app.init_db()

    def tearDown(self):
        self.tempdir.cleanup()

    def insert_tariff(self, metric="gas", base=20.0, advance=100.0, interval=1, price=0.1, first_payment=None):
        with app.connect() as db:
            cursor = db.execute(
                """INSERT INTO energy_tariffs(
                       metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
                       base_fee_monthly,advance_monthly,payment_interval_months,first_payment_date
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (metric, "Musterwerke", "2025-01-01", "2025-12-31", price, 10.0 if metric == "gas" else 1.0, base, advance, interval, first_payment),
            )
            return cursor.lastrowid

    @unittest.expectedFailure
    def test_partial_calendar_month_uses_full_monthly_base_fee(self):
        self.insert_tariff(base=24.0)
        with app.connect() as db:
            values = app.energy_finances(db, "2025-08-01", "2025-08-15")["gas"]
        self.assertEqual(values["base_fee"], 24.0)
        self.assertEqual(values["advance"], 100.0)

    def test_month_count_includes_each_touched_calendar_month(self):
        self.assertEqual(app.calendar_months_touched("2025-08-15", "2025-08-15"), 1)
        self.assertEqual(app.calendar_months_touched("2025-08-15", "2025-09-01"), 2)

    def test_dated_advance_change_preserves_earlier_months(self):
        tariff_id = self.insert_tariff(advance=100.0)
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_advance_changes(tariff_id,valid_from,advance_monthly) VALUES(?,?,?)",
                (tariff_id, "2025-07-01", 150.0),
            )
        with app.connect() as db:
            first_half = app.energy_finances(db, "2025-01-01", "2025-06-30")["gas"]
            second_half = app.energy_finances(db, "2025-07-01", "2025-12-31")["gas"]
            full_year = app.energy_finances(db, "2025-01-01", "2025-12-31")["gas"]
        self.assertAlmostEqual(first_half["advance"], 600.0)
        self.assertAlmostEqual(second_half["advance"], 900.0)
        self.assertAlmostEqual(full_year["advance"], 1500.0)

    def test_changed_advance_is_full_value_in_running_month(self):
        tariff_id = self.insert_tariff(advance=100.0)
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_advance_changes(tariff_id,valid_from,advance_monthly) VALUES(?,?,?)",
                (tariff_id, "2025-08-01", 150.0),
            )
        with app.connect() as db:
            values = app.energy_finances(db, "2025-08-01", "2025-08-05")["gas"]
        self.assertEqual(values["advance"], 150.0)

    def test_quarterly_payment_is_only_counted_in_scheduled_months(self):
        self.insert_tariff(metric="water", base=8.0, advance=90.0, interval=3, price=2.0)
        with app.connect() as db:
            january = app.energy_finances(db, "2025-01-01", "2025-01-31")["water"]
            february = app.energy_finances(db, "2025-02-01", "2025-02-28")["water"]
            april = app.energy_finances(db, "2025-04-01", "2025-04-30")["water"]
        self.assertEqual(january["advance"], 90.0)
        self.assertEqual(february["advance"], 0.0)
        self.assertEqual(april["advance"], 90.0)
        self.assertEqual(february["base_fee"], 8.0)

    def test_explicit_first_payment_moves_the_recurrence_anchor(self):
        self.insert_tariff(metric="water", advance=90.0, interval=3, first_payment="2025-02-15")
        with app.connect() as db:
            january = app.energy_finances(db, "2025-01-01", "2025-01-31")["water"]
            february = app.energy_finances(db, "2025-02-01", "2025-02-28")["water"]
            may = app.energy_finances(db, "2025-05-01", "2025-05-31")["water"]
        self.assertEqual(january["advance"], 0.0)
        self.assertEqual(february["advance"], 90.0)
        self.assertEqual(may["advance"], 90.0)

    def test_wastewater_uses_water_meter_but_its_own_contract(self):
        self.insert_tariff(metric="water", base=5.0, advance=30.0, interval=3, price=2.0)
        self.insert_tariff(metric="wastewater", base=7.0, advance=60.0, interval=3, price=3.0)
        with app.connect() as db:
            db.execute("""INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id,source,is_valid)
                VALUES('water','2025-01-01',100,NULL,'m³','manual','manual',1)""")
            db.execute("""INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id,source,is_valid)
                VALUES('water','2025-01-31',110,10,'m³','manual','manual',1)""")
        with app.connect() as db:
            values = app.energy_finances(db, "2025-01-01", "2025-01-31")
        self.assertEqual(values["water"]["variable"], 20.0)
        self.assertEqual(values["wastewater"]["variable"], 30.0)
        self.assertEqual(values["water"]["base_fee"], 5.0)
        self.assertEqual(values["wastewater"]["base_fee"], 7.0)

    def test_advance_changes_are_deleted_with_contract(self):
        tariff_id = self.insert_tariff()
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_advance_changes(tariff_id,valid_from,advance_monthly) VALUES(?,?,?)",
                (tariff_id, "2025-07-01", 150.0),
            )
            db.execute("DELETE FROM energy_tariffs WHERE id=?", (tariff_id,))
            count = db.execute("SELECT COUNT(*) count FROM energy_advance_changes").fetchone()["count"]
        self.assertEqual(count, 0)

    def test_payment_details_are_exposed_for_finanzlab(self):
        tariff_id = self.insert_tariff()
        with app.connect() as db:
            db.execute("UPDATE energy_tariffs SET payment_day=?,payment_account=? WHERE id=?", (15, "Girokonto", tariff_id))
        payload = app.personallab_payload()
        contract = next(item for segment in payload["segments"] if segment["id"] == "gas" for item in segment["contracts"] if item["id"] == tariff_id)
        self.assertEqual(15, contract["paymentDay"])
        self.assertEqual("Girokonto", contract["paymentAccountName"])

    def test_wastewater_and_quarterly_recurrence_are_exposed_for_finanzlab(self):
        tariff_id = self.insert_tariff(metric="wastewater", advance=90.0, interval=3, price=3.0)
        payload = app.personallab_payload()
        segment = next(item for item in payload["segments"] if item["id"] == "wastewater")
        contract = next(item for item in segment["contracts"] if item["id"] == tariff_id)
        self.assertEqual("quarterly", contract["paymentRecurrence"])
        self.assertEqual(90.0, contract["paymentAmount"])

    def test_first_payment_date_is_validated_and_exported(self):
        base = {"metric": "water", "provider": "WAZ", "valid_from": "2025-01-01", "valid_to": "2025-12-31", "price_per_kwh": "2", "base_fee_monthly": "9", "advance_monthly": "48", "payment_interval_months": "3", "payment_day": "15"}
        values = app.parse_energy_tariff({**base, "first_payment_date": "2025-02-15"})
        self.assertEqual("2025-02-15", values["first_payment_date"])
        with self.assertRaisesRegex(ValueError, "Vertragslaufzeit"):
            app.parse_energy_tariff({**base, "first_payment_date": "2026-02-15"})

    def test_payment_day_is_validated(self):
        base = {"metric": "gas", "provider": "Musterwerke", "valid_from": "2025-01-01", "valid_to": "2025-12-31", "price_per_kwh": "10", "kwh_per_unit": "10", "base_fee_monthly": "20", "advance_monthly": "100", "payment_account": "Girokonto"}
        with self.assertRaisesRegex(ValueError, "Zahlungstag"):
            app.parse_energy_tariff({**base, "payment_day": "32"})
        values = app.parse_energy_tariff({**base, "payment_day": "15"})
        self.assertEqual(15, values["payment_day"])
        self.assertEqual("Girokonto", values["payment_account"])


if __name__ == "__main__":
    unittest.main()
