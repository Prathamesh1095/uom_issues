import pandas as pd
import numpy as np
import re
import math
import json
import io
import os
import asyncio
import uuid
import tempfile
import time
import sqlite3
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

# PostgreSQL support
try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

# Load .env for local development (DATABASE_URL, etc.)
from dotenv import load_dotenv
load_dotenv()

app = FastAPI(title="Supply Chain GRN Smart Entry System")

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
try:
    with open(config_path, 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database connection config
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL and HAS_PSYCOPG2)
DB_PATH = os.path.join(os.path.dirname(__file__), 'data.db')

sku_profiles = {}
uom_master_lookup: dict[str, dict[str, int]] = {}  # {sku_code: {uom_name: cf, ...}}
website_price_lookup: dict[str, dict] = {}  # {sku_code: {base_rate: float, uom: str, cf: int, price: float}}
global_df = None

# In-memory task status store for async uploads
upload_tasks = {}

# Startup logs — accumulated during boot, exposed via API
startup_logs = []
startup_complete = False
startup_data_loaded = False
startup_total_rows = 0
startup_processed_rows = 0

# Export report readiness tracking (0-100, -1 = not started)
grn_outliers_progress = -1
sales_outliers_progress = -1
sales_loss_progress = -1
grn_template_available = False
sales_template_available = True  # sales template is always available (static columns)


class PredictRequest(BaseModel):
    sku_code: str
    input_price: float


def get_db():
    """Get a database connection — PostgreSQL if DATABASE_URL is set, otherwise SQLite."""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    else:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


def get_cursor(conn):
    """Get a cursor appropriate for the database type — returns dict-like rows."""
    if USE_POSTGRES:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        return conn.cursor()


def close_db(conn):
    """Safely close a database connection."""
    try:
        conn.close()
    except Exception:
        pass


def execute_sql(conn, sql, params=None):
    """Execute a single SQL statement with optional params. Returns cursor."""
    cur = get_cursor(conn)
    if params is not None:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    return cur


def init_db():
    """Initialize database tables if they don't exist."""
    conn = get_db()
    try:
        if USE_POSTGRES:
            statements = [
                """CREATE TABLE IF NOT EXISTS raw_data (
                    id SERIAL PRIMARY KEY,
                    filename TEXT NOT NULL,
                    uploaded_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    row_count INTEGER NOT NULL,
                    csv_content BYTEA NOT NULL
                );""",
                """CREATE TABLE IF NOT EXISTS sku_profiles (
                    sku_code TEXT PRIMARY KEY,
                    latest_br REAL NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );""",
                """CREATE TABLE IF NOT EXISTS sku_uoms (
                    id SERIAL PRIMARY KEY,
                    sku_code TEXT NOT NULL,
                    uom TEXT NOT NULL,
                    cf INTEGER NOT NULL,
                    FOREIGN KEY (sku_code) REFERENCES sku_profiles(sku_code)
                );""",
                """CREATE TABLE IF NOT EXISTS outliers_cache (
                    id SERIAL PRIMARY KEY,
                    generated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    excel_data BYTEA NOT NULL,
                    row_count INTEGER NOT NULL
                );""",
                """CREATE INDEX IF NOT EXISTS idx_sku_uoms_sku ON sku_uoms(sku_code);""",
                """CREATE TABLE IF NOT EXISTS sales_data (
                    id SERIAL PRIMARY KEY,
                    filename TEXT NOT NULL,
                    uploaded_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    row_count INTEGER NOT NULL,
                    csv_content BYTEA NOT NULL
                );""",
                """CREATE TABLE IF NOT EXISTS sales_outliers_cache (
                    id SERIAL PRIMARY KEY,
                    generated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    excel_data BYTEA NOT NULL,
                    row_count INTEGER NOT NULL
                );""",
                """CREATE TABLE IF NOT EXISTS sales_loss_summary_cache (
                    id SERIAL PRIMARY KEY,
                    generated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    excel_data BYTEA NOT NULL,
                    row_count INTEGER NOT NULL
                );""",
                """CREATE TABLE IF NOT EXISTS uom_master (
                    id SERIAL PRIMARY KEY,
                    old_sku_id TEXT NOT NULL,
                    listing_title TEXT,
                    business_category TEXT,
                    uom TEXT NOT NULL,
                    flag TEXT DEFAULT 'Enabled',
                    cf INTEGER NOT NULL,
                    uom_type TEXT,
                    price REAL,
                    uploaded_at TIMESTAMP NOT NULL DEFAULT NOW()
                );""",
                """CREATE INDEX IF NOT EXISTS idx_uom_master_sku ON uom_master(old_sku_id);""",
            ]
        else:
            statements = [
                """CREATE TABLE IF NOT EXISTS raw_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
                    row_count INTEGER NOT NULL,
                    csv_content BLOB NOT NULL
                );""",
                """CREATE TABLE IF NOT EXISTS sku_profiles (
                    sku_code TEXT PRIMARY KEY,
                    latest_br REAL NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );""",
                """CREATE TABLE IF NOT EXISTS sku_uoms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku_code TEXT NOT NULL,
                    uom TEXT NOT NULL,
                    cf INTEGER NOT NULL,
                    FOREIGN KEY (sku_code) REFERENCES sku_profiles(sku_code)
                );""",
                """CREATE TABLE IF NOT EXISTS outliers_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    excel_data BLOB NOT NULL,
                    row_count INTEGER NOT NULL
                );""",
                """CREATE INDEX IF NOT EXISTS idx_sku_uoms_sku ON sku_uoms(sku_code);""",
                """CREATE TABLE IF NOT EXISTS sales_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
                    row_count INTEGER NOT NULL,
                    csv_content BLOB NOT NULL
                );""",
                """CREATE TABLE IF NOT EXISTS sales_outliers_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    excel_data BLOB NOT NULL,
                    row_count INTEGER NOT NULL
                );""",
                """CREATE TABLE IF NOT EXISTS sales_loss_summary_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    excel_data BLOB NOT NULL,
                    row_count INTEGER NOT NULL
                );""",
                """CREATE TABLE IF NOT EXISTS uom_master (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    old_sku_id TEXT NOT NULL,
                    listing_title TEXT,
                    business_category TEXT,
                    uom TEXT NOT NULL,
                    flag TEXT DEFAULT 'Enabled',
                    cf INTEGER NOT NULL,
                    uom_type TEXT,
                    price REAL,
                    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
                );""",
                """CREATE INDEX IF NOT EXISTS idx_uom_master_sku ON uom_master(old_sku_id);""",
            ]
        for stmt in statements:
            cur = get_cursor(conn)
            cur.execute(stmt)
            cur.close()
        # Migration: add price column to uom_master if missing (for databases created before schema update)
        try:
            if USE_POSTGRES:
                execute_sql(conn, "ALTER TABLE uom_master ADD COLUMN IF NOT EXISTS price REAL").close()
            else:
                execute_sql(conn, "ALTER TABLE uom_master ADD COLUMN price REAL").close()
        except Exception:
            pass  # Column already exists (SQLite: ALTER TABLE ADD COLUMN fails if column exists)
        conn.commit()
    finally:
        close_db(conn)


def save_raw_data_to_db(csv_bytes: bytes, filename: str, row_count: int):
    """Replace raw_data table with new CSV content."""
    conn = get_db()
    try:
        execute_sql(conn, "DELETE FROM raw_data").close()
        if USE_POSTGRES:
            execute_sql(conn,
                "INSERT INTO raw_data (filename, row_count, csv_content) VALUES (%s, %s, %s)",
                (filename, row_count, csv_bytes)
            ).close()
        else:
            execute_sql(conn,
                "INSERT INTO raw_data (filename, row_count, csv_content) VALUES (?, ?, ?)",
                (filename, row_count, csv_bytes)
            ).close()
        conn.commit()
    finally:
        close_db(conn)


def load_raw_csv_from_db():
    """Load the stored CSV content from DB and return as BytesIO, or None if empty."""
    conn = get_db()
    try:
        cur = execute_sql(conn, "SELECT csv_content, row_count FROM raw_data ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None, 0, None
        raw_bytes = row['csv_content']
        row_count = row['row_count']
        # PostgreSQL returns BYTEA as memoryview; SQLite returns bytes directly
        if hasattr(raw_bytes, 'tobytes'):
            csv_bytes = raw_bytes.tobytes()
        elif isinstance(raw_bytes, memoryview):
            csv_bytes = bytes(raw_bytes)
        else:
            csv_bytes = raw_bytes
        return io.BytesIO(csv_bytes), row_count, io.StringIO(csv_bytes.decode('utf-8'))
    finally:
        close_db(conn)


def save_profiles_to_db(profiles: dict):
    """Save SKU profiles to database (replace all existing)."""
    conn = get_db()
    try:
        execute_sql(conn, "DELETE FROM sku_uoms").close()
        execute_sql(conn, "DELETE FROM sku_profiles").close()
        for sku, profile in profiles.items():
            if USE_POSTGRES:
                execute_sql(conn,
                    "INSERT INTO sku_profiles (sku_code, latest_br) VALUES (%s, %s)",
                    (sku, profile['latest_br'])
                ).close()
            else:
                execute_sql(conn,
                    "INSERT INTO sku_profiles (sku_code, latest_br) VALUES (?, ?)",
                    (sku, profile['latest_br'])
                ).close()
            for uom, cf in profile['valid_uoms'].items():
                if USE_POSTGRES:
                    execute_sql(conn,
                        "INSERT INTO sku_uoms (sku_code, uom, cf) VALUES (%s, %s, %s)",
                        (sku, uom, cf)
                    ).close()
                else:
                    execute_sql(conn,
                        "INSERT INTO sku_uoms (sku_code, uom, cf) VALUES (?, ?, ?)",
                        (sku, uom, cf)
                    ).close()
        conn.commit()
    finally:
        close_db(conn)


def load_profiles_from_db() -> dict:
    """Load all SKU profiles from database."""
    conn = get_db()
    try:
        profiles = {}
        cur = execute_sql(conn, "SELECT sku_code, latest_br FROM sku_profiles")
        sku_rows = cur.fetchall()
        cur.close()
        for sku_row in sku_rows:
            sku = sku_row['sku_code']
            uom_cur = execute_sql(conn,
                "SELECT uom, cf FROM sku_uoms WHERE sku_code = %s" if USE_POSTGRES else "SELECT uom, cf FROM sku_uoms WHERE sku_code = ?",
                (sku,)
            )
            uom_rows = uom_cur.fetchall()
            uom_cur.close()
            valid_uoms = {row['uom']: row['cf'] for row in uom_rows}
            profiles[sku] = {
                'latest_br': sku_row['latest_br'],
                'valid_uoms': valid_uoms
            }
        return profiles
    finally:
        close_db(conn)


def save_uom_master_to_db(df: pd.DataFrame):
    """Replace uom_master table with new data and rebuild lookup."""
    global uom_master_lookup, website_price_lookup
    conn = get_db()
    try:
        execute_sql(conn, "DELETE FROM uom_master").close()
        for _, row in df.iterrows():
            sku = str(row.get('old_sku_id', '')).strip()
            uom = str(row.get('UOM', '')).strip()
            cf = int(str(row.get('cf', '1')).replace(',', ''))
            listing_title = row.get('listing_title', '')
            business_category = row.get('business_category', '')
            flag = row.get('Flag', 'Enabled')
            uom_type = row.get('uom_type', '')
            price_raw = row.get('price', '')
            try:
                price = float(str(price_raw).replace(',', '')) if price_raw != '' and price_raw is not None else None
            except (ValueError, TypeError):
                price = None
            if USE_POSTGRES:
                execute_sql(conn,
                    "INSERT INTO uom_master (old_sku_id, listing_title, business_category, uom, flag, cf, uom_type, price) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (sku, listing_title, business_category, uom, flag, cf, uom_type, price)
                ).close()
            else:
                execute_sql(conn,
                    "INSERT INTO uom_master (old_sku_id, listing_title, business_category, uom, flag, cf, uom_type, price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (sku, listing_title, business_category, uom, flag, cf, uom_type, price)
                ).close()
        conn.commit()
        # Rebuild in-memory lookups
        uom_master_lookup = build_uom_master_lookup_from_db()
        website_price_lookup = build_website_price_lookup_from_db()
    finally:
        close_db(conn)


def load_uom_master_from_db() -> pd.DataFrame:
    """Load all UOM master records from database."""
    conn = get_db()
    try:
        cur = execute_sql(conn, "SELECT * FROM uom_master ORDER BY old_sku_id")
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])
    finally:
        close_db(conn)


def build_uom_master_lookup_from_db() -> dict[str, dict[str, int]]:
    """Build {sku_code: {uom_name: cf}} lookup from uom_master table.
    For 'Enabled' rows only, takes the first cf per uom per sku."""
    df = load_uom_master_from_db()
    if df.empty:
        return {}
    lookup = {}
    df_filtered = df[df['flag'].str.lower() == 'enabled'] if 'flag' in df.columns else df
    for _, row in df_filtered.iterrows():
        sku = str(row.get('old_sku_id', '')).strip()
        uom = str(row.get('uom', '')).strip()
        cf = int(str(row.get('cf', '1')).replace(',', ''))
        if not sku or not uom:
            continue
        if sku not in lookup:
            lookup[sku] = {}
        if uom not in lookup[sku]:
            lookup[sku][uom] = cf
    return lookup


def update_sku_profiles_valid_uoms_from_master():
    """Update valid_uoms in all sku_profiles from the current uom_master_lookup.
    SKUs not found in UOM master get empty valid_uoms."""
    global sku_profiles
    for sku in sku_profiles:
        sku_profiles[sku]['valid_uoms'] = uom_master_lookup.get(sku, {})


