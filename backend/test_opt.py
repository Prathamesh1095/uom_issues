import pandas as pd
import time
import json
import os

config = {}

def build_sku_profiles_opt(grn_df):
    implied_cf = grn_df['alternate_uom'].astype(str).str.extract(r'(?i)of\s+(\d+)', expand=False).astype(float)
    grn_df['Effective_CF'] = implied_cf.fillna(grn_df['CF'])
    grn_df['Row_Base_Rate'] = grn_df['Price'] / grn_df['Effective_CF']
    
    lower_mult = config.get("historical_data", {}).get("median_br_lower_multiplier", 0.2)
    upper_mult = config.get("historical_data", {}).get("median_br_upper_multiplier", 5.0)

    medians = grn_df.groupby('SKU Code')['Row_Base_Rate'].transform('median')
    
    valid_mask = (grn_df['Row_Base_Rate'] >= lower_mult * medians) & (grn_df['Row_Base_Rate'] <= upper_mult * medians)
    clean_df = grn_df[valid_mask].copy()
    
    if clean_df.empty:
        return {}
        
    if 'Date' in clean_df.columns:
        clean_df = clean_df.sort_values(['SKU Code', 'Date'])
    
    latest_br_df = clean_df.groupby('SKU Code').tail(1).set_index('SKU Code')['Row_Base_Rate']
    
    # Calculate mode by taking the most frequent value using value_counts
    # this is much faster than .apply(lambda x: x.mode())
    mode_df = clean_df.groupby(['SKU Code', 'alternate_uom', 'Effective_CF']).size().reset_index(name='count')
    # Sort by count desc, then drop duplicates per SKU and alternate_uom to keep the mode
    mode_df = mode_df.sort_values('count', ascending=False).drop_duplicates(subset=['SKU Code', 'alternate_uom'])
    
    valid_uoms_df = mode_df.set_index(['SKU Code', 'alternate_uom'])['Effective_CF']
    
    profiles = {}
    
    # Iterate over unique SKUs
    for sku, group in valid_uoms_df.groupby('SKU Code'):
        latest_br = latest_br_df.get(sku)
        if pd.isna(latest_br):
            continue
            
        uoms = group.loc[sku].to_dict()
        profiles[sku] = {'latest_br': latest_br, 'valid_uoms': {k: int(v) for k,v in uoms.items() if pd.notna(v)}}
        
    return profiles

if __name__ == '__main__':
    start = time.time()
    df = pd.read_excel('../GRN Data Final last 1 year UOM Adjusted.xlsx')
    print('Read:', time.time()-start)
    df.rename(columns={'PO Purchase Rate': 'Price', 'invoice_date': 'Date'}, inplace=True)
    start2 = time.time()
    res = build_sku_profiles_opt(df)
    print('Build:', time.time()-start2)
