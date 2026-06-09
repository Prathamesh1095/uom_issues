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
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

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

sku_profiles = {}
global_df = None

# In-memory task status store for async uploads
upload_tasks = {}

# Startup logs — accumulated during boot, exposed via API
startup_logs = []
startup_complete = False
startup_total_rows = 0
startup_processed_rows = 0


class PredictRequest(BaseModel):
    sku_code: str
    input_price: float


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
    lower_mult = config.get("historical_data", {}).get("median_br_lower_multiplier", 0.2)
    upper_mult = config.get("historical_data", {}).get("median_br_upper_multiplier", 5.0)
    total_rows_processed = 0

    # Accumulators across chunks
    all_rates = {}          # sku -> list of valid rates
    all_dates = {}          # sku -> dict of {date_str: (rate, uom, cf)}
    all_uoms = {}           # sku -> dict of {(uom, cf): count}
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

        # Vectorized Implied_CF extraction (str.extract is C-level)
        if 'alternate_uom' in chunk.columns:
            chunk['Implied_CF'] = chunk['alternate_uom'].str.extract(
                r'of\s+(\d+)', flags=re.IGNORECASE, expand=False
            ).astype(float)
        else:
            chunk['Implied_CF'] = None

        if 'CF' not in chunk.columns:
            chunk['CF'] = 1.0

        chunk['Effective_CF'] = chunk['Implied_CF'].fillna(chunk['CF'])
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
                        all_dates[sku][str_date] = (rate_val, str(uom_val) if pd.notna(uom_val) else '', int(cf_val) if pd.notna(cf_val) else 1)

            # Track valid UOMs with their CF counts
            if 'alternate_uom' in valid_chunk.columns:
                if sku not in all_uoms:
                    all_uoms[sku] = {}
                for _, row in group.iterrows():
                    uom_val = row.get('alternate_uom', '')
                    cf_val = row.get('Effective_CF', 1)
                    if pd.notna(uom_val) and str(uom_val).strip():
                        key = (str(uom_val).strip(), int(cf_val) if pd.notna(cf_val) else 1)
                        all_uoms[sku][key] = all_uoms[sku].get(key, 0) + 1

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
            rate_val, uom_val, cf_val = all_dates[sku][latest_date]
            latest_br = rate_val

        # Build valid_uoms dict with most frequent CF per UOM
        valid_uoms = {}
        if sku in all_uoms:
            # Sort by count desc per UOM, keep highest CF for each UOM
            uom_groups = {}
            for (uom, cf), count in all_uoms[sku].items():
                if uom not in uom_groups or count > uom_groups[uom][1]:
                    uom_groups[uom] = (cf, count)
            valid_uoms = {uom: cf for uom, (cf, _) in uom_groups.items()}

        final_profiles[sku] = {
            'latest_br': latest_br,
            'valid_uoms': valid_uoms
        }
        skus_finalized += 1

    if log_callback:
        log_callback(
            f"Finalized profiles for {skus_finalized} SKUs from {total_rows_processed:,} total rows",
            total_rows_processed,
            known_total_rows
        )

    return final_profiles


# --- Background startup data loading ---
async def load_startup_data_async():
    """Load data in background so server starts immediately."""
    global sku_profiles, global_df, startup_complete, startup_logs
    global startup_processed_rows, startup_total_rows

    add_startup_log("Starting background data loading...")

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

        # Streaming processing with logs
        add_startup_log("Starting streaming chunk processing...")

        def startup_log_callback(msg, processed, total):
            add_startup_log(msg, processed, total)

        # Run CPU-bound processing in thread pool to not block event loop
        chunks = pd.read_csv(file_path, chunksize=50000, low_memory=False)

        def process():
            return build_sku_profiles_from_chunks(
                chunks,
                log_callback=startup_log_callback,
                known_total_rows=total_data_rows
            )

        sku_profiles = await asyncio.to_thread(process)

        add_startup_log(f"✓ Startup complete. Loaded profiles for {len(sku_profiles)} SKUs.")
    else:
        add_startup_log(f"⚠ Data file not found at {file_path}. Place it in the configured path.")

    startup_complete = True
    add_startup_log("Server is ready.")


@app.on_event("startup")
def startup_event():
    global startup_logs, startup_complete, startup_processed_rows, startup_total_rows

    startup_logs = []
    startup_complete = False
    startup_processed_rows = 0
    startup_total_rows = 0

    add_startup_log("Starting server...")

    # Launch background loading — server starts immediately
    asyncio.create_task(load_startup_data_async())


@app.get("/startup_logs")
def get_startup_logs():
    """Return startup logs + status. Frontend polls this on mount."""
    return {
        "complete": startup_complete,
        "logs": startup_logs,
        "total_rows": startup_total_rows,
        "processed_rows": startup_processed_rows
    }


@app.post("/predict_uom")
def predict_uom(req: PredictRequest):
    sku_profile = sku_profiles.get(req.sku_code)
    if not sku_profile:
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
    
    return {"status": "success", "uom": best['uom'], "cf": best['cf']}