def build_website_price_lookup_from_db() -> dict[str, dict]:
    """Build {sku_code: {base_rate, uom, cf, price}} from UOM master rows
    where uom_type is 'Website UOM' and price is valid.
    If multiple website UOMs exist per SKU, median base rate is used."""
    df = load_uom_master_from_db()
    if df.empty:
        return {}
    if 'uom_type' not in df.columns or 'price' not in df.columns:
        return {}

    df['price'] = pd.to_numeric(df['price'], errors='coerce')
    website_df = df[
        (df['flag'].str.lower() == 'enabled') &
        (df['uom_type'].str.strip().str.lower() == 'website uom') &
        (df['price'].notna()) &
        (df['price'] > 0)
    ]
    if website_df.empty:
        return {}

    sku_rates: dict[str, list[float]] = {}
    sku_details: dict[str, dict] = {}

    for _, row in website_df.iterrows():
        sku = str(row.get('old_sku_id', '')).strip()
        uom = str(row.get('uom', '')).strip()
        cf_str = str(row.get('cf', '1')).replace(',', '')
        try:
            cf = int(float(cf_str))
        except (ValueError, TypeError):
            cf = 1
        price = float(row.get('price', 0))
        if not sku or not uom or cf <= 0 or price <= 0:
            continue
        base_rate = price / cf
        if sku not in sku_rates:
            sku_rates[sku] = []
        sku_rates[sku].append(base_rate)
        sku_details[sku] = {'uom': uom, 'cf': cf, 'price': price}

    lookup = {}
    for sku, rates in sku_rates.items():
        median_rate = float(np.median(rates)) if len(rates) > 1 else float(rates[0])
        lookup[sku] = {
            'base_rate': median_rate,
            **sku_details[sku]
        }
    return lookup


def save_outliers_cache(excel_bytes: bytes, row_count: int):
    """Replace outliers cache with newly computed Excel data."""
    conn = get_db()
    try:
        execute_sql(conn, "DELETE FROM outliers_cache").close()
        if USE_POSTGRES:
            execute_sql(conn,
                "INSERT INTO outliers_cache (excel_data, row_count) VALUES (%s, %s)",
                (excel_bytes, row_count)
            ).close()
        else:
            execute_sql(conn,
                "INSERT INTO outliers_cache (excel_data, row_count) VALUES (?, ?)",
                (excel_bytes, row_count)
            ).close()
        conn.commit()
    finally:
        close_db(conn)


