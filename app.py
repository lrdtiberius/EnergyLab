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
import zipfile
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn


APP_NAME = "EnergieLab"
APP_VERSION = "1.2.2"
ASSET_DIR = Path(__file__).resolve().parent
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
SYNC_HOUR = int(os.getenv("ENERGYLAB_SYNC_HOUR", "23"))
SYNC_MINUTE = int(os.getenv("ENERGYLAB_SYNC_MINUTE", "30"))
MAX_PERIOD_CROSSING_GAP_DAYS = int(os.getenv("ENERGYLAB_MAX_PERIOD_CROSSING_GAP_DAYS", "62"))
FINANZLAB_URL = os.getenv("FINANZLAB_URL", "").strip().rstrip("/")
FINANZLAB_HOUSEHOLD_ID = os.getenv("FINANZLAB_HOUSEHOLD_ID", "").strip()
FINANZLAB_TOKEN = os.getenv("FINANZLAB_TOKEN", "").strip()
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_RETENTION = max(3, int(os.getenv("ENERGYLAB_BACKUP_RETENTION", "12")))

SEGMENT_BY_METRIC = {
    "grid_import": "electricity",
    "gas": "gas",
    "water": "water",
    "wastewater": "wastewater",
}
PAYMENT_EVENT_LABELS = {
    "regular_payment": "Abschlagszahlung",
    "one_off_payment": "Einmalzahlung",
    "suspension": "Zahlung ausgesetzt",
    "chargeback": "Rücklastschrift",
    "credit_payout": "Guthabenauszahlung",
    "correction": "Korrektur",
}

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
    "wastewater": {
        "label": "Abwasser",
        "icon": "🚰",
        "unit": "m³",
        "entity": "",
        "reading_metric": "water",
    },
}

FLUIDS = ("Diesel", "Benzin", "E10", "Super Plus", "AdBlue", "LPG", "CNG", "Strom")
FILL_TYPES = {"first": "Erste Tankung", "full": "Volltankung", "partial": "Teiltankung"}
VEHICLE_COST_CATEGORIES = (
    "Versicherung",
    "Kfz-Steuer",
    "Reparatur / Wartung",
    "Reifen",
    "Hauptuntersuchung",
    "Parken / Maut",
    "Sonstiges",
)


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt_num(value, digits=2) -> str:
    if value is None:
        return "–"
    text = f"{float(value):,.{digits}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_money(value) -> str:
    return f"{fmt_num(value, 2)} €" if value is not None else "–"


def comparison_difference(value_a, value_b):
    """Describe how period B changed compared with period A."""
    value_a = float(value_a or 0.0)
    value_b = float(value_b or 0.0)
    difference = value_b - value_a
    if math.isclose(difference, 0.0, abs_tol=1e-9):
        return {"difference": 0.0, "percent": 0.0, "label": "gleich", "css": "same"}
    percent = None if math.isclose(value_a, 0.0, abs_tol=1e-9) else difference / value_a * 100.0
    return {
        "difference": difference,
        "percent": percent,
        "label": "mehr" if difference > 0 else "weniger",
        "css": "more" if difference > 0 else "less",
    }


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


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3, then also release the file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=20, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def get_meta(key: str, default="", db=None) -> str:
    """Read one setting without forcing callers to manage a connection."""
    owns_connection = db is None
    db = db or connect()
    try:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except sqlite3.OperationalError:
        return default
    finally:
        if owns_connection:
            db.close()


