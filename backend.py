import pandas as pd
import numpy as np
import re
import math

def build_sku_profiles(grn_df):
    # 1. Parse hidden CF from text (fixes cases where CF is left as 1 for 'Pack of 100')
    def get_implied_cf(text):
        match = re.search(r'of\s+(\d+)', str(text), re.IGNORECASE)
        return int(match.group(1)) if match else None
        
    grn_df['Implied_CF'] = grn_df['alternate_uom'].apply(get_implied_cf)
    grn_df['Effective_CF'] = grn_df['Implied_CF'].fillna(grn_df['CF'])
    
    # 2. Calculate row-level Base Rate
    grn_df['Row_Base_Rate'] = grn_df['Price'] / grn_df['Effective_CF']
    
    profiles = {}
    for sku, group in grn_df.groupby('SKU Code'):
        median_br = group['Row_Base_Rate'].median()
        
        # 3. Outlier Filter (Mathematically drops the errors you mentioned)
        valid_mask = (group['Row_Base_Rate'] >= 0.2 * median_br) & (group['Row_Base_Rate'] <= 5.0 * median_br)
        clean_group = group[valid_mask].sort_values('Date')
        
        if clean_group.empty:
            continue
            
        # 4. Extract Valid UOMs and Latest Base Rate (Accounts for Price Increases)
        latest_br = clean_group.iloc[-1]['Row_Base_Rate']
        
        valid_uoms = {}
        for uom, sub_group in clean_group.groupby('alternate_uom'):
            valid_uoms[uom] = int(sub_group['Effective_CF'].mode()[0])
            
        profiles[sku] = {'latest_br': latest_br, 'valid_uoms': valid_uoms}
        
    return profiles

def predict_uom_cf(input_price, sku_profile):
    if not sku_profile: return "Manual Review"
    
    latest_br = sku_profile['latest_br']
    candidates = []
    
    # 5. User Input matching within +/- 80% Margin rule
    for uom, cf in sku_profile['valid_uoms'].items():
        expected_price = latest_br * cf
        ratio = input_price / expected_price if expected_price > 0 else 0
        
        # Margin bounds check (Ratio between 0.2 and 1.8)
        if 0.2 <= ratio <= 1.8:
            score = abs(math.log(ratio)) # Finds closest match to expected price
            candidates.append({'uom': uom, 'cf': cf, 'score': score})
            
    if not candidates:
        return "Manual Review" # Fails completely
        
    candidates.sort(key=lambda x: x['score'])
    return candidates[0] # Returns the winning {uom, cf, score}