def load_outliers_cache() -> tuple:
    """Load cached Excel data. Returns (excel_bytes, row_count) or (None, 0)."""
    conn = get_db()
    try:
        cur = execute_sql(conn, "SELECT excel_data, row_count FROM outliers_cache ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None, 0
        raw_bytes = row['excel_data']
        if hasattr(raw_bytes, 'tobytes'):
            excel_bytes = raw_bytes.tobytes()
        elif isinstance(raw_bytes, memoryview):
            excel_bytes = bytes(raw_bytes)
        else:
            excel_bytes = raw_bytes
        return excel_bytes, row['row_count']
    finally:
        close_db(conn)


# ─── Sales Data CRUD Helpers ────────────────────────────────────────────────────


def save_sales_data_to_db(csv_bytes: bytes, filename: str, row_count: int):
    """Replace sales_data table with new CSV content."""
    conn = get_db()
    try:
        execute_sql(conn, "DELETE FROM sales_data").close()
        if USE_POSTGRES:
            execute_sql(conn,
                "INSERT INTO sales_data (filename, row_count, csv_content) VALUES (%s, %s, %s)",
                (filename, row_count, csv_bytes)
            ).close()
        else:
            execute_sql(conn,
                "INSERT INTO sales_data (filename, row_count, csv_content) VALUES (?, ?, ?)",
                (filename, row_count, csv_bytes)
            ).close()
        conn.commit()
    finally:
        close_db(conn)


def load_sales_csv_from_db():
    """Load the stored Sales CSV content from DB. Returns (BytesIO, row_count) or (None, 0)."""
    conn = get_db()
    try:
        cur = execute_sql(conn, "SELECT csv_content, row_count FROM sales_data ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None, 0
        raw_bytes = row['csv_content']
        if hasattr(raw_bytes, 'tobytes'):
            csv_bytes = raw_bytes.tobytes()
        elif isinstance(raw_bytes, memoryview):
            csv_bytes = bytes(raw_bytes)
        else:
            csv_bytes = raw_bytes
        return io.BytesIO(csv_bytes), row['row_count']
    finally:
        close_db(conn)


def save_sales_outliers_cache(excel_bytes: bytes, row_count: int):
    """Replace sales_outliers_cache with newly computed Excel data."""
    conn = get_db()
    try:
        execute_sql(conn, "DELETE FROM sales_outliers_cache").close()
        if USE_POSTGRES:
            execute_sql(conn,
                "INSERT INTO sales_outliers_cache (excel_data, row_count) VALUES (%s, %s)",
                (excel_bytes, row_count)
            ).close()
        else:
            execute_sql(conn,
                "INSERT INTO sales_outliers_cache (excel_data, row_count) VALUES (?, ?)",
                (excel_bytes, row_count)
            ).close()
        conn.commit()
    finally:
        close_db(conn)


def load_sales_outliers_cache() -> tuple:
    """Load cached Sales outliers Excel data. Returns (excel_bytes, row_count) or (None, 0)."""
    conn = get_db()
    try:
        cur = execute_sql(conn, "SELECT excel_data, row_count FROM sales_outliers_cache ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None, 0
        raw_bytes = row['excel_data']
        if hasattr(raw_bytes, 'tobytes'):
            excel_bytes = raw_bytes.tobytes()
        elif isinstance(raw_bytes, memoryview):
            excel_bytes = bytes(raw_bytes)
        else:
            excel_bytes = raw_bytes
        return excel_bytes, row['row_count']
    finally:
        close_db(conn)


def save_sales_loss_summary_cache(excel_bytes: bytes, row_count: int):
    """Replace sales_loss_summary_cache with newly computed Excel data."""
    conn = get_db()
    try:
        execute_sql(conn, "DELETE FROM sales_loss_summary_cache").close()
        if USE_POSTGRES:
            execute_sql(conn,
                "INSERT INTO sales_loss_summary_cache (excel_data, row_count) VALUES (%s, %s)",
                (excel_bytes, row_count)
            ).close()
        else:
            execute_sql(conn,
                "INSERT INTO sales_loss_summary_cache (excel_data, row_count) VALUES (?, ?)",
                (excel_bytes, row_count)
            ).close()
        conn.commit()
    finally:
        close_db(conn)


def load_sales_loss_summary_cache() -> tuple:
    """Load cached Sales loss summary Excel data. Returns (excel_bytes, row_count) or (None, 0)."""
    conn = get_db()
    try:
        cur = execute_sql(conn, "SELECT excel_data, row_count FROM sales_loss_summary_cache ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None, 0
        raw_bytes = row['excel_data']
        if hasattr(raw_bytes, 'tobytes'):
            excel_bytes = raw_bytes.tobytes()
        elif isinstance(raw_bytes, memoryview):
            excel_bytes = bytes(raw_bytes)
        else:
            excel_bytes = raw_bytes
        return excel_bytes, row['row_count']
    finally:
        close_db(conn)


# ─── UOM Name Extraction ────────────────────────────────────────────────────────


def extract_uom_name(uom_str: str) -> str:
    """Extract the base UOM name from a UOM string.

    Examples:
        '1 Piece' -> 'Piece'
        '1 Pack' -> 'Pack'
        '1 Pack of 100 Piece' -> 'Pack'
        'Pack of 100 Piece' -> 'Pack'
        'Pack 1' -> 'Pack'
        'Box of 5' -> 'Box'
        'Each' -> 'Each'
    """
    filler = {"of", "and", "per", "the", "a", "an"}
    for word in uom_str.split():
        clean = word.strip()
        if clean.isdigit() or clean.lower() in filler:
            continue
        if clean.isalpha():
            return clean
    return uom_str


# ─── Sales Outlier Detection Logic ──────────────────────────────────────────────


def compute_sales_outliers(sales_csv_source, log_callback=None) -> tuple:
    """
    Compute sales outliers and loss summary from Sales CSV data, using existing GRN SKU profiles.
    Normalizes sales price to per-base-unit (using UOM master CF) before outlier detection.

    Returns (outliers_df, loss_summary_df) as pandas DataFrames.
    """
    global uom_master_lookup

    # Read Sales CSV
    if isinstance(sales_csv_source, str):
        sales_df = pd.read_csv(sales_csv_source, low_memory=False)
    else:
        sales_df = pd.read_csv(sales_csv_source, low_memory=False)

    required_cols = ['SKU Code', 'Sales Price', 'Sales UOM', 'Sales Qty']
    missing = [c for c in required_cols if c not in sales_df.columns]
    if missing:
        raise ValueError(f"Sales CSV missing required columns: {missing}")

    # Ensure numeric types
    sales_df['Sales Price'] = pd.to_numeric(sales_df['Sales Price'], errors='coerce')
    sales_df['Sales Qty'] = pd.to_numeric(sales_df['Sales Qty'], errors='coerce').fillna(0).astype(int)
    sales_df.dropna(subset=['Sales Price'], inplace=True)

    lower_mult = config.get("sales_analysis", {}).get("normalized_price_lower_multiplier", 0.6)
    upper_mult = config.get("sales_analysis", {}).get("normalized_price_upper_multiplier", 3.0)
    conf_weights = config.get("confidence_scoring", {})

    outliers_list = []
    loss_summary = {}  # sku -> {'total_qty': 0, 'total_loss': 0.0, 'suggested_uoms': {}, 'count': 0}

    total_rows = len(sales_df)
    last_reported_pct = -1

    for idx, (_, row) in enumerate(sales_df.iterrows()):
        pct = int((idx / total_rows) * 100) if total_rows > 0 else 100
        if pct // 10 > last_reported_pct // 10:
            last_reported_pct = pct
            if log_callback:
                log_callback(f"Analyzing sales row {idx + 1:,} of {total_rows:,} ({pct}%)...", pct, None)
        sku = str(row.get('SKU Code', '')).strip()
        sales_uom = str(row.get('Sales UOM', '')).strip()
        extracted_uom = extract_uom_name(sales_uom)
        sales_price = row.get('Sales Price', 0)
        sales_qty = int(row.get('Sales Qty', 0))
        sales_date = row.get('Date', '')
        order_id = row.get('Order ID', '')

        # Skip if no SKU
        if not sku:
            continue

        # Look up SKU in GRN profiles
        profile = sku_profiles.get(sku)

        # If SKU not found, mark as outlier with no loss calculation
        if profile is None:
            outliers_list.append({
                'Date': sales_date,
                'Order ID': order_id,
                'SKU Code': sku,
                'Sales UOM': sales_uom,
                'Sales Qty': sales_qty,
                'Actual Sales Price': sales_price,
                'Normalized Price (per base UOM)': '',
                'Expected Price (Correct UOM)': '',
                'Suggested UOM': '',
                'Suggested CF': '',
                'Suggested_Correct_Price': '',
                'Price Difference per Unit': '',
                'Sales Loss': 0,
                'Historical Median Base Rate': '',
                'Confidence': '',
                'CF_Source': '',
                'Outlier Reason': f'SKU "{sku}" not found in GRN historical profiles.'
            })
            continue

        valid_uoms = profile['valid_uoms']  # {uom: cf, ...} — from UOM master
        latest_br = profile['latest_br']

        # If no valid UOMs defined, skip
        if not valid_uoms:
            continue

        # --- Step 1: Look up CF (UOM master first, then GRN valid_uoms) ---
        cf = uom_master_lookup.get(sku, {}).get(extracted_uom)
        matched_uom_source = 'UOM Master'

        if cf is None:
            # Fall back to GRN valid_uoms: try exact match first, then partial
            if sales_uom in valid_uoms:
                cf = valid_uoms[sales_uom]
                matched_uom_source = 'GRN'
            else:
                for uom_key in valid_uoms:
                    if extracted_uom.lower() in uom_key.lower():
                        cf = valid_uoms[uom_key]
                        matched_uom_source = 'GRN'
                        break

        # --- Step 2: If no CF found, flag as outlier ---
        is_outlier = False
        reason_parts = []
        suggested_uom = ''
        suggested_cf = ''
        normalized_price = 0
        correct_expected_price = 0
        confidence = 0

        if cf is None:
            is_outlier = True
            reason_parts.append(
                f"UOM '{sales_uom}' (extracted: '{extracted_uom}') not found in UOM master or GRN valid UOMs for SKU '{sku}'. "
                f"Valid UOMs: {', '.join(valid_uoms.keys())}"
            )
            confidence = 0.2
            # Find closest valid UOM for suggestion
            candidates = []
            for uom, cf_val in valid_uoms.items():
                exp_price = latest_br * cf_val
                ratio = sales_price / exp_price if exp_price > 0 else 0
                score = abs(math.log(ratio)) if ratio > 0 else float('inf')
                candidates.append({'uom': uom, 'cf': cf_val, 'expected_price': exp_price, 'score': score})
            if candidates:
                candidates.sort(key=lambda x: x['score'])
                best = candidates[0]
                correct_expected_price = best['expected_price']
                suggested_uom = best['uom']
                suggested_cf = best['cf']
                confidence = max(confidence, conf_weights.get("grn_pattern_source_weight", 0.6))
            else:
                correct_expected_price = 0
        else:
            # --- Step 3: Normalize price ---
            normalized_price = sales_price / cf
            expected_price_for_uom = latest_br * cf

            lower_bound = latest_br * lower_mult
            upper_bound = latest_br * upper_mult

            # Confidence based on CF source
            confidence = conf_weights.get("uom_master_source_weight", 0.9) if matched_uom_source == 'UOM Master' else conf_weights.get("grn_pattern_source_weight", 0.6)

            if normalized_price < lower_bound or normalized_price > upper_bound:
                is_outlier = True
                reason_parts.append(
                    f"Normalized price {normalized_price:.2f} (from '{sales_uom}' / CF={cf}) is outside expected range "
                    f"[{lower_bound:.2f}, {upper_bound:.2f}] for base rate {latest_br:.4f}."
                )
                correct_expected_price = expected_price_for_uom
                suggested_uom = extracted_uom
                suggested_cf = cf
            else:
                # Not an outlier, skip
                continue

        if not is_outlier:
            continue

        # --- Step 4: Sales loss calculation ---
        # Loss = (base_rate - normalized_price) * qty * cf  (total loss at UOM level)
        price_diff = correct_expected_price - sales_price if correct_expected_price else 0
        sales_loss = max(0, round(price_diff * sales_qty, 2))

        if reason_parts:
            reason_parts.append(
                f"Closest valid UOM is '{suggested_uom}' (CF={suggested_cf}, expected price={correct_expected_price:.2f})."
            )
        reason = " | ".join(reason_parts) if reason_parts else "Unknown reason."

        outliers_list.append({
            'Date': sales_date,
            'Order ID': order_id,
            'SKU Code': sku,
            'Sales UOM': sales_uom,
            'Sales Qty': sales_qty,
            'Actual Sales Price': sales_price,
            'Normalized Price (per base UOM)': round(normalized_price, 4) if normalized_price else '',
            'Expected Price (Correct UOM)': round(correct_expected_price, 2) if correct_expected_price else '',
            'Suggested UOM': suggested_uom,
            'Suggested CF': suggested_cf,
            'Suggested_Correct_Price': round(correct_expected_price, 2) if correct_expected_price else '',
            'Price Difference per Unit': round(price_diff, 2),
            'Sales Loss': sales_loss,
            'Historical Median Base Rate': round(latest_br, 4),
            'Confidence': round(confidence, 2),
            'CF_Source': matched_uom_source,
            'Outlier Reason': reason
        })

        # Accumulate summary per SKU
        if sku not in loss_summary:
            loss_summary[sku] = {
                'total_qty': 0,
                'total_loss': 0.0,
                'suggested_uoms': {},
                'count': 0,
                'most_common_sales_uom': sales_uom
            }
        loss_summary[sku]['total_qty'] += sales_qty
        loss_summary[sku]['total_loss'] += sales_loss
        loss_summary[sku]['count'] += 1
        loss_summary[sku]['suggested_uoms'][suggested_uom] = loss_summary[sku]['suggested_uoms'].get(suggested_uom, 0) + 1

    outliers_df = pd.DataFrame(outliers_list)

    # Build summary DataFrame
    summary_rows = []
    for sku, summary in loss_summary.items():
        # Find most common suggested UOM
        most_common_uom = max(summary['suggested_uoms'], key=summary['suggested_uoms'].get) if summary['suggested_uoms'] else ''
        summary_rows.append({
            'SKU Code': sku,
            'Total Units Sold at Wrong UOM/Price': summary['total_qty'],
            'Total Sales Loss': round(summary['total_loss'], 2),
            'Outlier Transaction Count': summary['count'],
            'Most Common Suggested UOM': most_common_uom
        })

    loss_summary_df = pd.DataFrame(summary_rows)
    if not loss_summary_df.empty:
        loss_summary_df = loss_summary_df.sort_values('Total Sales Loss', ascending=False)

    return outliers_df, loss_summary_df


def precompute_and_cache_sales_outliers(csv_source, log_callback=None):
    """Compute sales outliers from CSV, cache both reports as Excel in DB. Returns (row_count, loss_row_count, bytes)."""
    global sales_outliers_progress, sales_loss_progress
    sales_outliers_progress = 0
    sales_loss_progress = 0
    try:
        if log_callback:
            log_callback("Computing sales outliers...", 0, None)
        outliers_df, loss_summary_df = compute_sales_outliers(csv_source, log_callback=log_callback)
        sales_outliers_progress = 50
        sales_loss_progress = 50
        if log_callback:
            log_callback(f"Found {len(outliers_df)} outlier rows, {len(loss_summary_df)} SKUs with loss.", 50, None)

        # Cache outliers Excel
        if log_callback:
            log_callback("Caching sales outliers report...", 60, None)
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            outliers_df.to_excel(writer, index=False, sheet_name='Sales Outliers')
        save_sales_outliers_cache(excel_buffer.getvalue(), len(outliers_df))
        sales_outliers_progress = 80

        # Cache loss summary Excel
        if log_callback:
            log_callback("Caching sales loss summary report...", 80, None)
        excel_buffer2 = io.BytesIO()
        with pd.ExcelWriter(excel_buffer2, engine='openpyxl') as writer:
            loss_summary_df.to_excel(writer, index=False, sheet_name='Sales Loss Summary')
        save_sales_loss_summary_cache(excel_buffer2.getvalue(), len(loss_summary_df))
        sales_loss_progress = 80

        sales_outliers_progress = 100
        sales_loss_progress = 100
        if log_callback:
            log_callback(f"✓ Sales outliers and loss summary cached.", 100, None)

        return len(outliers_df), len(loss_summary_df)
    except Exception as e:
        add_startup_log(f"⚠ Sales outliers precomputation failed: {str(e)}")
        sales_outliers_progress = -1
        sales_loss_progress = -1
        if log_callback:
            log_callback(f"✗ Error: {str(e)}", 0, None)
        return 0, 0


def add_startup_log(message, processed=None, total=None):
    """Append a log entry with timestamp for startup."""
    global startup_logs, startup_processed_rows, startup_total_rows
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {message}"
    startup_logs.append(entry)
    # Keep last 200 logs to avoid unbounded memory
    if len(startup_logs) > 200:
        startup_logs = startup_logs[-200:]
    if processed is not None:
        startup_processed_rows = processed
    if total is not None:
        startup_total_rows = total


def build_sku_profiles_from_chunks(chunks, log_callback=None, known_total_rows=None):
    """
    Process CSV in streaming chunks and build SKU profiles using fully vectorized operations.
    Uses transform/groupby instead of per-SKU loops for ~100x faster processing.
    Tracks latest date during accumulation instead of sorting at the end.
    """
    global uom_master_lookup, website_price_lookup
    lower_mult = config.get("grn_profiling", {}).get("base_rate_lower_multiplier", 0.3)
    upper_mult = config.get("grn_profiling", {}).get("base_rate_upper_multiplier", 3.0)
    total_rows_processed = 0

    # Accumulators across chunks
    all_rates = {}          # sku -> list of valid rates
    all_dates = {}          # sku -> dict of {date_str: (rate, uom, cf)}
    total_chunks = 0

    for chunk_idx, chunk in enumerate(chunks):
        total_chunks = chunk_idx + 1

        # Rename columns for consistency
        rename_map = {"PO Purchase Rate": "Price", "invoice_date": "Date"}
        chunk.rename(columns=rename_map, inplace=True)

        # Ensure required columns exist
        if 'Price' not in chunk.columns or 'SKU Code' not in chunk.columns:
            if log_callback:
                log_callback(f"Skipped chunk {chunk_idx + 1}: missing 'Price' or 'SKU Code' columns", None, None)
            continue

        # Vectorized preprocessing
        chunk['Price'] = pd.to_numeric(chunk['Price'], errors='coerce')
        chunk.dropna(subset=['Price'], inplace=True)

        if chunk.empty:
            continue

        if 'CF' not in chunk.columns:
            chunk['CF'] = 1.0

        # Use UOM Master CF as authoritative source; fall back to CSV CF column
        def lookup_master_cf(row):
            sku = str(row.get('SKU Code', '')).strip()
            uom_raw = str(row.get('alternate_uom', '')) if pd.notna(row.get('alternate_uom')) else ''
            if sku and uom_raw:
                uom_name = extract_uom_name(uom_raw)
                cf = uom_master_lookup.get(sku, {}).get(uom_name)
                if cf is not None:
                    return cf, 'UOM Master'
            csv_cf = row.get('CF', 1.0)
            return csv_cf, 'CSV Fallback'

        cf_info = chunk.apply(lookup_master_cf, axis=1, result_type='expand')
        chunk['Effective_CF'] = cf_info[0].astype(float)
        chunk['CF_Source'] = cf_info[1]
        chunk['Row_Base_Rate'] = chunk['Price'] / chunk['Effective_CF']

        # --- Fully vectorized per-SKU processing using groupby transforms ---
        # Compute median per SKU in one vectorized pass
        medians = chunk.groupby('SKU Code')['Row_Base_Rate'].transform('median')

        # Vectorized outlier filter
        valid_mask = (chunk['Row_Base_Rate'] >= lower_mult * medians) & (chunk['Row_Base_Rate'] <= upper_mult * medians)
        valid_chunk = chunk[valid_mask].copy()

        if valid_chunk.empty:
            if log_callback:
                log_callback(f"Chunk {chunk_idx + 1}: 0 rows after outlier filter", total_rows_processed, known_total_rows)
            total_rows_processed += len(chunk)
            continue

        # Accumulate valid rates per SKU
        for sku, group in valid_chunk.groupby('SKU Code'):
            if sku not in all_rates:
                all_rates[sku] = []
            all_rates[sku].extend(group['Row_Base_Rate'].tolist())

            # Track latest date per SKU
            if 'Date' in valid_chunk.columns:
                if sku not in all_dates:
                    all_dates[sku] = {}
                for _, row in group.iterrows():
                    date_val = row.get('Date')
                    if pd.notna(date_val):
                        str_date = str(date_val)
                        rate_val = row['Row_Base_Rate']
                        uom_val = row.get('alternate_uom', '')
                        cf_val = row.get('Effective_CF', 1)
                        cf_src = row.get('CF_Source', 'CSV Fallback')
                        all_dates[sku][str_date] = (rate_val, str(uom_val) if pd.notna(uom_val) else '', int(cf_val) if pd.notna(cf_val) else 1, cf_src)

        total_rows_processed += len(chunk)

        if log_callback:
            log_callback(
                f"Chunk {chunk_idx + 1}: {len(chunk):,} rows processed, {len(all_rates)} SKUs accumulated",
                total_rows_processed,
                known_total_rows
            )

    # --- Finalize profiles from accumulated data (vectorized) ---
    final_profiles = {}
    skus_finalized = 0

    for sku, rates in all_rates.items():
        if not rates:
            continue

        rates_arr = np.array(rates)
        median_br = float(np.median(rates_arr))

        # Find latest rate from dates
        latest_br = median_br
        latest_date = None
        if sku in all_dates and all_dates[sku]:
            sorted_dates = sorted(all_dates[sku].keys())
            latest_date = sorted_dates[-1]
            rate_val, uom_val, cf_val, cf_src = all_dates[sku][latest_date]
            latest_br = rate_val

        # If GRN base rate is outside website price limits, use website price as source of truth
        wp_data = website_price_lookup.get(sku)
        if wp_data:
            wp_br = wp_data['base_rate']
            wp_lower_mult = config.get("grn_profiling", {}).get("website_override_lower_multiplier", 0.7)
            wp_upper_mult = config.get("grn_profiling", {}).get("website_override_upper_multiplier", 1.5)
            wp_lower = wp_br * wp_lower_mult
            wp_upper = wp_br * wp_upper_mult
            if latest_br < wp_lower or latest_br > wp_upper:
                if log_callback:
                    log_callback(
                        f"SKU {sku}: GRN base rate {latest_br:.4f} outside website limits "
                        f"[{wp_lower:.4f}, {wp_upper:.4f}], using website base rate {wp_br:.4f}",
                        total_rows_processed,
                        known_total_rows
                    )
                latest_br = wp_br

        # Use UOM master for valid_uoms (authoritative source)
        # If not available in UOM master, valid_uoms stays empty
        valid_uoms = uom_master_lookup.get(sku, {})

        final_profiles[sku] = {
            'latest_br': latest_br,
            'valid_uoms': valid_uoms
        }
        skus_finalized += 1

    # Add profiles for SKUs that exist in website price lookup but have no GRN inward data
    website_added = 0
    for sku, wp_data in website_price_lookup.items():
        if sku not in final_profiles:
            final_profiles[sku] = {
                'latest_br': wp_data['base_rate'],
                'valid_uoms': uom_master_lookup.get(sku, {})
            }
            website_added += 1

    if website_added > 0 and log_callback:
        log_callback(
            f"Added {website_added} SKU profiles from website prices (no inward data).",
            total_rows_processed,
            known_total_rows
        )

    if log_callback:
        log_callback(
            f"Finalized profiles for {skus_finalized} SKUs from {total_rows_processed:,} total rows",
            total_rows_processed,
            known_total_rows
        )

    return final_profiles


def compute_outliers_from_csv(csv_source) -> pd.DataFrame:
    """
    Compute outliers DataFrame from a CSV source (file path or StringIO/BytesIO).
    Same logic as the original /export_outliers endpoint.
    """
    # Read full data if csv_source is a path or a stream
    if isinstance(csv_source, str):
        grn_df = pd.read_csv(csv_source, low_memory=False)
    else:
        grn_df = pd.read_csv(csv_source, low_memory=False)

    # Rename columns for consistency
    rename_map = {"PO Purchase Rate": "Price", "invoice_date": "Date"}
    grn_df.rename(columns=rename_map, inplace=True)

    # [BUGFIX] Convert Price to numeric (matches streaming behavior in build_sku_profiles_from_chunks)
    grn_df['Price'] = pd.to_numeric(grn_df['Price'], errors='coerce')
    # [BUGFIX] Drop rows with unparseable prices
    grn_df.dropna(subset=['Price'], inplace=True)

    # [BUGFIX] Default CF column if missing (matches streaming behavior)
    if 'CF' not in grn_df.columns:
        grn_df['CF'] = 1.0
    # [BUGFIX] Ensure CF is numeric
    grn_df['CF'] = grn_df['CF'].astype(str).str.replace(',', '', regex=False)
    grn_df['CF'] = pd.to_numeric(grn_df['CF'], errors='coerce').fillna(1.0)

    lower_mult = config.get("outlier_detection", {}).get("statistical_lower_multiplier", 0.3)
    upper_mult = config.get("outlier_detection", {}).get("statistical_upper_multiplier", 3.0)
    wp_lower_mult = config.get("outlier_detection", {}).get("website_lower_multiplier", 0.7)
    wp_upper_mult = config.get("outlier_detection", {}).get("website_upper_multiplier", 1.5)

    # Use UOM Master CF as authoritative source; fall back to CSV CF column
    def lookup_master_cf(row):
        sku = str(row.get('SKU Code', '')).strip()
        uom_raw = str(row.get('alternate_uom', '')) if pd.notna(row.get('alternate_uom')) else ''
        if sku and uom_raw:
            uom_name = extract_uom_name(uom_raw)
            cf = uom_master_lookup.get(sku, {}).get(uom_name)
            if cf is not None:
                return cf, 'UOM Master'
        csv_cf = row.get('CF', 1.0)
        return csv_cf, 'CSV Fallback'

    cf_info = grn_df.apply(lookup_master_cf, axis=1, result_type='expand')
    grn_df['Effective_CF'] = cf_info[0].astype(float)
    grn_df['CF_Source'] = cf_info[1]
    grn_df['Row_Base_Rate'] = grn_df['Price'] / grn_df['Effective_CF']

    outliers_list = []

    for sku, group in grn_df.groupby('SKU Code'):
        median_br = group['Row_Base_Rate'].median()

        valid_mask = (group['Row_Base_Rate'] >= lower_mult * median_br) & (group['Row_Base_Rate'] <= upper_mult * median_br)
        valid_count = int(valid_mask.sum())
        invalid_count = int((~valid_mask).sum())

        valid_group = group[valid_mask]
        unique_combos = set()
        for _, r in valid_group.iterrows():
            u = r.get('alternate_uom', '')
            c = r.get('Effective_CF', '')
            p = r.get('Price', '')
            s = r.get('CF_Source', '')
            unique_combos.add(f"{u} | {c} | {p} | {s}")
        valid_combos_str = ", ".join(sorted(list(unique_combos)))

        # Website price reference for this SKU
        wp_data = website_price_lookup.get(sku)
        wp_br = wp_data['base_rate'] if wp_data else None

        outlier_group = group[~valid_mask]

        for _, row in outlier_group.iterrows():
            rate = row['Row_Base_Rate']
            cf_source = row.get('CF_Source', 'CSV Fallback')
            reason = []

            if pd.isna(rate):
                reason.append("Could not calculate Base Rate (missing UOM or Price).")
            elif rate > upper_mult * median_br:
                mult = rate / median_br if median_br > 0 else float('inf')
                reason.append(f"Price is exceptionally high ({mult:.1f}x the historical median of {median_br:.2f}).")
            elif rate < lower_mult * median_br:
                mult = rate / median_br if median_br > 0 else 0
                reason.append(f"Price is exceptionally low ({mult:.2f}x the historical median of {median_br:.2f}).")

            row_dict = row.to_dict()
            row_dict['Historical_Median_Base_Rate'] = median_br
            row_dict['Calculated_Row_Base_Rate'] = rate
            row_dict['Valid_Occurrences'] = valid_count
            row_dict['Invalid_Occurrences'] = invalid_count
            row_dict['Valid_UOM_CF_Price_Combinations'] = valid_combos_str
            row_dict['CF_Source'] = cf_source

            # Confidence based on CF source
            conf_weights = config.get("confidence_scoring", {})
            confidence = conf_weights.get("uom_master_source_weight", 0.9) if cf_source == 'UOM Master' else conf_weights.get("csv_fallback_source_weight", 0.3)
            row_dict['Confidence'] = confidence

            # Website price validation
            if wp_br is not None and not pd.isna(rate):
                wp_lower = wp_br * wp_lower_mult
                wp_upper = wp_br * wp_upper_mult
                row_dict['Website_Base_Rate'] = round(wp_br, 4)
                row_dict['Website_Price_Ratio'] = round(rate / wp_br, 4) if wp_br > 0 else ''
                if rate < wp_lower or rate > wp_upper:
                    row_dict['Website_Price_Validation'] = "Outside range"
                    row_dict['Website_Corrected_Price'] = round(wp_br * row.get('Effective_CF', 1), 4)
                    row_dict['Suggested_Correct_Price'] = round(wp_br * row.get('Effective_CF', 1), 2)
                    reason.append(
                        f"Row base rate {rate:.4f} is outside acceptable range vs website price "
                        f"[{wp_lower:.4f}, {wp_upper:.4f}] (website base rate={wp_br:.4f}). "
                        f"Suggested correct price: {row_dict['Suggested_Correct_Price']:.2f}"
                    )
                else:
                    row_dict['Website_Price_Validation'] = "Within range"
                    row_dict['Website_Corrected_Price'] = ''
                    row_dict['Suggested_Correct_Price'] = ''
            else:
                row_dict['Website_Base_Rate'] = ''
                row_dict['Website_Price_Ratio'] = ''
                row_dict['Website_Price_Validation'] = ''
                row_dict['Website_Corrected_Price'] = ''
                row_dict['Suggested_Correct_Price'] = ''

            row_dict['Outlier_Reason'] = " | ".join(reason)
            outliers_list.append(row_dict)

        # Also flag rows that pass the median filter but fail the website price check
        if wp_br is not None:
            wp_lower = wp_br * wp_lower_mult
            wp_upper = wp_br * wp_upper_mult
            wp_failed_mask = valid_mask & ~((group['Row_Base_Rate'] >= wp_lower) & (group['Row_Base_Rate'] <= wp_upper))
            wp_failed_group = group[wp_failed_mask]
            for _, row in wp_failed_group.iterrows():
                rate = row['Row_Base_Rate']
                cf_source = row.get('CF_Source', 'CSV Fallback')
                row_dict = row.to_dict()
                row_dict['Historical_Median_Base_Rate'] = median_br
                row_dict['Calculated_Row_Base_Rate'] = rate
                row_dict['Valid_Occurrences'] = valid_count
                row_dict['Invalid_Occurrences'] = invalid_count
                row_dict['Valid_UOM_CF_Price_Combinations'] = valid_combos_str
                row_dict['Website_Base_Rate'] = round(wp_br, 4)
                row_dict['Website_Price_Ratio'] = round(rate / wp_br, 4) if wp_br > 0 else ''
                row_dict['Website_Price_Validation'] = "Outside range"
                row_dict['Website_Corrected_Price'] = round(wp_br * row.get('Effective_CF', 1), 4)
                row_dict['Suggested_Correct_Price'] = round(wp_br * row.get('Effective_CF', 1), 2)
                row_dict['CF_Source'] = cf_source
                conf_weights = config.get("confidence_scoring", {})
                row_dict['Confidence'] = conf_weights.get("website_source_weight", 0.9)
                row_dict['Outlier_Reason'] = (
                    f"GRN base rate {rate:.4f} differs from website base rate {wp_br:.4f} "
                    f"(acceptable range vs website: [{wp_lower:.4f}, {wp_upper:.4f}]). "
                    f"Suggested correct price: {row_dict['Suggested_Correct_Price']:.2f}"
                )
                outliers_list.append(row_dict)

    outliers_df = pd.DataFrame(outliers_list)

    if not outliers_df.empty:
        if 'Date' in outliers_df.columns:
            outliers_df['Date'] = pd.to_datetime(outliers_df['Date'], errors='coerce')
            outliers_df = outliers_df.sort_values(by=['SKU Code', 'Date'])
        elif 'SKU Code' in outliers_df.columns:
            outliers_df = outliers_df.sort_values(by=['SKU Code'])

    cols_to_drop = ['Effective_CF', 'Row_Base_Rate']
    outliers_df = outliers_df.drop(columns=[c for c in cols_to_drop if c in outliers_df.columns], errors='ignore')

    return outliers_df


def precompute_and_cache_outliers(csv_source):
    """Compute outliers from CSV data and cache as Excel in DB. Returns (row_count, file_size_bytes)."""
    global startup_logs, grn_outliers_progress
    add_startup_log("Precomputing outliers report...")
    grn_outliers_progress = 0

    try:
        grn_outliers_progress = 10
        outliers_df = compute_outliers_from_csv(csv_source)
        grn_outliers_progress = 70

        # Write to Excel in memory
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            outliers_df.to_excel(writer, index=False, sheet_name='Outliers')
        excel_bytes = excel_buffer.getvalue()
        grn_outliers_progress = 90

        row_count = len(outliers_df)
        save_outliers_cache(excel_bytes, row_count)

        add_startup_log(f"✓ Outliers precomputed: {row_count:,} rows ({len(excel_bytes):,} bytes)")
        grn_outliers_progress = 100
        return row_count, len(excel_bytes)
    except Exception as e:
        add_startup_log(f"⚠ Outliers precomputation failed: {str(e)}")
        grn_outliers_progress = -1
        return 0, 0


# --- Background startup data loading ---
async def load_startup_data_async():
    """Load data in background so server starts immediately. Checks DB first, then falls back to file."""
    global sku_profiles, global_df, startup_complete, startup_logs, uom_master_lookup
    global startup_processed_rows, startup_total_rows, startup_data_loaded
    global grn_outliers_progress, grn_template_available
    global sales_outliers_progress, sales_loss_progress, website_price_lookup

    add_startup_log("Starting background data loading...")

    # Load UOM master from DB
    uom_master_lookup = build_uom_master_lookup_from_db()
    website_price_lookup = build_website_price_lookup_from_db()
    if uom_master_lookup:
        add_startup_log(f"✓ Loaded UOM master for {len(uom_master_lookup)} SKUs.")
    if website_price_lookup:
        add_startup_log(f"✓ Loaded website prices for {len(website_price_lookup)} SKUs.")

    # Check if we have cached data in DB first
    db_profiles = load_profiles_from_db()
    if db_profiles:
        sku_profiles = db_profiles
        startup_data_loaded = True
        startup_complete = True
        add_startup_log(f"✓ Loaded {len(sku_profiles)} SKU profiles from database cache.")

        # Also load global_df sample from raw_data if available
        csv_buf, row_count, string_buf = load_raw_csv_from_db()
        if csv_buf:
            first_chunk = pd.read_csv(csv_buf, nrows=5)
            global_df = first_chunk.copy()
            startup_total_rows = row_count
            add_startup_log(f"Loaded sample ({row_count:,} rows total) from database.")
            grn_template_available = True
        else:
            add_startup_log("No raw data in database. Template download may be unavailable.")

        # Check if outliers cache exists and has rows; if not, try to rebuild it
        _, cached_row_count = load_outliers_cache()
        if cached_row_count == 0:
            add_startup_log("Outliers cache is empty or missing. Attempting to recompute...")
            try:
                csv_buf_recompute, _, _ = load_raw_csv_from_db()
                if csv_buf_recompute:
                    precompute_and_cache_outliers(csv_buf_recompute)
                else:
                    add_startup_log("⚠ Cannot recompute outliers: no raw CSV data in database.")
            except Exception as e:
                add_startup_log(f"⚠ Outliers recomputation failed during startup: {str(e)}")
        else:
            # Cache exists and has rows — mark as ready
            grn_outliers_progress = 100
            add_startup_log(f"✓ Outliers cache loaded ({cached_row_count:,} rows).")

        # Check if sales outliers cache exists and has rows
        _, sales_outlier_count = load_sales_outliers_cache()
        if sales_outlier_count > 0:
            sales_outliers_progress = 100
            add_startup_log(f"✓ Sales outliers cache loaded ({sales_outlier_count:,} rows).")

        # Check if sales loss summary cache exists and has rows
        _, sales_loss_count = load_sales_loss_summary_cache()
        if sales_loss_count > 0:
            sales_loss_progress = 100
            add_startup_log(f"✓ Sales loss summary cache loaded ({sales_loss_count:,} rows).")

        add_startup_log("Server is ready.")
        return

    # No DB cache — try to load from file
    data_file_path = config.get("data_file_path", "../GRN Data Final last 1 year UOM Adjusted.csv")
    file_path = os.path.join(os.path.dirname(__file__), data_file_path)

    # Also try .xlsx as fallback
    if not os.path.exists(file_path):
        xlsx_path = file_path.replace('.csv', '.xlsx')
        if os.path.exists(xlsx_path):
            add_startup_log(f"Converting XLSX to CSV: {xlsx_path}")
            df_temp = pd.read_excel(xlsx_path)
            csv_path = file_path.replace('.csv', '_converted.csv')
            df_temp.to_csv(csv_path, index=False)
            file_path = csv_path

    if os.path.exists(file_path):
        # Count total rows for progress tracking
        add_startup_log(f"Counting rows in {os.path.basename(file_path)}...")
        line_count = 0
        with open(file_path, 'r') as f:
            for _ in f:
                line_count += 1
        total_data_rows = max(0, line_count - 1)  # subtract header
        startup_total_rows = total_data_rows
        add_startup_log(f"Total rows to process: {total_data_rows:,}")

        # Read first chunk to get column names and store a sample
        first_chunk = pd.read_csv(file_path, nrows=5)
        global_df = first_chunk.copy()
        add_startup_log(f"Sample loaded: columns={list(global_df.columns)}")

        # Read full file into memory to store in DB and for outliers computation
        add_startup_log("Reading full CSV into memory for persistence...")
        with open(file_path, 'rb') as f:
            csv_bytes = f.read()

        # Store raw data in DB
        save_raw_data_to_db(csv_bytes, os.path.basename(file_path), total_data_rows)
        add_startup_log(f"Stored {len(csv_bytes):,} bytes in database.")

        # Streaming processing with logs
        add_startup_log("Starting streaming chunk processing...")

        def startup_log_callback(msg, processed, total):
            add_startup_log(msg, processed, total)

        # Run CPU-bound processing in thread pool to not block event loop
        chunks = pd.read_csv(io.BytesIO(csv_bytes), chunksize=50000, low_memory=False)

        def process():
            return build_sku_profiles_from_chunks(
                chunks,
                log_callback=startup_log_callback,
                known_total_rows=total_data_rows
            )

        sku_profiles = await asyncio.to_thread(process)
        startup_data_loaded = True
        add_startup_log(f"✓ Startup complete. Loaded profiles for {len(sku_profiles)} SKUs.")

        grn_template_available = True

        # Save profiles to DB
        save_profiles_to_db(sku_profiles)
        add_startup_log(f"Saved {len(sku_profiles)} SKU profiles to database.")

        # Precompute outliers and cache as Excel in DB
        csv_buf_for_outliers = io.BytesIO(csv_bytes)
        precompute_and_cache_outliers(csv_buf_for_outliers)

        # Check if sales outliers cache exists and has rows (from prior uploads)
        _, sales_outlier_count = load_sales_outliers_cache()
        if sales_outlier_count > 0:
            sales_outliers_progress = 100
            add_startup_log(f"✓ Sales outliers cache loaded ({sales_outlier_count:,} rows).")

        # Check if sales loss summary cache exists and has rows
        _, sales_loss_count = load_sales_loss_summary_cache()
        if sales_loss_count > 0:
            sales_loss_progress = 100
            add_startup_log(f"✓ Sales loss summary cache loaded ({sales_loss_count:,} rows).")
    else:
        startup_data_loaded = False
        add_startup_log("⚠ No data file found on server. Please upload a CSV file to get started.")
        add_startup_log("Use the 'Select CSV File' button below to upload your GRN data.")

    startup_complete = True
    add_startup_log("Server is ready.")


@app.on_event("startup")
def startup_event():
    global startup_logs, startup_complete, startup_processed_rows, startup_total_rows, startup_data_loaded

    startup_logs = []
    startup_complete = False
    startup_data_loaded = False
    startup_processed_rows = 0
    startup_total_rows = 0

    # Log which database is being used
    if USE_POSTGRES:
        add_startup_log("✓ Using PostgreSQL database (persistent storage across restarts).")
    else:
        add_startup_log("ℹ Using SQLite database (local development mode). Set DATABASE_URL for PostgreSQL.")

    # Initialize database
    init_db()
    add_startup_log("Database initialized.")

    add_startup_log("Starting server...")

    # Launch background loading — server starts immediately
    asyncio.create_task(load_startup_data_async())


@app.get("/")
def root():
    """Health check endpoint."""
    return {
        "status": "ok",
        "data_loaded": startup_data_loaded,
        "startup_complete": startup_complete,
        "sku_count": len(sku_profiles)
    }


@app.get("/startup_logs")
def get_startup_logs():
    """Return startup logs + status. Frontend polls this on mount."""
    return {
        "complete": startup_complete,
        "data_loaded": startup_data_loaded,
        "logs": startup_logs,
        "total_rows": startup_total_rows,
        "processed_rows": startup_processed_rows
    }


@app.post("/predict_uom")
def predict_uom(req: PredictRequest):
    if not req.sku_code or not req.sku_code.strip():
        return {"status": "error", "message": "Manual Review Required: SKU code is required."}
    if req.input_price is None or req.input_price <= 0:
        return {"status": "error", "message": "Manual Review Required: Input price must be a positive number."}
    if req.input_price > 1e15:
        return {"status": "error", "message": "Manual Review Required: Input price is unrealistically large. Please verify the value."}

    sku_profile = sku_profiles.get(req.sku_code.strip())
    if not sku_profile:
        # Fall back to website price lookup if SKU has no inward data
        wp_data = website_price_lookup.get(req.sku_code.strip())
        if wp_data:
            latest_br = wp_data['base_rate']
            valid_uoms = uom_master_lookup.get(req.sku_code.strip(), {})
            if not valid_uoms:
                return {"status": "error", "message": "Manual Review Required: SKU found in website prices but no valid UOMs defined in UOM master."}
            sku_profile = {'latest_br': latest_br, 'valid_uoms': valid_uoms}
        else:
            return {"status": "error", "message": "Manual Review Required: SKU not found or insufficient historical data."}
    
    latest_br = sku_profile['latest_br']
    input_price = req.input_price
    
    candidates = []
    closest_overall = None
    min_score = float('inf')
    
    ratio_lower = config.get("prediction", {}).get("acceptable_ratio_lower", 0.2)
    ratio_upper = config.get("prediction", {}).get("acceptable_ratio_upper", 1.8)
    
    for uom, cf in sku_profile['valid_uoms'].items():
        expected_price = latest_br * cf
        ratio = input_price / expected_price if expected_price > 0 else 0
        
        score = abs(math.log(ratio)) if ratio > 0 else float('inf')
        if score < min_score:
            min_score = score
            closest_overall = {'uom': uom, 'cf': cf, 'expected_price': expected_price}
            
        if ratio_lower <= ratio <= ratio_upper:
            candidates.append({'uom': uom, 'cf': cf, 'score': score})
            
    if not candidates:
        msg = f"Manual Review Required: Input price {input_price} is outside acceptable historical margins."
        if closest_overall:
            expected = closest_overall['expected_price']
            min_acc = expected * ratio_lower
            max_acc = expected * ratio_upper
            msg += f" Closest UOM: {closest_overall['uom']} (CF: {closest_overall['cf']})."
            msg += f" Expected price for this UOM is {expected:.2f} (Acceptable range: {min_acc:.2f} - {max_acc:.2f})."
        return {"status": "error", "message": msg}
        
    candidates.sort(key=lambda x: x['score'])
    best = candidates[0]
    best_score = best['score']
    # Convert log-score to confidence (score=0 → confidence=1.0, score=1.0 → confidence=0.5)
    confidence = round(max(0, min(1.0, 1.0 / (1.0 + best_score))), 4)
    # Alternative suggestions for display
    alternatives = [{'uom': c['uom'], 'cf': c['cf'], 'score': round(c['score'], 4)} for c in candidates[1:4]]
    
    return {
        "status": "success",
        "uom": best['uom'],
        "cf": best['cf'],
        "confidence": confidence,
        "alternatives": alternatives
    }


@app.get("/export_outliers")
def export_outliers():
    """Return precomputed Excel outliers report directly from DB cache.
    If the cache is missing or has 0 rows, automatically recompute once to verify.
    """
    excel_bytes, row_count = load_outliers_cache()
    
    # If cache is missing or has 0 rows, try to recompute from raw data
    if excel_bytes is None or row_count == 0:
        # Load raw CSV from database and recompute outliers
        csv_buf, _, _ = load_raw_csv_from_db()
        if csv_buf is not None:
            try:
                outliers_df = compute_outliers_from_csv(csv_buf)
                excel_buffer = io.BytesIO()
                with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                    outliers_df.to_excel(writer, index=False, sheet_name='Outliers')
                excel_bytes_new = excel_buffer.getvalue()
                row_count_new = len(outliers_df)
                save_outliers_cache(excel_bytes_new, row_count_new)
                excel_bytes = excel_bytes_new
                row_count = row_count_new
            except Exception as e:
                return {"status": "error", "message": f"Failed to recompute outliers: {str(e)}"}
        else:
            return {"status": "error", "message": "No data available. Please upload a CSV file first."}
    
    # If still 0 rows after recomputation, return a proper empty Excel
    if row_count == 0:
        # Return an empty Excel file with the outliers sheet
        empty_buffer = io.BytesIO()
        with pd.ExcelWriter(empty_buffer, engine='openpyxl') as writer:
            pd.DataFrame().to_excel(writer, index=False, sheet_name='Outliers')
        excel_bytes = empty_buffer.getvalue()
    
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=outliers_report.xlsx"}
    )


async def process_upload_async(task_id: str, file_path: str, total_rows: int):
    """Process a large GRN upload file in the background with progress tracking.
    Uses asyncio.to_thread to avoid blocking the event loop.
    Also stores raw data in DB and precomputes outliers.
    """
    try:
        # Update task status to show we're starting
        upload_tasks[task_id]["logs"].append(f"Starting to process {total_rows:,} rows...")
        
        def upload_log_callback(msg, processed, total):
            task = upload_tasks.get(task_id)
            if task is None:
                return
            task["logs"].append(msg)
            if len(task["logs"]) > 100:
                task["logs"] = task["logs"][-100:]
            task["processed_rows"] = processed
            if total:
                task["total_rows"] = total
                task["percentage"] = min(99, int((processed / total) * 100))
        
        def validate_columns_in_thread():
            df_check = pd.read_csv(file_path, nrows=1)
            required = ['SKU Code', 'Price']
            missing = [c for c in required if c not in df_check.columns]
            if missing:
                raise ValueError(f"CSV missing required columns: {missing}. Make sure the file contains 'SKU Code' and 'Price' columns.")
        
        try:
            await asyncio.to_thread(validate_columns_in_thread)
        except ValueError as ve:
            upload_tasks[task_id].update({
                "status": "error",
                "percentage": 0,
                "processed_rows": 0,
                "total_rows": total_rows,
                "message": f"Upload failed: {str(ve)}",
                "logs": upload_tasks[task_id].get("logs", []) + [f"✗ {str(ve)}"]
            })
            try:
                os.remove(file_path)
            except:
                pass
            return

        def process_in_thread():
            chunks = pd.read_csv(file_path, chunksize=50000, low_memory=False)
            return build_sku_profiles_from_chunks(chunks, log_callback=upload_log_callback, known_total_rows=total_rows)
        
        upload_log_callback("Reading and processing CSV chunks...", 0, total_rows)
        
        # Run CPU-bound work in thread pool so event loop stays free for polling
        profiles = await asyncio.to_thread(process_in_thread)
        
        # Update global state
        global sku_profiles, global_df, startup_data_loaded, grn_template_available
        upload_log_callback(f"Updating system with {len(profiles)} SKU profiles...", total_rows, total_rows)
        if not profiles:
            upload_logs_entry = upload_tasks[task_id]["logs"]
            upload_logs_entry.append("✗ Error: No valid SKU profiles found in CSV. Missing required columns (SKU Code, Price).")
            upload_tasks[task_id].update({
                "status": "error",
                "percentage": 0,
                "processed_rows": total_rows,
                "message": "Upload failed: CSV file has no valid SKU profiles. Please check that the file contains 'SKU Code' and 'Price' columns.",
                "logs": upload_logs_entry
            })
            try:
                os.remove(file_path)
            except:
                pass
            return
        sku_profiles = profiles
        startup_data_loaded = True
        grn_template_available = True
        
        # Read first chunk as sample for global_df
        sample_df = pd.read_csv(file_path, nrows=5)
        global_df = sample_df.copy()
        
        # Store raw data in DB (read full file from disk)
        upload_log_callback("Storing raw data in database...", total_rows, total_rows)
        with open(file_path, 'rb') as f:
            csv_bytes = f.read()
        save_raw_data_to_db(csv_bytes, os.path.basename(file_path), total_rows)
        
        # Save profiles to DB
        upload_log_callback("Saving SKU profiles to database...", total_rows, total_rows)
        save_profiles_to_db(profiles)
        
        # Precompute outliers and cache as Excel in DB
        upload_log_callback("Precomputing outliers report...", total_rows, total_rows)
        csv_buf = io.BytesIO(csv_bytes)
        outlier_row_count, file_size = precompute_and_cache_outliers(csv_buf)
        
        # Clean up temp file
        try:
            os.remove(file_path)
        except:
            pass
        
        upload_tasks[task_id].update({
            "status": "success",
            "percentage": 100,
            "processed_rows": total_rows,
            "message": f"Successfully loaded {len(sku_profiles):,} SKUs from {total_rows:,} rows. Outliers: {outlier_row_count:,} rows.",
            "logs": upload_tasks[task_id]["logs"] + [
                f"✓ Profiles saved to database.",
                f"✓ Outliers report cached ({outlier_row_count:,} rows)."
            ]
        })
    except Exception as e:
        upload_tasks[task_id].update({
            "status": "error",
            "percentage": 0,
            "message": f"Failed to process file: {str(e)}",
            "logs": upload_tasks[task_id].get("logs", []) + [f"✗ Error: {str(e)}"]
        })


async def process_sales_upload_async(task_id: str, file_path: str, total_rows: int):
    """Process a Sales CSV upload in the background.
    Validates against existing GRN SKU profiles and precomputes outliers.
    Uses asyncio.to_thread to avoid blocking the event loop during CPU-bound work.
    """
    try:
        upload_tasks[task_id]["logs"].append(f"Starting to process sales data ({total_rows:,} rows)...")

        def sales_log_callback(msg, processed, total):
            task = upload_tasks.get(task_id)
            if task is None:
                return
            task["logs"].append(msg)
            if len(task["logs"]) > 100:
                task["logs"] = task["logs"][-100:]
            task["processed_rows"] = processed
            if total:
                task["total_rows"] = total
                task["percentage"] = min(99, int((processed / total) * 100))

        # Read full CSV (I/O bound — fast)
        sales_log_callback("Reading sales file...", 0, total_rows)
        with open(file_path, 'rb') as f:
            csv_bytes = f.read()
        sales_log_callback("Sales file read complete.", 10, total_rows)

        # Validate GRN profiles exist
        global sku_profiles
        if not sku_profiles:
            sales_log_callback("⚠ No GRN profiles loaded. Sales outliers cannot be computed.", total_rows, total_rows)
            upload_tasks[task_id].update({
                "status": "error",
                "percentage": 100,
                "processed_rows": total_rows,
                "message": "No GRN profiles available. Please upload GRN data first.",
                "logs": upload_tasks[task_id]["logs"] + [f"✗ Aborted: No GRN profiles found."]
            })
            try:
                os.remove(file_path)
            except:
                pass
            return

        # Store sales raw data in DB (I/O bound — fast)
        sales_log_callback("Storing sales data in database...", 20, total_rows)
        save_sales_data_to_db(csv_bytes, os.path.basename(file_path), total_rows)
        sales_log_callback("Sales data stored in database.", 30, total_rows)

        # Compute sales outliers — CPU-bound, run in thread pool
        sales_log_callback("Computing sales outliers against GRN profiles...", 30, total_rows)

        def compute_sales_in_thread():
            csv_buf = io.BytesIO(csv_bytes)
            return precompute_and_cache_sales_outliers(csv_buf, log_callback=sales_log_callback)

        outlier_count, loss_count = await asyncio.to_thread(compute_sales_in_thread)

        # Clean up temp file
        try:
            os.remove(file_path)
        except:
            pass

        upload_tasks[task_id].update({
            "status": "success",
            "percentage": 100,
            "processed_rows": total_rows,
            "message": f"Sales data processed: {total_rows:,} rows analyzed. Outliers: {outlier_count:,}, SKUs with loss: {loss_count:,}.",
            "logs": upload_tasks[task_id]["logs"] + [
                f"✓ Sales data stored ({len(csv_bytes):,} bytes).",
                f"✓ Sales outliers computed ({outlier_count:,} rows).",
                f"✓ Sales loss summary cached ({loss_count:,} SKUs)."
            ]
        })
    except Exception as e:
        upload_tasks[task_id].update({
            "status": "error",
            "percentage": 0,
            "message": f"Failed to process sales file: {str(e)}",
            "logs": upload_tasks[task_id].get("logs", []) + [f"✗ Error: {str(e)}"]
        })


@app.post("/upload_data")
async def upload_data(file: UploadFile = File(...), file_type: str = Form("grn")):
    global sku_profiles, global_df
    
    # Validate file extension
    if not file.filename.endswith('.csv'):
        return {"status": "error", "message": "Only .csv files are accepted. Please upload a CSV file."}
    
    # Validate file_type
    if file_type not in ("grn", "sales"):
        return {"status": "error", "message": "Invalid file_type. Use 'grn' for GRN/PO data or 'sales' for Sales data."}
    
    try:
        # Read file content
        contents = await file.read()
        
        # Count rows without loading full file into memory
        row_count = contents.count(b'\n') - 1  # subtract header
        if row_count <= 0:
            return {"status": "error", "message": "File appears to be empty or has no data rows."}
        
        # Generate task ID
        task_id = str(uuid.uuid4())
        
        # Save to temp file
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"{file_type}_upload_{task_id}.csv")
        with open(temp_path, 'wb') as f:
            f.write(contents)
        
        # Initialize task status with progress tracking
        upload_tasks[task_id] = {
            "status": "processing",
            "percentage": 0,
            "processed_rows": 0,
            "total_rows": row_count,
            "file_type": file_type,
            "message": f"Queued {row_count:,} rows for processing...",
            "logs": [f"File received: {file.filename} ({row_count:,} rows, type={file_type})", "Queued for background processing..."]
        }
        
        # Launch background processing based on type
        if file_type == "sales":
            asyncio.create_task(process_sales_upload_async(task_id, temp_path, row_count))
        else:
            asyncio.create_task(process_upload_async(task_id, temp_path, row_count))
        
        return {
            "status": "accepted",
            "task_id": task_id,
            "file_type": file_type,
            "message": f"{'Sales' if file_type == 'sales' else 'GRN'} file with {row_count:,} rows is being processed in the background."
        }
            
    except Exception as e:
        return {"status": "error", "message": f"Failed to process file: {str(e)}"}


async def process_uom_master_async(task_id: str, file_path: str, total_rows: int):
    """Process UOM master upload in background with progress tracking."""
    global uom_master_lookup, sku_profiles, website_price_lookup
    try:
        task = upload_tasks.get(task_id)
        if task:
            task["logs"].append("Reading UOM master CSV...")

        df = pd.read_csv(file_path, low_memory=False)

        if task:
            task["logs"].append(f"Validating {len(df):,} rows...")
            task["percentage"] = 20

        required = ['old_sku_id', 'UOM', 'cf']
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"UOM master CSV missing required columns: {missing}. Required: old_sku_id, UOM, cf")

        df['cf'] = df['cf'].astype(str).str.replace(',', '', regex=False)
        df['cf'] = pd.to_numeric(df['cf'], errors='coerce').fillna(1).astype(int)

        if task:
            task["logs"].append("Saving to database...")
            task["percentage"] = 40

        save_uom_master_to_db(df)

        if task:
            task["logs"].append(f"Rebuilding UOM lookup ({len(uom_master_lookup)} SKUs)...")
            task["percentage"] = 60

        uom_master_lookup = build_uom_master_lookup_from_db()
        website_price_lookup = build_website_price_lookup_from_db()

        if task:
            task["logs"].append(f"UOM lookup: {len(uom_master_lookup)} SKUs, website prices: {len(website_price_lookup)} SKUs.")
            task["logs"].append("Updating SKU profiles...")
            task["percentage"] = 80

        update_sku_profiles_valid_uoms_from_master()
        save_profiles_to_db(sku_profiles)

        if task:
            task["logs"].append(f"Saving profiles ({len(sku_profiles)} SKUs)...")
            task["percentage"] = 90

        try:
            os.remove(file_path)
        except:
            pass

        if task:
            task.update({
                "status": "success",
                "percentage": 100,
                "processed_rows": total_rows,
                "message": f"UOM master uploaded: {total_rows:,} rows processed. {len(uom_master_lookup)} SKUs in master.",
                "logs": task["logs"] + [
                    f"✓ UOM master saved to database.",
                    f"✓ SKU profiles updated with new UOM mappings."
                ]
            })
    except Exception as e:
        task = upload_tasks.get(task_id)
        if task:
            task.update({
                "status": "error",
                "percentage": 0,
                "message": f"Failed to process UOM master file: {str(e)}",
                "logs": task.get("logs", []) + [f"✗ Error: {str(e)}"]
            })


@app.post("/upload_uom_master")
async def upload_uom_master(file: UploadFile = File(...)):
    """Upload UOM master CSV (old_sku_id, listing_title, business_category, UOM, Flag, cf, uom_type).
    Replaces all existing UOM master data and rebuilds sku_profiles valid_uoms."""

    if not file.filename.endswith('.csv'):
        return {"status": "error", "message": "Only .csv files are accepted. Please upload a CSV file."}

    try:
        contents = await file.read()
        row_count = contents.count(b'\n') - 1
        if row_count <= 0:
            return {"status": "error", "message": "File appears to be empty or has no data rows."}

        task_id = str(uuid.uuid4())
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"uom_master_upload_{task_id}.csv")
        with open(temp_path, 'wb') as f:
            f.write(contents)

        upload_tasks[task_id] = {
            "status": "processing",
            "percentage": 0,
            "processed_rows": 0,
            "total_rows": row_count,
            "file_type": "uom_master",
            "message": f"Queued {row_count:,} rows for processing...",
            "logs": [f"File received: {file.filename} ({row_count:,} rows, type=uom_master)", "Queued for background processing..."]
        }

        asyncio.create_task(process_uom_master_async(task_id, temp_path, row_count))

        return {
            "status": "accepted",
            "task_id": task_id,
            "file_type": "uom_master",
            "message": f"UOM master file with {row_count:,} rows is being processed in the background."
        }

    except Exception as e:
        return {"status": "error", "message": f"Failed to process file: {str(e)}"}