def set_meta(key: str, value, db=None) -> None:
    owns_connection = db is None
    db = db or connect()
    try:
        db.execute(
            """INSERT INTO meta(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)),
        )
        if owns_connection:
            db.commit()
    finally:
        if owns_connection:
            db.close()


def _installed_version() -> str:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return ""
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            row = db.execute("SELECT value FROM meta WHERE key='app_version'").fetchone()
            return str(row[0]) if row else "legacy"
        finally:
            db.close()
    except sqlite3.Error:
        return "legacy"


def create_automatic_backup(reason="manual") -> Path | None:
    """Create a consistent SQLite copy and retain a bounded local history."""
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(reason))[:48] or "backup"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = BACKUP_DIR / f"energylab-{safe_reason}-{stamp}.sqlite3"
    source = sqlite3.connect(DB_PATH, timeout=20)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Die Sicherung hat die Integritätsprüfung nicht bestanden.")
    finally:
        destination.close()
        source.close()
    backups = sorted(BACKUP_DIR.glob("energylab-*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    for old_backup in backups[BACKUP_RETENTION:]:
        old_backup.unlink(missing_ok=True)
    return target


def restore_database_backup(filename: str) -> Path:
    """Restore a generated local backup after validation and a safety backup."""
    source_path = BACKUP_DIR / Path(str(filename)).name
    if source_path.parent != BACKUP_DIR or not source_path.is_file() or not source_path.name.startswith("energylab-"):
        raise ValueError("Sicherung nicht gefunden.")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Die gewählte Sicherung ist beschädigt.")
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"meta", "energy_readings", "energy_tariffs"}.issubset(tables):
            raise ValueError("Die Datei ist keine gültige EnergieLab-Sicherung.")
        safety = create_automatic_backup("vor-wiederherstellung")
        destination = sqlite3.connect(DB_PATH, timeout=20)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    init_db()
    return safety or source_path


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    previous_version = _installed_version()
    upgrade_backup = None
    if previous_version and previous_version != APP_VERSION:
        upgrade_backup = create_automatic_backup(f"vor-update-{previous_version}-auf-{APP_VERSION}")
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
            CREATE TABLE IF NOT EXISTS vehicle_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
                incurred_on TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL CHECK(amount >= 0),
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_vehicle_expenses_vehicle_date
                ON vehicle_expenses(vehicle_id, incurred_on);
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
                metric TEXT NOT NULL CHECK(metric IN ('grid_import','gas','water','wastewater')),
                provider TEXT NOT NULL DEFAULT '',
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                price_per_kwh REAL NOT NULL CHECK(price_per_kwh >= 0),
                kwh_per_unit REAL NOT NULL CHECK(kwh_per_unit > 0),
                base_fee_monthly REAL NOT NULL DEFAULT 0 CHECK(base_fee_monthly >= 0),
                advance_monthly REAL NOT NULL DEFAULT 0 CHECK(advance_monthly >= 0),
                payment_day INTEGER NOT NULL DEFAULT 1 CHECK(payment_day BETWEEN 1 AND 31),
                payment_interval_months INTEGER NOT NULL DEFAULT 1 CHECK(payment_interval_months IN (1,3,6,12)),
                first_payment_date TEXT,
                payment_account TEXT NOT NULL DEFAULT '',
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
            CREATE TABLE IF NOT EXISTS energy_payment_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT,
                tariff_id INTEGER REFERENCES energy_tariffs(id) ON DELETE SET NULL,
                segment_id TEXT NOT NULL DEFAULT '',
                due_on TEXT,
                booked_on TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                event_type TEXT NOT NULL CHECK(event_type IN ('regular_payment','one_off_payment','suspension','chargeback','credit_payout','correction')),
                status TEXT NOT NULL DEFAULT 'paid',
                source TEXT NOT NULL DEFAULT 'manual',
                match_method TEXT NOT NULL DEFAULT '',
                confidence REAL,
                confirmed INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                raw_payload TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_energy_payment_events_external
                ON energy_payment_events(external_id) WHERE external_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_energy_payment_events_tariff_date
                ON energy_payment_events(tariff_id,booked_on,due_on);
            CREATE TABLE IF NOT EXISTS energy_settlement_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tariff_id INTEGER NOT NULL REFERENCES energy_tariffs(id) ON DELETE RESTRICT,
                revision INTEGER NOT NULL,
                period_from TEXT NOT NULL,
                period_to TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                payment_basis TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tariff_id,period_from,period_to,revision)
            );
            CREATE INDEX IF NOT EXISTS idx_energy_settlement_snapshots_tariff
                ON energy_settlement_snapshots(tariff_id,period_from,period_to,revision DESC);
            CREATE TRIGGER IF NOT EXISTS energy_settlement_snapshots_no_update
                BEFORE UPDATE ON energy_settlement_snapshots BEGIN
                    SELECT RAISE(ABORT,'Fixierte Abrechnungen sind unveränderlich.');
                END;
            CREATE TRIGGER IF NOT EXISTS energy_settlement_snapshots_no_delete
                BEFORE DELETE ON energy_settlement_snapshots BEGIN
                    SELECT RAISE(ABORT,'Fixierte Abrechnungen sind unveränderlich.');
                END;
            CREATE TABLE IF NOT EXISTS integration_sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                integration TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'pull',
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                imported_count INTEGER NOT NULL DEFAULT 0,
                unresolved_count INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_integration_sync_latest
                ON integration_sync_log(integration,synced_at DESC,id DESC);
            CREATE TABLE IF NOT EXISTS backup_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        if "payment_day" not in tariff_columns:
            db.execute("ALTER TABLE energy_tariffs ADD COLUMN payment_day INTEGER NOT NULL DEFAULT 1")
        if "payment_interval_months" not in tariff_columns:
            db.execute("ALTER TABLE energy_tariffs ADD COLUMN payment_interval_months INTEGER NOT NULL DEFAULT 1")
        if "first_payment_date" not in tariff_columns:
            db.execute("ALTER TABLE energy_tariffs ADD COLUMN first_payment_date TEXT")
        if "payment_account" not in tariff_columns:
            db.execute("ALTER TABLE energy_tariffs ADD COLUMN payment_account TEXT NOT NULL DEFAULT ''")
        upgrade_energy_tariff_table(db)
        db.executescript(
            """CREATE TABLE IF NOT EXISTS energy_advance_changes (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   tariff_id INTEGER NOT NULL REFERENCES energy_tariffs(id) ON DELETE CASCADE,
                   valid_from TEXT NOT NULL,
                   advance_monthly REAL NOT NULL CHECK(advance_monthly >= 0),
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(tariff_id,valid_from)
               );
               CREATE INDEX IF NOT EXISTS idx_energy_advance_changes_period
                   ON energy_advance_changes(tariff_id,valid_from);"""
        )
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
        db.execute(
            """INSERT INTO meta(key,value) VALUES('app_version',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (APP_VERSION,),
        )
        if upgrade_backup:
            db.execute(
                "INSERT INTO backup_log(filename,reason,status) VALUES(?,?, 'ok')",
                (upgrade_backup.name, f"Automatisch vor Update von {previous_version} auf {APP_VERSION}"),
            )
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


def period_bounds(period: str, reference: date | None = None):
    """Return inclusive date bounds for a dashboard period."""
    today = reference or date.today()
    if period == "year":
        return date(today.year, 1, 1).isoformat(), date(today.year, 12, 31).isoformat()
    if period == "all":
        return None, None
    if period == "month":
        start = date(today.year, today.month, 1)
        next_month = date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
        return start.isoformat(), (next_month - timedelta(days=1)).isoformat()
    raise ValueError("Unbekannter Zeitraum.")


ENERGY_DETAIL_VIEWS = ("overview", "contract", "payments", "imports")


def normalize_energy_detail_view(value: str | None) -> str:
    """Resolve friendly and legacy query values to a safe detail tab."""
    normalized = str(value or "").strip().lower()
    aliases = {
        "": "overview",
        "overview": "overview",
        "uebersicht": "overview",
        "übersicht": "overview",
        "contract": "contract",
        "vertrag": "contract",
        "payments": "payments",
        "payment": "payments",
        "zahlungsplan": "payments",
        "tilgungsplan": "payments",
        "imports": "imports",
        "import": "imports",
        "historie": "imports",
        "importhistorie": "imports",
    }
    return aliases.get(normalized, "overview")


MONTH_NAMES_DE = (
    "",
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


def month_start_from_query(value: str | None, reference: date | None = None) -> date:
    """Resolve YYYY-MM for the detail view and never allow a future month."""
    today = reference or date.today()
    current_month = date(today.year, today.month, 1)
    try:
        selected = datetime.strptime(str(value or ""), "%Y-%m").date().replace(day=1)
    except ValueError:
        selected = current_month
    return min(selected, current_month)


def shift_month(month_start: date, offset: int) -> date:
    """Move a first-of-month date by an arbitrary number of calendar months."""
    absolute = month_start.year * 12 + month_start.month - 1 + offset
    return date(absolute // 12, absolute % 12 + 1, 1)


def add_months_anchored(day: date, offset: int) -> date:
    """Move a date by months while retaining its contractual day where possible."""
    target = shift_month(day.replace(day=1), offset)
    last_day = (shift_month(target, 1) - timedelta(days=1)).day
    return target.replace(day=min(day.day, last_day))


def sync_time(db=None) -> tuple[int, int]:
    """Return the persisted import time, falling back to the environment."""
    owns_connection = db is None
    db = db or connect()
    try:
        row = db.execute("SELECT value FROM meta WHERE key='sync_time'").fetchone()
        raw = row["value"] if row else f"{SYNC_HOUR:02d}:{SYNC_MINUTE:02d}"
        parsed = datetime.strptime(raw, "%H:%M")
        return parsed.hour, parsed.minute
    except (TypeError, ValueError):
        return SYNC_HOUR, SYNC_MINUTE
    finally:
        if owns_connection:
            db.close()


def reading_metric_for(metric: str) -> str:
    """Abwasser verwendet immer exakt die Messreihe des Wasserzählers."""
    return METRICS.get(metric, {}).get("reading_metric", metric)


def active_energy_tariff(db, metric: str, reference: date | None = None):
    """Return the tariff that is active on the reference day, independent of readings."""
    today = reference or date.today()
    today_text = today.isoformat()
    return db.execute(
        """SELECT * FROM energy_tariffs
           WHERE metric=? AND valid_from<=?
             AND (valid_to IS NULL OR valid_to>=?)
           ORDER BY valid_from DESC,id DESC LIMIT 1""",
        (metric, today_text, today_text),
    ).fetchone()


def tariff_display_sort_key(valid_from, valid_to=None, tariff_id=0, reference: date | None = None):
    """Sort tariffs/sections as: active first, future next, ended last."""
    today = reference or date.today()
    start = date.fromisoformat(str(valid_from))
    end = date.fromisoformat(str(valid_to)) if valid_to else None
    ident = int(tariff_id or 0)
    if start <= today and (end is None or today <= end):
        return (0, -start.toordinal(), -ident)
    if start > today:
        return (1, start.toordinal(), ident)
    ended_on = end or start
    return (2, -ended_on.toordinal(), -start.toordinal(), -ident)


def tariff_display_sort_key_for_row(tariff, reference: date | None = None):
    return tariff_display_sort_key(
        tariff["valid_from"],
        tariff["valid_to"],
        tariff["id"],
        reference,
    )


def tariff_end_or_today(tariff, reference: date | None = None) -> str:
    """Use a bounded end for open contracts without inventing future consumption."""
    today = reference or date.today()
    return tariff["valid_to"] or today.isoformat()


def payment_recurrence(interval_months) -> str:
    return {1: "monthly", 3: "quarterly", 6: "semiannual", 12: "yearly"}.get(int(interval_months or 1), "monthly")


def payment_interval_label(interval_months) -> str:
    return {1: "monatlich", 3: "quartalsweise", 6: "halbjährlich", 12: "jährlich"}.get(int(interval_months or 1), "monatlich")


def selected_month_bounds(month_start: date) -> tuple[str, str]:
    return month_start.isoformat(), (shift_month(month_start, 1) - timedelta(days=1)).isoformat()


def month_label(month_start: date) -> str:
    return f"{MONTH_NAMES_DE[month_start.month]} {month_start.year}"


def upgrade_energy_tariff_table(db) -> None:
    schema_row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='energy_tariffs'"
    ).fetchone()
    schema_sql = (schema_row["sql"] or "") if schema_row else ""
    if "'wastewater'" in schema_sql and "payment_interval_months" in schema_sql and "first_payment_date" in schema_sql:
        return
    changes_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='energy_advance_changes'"
    ).fetchone()
    saved_changes = []
    if changes_table:
        saved_changes = [dict(row) for row in db.execute(
            "SELECT id,tariff_id,valid_from,advance_monthly,created_at FROM energy_advance_changes ORDER BY id"
        )]
        # SQLite follows a renamed parent table in foreign-key definitions. Recreate
        # the child explicitly so dropping the old parent cannot remove its history.
        db.execute("DROP TABLE energy_advance_changes")
    db.execute("ALTER TABLE energy_tariffs RENAME TO energy_tariffs_before_upgrade")
    db.execute(
        """CREATE TABLE energy_tariffs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               metric TEXT NOT NULL CHECK(metric IN ('grid_import','gas','water','wastewater')),
               provider TEXT NOT NULL DEFAULT '',
               valid_from TEXT NOT NULL,
               valid_to TEXT,
               price_per_kwh REAL NOT NULL CHECK(price_per_kwh >= 0),
               kwh_per_unit REAL NOT NULL CHECK(kwh_per_unit > 0),
               base_fee_monthly REAL NOT NULL DEFAULT 0 CHECK(base_fee_monthly >= 0),
               advance_monthly REAL NOT NULL DEFAULT 0 CHECK(advance_monthly >= 0),
               payment_day INTEGER NOT NULL DEFAULT 1 CHECK(payment_day BETWEEN 1 AND 31),
               payment_interval_months INTEGER NOT NULL DEFAULT 1 CHECK(payment_interval_months IN (1,3,6,12)),
               first_payment_date TEXT,
               payment_account TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               CHECK(valid_to IS NULL OR valid_to >= valid_from)
           )"""
    )
    db.execute(
        """INSERT INTO energy_tariffs(
               id,metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
               base_fee_monthly,advance_monthly,payment_day,payment_interval_months,first_payment_date,payment_account,created_at
           )
           SELECT id,metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,
                  base_fee_monthly,advance_monthly,payment_day,payment_interval_months,first_payment_date,payment_account,created_at
           FROM energy_tariffs_before_upgrade"""
    )
    db.execute("DROP TABLE energy_tariffs_before_upgrade")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_energy_tariffs_period ON energy_tariffs(metric,valid_from,valid_to)"
    )
    if changes_table:
        db.execute(
            """CREATE TABLE energy_advance_changes (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   tariff_id INTEGER NOT NULL REFERENCES energy_tariffs(id) ON DELETE CASCADE,
                   valid_from TEXT NOT NULL,
                   advance_monthly REAL NOT NULL CHECK(advance_monthly >= 0),
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(tariff_id,valid_from)
               )"""
        )
        db.executemany(
            "INSERT INTO energy_advance_changes(id,tariff_id,valid_from,advance_monthly,created_at) VALUES(?,?,?,?,?)",
            [(row["id"], row["tariff_id"], row["valid_from"], row["advance_monthly"], row["created_at"]) for row in saved_changes],
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_energy_advance_changes_period ON energy_advance_changes(tariff_id,valid_from)"
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


def calendar_months_touched(start_on: str, end_on: str) -> int:
    """Count every calendar month touched by the inclusive period once."""
    start = date.fromisoformat(start_on)
    end = date.fromisoformat(end_on)
    if end < start:
        return 0
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _row_value(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def scheduled_payment_events(db, tariff, start_on: str, end_on: str):
    """Return a deterministic contractual schedule, including explicit suspensions."""
    period_start = date.fromisoformat(start_on)
    period_end = date.fromisoformat(end_on)
    if period_end < period_start:
        return []
    changes = db.execute(
        """SELECT valid_from,advance_monthly FROM energy_advance_changes
           WHERE tariff_id=? ORDER BY valid_from,id""",
        (tariff["id"],),
    ).fetchall()
    contract_start = date.fromisoformat(tariff["valid_from"])
    contract_end = date.fromisoformat(tariff["valid_to"]) if tariff["valid_to"] else None
    schedule = [(contract_start, float(tariff["advance_monthly"]))]
    schedule.extend((date.fromisoformat(row["valid_from"]), float(row["advance_monthly"])) for row in changes)
    interval_months = int(tariff["payment_interval_months"] or 1)
    if tariff["first_payment_date"]:
        anchor = date.fromisoformat(tariff["first_payment_date"])
        preferred_day = anchor.day
    else:
        month = contract_start.replace(day=1)
        preferred_day = int(tariff["payment_day"] or 1)
        last_day = (shift_month(month, 1) - timedelta(days=1)).day
        anchor = month.replace(day=min(preferred_day, last_day))
        if anchor < contract_start:
            target_month = shift_month(month, interval_months)
            target_last_day = (shift_month(target_month, 1) - timedelta(days=1)).day
            anchor = target_month.replace(day=min(preferred_day, target_last_day))
    effective_end = min(value for value in (period_end, contract_end) if value is not None)
    suspensions = {
        row["due_on"] or row["booked_on"]
        for row in db.execute(
            """SELECT due_on,booked_on FROM energy_payment_events
               WHERE tariff_id=? AND event_type='suspension'
                 AND status NOT IN ('cancelled','reversed')""",
            (tariff["id"],),
        )
    }
    events = []
    occurrence = 0
    due = anchor
    while due <= effective_end:
        if due >= period_start and due >= contract_start:
            applicable = [amount for effective_from, amount in schedule if effective_from <= due]
            if applicable:
                due_text = due.isoformat()
                suspended = due_text in suspensions
                amount = 0.0 if suspended else float(applicable[-1])
                segment_id = SEGMENT_BY_METRIC.get(tariff["metric"], tariff["metric"])
                events.append({
                    "id": f"energylab:advance:{segment_id}:{tariff['id']}:{due_text}",
                    "sourceKey": f"energylab:advance:{segment_id}:{tariff['id']}",
                    "segmentId": segment_id,
                    "contractId": str(tariff["id"]),
                    "dueOn": due_text,
                    "plannedDate": due_text,
                    "amount": amount,
                    "amountCents": int(round(amount * 100)),
                    "currency": "EUR",
                    "status": "suspended" if suspended else "planned",
                })
        occurrence += interval_months
        target_month = shift_month(anchor.replace(day=1), occurrence)
        target_last_day = (shift_month(target_month, 1) - timedelta(days=1)).day
        due = target_month.replace(day=min(preferred_day, target_last_day))
    return events


def advance_for_period(db, tariff, start_on: str, end_on: str) -> float:
    """Sum scheduled payments actually due in the inclusive period."""
    return sum(event["amount"] for event in scheduled_payment_events(db, tariff, start_on, end_on))


def actual_payments_for_period(db, tariff, start_on: str, end_on: str, basis="cash"):
    """Sum confirmed payments on either a cash or supplier-billing basis.

    Cash views use the bank booking date.  A supplier statement instead uses the
    explicitly matched due date first, falling back to the booking date only for
    events that have no contractual allocation.  Keeping both views explicit is
    important at a supplier switch, where a final instalment can be booked one or
    two days after the old contract ended.
    """
    if basis not in ("cash", "billing"):
        raise ValueError("Zahlungsbasis muss 'cash' oder 'billing' sein.")
    period_column = (
        "booked_on" if basis == "cash"
        else "COALESCE(NULLIF(due_on,''),booked_on)"
    )
    rows = db.execute(
        f"""SELECT * FROM energy_payment_events
            WHERE tariff_id=? AND {period_column}>=? AND {period_column}<=?
            ORDER BY {period_column},booked_on,id""",
        (tariff["id"], start_on, end_on),
    ).fetchall()
    counted = []
    ignored = []
    for row in rows:
        rejected = row["status"] in ("cancelled", "pending", "review", "ignored")
        reversed_original = row["status"] == "reversed" and row["event_type"] not in (
            "chargeback", "credit_payout", "correction"
        )
        if not row["confirmed"] or rejected or reversed_original:
            ignored.append(dict(row))
            continue
        if row["event_type"] == "suspension":
            continue
        counted.append(dict(row))
    planned = scheduled_payment_events(db, tariff, start_on, end_on)
    # Only a regular payment or an explicit suspension fulfils a planned
    # instalment.  Corrections, one-off payments and chargebacks are additive
    # effects; treating one of them as the instalment itself would understate
    # (or even invert) the paid amount during a partial bank import.
    represented_due_dates = {
        row["due_on"] or row["booked_on"] for row in rows
        if row["confirmed"]
        and row["event_type"] in ("regular_payment", "suspension")
        and row["status"] not in ("cancelled", "pending", "review", "ignored", "reversed")
    }
    represented_days = [date.fromisoformat(value) for value in represented_due_dates if value]
    missing_due_dates = [
        event["plannedDate"] for event in planned
        if not any(abs((candidate-date.fromisoformat(event["plannedDate"])).days) <= 3 for candidate in represented_days)
    ]
    complete = not missing_due_dates and (bool(rows) or not planned)
    confirmed_suspension = any(
        row["confirmed"]
        and row["event_type"] == "suspension"
        and row["status"] not in ("cancelled", "pending", "review", "ignored", "reversed")
        for row in rows
    )
    return {
        "amount": sum(float(row["amount"] or 0) for row in counted),
        "count": len(counted),
        # A pending or rejected import must never replace the contractual plan
        # with zero.  A confirmed suspension, on the other hand, is a valid
        # zero-payment result and therefore makes actual data available.
        "available": bool(counted) or confirmed_suspension,
        "complete": complete,
        "missing_due_dates": missing_due_dates,
        "events": counted,
        "ignored": ignored,
        "basis": basis,
    }


def payment_basis_for_period(db, tariff, start_on: str, end_on: str, basis="cash"):
    """Combine confirmed payments with the plan for still-unmatched dues.

    A bank import can be incomplete (for example, only the most recent month
    was loaded).  Confirmed entries remain authoritative, while missing due
    dates continue to use their contractual amount until they are matched.
    This avoids turning every not-yet-imported instalment into an apparent
    missed payment and a false projected additional payment.
    """
    planned_events = scheduled_payment_events(db, tariff, start_on, end_on)
    planned_total = sum(float(event["amount"]) for event in planned_events)
    actual = actual_payments_for_period(db, tariff, start_on, end_on, basis=basis)
    missing = set(actual["missing_due_dates"])
    missing_planned = sum(
        float(event["amount"]) for event in planned_events
        if event["plannedDate"] in missing
    )
    if not actual["available"]:
        effective = planned_total
        basis = "planned"
    elif missing:
        effective = float(actual["amount"]) + missing_planned
        basis = "actual_plus_planned"
    else:
        effective = float(actual["amount"])
        basis = "actual"
    return {
        "planned": planned_total,
        "actual": float(actual["amount"]),
        "actual_available": bool(actual["available"]),
        "effective": effective,
        "basis": basis,
        "complete": bool(actual["complete"]),
        "missing_due_dates": sorted(missing),
        "missing_planned": missing_planned,
        "events": actual["events"],
        "ignored": actual["ignored"],
    }


def energy_payment_plan(db, metric: str, start_on=None, end_on=None, reference: date | None = None):
    """Build read-only payment rows with their proportionate energy cost period.

    Each contractual due date owns a non-overlapping consumption interval. Confirmed
    bank events are matched by their explicit due date first and, for regular
    payments without a due date, by a narrow booking-date window. Unmatched special
    payments remain visible as separate rows instead of silently changing a plan.
    """
    if metric not in ("grid_import", "gas", "water", "wastewater"):
        return []
    today = reference or date.today()
    filter_start = date.fromisoformat(start_on) if start_on else None
    filter_end = date.fromisoformat(end_on) if end_on else None
    reading_metric = reading_metric_for(metric)
    rows = []

    def event_is_counted(event) -> bool:
        if not bool(event["confirmed"]):
            return False
        if event["status"] in ("cancelled", "pending", "review", "ignored"):
            return False
        return not (
            event["status"] == "reversed"
            and event["event_type"] not in ("chargeback", "credit_payout", "correction")
        )

    tariffs = db.execute(
        "SELECT * FROM energy_tariffs WHERE metric=? ORDER BY valid_from,id", (metric,)
    ).fetchall()
    for tariff in tariffs:
        contract_start = date.fromisoformat(tariff["valid_from"])
        contract_end = date.fromisoformat(tariff["valid_to"]) if tariff["valid_to"] else None
        # Open-ended contracts need a bounded, useful planning horizon.
        horizon = filter_end or contract_end or date(today.year, 12, 31)
        if horizon < contract_start:
            continue
        schedule = scheduled_payment_events(
            db, tariff, tariff["valid_from"], horizon.isoformat()
        )
        actual_events = db.execute(
            """SELECT * FROM energy_payment_events WHERE tariff_id=?
               ORDER BY COALESCE(due_on,booked_on),booked_on,id""",
            (tariff["id"],),
        ).fetchall()
        assigned_event_ids = set()

        for index, planned in enumerate(schedule):
            due = date.fromisoformat(planned["dueOn"])
            if filter_start and due < filter_start:
                continue
            if filter_end and due > filter_end:
                continue
            next_due = (
                date.fromisoformat(schedule[index + 1]["dueOn"])
                if index + 1 < len(schedule)
                else None
            )
            cost_start = contract_start if index == 0 else due
            cost_end = (next_due - timedelta(days=1)) if next_due else horizon
            if contract_end:
                cost_end = min(cost_end, contract_end)
            if filter_start:
                cost_start = max(cost_start, filter_start)
            if filter_end:
                cost_end = min(cost_end, filter_end)

            explicit = [
                event for event in actual_events
                if event["id"] not in assigned_event_ids and event["due_on"] == planned["dueOn"]
            ]
            matched = explicit
            if not matched:
                candidates = [
                    event for event in actual_events
                    if event["id"] not in assigned_event_ids
                    and not event["due_on"]
                    and event["event_type"] == "regular_payment"
                    and abs((date.fromisoformat(event["booked_on"]) - due).days) <= 3
                ]
                if candidates:
                    matched = [min(
                        candidates,
                        key=lambda event: (
                            abs((date.fromisoformat(event["booked_on"]) - due).days),
                            event["id"],
                        ),
                    )]
            for event in matched:
                assigned_event_ids.add(event["id"])

            counted = [event for event in matched if event_is_counted(event)]
            review = any(
                not bool(event["confirmed"]) or event["status"] in ("pending", "review")
                for event in matched
            )
            actual_available = bool(counted) or any(
                event["event_type"] == "suspension" and event_is_counted(event)
                for event in matched
            )
            actual_amount = (
                sum(float(event["amount"] or 0) for event in counted) if actual_available else None
            )
            planned_amount = float(planned["amount"] or 0)

            if cost_end >= cost_start:
                consumption, variable_cost = allocated_usage(
                    db,
                    reading_metric,
                    cost_start.isoformat(),
                    cost_end.isoformat(),
                    metric,
                )
                base_fee = prorated_months(
                    cost_start.isoformat(), cost_end.isoformat()
                ) * float(tariff["base_fee_monthly"])
                total_cost = variable_cost + base_fee
            else:
                consumption = variable_cost = base_fee = total_cost = 0.0
            effective_payment = actual_amount if actual_available else planned_amount
            balance = effective_payment - total_cost

            if planned["status"] == "suspended":
                status, status_class = "Ausgesetzt", "warn"
            elif review and not actual_available:
                status, status_class = "Prüfung nötig", "warn"
            elif actual_available:
                if actual_amount < 0:
                    status, status_class = "Rückbelastet", "warn"
                elif math.isclose(actual_amount, planned_amount, abs_tol=0.01):
                    status, status_class = "Bezahlt", "ok"
                elif actual_amount < planned_amount:
                    status, status_class = "Teilzahlung", "warn"
                else:
                    status, status_class = "Mehrzahlung", "ok"
            elif due > today:
                status, status_class = "Geplant", ""
            elif due == today:
                status, status_class = "Heute fällig", "warn"
            else:
                status, status_class = "Offen", "warn"

            rows.append({
                "tariff_id": tariff["id"],
                "provider": tariff["provider"],
                "tariff_from": tariff["valid_from"],
                "tariff_to": tariff["valid_to"],
                "due_on": planned["dueOn"],
                "period_from": cost_start.isoformat(),
                "period_to": cost_end.isoformat(),
                "planned": planned_amount,
                "actual": actual_amount,
                "actual_available": actual_available,
                "base_fee": base_fee,
                "consumption": consumption,
                "variable_cost": variable_cost,
                "total_cost": total_cost,
                "balance": balance,
                "status": status,
                "status_class": status_class,
                "event_count": len(matched),
                "kind": "scheduled",
            })

        # Keep one-off payments, corrections and unmatched bank entries visible.
        for event in actual_events:
            if event["id"] in assigned_event_ids or not event_is_counted(event):
                continue
            event_day = date.fromisoformat(event["due_on"] or event["booked_on"])
            if filter_start and event_day < filter_start:
                continue
            if filter_end and event_day > filter_end:
                continue
            amount = float(event["amount"] or 0)
            rows.append({
                "tariff_id": tariff["id"],
                "provider": tariff["provider"],
                "tariff_from": tariff["valid_from"],
                "tariff_to": tariff["valid_to"],
                "due_on": event_day.isoformat(),
                "booked_on": event["booked_on"],
                "period_from": None,
                "period_to": None,
                "planned": None,
                "actual": amount,
                "actual_available": True,
                "base_fee": None,
                "consumption": None,
                "variable_cost": None,
                "total_cost": None,
                "balance": amount,
                "status": PAYMENT_EVENT_LABELS.get(event["event_type"], "Sonderbuchung"),
                "status_class": "warn" if amount < 0 else "ok",
                "event_count": 1,
                "kind": "special",
            })

    return sorted(rows, key=lambda item: (item["due_on"], item["kind"], item["tariff_id"]))


def energy_payment_plan_sections(db, metric: str, start_on=None, end_on=None, reference: date | None = None):
    """Return one independent payment and cost summary per tariff revision.

    Sections are deliberately keyed by tariff id, not provider name. A new tariff
    from the same provider is therefore just as strict an accounting boundary as
    an actual supplier change.
    """
    if metric not in ("grid_import", "gas", "water", "wastewater"):
        return []
    today = reference or date.today()
    filter_start = date.fromisoformat(start_on) if start_on else None
    filter_end = date.fromisoformat(end_on) if end_on else None
    plan_rows = energy_payment_plan(db, metric, start_on, end_on, today)
    rows_by_tariff = {}
    for row in plan_rows:
        rows_by_tariff.setdefault(row["tariff_id"], []).append(row)

    sections = []
    tariffs = db.execute(
        "SELECT * FROM energy_tariffs WHERE metric=? ORDER BY valid_from,id", (metric,)
    ).fetchall()
    for tariff in tariffs:
        tariff_start = date.fromisoformat(tariff["valid_from"])
        tariff_end = date.fromisoformat(tariff["valid_to"]) if tariff["valid_to"] else None
        section_start = max(value for value in (tariff_start, filter_start) if value is not None)
        fallback_end = filter_end or tariff_end or date(today.year, 12, 31)
        section_end = min(value for value in (tariff_end, fallback_end) if value is not None)
        if section_end < section_start:
            continue

        section_rows = rows_by_tariff.get(tariff["id"], [])
        calculated_to = min(section_end, today)
        if calculated_to >= section_start:
            consumption, variable_cost = allocated_usage(
                db,
                reading_metric_for(metric),
                section_start.isoformat(),
                calculated_to.isoformat(),
                metric,
            )
            base_fee = prorated_months(
                section_start.isoformat(), calculated_to.isoformat()
            ) * float(tariff["base_fee_monthly"])
        else:
            consumption = variable_cost = base_fee = 0.0
        balance_rows = [
            row for row in section_rows
            if date.fromisoformat(row["due_on"]) <= calculated_to
        ] if calculated_to >= section_start else []
        scheduled_balance_rows = [row for row in balance_rows if row["kind"] == "scheduled"]
        special_balance_rows = [row for row in balance_rows if row["kind"] == "special"]
        missing_due_dates = [
            row["due_on"] for row in scheduled_balance_rows
            if not row["actual_available"] and float(row["planned"] or 0) > 0
        ]
        actual_balance_rows = [row for row in balance_rows if row["actual_available"]]
        effective_payments = sum(
            float(row["actual"] or 0) if row["actual_available"] else float(row["planned"] or 0)
            for row in scheduled_balance_rows
        ) + sum(float(row["actual"] or 0) for row in special_balance_rows)
        if not actual_balance_rows:
            payment_basis = "planned"
        elif missing_due_dates:
            payment_basis = "actual_plus_planned"
        else:
            payment_basis = "actual"
        total_cost = variable_cost + base_fee
        balance = effective_payments - total_cost
        latest = db.execute(
            """SELECT read_on FROM energy_readings
               WHERE metric=? AND is_valid=1 AND read_on>=? AND read_on<=?
               ORDER BY read_on DESC,id DESC LIMIT 1""",
            (
                reading_metric_for(metric),
                section_start.isoformat(),
                calculated_to.isoformat(),
            ),
        ).fetchone() if calculated_to >= section_start else None
        meter_complete = bool(latest and latest["read_on"] >= calculated_to.isoformat())
        boundary_missing = missing_supplier_switch_readings(
            db,
            tariff,
            tariff["valid_from"],
            tariff["valid_to"] or section_end.isoformat(),
        )
        final = bool(
            tariff_end
            and tariff_end <= today
            and section_start == tariff_start
            and section_end == tariff_end
            and meter_complete
            and not missing_due_dates
            and not boundary_missing
        )
        if math.isclose(balance, 0.0, abs_tol=0.005):
            result_label = "Ausgeglichen" if final else "Vorläufig ausgeglichen"
            result_class = "ok"
        elif balance > 0:
            result_label = "Erstattung" if final else "Voraussichtliche Erstattung"
            result_class = "ok"
        else:
            result_label = "Nachzahlung" if final else "Voraussichtliche Nachzahlung"
            result_class = "warn"

        sections.append({
            "tariff_id": tariff["id"],
            "provider": tariff["provider"] or "Tarif ohne Anbieter",
            "tariff_from": tariff["valid_from"],
            "tariff_to": tariff["valid_to"],
            "period_from": section_start.isoformat(),
            "period_to": section_end.isoformat(),
            "calculated_to": calculated_to.isoformat() if calculated_to >= section_start else None,
            "payment_interval_months": int(tariff["payment_interval_months"] or 1),
            "rows": section_rows,
            "planned": sum(float(row["planned"] or 0) for row in section_rows if row["kind"] == "scheduled"),
            "actual": sum(float(row["actual"] or 0) for row in section_rows if row["actual_available"]),
            "actual_available": any(row["actual_available"] for row in section_rows),
            "payment_basis": payment_basis,
            "missing_due_dates": missing_due_dates,
            "missing_boundary_readings": boundary_missing,
            "effective_payments": effective_payments,
            "base_fee": base_fee,
            "consumption": consumption,
            "variable_cost": variable_cost,
            "total_cost": total_cost,
            "balance": balance,
            "result_label": result_label,
            "result_class": result_class,
            "final": final,
        })
    return sorted(
        sections,
        key=lambda section: tariff_display_sort_key(
            section["tariff_from"],
            section["tariff_to"],
            section["tariff_id"],
            today,
        ),
    )


def settlement_values(variable=0.0, base_fee=0.0, advance=0.0, actual_advance=None):
    """Single source of truth for every settlement shown or exported."""
    variable = float(variable or 0.0)
    base_fee = float(base_fee or 0.0)
    planned_advance = float(advance or 0.0)
    actual_available = actual_advance is not None
    actual_advance = float(actual_advance or 0.0) if actual_available else None
    effective_advance = actual_advance if actual_available else planned_advance
    cost = variable + base_fee
    return {
        "variable": variable,
        "base_fee": base_fee,
        "cost": cost,
        # ``advance`` remains for API v2 consumers; it is now the best available
        # settlement basis (confirmed actuals, otherwise the contractual plan).
        "advance": effective_advance,
        "planned_advance": planned_advance,
        "actual_advance": actual_advance,
        "actual_available": actual_available,
        "payment_basis": "actual" if actual_available else "planned",
        "balance": effective_advance - cost,
    }


def allocated_usage(db, metric, start_on=None, end_on=None, tariff_metric=None):
    """Split short intervals by day, but never estimate long gaps across a period boundary."""
    readings = db.execute(
        """SELECT read_on,delta_value FROM energy_readings
           WHERE metric=? AND is_valid=1 ORDER BY read_on,id""",
        (metric,),
    ).fetchall()
    tariffs = db.execute(
        "SELECT * FROM energy_tariffs WHERE metric=? ORDER BY valid_from,id",
        (tariff_metric or metric,),
    ).fetchall()
    range_start = date.fromisoformat(start_on) if start_on else None
    range_end = date.fromisoformat(end_on) if end_on else None
    previous_day = None
    consumption = 0.0
    variable_cost = 0.0
    for reading in readings:
        current_day = date.fromisoformat(reading["read_on"])
        delta = reading["delta_value"]
        if delta is not None:
            interval_start = previous_day + timedelta(days=1) if previous_day else current_day
            interval_days = max(1, (current_day - interval_start).days + 1)
            daily_usage = float(delta) / interval_days
            crosses_uncertain_report_boundary = bool(
                range_start
                and interval_days > MAX_PERIOD_CROSSING_GAP_DAYS
                and interval_start < range_start <= current_day
            )
            cursor = interval_start
            while cursor <= current_day:
                if (range_start is None or cursor >= range_start) and (range_end is None or cursor <= range_end):
                    cursor_iso = cursor.isoformat()
                    tariff = next((item for item in tariffs if item["valid_from"] <= cursor_iso and (not item["valid_to"] or cursor_iso <= item["valid_to"])), None)
                    if not crosses_uncertain_report_boundary:
                        consumption += daily_usage
                    crosses_uncertain_tariff_boundary = bool(
                        tariff
                        and interval_days > MAX_PERIOD_CROSSING_GAP_DAYS
                        and interval_start < date.fromisoformat(tariff["valid_from"]) <= current_day
                    )
                    if tariff and not crosses_uncertain_report_boundary and not crosses_uncertain_tariff_boundary:
                        variable_cost += daily_usage * float(tariff["kwh_per_unit"]) * float(tariff["price_per_kwh"])
                cursor += timedelta(days=1)
        previous_day = current_day
    return consumption, variable_cost


def energy_finances(db, start_on=None, end_on=None):
    result = {}
    for metric in ("grid_import", "gas", "water", "wastewater"):
        _consumption, variable = allocated_usage(
            db, reading_metric_for(metric), start_on, end_on, metric
        )
        result[metric] = {
            "variable": variable, "base_fee": 0.0, "planned_advance": 0.0,
            "actual_advance": 0.0, "actual_available": False,
            "effective_advance": 0.0, "payment_bases": [], "missing_due_dates": [],
        }
    for tariff in db.execute("SELECT * FROM energy_tariffs ORDER BY valid_from,id"):
        metric = tariff["metric"]
        period_start = max(value for value in (tariff["valid_from"], start_on) if value)
        candidates = [value for value in (tariff["valid_to"], end_on, date.today().isoformat()) if value]
        period_end = min(candidates)
        if period_end < period_start:
            continue
        result.setdefault(metric, {
            "variable": 0.0, "base_fee": 0.0, "planned_advance": 0.0,
            "actual_advance": 0.0, "actual_available": False,
            "effective_advance": 0.0, "payment_bases": [], "missing_due_dates": [],
        })
        result[metric]["base_fee"] += prorated_months(period_start, period_end) * tariff["base_fee_monthly"]
        payments = payment_basis_for_period(db, tariff, period_start, period_end)
        result[metric]["planned_advance"] += payments["planned"]
        result[metric]["actual_advance"] += payments["actual"]
        result[metric]["effective_advance"] += payments["effective"]
        result[metric]["payment_bases"].append(payments["basis"])
        result[metric]["missing_due_dates"].extend(payments["missing_due_dates"])
        result[metric]["actual_available"] = result[metric]["actual_available"] or payments["actual_available"]
    for metric, values in tuple(result.items()):
        settled = settlement_values(values["variable"], values["base_fee"], values["planned_advance"])
        settled["actual_advance"] = values["actual_advance"] if values["actual_available"] else None
        settled["actual_available"] = values["actual_available"]
        settled["advance"] = values["effective_advance"]
        settled["balance"] = settled["advance"] - settled["cost"]
        bases = values["payment_bases"]
        settled["payment_basis"] = (
            "planned" if not values["actual_available"]
            else "actual" if bases and all(basis == "actual" for basis in bases)
            else "actual_plus_planned"
        )
        settled["missing_due_dates"] = sorted(set(values["missing_due_dates"]))
        result[metric] = settled
    return result


def energy_costs(db, start_on=None, end_on=None):
    return {metric: values["cost"] for metric, values in energy_finances(db, start_on, end_on).items()}


def settlement_forecast(db, metric: str):
    """Project today's active contract and expose every accounting assumption."""
    if metric not in ("grid_import", "gas", "water", "wastewater"):
        return None
    reading_metric = reading_metric_for(metric)
    today = date.today()
    today_on = today.isoformat()
    tariff = db.execute(
        """SELECT * FROM energy_tariffs
           WHERE metric=? AND valid_from<=?
             AND (valid_to IS NULL OR valid_to>=?)
           ORDER BY valid_from DESC,id DESC LIMIT 1""",
        (metric, today_on, today_on),
    ).fetchone()
    if not tariff:
        return {"reason": "Aktuell ist kein laufender Tarif hinterlegt."}
    if not tariff["valid_to"]:
        return {"reason": "Bitte beim aktuellen Tarif ein Enddatum hinterlegen."}

    contract_start = date.fromisoformat(tariff["valid_from"])
    contract_end = date.fromisoformat(tariff["valid_to"])
    latest = db.execute(
        """SELECT read_on FROM energy_readings
           WHERE metric=? AND is_valid=1 AND read_on>=? AND read_on<=?
           ORDER BY read_on DESC,id DESC LIMIT 1""",
        (reading_metric, tariff["valid_from"], min(today, contract_end).isoformat()),
    ).fetchone()
    if not latest:
        return {
            "reason": "Für den aktuell laufenden Tarif ist noch kein gültiger Zählerstand vorhanden.",
            "provider": tariff["provider"],
            "contract_end": tariff["valid_to"],
        }
    latest_on = latest["read_on"]
    latest_day = date.fromisoformat(latest_on)
    consumption, current_variable = allocated_usage(
        db, reading_metric, tariff["valid_from"], latest_on, metric
    )
    if consumption <= 0:
        return {
            "reason": "Für die Hochrechnung werden mindestens zwei gültige Zählerstände benötigt.",
            "provider": tariff["provider"],
            "contract_end": tariff["valid_to"],
        }
    first_reading = db.execute(
        """SELECT read_on FROM energy_readings
           WHERE metric=? AND is_valid=1 AND read_on>=? AND read_on<=?
           ORDER BY read_on,id LIMIT 1""",
        (reading_metric, tariff["valid_from"], latest_on),
    ).fetchone()
    # On a supplier change, the old contract's exact closing reading (D) is
    # also the baseline for the new contract beginning on D+1.  It deliberately
    # sits one day outside the new tariff and must not be discarded when the
    # observed daily average is calculated.
    previous_day = contract_start - timedelta(days=1)
    switch_baseline = db.execute(
        """SELECT read_on FROM energy_readings
           WHERE metric=? AND is_valid=1 AND read_on=?
           ORDER BY id DESC LIMIT 1""",
        (reading_metric, previous_day.isoformat()),
    ).fetchone()
    observed_start = (
        previous_day
        if switch_baseline
        else max(contract_start, date.fromisoformat(first_reading["read_on"]))
    )
    observed_days = max(1, (latest_day - observed_start).days)
    daily_average = consumption / observed_days
    remaining_days = max(0, (contract_end - latest_day).days)
    price_per_unit = float(tariff["price_per_kwh"]) * float(tariff["kwh_per_unit"])

    current_base = prorated_months(tariff["valid_from"], latest_on) * float(tariff["base_fee_monthly"])
    current_payments = payment_basis_for_period(db, tariff, tariff["valid_from"], latest_on)

    projected_consumption = consumption + daily_average * remaining_days
    projected_variable = current_variable + daily_average * remaining_days * price_per_unit
    projected_base = prorated_months(tariff["valid_from"], tariff["valid_to"]) * float(tariff["base_fee_monthly"])
    projected_planned = advance_for_period(db, tariff, tariff["valid_from"], tariff["valid_to"])

    current = settlement_values(current_variable, current_base, current_payments["planned"])
    current["actual_advance"] = current_payments["actual"] if current_payments["actual_available"] else None
    current["actual_available"] = current_payments["actual_available"]
    current["advance"] = current_payments["effective"]
    current["balance"] = current["advance"] - current["cost"]
    current["payment_basis"] = current_payments["basis"]
    current["missing_due_dates"] = current_payments["missing_due_dates"]

    # Bank payments are known through today, independently of how recent the
    # latest meter reading is.  Confirmed values replace their matching dues;
    # missing past and all future dues retain the contractual plan.
    payment_cutoff = min(today, contract_end)
    elapsed_payments = payment_basis_for_period(
        db, tariff, tariff["valid_from"], payment_cutoff.isoformat()
    )
    future_start = payment_cutoff + timedelta(days=1)
    future_planned = (
        advance_for_period(db, tariff, future_start.isoformat(), tariff["valid_to"])
        if future_start <= contract_end else 0.0
    )
    projected = settlement_values(projected_variable, projected_base, projected_planned)
    projected["actual_advance"] = elapsed_payments["actual"] if elapsed_payments["actual_available"] else None
    projected["actual_available"] = elapsed_payments["actual_available"]
    projected["advance"] = elapsed_payments["effective"] + future_planned
    projected["balance"] = projected["advance"] - projected["cost"]
    projected["payment_basis"] = (
        "planned" if not elapsed_payments["actual_available"]
        else "actual" if not future_planned and elapsed_payments["basis"] == "actual"
        else "actual_plus_planned"
    )
    projected["missing_due_dates"] = elapsed_payments["missing_due_dates"]
    projected["future_planned"] = future_planned
    current["consumption"] = consumption
    projected["consumption"] = projected_consumption
    data_age_days = max(0, (today - latest_day).days)
    quality = "good"
    quality_label = "Gute Datenbasis"
    if observed_days < 30:
        quality, quality_label = "limited", "Kurze Datenbasis"
    if data_age_days > 7:
        quality, quality_label = "stale", f"Zählerstand {data_age_days} Tage alt"
    method = f"Lineare Hochrechnung aus {observed_days} Beobachtungstagen"
    if metric == "gas":
        method += "; saisonale Temperaturschwankungen sind nicht eingerechnet"
    return {
        "provider": tariff["provider"],
        "contract_start": tariff["valid_from"],
        "contract_end": tariff["valid_to"],
        "data_until": latest_on,
        "baseline_on": observed_start.isoformat(),
        "payment_until": payment_cutoff.isoformat(),
        "observed_days": observed_days,
        "daily_average": daily_average,
        "data_age_days": data_age_days,
        "quality": quality,
        "quality_label": quality_label,
        "method": method,
        "current": current,
        "projected": projected,
    }


def pv_savings(db, start_on=None, end_on=None):
    """Value PV self-consumption at the applicable grid work price without offsetting costs."""
    consumption, savings = allocated_usage(db, "pv_self", start_on, end_on, "grid_import")
    return savings if consumption else None


def personallab_payload():
    """Expose EnergyLab's own calculations as a read-only PersonalLab API."""
    today = date.today()
    today_text = today.isoformat()
    month_start = date(today.year, today.month, 1).isoformat()
    year_start = date(today.year, 1, 1).isoformat()
    specs = (
        ("electricity", "grid_import", "Strom", "kWh"),
        ("water", "water", "Wasser", "m³"),
        ("wastewater", "wastewater", "Abwasser", "m³"),
        ("gas", "gas", "Gas", "m³"),
        ("pv", "pv_self", "Photovoltaik", "kWh"),
    )

    def finance_values(values):
        if not values:
            return None
        return {
            "variable": values.get("variable"),
            "baseFee": values.get("base_fee"),
            "cost": values.get("cost"),
            "advance": values.get("advance"),
            "plannedAdvance": values.get("planned_advance"),
            "actualAdvance": values.get("actual_advance"),
            "actualAvailable": values.get("actual_available", False),
            "paymentBasis": values.get("payment_basis", "planned"),
            "balance": values.get("balance"),
        }

    with connect() as db:
        month_finances = energy_finances(db, month_start, today_text)
        year_finances = energy_finances(db, year_start, today_text)
        tariffs = [dict(row) for row in db.execute(
            "SELECT * FROM energy_tariffs ORDER BY valid_from DESC,id DESC"
        )]
        segments = []
        for segment_id, metric, label, unit in specs:
            reading_metric = reading_metric_for(metric)
            history = [{
                "id": row["id"], "date": row["read_on"],
                "total": row["total_value"], "delta": row["delta_value"],
                "unit": row["unit"], "source": row["source"],
            } for row in db.execute(
                """SELECT id,read_on,total_value,delta_value,unit,source
                   FROM energy_readings WHERE metric=? AND is_valid=1
                   ORDER BY read_on DESC,id DESC LIMIT 120""", (reading_metric,)
            )]
            tariff_metric = "grid_import" if metric == "pv_self" else metric
            contracts = []
            for row in (item for item in tariffs if item["metric"] == tariff_metric):
                schedule_end = row["valid_to"] or (today + timedelta(days=370)).isoformat()
                planned_payments = scheduled_payment_events(db, row, row["valid_from"], schedule_end)
                payment_events = []
                for event in db.execute(
                    """SELECT * FROM energy_payment_events WHERE tariff_id=?
                       ORDER BY booked_on,id""",
                    (row["id"],),
                ):
                    payment_events.append({
                        "id": event["external_id"] or f"energylab:event:{event['id']}",
                        "externalId": event["external_id"],
                        "segmentId": event["segment_id"] or segment_id,
                        "contractId": str(row["id"]),
                        "plannedDate": event["due_on"],
                        "bookingDate": event["booked_on"],
                        "amountCents": int(round(float(event["amount"]) * 100)),
                        "currency": "EUR", "status": event["status"],
                        "eventType": event["event_type"],
                        "confirmed": bool(event["confirmed"]),
                        "source": event["source"],
                    })
                contracts.append({
                    "id": row["id"], "provider": row["provider"],
                    "validFrom": row["valid_from"], "validTo": row["valid_to"],
                    "unitPrice": row["price_per_kwh"],
                    "unitPriceLabel": (
                        f"{row['price_per_kwh'] * 100:.2f} Cent/kWh"
                        if tariff_metric in ("grid_import", "gas")
                        else f"{row['price_per_kwh']:.4f} €/m³"
                    ).replace(".", ","),
                    "baseFeeMonthly": row["base_fee_monthly"],
                    "advanceMonthly": row["advance_monthly"],
                    "paymentAmount": row["advance_monthly"],
                    "paymentRecurrence": payment_recurrence(row["payment_interval_months"]),
                    "paymentIntervalMonths": row["payment_interval_months"],
                    "paymentDay": row["payment_day"],
                    "firstPaymentDate": row["first_payment_date"],
                    "paymentAccountName": row["payment_account"],
                    "advanceChanges": [{
                        "validFrom": change["valid_from"],
                        "advanceMonthly": change["advance_monthly"],
                    } for change in db.execute(
                        "SELECT valid_from,advance_monthly FROM energy_advance_changes WHERE tariff_id=? ORDER BY valid_from,id",
                        (row["id"],),
                    )],
                    "plannedPayments": planned_payments,
                    "paymentEvents": payment_events,
                    "active": row["valid_from"] <= today_text and (
                        not row["valid_to"] or row["valid_to"] >= today_text
                    ),
                })
            forecast = settlement_forecast(db, tariff_metric) if metric != "pv_self" else None
            projected = forecast.get("projected") if forecast else None
            invalid = db.execute(
                "SELECT COUNT(*) count FROM energy_readings WHERE metric=? AND is_valid=0",
                (reading_metric,),
            ).fetchone()["count"]
            segments.append({
                "id": segment_id, "metric": metric, "label": label, "unit": unit,
                "latest": history[0] if history else None,
                "consumption": {
                    "month": allocated_usage(db, reading_metric, month_start, today_text, tariff_metric)[0],
                    "year": allocated_usage(db, reading_metric, year_start, today_text, tariff_metric)[0],
                    "total": allocated_usage(db, reading_metric, None, today_text, tariff_metric)[0],
                },
                "finances": {
                    "month": finance_values(month_finances.get(tariff_metric)) if metric != "pv_self" else None,
                    "year": finance_values(year_finances.get(tariff_metric)) if metric != "pv_self" else None,
                },
                "forecast": ({
                    "through": forecast.get("contract_end"),
                    "cost": projected.get("cost"),
                    "advance": projected.get("advance"),
                    "balance": projected.get("balance"),
                } if projected else None),
                "savings": {
                    "month": pv_savings(db, month_start, today_text) if metric == "pv_self" else None,
                    "year": pv_savings(db, year_start, today_text) if metric == "pv_self" else None,
                },
                "contracts": contracts, "history": history,
                "invalidCount": int(invalid or 0),
            })
        sync_status = finanzlab_sync_status(db)
    return {
        "version": "3", "source": {"app": APP_NAME, "version": APP_VERSION},
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "period": {"monthStart": month_start, "yearStart": year_start},
        "segments": segments, "integrationStatus": {"financeLab": sync_status},
    }


def save_manual_energy_reading(metric, read_on, total_value):
    if metric not in ("grid_import", "gas", "water"):
        raise ValueError("Diese Messgröße unterstützt keine manuelle Zählerstandseingabe.")
    read_on = parse_iso_date(read_on)
    total_value = parse_num(total_value)
    label = METRICS[metric]["label"]
    unit = METRICS[metric]["unit"]
    if not read_on or read_on > date.today().isoformat() or total_value is None or total_value < 0:
        raise ValueError(f"Bitte ein gültiges Datum bis heute und einen nichtnegativen {label}-Zählerstand eingeben.")
    with connect() as db:
        db.execute(
            """INSERT INTO energy_readings(metric,read_on,total_value,delta_value,unit,entity_id,source,is_valid,invalid_reason)
               VALUES(?,?,?,NULL,?,'manual','manual',1,'')
               ON CONFLICT(metric,read_on) DO UPDATE SET
                 total_value=excluded.total_value,source='manual',entity_id='manual',
                 unit=excluded.unit,is_valid=1,invalid_reason='',created_at=CURRENT_TIMESTAMP""",
            (metric, read_on, total_value, unit),
        )
        rows = db.execute(
            "SELECT id,read_on,total_value FROM energy_readings WHERE metric=? AND is_valid=1 ORDER BY read_on,id",
            (metric,),
        ).fetchall()
        previous = None
        for row in rows:
            current = row["total_value"]
            if previous is not None and current < previous:
                raise ValueError(f"Der {label}-Zählerstand liegt nicht zwischen dem vorherigen und dem nachfolgenden Stand.")
            delta = None if previous is None else current - previous
            db.execute("UPDATE energy_readings SET delta_value=? WHERE id=?", (delta, row["id"]))
            previous = current
        if metric in MAX_DAILY_CHANGE:
            sanitize_cumulative_readings(db, metric)
            inserted = db.execute(
                "SELECT is_valid,invalid_reason FROM energy_readings WHERE metric=? AND read_on=?",
                (metric, read_on),
            ).fetchone()
            if not inserted["is_valid"]:
                raise ValueError(f"Der eingegebene {label}-Stand wurde als unplausibel erkannt: {inserted['invalid_reason']}.")
    return read_on, total_value


def save_manual_water_reading(read_on, total_value):
    """Compatibility wrapper for existing integrations and tests."""
    return save_manual_energy_reading("water", read_on, total_value)


def tariff_price_to_eur(metric: str, value):
    """Normalize energy UI prices; water and wastewater use euros per m³."""
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
    factor = 1.0 if metric in ("grid_import", "water", "wastewater") else parse_num(form.get("kwh_per_unit"))
    base_fee = parse_num(form.get("base_fee_monthly"))
    advance = parse_num(form.get("advance_monthly"))
    payment_account = str(form.get("payment_account", "")).strip()
    try:
        payment_day = int(str(form.get("payment_day", "1")).strip())
    except ValueError:
        payment_day = 0
    try:
        payment_interval_months = int(str(form.get("payment_interval_months", "1")).strip())
    except ValueError:
        payment_interval_months = 0
    first_payment_raw = str(form.get("first_payment_date", "")).strip()
    first_payment_date = parse_iso_date(first_payment_raw) if first_payment_raw else None
    if metric not in ("grid_import", "gas", "water", "wastewater") or not provider or len(provider) > 100 or not valid_from or (valid_to_raw and not valid_to):
        raise ValueError("Bitte Anbieter, Messgröße und gültigen Datumsbereich prüfen.")
    if valid_to and valid_to < valid_from:
        raise ValueError("Das Enddatum darf nicht vor dem Startdatum liegen.")
    if first_payment_raw and not first_payment_date:
        raise ValueError("Bitte für die erste Zahlung ein gültiges Datum eingeben.")
    if first_payment_date and (first_payment_date < valid_from or (valid_to and first_payment_date > valid_to)):
        raise ValueError("Die erste Zahlung muss innerhalb der Vertragslaufzeit liegen.")
    if price is None or price < 0 or factor is None or factor <= 0 or base_fee is None or base_fee < 0 or advance is None or advance < 0:
            raise ValueError("Arbeitspreis, Umrechnungsfaktor, Grundpreis und Zahlungsbetrag müssen gültige Zahlen sein.")
    if not 1 <= payment_day <= 31 or payment_interval_months not in (1, 3, 6, 12) or len(payment_account) > 100:
        raise ValueError("Bitte Zahlungstag, Zahlungsrhythmus und Kontonamen prüfen.")
    return {
        "metric": metric,
        "provider": provider,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "price": price,
        "factor": factor,
        "base_fee": base_fee,
        "advance": advance,
        "payment_day": payment_day,
        "payment_interval_months": payment_interval_months,
        "first_payment_date": first_payment_date,
        "payment_account": payment_account,
    }


def validate_tariff_timeline_neighbors(
    db, metric: str, valid_from: str, valid_to: str | None, exclude_id=None
) -> None:
    """Reject silent holes between neighbouring contracts for one meter stream."""
    exclusion = " AND id<>?" if exclude_id is not None else ""
    previous_args = [metric, valid_from]
    next_args = [metric, valid_from]
    if exclude_id is not None:
        previous_args.append(exclude_id)
        next_args.append(exclude_id)
    previous = db.execute(
        f"""SELECT id,valid_from,valid_to FROM energy_tariffs
            WHERE metric=? AND valid_from<?{exclusion}
            ORDER BY valid_from DESC,id DESC LIMIT 1""",
        previous_args,
    ).fetchone()
    following = db.execute(
        f"""SELECT id,valid_from,valid_to FROM energy_tariffs
            WHERE metric=? AND valid_from>?{exclusion}
            ORDER BY valid_from,id LIMIT 1""",
        next_args,
    ).fetchone()

    start_day = date.fromisoformat(valid_from)
    if previous and previous["valid_to"]:
        expected_start = date.fromisoformat(previous["valid_to"]) + timedelta(days=1)
        if start_day != expected_start:
            raise ValueError(
                "Zwischen den Verträgen würde eine unbeabsichtigte Lücke entstehen. "
                f"Der neue Zeitraum muss lückenlos am {expected_start.strftime('%d.%m.%Y')} beginnen."
            )
    if following:
        expected_end = date.fromisoformat(following["valid_from"]) - timedelta(days=1)
        if not valid_to or date.fromisoformat(valid_to) != expected_end:
            raise ValueError(
                "Zwischen den Verträgen würde eine unbeabsichtigte Lücke entstehen. "
                f"Der Zeitraum muss lückenlos am {expected_end.strftime('%d.%m.%Y')} enden."
            )


def vehicle_fluids(db, vehicle_id: int):
    return [r["fluid"] for r in db.execute("SELECT fluid FROM vehicle_fluids WHERE vehicle_id=? ORDER BY fluid", (vehicle_id,))]


def consumption_summary(db, vehicle_id: int, fluid="Diesel"):
    rows = db.execute(
        "SELECT * FROM fuelings WHERE vehicle_id=? AND fluid=? ORDER BY odometer,id",
        (vehicle_id, fluid),
    ).fetchall()
    previous_full = None
    liters_since_full = 0.0
    cost_since_full = 0.0
    cycles = []
    row_values = {}
    row_costs = {}
    for row in rows:
        if previous_full is None:
            if row["fill_type"] in ("first", "full"):
                previous_full = row["odometer"]
                liters_since_full = 0.0
                cost_since_full = 0.0
            continue
        liters_since_full += row["liters"]
        cost_since_full += row["total_price"]
        if row["fill_type"] == "full":
            distance = row["odometer"] - previous_full
            if distance > 0:
                value = liters_since_full / distance * 100
                cost_per_km = cost_since_full / distance
                cycles.append((distance, liters_since_full, value, cost_since_full, cost_per_km))
                row_values[row["id"]] = value
                row_costs[row["id"]] = cost_per_km
            previous_full = row["odometer"]
            liters_since_full = 0.0
            cost_since_full = 0.0
    total_distance = sum(c[0] for c in cycles)
    total_liters = sum(c[1] for c in cycles)
    total_cost = sum(c[3] for c in cycles)
    average = total_liters / total_distance * 100 if total_distance else None
    cost_per_km = total_cost / total_distance if total_distance else None
    return {"average": average, "cost_per_km": cost_per_km, "distance": total_distance, "liters": total_liters, "cost": total_cost, "cycles": cycles, "rows": row_values, "row_costs": row_costs}


def vehicle_cost_summary(db, vehicle_id: int):
    """Calculate lifetime vehicle costs over all odometer-backed driven kilometres."""
    fuel = db.execute(
        """SELECT COALESCE(SUM(total_price),0) fuel_cost,
                  MIN(odometer) first_odometer,MAX(odometer) last_odometer,
                  MIN(fueled_on) first_date,MAX(fueled_on) last_date
           FROM fuelings WHERE vehicle_id=?""",
        (vehicle_id,),
    ).fetchone()
    extra_cost = db.execute(
        "SELECT COALESCE(SUM(amount),0) cost FROM vehicle_expenses WHERE vehicle_id=?",
        (vehicle_id,),
    ).fetchone()["cost"]
    first_odometer = fuel["first_odometer"]
    last_odometer = fuel["last_odometer"]
    distance = last_odometer - first_odometer if first_odometer is not None and last_odometer is not None else 0.0
    fuel_cost = float(fuel["fuel_cost"] or 0.0)
    extra_cost = float(extra_cost or 0.0)
    total_cost = fuel_cost + extra_cost
    return {
        "distance": distance,
        "fuel_cost": fuel_cost,
        "extra_cost": extra_cost,
        "total_cost": total_cost,
        "cost_per_km": total_cost / distance if distance > 0 else None,
        "first_odometer": first_odometer,
        "last_odometer": last_odometer,
        "first_date": fuel["first_date"],
        "last_date": fuel["last_date"],
    }


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


def finanzlab_config(db=None):
    owns_connection = db is None
    db = db or connect()
    try:
        return {
            "financeLabBaseUrl": get_meta("finanzlab_base_url", FINANZLAB_URL, db).strip().rstrip("/"),
            "householdId": get_meta("finanzlab_household_id", FINANZLAB_HOUSEHOLD_ID, db).strip(),
            "accessToken": get_meta("finanzlab_access_token", FINANZLAB_TOKEN, db).strip(),
        }
    finally:
        if owns_connection:
            db.close()


def finanzlab_ready(db=None) -> bool:
    config = finanzlab_config(db)
    return bool(config["financeLabBaseUrl"] and config["householdId"])


def finanzlab_sync_status(db=None):
    owns_connection = db is None
    db = db or connect()
    try:
        config = finanzlab_config(db)
        row = db.execute(
            """SELECT * FROM integration_sync_log WHERE integration='finanzlab'
               ORDER BY synced_at DESC,id DESC LIMIT 1"""
        ).fetchone()
        unresolved = db.execute(
            """SELECT COUNT(*) count FROM energy_payment_events
               WHERE tariff_id IS NULL OR confirmed=0 OR status IN ('pending','review')"""
        ).fetchone()["count"]
        return {
            "configured": bool(config["financeLabBaseUrl"] and config["householdId"]),
            "baseUrl": config["financeLabBaseUrl"],
            "householdId": config["householdId"],
            "lastSyncAt": row["synced_at"] if row else None,
            "status": row["status"] if row else "never",
            "message": row["message"] if row else "Noch kein Zahlungsabgleich durchgeführt.",
            "importedCount": int(row["imported_count"] or 0) if row else 0,
            "unresolvedCount": int(unresolved or 0),
        }
    finally:
        if owns_connection:
            db.close()


def _bool_value(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "ja", "confirmed")


def _payment_event_type(value: str) -> str:
    raw = str(value or "regular_payment").strip().lower().replace("-", "_")
    aliases = {
        "payment": "regular_payment", "paid": "regular_payment", "installment": "regular_payment",
        "advance": "regular_payment", "one_off": "one_off_payment", "extra_payment": "one_off_payment",
        "suspended": "suspension", "pause": "suspension", "reversal": "chargeback",
        "reversed": "chargeback", "refund": "credit_payout", "credit": "credit_payout",
        "adjustment": "correction",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in PAYMENT_EVENT_LABELS else "regular_payment"


def _signed_payment_amount(event_type: str, amount) -> float:
    value = float(amount or 0.0)
    if event_type in ("regular_payment", "one_off_payment"):
        return abs(value)
    if event_type in ("chargeback", "credit_payout"):
        return -abs(value)
    if event_type == "suspension":
        return 0.0
    return value


def sync_finanzlab():
    """Pull confirmed payment matches from FinanzLab 1.3, idempotently."""
    config = finanzlab_config()
    if not config["financeLabBaseUrl"] or not config["householdId"]:
        raise RuntimeError("FinanzLab-URL oder Haushalts-ID ist noch nicht eingerichtet.")
    with connect() as db:
        earliest = db.execute("SELECT MIN(valid_from) first_on FROM energy_tariffs").fetchone()["first_on"]
    params = {"household_id": config["householdId"]}
    if earliest:
        params["since"] = earliest
    url = config["financeLabBaseUrl"] + "/api/integrations/energylab/actual-payments?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": f"EnergieLab/{APP_VERSION}"}
    if config["accessToken"]:
        headers["Authorization"] = f"Bearer {config['accessToken']}"
    try:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError(f"FinanzLab antwortet mit Status {response.status}.")
                raw = response.read(MAX_UPLOAD + 1)
        except urllib.error.HTTPError as exc:
            response_text = ""
            try:
                response_text = exc.read(MAX_UPLOAD).decode("utf-8", errors="replace").strip()
                response_payload = json.loads(response_text)
                response_text = str(response_payload.get("error") or response_text).strip()
            except (ValueError, TypeError):
                pass
            if response_text == "EnergyLab-Verbindung nicht gefunden.":
                response_text += (
                    " Bitte in FinanzLab unter Einstellungen zuerst die EnergyLab-Verbindung "
                    "für genau diesen Haushalt speichern."
                )
            detail = f": {response_text}" if response_text else ""
            raise RuntimeError(f"FinanzLab lehnt den Zahlungsabgleich ab (HTTP {exc.code}){detail}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise RuntimeError(
                f"FinanzLab ist unter {config['financeLabBaseUrl']} nicht erreichbar: {reason}"
            ) from exc
        if len(raw) > MAX_UPLOAD:
            raise RuntimeError("Die Zahlungsantwort von FinanzLab ist unerwartet groß.")
        payload = json.loads(raw.decode("utf-8"))
        payments = payload.get("payments") or []
        if not isinstance(payments, list):
            raise ValueError("FinanzLab hat keine gültige Zahlungsliste geliefert.")
        imported = unresolved = ignored = 0
        with connect() as db:
            for item in payments:
                if not isinstance(item, dict) or not str(item.get("id") or "").strip():
                    ignored += 1
                    continue
                external_id = str(item["id"]).strip()[:240]
                contract_id = str(item.get("contractId") or item.get("contract_id") or "").strip()
                tariff = None
                if contract_id.isdigit():
                    tariff = db.execute("SELECT id,metric FROM energy_tariffs WHERE id=?", (int(contract_id),)).fetchone()
                segment_id = str(item.get("segmentId") or item.get("segment_id") or "").strip()
                if tariff and segment_id and SEGMENT_BY_METRIC.get(tariff["metric"]) != segment_id:
                    tariff = None
                due_value = (item.get("plannedDate") or item.get("planned_date") or
                             item.get("occurrenceDate") or item.get("occurrence_date"))
                booked_value = (item.get("bookingDate") or item.get("booking_date") or
                                item.get("occurrenceDate") or item.get("occurrence_date") or due_value)
                if not booked_value:
                    ignored += 1
                    continue
                try:
                    booked_on = parse_iso_date(booked_value)
                    due_on = parse_iso_date(due_value or booked_value)
                except (TypeError, ValueError):
                    ignored += 1
                    continue
                event_type = _payment_event_type(item.get("eventType") or item.get("event_type"))
                raw_cents = item.get("actualAmountCents")
                if raw_cents is None:
                    raw_cents = item.get("actual_amount_cents")
                if raw_cents is None:
                    raw_cents = item.get("amountCents", item.get("amount_cents",
                        item.get("plannedAmountCents", item.get("planned_amount_cents", 0))))
                try:
                    amount = _signed_payment_amount(event_type, float(raw_cents) / 100.0)
                except (TypeError, ValueError):
                    ignored += 1
                    continue
                status = str(item.get("status") or "paid").strip().lower()[:24]
                confirmed = _bool_value(item.get("confirmed"), default=True)
                try:
                    confidence = float(item["confidence"]) if item.get("confidence") is not None else None
                except (TypeError, ValueError):
                    confidence = None
                db.execute(
                    """INSERT INTO energy_payment_events(
                           external_id,tariff_id,segment_id,due_on,booked_on,amount,event_type,status,
                           source,match_method,confidence,confirmed,note,raw_payload)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(external_id) WHERE external_id IS NOT NULL DO UPDATE SET
                           tariff_id=excluded.tariff_id,segment_id=excluded.segment_id,
                           due_on=excluded.due_on,booked_on=excluded.booked_on,amount=excluded.amount,
                           event_type=excluded.event_type,status=excluded.status,
                           match_method=excluded.match_method,confidence=excluded.confidence,
                           confirmed=excluded.confirmed,note=excluded.note,
                           raw_payload=excluded.raw_payload,updated_at=CURRENT_TIMESTAMP""",
                    (
                        external_id, tariff["id"] if tariff else None, segment_id, due_on, booked_on,
                        amount, event_type, status, "finanzlab", str(item.get("matchMethod") or item.get("match_method") or "")[:80],
                        confidence, 1 if confirmed else 0, "Von FinanzLab abgeglichen",
                        json.dumps(item, ensure_ascii=False, sort_keys=True),
                    ),
                )
                imported += 1
                if not tariff or not confirmed or status in ("pending", "review"):
                    unresolved += 1
            set_meta("actual_payments_enabled", "1", db)
            message = f"{imported} Zahlungsereignisse abgeglichen"
            if unresolved:
                message += f", {unresolved} noch zu prüfen"
            if ignored:
                message += f", {ignored} ungültige Einträge übersprungen"
            db.execute(
                """INSERT INTO integration_sync_log(
                       integration,direction,status,message,imported_count,unresolved_count,details_json)
                   VALUES('finanzlab','pull',?,?,?,?,?)""",
                (
                    "warning" if unresolved or ignored else "ok", message, imported, unresolved,
                    json.dumps({"source": payload.get("source"), "ignored": ignored}, ensure_ascii=False),
                ),
            )
        return imported, [message]
    except Exception as exc:
        with connect() as db:
            db.execute(
                """INSERT INTO integration_sync_log(integration,direction,status,message)
                   VALUES('finanzlab','pull','error',?)""",
                (str(exc)[:500],),
            )
        raise


def sync_all_sources():
    messages = []
    failures = []
    if ha_ready():
        try:
            messages.extend(sync_home_assistant())
        except Exception as exc:
            failures.append(f"Home Assistant: {exc}")
    if finanzlab_ready():
        try:
            _count, finance_messages = sync_finanzlab()
            messages.extend(finance_messages)
        except Exception as exc:
            failures.append(f"FinanzLab: {exc}")
    if not ha_ready() and not finanzlab_ready():
        raise RuntimeError("Weder Home Assistant noch FinanzLab ist eingerichtet.")
    messages.extend(failures)
    return messages


def missing_supplier_switch_readings(db, tariff, start_on: str, end_on: str):
    """Return exact meter boundaries missing between adjacent tariff records."""
    reading_metric = reading_metric_for(tariff["metric"])
    tariff_start = date.fromisoformat(tariff["valid_from"])
    tariff_end = date.fromisoformat(tariff["valid_to"]) if tariff["valid_to"] else None

    required = []
    previous_day = tariff_start - timedelta(days=1)
    previous_tariff = db.execute(
        """SELECT id FROM energy_tariffs
           WHERE id<>? AND metric=? AND valid_to=? LIMIT 1""",
        (tariff["id"], tariff["metric"], previous_day.isoformat()),
    ).fetchone()
    if previous_tariff and start_on == tariff["valid_from"]:
        required.append(previous_day.isoformat())

    if tariff_end and end_on == tariff["valid_to"]:
        next_day = tariff_end + timedelta(days=1)
        next_tariff = db.execute(
            """SELECT id FROM energy_tariffs
               WHERE id<>? AND metric=? AND valid_from=? LIMIT 1""",
            (tariff["id"], tariff["metric"], next_day.isoformat()),
        ).fetchone()
        if next_tariff:
            required.append(tariff_end.isoformat())

    missing = []
    for boundary_on in sorted(set(required)):
        reading = db.execute(
            """SELECT 1 FROM energy_readings
               WHERE metric=? AND read_on=? AND is_valid=1 LIMIT 1""",
            (reading_metric, boundary_on),
        ).fetchone()
        if not reading:
            missing.append(boundary_on)
    return missing


def require_supplier_switch_readings(db, tariff, start_on: str, end_on: str) -> None:
    """Require an exact shared meter boundary before freezing adjacent tariffs."""
    missing = missing_supplier_switch_readings(db, tariff, start_on, end_on)
    if missing:
        formatted = ", ".join(
            date.fromisoformat(value).strftime("%d.%m.%Y") for value in missing
        )
        raise ValueError(
            "Für die Schlussrechnung am Lieferantenwechsel fehlt ein exakter "
            f"gültiger Zählerstand ({formatted}). Bitte den Wechselstand zuerst erfassen."
        )


def settlement_snapshot_payload(db, tariff_id: int, start_on: str, end_on: str):
    """Build the exact, self-contained data recorded by a final settlement."""
    start_on = parse_iso_date(start_on)
    end_on = parse_iso_date(end_on)
    tariff = db.execute("SELECT * FROM energy_tariffs WHERE id=?", (tariff_id,)).fetchone()
    if not tariff or not start_on or not end_on or end_on < start_on:
        raise ValueError("Tarif oder Abrechnungszeitraum ist ungültig.")
    if start_on < tariff["valid_from"] or (tariff["valid_to"] and end_on > tariff["valid_to"]):
        raise ValueError("Der Abrechnungszeitraum muss vollständig innerhalb des Vertrags liegen.")
    require_supplier_switch_readings(db, tariff, start_on, end_on)
    reading_metric = reading_metric_for(tariff["metric"])
    consumption, variable = allocated_usage(db, reading_metric, start_on, end_on, tariff["metric"])
    base_fee = prorated_months(start_on, end_on) * float(tariff["base_fee_monthly"])
    planned_events = scheduled_payment_events(
        db,
        tariff,
        start_on,
        end_on,
    )
    planned = sum(
        float(event["amount"])
        for event in planned_events
    )

    # Für die Schlussabrechnung gilt der hinterlegte Zahlungsplan.
    # Eine zusätzliche Bestätigung über FinanzLab ist nicht nötig.
    values = settlement_values(
        variable,
        base_fee,
        planned,
    )

    changes = [dict(row) for row in db.execute(
        """SELECT valid_from,advance_monthly,created_at FROM energy_advance_changes
           WHERE tariff_id=? AND valid_from<=? ORDER BY valid_from,id""",
        (tariff_id, end_on),
    )]
    return {
        "schemaVersion": "2", "source": {"app": APP_NAME, "version": APP_VERSION},
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "contract": {
            "id": str(tariff["id"]), "segmentId": SEGMENT_BY_METRIC.get(tariff["metric"], tariff["metric"]),
            "metric": tariff["metric"], "provider": tariff["provider"],
            "validFrom": tariff["valid_from"], "validTo": tariff["valid_to"],
            "unitPrice": tariff["price_per_kwh"], "kwhPerUnit": tariff["kwh_per_unit"],
            "baseFeeMonthly": tariff["base_fee_monthly"],
            "originalPaymentAmount": tariff["advance_monthly"],
            "paymentDay": tariff["payment_day"],
            "paymentIntervalMonths": tariff["payment_interval_months"],
            "firstPaymentDate": tariff["first_payment_date"],
            "paymentAccountName": tariff["payment_account"],
            "advanceChanges": changes,
        },
        "period": {"from": start_on, "to": end_on},
        "consumption": {"amount": consumption, "unit": METRICS[reading_metric]["unit"]},
        "plannedPayments": planned_events,
        "actualPayments": [],
        "settlement": {
            "variableCost": values["variable"], "baseFee": values["base_fee"],
            "totalCost": values["cost"], "plannedPayments": values["planned_advance"],
            "actualPayments": None, "paymentBasis": "planned",
            "balance": values["balance"],
        },
    }


def create_settlement_snapshot(tariff_id: int, start_on: str, end_on: str, title=""):
    with connect() as db:
        payload = settlement_snapshot_payload(db, tariff_id, start_on, end_on)
        revision = db.execute(
            """SELECT COALESCE(MAX(revision),0)+1 revision FROM energy_settlement_snapshots
               WHERE tariff_id=? AND period_from=? AND period_to=?""",
            (tariff_id, payload["period"]["from"], payload["period"]["to"]),
        ).fetchone()["revision"]
        payload["revision"] = revision
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        cur = db.execute(
            """INSERT INTO energy_settlement_snapshots(
                   tariff_id,revision,period_from,period_to,title,payment_basis,payload_json,payload_sha256)
               VALUES(?,?,?,?,?,'planned',?,?)""",
            (tariff_id, revision, payload["period"]["from"], payload["period"]["to"], str(title).strip()[:160], raw, digest),
        )
        return cur.lastrowid, revision, digest


def settlement_snapshots_payload(snapshot_id=None):
    with connect() as db:
        if snapshot_id is None:
            rows = db.execute(
                """SELECT s.id,s.tariff_id,s.revision,s.period_from,s.period_to,s.title,
                          s.payment_basis,s.payload_sha256,s.created_at,t.metric,t.provider
                   FROM energy_settlement_snapshots s JOIN energy_tariffs t ON t.id=s.tariff_id
                   ORDER BY s.created_at DESC,s.id DESC"""
            ).fetchall()
            return {"source": {"app": APP_NAME, "version": APP_VERSION}, "snapshots": [dict(row) for row in rows]}
        row = db.execute("SELECT * FROM energy_settlement_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        payload["snapshot"] = {
            "id": row["id"], "revision": row["revision"], "title": row["title"],
            "createdAt": row["created_at"], "sha256": row["payload_sha256"],
            "immutable": True,
        }
        return payload


def sync_due(now: datetime, last_attempt: str | None) -> bool:
    today = now.date().isoformat()
    hour, minute = sync_time()
    return last_attempt != today and (now.hour, now.minute) >= (hour, minute)


def sync_scheduler():
    last_attempt = None
    while True:
        now = datetime.now()
        today = now.date().isoformat()
        if (ha_ready() or finanzlab_ready()) and sync_due(now, last_attempt):
            try:
                sync_all_sources()
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
.settings-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
details.tariff-history{background:linear-gradient(155deg,rgba(31,38,48,.96),rgba(22,27,34,.96));border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow)}
details.tariff-history summary{cursor:pointer;list-style:none;padding:20px;font-size:19px;font-weight:750;display:flex;justify-content:space-between;gap:12px;align-items:center}
details.tariff-history summary::-webkit-details-marker{display:none}
details.tariff-history summary:after{content:"▾";color:var(--blue);transition:transform .18s ease}
details.tariff-history[open] summary:after{transform:rotate(180deg)}
details.tariff-history .details-body{padding:0 20px 20px}
button:disabled{opacity:.45;cursor:not-allowed;filter:none}
.settlement-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.settlement-card h3{margin:0;font-size:18px}.settlement-card .contract{color:var(--muted);font-size:12px;margin:3px 0 16px}
.settlement-block{border-top:1px solid var(--line);padding-top:12px;margin-top:12px}.settlement-block:first-of-type{border-top:0;padding-top:0}
.settlement-block .row{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-top:6px}
.settlement-block .amount{font-size:20px;font-weight:800}.settlement-block .amount.ok{color:var(--green)}.settlement-block .amount.warn{color:#ffb0b0}
.metric-link{display:block;color:var(--text);transition:transform .16s ease,border-color .16s ease}.metric-link:hover{transform:translateY(-2px);border-color:var(--blue);text-decoration:none}
.detail-chart{width:100%;min-width:620px;height:280px;display:block}.chart-grid line{stroke:var(--line);stroke-width:1}.chart-grid text,.axis-label{fill:var(--muted);font-size:12px}.chart-line polyline{fill:none;stroke:var(--cyan);stroke-width:3;stroke-linecap:round;stroke-linejoin:round}.invalid-point{fill:var(--red);stroke:#ffd4d4;stroke-width:2}.chart-legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px}.legend-line:before{content:"";display:inline-block;width:24px;border-top:3px solid var(--cyan);vertical-align:middle;margin-right:7px}.legend-invalid:before{content:"";display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--red);margin-right:7px}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.summary-box{background:#111820;border:1px solid var(--line);border-radius:13px;padding:14px}.summary-box .value{font-size:22px;font-weight:800;margin-top:5px}.summary-box .value.ok{color:var(--green)}.summary-box .value.warn{color:#ffb0b0}.reading-invalid td{background:rgba(255,107,107,.06);color:#ffc8cc}
.month-nav{display:grid;grid-template-columns:46px minmax(180px,280px) 46px;align-items:center;justify-content:center;gap:12px;margin:-8px 0 22px}.month-nav .month-title{text-align:center}.month-nav .month-title strong{display:block;font-size:19px}.month-arrow{width:46px;height:42px;border-radius:11px;display:grid;place-items:center;background:var(--panel2);border:1px solid var(--line);color:var(--text);font-size:25px;line-height:1}.month-arrow:hover{border-color:var(--blue);text-decoration:none}.month-arrow.disabled{opacity:.35;cursor:not-allowed}.current-month-link{grid-column:1/-1;justify-self:center;margin-top:2px}.current-month-link.disabled{opacity:.45;cursor:not-allowed}
.comparison-delta{font-weight:800}.comparison-delta.more{color:var(--orange)}.comparison-delta.less{color:var(--green)}.comparison-delta.same{color:var(--muted)}.comparison-percent{display:block;font-size:12px;font-weight:600;margin-top:2px}
.main{min-width:0;overflow-x:hidden}.card,.section,.topbar>*,.detail-view{min-width:0}.table-wrap{max-width:100%}
.detail-primary-tabs{display:flex;gap:7px;max-width:100%;overflow-x:auto;padding:5px;margin-bottom:20px;scrollbar-width:thin}
.detail-primary-tabs a{display:flex;align-items:center;gap:7px;flex:1 0 max-content;justify-content:center;min-height:42px;padding:9px 14px}
.detail-primary-tabs a.active{box-shadow:0 5px 18px rgba(61,169,252,.18)}
.detail-toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:18px}.detail-toolbar .tabs{max-width:100%;overflow-x:auto}
.detail-intro{margin:0 0 16px}.detail-intro h2{margin:0 0 4px;font-size:20px}.detail-intro p{margin:0}
.plan-payment{display:grid;gap:2px}.plan-payment strong{font-size:15px}.plan-payment .muted{font-size:12px}
.payment-plan-table th,.payment-plan-table td{vertical-align:top}.payment-plan-table td:nth-child(1),.payment-plan-table td:nth-child(2){white-space:normal;min-width:132px}
.payment-plan-table td:nth-child(3){min-width:105px}.payment-plan-table td:last-child{white-space:normal;min-width:120px}
.source-detail{display:grid;gap:2px;white-space:normal;min-width:155px}.source-detail .muted{font-size:12px;overflow-wrap:anywhere}
.compact-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.forecast-note{margin:14px 0 0}.forecast-note strong{display:block;margin-bottom:3px}.summary-box .meta{font-size:12px;color:var(--muted);margin-top:7px}
.supplier-payment-section{padding:0;overflow:hidden}.supplier-header{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;padding:20px;border-bottom:1px solid var(--line)}.supplier-header h2{margin:2px 0 3px;font-size:22px}.supplier-kicker{color:var(--cyan);font-size:11px;font-weight:800;letter-spacing:.09em;text-transform:uppercase}.supplier-badges{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}.supplier-summary{padding:18px 20px}.supplier-result{border-color:#3a6176;background:linear-gradient(145deg,#132530,#111820)}.supplier-note{margin:0 20px 18px}.supplier-table{border-top:1px solid var(--line)}.supplier-table table{min-width:1040px}.supplier-table th:first-child,.supplier-table td:first-child{padding-left:20px}.supplier-table th:last-child,.supplier-table td:last-child{padding-right:20px}
@media(max-width:1000px){.settlement-grid,.settings-grid{grid-template-columns:1fr}}
@media(max-width:760px){.detail-primary-tabs{margin-inline:-4px}.detail-primary-tabs a{flex:0 0 auto}.detail-toolbar{align-items:flex-start}.summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.summary-box{padding:12px}.summary-box .value{font-size:18px}.supplier-header{padding:16px;flex-direction:column}.supplier-badges{justify-content:flex-start}.supplier-summary{padding:14px 16px}.supplier-note{margin-inline:16px}}
@media(max-width:430px){.summary-grid{grid-template-columns:1fr}.detail-primary-tabs a{padding-inline:11px}}
"""


def page(title, body, active="", notice="", warning=False):
    items = [
        ("/", "Dashboard", "⌂"),
        ("/energy/grid_import", "Strom", "⚡"),
        ("/energy/gas", "Gas", "🔥"),
        ("/energy/water", "Wasser", "💧"),
        ("/energy/wastewater", "Abwasser", "🚰"),
        ("/compare", "Vergleich", "⇄"),
        ("/settings", "Einstellungen", "⚙"),
        ("/vehicles", "Fahrzeuge", "🚐"),
        ("/fuelings", "Tankungen", "⛽"),
        ("/vehicle-costs", "Fahrzeugkosten", "€"),
        ("/contract-history", "Vertragshistorie", "🗂"),
        ("/support", "Unterstützung", "♥"),
    ]
    nav = "".join(f'<a class="{"active" if active == href else ""}" href="{href}"><span>{icon}</span> {label}</a>' for href, label, icon in items)
    notice_html = f'<div class="notice {"warn" if warning else ""}">{esc(notice)}</div>' if notice else ""
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><meta name="theme-color" content="#111827"><link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"><link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png"><link rel="manifest" href="/site.webmanifest"><title>{esc(title)} · {APP_NAME}</title><style>{STYLE}</style></head><body><div class="layout"><aside class="sidebar"><div class="brand"><span class="logo">EL</span><span>{APP_NAME}</span></div><nav>{nav}</nav><div class="side-bottom">Lokal auf deinem Homelab</div></aside><main class="main">{notice_html}{body}<footer class="footer">by Lrd.Tiberius · EnergieLab {APP_VERSION}</footer></main></div></body></html>"""


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


def detail_chart(rows, field: str, unit: str, show_invalid=False):
    """Render a dependency-free time-series chart and expose invalid meter points."""
    prepared = []
    for row in rows:
        value = row[field]
        if value is None:
            prepared.append((date.fromisoformat(row["read_on"]), None, bool(row["is_valid"])))
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
        prepared.append((date.fromisoformat(row["read_on"]), number if math.isfinite(number) else None, bool(row["is_valid"])))
    plotted = [(day, value, valid) for day, value, valid in prepared if value is not None and (valid or show_invalid)]
    valid_values = [value for _, value, valid in plotted if valid]
    scale_values = [value for _, value, _ in plotted] if show_invalid else valid_values
    if len(valid_values) < 2 or not scale_values:
        return '<div class="empty">Für dieses Diagramm werden mindestens zwei gültige Werte benötigt.</div>'

    first_day = min(day for day, _, _ in plotted)
    last_day = max(day for day, _, _ in plotted)
    day_span = max(1, (last_day - first_day).days)
    low, high = min(scale_values), max(scale_values)
    padding = (high - low) * 0.08 or max(abs(high) * 0.02, 1.0)
    low -= padding
    high += padding
    value_span = high - low or 1.0
    left, right, top, bottom = 72.0, 980.0, 18.0, 222.0

    def point(day, value):
        x = left + ((day - first_day).days / day_span) * (right - left)
        y = bottom - ((value - low) / value_span) * (bottom - top)
        return x, y

    grid = []
    for idx in range(5):
        ratio = idx / 4
        y = top + ratio * (bottom - top)
        value = high - ratio * value_span
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}"/><text x="{left-10}" y="{y+4:.1f}" text-anchor="end">{fmt_num(value)}</text>')

    segments = []
    current = []
    invalid_points = []
    for day, value, valid in prepared:
        if value is None or not valid:
            if current:
                segments.append(current)
                current = []
            if show_invalid and value is not None:
                invalid_points.append(point(day, value))
            continue
        current.append(point(day, value))
    if current:
        segments.append(current)
    lines = "".join(
        '<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in segment) + '"/>'
        for segment in segments if len(segment) >= 2
    )
    dots = "".join(f'<circle class="invalid-point" cx="{x:.1f}" cy="{y:.1f}" r="5"/>' for x, y in invalid_points)
    return f'''<svg class="detail-chart" viewBox="0 0 1000 260" role="img" aria-label="Verlauf in {esc(unit)}"><g class="chart-grid">{"".join(grid)}</g><g class="chart-line">{lines}</g>{dots}<text class="axis-label" x="{left}" y="250">{first_day.strftime("%d.%m.%Y")}</text><text class="axis-label" x="{right}" y="250" text-anchor="end">{last_day.strftime("%d.%m.%Y")}</text><text class="axis-label" x="12" y="14">{esc(unit)}</text></svg>'''


def build_xlsx(sheets):
    """Create a small standards-compliant XLSX using only the Python standard library."""
    def col_name(number):
        name = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            name = chr(65 + remainder) + name
        return name

    def sheet_xml(rows):
        xml_rows = []
        for row_number, row in enumerate(rows, 1):
            cells = []
            for column_number, value in enumerate(row, 1):
                ref = f"{col_name(column_number)}{row_number}"
                if isinstance(value, (int, float)) and math.isfinite(float(value)):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{esc(value)}</t></is></c>')
            xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
        return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' \
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' \
               '<sheetData>' + "".join(xml_rows) + '</sheetData></worksheet>'

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
            "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1)) +
            '</Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
        archive.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' +
            "".join(f'<sheet name="{esc(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1)) +
            '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1)) +
            '</Relationships>')
        for index, (_, rows) in enumerate(sheets, 1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml(rows))
    return output.getvalue()


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
        if not headers or "Cache-Control" not in headers:
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
        static_assets = {
            "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
            "/favicon-32x32.png": ("favicon-32x32.png", "image/png"),
            "/icon-192.png": ("icon-192.png", "image/png"),
            "/icon-512.png": ("icon-512.png", "image/png"),
            "/site.webmanifest": ("site.webmanifest", "application/manifest+json; charset=utf-8"),
        }
        if path in static_assets:
            filename, content_type = static_assets[path]
            self.send_bytes(
                (ASSET_DIR / filename).read_bytes(),
                content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
            return
        if path == "/contract-history":
            self.contract_history_page()
            return

        if path == "/api/health":
            self.send_bytes(json.dumps({"status": "ok", "version": APP_VERSION}).encode(), "application/json")
            return
        if path == "/api/personallab":
            payload = json.dumps(personallab_payload(), ensure_ascii=False).encode("utf-8")
            self.send_bytes(payload, "application/json; charset=utf-8")
            return
        if path == "/api/integrations/finanzlab/status":
            self.send_bytes(json.dumps(finanzlab_sync_status(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        request_path = urllib.parse.urlparse(
            self.path
        ).path

        if (
            request_path.startswith(
                "/api/energy/tariffs/"
            )
            and request_path.endswith(
                "/planned-payments"
            )
        ):
            try:
                tariff_id = int(
                    request_path.split("/")[4]
                )

                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(
                        self.path
                    ).query
                )

                start_on = parse_iso_date(
                    str(
                        query.get(
                            "from",
                            [""],
                        )[0]
                    )
                )

                end_on = parse_iso_date(
                    str(
                        query.get(
                            "to",
                            [""],
                        )[0]
                    )
                )

                with connect() as db:
                    tariff = db.execute(
                        """
                        SELECT *
                        FROM energy_tariffs
                        WHERE id=?
                        """,
                        (tariff_id,),
                    ).fetchone()

                    if (
                        not tariff
                        or not start_on
                        or not end_on
                        or end_on < start_on
                    ):
                        raise ValueError(
                            "Ungültiger Abrechnungszeitraum."
                        )

                    if (
                        start_on
                        < tariff["valid_from"]
                    ):
                        raise ValueError(
                            "Zeitraum beginnt vor dem Vertrag."
                        )

                    if (
                        tariff["valid_to"]
                        and end_on
                        > tariff["valid_to"]
                    ):
                        raise ValueError(
                            "Zeitraum endet nach dem Vertrag."
                        )

                    events = scheduled_payment_events(
                        db,
                        tariff,
                        start_on,
                        end_on,
                    )

                    result = {
                        "amount": round(
                            sum(
                                float(
                                    event["amount"]
                                )
                                for event in events
                            ),
                            2,
                        ),
                        "count": len(events),
                    }

            except Exception as exc:
                result = {
                    "amount": 0.0,
                    "count": 0,
                    "error": str(exc),
                }

            self.send_bytes(
                json.dumps(
                    result,
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return

        if path == "/api/settlements":
            self.send_bytes(json.dumps(settlement_snapshots_payload(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path.startswith("/api/settlements/"):
            try:
                payload = settlement_snapshots_payload(int(path.split("/")[3]))
            except (ValueError, IndexError):
                payload = None
            if payload is None:
                self.send_bytes(b'{"error":"not_found"}', "application/json; charset=utf-8", 404)
            else:
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path in ("/login", "/logout"):
            self.redirect("/")
            return
        notice = query.get("notice", [""])[0]
        if path == "/":
            self.dashboard(query.get("period", ["month"])[0], notice)
        elif path == "/energy":
            self.redirect("/energy/grid_import" + (("?notice=" + urllib.parse.quote(notice)) if notice else ""))
        elif path == "/compare":
            self.compare_page(query)
        elif path == "/settings":
            self.settings_page(notice)
        elif path.startswith("/settlements/"):
            self.settlement_snapshot_page(int(path.split("/")[2]))
        elif path.startswith("/settings/backups/") and path.endswith("/download"):
            self.download_database_backup(urllib.parse.unquote(path.split("/")[3]))
        elif path in {f"/energy/{metric}" for metric in METRICS}:
            requested_view = query.get("view", query.get("tab", ["overview"]))[0]
            detail_view = normalize_energy_detail_view(requested_view)
            default_period = "all" if detail_view in ("payments", "imports") else "year"
            self.energy_detail_page(
                path.split("/")[-1],
                query.get("period", [default_period])[0],
                query.get("month", [""])[0],
                notice,
                detail_view,
            )
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
        elif path == "/vehicle-costs":
            self.vehicle_costs_page(notice)
        elif path == "/import":
            self.import_page(notice)
        elif path == "/support":
            self.support_page()
        elif path == "/export/backup.json":
            self.backup_json()
        elif path == "/export/fuelings.csv":
            self.export_fuelings()
        elif path == "/export/energylab.xlsx":
            self.export_xlsx()
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
            elif path == "/vehicle-costs/save":
                self.save_vehicle_cost(form)
            elif path.startswith("/vehicle-costs/") and path.endswith("/delete"):
                self.delete_vehicle_cost(int(path.split("/")[2]))
            elif path == "/import/preview":
                self.import_preview(form)
            elif path == "/import/commit":
                self.import_commit(form)
            elif path == "/energy/sync":
                messages = sync_home_assistant()
                self.redirect("/energy?notice=" + urllib.parse.quote("; ".join(messages)))
            elif path == "/settings/import-now":
                messages = sync_all_sources()
                notice = "; ".join(messages) or "Keine Sensoren konfiguriert."
                self.redirect("/settings?notice=" + urllib.parse.quote(notice))
            elif path == "/settings/sync-time":
                raw = str(form.get("sync_time", "")).strip()
                try:
                    parsed = datetime.strptime(raw, "%H:%M")
                except ValueError:
                    raise ValueError("Bitte eine gültige Uhrzeit wählen.")
                normalized = f"{parsed.hour:02d}:{parsed.minute:02d}"
                with connect() as db:
                    db.execute("""INSERT INTO meta(key,value) VALUES('sync_time',?)
                                  ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (normalized,))
                self.redirect("/settings?notice=" + urllib.parse.quote(f"Automatischer Import auf {normalized} Uhr eingestellt."))
            elif path == "/settings/finanzlab":
                self.save_finanzlab_settings(form)
            elif path == "/settings/finanzlab-sync":
                try:
                    _count, messages = sync_finanzlab()
                    notice = "; ".join(messages)
                except Exception as exc:
                    notice = f"Fehler beim Zahlungsabgleich: {exc}"
                self.redirect("/settings?notice=" + urllib.parse.quote(notice))
            elif path == "/settings/backups/create":
                backup = create_automatic_backup("manuell")
                if not backup:
                    raise ValueError("Es gibt noch keine Datenbank zum Sichern.")
                with connect() as db:
                    db.execute("INSERT INTO backup_log(filename,reason,status) VALUES(?,?,'ok')", (backup.name, "Manuell"))
                self.redirect("/settings?notice=" + urllib.parse.quote("Lokale Datenbanksicherung wurde erstellt."))
            elif path.startswith("/settings/backups/") and path.endswith("/restore"):
                filename = urllib.parse.unquote(path.split("/")[3])
                restore_database_backup(filename)
                self.redirect("/settings?notice=" + urllib.parse.quote("Sicherung wurde wiederhergestellt; zuvor wurde eine Sicherheitssicherung erstellt."))
            elif path == "/energy/backfill":
                imported, messages = backfill_home_assistant(str(form.get("start_date", "")))
                notice = f"{imported} tägliche Historienwerte übernommen. " + "; ".join(messages)
                self.redirect(f"/energy/{metric}?notice=" + urllib.parse.quote(notice))
            elif path in ("/energy/readings/save", "/energy/water/save"):
                metric = "water" if path == "/energy/water/save" else str(form.get("metric", ""))
                read_on, total = save_manual_energy_reading(metric, form.get("read_on"), form.get("total_value"))
                cfg = METRICS[metric]
                notice = f"{cfg['label']}-Zählerstand {fmt_num(total)} {cfg['unit']} vom {read_on} wurde gespeichert."
                self.redirect("/energy?notice=" + urllib.parse.quote(notice))
            elif path == "/energy/tariffs/save":
                self.save_energy_tariff(form)
            elif path.startswith("/energy/tariffs/") and path.endswith("/advance/save"):
                self.save_advance_change(int(path.split("/")[3]), form)
            elif path.startswith("/energy/tariffs/") and path.endswith("/payments/save"):
                self.save_payment_event(int(path.split("/")[3]), form)
            elif path.startswith("/energy/tariffs/") and "/payments/" in path and path.endswith("/cancel"):
                self.cancel_payment_event(int(path.split("/")[3]), int(path.split("/")[5]))
            elif path.startswith("/energy/tariffs/") and path.endswith("/settlements/finalize"):
                self.finalize_settlement(int(path.split("/")[3]), form)
            elif path.startswith("/energy/tariffs/") and "/advance/" in path and path.endswith("/delete"):
                self.delete_advance_change(int(path.split("/")[3]), int(path.split("/")[5]))
            elif path.startswith("/energy/tariffs/") and path.endswith("/update"):
                self.update_energy_tariff(int(path.split("/")[3]), form)
            elif path.startswith("/energy/tariffs/") and path.endswith("/delete"):
                self.delete_energy_tariff(int(path.split("/")[3]))
            else:
                self.send_html(page("Nicht gefunden", '<div class="card empty">Diese Aktion gibt es nicht.</div>'), 404)
        except Exception as exc:
            self.send_html(page("Fehler", f'<div class="notice warn">{esc(exc)}</div><a class="btn secondary" href="javascript:history.back()">Zurück</a>'), 400)

    def dashboard(self, period, notice):
        period = period if period in ("month", "year", "contract", "all") else "month"
        if period == "contract":
            start, end = None, None
        else:
            start, end = period_bounds(period)
        period_clauses, args = ["is_valid=1"], []
        if start:
            period_clauses.append("read_on>=?")
            args.append(start)
        if end:
            period_clauses.append("read_on<=?")
            args.append(end)
        where = "WHERE " + " AND ".join(period_clauses)
        args = tuple(args)
        with connect() as db:
            forecasts = {metric: settlement_forecast(db, metric) for metric in ("grid_import", "gas", "water", "wastewater")}

            if period == "contract":
                energy = {metric: None for metric in METRICS}
                costs = {}

                for contract_metric in ("grid_import", "gas", "water", "wastewater"):
                    active_contract = active_energy_tariff(db, contract_metric)
                    if not active_contract:
                        continue
                    contract_start = active_contract["valid_from"]
                    contract_end = tariff_end_or_today(active_contract)
                    energy[contract_metric] = allocated_usage(
                        db,
                        reading_metric_for(contract_metric),
                        contract_start,
                        contract_end,
                        contract_metric,
                    )[0]
                    costs[contract_metric] = energy_finances(
                        db, contract_start, contract_end
                    ).get(contract_metric, {}).get("cost")

                grid_contract = active_energy_tariff(db, "grid_import")
                if grid_contract:
                    grid_start = grid_contract["valid_from"]
                    grid_end = tariff_end_or_today(grid_contract)
                    energy["pv_self"] = allocated_usage(
                        db,
                        reading_metric_for("pv_self"),
                        grid_start,
                        grid_end,
                        "grid_import",
                    )[0]
                    pv_saved = pv_savings(db, grid_start, grid_end)
                else:
                    pv_saved = None
            else:
                energy = {
                    metric: allocated_usage(db, reading_metric_for(metric), start, end, "grid_import" if metric == "pv_self" else metric)[0]
                    for metric in METRICS
                }
                costs = energy_costs(db, start, end)
                pv_saved = pv_savings(db, start, end)
            pv_saved_text = f"−{fmt_money(pv_saved)}" if pv_saved is not None else "–"

            def dashboard_contract_result(metric_key):
                if period != "contract":
                    return ""
                forecast = forecasts.get(metric_key)
                projected = forecast.get("projected") if forecast else None
                if not projected:
                    return ""
                balance = float(projected.get("balance") or 0.0)
                if balance >= 0:
                    return f'<div class="muted"><strong>Erstattung: {fmt_money(balance)}</strong></div>'
                return f'<div class="muted"><strong style="color:var(--red)">Nachzahlung: −{fmt_money(abs(balance))}</strong></div>'
            fuel_clauses, fuel_args = [], []
            if start:
                fuel_clauses.append("fueled_on>=?")
                fuel_args.append(start)
            if end:
                fuel_clauses.append("fueled_on<=?")
                fuel_args.append(end)
            fwhere = "WHERE " + " AND ".join(fuel_clauses) if fuel_clauses else ""
            fuel_args = tuple(fuel_args)
            totals = db.execute(f"SELECT SUM(total_price) cost,SUM(liters) liters,MIN(odometer) minodo,MAX(odometer) maxodo FROM fuelings {fwhere}", fuel_args).fetchone()
            vehicles = db.execute("SELECT * FROM vehicles WHERE active=1 ORDER BY name").fetchall()
            diesel_avg = None
            total_cost_per_km = None
            dashboard_vehicle_name = "Fahrzeug"
            if vehicles:
                dashboard_vehicle_name = vehicles[0]["name"]
                diesel_avg = consumption_summary(db, vehicles[0]["id"])["average"]
                total_cost_per_km = vehicle_cost_summary(db, vehicles[0]["id"])["cost_per_km"]
            distance = (totals["maxodo"] - totals["minodo"]) if totals["maxodo"] is not None and totals["minodo"] is not None else 0
            cards = "".join(
                [
                    self.metric_card("⚡", "Strombezug", energy.get("grid_import"), "kWh", f'<div class="muted">Kosten: {fmt_money(costs.get("grid_import"))}</div>' + dashboard_contract_result("grid_import") + sparkline(db, "grid_import"), f"/energy/grid_import?period={period}"),
                    self.metric_card("☀️", "PV-Eigenverbrauch", energy.get("pv_self"), "kWh", f'<div class="muted">Dadurch gespart: {pv_saved_text}</div>' + sparkline(db, "pv_self"), f"/energy/pv_self?period={period}"),
                    self.metric_card("🔥", "Gas", energy.get("gas"), "m³", f'<div class="muted">Kosten: {fmt_money(costs.get("gas"))}</div>' + dashboard_contract_result("gas") + sparkline(db, "gas"), f"/energy/gas?period={period}"),
                    self.metric_card("💧", "Wasser", energy.get("water"), "m³", f'<div class="muted">Kosten: {fmt_money(costs.get("water"))}</div>' + dashboard_contract_result("water") + sparkline(db, "water"), f"/energy/water?period={period}"),
                    self.metric_card("🚰", "Abwasser", energy.get("wastewater"), "m³", f'<div class="muted">Kosten: {fmt_money(costs.get("wastewater"))}</div>' + dashboard_contract_result("wastewater") + sparkline(db, "water"), f"/energy/wastewater?period={period}"),
                    self.metric_card("🚐", f"{dashboard_vehicle_name} Ø-Verbrauch", diesel_avg, "l/100 km", f'<div class="muted">Gesamtkosten: {fmt_num(total_cost_per_km,3)} €/km</div>', "/vehicle-costs"),
                ]
            )
            vehicle_rows = ""
            for vehicle in vehicles:
                summary = consumption_summary(db, vehicle["id"])
                total_summary = vehicle_cost_summary(db, vehicle["id"])
                adblue = adblue_summary(db, vehicle["id"])
                vehicle_rows += f"<tr><td><strong>{esc(vehicle['name'])}</strong></td><td>{fmt_num(summary['average'])} l/100 km</td><td>{fmt_num(total_summary['cost_per_km'],3)} €/km</td><td>{fmt_num(adblue['per_1000'])} l/1.000 km</td><td>{fmt_num(total_summary['distance'],0)} km</td></tr>"
        settlement_cards = ""
        for metric in ("grid_import", "gas", "water", "wastewater"):
            cfg = METRICS[metric]
            forecast = forecasts.get(metric) or {"reason": "Keine Prognose verfügbar."}
            if forecast.get("reason"):
                settlement_cards += f"""<div class="card settlement-card"><h3>{cfg['icon']} {esc(cfg['label'])}</h3><div class="contract">Abrechnungsvorschau</div><div class="muted">{esc(forecast['reason'])}</div><div class="actions" style="margin-top:16px"><a href="/energy">Energiedaten ergänzen →</a></div></div>"""
                continue
            current = forecast["current"]
            projected = forecast["projected"]
            current_sign = "+" if current["balance"] >= 0 else "−"
            projected_label = "voraussichtliche Erstattung" if projected["balance"] >= 0 else "voraussichtliche Nachzahlung"
            projected_class = "ok" if projected["balance"] >= 0 else "warn"
            settlement_cards += f"""<div class="card settlement-card"><h3>{cfg['icon']} {esc(cfg['label'])}</h3><div class="contract">{esc(forecast['provider'] or 'Tarif')} · {esc(forecast['contract_start'])} bis {esc(forecast['contract_end'])}</div><div class="settlement-block"><strong>Stand bis {esc(forecast['data_until'])}</strong><div class="row muted"><span>Kosten</span><span>{fmt_money(current['cost'])}</span></div><div class="row muted"><span>anteilige Abschläge</span><span>{fmt_money(current['advance'])}</span></div><div class="row"><span>Zwischenstand</span><strong>{current_sign}{fmt_money(abs(current['balance']))}</strong></div></div><div class="settlement-block"><strong>Hochrechnung bis {esc(forecast['contract_end'])}</strong><div class="row muted"><span>Kosten</span><span>{fmt_money(projected['cost'])}</span></div><div class="row muted"><span>Abschläge</span><span>{fmt_money(projected['advance'])}</span></div><div class="row"><span>{projected_label}</span><strong class="amount {projected_class}">{fmt_money(abs(projected['balance']))}</strong></div></div></div>"""
        labels = {"month": "Monat", "year": "Jahr", "contract": "Vertragszeitraum", "all": "Gesamt"}
        tabs = "".join(f'<a class="{"active" if period==key else ""}" href="/?period={key}">{label}</a>' for key, label in labels.items())
        body = f"""<div class="topbar"><div><h1>Energie & Verbrauch</h1><div class="subtitle">Deine lokalen Verbrauchsdaten auf einen Blick</div></div><div class="tabs">{tabs}</div></div><div class="grid">{cards}</div><section class="section"><div class="section-head"><div><h2>Voraussichtliche Abrechnung</h2><div class="muted">Hochrechnung aus dem bisherigen Tagesverbrauch bis zum Ende des jeweils aktuellen Tarifzeitraums</div></div><a href="/energy/grid_import">Stromvertrag bearbeiten →</a></div><div class="settlement-grid">{settlement_cards}</div><p class="muted">Berechnung: Zahlungen − (Verbrauchskosten + Grundpreis). Ein positiver Saldo ergibt eine Erstattung, ein negativer eine Nachzahlung. Die PV-Ersparnis bleibt separat und reduziert die tatsächlichen Stromkosten nicht.</p></section><section class="section two"><div class="card"><div class="section-head"><h2>Fahrzeuge</h2><a href="/vehicle-costs">Fahrzeugkosten verwalten →</a></div><div class="table-wrap"><table><thead><tr><th>Fahrzeug</th><th>Diesel</th><th>Gesamtkosten/km</th><th>AdBlue</th><th>gefahren</th></tr></thead><tbody>{vehicle_rows or '<tr><td colspan="5" class="empty">Noch keine Fahrzeugdaten</td></tr>'}</tbody></table></div></div><div class="card metric"><div class="icon">€</div><div class="label">Tankkosten {labels[period]}</div><div class="value">{fmt_money(totals['cost'])}</div><div class="muted">{fmt_num(totals['liters'])} Liter · ca. {fmt_num(distance,0)} km</div><div class="actions" style="margin-top:20px"><a class="btn" href="/fuelings/new">+ Tankung</a><a class="btn secondary" href="/vehicle-costs">+ weitere Kosten</a></div></div></section>"""
        self.send_html(page("Dashboard", body, "/", notice))

    @staticmethod
    def metric_card(icon, label, value, unit, extra, href=None):
        tag, end_tag = (f'<a class="card metric metric-link" href="{esc(href)}">', "</a>") if href else ('<div class="card metric">', "</div>")
        return f'{tag}<div class="icon">{icon}</div><div class="value">{fmt_num(value)} <small style="font-size:14px">{esc(unit)}</small></div><div class="label">{esc(label)}</div>{extra}{end_tag}'

    def energy_metric_management(self, metric, tariffs, section="all"):
        if metric == "pv_self":
            explanation = '<section class="section card"><h2>Photovoltaik</h2><p class="muted">PV-Eigenverbrauch verwendet den gültigen Stromtarif. Ein eigener Vertrag oder Abschlag wird dafür nicht angelegt.</p></section>'
            return explanation if section in ("all", "contract") else ""
        csrf = csrf_for(self.cookie_token())
        cfg = METRICS[metric]
        default_interval = 3 if metric in ("water", "wastewater") else 1
        interval_options = "".join(
            f'<option value="{months}" {"selected" if months == default_interval else ""}>{label}</option>'
            for months, label in ((1, "monatlich"), (3, "quartalsweise"), (6, "halbjährlich"), (12, "jährlich"))
        )
        if metric == "grid_import":
            price_label, price_placeholder = "Strompreis in Cent/kWh", "z. B. 32,90"
            factor_field = '<input type="hidden" name="kwh_per_unit" value="1">'
        elif metric == "gas":
            price_label, price_placeholder = "Gaspreis in Cent/kWh", "z. B. 10,90"
            factor_field = '<div class="field"><label>Umrechnung kWh pro m³</label><input inputmode="decimal" name="kwh_per_unit" placeholder="z. B. 10,42" required></div>'
        else:
            price_label, price_placeholder = "Verbrauchspreis in €/m³", "z. B. 2,8500"
            factor_field = '<input type="hidden" name="kwh_per_unit" value="1">'
        rows = ""
        with connect() as db:
            for tariff in tariffs:
                current_amount = float(tariff["advance_monthly"])
                for change in db.execute("SELECT valid_from,advance_monthly FROM energy_advance_changes WHERE tariff_id=? ORDER BY valid_from,id", (tariff["id"],)):
                    if change["valid_from"] <= date.today().isoformat():
                        current_amount = float(change["advance_monthly"])
                price = (f"{fmt_num(tariff['price_per_kwh'] * 100, 2)} Cent/kWh" if metric in ("grid_import", "gas") else f"{fmt_num(tariff['price_per_kwh'], 4)} €/m³")
                first_payment = tariff["first_payment_date"] or "automatisch"
                rows += f"""<tr><td><strong>{esc(tariff['provider'])}</strong></td><td>{esc(tariff['valid_from'])}</td><td>{esc(tariff['valid_to'] or 'offen')}</td><td>{price}</td><td>{fmt_money(tariff['base_fee_monthly'])}/Monat</td><td>{fmt_money(current_amount)} · {payment_interval_label(tariff['payment_interval_months'])}</td><td>am {tariff['payment_day']}.<br><span class="muted">Erste Zahlung: {esc(first_payment)}<br>{esc(tariff['payment_account'] or 'Standardkonto in FinanzLab')}</span></td><td><a href="/energy/tariffs/{tariff['id']}/edit">Vertrag und Historie bearbeiten →</a></td></tr>"""
        manual = ""
        if metric in ("grid_import", "gas", "water"):
            example = "12.345,67" if metric == "grid_import" else "1.234,567"
            manual = f"""<section class="section card"><div class="section-head"><div><h2>Zählerstand manuell erfassen</h2><div class="muted">Auch rückwirkend möglich; angrenzende Verbrauchswerte werden neu berechnet.</div></div><span class="badge">manuell</span></div><form method="post" action="/energy/readings/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="metric" value="{metric}"><div class="form-grid"><div class="field"><label>Ablesedatum</label><input type="date" name="read_on" value="{date.today().isoformat()}" max="{date.today().isoformat()}" required></div><div class="field"><label>Zählerstand in {esc(cfg['unit'])}</label><input inputmode="decimal" name="total_value" placeholder="z. B. {example}" required></div></div><button class="btn" style="margin-top:16px">Zählerstand speichern</button></form></section>"""
        derived_note = '<p class="muted">Die Verbrauchsmenge stammt automatisch vom Wasserzähler. Kosten und Zahlungen bleiben vollständig getrennt.</p>' if metric == "wastewater" else ""
        form = f"""<section class="section card"><div class="section-head"><div><h2>{esc(cfg['label'])}-Vertrag hinzufügen</h2><div class="muted">Tarif, Grundpreis, Zahlungsrhythmus und FinanzLab-Konto werden historisch geführt.</div></div><span class="badge">{len(tariffs)} von {TARIFF_LIMIT}</span></div>{derived_note}<form method="post" action="/energy/tariffs/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="metric" value="{metric}"><div class="form-grid"><div class="field full"><label>Anbieter</label><input name="provider" maxlength="100" placeholder="z. B. Stadtwerke Musterstadt" required></div><div class="field"><label>Gültig von</label><input type="date" name="valid_from" required></div><div class="field"><label>Gültig bis</label><input type="date" name="valid_to"></div><div class="field"><label>{price_label}</label><input inputmode="decimal" name="price_per_kwh" placeholder="{price_placeholder}" required></div>{factor_field}<div class="field"><label>Grundpreis in €/Monat</label><input inputmode="decimal" name="base_fee_monthly" placeholder="z. B. 8,50" required></div><div class="field"><label>Betrag je Zahlung in €</label><input inputmode="decimal" name="advance_monthly" placeholder="z. B. 120,00" required></div><div class="field"><label>Zahlungsrhythmus</label><select name="payment_interval_months">{interval_options}</select></div><div class="field"><label>Zahlungstag</label><input type="number" name="payment_day" min="1" max="31" value="1" required></div><div class="field"><label>Erste Zahlung <span class="muted">(optional)</span></label><input type="date" name="first_payment_date"><span class="muted">Leer = automatisch aus Vertragsbeginn, Zahlungstag und Rhythmus.</span></div><div class="field full"><label>Konto in FinanzLab</label><input name="payment_account" maxlength="100" placeholder="exakter Kontoname, z. B. Girokonto"></div></div><button class="btn" style="margin-top:18px" {"disabled" if len(tariffs) >= TARIFF_LIMIT else ""}>Vertrag speichern</button></form></section>"""
        history = f"""<section class="section card"><div class="section-head"><h2>Gespeicherte Verträge</h2><span class="badge">{len(tariffs)} Verträge</span></div><div class="table-wrap"><table><thead><tr><th>Anbieter</th><th>Von</th><th>Bis</th><th>Verbrauchspreis</th><th>Grundpreis</th><th>Zahlungsbetrag</th><th>Zahlung</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan="8" class="empty">Noch kein Vertrag hinterlegt.</td></tr>'}</tbody></table></div></section>"""
        contract_content = history + form
        if section == "contract":
            return contract_content
        if section == "imports":
            return manual
        return contract_content + manual

    def energy_detail_page(self, metric, period, requested_month="", notice="", view="overview"):
        cfg = METRICS[metric]
        reading_metric = reading_metric_for(metric)
        view = normalize_energy_detail_view(view)
        period = period if period in ("month", "year", "contract", "all") else "year"
        current_month = date.today().replace(day=1)
        selected_month = month_start_from_query(requested_month) if period == "month" else None
        if selected_month:
            start_on, end_on = selected_month_bounds(selected_month)
        elif period == "contract":
            start_on, end_on = None, None
        else:
            start_on, end_on = period_bounds(period)
        clauses = ["metric=?"]
        args = [reading_metric]
        if start_on:
            clauses.append("read_on>=?")
            args.append(start_on)
        if end_on:
            clauses.append("read_on<=?")
            args.append(end_on)
        with connect() as db:
            readings = db.execute(
                f"SELECT * FROM energy_readings WHERE {' AND '.join(clauses)} ORDER BY read_on,id",
                args,
            ).fetchall()
            tariff_metric = "grid_import" if metric == "pv_self" else metric
            tariffs = sorted(
                db.execute(
                    "SELECT * FROM energy_tariffs WHERE metric=? ORDER BY valid_from,id",
                    (tariff_metric,),
                ).fetchall(),
                key=tariff_display_sort_key_for_row,
            )
            earliest = db.execute(
                "SELECT MIN(read_on) AS first_on FROM energy_readings WHERE metric=?",
                (reading_metric,),
            ).fetchone()["first_on"]
            latest_reading = db.execute(
                """SELECT * FROM energy_readings WHERE metric=? AND is_valid=1
                   ORDER BY read_on DESC,id DESC LIMIT 1""",
                (reading_metric,),
            ).fetchone()
            sync_logs = db.execute(
                "SELECT * FROM sync_log ORDER BY synced_at DESC,id DESC LIMIT 50"
            ).fetchall()
            active_contract = None
            if period == "contract":
                active_contract = active_energy_tariff(db, tariff_metric)
                if active_contract:
                    start_on = active_contract["valid_from"]
                    end_on = tariff_end_or_today(active_contract)
                    clauses = ["metric=?"]
                    args = [reading_metric]
                    clauses.append("read_on>=?")
                    args.append(start_on)
                    clauses.append("read_on<=?")
                    args.append(end_on)
                    readings = db.execute(
                        f"SELECT * FROM energy_readings WHERE {' AND '.join(clauses)} ORDER BY read_on,id",
                        args,
                    ).fetchall()
            finances = energy_finances(db, start_on, end_on)
            pv_saved = pv_savings(db, start_on, end_on) if metric == "pv_self" else None
            historical_month = bool(selected_month and selected_month < current_month)
            forecast = settlement_forecast(db, metric) if metric in ("grid_import", "gas", "water", "wastewater") and not historical_month else None
            if period == "contract" and forecast and forecast.get("projected"):
                start_on = forecast["contract_start"]
                end_on = forecast["contract_end"]
                clauses = ["metric=?"]
                args = [reading_metric]
                clauses.append("read_on>=?")
                args.append(start_on)
                clauses.append("read_on<=?")
                args.append(end_on)
                readings = db.execute(
                    f"SELECT * FROM energy_readings WHERE {' AND '.join(clauses)} ORDER BY read_on,id",
                    args,
                ).fetchall()
                finances = energy_finances(db, start_on, end_on)
                pv_saved = pv_savings(db, start_on, end_on) if metric == "pv_self" else None
            total_consumption = allocated_usage(db, reading_metric, start_on, end_on, tariff_metric)[0]
            reading_costs = {}
            previous_valid_day = None
            for item in db.execute("SELECT read_on,delta_value FROM energy_readings WHERE metric=? AND is_valid=1 ORDER BY read_on,id", (reading_metric,)):
                current_valid_day = date.fromisoformat(item["read_on"])
                if item["delta_value"] is not None:
                    interval_start = (previous_valid_day + timedelta(days=1)) if previous_valid_day else current_valid_day
                    reading_costs[item["read_on"]] = allocated_usage(db, reading_metric, interval_start.isoformat(), item["read_on"], tariff_metric)[1]
                previous_valid_day = current_valid_day
            payment_sections = (
                energy_payment_plan_sections(db, metric, start_on, end_on)
                if view == "payments" else []
            )

        labels = {"month": "Monat", "year": "Jahr", "contract": "Vertragszeitraum", "all": "Gesamt"}

        def detail_href(target_view, target_period=None, target_month=None):
            params = {"view": target_view}
            if target_period:
                params["period"] = target_period
            if target_month:
                params["month"] = target_month
            return f"/energy/{metric}?" + urllib.parse.urlencode(params)

        view_labels = (
            ("overview", "▦", "Übersicht"),
            ("contract", "▤", "Vertrag"),
            ("payments", "€", "Zahlungsplan"),
            ("imports", "⇩", "Importhistorie"),
        )
        primary_tabs = "".join(
            f'<a class="{"active" if view == key else ""}" '
            f'href="{esc(detail_href(key, period if key != "contract" else None, selected_month.strftime("%Y-%m") if selected_month and key != "contract" else None))}" '
            f'{"aria-current=page" if view == key else ""}><span aria-hidden="true">{icon}</span>{label}</a>'
            for key, icon, label in view_labels
        )
        period_tabs = "".join(
            f'<a class="{"active" if period == key else ""}" '
            f'href="{esc(detail_href(view, key, selected_month.strftime("%Y-%m") if key == "month" and selected_month else None))}">{label}</a>'
            for key, label in labels.items()
        )
        month_navigation = ""
        if selected_month:
            earliest_month = date.fromisoformat(earliest).replace(day=1) if earliest else current_month
            previous_month = shift_month(selected_month, -1) if selected_month > earliest_month else None
            next_month = shift_month(selected_month, 1) if selected_month < current_month else None
            previous_control = (
                f'<a class="month-arrow" href="{esc(detail_href(view, "month", previous_month.strftime("%Y-%m")))}" aria-label="Vorheriger Monat">‹</a>'
                if previous_month
                else '<span class="month-arrow disabled" aria-disabled="true" title="Erster Monat mit Quelldaten">‹</span>'
            )
            next_control = (
                f'<a class="month-arrow" href="{esc(detail_href(view, "month", next_month.strftime("%Y-%m")))}" aria-label="Nächster Monat">›</a>'
                if next_month
                else '<span class="month-arrow disabled" aria-disabled="true" title="Aktueller Monat">›</span>'
            )
            current_control = (
                f'<a class="btn secondary current-month-link" href="{esc(detail_href(view, "month"))}">Zum aktuellen Monat</a>'
                if selected_month < current_month
                else '<span class="btn secondary current-month-link disabled" aria-disabled="true">Zum aktuellen Monat</span>'
            )
            month_navigation = f'<div class="month-nav">{previous_control}<div class="month-title"><span class="muted">Monatsübersicht</span><strong>{month_label(selected_month)}</strong></div>{next_control}{current_control}</div>'
        invalid_count = sum(1 for row in readings if not row["is_valid"])
        values = finances.get(tariff_metric)

        def applicable_tariff(read_on):
            return next(
                (
                    tariff
                    for tariff in tariffs
                    if tariff["valid_from"] <= read_on
                    and (not tariff["valid_to"] or read_on <= tariff["valid_to"])
                ),
                None,
            )

        reading_rows = ""
        converted_total = 0.0
        for row in reversed(readings):
            tariff = applicable_tariff(row["read_on"])
            delta = float(row["delta_value"]) if row["delta_value"] is not None and row["is_valid"] else None
            factor = float(tariff["kwh_per_unit"]) if tariff and metric == "gas" else 1.0
            converted = delta * factor if delta is not None and metric == "gas" else None
            if converted is not None:
                converted_total += converted
            amount = None
            if delta is not None:
                amount = reading_costs.get(row["read_on"])
            if tariff:
                if tariff_metric in ("grid_import", "gas"):
                    price_text = f"{fmt_num(float(tariff['price_per_kwh']) * 100, 2)} Cent/kWh"
                else:
                    price_text = f"{fmt_num(tariff['price_per_kwh'], 4)} €/m³"
                tariff_text = tariff["provider"] or "Tarif ohne Anbieter"
                if metric == "gas":
                    tariff_text += f" · {fmt_num(factor, 4)} kWh/m³"
            else:
                price_text = "–"
                tariff_text = "kein Tarif für dieses Datum"
            if not row["is_valid"]:
                status = f'<span class="badge warn">ausgeschlossen</span><br><span>{esc(row["invalid_reason"] or "unplausibler Wert")}</span>'
                row_class = ' class="reading-invalid"'
            elif row["delta_value"] is None:
                status = '<span class="badge">Ausgangsstand</span>'
                row_class = ""
            else:
                status = '<span class="badge ok">gültig</span>'
                row_class = ""
            source = "manuell" if row["source"] == "manual" else (row["source"] or "Home Assistant")
            if metric == "wastewater":
                source = "vom Wasserzähler übernommen"
            reading_rows += f"""<tr{row_class}><td>{date.fromisoformat(row['read_on']).strftime('%d.%m.%Y')}</td><td><strong>{fmt_num(row['total_value'])} {esc(row['unit'])}</strong></td><td>{fmt_num(delta) + ' ' + esc(cfg['unit']) if delta is not None else '–'}</td>{f'<td>{fmt_num(converted)} kWh</td>' if metric == 'gas' else ''}<td>{esc(tariff_text)}</td><td>{price_text}</td><td>{fmt_money(amount)}</td><td><span class="source-detail"><strong>{esc(source)}</strong><span class="muted">{esc(row['entity_id'] or 'ohne Sensor-ID')}</span><span class="muted">eingelesen {esc(row['created_at'])}</span></span></td><td>{status}</td></tr>"""

        period_caption = month_label(selected_month) if selected_month else labels[period]
        contract_values = forecast.get("projected") if period == "contract" and forecast and forecast.get("projected") else None
        if period == "contract" and active_contract:
            contract_caption = f'Vertrag {esc(active_contract["valid_from"])} bis {esc(active_contract["valid_to"] or "offen")}'
        elif contract_values:
            contract_caption = f'Vertrag {esc(forecast["contract_start"])} bis {esc(forecast["contract_end"])}'
        else:
            contract_caption = period_caption
        # Kein DB-Zugriff hier: Die Datenbank ist an dieser Stelle bereits geschlossen.
        # Verbrauch, Kosten, Grundpreis und Abschläge stammen aus demselben
        # ausgewählten Zeitraum. Hochrechnungen werden nur separat angezeigt.
        display_consumption = total_consumption
        display_converted_total = converted_total

        summary_boxes = []
        if latest_reading:
            latest_source = "Wasserzähler" if metric == "wastewater" else (
                "manuell" if latest_reading["source"] == "manual" else "automatisch"
            )
            summary_boxes.append(
                f'<div class="summary-box"><div class="muted">Aktueller Zählerstand</div>'
                f'<div class="value">{fmt_num(latest_reading["total_value"])} {esc(cfg["unit"])}</div>'
                f'<div class="muted">vom {date.fromisoformat(latest_reading["read_on"]).strftime("%d.%m.%Y")} · {esc(latest_source)}</div></div>'
            )
        summary_boxes.append(
            f'<div class="summary-box"><div class="muted">Verbrauch {contract_caption}</div><div class="value">{fmt_num(display_consumption)} {esc(cfg["unit"])}</div></div>'
        )
        if metric == "gas":
            summary_boxes.append(f'<div class="summary-box"><div class="muted">umgerechnet</div><div class="value">{fmt_num(display_converted_total)} kWh</div></div>')
        if metric == "pv_self":
            summary_boxes.append(f'<div class="summary-box"><div class="muted">Dadurch gespart</div><div class="value">−{fmt_money(pv_saved)}</div></div>')
        elif values:
            # The summary boxes must use the same selected period as the displayed
            # consumption. Forecast/projected values are shown separately below.
            display_values = values
            summary_boxes.extend(
                [
                    f'<div class="summary-box"><div class="muted">Verbrauchskosten</div><div class="value">{fmt_money(display_values["variable"])}</div></div>',
                    f'<div class="summary-box"><div class="muted">Grundpreis</div><div class="value">{fmt_money(display_values["base_fee"])}</div></div>',
                    f'<div class="summary-box"><div class="muted">Gesamtkosten</div><div class="value">{fmt_money(display_values["cost"])}</div></div>',
                    f'<div class="summary-box"><div class="muted">Abschläge</div><div class="value">{fmt_money(display_values["advance"])}</div></div>',
                ]
            )
        forecast_explanation = ""
        if forecast and forecast.get("projected"):
            projected_balance = forecast["projected"]["balance"]
            projected_label = "Voraussichtliche Erstattung" if projected_balance >= 0 else "Voraussichtliche Nachzahlung"
            projected_class = "ok" if projected_balance >= 0 else "warn"
            basis_labels = {
                "planned": "Zahlungsbasis: Vertragsplan",
                "actual": "Zahlungsbasis: bestätigte Ist-Zahlungen",
                "actual_plus_planned": "Zahlungsbasis: Ist bis heute + künftiger Plan",
            }
            payment_basis = forecast["projected"].get("payment_basis", "planned")
            basis_label = basis_labels.get(payment_basis, "Zahlungsbasis: Vertragsplan")
            quality_label = forecast.get("quality_label", "Hochrechnung")
            quality_class = "ok" if forecast.get("quality") == "good" else "warn"
            summary_boxes.append(
                f'<div class="summary-box"><div class="muted">{projected_label} bis {esc(forecast["contract_end"])}</div>'
                f'<div class="value {projected_class}">{fmt_money(abs(projected_balance))}</div>'
                f'<div class="meta"><span class="badge {quality_class}">{esc(quality_label)}</span><br>{esc(basis_label)}</div>'
                f'<div class="meta">Zahlungsbasis Hochrechnung: {fmt_money(forecast["projected"].get("advance", 0))}</div>'
                f'<div class="meta">Kosten Hochrechnung: {fmt_money(forecast["projected"].get("cost", 0))}</div>'
                f'</div>'
            )
            missing_count = len(forecast["projected"].get("missing_due_dates") or [])
            missing_note = (
                f" {missing_count} vergangene Zahlung(en) werden laut Vertragsplan berücksichtigt."
                if missing_count else ""
            )
            forecast_explanation = (
                f'<div class="notice {"" if forecast.get("quality") == "good" else "warn"} forecast-note">'
                f'<strong>Orientierungswert – keine garantierte Schlussrechnung</strong>'
                f'{esc(forecast.get("method") or "Hochrechnung aus dem bisherigen Verbrauch")}. '
                f'{esc(basis_label)}.{esc(missing_note)}</div>'
            )

        anomaly_html = f'<div class="notice warn">{invalid_count} unplausible oder während eines Ausfalls erfasste Werte sind rot markiert und werden in Verbrauch und Kosten nicht berücksichtigt.</div>' if invalid_count else ""
        gas_help = '<p class="muted">Beim Gas werden für jeden Tag der Zählerverbrauch in m³, der gültige Umrechnungsfaktor in kWh/m³ und die daraus berechneten kWh getrennt angezeigt.</p>' if metric == "gas" else ""
        derived_help = '<div class="notice">Abwasser verwendet automatisch dieselben Verbrauchsmengen und Zählerstände wie Wasser. Kosten, Vertrag, Grundpreis und Zahlungen werden dennoch vollständig getrennt berechnet.</div>' if metric == "wastewater" else ""
        conversion_header = "<th>Umgerechnet</th>" if metric == "gas" else ""
        contract_management = self.energy_metric_management(metric, tariffs, "contract")
        import_management = self.energy_metric_management(metric, tariffs, "imports")

        def render_payment_rows(items):
            rendered = ""
            for item in items:
                due_label = date.fromisoformat(item["due_on"]).strftime("%d.%m.%Y")
                if item["period_from"]:
                    period_label = (
                        f'{date.fromisoformat(item["period_from"]).strftime("%d.%m.%Y")} bis '
                        f'{date.fromisoformat(item["period_to"]).strftime("%d.%m.%Y")}'
                    )
                else:
                    period_label = "Sonderbuchung ohne Verbrauchszeitraum"
                planned_label = fmt_money(item["planned"]) if item["planned"] is not None else "–"
                actual_label = fmt_money(item["actual"]) if item["actual_available"] else "noch nicht bestätigt"
                money_class = "ok" if item["balance"] >= 0 else "warn"
                rendered += f"""<tr><td><strong>{due_label}</strong></td><td>{period_label}</td><td><span class="plan-payment"><span class="muted">Plan {planned_label}</span><strong>Ist {actual_label}</strong></span></td><td>{fmt_money(item['base_fee'])}</td><td>{fmt_num(item['consumption']) + ' ' + esc(cfg['unit']) if item['consumption'] is not None else '–'}</td><td>{fmt_money(item['variable_cost'])}</td><td><strong>{fmt_money(item['total_cost'])}</strong></td><td><strong class="{money_class}">{fmt_money(item['balance'])}</strong></td><td><span class="badge {item['status_class']}">{esc(item['status'])}</span></td></tr>"""
            return rendered

        payment_sections_html = ""
        payment_basis_labels = {
            "planned": "Vertragsplan",
            "actual": "bestätigte Ist-Zahlungen",
            "actual_plus_planned": "bestätigte Ist-Zahlungen + offene Planbeträge",
        }
        for section in payment_sections:
            section_from = date.fromisoformat(section["period_from"]).strftime("%d.%m.%Y")
            section_to = date.fromisoformat(section["period_to"]).strftime("%d.%m.%Y")
            calculated_to = (
                date.fromisoformat(section["calculated_to"]).strftime("%d.%m.%Y")
                if section["calculated_to"] else "noch nicht begonnen"
            )
            actual_total = fmt_money(section["actual"]) if section["actual_available"] else "–"
            section_summary = [
                f'<div class="summary-box"><div class="muted">Plan-Zahlungen</div><div class="value">{fmt_money(section["planned"])}</div><div class="meta">im gewählten Zeitraum</div></div>',
                f'<div class="summary-box"><div class="muted">Bestätigte Ist-Zahlungen</div><div class="value">{actual_total}</div><div class="meta">im gewählten Zeitraum</div></div>',
                f'<div class="summary-box"><div class="muted">Anteilige Grundgebühr</div><div class="value">{fmt_money(section["base_fee"])}</div><div class="meta">bis {calculated_to}</div></div>',
                f'<div class="summary-box"><div class="muted">Verbrauch</div><div class="value">{fmt_num(section["consumption"])} {esc(cfg["unit"])}</div><div class="meta">bis {calculated_to}</div></div>',
                f'<div class="summary-box"><div class="muted">Verbrauchskosten</div><div class="value">{fmt_money(section["variable_cost"])}</div><div class="meta">bis {calculated_to}</div></div>',
                f'<div class="summary-box"><div class="muted">Gesamtkosten</div><div class="value">{fmt_money(section["total_cost"])}</div><div class="meta">Grundgebühr + Verbrauch</div></div>',
                f'<div class="summary-box supplier-result"><div class="muted">{esc(section["result_label"])}</div><div class="value {section["result_class"]}">{fmt_money(abs(section["balance"]))}</div><div class="meta">Basis: {esc(payment_basis_labels.get(section["payment_basis"], "Vertragsplan"))}</div></div>',
            ]
            missing_count = len(section["missing_due_dates"])
            missing_note = (
                f'<div class="notice warn supplier-note">{missing_count} fällige Zahlung(en) sind noch nicht bestätigt; für den Zwischenstand gilt dort weiterhin der Vertragsplan.</div>'
                if missing_count else ""
            )
            boundary_dates = ", ".join(
                date.fromisoformat(value).strftime("%d.%m.%Y")
                for value in section["missing_boundary_readings"]
            )
            boundary_note = (
                '<div class="notice warn supplier-note"><strong>Wechselstand fehlt.</strong> '
                f'Für {esc(boundary_dates)} ist noch ein gültiger Zählerstand nötig. '
                'Die Lieferantenabrechnung bleibt bis dahin ausdrücklich vorläufig.</div>'
                if boundary_dates else ""
            )
            rows_for_section = render_payment_rows(section["rows"])
            payment_sections_html += f"""<section class="section card supplier-payment-section" data-tariff-id="{section['tariff_id']}"><div class="supplier-header"><div><div class="supplier-kicker">Lieferant · eigener Tarifabschnitt</div><h2>{esc(section['provider'])}</h2><div class="muted">{section_from} bis {section_to} · {esc(payment_interval_label(section['payment_interval_months']))}</div></div><div class="supplier-badges"><span class="badge">Tarif {section['tariff_id']}</span><span class="badge {'ok' if section['final'] else 'warn'}">{'abgeschlossen' if section['final'] else 'vorläufig'}</span></div></div><div class="summary-grid supplier-summary">{"".join(section_summary)}</div>{missing_note}{boundary_note}<div class="table-wrap supplier-table"><table class="payment-plan-table"><thead><tr><th>Fälligkeit</th><th>Kostenzeitraum</th><th>Plan / Ist</th><th>Grundgebühr</th><th>Verbrauch</th><th>Verbrauchskosten</th><th>Gesamtkosten</th><th>Saldo</th><th>Status</th></tr></thead><tbody>{rows_for_section or '<tr><td colspan="9" class="empty">In diesem Tarifabschnitt liegt keine Abschlagsfälligkeit. Die anteiligen Kosten stehen trotzdem vollständig in der Zusammenfassung.</td></tr>'}</tbody></table></div></section>"""
        if not payment_sections_html:
            payment_sections_html = '<div class="card empty">Für diesen Zeitraum ist noch kein Tarif mit Zahlungsplan vorhanden.</div>'
        sync_rows = "".join(
            f'<tr><td>{esc(item["synced_at"])}</td><td><span class="badge {"ok" if item["status"] == "ok" else "warn"}">{esc(item["status"])}</span></td><td style="white-space:normal">{esc(item["message"])}</td></tr>'
            for item in sync_logs
        )

        overview_content = f"""{derived_help}{anomaly_html}<div class="card"><div class="summary-grid">{"".join(summary_boxes)}</div></div>{forecast_explanation}<section class="section card"><div class="section-head"><h2>Verbrauch im Verlauf</h2><div class="chart-legend"><span class="legend-line">gültige Werte</span></div></div><div class="table-wrap">{detail_chart(readings, 'delta_value', cfg['unit'])}</div></section><section class="section card"><div class="section-head"><h2>Zählerstand im Verlauf</h2><div class="chart-legend"><span class="legend-line">gültiger Verlauf</span><span class="legend-invalid">ausgeschlossen</span></div></div><div class="table-wrap">{detail_chart(readings, 'total_value', cfg['unit'], True)}</div></section>"""
        contract_content = f"""{derived_help}<div class="detail-intro"><h2>Tarife und Vertragslaufzeiten</h2><p class="muted">Hier findest du zuerst deine bestehenden Verträge. Neue Zeiträume kannst du direkt darunter ergänzen.</p></div>{contract_management}"""
        payments_content = f"""<div class="detail-intro"><h2>Abschläge und Zahlungsstand</h2><p class="muted">Jeder Lieferanten- und Tarifwechsel beginnt einen eigenen Abschnitt. Kein Verbrauch, Grundpreis oder Saldo wird über eine Tarifgrenze hinweg zusammengefasst.</p></div>{payment_sections_html}<p class="muted section">Saldo je Tarifabschnitt: bestätigte Ist-Zahlungen plus noch offene Planbeträge, abzüglich anteiliger Grundgebühr und Verbrauchskosten. Rücklastschriften und Sonderzahlungen bleiben in ihrem zugehörigen Tarifabschnitt nachvollziehbar.</p>"""
        history_colspan = 10 if metric == "gas" else 9
        imports_content = f"""{derived_help}{import_management}{anomaly_html}<div class="detail-intro section"><h2>Eingelesene Zählerstände</h2><p class="muted">Alle Werte bleiben mit Datenquelle, Einlesezeitpunkt und Ergebnis der Plausibilitätsprüfung nachvollziehbar.</p></div><section class="card"><div class="section-head"><div><h2>Importhistorie</h2>{gas_help}</div><span class="badge">{len(readings)} Einträge</span></div><div class="table-wrap"><table><thead><tr><th>Datum</th><th>Zählerstand</th><th>Verbrauch</th>{conversion_header}<th>Tarif / Faktor</th><th>Arbeitspreis</th><th>{'Ersparnis' if metric == 'pv_self' else 'Kosten'}</th><th>Quelle / eingelesen</th><th>Prüfung</th></tr></thead><tbody>{reading_rows or f'<tr><td colspan="{history_colspan}" class="empty">In diesem Zeitraum sind noch keine Werte vorhanden.</td></tr>'}</tbody></table></div></section><section class="section card"><div class="section-head"><h2>Letzte Importläufe</h2><span class="badge">{len(sync_logs)} Einträge</span></div><div class="table-wrap"><table><thead><tr><th>Zeit</th><th>Status</th><th>Meldung</th></tr></thead><tbody>{sync_rows or '<tr><td colspan="3" class="empty">Noch kein Import protokolliert.</td></tr>'}</tbody></table></div></section>"""
        contents = {
            "overview": overview_content,
            "contract": contract_content,
            "payments": payments_content,
            "imports": imports_content,
        }
        view_titles = {
            "overview": "Aktueller Stand und Verbrauch",
            "contract": "Vertragsdaten",
            "payments": "Zahlungsplan",
            "imports": "Importhistorie",
        }
        period_toolbar = ""
        if view != "contract":
            period_toolbar = f'<div class="detail-toolbar"><div><strong>{esc(view_titles[view])}</strong><div class="muted">Zeitraum auswählen</div></div><div class="tabs" aria-label="Zeitraum">{period_tabs}</div></div>{month_navigation}'
        body = f"""<div class="topbar"><div><a href="/">← Dashboard</a><h1 style="margin-top:8px">{cfg['icon']} {esc(cfg['label'])}</h1><div class="subtitle">Stand, Vertrag, Zahlungen und eingelesene Daten getrennt im Blick</div></div></div><nav class="tabs detail-primary-tabs" aria-label="Bereiche">{primary_tabs}</nav>{period_toolbar}<div class="detail-view">{contents[view]}</div>"""
        self.send_html(page(f"{cfg['label']} Details", body, f"/energy/{metric}", notice, "Fehler" in notice))

    def energy_page(self, notice):
        token = self.cookie_token()
        scheduled_hour, scheduled_minute = sync_time()
        with connect() as db:
            latest = {r["metric"]: r for r in db.execute("SELECT e.* FROM energy_readings e JOIN (SELECT metric,MAX(read_on) d FROM energy_readings WHERE is_valid=1 GROUP BY metric) x ON x.metric=e.metric AND x.d=e.read_on WHERE e.is_valid=1")}
            logs = db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 12").fetchall()
            tariffs = db.execute(
                """SELECT t.*,
                          COALESCE((SELECT c.advance_monthly FROM energy_advance_changes c
                                    WHERE c.tariff_id=t.id AND c.valid_from<=?
                                    ORDER BY c.valid_from DESC,c.id DESC LIMIT 1),t.advance_monthly) current_advance,
                          COALESCE((SELECT c.valid_from FROM energy_advance_changes c
                                    WHERE c.tariff_id=t.id AND c.valid_from<=?
                                    ORDER BY c.valid_from DESC,c.id DESC LIMIT 1),t.valid_from) current_advance_from
                   FROM energy_tariffs t ORDER BY t.valid_from DESC,t.id DESC""",
                (date.today().isoformat(), date.today().isoformat()),
            ).fetchall()
            finances = energy_finances(db)
            forecasts = {metric: settlement_forecast(db, metric) for metric in ("grid_import", "gas", "water", "wastewater")}
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
            forecast = forecasts.get(key)
            values = forecast["current"] if forecast and forecast.get("current") else finances.get(key)
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
            if forecast and forecast.get("projected"):
                projected_balance = forecast["projected"]["balance"]
                projected_label = "Erstattung" if projected_balance >= 0 else "Nachzahlung"
                projected_class = "ok" if projected_balance >= 0 else "warn"
                projected_html = f'<span class="badge {projected_class}">{fmt_money(abs(projected_balance))} {projected_label}</span><br><span class="muted">bis {esc(forecast["contract_end"])}</span>'
            elif key in ("grid_import", "gas", "water", "wastewater"):
                projected_html = f'<span class="muted">{esc((forecast or {}).get("reason", "Keine Hochrechnung verfügbar."))}</span>'
            else:
                projected_html = "–"
            entity_label = "manuelle Eingabe" if key == "water" else (cfg["entity"] or "nicht eingerichtet")
            saving_html = f'<strong>−{fmt_money(pv_saved)}</strong><br><span class="badge ok">dadurch gespart</span>' if key == "pv_self" and pv_saved is not None else "–"
            rows += f"<tr><td>{cfg['icon']} <strong>{esc(cfg['label'])}</strong></td><td>{esc(entity_label)}</td><td>{fmt_num(value['total_value'])+' '+esc(value['unit']) if value else '–'}</td><td>{esc(value['read_on']) if value else '–'}</td><td>{variable}</td><td>{base_fee}</td><td><strong>{cost}</strong></td><td>{advance}</td><td>{balance_html if key in ('grid_import', 'gas', 'water', 'wastewater') else '–'}</td><td>{projected_html}</td><td>{saving_html}</td></tr>"
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
            tariffrows += f"""<tr><td><strong>{label}</strong></td><td>{esc(tariff['provider'] or '–')}</td><td>{esc(tariff['valid_from'])}</td><td>{esc(tariff['valid_to'] or 'offen')}</td><td>{price}</td><td>{factor}</td><td>{fmt_money(tariff['base_fee_monthly'])}/Monat</td><td>{fmt_money(tariff['current_advance'])}/Monat<br><span class="muted">ab {esc(tariff['current_advance_from'])}</span></td><td>am {tariff['payment_day']}.<br><span class="muted">{esc(tariff['payment_account'] or 'Standardkonto in FinanzLab')}</span></td><td><div class="actions"><a href="/energy/tariffs/{tariff['id']}/edit">Bearbeiten</a><form method="post" action="/energy/tariffs/{tariff['id']}/delete" onsubmit="return confirm('Tarifzeitraum wirklich löschen?')"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button style="border:0;background:none;color:#ff8e98;cursor:pointer">Löschen</button></form></div></td></tr>"""
        anomaly_items = [f"{METRICS[key]['label']}: {count}" for key, count in invalid_counts.items() if key in METRICS]
        anomaly_html = f'<div class="notice warn">Automatische Plausibilitätsprüfung: {esc(", ".join(anomaly_items))} unplausible Werte werden nicht mitgerechnet.</div>' if anomaly_items else ""
        readiness = '<span class="good-dot"></span> eingerichtet' if ha_ready() else '<span class="warning-dot"></span> Token/URL noch eintragen'
        default_start = date(date.today().year, 1, 1).isoformat()
        csrf = csrf_for(token)
        manual_forms = ""
        for metric in ("grid_import", "gas", "water"):
            cfg = METRICS[metric]
            example = "12.345,67" if metric == "grid_import" else "1.234,567"
            manual_forms += f"""<form class="card" method="post" action="/energy/readings/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="metric" value="{metric}"><div class="section-head"><h2>{cfg['icon']} {esc(cfg['label'])}</h2><span class="badge">manuell</span></div><div class="field"><label>Ablesedatum</label><input type="date" name="read_on" value="{date.today().isoformat()}" max="{date.today().isoformat()}" required></div><div class="field" style="margin-top:12px"><label>Zählerstand in {esc(cfg['unit'])}</label><input inputmode="decimal" name="total_value" placeholder="z. B. {example}" required></div><button class="btn" style="margin-top:16px">Zählerstand speichern</button></form>"""
        body = f"""<div class="topbar"><div><h1>Energie</h1><div class="subtitle">Tägliche Zählerstände aus Home Assistant · automatisch um {scheduled_hour:02d}:{scheduled_minute:02d} Uhr · {readiness}</div></div><form method="post" action="/energy/sync"><input type="hidden" name="csrf" value="{csrf}"><button class="btn">Jetzt synchronisieren</button></form></div>{anomaly_html}
        <div class="card"><div class="table-wrap"><table><thead><tr><th>Messgröße</th><th>Datenquelle</th><th>Letzter Stand</th><th>Datum</th><th>Verbrauchskosten</th><th>Grundpreis</th><th>Gesamtkosten</th><th>Abschläge</th><th>Zwischenstand</th><th>Hochrechnung</th><th>PV-Ersparnis</th></tr></thead><tbody>{rows}</tbody></table></div><p class="muted" style="padding:0 28px 22px">Verbrauchskosten + Grundpreis = Gesamtkosten. Die Hochrechnung verwendet den bisherigen durchschnittlichen Tagesverbrauch bis zum Tarifende. Die PV-Ersparnis wird separat mit dem gültigen Strom-Arbeitspreis berechnet und reduziert diese Kosten nicht.</p></div>
        <section class="section"><div class="section-head"><div><h2>Zählerstände manuell erfassen</h2><div class="muted">Auch rückwirkend möglich. Der Stand muss chronologisch zwischen dem vorherigen und dem nachfolgenden Zählerstand liegen.</div></div></div><div class="grid">{manual_forms}</div><p class="muted">Eine Eingabe für ein bereits vorhandenes Datum korrigiert diesen Wert. Danach werden Verbrauch, historische Tarifkosten und Salden automatisch neu berechnet.</p></section>
        <section class="section card"><div class="section-head"><h2>Letzte manuelle Wasserstände</h2></div><div class="table-wrap"><table><thead><tr><th>Datum</th><th>Zählerstand</th><th>Verbrauch seit davor</th></tr></thead><tbody>{waterrows or '<tr><td colspan="3" class="empty">Noch keine Wasserstände erfasst</td></tr>'}</tbody></table></div></section>
        <section class="section two"><div class="card"><div class="section-head"><h2>Historie einmalig importieren</h2></div><p class="muted">Übernimmt vorhandene tägliche Langzeitstatistiken aus Home Assistant. Ein erneuter Lauf aktualisiert dieselben Tage und erzeugt keine Duplikate.</p><form method="post" action="/energy/backfill"><input type="hidden" name="csrf" value="{csrf}"><div class="field"><label>Historie ab</label><input type="date" name="start_date" value="{default_start}" max="{date.today().isoformat()}" required></div><button class="btn" style="margin-top:18px">Historie importieren</button></form></div>
        <div class="card"><div class="section-head"><h2>Stromtarif hinzufügen</h2><span class="badge">{tariff_counts['grid_import']} von {TARIFF_LIMIT}</span></div><form method="post" action="/energy/tariffs/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="metric" value="grid_import"><div class="form-grid"><div class="field full"><label>Anbieter</label><input name="provider" maxlength="100" placeholder="z. B. Stadtwerke Musterstadt" required></div><div class="field"><label>Gültig von</label><input type="date" name="valid_from" required></div><div class="field"><label>Gültig bis</label><input type="date" name="valid_to"></div><div class="field"><label>Strompreis in Cent/kWh</label><input inputmode="decimal" name="price_per_kwh" placeholder="z. B. 32,90" required></div><div class="field"><label>Grundpreis in €/Monat</label><input inputmode="decimal" name="base_fee_monthly" placeholder="z. B. 12,50" required></div><div class="field full"><label>Abschlag in €/Monat</label><input inputmode="decimal" name="advance_monthly" placeholder="z. B. 95,00" required></div><div class="field"><label>Zahlungstag im Monat</label><input type="number" name="payment_day" min="1" max="31" value="1" required></div><div class="field"><label>Konto in FinanzLab</label><input name="payment_account" maxlength="100" placeholder="exakter Kontoname, z. B. Girokonto"></div></div><button class="btn" style="margin-top:18px" {'disabled' if tariff_counts['grid_import'] >= TARIFF_LIMIT else ''}>Stromtarif speichern</button>{'<p class="muted">Das Limit von fünf Stromtarifen ist erreicht. Lösche bei Bedarf einen alten Zeitraum.</p>' if tariff_counts['grid_import'] >= TARIFF_LIMIT else ''}</form></div></section>
        <section class="section card"><div class="section-head"><h2>Gastarif hinzufügen</h2><span class="badge">{tariff_counts['gas']} von {TARIFF_LIMIT}</span></div><form method="post" action="/energy/tariffs/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="metric" value="gas"><div class="form-grid"><div class="field full"><label>Anbieter</label><input name="provider" maxlength="100" placeholder="z. B. Stadtwerke Musterstadt" required></div><div class="field"><label>Gültig von</label><input type="date" name="valid_from" required></div><div class="field"><label>Gültig bis</label><input type="date" name="valid_to"></div><div class="field"><label>Gaspreis in Cent/kWh</label><input inputmode="decimal" name="price_per_kwh" placeholder="z. B. 10,90" required></div><div class="field"><label>Umrechnung kWh pro m³</label><input inputmode="decimal" name="kwh_per_unit" placeholder="laut Gasabrechnung, z. B. 10,42" required></div><div class="field"><label>Grundpreis in €/Monat</label><input inputmode="decimal" name="base_fee_monthly" placeholder="z. B. 14,00" required></div><div class="field"><label>Abschlag in €/Monat</label><input inputmode="decimal" name="advance_monthly" placeholder="z. B. 120,00" required></div><div class="field"><label>Zahlungstag im Monat</label><input type="number" name="payment_day" min="1" max="31" value="1" required></div><div class="field"><label>Konto in FinanzLab</label><input name="payment_account" maxlength="100" placeholder="exakter Kontoname, z. B. Girokonto"></div></div><p class="muted">Der Gaszähler liefert m³. Den periodenbezogenen Umrechnungsfaktor findest du auf der Gasabrechnung.</p><button class="btn" style="margin-top:8px" {'disabled' if tariff_counts['gas'] >= TARIFF_LIMIT else ''}>Gastarif speichern</button>{'<p class="muted">Das Limit von fünf Gastarifen ist erreicht. Lösche bei Bedarf einen alten Zeitraum.</p>' if tariff_counts['gas'] >= TARIFF_LIMIT else ''}</form></section>
        <section class="section card"><div class="section-head"><h2>Wassertarif hinzufügen</h2><span class="badge">{tariff_counts['water']} von {TARIFF_LIMIT}</span></div><form method="post" action="/energy/tariffs/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="metric" value="water"><div class="form-grid"><div class="field full"><label>Anbieter</label><input name="provider" maxlength="100" placeholder="z. B. Wasserverband Musterstadt" required></div><div class="field"><label>Gültig von</label><input type="date" name="valid_from" required></div><div class="field"><label>Gültig bis</label><input type="date" name="valid_to"></div><div class="field"><label>Verbrauchspreis in €/m³</label><input inputmode="decimal" name="price_per_kwh" placeholder="z. B. 4,2500" required></div><div class="field"><label>Grundpreis in €/Monat</label><input inputmode="decimal" name="base_fee_monthly" placeholder="z. B. 8,50" required></div><div class="field full"><label>Abschlag in €/Monat</label><input inputmode="decimal" name="advance_monthly" placeholder="z. B. 45,00" required></div><div class="field"><label>Zahlungstag im Monat</label><input type="number" name="payment_day" min="1" max="31" value="1" required></div><div class="field"><label>Konto in FinanzLab</label><input name="payment_account" maxlength="100" placeholder="exakter Kontoname, z. B. Girokonto"></div></div><p class="muted">Für vollständige variable Kosten kannst du Trinkwasser und Abwasser im Verbrauchspreis zusammenfassen.</p><button class="btn" style="margin-top:8px" {'disabled' if tariff_counts['water'] >= TARIFF_LIMIT else ''}>Wassertarif speichern</button>{'<p class="muted">Das Limit von fünf Wassertarifen ist erreicht. Lösche bei Bedarf einen alten Zeitraum.</p>' if tariff_counts['water'] >= TARIFF_LIMIT else ''}</form></section>
        <details class="section tariff-history"><summary><span>Gespeicherte Tarife anzeigen</span><span class="badge">Strom {tariff_counts['grid_import']}/{TARIFF_LIMIT} · Gas {tariff_counts['gas']}/{TARIFF_LIMIT} · Wasser {tariff_counts['water']}/{TARIFF_LIMIT}</span></summary><div class="details-body"><div class="table-wrap"><table><thead><tr><th>Messgröße</th><th>Anbieter</th><th>Von</th><th>Bis</th><th>Preis</th><th>Umrechnung</th><th>Grundpreis</th><th>Aktueller Abschlag</th><th>Zahlung</th><th></th></tr></thead><tbody>{tariffrows or '<tr><td colspan="10" class="empty">Noch keine Tarife hinterlegt</td></tr>'}</tbody></table></div></div></details>
        <section class="section card"><div class="section-head"><h2>Importprotokoll</h2></div><div class="table-wrap"><table><thead><tr><th>Zeit</th><th>Status</th><th>Meldung</th></tr></thead><tbody>{logrows or '<tr><td colspan="3" class="empty">Noch keine Synchronisierung</td></tr>'}</tbody></table></div></section>"""
        self.send_html(page("Energie", body, "/energy", notice, "Fehler" in notice))

    def save_energy_tariff(self, form):
        values = parse_energy_tariff(form)
        end_bound = values["valid_to"] or "9999-12-31"
        with connect() as db:
            count = db.execute("SELECT COUNT(*) c FROM energy_tariffs WHERE metric=?", (values["metric"],)).fetchone()["c"]
            if count >= TARIFF_LIMIT:
                label = {"grid_import": "Stromtarife", "gas": "Gastarife", "water": "Wassertarife", "wastewater": "Abwassertarife"}[values["metric"]]
                raise ValueError(f"Es können maximal {TARIFF_LIMIT} {label} gespeichert werden.")
            overlap = db.execute(
                """SELECT 1 FROM energy_tariffs
                   WHERE metric=? AND valid_from<=? AND COALESCE(valid_to,'9999-12-31')>=?
                   LIMIT 1""",
                (values["metric"], end_bound, values["valid_from"]),
            ).fetchone()
            if overlap:
                raise ValueError("Für diesen Zeitraum besteht bereits ein Tarif. Bitte Zeiträume lückenlos, aber ohne Überschneidung anlegen.")
            validate_tariff_timeline_neighbors(
                db, values["metric"], values["valid_from"], values["valid_to"]
            )
            db.execute(
                "INSERT INTO energy_tariffs(metric,provider,valid_from,valid_to,price_per_kwh,kwh_per_unit,base_fee_monthly,advance_monthly,payment_day,payment_interval_months,first_payment_date,payment_account) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (values["metric"], values["provider"], values["valid_from"], values["valid_to"], values["price"], values["factor"], values["base_fee"], values["advance"], values["payment_day"], values["payment_interval_months"], values["first_payment_date"], values["payment_account"]),
            )
        label = {"grid_import": "Stromtarif", "gas": "Gastarif", "water": "Wassertarif", "wastewater": "Abwassertarif"}[values["metric"]]
        self.redirect(f"/energy/{values['metric']}?notice=" + urllib.parse.quote(f"{label} wurde gespeichert."))

    def energy_tariff_edit_page(self, tariff_id, notice):
        token = self.cookie_token()
        with connect() as db:
            tariff = db.execute("SELECT * FROM energy_tariffs WHERE id=?", (tariff_id,)).fetchone()
            advance_changes = db.execute(
                "SELECT * FROM energy_advance_changes WHERE tariff_id=? ORDER BY valid_from,id",
                (tariff_id,),
            ).fetchall()
            payment_events = db.execute(
                "SELECT * FROM energy_payment_events WHERE tariff_id=? ORDER BY booked_on DESC,id DESC",
                (tariff_id,),
            ).fetchall()
            snapshots = db.execute(
                "SELECT * FROM energy_settlement_snapshots WHERE tariff_id=? ORDER BY period_to DESC,revision DESC,id DESC",
                (tariff_id,),
            ).fetchall()
        if not tariff:
            self.send_html(page("Nicht gefunden", '<div class="card empty">Tarif nicht gefunden.</div>'), 404)
            return
        label = {"grid_import": "Stromtarif", "gas": "Gastarif", "water": "Wassertarif", "wastewater": "Abwassertarif"}[tariff["metric"]]
        factor_field = f'<div class="field"><label>Umrechnung kWh pro m³</label><input inputmode="decimal" name="kwh_per_unit" value="{fmt_num(tariff["kwh_per_unit"], 4)}" required></div>' if tariff["metric"] == "gas" else ""
        if tariff["metric"] in ("water", "wastewater"):
            price_label = "Verbrauchspreis in €/m³"
            price_value = fmt_num(tariff["price_per_kwh"], 4)
        else:
            price_label = "Arbeitspreis in Cent/kWh"
            price_value = fmt_num(tariff["price_per_kwh"] * 100, 2)
        change_rows = "".join(
            f"""<tr><td>{esc(change['valid_from'])}</td><td>{fmt_money(change['advance_monthly'])} je Zahlung</td><td><form method="post" action="/energy/tariffs/{tariff_id}/advance/{change['id']}/delete" onsubmit="return confirm('Zahlungsänderung wirklich löschen?')"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button style="border:0;background:none;color:#ff8e98;cursor:pointer">Löschen</button></form></td></tr>"""
            for change in advance_changes
        )
        interval_options = "".join(f'<option value="{months}" {"selected" if months == tariff["payment_interval_months"] else ""}>{text}</option>' for months, text in ((1,"monatlich"),(3,"quartalsweise"),(6,"halbjährlich"),(12,"jährlich")))
        body = f"""<div class="topbar"><div><h1>{label} bearbeiten</h1><div class="subtitle">Vertragsdaten und historische Zahlungsänderungen verwalten</div></div></div><div class="card"><form method="post" action="/energy/tariffs/{tariff_id}/update"><input type="hidden" name="csrf" value="{csrf_for(token)}"><input type="hidden" name="metric" value="{esc(tariff['metric'])}"><div class="form-grid"><div class="field full"><label>Anbieter</label><input name="provider" maxlength="100" value="{esc(tariff['provider'])}" required></div><div class="field"><label>Gültig von</label><input type="date" name="valid_from" value="{esc(tariff['valid_from'])}" required></div><div class="field"><label>Gültig bis</label><input type="date" name="valid_to" value="{esc(tariff['valid_to'] or '')}"></div><div class="field"><label>{price_label}</label><input inputmode="decimal" name="price_per_kwh" value="{price_value}" required></div>{factor_field}<div class="field"><label>Grundpreis in €/Monat</label><input inputmode="decimal" name="base_fee_monthly" value="{fmt_num(tariff['base_fee_monthly'], 2)}" required></div><div class="field"><label>Ursprünglicher Betrag je Zahlung</label><input inputmode="decimal" name="advance_monthly" value="{fmt_num(tariff['advance_monthly'], 2)}" required></div><div class="field"><label>Zahlungsrhythmus</label><select name="payment_interval_months">{interval_options}</select></div><div class="field"><label>Zahlungstag</label><input type="number" name="payment_day" min="1" max="31" value="{tariff['payment_day']}" required></div><div class="field"><label>Erste Zahlung <span class="muted">(optional)</span></label><input type="date" name="first_payment_date" value="{esc(tariff['first_payment_date'] or '')}" min="{esc(tariff['valid_from'])}" {f'max="{esc(tariff["valid_to"])}"' if tariff['valid_to'] else ''}><span class="muted">Leer = automatisch berechnen.</span></div><div class="field full"><label>Konto in FinanzLab</label><input name="payment_account" maxlength="100" value="{esc(tariff['payment_account'])}" placeholder="exakter Kontoname, z. B. Girokonto"></div></div><p class="muted">Den ursprünglichen Betrag nur korrigieren, wenn er falsch erfasst wurde. Spätere Änderungen bitte unten mit Gültigkeitsmonat anlegen.</p><div class="actions" style="margin-top:18px"><button class="btn">Vertragsdaten speichern</button><a class="btn secondary" href="/energy/{tariff['metric']}">Zurück</a></div></form></div>
        <section class="section card"><div class="section-head"><div><h2>Zahlungsbetrag ändern, ohne den Vertrag neu anzulegen</h2><div class="muted">Frühere Zahlungen bleiben erhalten. Der neue Betrag gilt ab dem gewählten Datum für danach fällige Zahlungen.</div></div></div><form method="post" action="/energy/tariffs/{tariff_id}/advance/save"><input type="hidden" name="csrf" value="{csrf_for(token)}"><div class="form-grid"><div class="field"><label>Neuer Betrag gültig ab</label><input type="date" name="valid_from" value="{date.today().isoformat()}" min="{esc(tariff['valid_from'])}" {f'max="{esc(tariff["valid_to"])}"' if tariff['valid_to'] else ''} required></div><div class="field"><label>Neuer Betrag je Zahlung</label><input inputmode="decimal" name="advance_monthly" placeholder="z. B. 145,00" required></div></div><button class="btn" style="margin-top:18px">Zahlungsänderung speichern</button></form><div class="table-wrap" style="margin-top:22px"><table><thead><tr><th>Gültig ab</th><th>Zahlungsbetrag</th><th></th></tr></thead><tbody>{change_rows or '<tr><td colspan="3" class="empty">Noch keine spätere Zahlungsänderung erfasst</td></tr>'}</tbody></table></div></section>"""
        event_options = "".join(f'<option value="{key}">{esc(value)}</option>' for key, value in PAYMENT_EVENT_LABELS.items())
        event_rows = ""
        for event in payment_events:
            status_class = "ok" if event["confirmed"] and event["status"] not in ("cancelled", "reversed", "pending", "review") else "warn"
            status_label = "berücksichtigt" if status_class == "ok" and event["event_type"] != "suspension" else ("ausgesetzt" if event["event_type"] == "suspension" else "nicht berücksichtigt")
            cancel = ""
            if event["status"] not in ("cancelled", "reversed"):
                cancel = f'''<form method="post" action="/energy/tariffs/{tariff_id}/payments/{event['id']}/cancel" onsubmit="return confirm('Ereignis wirklich stornieren? Der Prüfverlauf bleibt erhalten.')"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button class="link-button danger-text">Stornieren</button></form>'''
            event_rows += f"""<tr><td>{esc(event['booked_on'])}</td><td>{esc(event['due_on'] or '–')}</td><td>{esc(PAYMENT_EVENT_LABELS.get(event['event_type'], event['event_type']))}</td><td class="{'negative' if float(event['amount']) < 0 else ''}">{fmt_money(event['amount'])}</td><td>{esc(event['source'])}<br><span class="muted">{esc(event['match_method'] or event['note'])}</span></td><td><span class="badge {status_class}">{status_label}</span></td><td>{cancel}</td></tr>"""
        snapshot_rows = "".join(
            f"""<tr><td>{esc(item['period_from'])} bis {esc(item['period_to'])}</td><td>Revision {item['revision']}</td><td>{esc(item['title'] or 'Schlussabrechnung')}</td><td>{esc(item['created_at'])}</td><td><a href="/settlements/{item['id']}">Öffnen →</a></td></tr>"""
            for item in snapshots
        )
        end_default = tariff["valid_to"] or date.today().isoformat()
        body += f"""<section class="section card"><div class="section-head"><div><h2>Ist-Zahlungen und Sonderfälle</h2><div class="muted">Bestätigte Bankzahlungen kommen automatisch aus FinanzLab. Manuelle Einträge sind für Sonderfälle oder den Betrieb ohne Bankabgleich gedacht.</div></div><span class="badge">Plan ≠ Ist</span></div><details class="inline-details"><summary>Ein Ereignis manuell erfassen</summary><div class="details-body"><form method="post" action="/energy/tariffs/{tariff_id}/payments/save"><input type="hidden" name="csrf" value="{csrf_for(token)}"><div class="form-grid"><div class="field"><label>Art</label><select name="event_type">{event_options}</select></div><div class="field"><label>Buchungsdatum</label><input type="date" name="booked_on" value="{date.today().isoformat()}" required></div><div class="field"><label>Zugehörige Fälligkeit <span class="muted">(optional)</span></label><input type="date" name="due_on"></div><div class="field"><label>Betrag in €</label><input inputmode="decimal" name="amount" value="0,00" required><span class="muted">Rücklastschrift und Guthabenauszahlung werden automatisch negativ gerechnet; Korrekturen dürfen ein Vorzeichen haben.</span></div><div class="field full"><label>Notiz</label><input name="note" maxlength="500" placeholder="z. B. Abschlag im Juni ausgesetzt"></div></div><button class="btn" style="margin-top:18px">Ereignis erfassen</button></form></div></details><div class="table-wrap" style="margin-top:18px"><table><thead><tr><th>Gebucht</th><th>Fälligkeit</th><th>Art</th><th>Wirkung</th><th>Quelle</th><th>Status</th><th></th></tr></thead><tbody>{event_rows or '<tr><td colspan="7" class="empty">Noch keine Ist-Zahlungen erfasst oder aus FinanzLab abgeglichen.</td></tr>'}</tbody></table></div></section>
        <section class="section card">
<div class="section-head">
<div>
<h2>Schlussabrechnung fixieren</h2>
<div class="muted">
Speichert die Werte der echten Schlussrechnung zusätzlich zur EnergieLab-Berechnung als unveränderliche Revision mit SHA-256-Prüfsumme.
</div>
</div>
<span class="badge ok">revisionssicher</span>
</div>

<form method="post"
      action="/energy/tariffs/{tariff_id}/settlements/finalize">

<input type="hidden"
       name="csrf"
       value="{csrf_for(token)}">

<div class="form-grid">

<div class="field">
<label>Abrechnung von</label>
<input type="date"
       name="period_from"
       value="{esc(tariff['valid_from'])}"
       min="{esc(tariff['valid_from'])}"
       required>
</div>

<div class="field">
<label>bis</label>
<input type="date"
       name="period_to"
       value="{esc(end_default)}"
       {f'max="{esc(tariff["valid_to"])}"' if tariff['valid_to'] else ''}
       required>
</div>

<div class="field">
<label>
Rechnungsdatum
<span class="muted">(optional)</span>
</label>
<input type="date"
       name="invoice_date">
</div>

<div class="field">
<label>Verbrauch laut Schlussrechnung</label>
<input inputmode="decimal"
       name="supplier_consumption"
       placeholder="kWh bei Strom/Gas · m³ bei Wasser/Abwasser"
       required>
</div>

<div class="field">
<label>
Gasmenge laut Rechnung in m³
<span class="muted">(nur Gas, optional)</span>
</label>
<input inputmode="decimal"
       name="supplier_meter_consumption">
</div>

<div class="field">
<label>
Zählerstand Anfang
<span class="muted">(optional)</span>
</label>
<input inputmode="decimal"
       name="meter_start">
</div>

<div class="field">
<label>
Zählerstand Ende
<span class="muted">(optional)</span>
</label>
<input inputmode="decimal"
       name="meter_end">
</div>

<div class="field">
<label>Rechnungsbetrag laut Schlussrechnung in €</label>
<input inputmode="decimal"
       name="invoice_total_cost"
       id="final-invoice-total-{tariff_id}"
       placeholder="z. B. 1240,50"
       required>
</div>

<div class="field">
<label>Gezahlte / berücksichtigte Abschläge in €</label>
<input inputmode="decimal"
       name="invoice_paid"
       id="final-invoice-paid-{tariff_id}"
       placeholder="wird aus dem Zahlungsplan übernommen"
       required>

<div class="actions"
     style="margin-top:8px">
<button type="button"
        class="btn secondary"
        data-use-plan>
Aus Zahlungsplan übernehmen
</button>
</div>

<span class="muted"
      data-plan-info>
Zahlungsplan wird berechnet …
</span>
</div>

<div class="field full">
<label>Ergebnis der Schlussrechnung</label>

<div class="card"
     style="padding:14px;margin-top:4px"
     data-settlement-result>
Rechnungsbetrag und Abschläge eingeben.
</div>

<span class="muted">
Automatische Berechnung:
Abschläge − Rechnungsbetrag.
</span>
</div>

<div class="field full">
<label>
Bezeichnung
<span class="muted">(optional)</span>
</label>
<input name="title"
       maxlength="160"
       placeholder="z. B. Schlussrechnung 2026">
</div>

<div class="field full">
<label class="checks">
<input type="checkbox"
       name="close_contract"
       value="1"
       checked>
Vertragszeitraum mit dieser Schlussrechnung beenden
</label>

<span class="muted">
Setzt das Vertragsende auf den letzten Abrechnungstag.
Bei einer reinen Jahresabrechnung eines weiterlaufenden
Vertrags den Haken entfernen.
</span>
</div>

</div>

<button class="btn"
        style="margin-top:18px">
Schlussabrechnung unveränderlich fixieren
</button>

</form>

<script>
(() => {{
  const form = document.querySelector(
    'form[action="/energy/tariffs/{tariff_id}/settlements/finalize"]'
  );

  if (!form) return;

  const fromInput =
    form.querySelector('[name="period_from"]');

  const toInput =
    form.querySelector('[name="period_to"]');

  const totalInput =
    form.querySelector('[name="invoice_total_cost"]');

  const paidInput =
    form.querySelector('[name="invoice_paid"]');

  const planInfo =
    form.querySelector('[data-plan-info]');

  const resultBox =
    form.querySelector('[data-settlement-result]');

  const usePlanButton =
    form.querySelector('[data-use-plan]');

  let manualPaid = false;

  const parseNumber = (raw) => {{
    let value = String(raw || "")
      .trim()
      .replace(/\\s/g, "");

    if (!value) return null;

    if (value.includes(",") && value.includes(".")) {{
      if (
        value.lastIndexOf(",")
        > value.lastIndexOf(".")
      ) {{
        value = value
          .replace(/\\./g, "")
          .replace(",", ".");
      }} else {{
        value = value.replace(/,/g, "");
      }}
    }} else if (value.includes(",")) {{
      value = value.replace(",", ".");
    }}

    const number = Number(value);

    return Number.isFinite(number)
      ? number
      : null;
  }};

  const formatMoney = (value) =>
    new Intl.NumberFormat(
      "de-DE",
      {{
        style: "currency",
        currency: "EUR"
      }}
    ).format(value);

  const updateBalance = () => {{
    const total = parseNumber(totalInput.value);
    const paid = parseNumber(paidInput.value);

    if (total === null || paid === null) {{
      resultBox.textContent =
        "Rechnungsbetrag und Abschläge eingeben.";
      return;
    }}

    const balance =
      Math.round((paid - total) * 100) / 100;

    if (balance > 0.004) {{
      resultBox.textContent =
        "Erstattung / Guthaben: "
        + formatMoney(balance);
    }} else if (balance < -0.004) {{
      resultBox.textContent =
        "Nachzahlung: "
        + formatMoney(Math.abs(balance));
    }} else {{
      resultBox.textContent =
        "Ausgeglichen: 0,00 €";
    }}
  }};

  const loadPlan = (force) => {{
    if (!fromInput.value || !toInput.value) {{
      return;
    }}

    planInfo.textContent =
      "Zahlungsplan wird berechnet …";

    const url =
      "/api/energy/tariffs/{tariff_id}/planned-payments"
      + "?from="
      + encodeURIComponent(fromInput.value)
      + "&to="
      + encodeURIComponent(toInput.value);

    fetch(url)
      .then((response) => response.json())
      .then((data) => {{
        if (data.error) {{
          planInfo.textContent =
            "Zahlungsplan konnte nicht berechnet werden: "
            + data.error;
          return;
        }}

        const amount =
          Number(data.amount || 0);

        const count =
          Number(data.count || 0);

        planInfo.textContent =
          "Zahlungsplan: "
          + formatMoney(amount)
          + " · "
          + count
          + " Abschlag"
          + (count === 1 ? "" : "e");

        if (force || !manualPaid) {{
          paidInput.value =
            amount
              .toFixed(2)
              .replace(".", ",");

          manualPaid = false;
          updateBalance();
        }}
      }})
      .catch(() => {{
        planInfo.textContent =
          "Zahlungsplan konnte nicht geladen werden.";
      }});
  }};

  paidInput.addEventListener(
    "input",
    () => {{
      manualPaid = true;
      updateBalance();
    }}
  );

  totalInput.addEventListener(
    "input",
    updateBalance
  );

  fromInput.addEventListener(
    "change",
    () => {{
      manualPaid = false;
      loadPlan(true);
    }}
  );

  toInput.addEventListener(
    "change",
    () => {{
      manualPaid = false;
      loadPlan(true);
    }}
  );

  usePlanButton.addEventListener(
    "click",
    () => {{
      manualPaid = false;
      loadPlan(true);
    }}
  );

  loadPlan(true);
  updateBalance();
}})();
</script>

<div class="table-wrap"
     style="margin-top:22px">

<table>

<thead>
<tr>
<th>Zeitraum</th>
<th>Stand</th>
<th>Bezeichnung</th>
<th>Fixiert am</th>
<th></th>
</tr>
</thead>

<tbody>
{snapshot_rows or '<tr><td colspan="5" class="empty">Noch keine Schlussabrechnung fixiert.</td></tr>'}
</tbody>

</table>
</div>
</section>"""
        self.send_html(page(f"{label} bearbeiten", body, f"/energy/{tariff['metric']}", notice))


    def finalize_settlement(self, tariff_id, form):
        period_from = str(form.get("period_from", "")).strip()
        period_to = str(form.get("period_to", "")).strip()
        title = str(form.get("title", "")).strip()
        invoice_date = str(form.get("invoice_date", "")).strip()
        close_contract = str(form.get("close_contract", "")) == "1"

        if invoice_date:
            try:
                date.fromisoformat(invoice_date)
            except ValueError:
                raise ValueError("Das Rechnungsdatum ist ungültig.")

        def form_num(name, label, required=False, allow_negative=False):
            raw_value = str(form.get(name, "")).strip()
            if not raw_value:
                if required:
                    raise ValueError(f"{label} ist erforderlich.")
                return None

            value = parse_num(raw_value)
            if value is None:
                raise ValueError(f"{label} ist ungültig.")

            value = float(value)

            if not allow_negative and value < 0:
                raise ValueError(f"{label} darf nicht negativ sein.")

            return value

        supplier_consumption = form_num(
            "supplier_consumption",
            "Verbrauch laut Schlussrechnung",
            required=True,
        )

        supplier_meter_consumption = form_num(
            "supplier_meter_consumption",
            "Gasmenge laut Schlussrechnung",
        )

        meter_start = form_num(
            "meter_start",
            "Zählerstand Anfang",
        )

        meter_end = form_num(
            "meter_end",
            "Zählerstand Ende",
        )

        invoice_total_cost = form_num(
            "invoice_total_cost",
            "Gesamtkosten laut Schlussrechnung",
            required=True,
        )

        invoice_paid = form_num(
            "invoice_paid",
            "Berücksichtigte Abschläge/Zahlungen",
            required=True,
        )

        if (
            meter_start is not None
            and meter_end is not None
            and meter_end < meter_start
        ):
            raise ValueError(
                "Der Endzählerstand darf nicht kleiner als der Anfangszählerstand sein."
            )

        with connect() as db:
            tariff = db.execute(
                "SELECT * FROM energy_tariffs WHERE id=?",
                (tariff_id,),
            ).fetchone()

            if not tariff:
                raise ValueError("Tarif nicht gefunden.")

            payload = settlement_snapshot_payload(
                db,
                tariff_id,
                period_from,
                period_to,
            )

            planned_paid_advances = round(
                float(
                    payload["settlement"]["plannedPayments"]
                    or 0
                ),
                2,
            )

            invoice_total_cost = round(
                float(invoice_total_cost),
                2,
            )

            invoice_paid = round(
                float(invoice_paid),
                2,
            )

            # EnergieLab-Konvention:
            # positiv = Erstattung/Guthaben
            # negativ = Nachzahlung
            invoice_balance = round(
                invoice_paid - invoice_total_cost,
                2,
            )

            paid_advances_source = (
                "plan"
                if abs(
                    invoice_paid
                    - planned_paid_advances
                ) < 0.005
                else "manual"
            )

            metric = tariff["metric"]

            supplier_unit = (
                "kWh"
                if metric in ("grid_import", "gas")
                else "m³"
            )

            if (
                metric == "gas"
                and supplier_meter_consumption is None
                and meter_start is not None
                and meter_end is not None
            ):
                supplier_meter_consumption = meter_end - meter_start

            calculated_meter_amount = float(
                payload["consumption"]["amount"] or 0
            )
            calculated_meter_unit = payload["consumption"]["unit"]

            if metric == "gas":
                calculated_billing_amount = (
                    calculated_meter_amount
                    * float(tariff["kwh_per_unit"] or 0)
                )
                calculated_billing_unit = "kWh"
            else:
                calculated_billing_amount = calculated_meter_amount
                calculated_billing_unit = calculated_meter_unit

            payload["schemaVersion"] = "3"

            payload["supplierInvoice"] = {
                "invoiceDate": invoice_date or None,

                "consumption": {
                    "amount": supplier_consumption,
                    "unit": supplier_unit,
                },

                "meterConsumption": (
                    {
                        "amount": supplier_meter_consumption,
                        "unit": "m³",
                    }
                    if (
                        metric == "gas"
                        and supplier_meter_consumption is not None
                    )
                    else None
                ),

                "meterStart": meter_start,
                "meterEnd": meter_end,

                "totalCost": invoice_total_cost,

                # EnergieLab-Vorschlag aus dem Zahlungsplan
                "plannedPaidAdvances": planned_paid_advances,

                # Auf der echten Schlussrechnung berücksichtigter Betrag.
                # Im Formular automatisch vorbelegt, aber überschreibbar.
                "paidAdvances": invoice_paid,
                "paidAdvancesSource": paid_advances_source,

                # EnergieLab-Konvention:
                # positiv = Erstattung
                # negativ = Nachzahlung
                "balance": invoice_balance,

                "balanceConvention":
                    "positive=refund,negative=additional_payment",
            }

            payload["calculatedBillingConsumption"] = {
                "amount": calculated_billing_amount,
                "unit": calculated_billing_unit,
            }

            payload["comparison"] = {
                "billingConsumptionDifference":
                    supplier_consumption
                    - calculated_billing_amount,

                "totalCostDifference":
                    invoice_total_cost
                    - float(
                        payload["settlement"]["totalCost"] or 0
                    ),

                "balanceDifference":
                    invoice_balance
                    - float(
                        payload["settlement"]["balance"] or 0
                    ),
            }

            if close_contract:
                invalid_change = db.execute(
                    """
                    SELECT 1
                    FROM energy_advance_changes
                    WHERE tariff_id=?
                      AND valid_from>?
                    LIMIT 1
                    """,
                    (tariff_id, period_to),
                ).fetchone()

                if invalid_change:
                    raise ValueError(
                        "Nach dem gewählten Vertragsende existiert noch "
                        "eine Abschlagsänderung. Bitte diese zuerst "
                        "korrigieren oder löschen."
                    )

                overlapping_tariff = db.execute(
                    """
                    SELECT 1
                    FROM energy_tariffs
                    WHERE id<>?
                      AND metric=?
                      AND valid_from<=?
                      AND COALESCE(valid_to,'9999-12-31')>=?
                    LIMIT 1
                    """,
                    (
                        tariff_id,
                        metric,
                        period_to,
                        period_to,
                    ),
                ).fetchone()

                if overlapping_tariff:
                    raise ValueError(
                        "Der gewählte Abschlusstag überschneidet sich "
                        "mit einem anderen Tarifzeitraum."
                    )

                db.execute(
                    """
                    UPDATE energy_tariffs
                    SET valid_to=?
                    WHERE id=?
                    """,
                    (period_to, tariff_id),
                )

                payload["contract"]["validTo"] = period_to
                payload["contract"]["closedByFinalSettlement"] = True

            else:
                payload["contract"]["closedByFinalSettlement"] = False

            revision = db.execute(
                """
                SELECT COALESCE(MAX(revision),0)+1 revision
                FROM energy_settlement_snapshots
                WHERE tariff_id=?
                  AND period_from=?
                  AND period_to=?
                """,
                (
                    tariff_id,
                    payload["period"]["from"],
                    payload["period"]["to"],
                ),
            ).fetchone()["revision"]

            payload["revision"] = revision

            raw = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

            digest = hashlib.sha256(
                raw.encode("utf-8")
            ).hexdigest()

            cur = db.execute(
                """
                INSERT INTO energy_settlement_snapshots(
                    tariff_id,
                    revision,
                    period_from,
                    period_to,
                    title,
                    payment_basis,
                    payload_json,
                    payload_sha256
                )
                VALUES(?,?,?,?,?,'planned',?,?)
                """,
                (
                    tariff_id,
                    revision,
                    payload["period"]["from"],
                    payload["period"]["to"],
                    title[:160],
                    raw,
                    digest,
                ),
            )

            snapshot_id = cur.lastrowid

        notice = (
            f"Schlussabrechnung als Revision {revision} "
            "unveränderlich fixiert."
        )

        if close_contract:
            notice += " Der Vertragszeitraum wurde beendet."

        self.redirect(
            f"/settlements/{snapshot_id}?notice="
            + urllib.parse.quote(notice)
        )


    def settlement_snapshot_page(self, snapshot_id):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )

        notice = str(
            query.get("notice", [""])[0]
        )

        with connect() as db:
            row = db.execute(
                """
                SELECT *
                FROM energy_settlement_snapshots
                WHERE id=?
                """,
                (snapshot_id,),
            ).fetchone()

        if not row:
            self.send_html(
                page(
                    "Nicht gefunden",
                    '<div class="card empty">'
                    'Schlussabrechnung nicht gefunden.'
                    '</div>',
                ),
                404,
            )
            return

        raw = row["payload_json"]
        payload = json.loads(raw)

        actual_digest = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

        verified = hmac.compare_digest(
            actual_digest,
            row["payload_sha256"],
        )

        contract = payload.get("contract", {})
        supplier = payload.get("supplierInvoice", {})
        supplier_consumption = (
            supplier.get("consumption") or {}
        )
        supplier_meter = (
            supplier.get("meterConsumption") or {}
        )

        calculated_consumption = (
            payload.get("calculatedBillingConsumption")
            or payload.get("consumption")
            or {}
        )

        settlement = payload.get("settlement", {})
        comparison = payload.get("comparison", {})

        metric = contract.get("metric", "")

        label = {
            "grid_import": "Strom",
            "gas": "Gas",
            "water": "Wasser",
            "wastewater": "Abwasser",
        }.get(
            metric,
            metric or "Energie",
        )

        def optional_num(value, digits=2):
            if value is None:
                return "–"

            return fmt_num(value, digits)

        def balance_text(value):
            if value is None:
                return "–"

            value = float(value)

            if value > 0:
                return (
                    f"Erstattung {fmt_money(value)}"
                )

            if value < 0:
                return (
                    f"Nachzahlung {fmt_money(abs(value))}"
                )

            return "ausgeglichen"

        gas_meter_row = ""

        if metric == "gas":
            gas_meter_row = (
                '<div class="row muted">'
                '<span>Gasmenge laut Rechnung</span>'
                f'<span>{optional_num(supplier_meter.get("amount"))} '
                'm³</span>'
                '</div>'
            )

        meter_rows = ""

        if (
            supplier.get("meterStart") is not None
            or supplier.get("meterEnd") is not None
        ):
            meter_unit = (
                "kWh"
                if metric == "grid_import"
                else "m³"
            )

            meter_rows = (
                '<div class="row muted">'
                '<span>Zählerstand Anfang</span>'
                f'<span>{optional_num(supplier.get("meterStart"))} '
                f'{meter_unit}</span>'
                '</div>'

                '<div class="row muted">'
                '<span>Zählerstand Ende</span>'
                f'<span>{optional_num(supplier.get("meterEnd"))} '
                f'{meter_unit}</span>'
                '</div>'
            )

        status_class = (
            "ok"
            if verified
            else "warn"
        )

        status_text = (
            "SHA-256 geprüft"
            if verified
            else "Prüfsumme stimmt NICHT"
        )

        body = f"""
        <div class="topbar">
          <div>
            <h1>Schlussabrechnung · {esc(label)}</h1>
            <div class="subtitle">
              {esc(contract.get('provider') or 'Tarif')}
              ·
              {esc(payload.get('period', {}).get('from', ''))}
              bis
              {esc(payload.get('period', {}).get('to', ''))}
            </div>
          </div>

          <a class="btn secondary"
             href="/energy/tariffs/{row['tariff_id']}/edit">
             Zum Vertrag
          </a>
        </div>

        <section class="section card">
          <div class="section-head">
            <div>
              <h2>{esc(row['title'] or 'Schlussabrechnung')}</h2>
              <div class="muted">
                Revision {row['revision']}
                · fixiert am {esc(row['created_at'])}
              </div>
            </div>

            <span class="badge {status_class}">
              {status_text}
            </span>
          </div>

          <div class="muted"
               style="word-break:break-all">
            SHA-256: {esc(row['payload_sha256'])}
          </div>
        </section>

        <section class="section two">

          <div class="card settlement-card">
            <h2>Schlussrechnung des Anbieters</h2>

            <div class="settlement-block">

              <div class="row muted">
                <span>Rechnungsdatum</span>
                <span>
                  {esc(supplier.get('invoiceDate') or '–')}
                </span>
              </div>

              <div class="row">
                <span>Verbrauch</span>
                <strong>
                  {optional_num(
                      supplier_consumption.get('amount')
                  )}
                  {esc(
                      supplier_consumption.get('unit') or ''
                  )}
                </strong>
              </div>

              {gas_meter_row}

              {meter_rows}

              <div class="row muted">
                <span>Gesamtkosten</span>
                <span>
                  {fmt_money(
                      supplier.get('totalCost') or 0
                  )}
                </span>
              </div>

              <div class="row muted">
                <span>berücksichtigte Abschläge</span>
                <span>
                  {fmt_money(
                      supplier.get('paidAdvances') or 0
                  )}
                </span>
              </div>

              <div class="row">
                <span>Endsaldo</span>
                <strong>
                  {balance_text(
                      supplier.get('balance')
                  )}
                </strong>
              </div>

            </div>
          </div>


          <div class="card settlement-card">
            <h2>EnergieLab-Berechnung</h2>

            <div class="settlement-block">

              <div class="row">
                <span>Verbrauch</span>
                <strong>
                  {optional_num(
                      calculated_consumption.get('amount')
                  )}
                  {esc(
                      calculated_consumption.get('unit') or ''
                  )}
                </strong>
              </div>

              <div class="row muted">
                <span>Gesamtkosten</span>
                <span>
                  {fmt_money(
                      settlement.get('totalCost') or 0
                  )}
                </span>
              </div>

              <div class="row muted">
                <span>Abschläge laut Zahlungsplan</span>
                <span>
                  {fmt_money(
                      settlement.get('plannedPayments') or 0
                  )}
                </span>
              </div>

              <div class="row">
                <span>Saldo</span>
                <strong>
                  {balance_text(
                      settlement.get('balance')
                  )}
                </strong>
              </div>

            </div>
          </div>

        </section>


        <section class="section card">
          <div class="section-head">
            <h2>Abweichungen Anbieter − EnergieLab</h2>
          </div>

          <div class="settlement-block">

            <div class="row">
              <span>Verbrauch</span>
              <strong>
                {optional_num(
                    comparison.get(
                        'billingConsumptionDifference'
                    )
                )}
                {esc(
                    calculated_consumption.get('unit') or ''
                )}
              </strong>
            </div>

            <div class="row">
              <span>Gesamtkosten</span>
              <strong>
                {fmt_money(
                    comparison.get(
                        'totalCostDifference'
                    ) or 0
                )}
              </strong>
            </div>

            <div class="row">
              <span>Saldo</span>
              <strong>
                {fmt_money(
                    comparison.get(
                        'balanceDifference'
                    ) or 0
                )}
              </strong>
            </div>

          </div>
        </section>
        """

        self.send_html(
            page(
                "Schlussabrechnung",
                body,
                "/energy",
                notice,
            )
        )

    def save_advance_change(self, tariff_id, form):
        valid_from = parse_iso_date(str(form.get("valid_from", "")))
        valid_from_text = valid_from.isoformat() if hasattr(valid_from, "isoformat") else str(valid_from or "")
        if valid_from_text and valid_from_text <= date.today().isoformat():
            raise ValueError("Abschlagsänderungen dürfen nur für zukünftige Zeiträume gespeichert werden.")
        advance = parse_num(form.get("advance_monthly"))
        if not valid_from or advance is None or advance < 0:
            raise ValueError("Bitte Gültigkeitsmonat und einen nichtnegativen Zahlungsbetrag eingeben.")
        with connect() as db:
            tariff = db.execute("SELECT * FROM energy_tariffs WHERE id=?", (tariff_id,)).fetchone()
            if not tariff:
                raise ValueError("Tarif nicht gefunden.")
            if valid_from <= tariff["valid_from"]:
                raise ValueError("Eine spätere Abschlagsänderung muss nach dem Vertragsbeginn liegen. Den ursprünglichen Wert kannst du oben korrigieren.")
            if tariff["valid_to"] and valid_from > tariff["valid_to"]:
                raise ValueError("Das Wirksamkeitsdatum liegt nach dem Vertragsende.")
            db.execute(
                """INSERT INTO energy_advance_changes(tariff_id,valid_from,advance_monthly)
                   VALUES(?,?,?) ON CONFLICT(tariff_id,valid_from) DO UPDATE SET
                   advance_monthly=excluded.advance_monthly,created_at=CURRENT_TIMESTAMP""",
                (tariff_id, valid_from, advance),
            )
        self.redirect(f"/energy/tariffs/{tariff_id}/edit?notice=" + urllib.parse.quote("Zahlungsänderung wurde historisch gespeichert."))
    def delete_advance_change(self, tariff_id, change_id):
        with connect() as db:
            db.execute("DELETE FROM energy_advance_changes WHERE id=? AND tariff_id=?", (change_id, tariff_id))
        self.redirect(f"/energy/tariffs/{tariff_id}/edit?notice=" + urllib.parse.quote("Zahlungsänderung wurde gelöscht."))

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
            validate_tariff_timeline_neighbors(
                db, values["metric"], values["valid_from"], values["valid_to"], tariff_id
            )
            invalid_change = db.execute(
                """SELECT 1 FROM energy_advance_changes
                   WHERE tariff_id=? AND (valid_from<=? OR valid_from>?) LIMIT 1""",
                (tariff_id, values["valid_from"], end_bound),
            ).fetchone()
            if invalid_change:
                raise ValueError("Mindestens eine Abschlagsänderung liegt außerhalb des neuen Vertragszeitraums. Bitte diese zuerst korrigieren oder löschen.")
            db.execute(
                """UPDATE energy_tariffs
                   SET provider=?,valid_from=?,valid_to=?,price_per_kwh=?,kwh_per_unit=?,
                       base_fee_monthly=?,advance_monthly=?,payment_day=?,payment_interval_months=?,first_payment_date=?,payment_account=?
                   WHERE id=?""",
                (values["provider"], values["valid_from"], values["valid_to"], values["price"], values["factor"], values["base_fee"], values["advance"], values["payment_day"], values["payment_interval_months"], values["first_payment_date"], values["payment_account"], tariff_id),
            )
        self.redirect(f"/energy/{values['metric']}?notice=" + urllib.parse.quote("Tarif wurde aktualisiert."))

    def delete_energy_tariff(self, tariff_id):
        with connect() as db:
            row = db.execute("SELECT metric FROM energy_tariffs WHERE id=?", (tariff_id,)).fetchone()
            db.execute("DELETE FROM energy_tariffs WHERE id=?", (tariff_id,))
        metric = row["metric"] if row else "grid_import"
        self.redirect(f"/energy/{metric}?notice=" + urllib.parse.quote("Tarifzeitraum wurde gelöscht."))

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
            costs = {}
            for vehicle_id in {r["vehicle_id"] for r in rows}:
                summary = consumption_summary(db, vehicle_id)
                values.update(summary["rows"])
                costs.update(summary["row_costs"])
        table = ""
        for row in rows:
            consumption = f"{fmt_num(values[row['id']])} l/100 km" if row["id"] in values else "–"
            cost_per_km = f"{fmt_num(costs[row['id']],3)} €/km" if row["id"] in costs else "–"
            table += f"""<tr><td>{esc(datetime.strptime(row['fueled_on'],'%Y-%m-%d').strftime('%d.%m.%Y'))}</td><td><strong>{esc(row['vehicle_name'])}</strong><br><span class="badge">{esc(row['fluid'])}</span></td><td>{fmt_num(row['odometer'],0)} km</td><td>{fmt_num(row['liters'])} l</td><td>{fmt_num(row['unit_price'],3)} €/l</td><td>{fmt_money(row['total_price'])}</td><td>{esc(FILL_TYPES[row['fill_type']])}</td><td>{consumption}</td><td>{cost_per_km}</td><td><div class="actions"><a href="/fuelings/{row['id']}/edit">Bearbeiten</a><form method="post" action="/fuelings/{row['id']}/delete" onsubmit="return confirm('Tankung wirklich löschen?')"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button style="border:0;background:none;color:#ff8e98;cursor:pointer">Löschen</button></form></div></td></tr>"""
        body = f"""<div class="topbar"><div><h1>Tankungen</h1><div class="subtitle">Voll- und Teiltankungen mit automatischer Verbrauchs- und Kostenberechnung</div></div><a class="btn" href="/fuelings/new">+ Tankung erfassen</a></div><div class="card table-wrap"><table><thead><tr><th>Datum</th><th>Fahrzeug</th><th>Km-Stand</th><th>Menge</th><th>Preis</th><th>Gesamt</th><th>Art</th><th>Verbrauch</th><th>Tankkosten/km</th><th></th></tr></thead><tbody>{table or '<tr><td colspan="10" class="empty">Noch keine Tankungen. Importiere deine Spritmonitor-CSV.</td></tr>'}</tbody></table></div>"""
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

    def vehicle_costs_page(self, notice):
        token = self.cookie_token()
        with connect() as db:
            vehicles = db.execute("SELECT * FROM vehicles WHERE active=1 ORDER BY name").fetchall()
            expenses = db.execute(
                """SELECT e.*,v.name vehicle_name FROM vehicle_expenses e
                   JOIN vehicles v ON v.id=e.vehicle_id
                   ORDER BY e.incurred_on DESC,e.id DESC"""
            ).fetchall()
            summary_cards = ""
            for vehicle in vehicles:
                summary = vehicle_cost_summary(db, vehicle["id"])
                range_text = "Noch keine Fahrstrecke"
                if summary["distance"] > 0:
                    range_text = f"{fmt_num(summary['first_odometer'],0)} bis {fmt_num(summary['last_odometer'],0)} km"
                summary_cards += f"""<div class="card"><div class="section-head"><h2>{esc(vehicle['name'])}</h2><span class="badge">{fmt_num(summary['distance'],0)} km</span></div><div class="summary-grid"><div class="summary-box"><div class="muted">Tankkosten</div><div class="value">{fmt_money(summary['fuel_cost'])}</div></div><div class="summary-box"><div class="muted">Weitere Kosten</div><div class="value">{fmt_money(summary['extra_cost'])}</div></div><div class="summary-box"><div class="muted">Gesamtkosten</div><div class="value">{fmt_money(summary['total_cost'])}</div></div><div class="summary-box"><div class="muted">Gesamtkosten/km</div><div class="value">{fmt_num(summary['cost_per_km'],3)} €/km</div></div></div><p class="muted" style="margin-bottom:0">Berechnete Fahrstrecke: {range_text}</p></div>"""
        vehicle_options = "".join(f'<option value="{v["id"]}">{esc(v["name"])}</option>' for v in vehicles)
        category_options = "".join(f'<option value="{esc(category)}">{esc(category)}</option>' for category in VEHICLE_COST_CATEGORIES)
        rows = ""
        for expense in expenses:
            description = esc(expense["description"]) or '<span class="muted">–</span>'
            note = f'<br><span class="muted">{esc(expense["note"])}</span>' if expense["note"] else ""
            rows += f"""<tr><td>{esc(datetime.strptime(expense['incurred_on'],'%Y-%m-%d').strftime('%d.%m.%Y'))}</td><td><strong>{esc(expense['vehicle_name'])}</strong></td><td>{esc(expense['category'])}</td><td>{description}{note}</td><td>{fmt_money(expense['amount'])}</td><td><form method="post" action="/vehicle-costs/{expense['id']}/delete" onsubmit="return confirm('Kosten wirklich löschen?')"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button style="border:0;background:none;color:#ff8e98;cursor:pointer">Löschen</button></form></td></tr>"""
        body = f"""<div class="topbar"><div><h1>Fahrzeugkosten</h1><div class="subtitle">Tankkosten und weitere Fahrzeugkosten gemeinsam pro Kilometer auswerten</div></div></div><div class="grid">{summary_cards or '<div class="card empty">Bitte zuerst ein Fahrzeug anlegen.</div>'}</div><section class="section card"><div class="section-head"><div><h2>Weitere Kosten erfassen</h2><div class="muted">Zum Beispiel Versicherung, Kfz-Steuer, Reparatur, Wartung oder Reifen</div></div></div><form method="post" action="/vehicle-costs/save"><input type="hidden" name="csrf" value="{csrf_for(token)}"><div class="form-grid"><div class="field"><label>Fahrzeug</label><select name="vehicle_id" required>{vehicle_options}</select></div><div class="field"><label>Datum</label><input type="date" name="incurred_on" value="{date.today().isoformat()}" required></div><div class="field"><label>Kategorie</label><select name="category" required>{category_options}</select></div><div class="field"><label>Betrag in €</label><input inputmode="decimal" name="amount" placeholder="z. B. 749,00" required></div><div class="field full"><label>Beschreibung</label><input name="description" maxlength="160" placeholder="z. B. Jahresbeitrag 2026 oder Inspektion"></div><div class="field full"><label>Bemerkung</label><textarea name="note" maxlength="1000"></textarea></div></div><button class="btn" style="margin-top:18px" {'disabled' if not vehicles else ''}>Kosten speichern</button></form></section><section class="section card"><div class="section-head"><h2>Erfasste weitere Kosten</h2><span class="badge">{len(expenses)} Einträge</span></div><div class="table-wrap"><table><thead><tr><th>Datum</th><th>Fahrzeug</th><th>Kategorie</th><th>Beschreibung</th><th>Betrag</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan="6" class="empty">Noch keine weiteren Fahrzeugkosten erfasst.</td></tr>'}</tbody></table></div></section><p class="muted">Gesamtkosten/km = alle Tankkosten + alle hier erfassten Kosten, geteilt durch die Strecke zwischen dem kleinsten und größten Kilometerstand der Tankungen.</p>"""
        self.send_html(page("Fahrzeugkosten", body, "/vehicle-costs", notice))

    def save_vehicle_cost(self, form):
        vehicle_id = int(form.get("vehicle_id", 0))
        incurred_on = parse_iso_date(form.get("incurred_on"))
        category = str(form.get("category", "")).strip()
        amount = parse_num(form.get("amount"))
        description = str(form.get("description", "")).strip()
        note = str(form.get("note", "")).strip()
        if not incurred_on or category not in VEHICLE_COST_CATEGORIES or amount is None or amount <= 0:
            raise ValueError("Bitte Fahrzeug, Datum, Kategorie und einen positiven Betrag prüfen.")
        if len(description) > 160 or len(note) > 1000:
            raise ValueError("Beschreibung oder Bemerkung ist zu lang.")
        with connect() as db:
            if not db.execute("SELECT 1 FROM vehicles WHERE id=?", (vehicle_id,)).fetchone():
                raise ValueError("Fahrzeug nicht gefunden.")
            db.execute(
                "INSERT INTO vehicle_expenses(vehicle_id,incurred_on,category,description,amount,note) VALUES(?,?,?,?,?,?)",
                (vehicle_id, incurred_on, category, description, round(amount, 2), note),
            )
        self.redirect("/vehicle-costs?notice=" + urllib.parse.quote("Fahrzeugkosten wurden gespeichert."))

    def delete_vehicle_cost(self, expense_id):
        with connect() as db:
            db.execute("DELETE FROM vehicle_expenses WHERE id=?", (expense_id,))
        self.redirect("/vehicle-costs?notice=" + urllib.parse.quote("Fahrzeugkosten wurden gelöscht."))

    def contract_history_page(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        notice = str(query.get("notice", [""])[0])
        metric_filter = str(query.get("type", ["all"])[0])
        year_filter = str(query.get("year", ["all"])[0])

        metric_items = (
            ("all", "Alle", "🗂"),
            ("grid_import", "Strom", "⚡"),
            ("gas", "Gas", "🔥"),
            ("water", "Wasser", "💧"),
            ("wastewater", "Abwasser", "🚰"),
        )

        allowed_metrics = {
            item[0]
            for item in metric_items
        }

        if metric_filter not in allowed_metrics:
            metric_filter = "all"

        with connect() as db:
            rows = db.execute(
                """
                SELECT
                    t.id AS tariff_id,
                    t.metric,
                    t.provider,
                    t.valid_from,
                    t.valid_to,

                    s.id AS snapshot_id,
                    s.revision AS snapshot_revision,
                    s.period_from AS snapshot_period_from,
                    s.period_to AS snapshot_period_to,
                    s.title AS snapshot_title,
                    s.payload_json,
                    s.created_at AS snapshot_created_at

                FROM energy_tariffs t

                LEFT JOIN energy_settlement_snapshots s
                  ON s.id = (
                      SELECT s2.id
                      FROM energy_settlement_snapshots s2
                      WHERE s2.tariff_id=t.id
                      ORDER BY
                          s2.period_to DESC,
                          s2.revision DESC,
                          s2.id DESC
                      LIMIT 1
                  )

                WHERE t.valid_to IS NOT NULL
                  AND t.valid_to <= ?

                ORDER BY
                    t.valid_to DESC,
                    t.valid_from DESC,
                    t.id DESC
                """,
                (date.today().isoformat(),),
            ).fetchall()

        entries = []
        years = set()

        for row in rows:
            item = dict(row)
            payload = {}

            if item["payload_json"]:
                try:
                    payload = json.loads(
                        item["payload_json"]
                    )
                except Exception:
                    payload = {}

            contract = (
                payload.get("contract")
                or {}
            )

            supplier = (
                payload.get("supplierInvoice")
                or {}
            )

            settlement = (
                payload.get("settlement")
                or {}
            )

            metric = (
                contract.get("metric")
                or item["metric"]
            )

            provider = (
                contract.get("provider")
                or item["provider"]
                or "Unbekannter Anbieter"
            )

            period_from = (
                item["snapshot_period_from"]
                or contract.get("validFrom")
                or item["valid_from"]
            )

            period_to = (
                item["snapshot_period_to"]
                or contract.get("validTo")
                or item["valid_to"]
            )

            archive_year = str(
                period_to or ""
            )[:4]

            if archive_year:
                years.add(archive_year)

            consumption = (
                supplier.get("consumption")
                or payload.get(
                    "calculatedBillingConsumption"
                )
                or payload.get("consumption")
                or {}
            )

            gas_meter = (
                supplier.get("meterConsumption")
                or {}
            )

            total_cost = supplier.get(
                "totalCost"
            )

            if total_cost is None:
                total_cost = settlement.get(
                    "totalCost"
                )

            paid_advances = supplier.get(
                "paidAdvances"
            )

            if paid_advances is None:
                paid_advances = settlement.get(
                    "plannedPayments"
                )

            balance = supplier.get(
                "balance"
            )

            if balance is None:
                balance = settlement.get(
                    "balance"
                )

            entries.append({
                **item,
                "metric": metric,
                "provider_display": provider,
                "period_from": period_from,
                "period_to": period_to,
                "year": archive_year,
                "supplier": supplier,
                "consumption": consumption,
                "gas_meter": gas_meter,
                "total_cost": total_cost,
                "paid_advances": paid_advances,
                "balance": balance,
            })

        years = sorted(
            years,
            reverse=True,
        )

        if (
            year_filter != "all"
            and year_filter not in years
        ):
            year_filter = "all"

        def history_url(
            metric_value=None,
            year_value=None,
        ):
            metric_value = (
                metric_filter
                if metric_value is None
                else metric_value
            )

            year_value = (
                year_filter
                if year_value is None
                else year_value
            )

            params = []

            if metric_value != "all":
                params.append(
                    "type="
                    + urllib.parse.quote(
                        metric_value
                    )
                )

            if year_value != "all":
                params.append(
                    "year="
                    + urllib.parse.quote(
                        year_value
                    )
                )

            return (
                "/contract-history"
                + (
                    "?" + "&".join(params)
                    if params
                    else ""
                )
            )

        metric_tabs = ""

        for key, label, icon in metric_items:
            active_class = (
                "btn"
                if key == metric_filter
                else "btn secondary"
            )

            metric_tabs += (
                f'<a class="{active_class}" '
                f'href="{history_url(metric_value=key)}">'
                f'{icon} {esc(label)}</a>'
            )

        year_tabs = (
            f'<a class="'
            f'{"btn" if year_filter == "all" else "btn secondary"}'
            f'" href="{history_url(year_value="all")}">'
            f'Alle Jahre</a>'
        )

        for year in years:
            active_class = (
                "btn"
                if year == year_filter
                else "btn secondary"
            )

            year_tabs += (
                f'<a class="{active_class}" '
                f'href="{history_url(year_value=year)}">'
                f'{esc(year)}</a>'
            )

        visible = []

        for entry in entries:
            if (
                metric_filter != "all"
                and entry["metric"]
                != metric_filter
            ):
                continue

            if (
                year_filter != "all"
                and entry["year"]
                != year_filter
            ):
                continue

            visible.append(entry)

        grouped = {}

        for entry in visible:
            grouped.setdefault(
                entry["year"] or "Ohne Jahr",
                [],
            ).append(entry)

        label_by_metric = {
            "grid_import": "Strom",
            "gas": "Gas",
            "water": "Wasser",
            "wastewater": "Abwasser",
        }

        icon_by_metric = {
            "grid_import": "⚡",
            "gas": "🔥",
            "water": "💧",
            "wastewater": "🚰",
        }

        def balance_text(value):
            if value is None:
                return "–"

            value = float(value)

            if value > 0.004:
                return (
                    "Erstattung "
                    + fmt_money(value)
                )

            if value < -0.004:
                return (
                    "Nachzahlung "
                    + fmt_money(abs(value))
                )

            return "ausgeglichen"

        def money_or_dash(value):
            if value is None:
                return "–"

            return fmt_money(value)

        sections = ""

        for year in sorted(
            grouped,
            reverse=True,
        ):
            cards = ""

            for entry in grouped[year]:
                metric = entry["metric"]

                label = label_by_metric.get(
                    metric,
                    metric,
                )

                icon = icon_by_metric.get(
                    metric,
                    "⚡",
                )

                snapshot_id = entry[
                    "snapshot_id"
                ]

                if snapshot_id:
                    status = (
                        '<span class="badge ok">'
                        f'Revision '
                        f'{entry["snapshot_revision"]} '
                        '· fixiert'
                        '</span>'
                    )
                else:
                    status = (
                        '<span class="badge warn">'
                        'noch nicht abgerechnet'
                        '</span>'
                    )

                invoice_date = (
                    entry["supplier"].get(
                        "invoiceDate"
                    )
                )

                invoice_row = ""

                if invoice_date:
                    invoice_row = (
                        '<div class="row muted">'
                        '<span>Rechnungsdatum</span>'
                        f'<span>{esc(invoice_date)}</span>'
                        '</div>'
                    )

                consumption = entry[
                    "consumption"
                ]

                consumption_row = ""

                if (
                    consumption.get("amount")
                    is not None
                ):
                    consumption_row = (
                        '<div class="row">'
                        '<span>Verbrauch</span>'
                        '<strong>'
                        f'{fmt_num(consumption.get("amount"), 2)} '
                        f'{esc(consumption.get("unit") or "")}'
                        '</strong>'
                        '</div>'
                    )

                gas_row = ""

                if (
                    metric == "gas"
                    and entry["gas_meter"].get(
                        "amount"
                    ) is not None
                ):
                    gas_row = (
                        '<div class="row muted">'
                        '<span>Gasmenge</span>'
                        f'<span>'
                        f'{fmt_num(entry["gas_meter"]["amount"], 2)} '
                        'm³'
                        '</span>'
                        '</div>'
                    )

                if snapshot_id:
                    financial_rows = (
                        consumption_row
                        + gas_row
                        + invoice_row
                        + '<div class="row muted">'
                          '<span>Rechnungsbetrag</span>'
                          f'<span>'
                          f'{money_or_dash(entry["total_cost"])}'
                          '</span>'
                          '</div>'
                        + '<div class="row muted">'
                          '<span>berücksichtigte Abschläge</span>'
                          f'<span>'
                          f'{money_or_dash(entry["paid_advances"])}'
                          '</span>'
                          '</div>'
                        + '<div class="row">'
                          '<span>Endsaldo</span>'
                          f'<strong>'
                          f'{balance_text(entry["balance"])}'
                          '</strong>'
                          '</div>'
                    )
                else:
                    financial_rows = (
                        '<div class="muted">'
                        'Für diesen beendeten Vertrag ist '
                        'noch keine fixierte Schlussabrechnung '
                        'vorhanden.'
                        '</div>'
                    )

                actions = (
                    '<div class="actions" '
                    'style="margin-top:18px">'
                )

                if snapshot_id:
                    actions += (
                        '<a class="btn" '
                        f'href="/settlements/{snapshot_id}">'
                        'Schlussabrechnung öffnen'
                        '</a>'
                    )

                actions += (
                    '<a class="btn secondary" '
                    f'href="/energy/tariffs/'
                    f'{entry["tariff_id"]}/edit">'
                    'Vertrag öffnen'
                    '</a>'
                    '</div>'
                )

                cards += f"""
                <div class="card settlement-card">

                  <div class="section-head">
                    <div>
                      <h2>
                        {icon}
                        {esc(entry["provider_display"])}
                      </h2>

                      <div class="muted">
                        {esc(label)}
                        ·
                        {esc(entry["period_from"] or "")}
                        bis
                        {esc(entry["period_to"] or "")}
                      </div>
                    </div>

                    {status}
                  </div>

                  <div class="settlement-block">
                    {financial_rows}
                  </div>

                  {actions}
                </div>
                """

            sections += f"""
            <section class="section">

              <div class="section-head">
                <div>
                  <h2>{esc(year)}</h2>
                  <div class="muted">
                    Abgeschlossene Energieverträge
                  </div>
                </div>

                <span class="badge">
                  {len(grouped[year])}
                  {
                    "Vertrag"
                    if len(grouped[year]) == 1
                    else "Verträge"
                  }
                </span>
              </div>

              <div class="settlement-grid">
                {cards}
              </div>

            </section>
            """

        if not sections:
            sections = """
            <section class="section card empty">
              Für diese Auswahl wurden keine abgeschlossenen
              Verträge gefunden.
            </section>
            """

        body = f"""
        <div class="topbar">
          <div>
            <h1>Vertragshistorie</h1>

            <div class="subtitle">
              Abgeschlossene Verträge und fixierte
              Schlussabrechnungen
            </div>
          </div>
        </div>

        <section class="card">

          <div class="section-head">
            <div>
              <h2>Energieart</h2>
              <div class="muted">
                Verträge nach Bereich filtern
              </div>
            </div>
          </div>

          <div class="actions">
            {metric_tabs}
          </div>

          <div class="section-head"
               style="margin-top:22px">
            <div>
              <h2>Jahr</h2>
              <div class="muted">
                Zuordnung nach Vertrags- bzw.
                Abrechnungsende
              </div>
            </div>
          </div>

          <div class="actions">
            {year_tabs}
          </div>

        </section>

        {sections}
        """

        self.send_html(
            page(
                "Vertragshistorie",
                body,
                "/contract-history",
                notice,
            )
        )


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
        self.send_html(page("Import", body, "/settings", notice))

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
        self.send_html(page("Importvorschau", body, "/settings"))

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

    def compare_page(self, query):
        today = date.today()
        default_a_start, default_a_end = date(today.year, 1, 1), today
        try:
            default_b_start = default_a_start.replace(year=today.year - 1)
            default_b_end = default_a_end.replace(year=today.year - 1)
        except ValueError:
            default_b_start, default_b_end = date(today.year - 1, 1, 1), date(today.year - 1, 2, 28)
        starts = [parse_iso_date(query.get("a_start", [default_a_start.isoformat()])[0]), parse_iso_date(query.get("b_start", [default_b_start.isoformat()])[0])]
        ends = [parse_iso_date(query.get("a_end", [default_a_end.isoformat()])[0]), parse_iso_date(query.get("b_end", [default_b_end.isoformat()])[0])]
        if not all(starts + ends) or starts[0] > ends[0] or starts[1] > ends[1]:
            raise ValueError("Bitte gültige Vergleichszeiträume wählen.")
        summaries = []
        with connect() as db:
            for start_on, end_on in zip(starts, ends):
                consumption = {
                    metric: allocated_usage(
                        db,
                        reading_metric_for(metric),
                        start_on,
                        end_on,
                        "grid_import" if metric == "pv_self" else metric,
                    )[0]
                    for metric in METRICS
                }
                summaries.append((consumption, energy_finances(db, start_on, end_on)))
        rows = ""
        for metric in ("grid_import", "pv_self", "gas", "water", "wastewater"):
            cfg = METRICS[metric]
            a_consumption, b_consumption = summaries[0][0].get(metric, 0.0), summaries[1][0].get(metric, 0.0)
            delta = comparison_difference(a_consumption, b_consumption)
            if delta["percent"] is None:
                percent_text = "Prozent nicht berechenbar (A = 0)"
            else:
                percent_text = f'{delta["percent"]:+.2f} %'.replace(".", ",")
            delta_html = f'<span class="comparison-delta {delta["css"]}">{fmt_num(abs(delta["difference"]))} {esc(cfg["unit"])} {delta["label"]}<span class="comparison-percent">{percent_text}</span></span>'
            if metric == "pv_self":
                rows += f"<tr><td>{cfg['icon']} {esc(cfg['label'])}</td><td>{fmt_num(a_consumption)} {cfg['unit']}</td><td>{fmt_num(b_consumption)} {cfg['unit']}</td><td>{delta_html}</td><td colspan='8' class='muted'>PV wird nicht mit Versorgerkosten oder Abschlägen verrechnet.</td></tr>"
                continue
            a = summaries[0][1].get(metric, settlement_values())
            b = summaries[1][1].get(metric, settlement_values())
            rows += f"<tr><td>{cfg['icon']} <strong>{esc(cfg['label'])}</strong></td><td>{fmt_num(a_consumption)} {cfg['unit']}</td><td>{fmt_num(b_consumption)} {cfg['unit']}</td><td>{delta_html}</td><td>{fmt_money(a['variable'])}</td><td>{fmt_money(b['variable'])}</td><td>{fmt_money(a['base_fee'])}</td><td>{fmt_money(b['base_fee'])}</td><td>{fmt_money(a['cost'])}</td><td>{fmt_money(b['cost'])}</td><td>{fmt_money(a['balance'])}</td><td>{fmt_money(b['balance'])}</td></tr>"
        body = f"""<div class="topbar"><div><h1>Vergleich</h1><div class="subtitle">Verbrauch, Kosten und Abrechnungssaldo zweier frei wählbarer Zeiträume</div></div></div><div class="card"><form method="get"><div class="form-grid"><div class="field"><label>Zeitraum A von</label><input type="date" name="a_start" value="{starts[0]}"></div><div class="field"><label>bis</label><input type="date" name="a_end" value="{ends[0]}"></div><div class="field"><label>Zeitraum B von</label><input type="date" name="b_start" value="{starts[1]}"></div><div class="field"><label>bis</label><input type="date" name="b_end" value="{ends[1]}"></div></div><button class="btn" style="margin-top:18px">Vergleichen</button></form></div><section class="section card table-wrap"><table><thead><tr><th>Bereich</th><th>Verbrauch A</th><th>Verbrauch B</th><th>Differenz B − A</th><th>Verbrauchskosten A</th><th>Verbrauchskosten B</th><th>Grundpreis A</th><th>Grundpreis B</th><th>Gesamtkosten A</th><th>Gesamtkosten B</th><th>Guthaben/Nachzahlung A</th><th>Guthaben/Nachzahlung B</th></tr></thead><tbody>{rows}</tbody></table><p class="muted">Die Differenz zeigt Zeitraum B im Vergleich zu Zeitraum A. Saldo = tatsächlich angesetzte Abschläge − (Verbrauchskosten + Grundpreis). Die monatliche Grundgebühr wird für jeden berührten Kalendermonat vollständig angesetzt.</p></section>"""
        self.send_html(page("Vergleich", body, "/compare"))

    def settings_page(self, notice):
        token = self.cookie_token()
        hour, minute = sync_time()
        config = finanzlab_config()
        integration = finanzlab_sync_status()
        status_class = "ok" if integration["status"] == "ok" else "warn"
        status_label = {
            "ok": "Verbunden",
            "warning": "Hinweis",
            "error": "Fehler",
            "never": "Noch nie",
        }.get(integration["status"], integration["status"])
        status_text = (
            f"{integration['importedCount']} Zahlungen übernommen · "
            f"{integration['unresolvedCount']} noch zu prüfen"
            if integration["lastSyncAt"] else "Noch kein Zahlungsabgleich"
        )
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backups = sorted(BACKUP_DIR.glob("energylab-*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
        backup_rows = "".join(
            f"""<tr><td>{esc(item.name)}</td><td>{datetime.fromtimestamp(item.stat().st_mtime).strftime('%d.%m.%Y %H:%M')}</td><td>{item.stat().st_size/1048576:.1f} MB</td><td><div class="actions"><a href="/settings/backups/{urllib.parse.quote(item.name)}/download">Herunterladen</a><form method="post" action="/settings/backups/{urllib.parse.quote(item.name)}/restore"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button class="btn secondary" onclick="return confirm('Diese Sicherung wiederherstellen? Der aktuelle Stand wird vorher gesichert.')">Wiederherstellen</button></form></div></td></tr>"""
            for item in backups
        )
        body = f"""
        <div class="topbar"><div><h1>Einstellungen</h1><div class="subtitle">Import, Verbindung und Datensicherheit übersichtlich an einem Ort</div></div></div>
        <div class="grid settings-grid">
          <div class="card"><h2>Automatischer Import</h2><p class="muted">EnergyLab prüft Home Assistant und – falls eingerichtet – FinanzLab täglich zur gewählten Uhrzeit.</p><form method="post" action="/settings/sync-time"><input type="hidden" name="csrf" value="{csrf_for(token)}"><div class="field"><label>Tägliche Importzeit</label><input type="time" name="sync_time" value="{hour:02d}:{minute:02d}" required></div><button class="btn" style="margin-top:16px">Uhrzeit speichern</button></form><form method="post" action="/settings/import-now" style="margin-top:14px"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button class="btn secondary">Jetzt importieren</button></form><p class="muted">„Jetzt importieren“ übernimmt alle aktuell verfügbaren Messwerte und bestätigten Zahlungen.</p></div>
          <div class="card"><div class="section-head"><div><h2>FinanzLab-Zahlungen</h2><div class="muted">Optionaler Zahlungsabgleich; für Schlussabrechnungen nicht erforderlich</div></div><span class="badge {status_class}">{esc(status_label)}</span></div><form method="post" action="/settings/finanzlab"><input type="hidden" name="csrf" value="{csrf_for(token)}"><div class="field"><label>FinanzLab-Adresse</label><input type="url" name="finanzlab_base_url" value="{esc(config['financeLabBaseUrl'])}" placeholder="http://finanzlab:8798"></div><div class="field"><label>FinanzLab-Haushalt <span class="muted">(Name oder interne ID)</span></label><input name="finanzlab_household_id" value="{esc(config['householdId'])}" placeholder="z. B. Zuhause"></div><div class="field"><label>Zugriffsschlüssel <span class="muted">(optional)</span></label><input type="password" name="finanzlab_access_token" value="{esc(config['accessToken'])}" autocomplete="new-password"></div><button class="btn" style="margin-top:16px">Verbindung speichern</button></form><form method="post" action="/settings/finanzlab-sync" style="margin-top:14px"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button class="btn secondary" {'disabled' if not integration['configured'] else ''}>Zahlungen jetzt abgleichen</button></form><p class="muted">{esc(status_text)}{(' · zuletzt '+esc(integration['lastSyncAt'])) if integration['lastSyncAt'] else ''}</p></div>
          <div class="card"><h2>Excel-Export</h2><p class="muted">Enthält Übersicht, Messwerte, Tarife, Abschlagsverlauf sowie die für Abrechnungen verwendeten Werte.</p><a class="btn" href="/export/energylab.xlsx">XLSX herunterladen</a></div>
          <div class="card"><div class="section-head"><div><h2>Datenbanksicherungen</h2><div class="muted">Automatisch vor Updates und Wiederherstellungen</div></div><form method="post" action="/settings/backups/create"><input type="hidden" name="csrf" value="{csrf_for(token)}"><button class="btn secondary">Jetzt sichern</button></form></div><div class="table-wrap"><table><thead><tr><th>Datei</th><th>Erstellt</th><th>Größe</th><th></th></tr></thead><tbody>{backup_rows or '<tr><td colspan="4" class="empty">Noch keine lokale Sicherung.</td></tr>'}</tbody></table></div></div>
          <div class="card">
            <div class="section-head">
              <div>
                <h2>⇩ Spritmonitor-Import</h2>
                <div class="muted">
                  Tankungen aus einer Spritmonitor-CSV
                  prüfen und übernehmen.
                </div>
              </div>
              <span class="badge">Import</span>
            </div>

            <div class="actions">
              <a class="btn secondary"
                 href="/import">
                CSV-Import öffnen
              </a>
            </div>
          </div>
        </div>"""
        self.send_html(page("Einstellungen", body, "/settings", notice, "Fehler" in notice))

    def save_finanzlab_settings(self, form):
        base_url = str(form.get("finanzlab_base_url", "")).strip().rstrip("/")
        household_id = str(form.get("finanzlab_household_id", "")).strip()
        access_token = str(form.get("finanzlab_access_token", "")).strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ValueError("Bitte eine gültige FinanzLab-Adresse eingeben.")
        if bool(base_url) != bool(household_id):
            raise ValueError("FinanzLab-Adresse und Haushalts-ID müssen gemeinsam angegeben werden.")
        with connect() as db:
            set_meta("finanzlab_base_url", base_url, db)
            set_meta("finanzlab_household_id", household_id, db)
            set_meta("finanzlab_access_token", access_token, db)
        self.redirect("/settings?notice=" + urllib.parse.quote("FinanzLab-Verbindung wurde gespeichert."))

    def download_database_backup(self, filename):
        target = (BACKUP_DIR / Path(str(filename)).name).resolve()
        if target.parent != BACKUP_DIR.resolve() or not target.is_file() or not target.name.startswith("energylab-"):
            self.send_html(page("Nicht gefunden", '<div class="notice warn">Sicherung nicht gefunden.</div>'), 404)
            return
        self.send_bytes(target.read_bytes(), "application/vnd.sqlite3", headers={
            "Content-Disposition": f'attachment; filename="{target.name}"'
        })

    def export_xlsx(self):
        with connect() as db:
            finances = energy_finances(db)
            overview = [["Bereich", "Verbrauchskosten", "Grundpreis", "Gesamtkosten", "Gezahlte Abschläge", "Guthaben (+) / Nachzahlung (-)"]]
            for metric in ("grid_import", "gas", "water", "wastewater"):
                values = finances.get(metric, settlement_values())
                overview.append([METRICS[metric]["label"], values["variable"], values["base_fee"], values["cost"], values["advance"], values["balance"]])
            overview.append([])
            overview.append(["Rechenregel", "Guthaben/Nachzahlung = gezahlte Abschläge - (Verbrauchskosten + Grundpreis)"])
            readings = [["Messgröße", "Datum", "Zählerstand", "Verbrauch", "Einheit", "Gültig", "Ausschlussgrund", "Quelle"]]
            readings += [[r["metric"], r["read_on"], r["total_value"], r["delta_value"] if r["delta_value"] is not None else "", r["unit"], r["is_valid"], r["invalid_reason"], r["source"]] for r in db.execute("SELECT * FROM energy_readings ORDER BY metric,read_on,id")]
            tariffs = [["Messgröße", "Anbieter", "Gültig von", "Gültig bis", "Arbeitspreis €/kWh bzw. €/m³", "kWh je Einheit", "Grundpreis €/Monat", "Betrag je Zahlung", "Zahlungsrhythmus", "Zahlungstag", "Erste Zahlung", "Konto in FinanzLab"]]
            tariffs += [[r["metric"], r["provider"], r["valid_from"], r["valid_to"] or "", r["price_per_kwh"], r["kwh_per_unit"], r["base_fee_monthly"], r["advance_monthly"], payment_interval_label(r["payment_interval_months"]), r["payment_day"], r["first_payment_date"] or "automatisch", r["payment_account"]] for r in db.execute("SELECT * FROM energy_tariffs ORDER BY metric,valid_from,id")]
            advance_changes = [["Messgröße", "Anbieter", "Tarif gültig von", "Neuer Betrag gültig ab", "Betrag je Zahlung"]]
            advance_changes += [[r["metric"], r["provider"], r["tariff_from"], r["change_from"], r["advance_monthly"]] for r in db.execute(
                """SELECT t.metric,t.provider,t.valid_from tariff_from,c.valid_from change_from,c.advance_monthly
                   FROM energy_advance_changes c JOIN energy_tariffs t ON t.id=c.tariff_id
                   ORDER BY t.metric,c.valid_from,c.id"""
            )]
        data = build_xlsx([("Übersicht", overview), ("Messwerte", readings), ("Tarife", tariffs), ("Abschlagsänderungen", advance_changes)])
        self.send_bytes(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="energylab-export-{date.today()}.xlsx"'})

    def support_page(self):
        if COFFEE_URL.startswith(("https://", "http://")):
            coffee = f'<a class="btn coffee" href="{esc(COFFEE_URL)}" target="_blank" rel="noopener noreferrer">☕ Buy me a coffee</a>'
        else:
            coffee = '<span class="btn secondary" title="BUY_ME_A_COFFEE_URL im Portainer-Stack eintragen">☕ Buy me a coffee</span><div class="muted" style="margin-top:10px">Coffee-Link noch im Stack eintragen</div>'
        body = f"""<div class="topbar"><div><h1>Unterstützung</h1><div class="subtitle">Über EnergieLab</div></div></div><div class="card support"><div class="coffee-cup">☕</div><div class="credit">Idea und umsetztung by Lrd.Tiberius</div><p class="muted">EnergieLab bündelt Strom-, PV-, Gas-, Wasser-, Abwasser- und Fahrzeugverbräuche lokal in deinem Homelab.</p><div style="margin-top:24px">{coffee}</div><div class="actions" style="justify-content:center;margin-top:28px"><a class="btn secondary" href="/export/backup.json">JSON-Backup</a><a class="btn secondary" href="/export/fuelings.csv">Tankungen als CSV</a></div></div>"""
        self.send_html(page("Unterstützung", body, "/support"))

    def backup_json(self):
        with connect() as db:
            payload = {
                "exported_at": datetime.now().isoformat(),
                "version": APP_VERSION,
                "vehicles": [dict(r) for r in db.execute("SELECT * FROM vehicles")],
                "vehicle_fluids": [dict(r) for r in db.execute("SELECT * FROM vehicle_fluids")],
                "fuelings": [dict(r) for r in db.execute("SELECT * FROM fuelings")],
                "vehicle_expenses": [dict(r) for r in db.execute("SELECT * FROM vehicle_expenses")],
                "energy_readings": [dict(r) for r in db.execute("SELECT * FROM energy_readings")],
                "energy_tariffs": [dict(r) for r in db.execute("SELECT * FROM energy_tariffs")],
                "energy_advance_changes": [dict(r) for r in db.execute("SELECT * FROM energy_advance_changes")],
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