@app.get("/export_outliers")
def export_outliers():
    if global_df is None:
        return {"status": "error", "message": "Data not loaded yet."}
        
    grn_df = global_df.copy()
    
    def get_implied_cf(text):
        match = re.search(r'of\s+(\d+)', str(text), re.IGNORECASE)
        return int(match.group(1)) if match else None
        
    grn_df['Implied_CF'] = grn_df['alternate_uom'].apply(get_implied_cf)
    grn_df['Effective_CF'] = grn_df['Implied_CF'].fillna(grn_df['CF'])
    grn_df['Row_Base_Rate'] = grn_df['Price'] / grn_df['Effective_CF']
    
    lower_mult = config.get("historical_data", {}).get("median_br_lower_multiplier", 0.2)
    upper_mult = config.get("historical_data", {}).get("median_br_upper_multiplier", 5.0)

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
            unique_combos.add(f"{u} | {c} | {p}")
        valid_combos_str = ", ".join(sorted(list(unique_combos)))
        
        outlier_group = group[~valid_mask]
        
        for _, row in outlier_group.iterrows():
            rate = row['Row_Base_Rate']
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
            row_dict['Outlier_Reason'] = " | ".join(reason)
            row_dict['Valid_Occurrences'] = valid_count
            row_dict['Invalid_Occurrences'] = invalid_count
            row_dict['Valid_UOM_CF_Price_Combinations'] = valid_combos_str
            outliers_list.append(row_dict)
            
    outliers_df = pd.DataFrame(outliers_list)
    
    if not outliers_df.empty:
        if 'Date' in outliers_df.columns:
            outliers_df['Date'] = pd.to_datetime(outliers_df['Date'], errors='coerce')
            outliers_df = outliers_df.sort_values(by=['SKU Code', 'Date'])
        elif 'SKU Code' in outliers_df.columns:
            outliers_df = outliers_df.sort_values(by=['SKU Code'])

    cols_to_drop = ['Implied_CF', 'Effective_CF', 'Row_Base_Rate']
    outliers_df = outliers_df.drop(columns=[c for c in cols_to_drop if c in outliers_df.columns], errors='ignore')

    csv_buffer = io.StringIO()
    outliers_df.to_csv(csv_buffer, index=False)
    
    return Response(
        content=csv_buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=outliers_report.csv"}
    )


async def process_upload_async(task_id: str, file_path: str, total_rows: int):
    """Process a large upload file in the background with progress tracking.
    Uses asyncio.to_thread to avoid blocking the event loop.
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
        
        def process_in_thread():
            chunks = pd.read_csv(file_path, chunksize=50000, low_memory=False)
            return build_sku_profiles_from_chunks(chunks, log_callback=upload_log_callback, known_total_rows=total_rows)
        
        upload_log_callback("Reading and processing CSV chunks...", 0, total_rows)
        
        # Run CPU-bound work in thread pool so event loop stays free for polling
        profiles = await asyncio.to_thread(process_in_thread)
        
        # Update global state
        global sku_profiles, global_df
        upload_log_callback(f"Updating system with {len(profiles)} SKU profiles...", total_rows, total_rows)
        sku_profiles = profiles
        
        # Read first chunk as sample for global_df
        sample_df = pd.read_csv(file_path, nrows=5)
        global_df = sample_df.copy()
        
        # Clean up temp file
        try:
            os.remove(file_path)
        except:
            pass
        
        upload_tasks[task_id].update({
            "status": "success",
            "percentage": 100,
            "processed_rows": total_rows,
            "message": f"Successfully loaded {len(sku_profiles):,} SKUs from {total_rows:,} rows.",
            "logs": upload_tasks[task_id]["logs"] + [f"✓ Complete! {len(sku_profiles):,} SKUs loaded."]
        })
    except Exception as e:
        upload_tasks[task_id].update({
            "status": "error",
            "percentage": 0,
            "message": f"Failed to process file: {str(e)}",
            "logs": upload_tasks[task_id].get("logs", []) + [f"✗ Error: {str(e)}"]
        })


@app.post("/upload_data")
async def upload_data(file: UploadFile = File(...)):
    global sku_profiles, global_df
    
    # Validate file extension
    if not file.filename.endswith('.csv'):
        return {"status": "error", "message": "Only .csv files are accepted. Please upload a CSV file."}
    
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
        temp_path = os.path.join(temp_dir, f"grn_upload_{task_id}.csv")
        with open(temp_path, 'wb') as f:
            f.write(contents)
        
        # Initialize task status with progress tracking
        upload_tasks[task_id] = {
            "status": "processing",
            "percentage": 0,
            "processed_rows": 0,
            "total_rows": row_count,
            "message": f"Queued {row_count:,} rows for processing...",
            "logs": [f"File received: {file.filename} ({row_count:,} rows)", "Queued for background processing..."]
        }
        
        # Launch background processing (always async for consistency)
        asyncio.create_task(process_upload_async(task_id, temp_path, row_count))
        
        return {
            "status": "accepted",
            "task_id": task_id,
            "message": f"File with {row_count:,} rows is being processed in the background."
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