@app.get("/upload_status/{task_id}")
def upload_status(task_id: str):
    """Poll this endpoint to check the status of an async upload.
    
    Returns:
        status: "processing" | "success" | "error"
        percentage: int (0-100)
        processed_rows: int
        total_rows: int
        logs: list[str]
        message: str
    """
    task = upload_tasks.get(task_id)
    if not task:
        return {"status": "error", "message": "Task ID not found."}
    return task


@app.get("/download_template")
def download_template():
    """Download an empty CSV template with the same columns as the loaded data."""
    if global_df is None:
        return {"status": "error", "message": "Data not loaded yet."}
    
    template_df = pd.DataFrame(columns=global_df.columns)
    
    csv_buffer = io.StringIO()
    template_df.to_csv(csv_buffer, index=False)
    
    return Response(
        content=csv_buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=grn_template.csv"}
    )


@app.get("/download_sales_template")
def download_sales_template():
    """Download an empty CSV template for sales data."""
    sales_columns = ['Date', 'Order ID', 'SKU Code', 'Sales Price', 'Sales UOM', 'Sales Qty']
    template_df = pd.DataFrame(columns=sales_columns)
    
    csv_buffer = io.StringIO()
    template_df.to_csv(csv_buffer, index=False)
    
    return Response(
        content=csv_buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sales_template.csv"}
    )


