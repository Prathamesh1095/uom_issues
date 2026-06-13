import pandas as pd
import time
import json
import os

config = {}

def extract_uom_name(uom_str: str) -> str:
    filler = {"of", "and", "per", "the", "a", "an"}
    for word in str(uom_str).split():
        clean = word.strip()
        if clean.isdigit() or clean.lower() in filler:
            continue
        if clean.isalpha():
            return clean
    return uom_str

def build_sku_profiles_opt(grn_df, uom_master_lookup=None):
    if uom_master_lookup is None:
        uom_master_lookup = {}

    # Use UOM Master CF when available; fall back to CSV CF column
    def lookup_master_cf(row):
        sku = str(row.get('SKU Code', '')).strip()
        uom_raw = str(row.get('alternate_uom', '')) if pd.notna(row.get('alternate_uom')) else ''
        if sku and uom_raw:
            uom_name = extract_uom_name(uom_raw)
            cf = uom_master_lookup.get(sku, {}).get(uom_name)
            if cf is not None:
                return cf
        return row.get('CF', 1.0)

    if 'CF' not in grn_df.columns:
        grn_df['CF'] = 1.0

    grn_df['Effective_CF'] = grn_df.apply(lookup_master_cf, axis=1).astype(float)
    grn_df['Row_Base_Rate'] = grn_df['Price'] / grn_df['Effective_CF']

    lower_mult = config.get("grn_profiling", {}).get("base_rate_lower_multiplier", 0.3)
    upper_mult = config.get("grn_profiling", {}).get("base_rate_upper_multiplier", 3.0)

    medians = grn_df.groupby('SKU Code')['Row_Base_Rate'].transform('median')

    valid_mask = (grn_df['Row_Base_Rate'] >= lower_mult * medians) & (grn_df['Row_Base_Rate'] <= upper_mult * medians)
    clean_df = grn_df[valid_mask].copy()

    if clean_df.empty:
        return {}

    if 'Date' in clean_df.columns:
        clean_df = clean_df.sort_values(['SKU Code', 'Date'])

    latest_br_df = clean_df.groupby('SKU Code').tail(1).set_index('SKU Code')['Row_Base_Rate']

    mode_df = clean_df.groupby(['SKU Code', 'alternate_uom', 'Effective_CF']).size().reset_index(name='count')
    mode_df = mode_df.sort_values('count', ascending=False).drop_duplicates(subset=['SKU Code', 'alternate_uom'])

    valid_uoms_df = mode_df.set_index(['SKU Code', 'alternate_uom'])['Effective_CF']

    profiles = {}
    for sku, group in valid_uoms_df.groupby('SKU Code'):
        latest_br = latest_br_df.get(sku)
        if pd.isna(latest_br):
            continue
        uoms = group.loc[sku].to_dict()
        profiles[sku] = {'latest_br': latest_br, 'valid_uoms': {k: int(v) for k, v in uoms.items() if pd.notna(v)}}

    return profiles

if __name__ == '__main__':
    start = time.time()
    df = pd.read_excel('../GRN Data Final last 1 year UOM Adjusted.xlsx')
    print('Read:', time.time() - start)
    df.rename(columns={'PO Purchase Rate': 'Price', 'invoice_date': 'Date'}, inplace=True)
    start2 = time.time()
    res = build_sku_profiles_opt(df)
    print('Build:', time.time() - start2)
    print('Profiles:', len(res))
