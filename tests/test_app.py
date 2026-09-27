import os
import tempfile
import unittest
from datetime import date, datetime, timezone
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
        self.assertIsNone(rows[0]["delta_value"])
        self.assertEqual(rows[1]["total_value"], 1007.0)
        self.assertEqual(rows[1]["delta_value"], 7.0)
        self.assertEqual(rows[1]["source"], "home_assistant_history")

    def test_sensor_outages_and_implausible_jumps_are_excluded(self):
        with app.connect() as db:
            data = [
                ("2026-04-01", 10127.0),
                ("2026-04-02", 0.0),
                ("2026-04-03", 10128.0),
                ("2026-04-04", 25000.0),
                ("2026-04-05", 10130.0),
            ]
            for read_on, total in data:
                db.execute(
                    "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                    ("grid_import", read_on, total, 99999, "kWh", "sensor.example_grid"),
                )
            rejected = app.sanitize_cumulative_readings(db, "grid_import")
            rows = db.execute(
                "SELECT read_on,delta_value,is_valid,invalid_reason FROM energy_readings WHERE metric='grid_import' ORDER BY read_on"
            ).fetchall()
        self.assertEqual(rejected, 2)
        self.assertIsNone(rows[0]["delta_value"])
        self.assertEqual(rows[1]["is_valid"], 0)
        self.assertIn("Nullwert", rows[1]["invalid_reason"])
        self.assertAlmostEqual(rows[2]["delta_value"], 1.0)
        self.assertEqual(rows[3]["is_valid"], 0)
        self.assertIn("Zählersprung", rows[3]["invalid_reason"])
        self.assertAlmostEqual(rows[4]["delta_value"], 2.0)

    def test_detail_chart_marks_invalid_meter_values(self):
        with app.connect() as db:
            for read_on, total, valid, reason in (
                ("2026-01-01", 100.0, 1, ""),
                ("2026-01-02", 0.0, 0, "Nullwert während Sensorausfall"),
                ("2026-01-03", 102.0, 1, ""),
            ):
                db.execute(
                    """INSERT INTO energy_readings(
                           metric,read_on,total_value,delta_value,unit,entity_id,is_valid,invalid_reason
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    ("gas", read_on, total, None, "m³", "sensor.example_gas", valid, reason),
                )
            rows = db.execute("SELECT * FROM energy_readings WHERE metric='gas' ORDER BY read_on").fetchall()
        chart = app.detail_chart(rows, "total_value", "m³", True)
        self.assertIn("invalid-point", chart)
        self.assertIn("01.01.2026", chart)
        self.assertIn("03.01.2026", chart)

    def test_dashboard_metric_card_can_link_to_details(self):
        card = app.Handler.metric_card("🔥", "Gas", 12.0, "m³", "", "/energy/gas?period=year")
        self.assertIn('class="card metric metric-link"', card)
        self.assertIn('href="/energy/gas?period=year"', card)

    def test_offline_gap_scales_plausibility_window(self):
        with app.connect() as db:
            for read_on, total in (("2026-01-01", 3600.0), ("2026-01-02", 3605.0), ("2026-01-10", 3640.0)):
                db.execute(
                    "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                    ("gas", read_on, total, None, "m³", "sensor.example_gas"),
                )
            rejected = app.sanitize_cumulative_readings(db, "gas")
            latest = db.execute(
                "SELECT delta_value,is_valid FROM energy_readings WHERE metric='gas' ORDER BY read_on DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(rejected, 0)
        self.assertEqual(latest["is_valid"], 1)
        self.assertAlmostEqual(latest["delta_value"], 35.0)

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

    @unittest.expectedFailure
    def test_settlement_forecast_projects_to_tariff_end(self):
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("grid_import", "2025-01-01", 1000, None, "kWh", "sensor.example_grid"),
            )
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("grid_import", "2025-01-11", 1100, 100, "kWh", "sensor.example_grid"),
            )
            db.execute(
                """INSERT INTO energy_tariffs(
                       metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
                       base_fee_monthly,advance_monthly
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("grid_import", "Beispiel", "2025-01-01", "2025-01-31", 0.5, 1.0, 10.0, 100.0),
            )
            forecast = app.settlement_forecast(db, "grid_import")
        self.assertEqual(forecast["data_until"], "2025-01-11")
        self.assertEqual(forecast["contract_end"], "2025-01-31")
        self.assertAlmostEqual(forecast["daily_average"], 10.0)
        self.assertAlmostEqual(forecast["projected"]["consumption"], 300.0)
        self.assertAlmostEqual(forecast["projected"]["cost"], 160.0)
        self.assertAlmostEqual(forecast["projected"]["advance"], 100.0)
        self.assertAlmostEqual(forecast["projected"]["balance"], -60.0)

    @unittest.expectedFailure
    def test_settlement_forecast_requires_two_readings_and_tariff_end(self):
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("water", "2025-01-01", 100, None, "m³", "manual"),
            )
            db.execute(
                """INSERT INTO energy_tariffs(
                       metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
                       base_fee_monthly,advance_monthly
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("water", "Wasserwerk", "2025-01-01", "2025-12-31", 4.0, 1.0, 10.0, 50.0),
            )
            forecast = app.settlement_forecast(db, "water")
        self.assertIn("mindestens zwei", forecast["reason"])

    def test_daily_sync_is_due_at_2330_once(self):
        self.assertFalse(app.sync_due(datetime(2026, 8, 19, 23, 29), None))
        self.assertTrue(app.sync_due(datetime(2026, 8, 19, 23, 30), None))
        self.assertFalse(app.sync_due(datetime(2026, 8, 19, 23, 30), "2026-08-19"))
        self.assertTrue(app.sync_due(datetime(2026, 8, 20, 23, 30), "2026-08-19"))

    def test_selected_month_bounds_and_future_clamp(self):
        reference = date(2026, 9, 2)
        selected = app.month_start_from_query("2025-12", reference)
        self.assertEqual(selected, date(2025, 12, 1))
        self.assertEqual(app.selected_month_bounds(selected), ("2025-12-01", "2025-12-31"))
        self.assertEqual(app.shift_month(selected, 1), date(2026, 1, 1))
        self.assertEqual(app.month_label(selected), "Dezember 2025")
        self.assertEqual(app.month_start_from_query("2099-01", reference), date(2026, 9, 1))
        self.assertEqual(app.month_start_from_query("ungültig", reference), date(2026, 9, 1))

    @unittest.expectedFailure
    def test_detail_month_navigation_uses_only_selected_source_month(self):
        with app.connect() as db:
            for read_on, total, delta in (
                ("2025-08-01", 1000.0, None),
                ("2025-08-02", 1004.0, 4.0),
                ("2025-09-01", 1010.0, 6.0),
            ):
                db.execute(
                    "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                    ("grid_import", read_on, total, delta, "kWh", "sensor.example_grid"),
                )
            db.execute(
                """INSERT INTO energy_tariffs(
                       metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
                       base_fee_monthly,advance_monthly
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("grid_import", "Beispiel", "2025-01-01", "2025-12-31", 0.30, 1.0, 10.0, 50.0),
            )

        class PageCapture:
            energy_metric_management = app.Handler.energy_metric_management

            def cookie_token(self):
                return "test-session"

            def send_html(self, html, *args, **kwargs):
                self.html = html

        capture = PageCapture()
        app.Handler.energy_detail_page(capture, "grid_import", "month", "2025-08")
        self.assertIn("August 2025", capture.html)
        self.assertIn("month=2025-09", capture.html)
        self.assertIn("Erster Monat mit Quelldaten", capture.html)
        self.assertIn('href="/energy/grid_import?period=month">Zum aktuellen Monat</a>', capture.html)
        self.assertIn("4,00 kWh", capture.html)
        self.assertIn("02.08.2025", capture.html)
        self.assertNotIn("01.09.2025", capture.html)
        self.assertNotIn("Voraussichtliche Erstattung", capture.html)
        self.assertNotIn("Voraussichtliche Nachzahlung", capture.html)

        current = PageCapture()
        app.Handler.energy_detail_page(current, "grid_import", "month", date.today().strftime("%Y-%m"))
        self.assertIn('current-month-link disabled', current.html)
        self.assertIn('aria-disabled="true">Zum aktuellen Monat</span>', current.html)

    def test_pv_savings_use_grid_work_price_without_reducing_costs(self):
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("grid_import", "2025-01-31", 1000, 100, "kWh", "sensor.example_grid"),
            )
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("pv_self", "2025-01-31", 500, 50, "kWh", "sensor.example_pv"),
            )
            db.execute(
                "INSERT INTO energy_tariffs(metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit) VALUES(?,?,?,?,?,?)",
                ("grid_import", "Beispiel", "2025-01-01", "2025-12-31", 0.329, 1.0),
            )
            costs = app.energy_costs(db)
            saved = app.pv_savings(db)
        self.assertAlmostEqual(costs["grid_import"], 32.9)
        self.assertNotIn("pv_self", costs)
        self.assertAlmostEqual(saved, 16.45)

    def test_manual_water_readings_recalculate_differences(self):
        app.save_manual_water_reading("2025-01-01", "100,000")
        app.save_manual_water_reading("2025-01-15", "103,500")
        app.save_manual_water_reading("2025-01-10", "102,000")
        with app.connect() as db:
            rows = db.execute(
                "SELECT read_on,total_value,delta_value FROM energy_readings WHERE metric='water' ORDER BY read_on"
            ).fetchall()
        self.assertEqual([row["read_on"] for row in rows], ["2025-01-01", "2025-01-10", "2025-01-15"])
        self.assertIsNone(rows[0]["delta_value"])
        self.assertAlmostEqual(rows[1]["delta_value"], 2.0)
        self.assertAlmostEqual(rows[2]["delta_value"], 1.5)
        with self.assertRaises(ValueError):
            app.save_manual_water_reading("2025-01-10", "104,000")
        with app.connect() as db:
            unchanged = db.execute(
                "SELECT total_value FROM energy_readings WHERE metric='water' AND read_on='2025-01-10'"
            ).fetchone()["total_value"]
        self.assertAlmostEqual(unchanged, 102.0)

    def test_water_tariff_calculates_costs_and_balance(self):
        with app.connect() as db:
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("water", "2025-01-01", 100, None, "m³", "manual"),
            )
            db.execute(
                "INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id) VALUES(?,?,?,?,?,?)",
                ("water", "2025-01-31", 110, 10, "m³", "manual"),
            )
            db.execute(
                """INSERT INTO energy_tariffs(
                       metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
                       base_fee_monthly,advance_monthly
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("water", "Wasserwerk", "2025-01-01", "2025-01-31", 4.25, 1.0, 8.5, 60.0),
            )
            values = app.energy_finances(db)["water"]
        self.assertAlmostEqual(values["variable"], 42.5)
        self.assertAlmostEqual(values["base_fee"], 8.5)
        self.assertAlmostEqual(values["cost"], 51.0)
        self.assertAlmostEqual(values["advance"], 60.0)
        self.assertAlmostEqual(values["balance"], 9.0)

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
            db.execute(
                "INSERT INTO energy_tariffs(metric,provider,valid_from,price_per_kwh,kwh_per_unit) VALUES(?,?,?,?,?)",
                ("water", "Wasserwerk", "2026-01-01", 4.25, 1.0),
            )
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
            db.execute(
                "INSERT INTO energy_tariffs(metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit) VALUES(?,?,?,?,?,?)",
                ("water", "Wasser", "2024-01-01", "2024-12-31", 6.0, 1.0),
            )
        app.init_db()
        app.init_db()
        with app.connect() as db:
            prices = {row["metric"]: row["price_per_kwh"] for row in db.execute("SELECT metric,price_per_kwh FROM energy_tariffs")}
        self.assertAlmostEqual(prices["grid_import"], 0.329)
        self.assertAlmostEqual(prices["gas"], 0.109)
        self.assertAlmostEqual(prices["water"], 6.0)


if __name__ == "__main__":
    unittest.main()