@app.get("/download_uom_master_template")
def download_uom_master_template():
    """Download an empty CSV template for UOM master data."""
    columns = ['old_sku_id', 'listing_title', 'business_category', 'UOM', 'Flag', 'cf', 'uom_type']
    template_df = pd.DataFrame(columns=columns)
    csv_buffer = io.StringIO()
    template_df.to_csv(csv_buffer, index=False)
    return Response(
        content=csv_buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=uom_master_template.csv"}
    )


@app.get("/uom_master_status")
def get_uom_master_status():
    """Return the current UOM master status."""
    return {
        "loaded": len(uom_master_lookup) > 0,
        "sku_count": len(uom_master_lookup)
    }


@app.get("/export_progress")
def get_export_progress():
    """Return the computation progress of all export reports."""
    global grn_outliers_progress, sales_outliers_progress, sales_loss_progress
    global grn_template_available, sales_template_available
    
    return {
        "grn_outliers": grn_outliers_progress,
        "sales_outliers": sales_outliers_progress,
        "sales_loss": sales_loss_progress,
        "grn_template_available": grn_template_available,
        "sales_template_available": sales_template_available
    }


# ─── Sales API Endpoints ────────────────────────────────────────────────────────


@app.get("/export_sales_outliers")
def export_sales_outliers():
    """Return precomputed Sales outliers Excel report from DB cache."""
    excel_bytes, row_count = load_sales_outliers_cache()
    
    # If cache is missing, try to recompute from sales raw data
    if excel_bytes is None or row_count == 0:
        csv_buf, csv_row_count = load_sales_csv_from_db()
        if csv_buf is not None and sku_profiles:
            try:
                precompute_and_cache_sales_outliers(csv_buf)
                excel_bytes, row_count = load_sales_outliers_cache()
            except Exception as e:
                return {"status": "error", "message": f"Failed to recompute sales outliers: {str(e)}"}
        else:
            msg = "No sales data available." if csv_buf is None else "No GRN profiles loaded. Upload GRN data first."
            return {"status": "error", "message": msg}
    
    if row_count == 0:
        empty_buffer = io.BytesIO()
        with pd.ExcelWriter(empty_buffer, engine='openpyxl') as writer:
            pd.DataFrame().to_excel(writer, index=False, sheet_name='Sales Outliers')
        excel_bytes = empty_buffer.getvalue()
    
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=sales_outliers_report.xlsx"}
    )


