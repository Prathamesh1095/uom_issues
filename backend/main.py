import pandas as pd
import numpy as np
import re
import math
import json
import io
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import os

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

class PredictRequest(BaseModel):
    sku_code: str
    input_price: float

def build_sku_profiles(grn_df):
    def get_implied_cf(text):
        match = re.search(r'of\s+(\d+)', str(text), re.IGNORECASE)
        return int(match.group(1)) if match else None
        
    grn_df['Implied_CF'] = grn_df['alternate_uom'].apply(get_implied_cf)
    grn_df['Effective_CF'] = grn_df['Implied_CF'].fillna(grn_df['CF'])
    
    grn_df['Row_Base_Rate'] = grn_df['Price'] / grn_df['Effective_CF']
    
    profiles = {}
    lower_mult = config.get("historical_data", {}).get("median_br_lower_multiplier", 0.2)
    upper_mult = config.get("historical_data", {}).get("median_br_upper_multiplier", 5.0)

    for sku, group in grn_df.groupby('SKU Code'):
        median_br = group['Row_Base_Rate'].median()
        
        valid_mask = (group['Row_Base_Rate'] >= lower_mult * median_br) & (group['Row_Base_Rate'] <= upper_mult * median_br)
        if 'Date' in group.columns:
            clean_group = group[valid_mask].sort_values('Date')
        else:
            clean_group = group[valid_mask]
        
        if clean_group.empty:
            continue
            
        latest_br = clean_group.iloc[-1]['Row_Base_Rate']
        
        valid_uoms = {}
        for uom, sub_group in clean_group.groupby('alternate_uom'):
            valid_uoms[uom] = int(sub_group['Effective_CF'].mode()[0])
            
        profiles[sku] = {'latest_br': latest_br, 'valid_uoms': valid_uoms}
        
    return profiles

@app.on_event("startup")
def startup_event():
    global sku_profiles, global_df
    data_file_path = config.get("data_file_path", "../GRN Data Final last 1 year UOM Adjusted.xlsx")
    file_path = os.path.join(os.path.dirname(__file__), data_file_path)
    if os.path.exists(file_path):
        df = pd.read_excel(file_path)
        df.rename(columns={"PO Purchase Rate": "Price", "invoice_date": "Date"}, inplace=True)
        global_df = df.copy()
        sku_profiles = build_sku_profiles(df)
        print(f"Loaded profiles for {len(sku_profiles)} SKUs.")
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

    # Drop intermediate processing columns if you want a cleaner export
    cols_to_drop = ['Implied_CF', 'Effective_CF', 'Row_Base_Rate']
    outliers_df = outliers_df.drop(columns=[c for c in cols_to_drop if c in outliers_df.columns], errors='ignore')

    csv_buffer = io.StringIO()
    outliers_df.to_csv(csv_buffer, index=False)
    
    return Response(
        content=csv_buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=outliers_report.csv"}
    )

@app.post("/upload_data")
async def upload_data(file: UploadFile = File(...)):
    global global_df, sku_profiles
    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        df.rename(columns={"PO Purchase Rate": "Price", "invoice_date": "Date"}, inplace=True)
        global_df = df.copy()
        sku_profiles = build_sku_profiles(df)
        return {"status": "success", "message": f"Successfully loaded {len(sku_profiles)} SKUs from uploaded file."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to process file: {str(e)}"}

@app.get("/download_template")
def download_template():
    if global_df is None:
        return {"status": "error", "message": "Data not loaded yet."}
    
    # Create an empty dataframe with the same columns
    template_df = pd.DataFrame(columns=global_df.columns)
    
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        template_df.to_excel(writer, index=False)
        
    excel_buffer.seek(0)
    return Response(
        content=excel_buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=grn_template.xlsx"}
    )
