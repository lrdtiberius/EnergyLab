#!/usr/bin/env python3
"""EnergieLab - dependency-free, single-file homelab web application."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import html
import io
import json
import math
import os
import secrets
import socket
import sqlite3
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn


APP_NAME = "EnergieLab"
APP_VERSION = "0.3.1"
DATA_DIR = Path(os.getenv("ENERGYLAB_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "energylab.sqlite3"
HOST = os.getenv("ENERGYLAB_HOST", "0.0.0.0")
PORT = int(os.getenv("ENERGYLAB_PORT", "8090"))
TIMEZONE = os.getenv("TZ", "Europe/Berlin")
HA_URL = os.getenv("HA_URL", "").strip().rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "").strip()
COFFEE_URL = os.getenv("BUY_ME_A_COFFEE_URL", "").strip()
MAX_UPLOAD = 5 * 1024 * 1024
TARIFF_LIMIT = 5

# Generous household safety limits. Gaps are multiplied by their day count.
MAX_DAILY_CHANGE = {
    "grid_import": float(os.getenv("ENERGYLAB_MAX_GRID_KWH_DAY", "250")),
    "pv_self": float(os.getenv("ENERGYLAB_MAX_PV_KWH_DAY", "250")),
    "gas": float(os.getenv("ENERGYLAB_MAX_GAS_M3_DAY", "100")),
}

METRICS = {
    "grid_import": {
        "label": "Strombezug",
        "icon": "⚡",
        "unit": "kWh",
        "entity": os.getenv("HA_GRID_IMPORT_ENTITY", "").strip(),
    },
    "pv_self": {
        "label": "PV-Eigenverbrauch",
        "icon": "☀️",
        "unit": "kWh",
        "entity": os.getenv("HA_PV_SELF_ENTITY", "").strip(),
    },
    "gas": {
        "label": "Gas",
        "icon": "🔥",
        "unit": "m³",
        "entity": os.getenv("HA_GAS_ENTITY", "").strip(),
    },
    "water": {
        "label": "Wasser",
        "icon": "💧",
        "unit": "m³",
        "entity": "",
        "manual_only": True,
    },
}

FLUIDS = ("Diesel", "Benzin", "E10", "Super Plus", "AdBlue", "LPG", "CNG", "Strom")
FILL_TYPES = {"first": "Erste Tankung", "full": "Volltankung", "partial": "Teiltankung"}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt_num(value, digits=2) -> str:
    if value is None:
        return "–"
    text = f"{float(value):,.{digits}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_money(value) -> str:
    return f"{fmt_num(value, 2)} €" if value is not None else "–"


def parse_num(value, default=None):
    if value is None:
        return default
    value = str(value).strip().replace(" ", "")
    if not value:
        return default
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return default


def parse_iso_date(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return None


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS vehicle_fluids (
                vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
                fluid TEXT NOT NULL,
                PRIMARY KEY(vehicle_id, fluid)
            );
            CREATE TABLE IF NOT EXISTS fuelings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
                fueled_on TEXT NOT NULL,
                odometer REAL NOT NULL,
                fluid TEXT NOT NULL,
                liters REAL NOT NULL,
                unit_price REAL NOT NULL,
                total_price REAL NOT NULL,
                fill_type TEXT NOT NULL CHECK(fill_type IN ('first','full','partial')),
                station TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'manual',
                external_key TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fuelings_external
                ON fuelings(vehicle_id, source, external_key)
                WHERE external_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_fuelings_vehicle_date
                ON fuelings(vehicle_id, fueled_on, odometer);
            CREATE TABLE IF NOT EXISTS energy_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                read_on TEXT NOT NULL,
                total_value REAL NOT NULL,
                delta_value REAL,
                unit TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'home_assistant',
                is_valid INTEGER NOT NULL DEFAULT 1,
                invalid_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(metric, read_on)
            );
            CREATE TABLE IF NOT EXISTS energy_tariffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL CHECK(metric IN ('grid_import','gas','water')),
                provider TEXT NOT NULL DEFAULT '',
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                price_per_kwh REAL NOT NULL CHECK(price_per_kwh >= 0),
                kwh_per_unit REAL NOT NULL CHECK(kwh_per_unit > 0),
                base_fee_monthly REAL NOT NULL DEFAULT 0 CHECK(base_fee_monthly >= 0),
                advance_monthly REAL NOT NULL DEFAULT 0 CHECK(advance_monthly >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(valid_to IS NULL OR valid_to >= valid_from)
            );
            CREATE INDEX IF NOT EXISTS idx_energy_tariffs_period
                ON energy_tariffs(metric,valid_from,valid_to);
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL,
                message TEXT NOT NULL
            );
            """
        )
        tariff_columns = {row["name"] for row in db.execute("PRAGMA table_info(energy_tariffs)")}
        if "provider" not in tariff_columns:
            db.execute("ALTER TABLE energy_tariffs ADD COLUMN provider TEXT NOT NULL DEFAULT ''")
        if "base_fee_monthly" not in tariff_columns:
            db.execute("ALTER TABLE energy_tariffs ADD COLUMN base_fee_monthly REAL NOT NULL DEFAULT 0")
        if "advance_monthly" not in tariff_columns:
            db.execute("ALTER TABLE energy_tariffs ADD COLUMN advance_monthly REAL NOT NULL DEFAULT 0")
        upgrade_tariff_table_for_water(db)
        reading_columns = {row["name"] for row in db.execute("PRAGMA table_info(energy_readings)")}
        if "is_valid" not in reading_columns:
            db.execute("ALTER TABLE energy_readings ADD COLUMN is_valid INTEGER NOT NULL DEFAULT 1")
        if "invalid_reason" not in reading_columns:
            db.execute("ALTER TABLE energy_readings ADD COLUMN invalid_reason TEXT NOT NULL DEFAULT ''")
        # Older forms were labelled €/kWh although users naturally entered cents.
        # Values above 5 €/kWh are unambiguously cent values and are corrected once.
        db.execute("""UPDATE energy_tariffs SET price_per_kwh=price_per_kwh/100.0
                      WHERE metric IN ('grid_import','gas') AND price_per_kwh>5.0""")
        for metric in MAX_DAILY_CHANGE:
            sanitize_cumulative_readings(db, metric)
        db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('session_secret',?)", (secrets.token_hex(32),))
        if not db.execute("SELECT 1 FROM vehicles LIMIT 1").fetchone():
            cur = db.execute("INSERT INTO vehicles(name) VALUES(?)", ("VW T6.1",))
            for fluid in ("Diesel", "AdBlue"):
                db.execute("INSERT INTO vehicle_fluids(vehicle_id,fluid) VALUES(?,?)", (cur.lastrowid, fluid))