@app.get("/export_sales_loss_summary")
def export_sales_loss_summary():
    """Return precomputed Sales Loss Summary Excel report from DB cache."""
    excel_bytes, row_count = load_sales_loss_summary_cache()
    
    if excel_bytes is None or row_count == 0:
        csv_buf, csv_row_count = load_sales_csv_from_db()
        if csv_buf is not None and sku_profiles:
            try:
                precompute_and_cache_sales_outliers(csv_buf)
                excel_bytes, row_count = load_sales_loss_summary_cache()
            except Exception as e:
                return {"status": "error", "message": f"Failed to recompute sales loss summary: {str(e)}"}
        else:
            msg = "No sales data available." if csv_buf is None else "No GRN profiles loaded. Upload GRN data first."
            return {"status": "error", "message": msg}
    
    if row_count == 0:
        empty_buffer = io.BytesIO()
        with pd.ExcelWriter(empty_buffer, engine='openpyxl') as writer:
            pd.DataFrame().to_excel(writer, index=False, sheet_name='Sales Loss Summary')
        excel_bytes = empty_buffer.getvalue()
    
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=sales_loss_summary.xlsx"}
    )


@app.post("/analyze_grn_quality")
async def analyze_grn_quality(file: UploadFile = File(...)):
    """Upload GRN CSV and get a comprehensive quality analysis + correction suggestions.
    Returns per-row analysis with confidence scores and suggested corrections."""
    global uom_master_lookup, website_price_lookup, sku_profiles

    if not file.filename.endswith('.csv'):
        return {"status": "error", "message": "Only .csv files are accepted."}

    try:
        contents = await file.read()
        df = pd.read_csv(io.BytesIO(contents), low_memory=False)

        rename_map = {"PO Purchase Rate": "Price", "invoice_date": "Date"}
        df.rename(columns=rename_map, inplace=True)

        required = ['SKU Code', 'Price']
        missing = [c for c in required if c not in df.columns]
        if missing:
            return {"status": "error", "message": f"CSV missing required columns: {missing}"}

        df['Price'] = pd.to_numeric(df['Price'], errors='coerce')
        df.dropna(subset=['Price'], inplace=True)
        df['Date'] = pd.to_datetime(df['Date'], errors='coerce') if 'Date' in df.columns else None

        conf_weights = config.get("confidence_scoring", {})
        wp_lower_mult = config.get("outlier_detection", {}).get("website_lower_multiplier", 0.7)
        wp_upper_mult = config.get("outlier_detection", {}).get("website_upper_multiplier", 1.5)

        analysis_rows = []
        total_rows = len(df)
        cf_found_count = 0
        website_cross_check_count = 0
        recommended_count = 0

        for _, row in df.iterrows():
            sku = str(row.get('SKU Code', '')).strip()
            price = row.get('Price', 0)
            uom_raw = str(row.get('alternate_uom', '')) if pd.notna(row.get('alternate_uom')) else ''
            csv_cf = row.get('CF', 1.0) if pd.notna(row.get('CF', 1.0)) else 1.0

            entry = {
                'SKU Code': sku,
                'Price': price,
                'alternate_uom': uom_raw,
                'CSV_CF': csv_cf,
                'Date': str(row.get('Date', '')) if pd.notna(row.get('Date')) else '',
            }

            # Resolve CF from UOM Master
            extracted_uom = extract_uom_name(uom_raw) if uom_raw else ''
            master_cf = uom_master_lookup.get(sku, {}).get(extracted_uom) if sku and extracted_uom else None

            if master_cf is not None:
                entry['CF_Source'] = 'UOM Master'
                entry['Effective_CF'] = master_cf
                entry['Confidence'] = conf_weights.get("uom_master_source_weight", 0.9)
                cf_found_count += 1
            else:
                entry['CF_Source'] = 'CSV Fallback'
                entry['Effective_CF'] = csv_cf
                entry['Confidence'] = conf_weights.get("csv_fallback_source_weight", 0.3)

            entry['Computed_Base_Rate'] = round(price / entry['Effective_CF'], 4) if entry['Effective_CF'] > 0 else 0

            # Cross-check against expected base rate
            profile = sku_profiles.get(sku)
            if profile:
                expected_br = profile.get('latest_br', 0)
                entry['Expected_Base_Rate'] = round(expected_br, 4)
                ratio = entry['Computed_Base_Rate'] / expected_br if expected_br > 0 else 0
                entry['Deviation_Ratio'] = round(ratio, 4)

                stat_lower = config.get("outlier_detection", {}).get("statistical_lower_multiplier", 0.3)
                stat_upper = config.get("outlier_detection", {}).get("statistical_upper_multiplier", 3.0)

                is_stat_outlier = ratio < stat_lower or ratio > stat_upper
                entry['Statistical_Outlier'] = is_stat_outlier

                if is_stat_outlier:
                    # Suggest correction
                    recommended_count += 1
                    entry['Suggested_Correct_Price'] = round(expected_br * entry['Effective_CF'], 2)
                    if master_cf is not None:
                        # Also suggest what UOM would give the correct price
                        correct_uom_options = []
                        for uom_name, uom_cf in uom_master_lookup.get(sku, {}).items():
                            expected = round(expected_br * uom_cf, 2)
                            diff = abs(price - expected)
                            correct_uom_options.append({'uom': uom_name, 'cf': uom_cf, 'expected_price': expected, 'price_diff': round(diff, 2)})
                        correct_uom_options.sort(key=lambda x: x['price_diff'])
                        entry['Suggested_UOM_Options'] = correct_uom_options[:3]
                        entry['Suggested_UOM'] = correct_uom_options[0]['uom'] if correct_uom_options else ''
                        entry['Suggested_UOM_CF'] = correct_uom_options[0]['cf'] if correct_uom_options else ''
                else:
                    entry['Suggested_Correct_Price'] = ''
                    entry['Suggested_UOM'] = ''
                    entry['Suggested_UOM_CF'] = ''
            else:
                entry['Expected_Base_Rate'] = ''
                entry['Deviation_Ratio'] = ''
                entry['Statistical_Outlier'] = ''
                entry['Suggested_Correct_Price'] = ''
                entry['Suggested_UOM'] = ''
                entry['Suggested_UOM_CF'] = ''

            # Website cross-validation
            wp_data = website_price_lookup.get(sku)
            if wp_data:
                website_cross_check_count += 1
                wp_br = wp_data['base_rate']
                entry['Website_Base_Rate'] = round(wp_br, 4)
                wp_ratio = entry['Computed_Base_Rate'] / wp_br if wp_br > 0 else 0
                entry['Website_Price_Ratio'] = round(wp_ratio, 4)
                wp_lower = wp_br * wp_lower_mult
                wp_upper = wp_br * wp_upper_mult
                entry['Website_In_Range'] = wp_lower <= entry['Computed_Base_Rate'] <= wp_upper
                if not entry['Website_In_Range']:
                    entry['Website_Corrected_Price'] = round(wp_br * entry['Effective_CF'], 2)
                    if not entry.get('Statistical_Outlier'):
                        recommended_count += 1
                else:
                    entry['Website_Corrected_Price'] = ''
            else:
                entry['Website_Base_Rate'] = ''
                entry['Website_Price_Ratio'] = ''
                entry['Website_In_Range'] = ''
                entry['Website_Corrected_Price'] = ''

            analysis_rows.append(entry)

        result_df = pd.DataFrame(analysis_rows)
        high_confidence = result_df[result_df['Confidence'] >= conf_weights.get("minimum_confidence_for_auto_suggest", 0.6)] if not result_df.empty else pd.DataFrame()

        return {
            "status": "success",
            "total_rows": total_rows,
            "uom_master_cf_found": cf_found_count,
            "uom_master_cf_missing": total_rows - cf_found_count,
            "website_available_for": website_cross_check_count,
            "corrections_recommended": recommended_count,
            "high_confidence_corrections": len(high_confidence[high_confidence['Statistical_Outlier'] == True]) if not high_confidence.empty else 0,
        }

    except Exception as e:
        return {"status": "error", "message": f"Analysis failed: {str(e)}"}


