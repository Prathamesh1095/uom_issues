import pandas as pd
import numpy as np
import re
import math
import json
import io
import os
import asyncio
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

class PredictRequest(BaseModel):
    sku_code: str
    input_price: float

def build_sku_profiles_from_chunks(chunks):
    """
    Process CSV in streaming chunks and build SKU profiles incrementally.
    Never holds the full DataFrame in memory — only the profiles dict grows.
    """
    profiles = {}
    lower_mult = config.get("historical_data", {}).get("median_br_lower_multiplier", 0.2)
    upper_mult = config.get("historical_data", {}).get("median_br_upper_multiplier", 5.0)

    for chunk_idx, chunk in enumerate(chunks):
        # Rename columns
        rename_map = {"PO Purchase Rate": "Price", "invoice_date": "Date"}
        chunk.rename(columns=rename_map, inplace=True)
        
        # Ensure required columns exist
        if 'Price' not in chunk.columns or 'SKU Code' not in chunk.columns:
            continue
        
        chunk['Price'] = pd.to_numeric(chunk['Price'], errors='coerce')
        chunk.dropna(subset=['Price'], inplace=True)
        
        def get_implied_cf(text):
            match = re.search(r'of\s+(\d+)', str(text), re.IGNORECASE)
            return int(match.group(1)) if match else None
        
        if 'alternate_uom' in chunk.columns:
            chunk['Implied_CF'] = chunk['alternate_uom'].apply(get_implied_cf)
        else:
            chunk['Implied_CF'] = None
        
        if 'CF' not in chunk.columns:
            chunk['CF'] = 1.0
        
        chunk['Effective_CF'] = chunk['Implied_CF'].fillna(chunk['CF'])
        chunk['Row_Base_Rate'] = chunk['Price'] / chunk['Effective_CF']
        
        # Group by SKU within this chunk
        for sku, group in chunk.groupby('SKU Code'):
            if sku not in profiles:
                # Initialize with first occurrence
                profiles[sku] = {'all_rates': [], 'date_rate_map': [], 'valid_uoms': {}}
            
            median_br = group['Row_Base_Rate'].median()
            valid_mask = (group['Row_Base_Rate'] >= lower_mult * median_br) & (group['Row_Base_Rate'] <= upper_mult * median_br)
            clean_group = group[valid_mask]
            
            if clean_group.empty:
                continue
            
            # Collect valid rates for this SKU
            for _, row in clean_group.iterrows():
                rate = row['Row_Base_Rate']
                uom = row.get('alternate_uom', '')
                cf = int(row['Effective_CF'])
                date = row.get('Date', None)
                profiles[sku]['all_rates'].append(rate)
                profiles[sku]['date_rate_map'].append((date, rate, uom, cf))
    
    # Finalize profiles from accumulated data
    final_profiles = {}
    for sku, data in profiles.items():
        if not data['all_rates']:
            continue
        
        rates = np.array(data['all_rates'])
        median_br = float(np.median(rates))
        
        # Sort by date to find latest
        sorted_by_date = sorted(data['date_rate_map'], key=lambda x: str(x[0]) if x[0] else '')
        latest_br = sorted_by_date[-1][1] if sorted_by_date else median_br
        
        # Build valid_uoms
        uom_cf_pairs = set()
        for _, rate, uom, cf in sorted_by_date:
            if uom:
                uom_cf_pairs.add((uom, cf))
        
        valid_uoms = {pair[0]: pair[1] for pair in uom_cf_pairs}
        final_profiles[sku] = {'latest_br': latest_br, 'valid_uoms': valid_uoms}
    
    return final_profiles


@app.on_event("startup")
def startup_event():
    global sku_profiles, global_df
    data_file_path = config.get("data_file_path", "../GRN Data Final last 1 year UOM Adjusted.csv")
    file_path = os.path.join(os.path.dirname(__file__), data_file_path)
    
    # Also try .xlsx as fallback
    if not os.path.exists(file_path):
        xlsx_path = file_path.replace('.csv', '.xlsx')
        if os.path.exists(xlsx_path):
            # Convert .xlsx to CSV first, then stream
            print(f"Converting XLSX to CSV for streaming: {xlsx_path}")
            df_temp = pd.read_excel(xlsx_path)
            csv_path = file_path.replace('.csv', '_converted.csv')
            df_temp.to_csv(csv_path, index=False)
            file_path = csv_path
    
    if os.path.exists(file_path):
        # Read first chunk to get column names and store a sample for template
        first_chunk = pd.read_csv(file_path, nrows=5)
        global_df = first_chunk.copy()
        
        # Stream the full file in chunks
        chunks = pd.read_csv(file_path, chunksize=50000, low_memory=False)
        sku_profiles = build_sku_profiles_from_chunks(chunks)
        print(f"Loaded profiles for {len(sku_profiles)} SKUs (streaming mode).")
    else:
        print(f"Data file not found at {file_path}. Please place it in the configured path.")


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


async def process_upload_async(task_id: str, file_path: str):
    """Process a large upload file in the background."""
    try:
        chunks = pd.read_csv(file_path, chunksize=50000, low_memory=False)
        profiles = build_sku_profiles_from_chunks(chunks)
        
        # Update global state
        global sku_profiles, global_df
        sku_profiles = profiles
        
        # Read first chunk as sample for global_df
        sample_df = pd.read_csv(file_path, nrows=5)
        global_df = sample_df.copy()
        
        # Clean up temp file
        try:
            os.remove(file_path)
        except:
            pass
        
        upload_tasks[task_id] = {
            "status": "success",
            "message": f"Successfully loaded {len(sku_profiles)} SKUs from uploaded file."
        }
    except Exception as e:
        upload_tasks[task_id] = {
            "status": "error",
            "message": f"Failed to process file: {str(e)}"
        }


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
        
        # For files <= 100K rows, process inline
        if row_count <= 100000:
            csv_buffer = io.BytesIO(contents)
            chunks = pd.read_csv(csv_buffer, chunksize=50000, low_memory=False)
            sku_profiles = build_sku_profiles_from_chunks(chunks)
            
            # Load sample for global_df from original bytes (buffer may be consumed)
            global_df = pd.read_csv(io.BytesIO(contents), nrows=5)
            
            return {
                "status": "success",
                "message": f"Successfully loaded {len(sku_profiles)} SKUs from {row_count:,} rows."
            }
        else:
            # For large files (> 100K rows), use async processing
            import uuid
            import tempfile
            
            task_id = str(uuid.uuid4())
            
            # Save to temp file
            temp_dir = tempfile.gettempdir()
            temp_path = os.path.join(temp_dir, f"grn_upload_{task_id}.csv")
            with open(temp_path, 'wb') as f:
                f.write(contents)
            
            # Initialize task status
            upload_tasks[task_id] = {"status": "processing", "message": f"Processing {row_count:,} rows in the background..."}
            
            # Launch background processing
            asyncio.create_task(process_upload_async(task_id, temp_path))
            
            return {
                "status": "accepted",
                "task_id": task_id,
                "message": f"File with {row_count:,} rows is being processed in the background."
            }
            
    except Exception as e:
        return {"status": "error", "message": f"Failed to process file: {str(e)}"}


@app.get("/upload_status/{task_id}")
def upload_status(task_id: str):
    """Poll this endpoint to check the status of an async upload."""
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