def get_secret() -> bytes:
    with connect() as db:
        row = db.execute("SELECT value FROM meta WHERE key='session_secret'").fetchone()
    return row["value"].encode()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64u(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_blob(payload) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    body = b64u(raw)
    sig = b64u(hmac.new(get_secret(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def verify_blob(token: str):
    try:
        body, sig = token.split(".", 1)
        expected = b64u(hmac.new(get_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        return json.loads(unb64u(body))
    except Exception:
        return None


def csrf_for(_token=None) -> str:
    return b64u(hmac.new(get_secret(), b"csrf:local-forms", hashlib.sha256).digest())


def period_bounds(period: str):
    today = date.today()
    if period == "year":
        return date(today.year, 1, 1).isoformat(), None
    if period == "all":
        return None, None
    return date(today.year, today.month, 1).isoformat(), None


def upgrade_tariff_table_for_water(db) -> None:
    schema_row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='energy_tariffs'"
    ).fetchone()
    if schema_row and "'water'" in (schema_row["sql"] or ""):
        return
    db.execute("ALTER TABLE energy_tariffs RENAME TO energy_tariffs_before_water")
    db.execute(
        """CREATE TABLE energy_tariffs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               metric TEXT NOT NULL CHECK(metric IN ('grid_import','gas','water')),
               provider TEXT NOT NULL DEFAULT '',
               valid_from TEXT NOT NULL,
               valid_to TEXT,
               price_per_kwh REAL NOT NULL CHECK(price_per_kwh >= 0),
               kwh_per_unit REAL NOT NULL CHECK(kwh_per_unit > 0),
               base_fee_monthly REAL NOT NULL DEFAULT 0 CHECK(base_fee_monthly >= 0),
               advance_monthly REAL NOT NULL DEFAULT 0 CHECK(advance_monthly >= 0),
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               CHECK(valid_to IS NULL OR valid_to >= valid_from)
           )"""
    )
    db.execute(
        """INSERT INTO energy_tariffs(
               id,metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
               base_fee_monthly,advance_monthly,created_at
           )
           SELECT id,metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
                  base_fee_monthly,advance_monthly,created_at
           FROM energy_tariffs_before_water"""
    )
    db.execute("DROP TABLE energy_tariffs_before_water")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_energy_tariffs_period ON energy_tariffs(metric,valid_from,valid_to)"
    )


def sanitize_cumulative_readings(db, metric: str) -> int:
    """Rebuild deltas from accepted meter totals and flag outages/outliers."""
    if metric not in MAX_DAILY_CHANGE:
        return 0
    rows = db.execute(
        "SELECT id,read_on,total_value FROM energy_readings WHERE metric=? ORDER BY read_on,id",
        (metric,),
    ).fetchall()
    previous_total = None
    previous_day = None
    rejected = 0
    for row in rows:
        current_day = date.fromisoformat(row["read_on"])
        try:
            current_total = float(row["total_value"])
        except (TypeError, ValueError):
            current_total = math.nan
        valid = True
        reason = ""
        delta = None
        if not math.isfinite(current_total) or current_total < 0:
            valid = False
            reason = "Ungültiger Zählerstand"
        elif previous_total is None:
            if current_total == 0:
                valid = False
                reason = "Nullwert ohne gültige Basis"
        elif current_total == 0:
            valid = False
            reason = "Nullwert während Sensorausfall"
        elif current_total < previous_total:
            valid = False
            reason = "Zählerstand ist zurückgesprungen"
        else:
            elapsed_days = max(1, (current_day - previous_day).days)
            delta = current_total - previous_total
            if delta > MAX_DAILY_CHANGE[metric] * elapsed_days:
                valid = False
                reason = "Unplausibel großer Zählersprung"
                delta = None
        if valid:
            previous_total = current_total
            previous_day = current_day
        else:
            rejected += 1
        db.execute(
            "UPDATE energy_readings SET delta_value=?,is_valid=?,invalid_reason=? WHERE id=?",
            (delta, 1 if valid else 0, reason, row["id"]),
        )
    return rejected


def prorated_months(start_on: str, end_on: str) -> float:
    """Return inclusive calendar-month fractions for period-based amounts."""
    start = date.fromisoformat(start_on)
    end = date.fromisoformat(end_on)
    if end < start:
        return 0.0
    total = 0.0
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        segment_start = max(start, cursor)
        segment_end = min(end, next_month - timedelta(days=1))
        if segment_start <= segment_end:
            total += ((segment_end - segment_start).days + 1) / (next_month - cursor).days
        cursor = next_month
    return total


def energy_finances(db, start_on=None):
    where = "AND e.read_on>=?" if start_on else ""
    args = (start_on,) if start_on else ()
    rows = db.execute(
        f"""SELECT e.metric,
                   SUM(e.delta_value * t.kwh_per_unit * t.price_per_kwh) AS cost
            FROM energy_readings e
            JOIN energy_tariffs t ON t.metric=e.metric
             AND e.read_on>=t.valid_from
             AND (t.valid_to IS NULL OR e.read_on<=t.valid_to)
            WHERE e.delta_value IS NOT NULL AND e.is_valid=1 {where}
            GROUP BY e.metric""",
        args,
    ).fetchall()
    result = {
        row["metric"]: {
            "variable": row["cost"] or 0.0,
            "base_fee": 0.0,
            "cost": row["cost"] or 0.0,
            "advance": 0.0,
            "balance": -(row["cost"] or 0.0),
        }
        for row in rows
    }
    latest = {
        row["metric"]: row["latest_on"]
        for row in db.execute("SELECT metric,MAX(read_on) latest_on FROM energy_readings WHERE is_valid=1 GROUP BY metric")
    }
    for tariff in db.execute("SELECT * FROM energy_tariffs ORDER BY valid_from,id"):
        metric = tariff["metric"]
        if metric not in result or metric not in latest:
            continue
        period_start = max(value for value in (tariff["valid_from"], start_on) if value)
        period_end = min(value for value in (tariff["valid_to"], latest[metric]) if value)
        months = prorated_months(period_start, period_end)
        result[metric]["base_fee"] += months * tariff["base_fee_monthly"]
        result[metric]["advance"] += months * tariff["advance_monthly"]
    for values in result.values():
        values["cost"] = values["variable"] + values["base_fee"]
        values["balance"] = values["advance"] - values["cost"]
    return result


def energy_costs(db, start_on=None):
    return {metric: values["cost"] for metric, values in energy_finances(db, start_on).items()}


def pv_savings(db, start_on=None):
    """Value PV self-consumption at the applicable grid work price without offsetting costs."""
    where = "AND e.read_on>=?" if start_on else ""
    args = (start_on,) if start_on else ()
    row = db.execute(
        f"""SELECT SUM(e.delta_value * t.price_per_kwh) AS savings
            FROM energy_readings e
            JOIN energy_tariffs t ON t.metric='grid_import'
             AND e.read_on>=t.valid_from
             AND (t.valid_to IS NULL OR e.read_on<=t.valid_to)
            WHERE e.metric='pv_self' AND e.delta_value IS NOT NULL AND e.is_valid=1 {where}""",
        args,
    ).fetchone()
    return row["savings"] if row and row["savings"] is not None else None


def save_manual_water_reading(read_on, total_value):
    read_on = parse_iso_date(read_on)
    total_value = parse_num(total_value)
    if not read_on or read_on > date.today().isoformat() or total_value is None or total_value < 0:
        raise ValueError("Bitte ein gültiges Datum bis heute und einen nichtnegativen Wasserzählerstand eingeben.")
    with connect() as db:
        db.execute(
            """INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id,source,is_valid,invalid_reason)
               VALUES('water',?,?,NULL,'m³','manual','manual',1,'')
               ON CONFLICT(metric,read_on) DO UPDATE SET
                 total_value=excluded.total_value,source='manual',entity_id='manual',
                 unit='m³',is_valid=1,invalid_reason='',created_at=CURRENT_TIMESTAMP""",
            (read_on, total_value),
        )
        rows = db.execute(
            "SELECT id,total_value FROM energy_readings WHERE metric='water' ORDER BY read_on,id"
        ).fetchall()
        previous = None
        for row in rows:
            current = row["total_value"]
            if previous is not None and current < previous:
                raise ValueError("Der Wasserzählerstand darf gegenüber der vorherigen Eingabe nicht sinken.")
            delta = None if previous is None else current - previous
            db.execute("UPDATE energy_readings SET delta_value=? WHERE id=?", (delta, row["id"]))
            previous = current
    return read_on, total_value


def tariff_price_to_eur(metric: str, value):
    """Normalize energy UI prices; water is already entered in euros per m³."""
    price = parse_num(value)
    if price is None:
        return None
    return price / 100 if metric in ("grid_import", "gas") else price


def parse_energy_tariff(form):
    metric = str(form.get("metric", ""))
    provider = str(form.get("provider", "")).strip()
    valid_from = parse_iso_date(form.get("valid_from"))
    valid_to_raw = str(form.get("valid_to", "")).strip()
    valid_to = parse_iso_date(valid_to_raw) if valid_to_raw else None
    price = tariff_price_to_eur(metric, form.get("price_per_kwh"))
    factor = 1.0 if metric in ("grid_import", "water") else parse_num(form.get("kwh_per_unit"))
    base_fee = parse_num(form.get("base_fee_monthly"))
    advance = parse_num(form.get("advance_monthly"))
    if metric not in ("grid_import", "gas", "water") or not provider or len(provider) > 100 or not valid_from or (valid_to_raw and not valid_to):
        raise ValueError("Bitte Anbieter, Messgröße und gültigen Datumsbereich prüfen.")
    if valid_to and valid_to < valid_from:
        raise ValueError("Das Enddatum darf nicht vor dem Startdatum liegen.")
    if price is None or price < 0 or factor is None or factor <= 0 or base_fee is None or base_fee < 0 or advance is None or advance < 0:
        raise ValueError("Arbeitspreis, Umrechnungsfaktor, Grundpreis und Abschlag müssen gültige Zahlen sein.")
    return {
        "metric": metric,
        "provider": provider,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "price": price,
        "factor": factor,
        "base_fee": base_fee,
        "advance": advance,
    }


def vehicle_fluids(db, vehicle_id: int):
    return [r["fluid"] for r in db.execute("SELECT fluid FROM vehicle_fluids WHERE vehicle_id=? ORDER BY fluid", (vehicle_id,))]


def consumption_summary(db, vehicle_id: int, fluid="Diesel"):
    rows = db.execute(
        "SELECT * FROM fuelings WHERE vehicle_id=? AND fluid=? ORDER BY odometer,id",
        (vehicle_id, fluid),
    ).fetchall()
    previous_full = None
    liters_since_full = 0.0
    cycles = []
    row_values = {}
    for row in rows:
        if previous_full is None:
            if row["fill_type"] in ("first", "full"):
                previous_full = row["odometer"]
                liters_since_full = 0.0
            continue
        liters_since_full += row["liters"]
        if row["fill_type"] == "full":
            distance = row["odometer"] - previous_full
            if distance > 0:
                value = liters_since_full / distance * 100
                cycles.append((distance, liters_since_full, value))
                row_values[row["id"]] = value
            previous_full = row["odometer"]
            liters_since_full = 0.0
    total_distance = sum(c[0] for c in cycles)
    total_liters = sum(c[1] for c in cycles)
    average = total_liters / total_distance * 100 if total_distance else None
    return {"average": average, "distance": total_distance, "liters": total_liters, "cycles": cycles, "rows": row_values}


def adblue_summary(db, vehicle_id: int):
    rows = db.execute(
        "SELECT * FROM fuelings WHERE vehicle_id=? AND fluid='AdBlue' ORDER BY odometer,id", (vehicle_id,)
    ).fetchall()
    if len(rows) < 2:
        return {"per_1000": None, "liters": sum(r["liters"] for r in rows), "distance": 0}
    distance = rows[-1]["odometer"] - rows[0]["odometer"]
    liters = sum(r["liters"] for r in rows[1:])
    return {"per_1000": liters / distance * 1000 if distance > 0 else None, "liters": liters, "distance": distance}


def normalize_cost(liters: float, raw_cost: float):
    """Normalize Spritmonitor rows that mix totals, cents and per-unit prices."""
    if liters <= 0 or raw_cost < 0:
        return raw_cost, "ungültig"
    ratio = raw_cost / liters
    if 0.5 <= raw_cost <= 5 and ratio < 0.2:
        return round(raw_cost * liters, 2), "Literpreis als Gesamtpreis erkannt"
    if ratio > 10 and 0.5 <= (raw_cost / 100) / liters <= 5:
        return round(raw_cost / 100, 2), "Centwert durch 100 korrigiert"
    if not 0.5 <= ratio <= 5:
        return raw_cost, "Preis prüfen"
    return raw_cost, ""


def parse_spritmonitor_csv(raw: bytes, fluid: str):
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    required = {"Datum", "Km-Stand", "Spritmenge", "Kosten", "Tankart"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError("Die Datei ist kein unterstützter Spritmonitor-Tankungs-Export.")
    result = []
    for idx, row in enumerate(reader, 2):
        try:
            fueled_on = datetime.strptime(row["Datum"], "%d.%m.%Y").date().isoformat()
            odometer = parse_num(row["Km-Stand"])
            liters = parse_num(row["Spritmenge"])
            raw_cost = parse_num(row["Kosten"])
            if odometer is None or liters is None or liters <= 0:
                raise ValueError
        except ValueError:
            raise ValueError(f"Ungültige Werte in CSV-Zeile {idx}.")
        if raw_cost is None:
            total_price, warning = 0.0, "Preis fehlt"
        else:
            total_price, warning = normalize_cost(liters, raw_cost)
        tank_type = {"1": "full", "2": "partial", "3": "first"}.get(row.get("Tankart", ""), "partial")
        external = "|".join([fueled_on, str(odometer), str(liters), fluid, tank_type])
        result.append(
            {
                "fueled_on": fueled_on,
                "odometer": odometer,
                "liters": liters,
                "total_price": total_price,
                "unit_price": round(total_price / liters, 4),
                "fill_type": tank_type,
                "fluid": fluid,
                "station": row.get("Tankstelle", "").strip(),
                "country": row.get("Land", "").strip(),
                "location": row.get("Ort", "").strip(),
                "note": row.get("Bemerkung", "").strip(),
                "warning": warning,
                "external_key": hashlib.sha256(external.encode()).hexdigest(),
            }
        )
    return sorted(result, key=lambda r: (r["fueled_on"], r["odometer"]))


def ha_ready() -> bool:
    return bool(HA_URL and HA_TOKEN and "BITTE" not in HA_TOKEN.upper())


def _ws_read_exact(reader, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = reader.read(size - len(data))
        if not chunk:
            raise ConnectionError("Home Assistant hat die WebSocket-Verbindung beendet.")
        data.extend(chunk)
    return bytes(data)


def _ws_send_frame(sock, payload: bytes, opcode=1) -> None:
    """Send a masked WebSocket frame, as required for client-to-server traffic."""
    mask = secrets.token_bytes(4)
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _ws_send_json(sock, payload) -> None:
    _ws_send_frame(sock, json.dumps(payload, separators=(",", ":")).encode())


def _ws_receive_json(reader, sock):
    fragments = bytearray()
    message_opcode = None
    while True:
        first, second = _ws_read_exact(reader, 2)
        finished = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _ws_read_exact(reader, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _ws_read_exact(reader, 8))[0]
        mask = _ws_read_exact(reader, 4) if masked else b""
        payload = _ws_read_exact(reader, length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 0x8:
            raise ConnectionError("Home Assistant hat die WebSocket-Verbindung geschlossen.")
        if opcode == 0x9:
            _ws_send_frame(sock, payload, opcode=0xA)
            continue
        if opcode == 0xA:
            continue
        if opcode in (0x1, 0x2):
            fragments = bytearray(payload)
            message_opcode = opcode
        elif opcode == 0x0 and message_opcode is not None:
            fragments.extend(payload)
        else:
            continue
        if finished:
            if message_opcode != 0x1:
                raise ValueError("Home Assistant hat eine unerwartete Binärantwort gesendet.")
            return json.loads(fragments.decode("utf-8"))


def ha_statistics(start_on: str):
    """Read daily long-term statistics through Home Assistant's WebSocket API."""
    if not ha_ready():
        raise RuntimeError("Home Assistant URL oder Token ist noch nicht eingerichtet.")
    entities = [cfg["entity"] for cfg in METRICS.values() if cfg["entity"]]
    if not entities:
        raise RuntimeError("Es sind keine Home-Assistant-Entitäten konfiguriert.")
    parsed = urllib.parse.urlparse(HA_URL)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Die Home-Assistant-URL muss mit http:// oder https:// beginnen.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host_header = parsed.hostname if port in (80, 443) else f"{parsed.hostname}:{port}"
    endpoint = (parsed.path.rstrip("/") + "/api/websocket") or "/api/websocket"
    raw_sock = socket.create_connection((parsed.hostname, port), timeout=30)
    sock = None
    reader = None
    try:
        sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=parsed.hostname) if parsed.scheme == "https" else raw_sock
        sock.settimeout(45)
        reader = sock.makefile("rb")
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            f"GET {endpoint} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        status = reader.readline().decode("latin-1").strip()
        headers = {}
        while True:
            line = reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, value = line.decode("latin-1").split(":", 1)
            headers[name.lower().strip()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if " 101 " not in f" {status} " or headers.get("sec-websocket-accept") != expected:
            raise ConnectionError(f"WebSocket-Verbindung abgelehnt ({status or 'keine Antwort'}).")
        hello = _ws_receive_json(reader, sock)
        if hello.get("type") != "auth_required":
            raise ConnectionError("Unerwartete Home-Assistant-Anmeldung.")
        _ws_send_json(sock, {"type": "auth", "access_token": HA_TOKEN})
        auth = _ws_receive_json(reader, sock)
        if auth.get("type") != "auth_ok":
            raise PermissionError("Home Assistant hat den Token abgelehnt.")
        start_date = datetime.strptime(start_on, "%Y-%m-%d").date()
        start_time = datetime.combine(start_date, datetime.min.time()).astimezone().isoformat()
        end_date = date.today() + timedelta(days=1)
        end_time = datetime.combine(end_date, datetime.min.time()).astimezone().isoformat()
        command_id = 1
        _ws_send_json(
            sock,
            {
                "id": command_id,
                "type": "recorder/statistics_during_period",
                "start_time": start_time,
                "end_time": end_time,
                "statistic_ids": entities,
                "period": "day",
                "types": ["change", "state", "sum"],
            },
        )
        while True:
            response = _ws_receive_json(reader, sock)
            if response.get("id") == command_id and response.get("type") == "result":
                if not response.get("success"):
                    error = response.get("error", {}).get("message", "unbekannter Recorder-Fehler")
                    raise RuntimeError(f"Home-Assistant-Statistik nicht verfügbar: {error}")
                return response.get("result") or {}
    finally:
        if reader is not None:
            reader.close()
        if sock is not None:
            sock.close()
        elif raw_sock is not None:
            raw_sock.close()


def _finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _statistic_day(value) -> str | None:
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().date().isoformat()
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone().date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def import_statistics_rows(result, metrics=None):
    """Store HA recorder rows. Extracted for deterministic tests and safe re-imports."""
    metrics = metrics or METRICS
    counts = {}
    with connect() as db:
        for metric, cfg in metrics.items():
            entity = cfg.get("entity", "")
            rows = result.get(entity, []) if entity else []
            previous_total = None
            count = 0
            for row in sorted(rows, key=lambda item: item.get("start", 0)):
                read_on = _statistic_day(row.get("start"))
                total = _finite_number(row.get("state"))
                if total is None:
                    total = _finite_number(row.get("sum"))
                delta = _finite_number(row.get("change"))
                if delta is None and total is not None and previous_total is not None:
                    candidate = total - previous_total
                    delta = candidate if candidate >= 0 else None
                if not read_on or total is None or read_on > date.today().isoformat():
                    previous_total = total if total is not None else previous_total
                    continue
                if delta is not None and delta < 0:
                    delta = None
                db.execute(
                    """INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id,source,is_valid,invalid_reason)
                       VALUES(?,?,?,?,?,?,?,1,'')
                       ON CONFLICT(metric,read_on) DO UPDATE SET
                         total_value=excluded.total_value,
                         delta_value=COALESCE(excluded.delta_value,energy_readings.delta_value),
                         unit=excluded.unit,entity_id=excluded.entity_id,source=excluded.source,
                         is_valid=1,invalid_reason='',
                         created_at=CURRENT_TIMESTAMP""",
                    (metric, read_on, total, delta, cfg["unit"], entity, "home_assistant_history"),
                )
                previous_total = total
                count += 1
            sanitize_cumulative_readings(db, metric)
            counts[metric] = count
    return counts


def backfill_home_assistant(start_on: str):
    start_on = parse_iso_date(start_on)
    if not start_on or start_on > date.today().isoformat():
        raise ValueError("Bitte ein gültiges Startdatum bis einschließlich heute wählen.")
    try:
        result = ha_statistics(start_on)
        counts = import_statistics_rows(result)
        messages = [f"{cfg['label']}: {counts.get(metric, 0)} Tage" for metric, cfg in METRICS.items() if cfg["entity"]]
        imported = sum(counts.values())
        status = "ok" if imported else "warning"
        message = "Historie ab " + start_on + ": " + ("; ".join(messages) if messages else "keine Sensoren konfiguriert")
        with connect() as db:
            db.execute("INSERT INTO sync_log(status,message) VALUES(?,?)", (status, message))
        return imported, messages
    except Exception as exc:
        with connect() as db:
            db.execute("INSERT INTO sync_log(status,message) VALUES('error',?)", (f"Historischer Import fehlgeschlagen: {exc}",))
        raise


def sync_home_assistant():
    if not ha_ready():
        raise RuntimeError("Home Assistant URL oder Token ist noch nicht eingerichtet.")
    messages = []
    today = date.today().isoformat()
    with connect() as db:
        for metric, cfg in METRICS.items():
            entity = cfg["entity"]
            if not entity:
                continue
            request = urllib.request.Request(
                f"{HA_URL}/api/states/{urllib.parse.quote(entity, safe='._')}",
                headers={"Authorization": f"Bearer {HA_TOKEN}", "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    state = json.load(response)
                value = float(state["state"])
            except (urllib.error.URLError, ValueError, KeyError) as exc:
                messages.append(f"{cfg['label']}: Fehler ({exc})")
                continue
            unit = state.get("attributes", {}).get("unit_of_measurement") or cfg["unit"]
            previous = db.execute(
                "SELECT total_value FROM energy_readings WHERE metric=? AND read_on<? AND is_valid=1 ORDER BY read_on DESC LIMIT 1",
                (metric, today),
            ).fetchone()
            delta = None
            if previous:
                candidate = value - previous["total_value"]
                delta = candidate if candidate >= 0 else None
            db.execute(
                """INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id,is_valid,invalid_reason)
                   VALUES(?,?,?,?,?,?,1,'')
                   ON CONFLICT(metric,read_on) DO UPDATE SET
                     total_value=excluded.total_value,
                     delta_value=excluded.delta_value,
                     unit=excluded.unit,entity_id=excluded.entity_id,
                     is_valid=1,invalid_reason='',created_at=CURRENT_TIMESTAMP""",
                (metric, today, value, delta, unit, entity),
            )
            sanitize_cumulative_readings(db, metric)
            messages.append(f"{cfg['label']}: {fmt_num(value)} {unit}")
        status = "ok" if messages and not any("Fehler" in m for m in messages) else "warning"
        db.execute("INSERT INTO sync_log(status,message) VALUES(?,?)", (status, "; ".join(messages) or "Keine Sensoren konfiguriert"))
    return messages


def sync_scheduler():
    last_attempt = None
    while True:
        now = datetime.now()
        today = now.date().isoformat()
        if ha_ready() and now.hour >= 0 and now.minute >= 10 and last_attempt != today:
            try:
                sync_home_assistant()
            except Exception as exc:
                with connect() as db:
                    db.execute("INSERT INTO sync_log(status,message) VALUES('error',?)", (str(exc),))
            last_attempt = today
        time.sleep(60)


STYLE = r"""
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1f2630;--text:#ecf2f8;--muted:#9aa7b4;--line:#303945;--blue:#3da9fc;--cyan:#35d0ba;--orange:#ffb347;--red:#ff6b6b;--green:#62d38b;--shadow:0 18px 50px rgba(0,0,0,.22)}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#0b1016,#101820 50%,#0c1117);color:var(--text);font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}.layout{display:grid;grid-template-columns:240px 1fr;min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;padding:26px 18px;background:rgba(13,17,23,.94);border-right:1px solid var(--line)}.brand{display:flex;gap:12px;align-items:center;font-weight:800;font-size:21px;margin:0 8px 28px}.logo{width:38px;height:38px;border-radius:13px;display:grid;place-items:center;background:linear-gradient(145deg,var(--blue),var(--cyan));box-shadow:0 8px 25px rgba(53,208,186,.2)}nav{display:grid;gap:6px}nav a{color:var(--muted);padding:10px 12px;border-radius:10px;font-weight:650}nav a:hover,nav a.active{background:var(--panel2);color:white;text-decoration:none}.side-bottom{position:absolute;bottom:22px;left:22px;color:var(--muted);font-size:12px}.main{padding:34px clamp(20px,4vw,56px) 24px;max-width:1500px;width:100%;margin:auto}.topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:26px}.topbar h1{margin:0;font-size:clamp(25px,3vw,36px);letter-spacing:-.03em}.subtitle{color:var(--muted);margin-top:5px}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}.card{background:linear-gradient(155deg,rgba(31,38,48,.96),rgba(22,27,34,.96));border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:var(--shadow)}.metric .icon{font-size:24px}.metric .value{font-size:28px;font-weight:780;margin:12px 0 1px}.metric .label,.muted{color:var(--muted)}.section{margin-top:22px}.section-head{display:flex;justify-content:space-between;align-items:center;margin:0 0 12px}.section-head h2{margin:0;font-size:19px}.two{display:grid;grid-template-columns:1.5fr 1fr;gap:18px}.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:0;border-radius:10px;padding:10px 15px;background:var(--blue);color:#07131d;font-weight:750;cursor:pointer}.btn:hover{text-decoration:none;filter:brightness(1.08)}.btn.secondary{background:var(--panel2);color:var(--text);border:1px solid var(--line)}.btn.danger{background:#4a252b;color:#ffc9cf}.btn.coffee{background:#ffdd00;color:#111}.actions{display:flex;gap:9px;flex-wrap:wrap}.tabs{display:flex;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:4px}.tabs a{padding:7px 12px;border-radius:8px;color:var(--muted)}.tabs a.active{background:var(--blue);color:#07131d;font-weight:750;text-decoration:none}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 10px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}.table-wrap{overflow:auto}.badge{display:inline-block;border-radius:99px;padding:4px 8px;background:#243346;color:#cbe6ff;font-size:12px}.badge.warn{background:#49391f;color:#ffdfa3}.badge.ok{background:#193b2b;color:#baf4cf}.notice{padding:12px 15px;border:1px solid #285272;background:#152b3c;border-radius:12px;margin-bottom:18px}.notice.warn{border-color:#725a28;background:#342a18}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}.field{display:grid;gap:6px}.field.full{grid-column:1/-1}label{color:#c9d4df;font-weight:650}input,select,textarea{width:100%;border:1px solid var(--line);border-radius:10px;background:#0e141b;color:var(--text);padding:11px 12px;font:inherit}input:focus,select:focus,textarea:focus{outline:2px solid rgba(61,169,252,.35);border-color:var(--blue)}textarea{min-height:100px;resize:vertical}.checks{display:flex;gap:12px;flex-wrap:wrap}.checks label{display:flex;gap:6px;align-items:center;background:#111820;padding:8px 10px;border-radius:9px}.checks input{width:auto}.empty{text-align:center;padding:40px;color:var(--muted)}.spark{width:100%;height:80px;margin-top:12px}.spark polyline{fill:none;stroke:var(--cyan);stroke-width:3;stroke-linecap:round;stroke-linejoin:round}.support{text-align:center;padding:44px 20px}.support .coffee-cup{font-size:64px}.credit{font-weight:800;font-size:20px;margin:18px 0}.footer{color:var(--muted);font-size:12px;text-align:center;margin-top:35px}.warning-dot{width:8px;height:8px;border-radius:50%;background:var(--orange);display:inline-block}.good-dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block}@media(max-width:1000px){.grid{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}}@media(max-width:760px){.layout{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);padding:16px}.brand{margin-bottom:12px}nav{display:flex;overflow:auto}.side-bottom{display:none}.main{padding:24px 15px}.grid,.form-grid{grid-template-columns:1fr}.field.full{grid-column:auto}.topbar{align-items:flex-end}th,td{padding:10px 8px}}
"""

STYLE += r"""
.grid{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
details.tariff-history{background:linear-gradient(155deg,rgba(31,38,48,.96),rgba(22,27,34,.96));border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow)}
details.tariff-history summary{cursor:pointer;list-style:none;padding:20px;font-size:19px;font-weight:750;display:flex;justify-content:space-between;gap:12px;align-items:center}
details.tariff-history summary::-webkit-details-marker{display:none}
details.tariff-history summary:after{content:"▾";color:var(--blue);transition:transform .18s ease}
details.tariff-history[open] summary:after{transform:rotate(180deg)}
details.tariff-history .details-body{padding:0 20px 20px}
button:disabled{opacity:.45;cursor:not-allowed;filter:none}
"""


def page(title, body, active="", notice="", warning=False):
    items = [
        ("/", "Dashboard", "⌂"),
        ("/energy", "Energie", "⚡"),
        ("/vehicles", "Fahrzeuge", "🚐"),
        ("/fuelings", "Tankungen", "⛽"),
        ("/import", "Import", "⇩"),
        ("/support", "Unterstützung", "♥"),
    ]
    nav = "".join(f'<a class="{"active" if active == href else ""}" href="{href}"><span>{icon}</span> {label}</a>' for href, label, icon in items)
    notice_html = f'<div class="notice {"warn" if warning else ""}">{esc(notice)}</div>' if notice else ""
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>{esc(title)} · {APP_NAME}</title><style>{STYLE}</style></head><body><div class="layout"><aside class="sidebar"><div class="brand"><span class="logo">EL</span><span>{APP_NAME}</span></div><nav>{nav}</nav><div class="side-bottom">Lokal auf deinem Homelab</div></aside><main class="main">{notice_html}{body}<footer class="footer">by Lrd.Tiberius · EnergieLab {APP_VERSION}</footer></main></div></body></html>"""


def sparkline(db, metric, days=30):
    rows = db.execute(
        "SELECT delta_value FROM energy_readings WHERE metric=? AND delta_value IS NOT NULL AND is_valid=1 ORDER BY read_on DESC LIMIT ?",
        (metric, days),
    ).fetchall()[::-1]
    values = [r["delta_value"] for r in rows]
    if len(values) < 2:
        return '<div class="muted" style="margin-top:14px">Verlauf entsteht nach den täglichen Synchronisierungen.</div>'
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    points = " ".join(f"{i * 100/(len(values)-1):.1f},{72-(v-lo)/span*60:.1f}" for i, v in enumerate(values))
    return f'<svg class="spark" viewBox="0 0 100 80" preserveAspectRatio="none"><polyline points="{points}"/></svg>'


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "EnergieLab"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def cookie_token(self):
        return ""

    def send_bytes(self, data: bytes, content_type, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, text, status=200, headers=None):
        self.send_bytes(text.encode(), "text/html; charset=utf-8", status, headers)

    def redirect(self, target, headers=None):
        all_headers = {"Location": target}
        all_headers.update(headers or {})
        self.send_bytes(b"", "text/plain", 303, all_headers)

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD:
            raise ValueError("Upload ist größer als 5 MB.")
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            raw = self.rfile.read(length)
            envelope = b"Content-Type: " + ctype.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
            message = BytesParser(policy=policy.default).parsebytes(envelope)
            result = {}
            for part in message.iter_parts():
                key = part.get_param("name", header="content-disposition")
                if not key:
                    continue
                payload = part.get_payload(decode=True) or b""
                if part.get_filename():
                    value = payload
                else:
                    value = payload.decode(part.get_content_charset() or "utf-8")
                if key in result:
                    result[key] = result[key] + [value] if isinstance(result[key], list) else [result[key], value]
                else:
                    result[key] = value
            return result
        raw = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def csrf_ok(self, form):
        return hmac.compare_digest(str(form.get("csrf", "")), csrf_for())

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(url.query)
        if path == "/api/health":
            self.send_bytes(json.dumps({"status": "ok", "version": APP_VERSION}).encode(), "application/json")
            return
        if path in ("/login", "/logout"):
            self.redirect("/")
            return
        notice = query.get("notice", [""])[0]
        if path == "/":
            self.dashboard(query.get("period", ["month"])[0], notice)
        elif path == "/energy":
            self.energy_page(notice)
        elif path.startswith("/energy/tariffs/") and path.endswith("/edit"):
            self.energy_tariff_edit_page(int(path.split("/")[3]), notice)
        elif path == "/vehicles":
            self.vehicles_page(notice)
        elif path.startswith("/vehicles/") and path.endswith("/edit"):
            self.vehicle_edit_page(int(path.split("/")[2]), notice)
        elif path == "/fuelings":
            self.fuelings_page(notice)
        elif path == "/fuelings/new":
            self.fueling_form(query)
        elif path.startswith("/fuelings/") and path.endswith("/edit"):
            self.fueling_form(query, int(path.split("/")[2]))
        elif path == "/import":
            self.import_page(notice)
        elif path == "/support":
            self.support_page()
        elif path == "/export/backup.json":
            self.backup_json()
        elif path == "/export/fuelings.csv":
            self.export_fuelings()
        else:
            self.send_html(page("Nicht gefunden", '<div class="card empty">Diese Seite gibt es nicht.</div>'), 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            form = self.read_form()
        except ValueError as exc:
            self.send_html(page("Fehler", f'<div class="notice warn">{esc(exc)}</div>'), 413)
            return
        if not self.csrf_ok(form):
            self.send_html(page("Sicherheitsprüfung", '<div class="notice warn">Die Sitzung ist abgelaufen. Bitte Seite neu laden.</div>'), 403)
            return
        try:
            if path == "/vehicles/save":
                self.save_vehicle(form)
            elif path == "/vehicles/update":
                self.update_vehicle(form)
            elif path == "/fuelings/save":
                self.save_fueling(form)
            elif path.startswith("/fuelings/") and path.endswith("/delete"):
                self.delete_fueling(int(path.split("/")[2]))
            elif path == "/import/preview":
                self.import_preview(form)
            elif path == "/import/commit":
                self.import_commit(form)
            elif path == "/energy/sync":
                messages = sync_home_assistant()
                self.redirect("/energy?notice=" + urllib.parse.quote("; ".join(messages)))
            elif path == "/energy/backfill":
                imported, messages = backfill_home_assistant(str(form.get("start_date", "")))
                notice = f"{imported} tägliche Historienwerte übernommen. " + "; ".join(messages)
                self.redirect("/energy?notice=" + urllib.parse.quote(notice))
            elif path == "/energy/water/save":
                read_on, total = save_manual_water_reading(form.get("read_on"), form.get("total_value"))
                notice = f"Wasserzählerstand {fmt_num(total)} m³ vom {read_on} wurde gespeichert."
                self.redirect("/energy?notice=" + urllib.parse.quote(notice))
            elif path == "/energy/tariffs/save":
                self.save_energy_tariff(form)
            elif path.startswith("/energy/tariffs/") and path.endswith("/update"):
                self.update_energy_tariff(int(path.split("/")[3]), form)
            elif path.startswith("/energy/tariffs/") and path.endswith("/delete"):
                self.delete_energy_tariff(int(path.split("/")[3]))
            else:
                self.send_html(page("Nicht gefunden", '<div class="card empty">Diese Aktion gibt es nicht.</div>'), 404)
        except Exception as exc:
            self.send_html(page("Fehler", f'<div class="notice warn">{esc(exc)}</div><a class="btn secondary" href="javascript:history.back()">Zurück</a>'), 400)

    def dashboard(self, period, notice):
        period = period if period in ("month", "year", "all") else "month"
        start, _ = period_bounds(period)
        where = "WHERE is_valid=1 AND read_on>=?" if start else "WHERE is_valid=1"
        args = (start,) if start else ()
        with connect() as db:
            energy = {r["metric"]: r["value"] for r in db.execute(f"SELECT metric,SUM(delta_value) value FROM energy_readings {where} GROUP BY metric", args)}
            costs = energy_costs(db, start)
            pv_saved = pv_savings(db, start)
            pv_saved_text = f"−{fmt_money(pv_saved)}" if pv_saved is not None else "–"
            fwhere = "WHERE fueled_on>=?" if start else ""
            fuel_args = (start,) if start else ()
            totals = db.execute(f"SELECT SUM(total_price) cost,SUM(liters) liters,MIN(odometer) minodo,MAX(odometer) maxodo FROM fuelings {fwhere}", fuel_args).fetchone()
            vehicles = db.execute("SELECT * FROM vehicles WHERE active=1 ORDER BY name").fetchall()
            diesel_avg = None
            for vehicle in vehicles:
                avg = consumption_summary(db, vehicle["id"])["average"]
                if avg is not None:
                    diesel_avg = avg
                    break
            distance = (totals["maxodo"] - totals["minodo"]) if totals["maxodo"] is not None and totals["minodo"] is not None else 0
            cards = "".join(
                [
                    self.metric_card("⚡", "Strombezug", energy.get("grid_import"), "kWh", f'<div class="muted">Kosten: {fmt_money(costs.get("grid_import"))}</div>' + sparkline(db, "grid_import")),
                    self.metric_card("☀️", "PV-Eigenverbrauch", energy.get("pv_self"), "kWh", f'<div class="muted">Dadurch gespart: {pv_saved_text}</div>' + sparkline(db, "pv_self")),
                    self.metric_card("🔥", "Gas", energy.get("gas"), "m³", f'<div class="muted">Kosten: {fmt_money(costs.get("gas"))}</div>' + sparkline(db, "gas")),
                    self.metric_card("💧", "Wasser", energy.get("water"), "m³", f'<div class="muted">Kosten: {fmt_money(costs.get("water"))}</div>' + sparkline(db, "water")),
                    self.metric_card("🚐", "T6.1 Ø-Verbrauch", diesel_avg, "l/100 km", ""),
                ]
            )
            vehicle_rows = ""
            for vehicle in vehicles:
                summary = consumption_summary(db, vehicle["id"])
                adblue = adblue_summary(db, vehicle["id"])
                vehicle_rows += f"<tr><td><strong>{esc(vehicle['name'])}</strong></td><td>{fmt_num(summary['average'])} l/100 km</td><td>{fmt_num(adblue['per_1000'])} l/1.000 km</td><td>{fmt_num(summary['distance'],0)} km</td></tr>"
        labels = {"month": "Monat", "year": "Jahr", "all": "Gesamt"}
        tabs = "".join(f'<a class="{"active" if period==key else ""}" href="/?period={key}">{label}</a>' for key, label in labels.items())
        body = f"""<div class="topbar"><div><h1>Energie & Verbrauch</h1><div class="subtitle">Deine lokalen Verbrauchsdaten auf einen Blick</div></div><div class="tabs">{tabs}</div></div><div class="grid">{cards}</div><section class="section two"><div class="card"><div class="section-head"><h2>Fahrzeuge</h2><a href="/fuelings/new">Tankung erfassen →</a></div><div class="table-wrap"><table><thead><tr><th>Fahrzeug</th><th>Diesel</th><th>AdBlue</th><th>ausgewertet</th></tr></thead><tbody>{vehicle_rows or '<tr><td colspan="4" class="empty">Noch keine Fahrzeugdaten</td></tr>'}</tbody></table></div></div><div class="card metric"><div class="icon">€</div><div class="label">Tankkosten {labels[period]}</div><div class="value">{fmt_money(totals['cost'])}</div><div class="muted">{fmt_num(totals['liters'])} Liter · ca. {fmt_num(distance,0)} km</div><div class="actions" style="margin-top:20px"><a class="btn" href="/fuelings/new">+ Tankung</a><a class="btn secondary" href="/import">CSV importieren</a></div></div></section>"""
        self.send_html(page("Dashboard", body, "/", notice))

    @staticmethod
    def metric_card(icon, label, value, unit, extra):
        return f'<div class="card metric"><div class="icon">{icon}</div><div class="value">{fmt_num(value)} <small style="font-size:14px">{esc(unit)}</small></div><div class="label">{esc(label)}</div>{extra}</div>'

    def energy_page(self, notice):
        token = self.cookie_token()
        with connect() as db:
            latest = {r["metric"]: r for r in db.execute("SELECT e.* FROM energy_readings e JOIN (SELECT metric,MAX(read_on) d FROM energy_readings WHERE is_valid=1 GROUP BY metric) x ON x.metric=e.metric AND x.d=e.read_on WHERE e.is_valid=1")}
            logs = db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 12").fetchall()
            tariffs = db.execute("SELECT * FROM energy_tariffs ORDER BY valid_from DESC,id DESC").fetchall()
            finances = energy_finances(db)
            pv_saved = pv_savings(db)
            water_readings = db.execute(
                "SELECT * FROM energy_readings WHERE metric='water' AND is_valid=1 ORDER BY read_on DESC,id DESC LIMIT 12"
            ).fetchall()
            invalid_counts = {
                row["metric"]: row["count"]
                for row in db.execute("SELECT metric,COUNT(*) count FROM energy_readings WHERE is_valid=0 GROUP BY metric")
            }
        tariff_counts = {
            "grid_import": sum(1 for tariff in tariffs if tariff["metric"] == "grid_import"),
            "gas": sum(1 for tariff in tariffs if tariff["metric"] == "gas"),
            "water": sum(1 for tariff in tariffs if tariff["metric"] == "water"),
        }
        rows = ""
        for key, cfg in METRICS.items():
            value = latest.get(key)
            values = finances.get(key)
            variable = fmt_money(values["variable"]) if values else "–"
            base_fee = fmt_money(values["base_fee"]) if values else "–"
            cost = fmt_money(values["cost"]) if values else "–"
            advance = fmt_money(values["advance"]) if values else "–"
            if values:
                balance = values["balance"]
                balance_label = "Guthaben" if balance >= 0 else "Nachzahlung"
                balance_class = "ok" if balance >= 0 else "warn"
                balance_html = f'<span class="badge {balance_class}">{fmt_money(abs(balance))} {balance_label}</span>'
            else:
                balance_html = "–"
            entity_label = "manuelle Eingabe" if key == "water" else (cfg["entity"] or "nicht eingerichtet")
            saving_html = f'<strong>−{fmt_money(pv_saved)}</strong><br><span class="badge ok">dadurch gespart</span>' if key == "pv_self" and pv_saved is not None else "–"
            rows += f"<tr><td>{cfg['icon']} <strong>{esc(cfg['label'])}</strong></td><td>{esc(entity_label)}</td><td>{fmt_num(value['total_value'])+' '+esc(value['unit']) if value else '–'}</td><td>{esc(value['read_on']) if value else '–'}</td><td>{variable}</td><td>{base_fee}</td><td><strong>{cost}</strong></td><td>{advance}</td><td>{balance_html if key in ('grid_import', 'gas', 'water') else '–'}</td><td>{saving_html}</td></tr>"
        logrows = "".join(f"<tr><td>{esc(r['synced_at'])}</td><td><span class=\"badge {'ok' if r['status']=='ok' else 'warn'}\">{esc(r['status'])}</span></td><td>{esc(r['message'])}</td></tr>" for r in logs)
        waterrows = "".join(
            f"<tr><td>{esc(r['read_on'])}</td><td>{fmt_num(r['total_value'])} m³</td><td>{fmt_num(r['delta_value']) + ' m³' if r['delta_value'] is not None else 'Erster Stand'}</td></tr>"
            for r in water_readings
        )
        tariffrows = ""
        for tariff in tariffs:
            if tariff["metric"] == "grid_import":
                label, factor = "Strombezug", "1,000 kWh/kWh"
                price = f"{fmt_num(tariff['price_per_kwh'] * 100, 2)} Cent/kWh"
            elif tariff["metric"] == "gas":
                label, factor = "Gas", f"{fmt_num(tariff['kwh_per_unit'], 4)} kWh/m³"
                price = f"{fmt_num(tariff['price_per_kwh'] * 100, 2)} Cent/kWh"
            else:
                label, factor = "Wasser", "–"
                price = f"{fmt_num(tariff['price_per_kwh'], 4)} €/m³"
            tariffrows += f"""<tr><td><strong>{label}</strong></td><td>{esc(tariff['provider'] or '–')}</td><td>{esc(tariff['valid_from'])}</td><td>{esc(tariff['valid_to'] or 'offen')}</td><td>{price}</td><td>{factor}</td><td>{fmt_money(tariff['base_fee_monthly'])}/Monat</td><td>{fmt_money(tariff['advance_monthly'])}/Monat</td><td><div class="actions"><a href="/energy/tariffs/{tariff['id']}/edit">Bearbeiten</a><form method="post" action="/energy/tariffs/{tariff['id']}/delete" onsubmit="return confirm('Tarifzeitraum wirklich löschen?')"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button style="border:0;background:none;color:#ff8e98;cursor:pointer">Löschen</button></form></div></td></tr>"""
        anomaly_items = [f"{METRICS[key]['label']}: {count}" for key, count in invalid_counts.items() if key in METRICS]
        anomaly_html = f'<div class="notice warn">Automatische Plausibilitätsprüfung: {esc(", ".join(anomaly_items))} unplausible Werte werden nicht mitgerechnet.</div>' if anomaly_items else ""
        readiness = '<span class="good-dot"></span> eingerichtet' if ha_ready() else '<span class="warning-dot"></span> Token/URL noch eintragen'
        default_start = date(date.today().year, 1, 1).isoformat()
        csrf = csrf_for(token)
        body = f"""<div class="topbar"><div><h1>Energie</h1><div class="subtitle">Tägliche Zählerstände aus Home Assistant · {readiness}</div></div><form method="post" action="/energy/sync"><input type="hidden" name="csrf" value="{csrf}"><button class="btn">Jetzt synchronisieren</button></form></div>{anomaly_html}
        <div class="card"><div class="table-wrap"><table><thead><tr><th>Messgröße</th><th>Datenquelle</th><th>Letzter Stand</th><th>Datum</th><th>Verbrauchskosten</th><th>Grundpreis</th><th>Gesamtkosten</th><th>Abschläge</th><th>Prognose</th><th>PV-Ersparnis</th></tr></thead><tbody>{rows}</tbody></table></div><p class="muted" style="padding:0 28px 22px">Verbrauchskosten + Grundpreis = Gesamtkosten. Die PV-Ersparnis wird separat mit dem gültigen Strom-Arbeitspreis berechnet und reduziert diese Kosten nicht.</p></div>
        <section class="section card"><div class="section-head"><h2>Wasserzähler manuell erfassen</h2><span class="badge">nur manuell</span></div><div class="two"><form method="post" action="/energy/water/save"><input type="hidden" name="csrf" value="{csrf}"><div class="form-grid"><div class="field"><label>Ablesedatum</label><input type="date" name="read_on" value="{date.today().isoformat()}" max="{date.today().isoformat()}" required></div><div class="field"><label>Zählerstand in m³</label><input inputmode="decimal" name="total_value" placeholder="z. B. 123,456" required></div></div><p class="muted">Eine erneute Eingabe für dasselbe Datum korrigiert den vorhandenen Stand. Die Verbräuche zwischen allen Ablesungen werden danach neu berechnet.</p><button class="btn" style="margin-top:8px">Wasserstand speichern</button></form><div class="table-wrap"><table><thead><tr><th>Datum</th><th>Zählerstand</th><th>Verbrauch seit davor</th></tr></thead><tbody>{waterrows or '<tr><td colspan="3" class="empty">Noch keine Wasserstände erfasst</td></tr>'}</tbody></table></div></div></section>
        <section class="section two"><div class="card"><div class="section-head"><h2>Historie einmalig importieren</h2></div><p class="muted">Übernimmt vorhandene tägliche Langzeitstatistiken aus Home Assistant. Ein erneuter Lauf aktualisiert dieselben Tage und erzeugt keine Duplikate.</p><form method="post" action="/energy/backfill"><input type="hidden" name="csrf" value="{csrf}"><div class="field"><label>Historie ab</label><input type="date" name="start_date" value="{default_start}" max="{date.today().isoformat()}" required></div><button class="btn" style="margin-top:18px">Historie importieren</button></form></div>
        <div class="card"><div class="section-head"><h2>Stromtarif hinzufügen</h2><span class="badge">{tariff_counts['grid_import']} von {TARIFF_LIMIT}</span></div><form method="post" action="/energy/tariffs/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="metric" value="grid_import"><div class="form-grid"><div class="field full"><label>Anbieter</label><input name="provider" maxlength="100" placeholder="z. B. Stadtwerke Musterstadt" required></div><div class="field"><label>Gültig von</label><input type="date" name="valid_from" required></div><div class="field"><label>Gültig bis</label><input type="date" name="valid_to"></div><div class="field"><label>Strompreis in Cent/kWh</label><input inputmode="decimal" name="price_per_kwh" placeholder="z. B. 32,90" required></div><div class="field"><label>Grundpreis in €/Monat</label><input inputmode="decimal" name="base_fee_monthly" placeholder="z. B. 12,50" required></div><div class="field full"><label>Abschlag in €/Monat</label><input inputmode="decimal" name="advance_monthly" placeholder="z. B. 95,00" required></div></div><button class="btn" style="margin-top:18px" {'disabled' if tariff_counts['grid_import'] >= TARIFF_LIMIT else ''}>Stromtarif speichern</button>{'<p class="muted">Das Limit von fünf Stromtarifen ist erreicht. Lösche bei Bedarf einen alten Zeitraum.</p>' if tariff_counts['grid_import'] >= TARIFF_LIMIT else ''}</form></div></section>
        <section class="section card"><div class="section-head"><h2>Gastarif hinzufügen</h2><span class="badge">{tariff_counts['gas']} von {TARIFF_LIMIT}</span></div><form method="post" action="/energy/tariffs/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="metric" value="gas"><div class="form-grid"><div class="field full"><label>Anbieter</label><input name="provider" maxlength="100" placeholder="z. B. Stadtwerke Musterstadt" required></div><div class="field"><label>Gültig von</label><input type="date" name="valid_from" required></div><div class="field"><label>Gültig bis</label><input type="date" name="valid_to"></div><div class="field"><label>Gaspreis in Cent/kWh</label><input inputmode="decimal" name="price_per_kwh" placeholder="z. B. 10,90" required></div><div class="field"><label>Umrechnung kWh pro m³</label><input inputmode="decimal" name="kwh_per_unit" placeholder="laut Gasabrechnung, z. B. 10,42" required></div><div class="field"><label>Grundpreis in €/Monat</label><input inputmode="decimal" name="base_fee_monthly" placeholder="z. B. 14,00" required></div><div class="field"><label>Abschlag in €/Monat</label><input inputmode="decimal" name="advance_monthly" placeholder="z. B. 120,00" required></div></div><p class="muted">Der Gaszähler liefert m³. Den periodenbezogenen Umrechnungsfaktor findest du auf der Gasabrechnung.</p><button class="btn" style="margin-top:8px" {'disabled' if tariff_counts['gas'] >= TARIFF_LIMIT else ''}>Gastarif speichern</button>{'<p class="muted">Das Limit von fünf Gastarifen ist erreicht. Lösche bei Bedarf einen alten Zeitraum.</p>' if tariff_counts['gas'] >= TARIFF_LIMIT else ''}</form></section>
        <section class="section card"><div class="section-head"><h2>Wassertarif hinzufügen</h2><span class="badge">{tariff_counts['water']} von {TARIFF_LIMIT}</span></div><form method="post" action="/energy/tariffs/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="metric" value="water"><div class="form-grid"><div class="field full"><label>Anbieter</label><input name="provider" maxlength="100" placeholder="z. B. Wasserverband Musterstadt" required></div><div class="field"><label>Gültig von</label><input type="date" name="valid_from" required></div><div class="field"><label>Gültig bis</label><input type="date" name="valid_to"></div><div class="field"><label>Verbrauchspreis in €/m³</label><input inputmode="decimal" name="price_per_kwh" placeholder="z. B. 4,2500" required></div><div class="field"><label>Grundpreis in €/Monat</label><input inputmode="decimal" name="base_fee_monthly" placeholder="z. B. 8,50" required></div><div class="field full"><label>Abschlag in €/Monat</label><input inputmode="decimal" name="advance_monthly" placeholder="z. B. 45,00" required></div></div><p class="muted">Für vollständige variable Kosten kannst du Trinkwasser und Abwasser im Verbrauchspreis zusammenfassen.</p><button class="btn" style="margin-top:8px" {'disabled' if tariff_counts['water'] >= TARIFF_LIMIT else ''}>Wassertarif speichern</button>{'<p class="muted">Das Limit von fünf Wassertarifen ist erreicht. Lösche bei Bedarf einen alten Zeitraum.</p>' if tariff_counts['water'] >= TARIFF_LIMIT else ''}</form></section>
        <details class="section tariff-history"><summary><span>Gespeicherte Tarife anzeigen</span><span class="badge">Strom {tariff_counts['grid_import']}/{TARIFF_LIMIT} · Gas {tariff_counts['gas']}/{TARIFF_LIMIT} · Wasser {tariff_counts['water']}/{TARIFF_LIMIT}</span></summary><div class="details-body"><div class="table-wrap"><table><thead><tr><th>Messgröße</th><th>Anbieter</th><th>Von</th><th>Bis</th><th>Preis</th><th>Umrechnung</th><th>Grundpreis</th><th>Abschlag</th><th></th></tr></thead><tbody>{tariffrows or '<tr><td colspan="9" class="empty">Noch keine Tarife hinterlegt</td></tr>'}</tbody></table></div></div></details>
        <section class="section card"><div class="section-head"><h2>Importprotokoll</h2></div><div class="table-wrap"><table><thead><tr><th>Zeit</th><th>Status</th><th>Meldung</th></tr></thead><tbody>{logrows or '<tr><td colspan="3" class="empty">Noch keine Synchronisierung</td></tr>'}</tbody></table></div></section>"""
        self.send_html(page("Energie", body, "/energy", notice, "Fehler" in notice))

    def save_energy_tariff(self, form):
        values = parse_energy_tariff(form)
        end_bound = values["valid_to"] or "9999-12-31"
        with connect() as db:
            count = db.execute("SELECT COUNT(*) c FROM energy_tariffs WHERE metric=?", (values["metric"],)).fetchone()["c"]
            if count >= TARIFF_LIMIT:
                label = {"grid_import": "Stromtarife", "gas": "Gastarife", "water": "Wassertarife"}[values["metric"]]
                raise ValueError(f"Es können maximal {TARIFF_LIMIT} {label} gespeichert werden.")
            overlap = db.execute(
                """SELECT 1 FROM energy_tariffs
                   WHERE metric=? AND valid_from<=? AND COALESCE(valid_to,'9999-12-31')>=?
                   LIMIT 1""",
                (values["metric"], end_bound, values["valid_from"]),
            ).fetchone()
            if overlap:
                raise ValueError("Für diesen Zeitraum besteht bereits ein Tarif. Bitte Zeiträume lückenlos, aber ohne Überschneidung anlegen.")
            db.execute(
                "INSERT INTO energy_tariffs(metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,base_fee_monthly,advance_monthly) VALUES(?,?,?,?,?,?,?,?)",
                (values["metric"], values["provider"], values["valid_from"], values["valid_to"], values["price"], values["factor"], values["base_fee"], values["advance"]),
            )
        label = {"grid_import": "Stromtarif", "gas": "Gastarif", "water": "Wassertarif"}[values["metric"]]
        self.redirect("/energy?notice=" + urllib.parse.quote(f"{label} wurde gespeichert."))

    def energy_tariff_edit_page(self, tariff_id, notice):
        token = self.cookie_token()
        with connect() as db:
            tariff = db.execute("SELECT * FROM energy_tariffs WHERE id=?", (tariff_id,)).fetchone()
        if not tariff:
            self.send_html(page("Nicht gefunden", '<div class="card empty">Tarif nicht gefunden.</div>'), 404)
            return
        label = {"grid_import": "Stromtarif", "gas": "Gastarif", "water": "Wassertarif"}[tariff["metric"]]
        factor_field = f'<div class="field"><label>Umrechnung kWh pro m³</label><input inputmode="decimal" name="kwh_per_unit" value="{fmt_num(tariff["kwh_per_unit"], 4)}" required></div>' if tariff["metric"] == "gas" else ""
        if tariff["metric"] == "water":
            price_label = "Verbrauchspreis in €/m³"
            price_value = fmt_num(tariff["price_per_kwh"], 4)
        else:
            price_label = "Arbeitspreis in Cent/kWh"
            price_value = fmt_num(tariff["price_per_kwh"] * 100, 2)
        body = f"""<div class="topbar"><div><h1>{label} bearbeiten</h1><div class="subtitle">Vertragsdaten, Grundpreis und Abschlag ergänzen oder ändern</div></div></div><div class="card"><form method="post" action="/energy/tariffs/{tariff_id}/update"><input type="hidden" name="csrf" value="{csrf_for(token)}"><input type="hidden" name="metric" value="{esc(tariff['metric'])}"><div class="form-grid"><div class="field full"><label>Anbieter</label><input name="provider" maxlength="100" value="{esc(tariff['provider'])}" required></div><div class="field"><label>Gültig von</label><input type="date" name="valid_from" value="{esc(tariff['valid_from'])}" required></div><div class="field"><label>Gültig bis</label><input type="date" name="valid_to" value="{esc(tariff['valid_to'] or '')}"></div><div class="field"><label>{price_label}</label><input inputmode="decimal" name="price_per_kwh" value="{price_value}" required></div>{factor_field}<div class="field"><label>Grundpreis in €/Monat</label><input inputmode="decimal" name="base_fee_monthly" value="{fmt_num(tariff['base_fee_monthly'], 2)}" required></div><div class="field"><label>Abschlag in €/Monat</label><input inputmode="decimal" name="advance_monthly" value="{fmt_num(tariff['advance_monthly'], 2)}" required></div></div><div class="actions" style="margin-top:18px"><button class="btn">Änderungen speichern</button><a class="btn secondary" href="/energy">Abbrechen</a></div></form></div>"""
        self.send_html(page(f"{label} bearbeiten", body, "/energy", notice))

    def update_energy_tariff(self, tariff_id, form):
        values = parse_energy_tariff(form)
        end_bound = values["valid_to"] or "9999-12-31"
        with connect() as db:
            existing = db.execute("SELECT 1 FROM energy_tariffs WHERE id=?", (tariff_id,)).fetchone()
            if not existing:
                raise ValueError("Tarif nicht gefunden.")
            overlap = db.execute(
                """SELECT 1 FROM energy_tariffs
                   WHERE id<>? AND metric=? AND valid_from<=?
                     AND COALESCE(valid_to,'9999-12-31')>=?
                   LIMIT 1""",
                (tariff_id, values["metric"], end_bound, values["valid_from"]),
            ).fetchone()
            if overlap:
                raise ValueError("Für diesen Zeitraum besteht bereits ein anderer Tarif.")
            db.execute(
                """UPDATE energy_tariffs
                   SET provider=?,valid_from=?,valid_to=?,price_per_kwh=?,kwh_per_unit=?,
                       base_fee_monthly=?,advance_monthly=?
                   WHERE id=?""",
                (values["provider"], values["valid_from"], values["valid_to"], values["price"], values["factor"], values["base_fee"], values["advance"], tariff_id),
            )
        self.redirect("/energy?notice=" + urllib.parse.quote("Tarif wurde aktualisiert."))

    def delete_energy_tariff(self, tariff_id):
        with connect() as db:
            db.execute("DELETE FROM energy_tariffs WHERE id=?", (tariff_id,))
        self.redirect("/energy?notice=" + urllib.parse.quote("Tarifzeitraum wurde gelöscht."))

    def vehicles_page(self, notice):
        token = self.cookie_token()
        with connect() as db:
            vehicles = db.execute("SELECT * FROM vehicles ORDER BY active DESC,name").fetchall()
            cards = ""
            for vehicle in vehicles:
                fluids = vehicle_fluids(db, vehicle["id"])
                count = db.execute("SELECT COUNT(*) c FROM fuelings WHERE vehicle_id=?", (vehicle["id"],)).fetchone()["c"]
                cards += f'<div class="card"><div class="section-head"><h2>{esc(vehicle["name"])}</h2><span class="badge">{count} Tankungen</span></div><div class="muted">Erlaubt: {esc(", ".join(fluids))}</div><a href="/vehicles/{vehicle["id"]}/edit" style="display:inline-block;margin-top:14px">Bearbeiten →</a></div>'
        checks = "".join(f'<label><input type="checkbox" name="fluid_{i}" value="{esc(fluid)}" {"checked" if fluid in ("Diesel","AdBlue") else ""}> {esc(fluid)}</label>' for i, fluid in enumerate(FLUIDS))
        body = f"""<div class="topbar"><div><h1>Fahrzeuge</h1><div class="subtitle">Betriebsstoffe werden je Fahrzeug festgelegt</div></div></div><div class="grid" style="grid-template-columns:repeat(3,minmax(0,1fr))">{cards}</div><section class="section card"><div class="section-head"><h2>Weiteres Fahrzeug anlegen</h2></div><form method="post" action="/vehicles/save"><input type="hidden" name="csrf" value="{csrf_for(token)}"><div class="form-grid"><div class="field full"><label>Fahrzeugname</label><input name="name" placeholder="z. B. Fahrzeug 2" required maxlength="80"></div><div class="field full"><label>Zulässige Betriebsstoffe</label><div class="checks">{checks}</div></div></div><button class="btn" style="margin-top:18px">Fahrzeug speichern</button></form></section>"""
        self.send_html(page("Fahrzeuge", body, "/vehicles", notice))

    def save_vehicle(self, form):
        name = str(form.get("name", "")).strip()
        fluids = [str(form.get(f"fluid_{i}", "")) for i in range(len(FLUIDS))]
        fluids = [f for f in fluids if f in FLUIDS]
        if not name or not fluids:
            raise ValueError("Name und mindestens ein Betriebsstoff sind erforderlich.")
        with connect() as db:
            cur = db.execute("INSERT INTO vehicles(name) VALUES(?)", (name,))
            db.executemany("INSERT INTO vehicle_fluids(vehicle_id,fluid) VALUES(?,?)", [(cur.lastrowid, f) for f in fluids])
        self.redirect("/vehicles?notice=" + urllib.parse.quote("Fahrzeug wurde angelegt."))

    def vehicle_edit_page(self, vehicle_id, notice):
        token = self.cookie_token()
        with connect() as db:
            vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if not vehicle:
                self.send_html(page("Nicht gefunden", '<div class="card empty">Fahrzeug nicht gefunden.</div>'), 404)
                return
            allowed = set(vehicle_fluids(db, vehicle_id))
        checks = "".join(f'<label><input type="checkbox" name="fluid_{i}" value="{esc(fluid)}" {"checked" if fluid in allowed else ""}> {esc(fluid)}</label>' for i, fluid in enumerate(FLUIDS))
        body = f"""<div class="topbar"><div><h1>Fahrzeug bearbeiten</h1><div class="subtitle">Name und zulässige Betriebsstoffe</div></div></div><div class="card"><form method="post" action="/vehicles/update"><input type="hidden" name="csrf" value="{csrf_for(token)}"><input type="hidden" name="vehicle_id" value="{vehicle_id}"><div class="form-grid"><div class="field full"><label>Fahrzeugname</label><input name="name" value="{esc(vehicle['name'])}" required maxlength="80"></div><div class="field full"><label>Zulässige Betriebsstoffe</label><div class="checks">{checks}</div></div></div><div class="actions" style="margin-top:18px"><button class="btn">Änderungen speichern</button><a class="btn secondary" href="/vehicles">Abbrechen</a></div></form></div>"""
        self.send_html(page("Fahrzeug bearbeiten", body, "/vehicles", notice))

    def update_vehicle(self, form):
        vehicle_id = int(form.get("vehicle_id", 0))
        name = str(form.get("name", "")).strip()
        fluids = [str(form.get(f"fluid_{i}", "")) for i in range(len(FLUIDS))]
        fluids = [f for f in fluids if f in FLUIDS]
        if not name or not fluids:
            raise ValueError("Name und mindestens ein Betriebsstoff sind erforderlich.")
        with connect() as db:
            used = {r["fluid"] for r in db.execute("SELECT DISTINCT fluid FROM fuelings WHERE vehicle_id=?", (vehicle_id,))}
            missing = used - set(fluids)
            if missing:
                raise ValueError("Bereits verwendete Betriebsstoffe können nicht entfernt werden: " + ", ".join(sorted(missing)))
            db.execute("UPDATE vehicles SET name=? WHERE id=?", (name, vehicle_id))
            db.execute("DELETE FROM vehicle_fluids WHERE vehicle_id=?", (vehicle_id,))
            db.executemany("INSERT INTO vehicle_fluids(vehicle_id,fluid) VALUES(?,?)", [(vehicle_id, fluid) for fluid in fluids])
        self.redirect("/vehicles?notice=" + urllib.parse.quote("Fahrzeug wurde aktualisiert."))

    def fuelings_page(self, notice):
        token = self.cookie_token()
        with connect() as db:
            rows = db.execute("SELECT f.*,v.name vehicle_name FROM fuelings f JOIN vehicles v ON v.id=f.vehicle_id ORDER BY fueled_on DESC,odometer DESC,id DESC").fetchall()
            values = {}
            for vehicle_id in {r["vehicle_id"] for r in rows}:
                values.update(consumption_summary(db, vehicle_id)["rows"])
        table = ""
        for row in rows:
            consumption = f"{fmt_num(values[row['id']])} l/100 km" if row["id"] in values else "–"
            table += f"""<tr><td>{esc(datetime.strptime(row['fueled_on'],'%Y-%m-%d').strftime('%d.%m.%Y'))}</td><td><strong>{esc(row['vehicle_name'])}</strong><br><span class="badge">{esc(row['fluid'])}</span></td><td>{fmt_num(row['odometer'],0)} km</td><td>{fmt_num(row['liters'])} l</td><td>{fmt_num(row['unit_price'],3)} €/l</td><td>{fmt_money(row['total_price'])}</td><td>{esc(FILL_TYPES[row['fill_type']])}</td><td>{consumption}</td><td><div class="actions"><a href="/fuelings/{row['id']}/edit">Bearbeiten</a><form method="post" action="/fuelings/{row['id']}/delete" onsubmit="return confirm('Tankung wirklich löschen?')"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button style="border:0;background:none;color:#ff8e98;cursor:pointer">Löschen</button></form></div></td></tr>"""
        body = f"""<div class="topbar"><div><h1>Tankungen</h1><div class="subtitle">Voll- und Teiltankungen mit automatischer Verbrauchsberechnung</div></div><a class="btn" href="/fuelings/new">+ Tankung erfassen</a></div><div class="card table-wrap"><table><thead><tr><th>Datum</th><th>Fahrzeug</th><th>Km-Stand</th><th>Menge</th><th>Preis</th><th>Gesamt</th><th>Art</th><th>Verbrauch</th><th></th></tr></thead><tbody>{table or '<tr><td colspan="9" class="empty">Noch keine Tankungen. Importiere deine Spritmonitor-CSV.</td></tr>'}</tbody></table></div>"""
        self.send_html(page("Tankungen", body, "/fuelings", notice))

    def fueling_form(self, query, fueling_id=None):
        token = self.cookie_token()
        with connect() as db:
            vehicles = db.execute("SELECT * FROM vehicles WHERE active=1 ORDER BY name").fetchall()
            fluid_map = {v["id"]: vehicle_fluids(db, v["id"]) for v in vehicles}
            row = db.execute("SELECT * FROM fuelings WHERE id=?", (fueling_id,)).fetchone() if fueling_id else None
            default_vehicle = row["vehicle_id"] if row else (vehicles[0]["id"] if vehicles else 0)
            default_fluid = row["fluid"] if row else (fluid_map.get(default_vehicle, ["Diesel"])[0])
            last_full = db.execute("SELECT odometer FROM fuelings WHERE vehicle_id=? AND fluid=? AND fill_type IN ('first','full') ORDER BY odometer DESC LIMIT 1", (default_vehicle, default_fluid)).fetchone()
        vehicle_options = "".join(f'<option value="{v["id"]}" {"selected" if v["id"]==default_vehicle else ""}>{esc(v["name"])}</option>' for v in vehicles)
        fluid_options = "".join(f'<option value="{esc(f)}" {"selected" if f==default_fluid else ""}>{esc(f)}</option>' for f in fluid_map.get(default_vehicle, []))
        fill_options = "".join(f'<option value="{key}" {"selected" if (row and row["fill_type"]==key) or (not row and key=="full") else ""}>{label}</option>' for key, label in FILL_TYPES.items())
        values = {"date": row["fueled_on"] if row else date.today().isoformat(), "odometer": row["odometer"] if row else "", "liters": row["liters"] if row else "", "unit_price": row["unit_price"] if row else "", "station": row["station"] if row else "", "location": row["location"] if row else "", "note": row["note"] if row else ""}
        fluid_json = json.dumps(fluid_map, ensure_ascii=False)
        last_hint = f"Letzte Volltankung: {fmt_num(last_full['odometer'],0)} km" if last_full else "Noch keine vorherige Volltankung"
        body = f"""<div class="topbar"><div><h1>{'Tankung bearbeiten' if row else 'Tankung erfassen'}</h1><div class="subtitle">{last_hint}</div></div></div><div class="card"><form method="post" action="/fuelings/save"><input type="hidden" name="csrf" value="{csrf_for(token)}"><input type="hidden" name="id" value="{row['id'] if row else ''}"><div class="form-grid"><div class="field"><label>Fahrzeug</label><select id="vehicle" name="vehicle_id">{vehicle_options}</select></div><div class="field"><label>Betriebsstoff</label><select id="fluid" name="fluid">{fluid_options}</select></div><div class="field"><label>Datum</label><input type="date" name="fueled_on" value="{esc(values['date'])}" required></div><div class="field"><label>Art</label><select name="fill_type">{fill_options}</select></div><div class="field"><label>Kilometerstand</label><input inputmode="decimal" name="odometer" value="{esc(values['odometer'])}" required></div><div class="field"><label>Tankmenge in Liter</label><input id="liters" inputmode="decimal" name="liters" value="{esc(values['liters'])}" required></div><div class="field"><label>Preis pro Liter</label><input id="unit_price" inputmode="decimal" name="unit_price" value="{esc(values['unit_price'])}" required></div><div class="field"><label>Gesamtpreis</label><input id="total" readonly value="{fmt_num((row['total_price'] if row else None))}"></div><div class="field"><label>Tankstelle</label><input name="station" value="{esc(values['station'])}"></div><div class="field"><label>Ort</label><input name="location" value="{esc(values['location'])}"></div><div class="field full"><label>Bemerkung</label><textarea name="note">{esc(values['note'])}</textarea></div></div><div class="actions" style="margin-top:18px"><button class="btn">Speichern</button><a class="btn secondary" href="/fuelings">Abbrechen</a></div></form></div><script>const fluids={fluid_json};const v=document.getElementById('vehicle'),f=document.getElementById('fluid');v.addEventListener('change',()=>{{f.innerHTML=(fluids[v.value]||[]).map(x=>`<option>${{x}}</option>`).join('')}});function n(x){{return parseFloat((x.value||'').replace(',','.'))||0}}function total(){{document.getElementById('total').value=(n(document.getElementById('liters'))*n(document.getElementById('unit_price'))).toFixed(2).replace('.',',')}}document.getElementById('liters').addEventListener('input',total);document.getElementById('unit_price').addEventListener('input',total);total();</script>"""
        self.send_html(page("Tankung", body, "/fuelings"))

    def save_fueling(self, form):
        vehicle_id = int(form.get("vehicle_id", 0))
        fueling_id = int(form["id"]) if str(form.get("id", "")).strip() else None
        fueled_on = parse_iso_date(form.get("fueled_on"))
        odometer = parse_num(form.get("odometer"))
        liters = parse_num(form.get("liters"))
        unit_price = parse_num(form.get("unit_price"))
        fluid = str(form.get("fluid", ""))
        fill_type = str(form.get("fill_type", ""))
        with connect() as db:
            allowed = vehicle_fluids(db, vehicle_id)
            if not fueled_on or odometer is None or odometer < 0 or liters is None or liters <= 0 or unit_price is None or unit_price < 0 or fluid not in allowed or fill_type not in FILL_TYPES:
                raise ValueError("Bitte prüfe Datum, Kilometerstand, Menge, Preis und Betriebsstoff.")
            values = (vehicle_id, fueled_on, odometer, fluid, liters, unit_price, round(liters * unit_price, 2), fill_type, str(form.get("station", "")).strip(), str(form.get("location", "")).strip(), str(form.get("note", "")).strip())
            if fueling_id:
                db.execute("UPDATE fuelings SET vehicle_id=?,fueled_on=?,odometer=?,fluid=?,liters=?,unit_price=?,total_price=?,fill_type=?,station=?,location=?,note=?,source='manual',external_key=NULL WHERE id=?", values + (fueling_id,))
            else:
                db.execute("INSERT INTO fuelings(vehicle_id,fueled_on,odometer,fluid,liters,unit_price,total_price,fill_type,station,location,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
        self.redirect("/fuelings?notice=" + urllib.parse.quote("Tankung wurde gespeichert."))

    def delete_fueling(self, fueling_id):
        with connect() as db:
            db.execute("DELETE FROM fuelings WHERE id=?", (fueling_id,))
        self.redirect("/fuelings?notice=" + urllib.parse.quote("Tankung wurde gelöscht."))

    def import_page(self, notice):
        token = self.cookie_token()
        with connect() as db:
            vehicles = db.execute("SELECT * FROM vehicles WHERE active=1 ORDER BY name").fetchall()
            options = ""
            first_fluids = []
            fluid_map = {}
            for vehicle in vehicles:
                fluids = vehicle_fluids(db, vehicle["id"])
                fluid_map[vehicle["id"]] = fluids
                options += f'<option value="{vehicle["id"]}">{esc(vehicle["name"])}</option>'
                if not first_fluids:
                    first_fluids = fluids
        fluid_options = "".join(f'<option>{esc(f)}</option>' for f in first_fluids)
        body = f"""<div class="topbar"><div><h1>Spritmonitor importieren</h1><div class="subtitle">Die Datei wird zuerst geprüft und noch nicht sofort gespeichert</div></div></div><div class="card"><form method="post" action="/import/preview" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{csrf_for(token)}"><div class="form-grid"><div class="field"><label>Fahrzeug</label><select id="vehicle" name="vehicle_id">{options}</select></div><div class="field"><label>Betriebsstoff der CSV</label><select id="fluid" name="fluid">{fluid_options}</select></div><div class="field full"><label>Spritmonitor-CSV</label><input type="file" name="csv_file" accept=".csv,text/csv" required></div></div><button class="btn" style="margin-top:18px">Datei prüfen</button></form></div><section class="section card"><h2>Was wird erkannt?</h2><p class="muted">Datum, Kilometerstand, Menge, Voll-/Teiltankung, Tankstelle und Kosten. Unplausible Cent- oder Literpreiswerte werden in der Vorschau gekennzeichnet und normalisiert. Bereits importierte Zeilen werden nicht doppelt angelegt.</p></section><script>const fluids={json.dumps(fluid_map,ensure_ascii=False)};const v=document.getElementById('vehicle'),f=document.getElementById('fluid');v.addEventListener('change',()=>{{f.innerHTML=(fluids[v.value]||[]).map(x=>`<option>${{x}}</option>`).join('')}});</script>"""
        self.send_html(page("Import", body, "/import", notice))

    def import_preview(self, form):
        vehicle_id = int(form.get("vehicle_id", 0))
        fluid = str(form.get("fluid", ""))
        raw = form.get("csv_file")
        if not isinstance(raw, bytes) or not raw:
            raise ValueError("Bitte eine CSV-Datei auswählen.")
        with connect() as db:
            if fluid not in vehicle_fluids(db, vehicle_id):
                raise ValueError("Der Betriebsstoff ist für dieses Fahrzeug nicht freigegeben.")
            vehicle = db.execute("SELECT name FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        rows = parse_spritmonitor_csv(raw, fluid)
        payload = sign_blob({"vehicle_id": vehicle_id, "fluid": fluid, "rows": rows, "exp": int(time.time()) + 1800})
        warnings = sum(bool(r["warning"]) for r in rows)
        table = "".join(f"<tr><td>{esc(datetime.strptime(r['fueled_on'],'%Y-%m-%d').strftime('%d.%m.%Y'))}</td><td>{fmt_num(r['odometer'],0)} km</td><td>{fmt_num(r['liters'])} l</td><td>{fmt_num(r['unit_price'],3)} €/l</td><td>{fmt_money(r['total_price'])}</td><td>{esc(FILL_TYPES[r['fill_type']])}</td><td>{f'<span class=\"badge warn\">{esc(r["warning"])}</span>' if r['warning'] else '<span class="badge ok">OK</span>'}</td></tr>" for r in rows)
        token = self.cookie_token()
        body = f"""<div class="topbar"><div><h1>Importvorschau</h1><div class="subtitle">{len(rows)} Tankungen für {esc(vehicle['name'])} · {warnings} Korrekturhinweise</div></div></div><div class="notice {'warn' if warnings else ''}">{'Bitte die markierten Kosten besonders prüfen. Die angezeigten Werte werden importiert.' if warnings else 'Alle Zeilen sehen plausibel aus.'}</div><div class="card table-wrap"><table><thead><tr><th>Datum</th><th>Km-Stand</th><th>Menge</th><th>Literpreis</th><th>Gesamt</th><th>Art</th><th>Prüfung</th></tr></thead><tbody>{table}</tbody></table></div><form method="post" action="/import/commit" style="margin-top:18px"><input type="hidden" name="csrf" value="{csrf_for(token)}"><input type="hidden" name="payload" value="{esc(payload)}"><div class="actions"><button class="btn">{len(rows)} Tankungen importieren</button><a class="btn secondary" href="/import">Abbrechen</a></div></form>"""
        self.send_html(page("Importvorschau", body, "/import"))

    def import_commit(self, form):
        data = verify_blob(str(form.get("payload", "")))
        if not data or data.get("exp", 0) < time.time():
            raise ValueError("Die Importvorschau ist abgelaufen. Bitte Datei erneut prüfen.")
        inserted = skipped = 0
        with connect() as db:
            for row in data["rows"]:
                cur = db.execute(
                    """INSERT OR IGNORE INTO fuelings(vehicle_id,fueled_on,odometer,fluid,liters,unit_price,total_price,fill_type,station,country,location,note,source,external_key)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (data["vehicle_id"], row["fueled_on"], row["odometer"], row["fluid"], row["liters"], row["unit_price"], row["total_price"], row["fill_type"], row["station"], row["country"], row["location"], row["note"], "spritmonitor", row["external_key"]),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
        self.redirect("/fuelings?notice=" + urllib.parse.quote(f"{inserted} Tankungen importiert, {skipped} bereits vorhanden."))

    def support_page(self):
        if COFFEE_URL.startswith(("https://", "http://")):
            coffee = f'<a class="btn coffee" href="{esc(COFFEE_URL)}" target="_blank" rel="noopener noreferrer">☕ Buy me a coffee</a>'
        else:
            coffee = '<span class="btn secondary" title="BUY_ME_A_COFFEE_URL im Portainer-Stack eintragen">☕ Buy me a coffee</span><div class="muted" style="margin-top:10px">Coffee-Link noch im Stack eintragen</div>'
        body = f"""<div class="topbar"><div><h1>Unterstützung</h1><div class="subtitle">Über EnergieLab</div></div></div><div class="card support"><div class="coffee-cup">☕</div><div class="credit">Idea und umsetztung by Lrd.Tiberius</div><p class="muted">EnergieLab bündelt Strom-, PV-, Gas-, Wasser- und Fahrzeugverbräuche lokal in deinem Homelab.</p><div style="margin-top:24px">{coffee}</div><div class="actions" style="justify-content:center;margin-top:28px"><a class="btn secondary" href="/export/backup.json">JSON-Backup</a><a class="btn secondary" href="/export/fuelings.csv">Tankungen als CSV</a></div></div>"""
        self.send_html(page("Unterstützung", body, "/support"))

    def backup_json(self):
        with connect() as db:
            payload = {
                "exported_at": datetime.now().isoformat(),
                "version": APP_VERSION,
                "vehicles": [dict(r) for r in db.execute("SELECT * FROM vehicles")],
                "vehicle_fluids": [dict(r) for r in db.execute("SELECT * FROM vehicle_fluids")],
                "fuelings": [dict(r) for r in db.execute("SELECT * FROM fuelings")],
                "energy_readings": [dict(r) for r in db.execute("SELECT * FROM energy_readings")],
                "energy_tariffs": [dict(r) for r in db.execute("SELECT * FROM energy_tariffs")],
            }
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_bytes(data, "application/json; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="energylab-backup-{date.today()}.json"'})

    def export_fuelings(self):
        out = io.StringIO()
        writer = csv.writer(out, delimiter=";")
        writer.writerow(["Datum", "Fahrzeug", "Betriebsstoff", "Kilometerstand", "Liter", "Preis/Liter", "Gesamtpreis", "Tankart", "Tankstelle", "Ort", "Bemerkung"])
        with connect() as db:
            for row in db.execute("SELECT f.*,v.name vehicle_name FROM fuelings f JOIN vehicles v ON v.id=f.vehicle_id ORDER BY fueled_on,odometer"):
                writer.writerow([row["fueled_on"], row["vehicle_name"], row["fluid"], row["odometer"], row["liters"], row["unit_price"], row["total_price"], row["fill_type"], row["station"], row["location"], row["note"]])
        data = ("\ufeff" + out.getvalue()).encode("utf-8")
        self.send_bytes(data, "text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="energylab-tankungen-{date.today()}.csv"'})


def main():
    init_db()
    threading.Thread(target=sync_scheduler, daemon=True, name="ha-sync").start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"{APP_NAME} {APP_VERSION} läuft auf http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