@app.post("/analyze_sales_quality")
async def analyze_sales_quality(file: UploadFile = File(...)):
    """Upload Sales CSV and get comprehensive quality analysis + correction suggestions."""
    global uom_master_lookup, sku_profiles, website_price_lookup

    if not file.filename.endswith('.csv'):
        return {"status": "error", "message": "Only .csv files are accepted."}

    try:
        contents = await file.read()
        sales_df = pd.read_csv(io.BytesIO(contents), low_memory=False)

        required = ['SKU Code', 'Sales Price', 'Sales UOM']
        missing = [c for c in required if c not in sales_df.columns]
        if missing:
            return {"status": "error", "message": f"CSV missing required columns: {missing}"}

        sales_df['Sales Price'] = pd.to_numeric(sales_df['Sales Price'], errors='coerce')
        sales_df.dropna(subset=['Sales Price'], inplace=True)

        conf_weights = config.get("confidence_scoring", {})
        lower_mult = config.get("sales_analysis", {}).get("normalized_price_lower_multiplier", 0.6)
        upper_mult = config.get("sales_analysis", {}).get("normalized_price_upper_multiplier", 3.0)

        analysis_rows = []
        total_rows = len(sales_df)
        uom_master_matched = 0
        recommended_count = 0

        for _, row in sales_df.iterrows():
            sku = str(row.get('SKU Code', '')).strip()
            sales_price = row.get('Sales Price', 0)
            sales_uom = str(row.get('Sales UOM', '')).strip()
            extracted_uom = extract_uom_name(sales_uom)
            sales_qty = int(row.get('Sales Qty', 0)) if pd.notna(row.get('Sales Qty', 0)) else 0
            order_id = row.get('Order ID', '')
            sales_date = row.get('Date', '')

            if not sku:
                continue

            entry = {
                'Date': str(sales_date) if pd.notna(sales_date) else '',
                'Order ID': str(order_id) if pd.notna(order_id) else '',
                'SKU Code': sku,
                'Sales UOM': sales_uom,
                'Extracted_UOM': extracted_uom,
                'Sales Price': sales_price,
                'Sales Qty': sales_qty,
            }

            profile = sku_profiles.get(sku)
            if not profile:
                entry['Status'] = 'SKU Not Found'
                entry['Confidence'] = 0.0
                entry['Suggested_Correct_Price'] = ''
                entry['Suggested_UOM'] = ''
                analysis_rows.append(entry)
                continue

            latest_br = profile.get('latest_br', 0)
            valid_uoms = profile.get('valid_uoms', {})
            entry['Expected_Base_Rate'] = round(latest_br, 4) if latest_br else ''

            # Look up CF from UOM Master first
            cf = uom_master_lookup.get(sku, {}).get(extracted_uom)

            if cf is not None:
                entry['CF_Source'] = 'UOM Master'
                entry['Confidence'] = conf_weights.get("uom_master_source_weight", 0.9)
                uom_master_matched += 1
            elif valid_uoms:
                if sales_uom in valid_uoms:
                    cf = valid_uoms[sales_uom]
                    entry['CF_Source'] = 'GRN'
                    entry['Confidence'] = conf_weights.get("grn_pattern_source_weight", 0.6)
                else:
                    for uom_key in valid_uoms:
                        if extracted_uom.lower() in uom_key.lower():
                            cf = valid_uoms[uom_key]
                            entry['CF_Source'] = 'GRN'
                            entry['Confidence'] = conf_weights.get("grn_pattern_source_weight", 0.6)
                            break

            if cf is None:
                entry['Status'] = 'UOM Not Found'
                entry['Confidence'] = 0.2
                entry['Effective_CF'] = ''
                entry['Normalized_Price'] = ''
                entry['Expected_Price'] = ''
                entry['Suggested_Correct_Price'] = ''
                entry['Suggested_UOM'] = ''
                analysis_rows.append(entry)
                continue

            entry['Effective_CF'] = cf
            normalized_price = sales_price / cf
            entry['Normalized_Price'] = round(normalized_price, 4)
            expected_price = latest_br * cf if latest_br else 0
            entry['Expected_Price'] = round(expected_price, 2)
            ratio = normalized_price / latest_br if latest_br > 0 else 0

            is_outlier = ratio < lower_mult or ratio > upper_mult
            entry['Is_Outlier'] = is_outlier
            entry['Deviation_Ratio'] = round(ratio, 4)

            if is_outlier:
                recommended_count += 1
                entry['Suggested_Correct_Price'] = round(expected_price, 2)
                # Suggest alternative UOM that would match the price
                if latest_br:
                    candidates = []
                    for uom_name, uom_cf in valid_uoms.items():
                        exp = latest_br * uom_cf
                        score = abs(math.log(sales_price / exp)) if exp > 0 else float('inf')
                        candidates.append({'uom': uom_name, 'cf': uom_cf, 'expected_price': round(exp, 2), 'score': round(score, 4)})
                    candidates.sort(key=lambda x: x['score'])
                    entry['Suggested_UOM_Options'] = candidates[:3]
                    entry['Suggested_UOM'] = candidates[0]['uom'] if candidates else extracted_uom
                else:
                    entry['Suggested_UOM'] = extracted_uom

                # Sales loss
                price_diff = expected_price - sales_price
                entry['Price_Difference'] = round(price_diff, 2)
                entry['Sales_Loss'] = round(max(0, price_diff * sales_qty), 2)
            else:
                entry['Suggested_Correct_Price'] = ''
                entry['Suggested_UOM'] = extracted_uom
                entry['Price_Difference'] = ''
                entry['Sales_Loss'] = 0

            entry['Status'] = 'Flagged' if is_outlier else 'OK'
            analysis_rows.append(entry)

        result_df = pd.DataFrame(analysis_rows)
        flagged = result_df[result_df['Is_Outlier'] == True] if 'Is_Outlier' in result_df.columns else pd.DataFrame()

        return {
            "status": "success",
            "total_rows": total_rows,
            "uom_master_matched": uom_master_matched,
            "uom_master_missing": total_rows - uom_master_matched,
            "flagged_count": len(flagged),
            "corrections_recommended": recommended_count,
            "total_sales_loss": round(flagged['Sales_Loss'].sum(), 2) if not flagged.empty and 'Sales_Loss' in flagged.columns else 0,
        }

    except Exception as e:
        return {"status": "error", "message": f"Analysis failed: {str(e)}"}


@app.get("/export_correction_report")
def export_correction_report():
    """Export a combined Excel report with GRN + Sales corrections and UOM master gaps."""
    try:
        excel_buffer = io.BytesIO()

        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            # Sheet 1: UOM Master Gaps (SKUs in GRN data but not in UOM Master)
            gap_rows = []
            for sku, profile in sku_profiles.items():
                if sku not in uom_master_lookup:
                    valid_count = len(profile.get('valid_uoms', {}))
                    if valid_count == 0:
                        gap_rows.append({
                            'SKU Code': sku,
                            'Latest Base Rate': round(profile.get('latest_br', 0), 4),
                            'Source': 'GRN Only',
                            'Missing From': 'UOM Master'
                        })

            gaps_df = pd.DataFrame(gap_rows)
            if not gaps_df.empty:
                gaps_df = gaps_df.sort_values('SKU Code')
            gaps_df.to_excel(writer, index=False, sheet_name='UOM Master Gaps')

            # Sheet 2: Summary
            summary_data = [{
                'Metric': 'Total SKUs in Profiles',
                'Value': len(sku_profiles)
            }, {
                'Metric': 'SKUs in UOM Master',
                'Value': len(uom_master_lookup)
            }, {
                'Metric': 'SKUs with Website Prices',
                'Value': len(website_price_lookup)
            }, {
                'Metric': 'SKUs Missing from UOM Master',
                'Value': len(gap_rows)
            }, {
                'Metric': 'Website Override Lower Multiplier',
                'Value': config.get("grn_profiling", {}).get("website_override_lower_multiplier", 0.7)
            }, {
                'Metric': 'Website Override Upper Multiplier',
                'Value': config.get("grn_profiling", {}).get("website_override_upper_multiplier", 1.5)
            }, {
                'Metric': 'Statistical Outlier Lower Multiplier',
                'Value': config.get("outlier_detection", {}).get("statistical_lower_multiplier", 0.3)
            }, {
                'Metric': 'Statistical Outlier Upper Multiplier',
                'Value': config.get("outlier_detection", {}).get("statistical_upper_multiplier", 3.0)
            }]
            summary_df = pd.DataFrame(summary_data)
            summary_df.to_excel(writer, index=False, sheet_name='Summary')

        excel_bytes = excel_buffer.getvalue()

        return Response(
            content=excel_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=correction_report.xlsx"}
        )
    except Exception as e:
        return {"status": "error", "message": f"Failed to generate report: {str(e)}"}


def compute_sku_quality_report():
    """
    Generate a consolidated per-SKU quality report covering all 3 problem types:
    1. GRN was done on invalid UOM/Price combination
    2. Website price and UOM combination is incorrect
    3. Sales happened at wrong UOM/price combination
    Returns (excel_bytes, row_count).
    """
    # Load UOM master for listing titles and categories
    uom_master_df = load_uom_master_from_db()
    sku_meta = {}
    if not uom_master_df.empty:
        for _, row in uom_master_df.iterrows():
            sku = str(row.get('old_sku_id', '')).strip()
            if sku and sku not in sku_meta:
                sku_meta[sku] = {
                    'listing_title': row.get('listing_title', '') or '',
                    'business_category': row.get('business_category', '') or '',
                }

    # --- Parse GRN outliers (Problem #1) ---
    grn_agg = {}
    grn_excel, grn_count = load_outliers_cache()
    if grn_excel and grn_count > 0:
        try:
            grn_df = pd.read_excel(io.BytesIO(grn_excel), sheet_name='Outliers')
            if not grn_df.empty and 'SKU Code' in grn_df.columns:
                for sku, group in grn_df.groupby('SKU Code'):
                    prices = pd.to_numeric(group.get('Price', 0), errors='coerce').fillna(0)
                    suggested = pd.to_numeric(group.get('Suggested_Correct_Price', 0), errors='coerce').fillna(0)
                    diff = suggested - prices
                    over = (prices - suggested).clip(lower=0).sum()
                    under = (suggested - prices).clip(lower=0).sum()
                    grn_agg[sku] = {
                        'grn_outlier_rows': len(group),
                        'grn_net_impact': round(diff.sum(), 2),
                        'grn_overpayment': round(over, 2),
                        'grn_underpayment': round(under, 2),
                        'grn_avg_confidence': round(
                            pd.to_numeric(group.get('Confidence', 0), errors='coerce').fillna(0).mean(), 2
                        ),
                    }
        except Exception:
            pass

    # --- Parse Sales Loss Summary (Problem #3) ---
    loss_agg = {}
    loss_excel, loss_count = load_sales_loss_summary_cache()
    if loss_excel and loss_count > 0:
        try:
            loss_df = pd.read_excel(io.BytesIO(loss_excel), sheet_name='Sales Loss Summary')
            if not loss_df.empty and 'SKU Code' in loss_df.columns:
                for _, row in loss_df.iterrows():
                    sku = str(row.get('SKU Code', '')).strip()
                    loss_agg[sku] = {
                        'units_lost': int(row.get('Total Units Sold at Wrong UOM/Price', 0) or 0),
                        'total_sales_loss': round(float(row.get('Total Sales Loss', 0) or 0), 2),
                        'outlier_transactions': int(row.get('Outlier Transaction Count', 0) or 0),
                        'most_common_suggested_uom': row.get('Most Common Suggested UOM', '') or '',
                    }
        except Exception:
            pass

    # Config multipliers
    wp_lower_mult = config.get("outlier_detection", {}).get("website_lower_multiplier", 0.7)
    wp_upper_mult = config.get("outlier_detection", {}).get("website_upper_multiplier", 1.5)

    # Collect all unique SKUs across all sources
    all_skus = set()
    all_skus.update(sku_profiles.keys())
    all_skus.update(uom_master_lookup.keys())
    all_skus.update(website_price_lookup.keys())
    all_skus.update(grn_agg.keys())
    all_skus.update(loss_agg.keys())

    rows = []
    for sku in sorted(all_skus):
        profile = sku_profiles.get(sku)
        wp_data = website_price_lookup.get(sku)
        g = grn_agg.get(sku, {})
        l = loss_agg.get(sku, {})
        meta = sku_meta.get(sku, {})

        grn_br = profile['latest_br'] if profile else None

        # Website price validation (Problem #2)
        wp_valid = ''
        wp_deviation = ''
        wp_suggested = ''
        if wp_data and grn_br and grn_br > 0:
            wp_br = wp_data['base_rate']
            wp_deviation = round((wp_br / grn_br - 1) * 100, 2)
            wp_lower = grn_br * wp_lower_mult
            wp_upper = grn_br * wp_upper_mult
            wp_valid = 'Yes' if wp_lower <= wp_br <= wp_upper else 'No'
            wp_suggested = round(grn_br * wp_data['cf'], 2)

        row = {
            'SKU Code': sku,
            'Listing Title': meta.get('listing_title', ''),
            'Business Category': meta.get('business_category', ''),

            # Problem 1: GRN Invalid UOM/Price Combination
            'GRN Outlier Rows': g.get('grn_outlier_rows', 0),
            'GRN Net Impact ($)': g.get('grn_net_impact', 0),
            'GRN Overpayment ($)': g.get('grn_overpayment', 0),
            'GRN Underpayment ($)': g.get('grn_underpayment', 0),
            'GRN Avg Confidence': g.get('grn_avg_confidence', ''),
            'GRN Issue Flag': 'Yes' if g.get('grn_outlier_rows', 0) > 0 else 'No',

            # Problem 2: Website Price / UOM Incorrect
            'Website Price ($)': round(wp_data['price'], 2) if wp_data else '',
            'Website UOM': wp_data['uom'] if wp_data else '',
            'Website CF': wp_data['cf'] if wp_data else '',
            'Website Base Rate ($)': round(wp_data['base_rate'], 4) if wp_data else '',
            'GRN Base Rate ($)': round(grn_br, 4) if grn_br else '',
            'Website Deviation (%)': wp_deviation,
            'Website Price Valid': wp_valid,
            'Website Suggested Price ($)': wp_suggested,
            'Website Issue Flag': 'Yes' if wp_valid == 'No' else ('No' if wp_valid == 'Yes' else ''),

            # Problem 3: Sales at Wrong UOM/Price
            'Sales Outlier Transactions': l.get('outlier_transactions', 0),
            'Units Sold Wrong': l.get('units_lost', 0),
            'Total Sales Loss ($)': l.get('total_sales_loss', 0),
            'Most Common Suggested UOM': l.get('most_common_suggested_uom', ''),
            'Sales Issue Flag': 'Yes' if l.get('outlier_transactions', 0) > 0 else 'No',
        }
        rows.append(row)

    report_df = pd.DataFrame(rows)

    # Build Summary
    grn_issue = [r for r in rows if r['GRN Issue Flag'] == 'Yes']
    wp_issue = [r for r in rows if r.get('Website Issue Flag', '') == 'Yes']
    sales_issue = [r for r in rows if r['Sales Issue Flag'] == 'Yes']
    all_three = [r for r in rows if r['GRN Issue Flag'] == 'Yes' and r.get('Website Issue Flag', '') == 'Yes' and r['Sales Issue Flag'] == 'Yes']
    clean = [r for r in rows if r['GRN Issue Flag'] != 'Yes' and r.get('Website Issue Flag', '') != 'Yes' and r['Sales Issue Flag'] != 'Yes']

    summary_rows = [
        {'Metric': 'Total SKUs Analyzed', 'Value': len(rows)},
        {'Metric': '', 'Value': ''},
        {'Metric': 'Problem #1: GRN Invalid UOM/Price', 'Value': ''},
        {'Metric': '  SKUs with GRN Issue', 'Value': len(grn_issue)},
        {'Metric': '  Total GRN Outlier Rows', 'Value': sum(r['GRN Outlier Rows'] for r in grn_issue)},
        {'Metric': '  Total GRN Net Impact ($)', 'Value': round(sum(r['GRN Net Impact ($)'] for r in grn_issue), 2)},
        {'Metric': '    - Overpayment ($)', 'Value': round(sum(r['GRN Overpayment ($)'] for r in grn_issue), 2)},
        {'Metric': '    - Underpayment ($)', 'Value': round(sum(r['GRN Underpayment ($)'] for r in grn_issue), 2)},
        {'Metric': '', 'Value': ''},
        {'Metric': 'Problem #2: Website Price/UOM Incorrect', 'Value': ''},
        {'Metric': '  SKUs with Website Price Issue', 'Value': len(wp_issue)},
        {'Metric': '', 'Value': ''},
        {'Metric': 'Problem #3: Sales Wrong UOM/Price', 'Value': ''},
        {'Metric': '  SKUs with Sales Issue', 'Value': len(sales_issue)},
        {'Metric': '  Total Sales Outlier Transactions', 'Value': sum(r['Sales Outlier Transactions'] for r in sales_issue)},
        {'Metric': '  Total Units Sold Wrong', 'Value': sum(r['Units Sold Wrong'] for r in sales_issue)},
        {'Metric': '  Total Sales Revenue Loss ($)', 'Value': round(sum(r['Total Sales Loss ($)'] for r in sales_issue), 2)},
        {'Metric': '', 'Value': ''},
        {'Metric': 'Cross-Cutting', 'Value': ''},
        {'Metric': '  SKUs with All 3 Problems', 'Value': len(all_three)},
        {'Metric': '  SKUs with Zero Issues', 'Value': len(clean)},
        {'Metric': '', 'Value': ''},
        {'Metric': 'Detection Parameters', 'Value': ''},
        {'Metric': '  GRN Statistical Lower Multiplier', 'Value': config.get("outlier_detection", {}).get("statistical_lower_multiplier", 0.3)},
        {'Metric': '  GRN Statistical Upper Multiplier', 'Value': config.get("outlier_detection", {}).get("statistical_upper_multiplier", 3.0)},
        {'Metric': '  Website Price Lower Multiplier', 'Value': wp_lower_mult},
        {'Metric': '  Website Price Upper Multiplier', 'Value': wp_upper_mult},
        {'Metric': '  Sales Analysis Lower Multiplier', 'Value': config.get("sales_analysis", {}).get("normalized_price_lower_multiplier", 0.6)},
        {'Metric': '  Sales Analysis Upper Multiplier', 'Value': config.get("sales_analysis", {}).get("normalized_price_upper_multiplier", 3.0)},
    ]
    summary_df = pd.DataFrame(summary_rows)

    # Sanitize all string cells: remove control characters (except \t \n \r) that break openpyxl
    for col in report_df.select_dtypes(include='object').columns:
        report_df[col] = report_df[col].apply(
            lambda v: re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(v)) if pd.notna(v) else v
        )
    for col in summary_df.select_dtypes(include='object').columns:
        summary_df[col] = summary_df[col].apply(
            lambda v: re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(v)) if pd.notna(v) else v
        )

    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        report_df.to_excel(writer, index=False, sheet_name='SKU Quality Report')
        summary_df.to_excel(writer, index=False, sheet_name='Summary')

    return excel_buffer.getvalue(), len(rows)


@app.get("/export_sku_quality_report")
def export_sku_quality_report():
    """Return consolidated per-SKU quality report covering all 3 problem types."""
    try:
        excel_bytes, row_count = compute_sku_quality_report()
        if row_count == 0:
            empty_buffer = io.BytesIO()
            with pd.ExcelWriter(empty_buffer, engine='openpyxl') as writer:
                pd.DataFrame().to_excel(writer, index=False, sheet_name='SKU Quality Report')
                pd.DataFrame().to_excel(writer, index=False, sheet_name='Summary')
            excel_bytes = empty_buffer.getvalue()

        return Response(
            content=excel_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=sku_quality_report.xlsx"}
        )
    except Exception as e:
        return {"status": "error", "message": f"Failed to generate SKU quality report: {str(e)}"}


@app.get("/sales_analysis_summary")
def sales_analysis_summary():
    """Return JSON summary of sales data analysis."""
    csv_buf, csv_row_count = load_sales_csv_from_db()
    if csv_buf is None:
        return {
            "has_sales_data": False,
            "total_sales_rows": 0,
            "outlier_count": 0,
            "sku_count": 0,
            "total_units_lost": 0,
            "total_sales_loss": 0.0,
            "message": "No sales data uploaded yet."
        }
    
    # Recompute from cache or recompute
    excel_bytes, outlier_count = load_sales_outliers_cache()
    if excel_bytes is None or outlier_count == 0:
        if sku_profiles:
            try:
                csv_buf_for_recompute, _ = load_sales_csv_from_db()
                precompute_and_cache_sales_outliers(csv_buf_for_recompute)
                _, outlier_count = load_sales_outliers_cache()
            except Exception:
                pass
    
    # Get loss summary to compute totals
    summary_excel, summary_count = load_sales_loss_summary_cache()
    total_units = 0
    total_loss = 0.0
    loss_sku_count = 0
    
    if summary_count > 0 and summary_excel is not None:
        try:
            summary_df = pd.read_excel(io.BytesIO(summary_excel), sheet_name='Sales Loss Summary')
            if not summary_df.empty:
                total_units = int(summary_df['Total Units Sold at Wrong UOM/Price'].sum())
                total_loss = float(summary_df['Total Sales Loss'].sum())
                loss_sku_count = len(summary_df)
        except Exception:
            pass
    
    return {
        "has_sales_data": True,
        "total_sales_rows": csv_row_count,
        "outlier_count": outlier_count,
        "sku_count": loss_sku_count,
        "total_units_lost": total_units,
        "total_sales_loss": round(total_loss, 2),
        "uom_master_sku_count": len(uom_master_lookup),
        "website_price_sku_count": len(website_price_lookup),
        "grn_profile_sku_count": len(sku_profiles)
    }
