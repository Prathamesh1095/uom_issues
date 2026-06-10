# 🐛 UOM Issues - Bug Bash Report
**Date:** 2026-06-10  
**Tested URL:** https://uom-issues.vercel.app/ (Frontend) + https://uom-issues.onrender.com/ (Backend)  
**Tester:** Automated (Browser MCP + Backend API)

---

## 🔴 CRITICAL BUGS

### BUG #1: Export Progress State Mismatch — GRN Buttons Permanently Disabled

**Severity:** Critical  
**Type:** Backend Logic Error  
**Affected Area:** `/export_progress` endpoint + Frontend button states

**Description:**  
The `/export_progress` endpoint returns `grn_outliers: -1` and `grn_template_available: false` even when the startup logs confirm "Outliers cache loaded (602 rows)" and data is fully loaded. This causes the **GRN Template** and **GRN Outliers** buttons to remain disabled on the frontend, making them unusable despite data being ready.

**Evidence:**
```
GET /startup_logs  →  "Outliers cache loaded (602 rows)", data_loaded=true
GET /export_progress →  grn_outliers: -1, grn_template_available: false
GET /               →  data_loaded=true, sku_count=15853
```

**Root Cause:**  
In `load_startup_data_async()` (backend/main.py lines 1011-1131), the function declares these globals:
```python
global sku_profiles, global_df, startup_complete, startup_logs
global startup_processed_rows, startup_total_rows, startup_data_loaded
```

But it does NOT declare `grn_outliers_progress`, `grn_template_available`, `sales_outliers_progress`, or `sales_loss_progress` as global. When the function assigns:
- `grn_outliers_progress = 100` (line 1051)
- `grn_template_available = True` (line 1033)

These create **local variables** instead of modifying the module-level globals. The `/export_progress` endpoint reads module-level globals which remain at their initial values (`-1` and `false`).

**Impact:**  
- GRN Template download button is disabled
- GRN Outliers export button is disabled  
- Users cannot export any GRN-related reports
- The entire GRN reporting workflow is broken after server restart

**Fix:** Add missing global declarations to `load_startup_data_async()`:
```python
global grn_outliers_progress, grn_template_available
global sales_outliers_progress, sales_loss_progress
```

---

### BUG #2: `file_type` Parameter Broken in Multipart Form Upload

**Severity:** Critical  
**Type:** Backend API Error  
**Affected Area:** `/upload_data` endpoint (Sales data upload via frontend UI)

**Description:**  
The `file_type` parameter is not correctly parsed when sent as multipart form data (which is how the frontend sends it via `formData.append('file_type', fileType)`). It always defaults to `"grn"`, meaning **Sales data uploaded through the frontend UI is always processed as GRN data**, corrupting the SKU profiles.

**Evidence:**
```
# Sending file_type=sales via multipart form → always returns grn
POST /upload_data
  form-data: file=sample_sales.csv, file_type=sales
  Response: {"file_type":"grn", "message":"GRN file with 5 rows..."}

# Sending file_type=sales via query param → works correctly
POST /upload_data?file_type=sales
  form-data: file=sample_sales.csv
  Response: {"file_type":"sales", "message":"Sales file with 5 rows..."}
```

**Root Cause:**  
The endpoint signature is:
```python
@app.post("/upload_data")
async def upload_data(file: UploadFile = File(...), file_type: str = "grn"):
```

FastAPI requires `Form(...)` annotation for non-file form fields in multipart requests. Without it, the `file_type` form field is not extracted from the multipart body and always falls back to the default `"grn"`.

**Fix:** Add `Form(...)` annotation:
```python
from fastapi import Form
async def upload_data(file: UploadFile = File(...), file_type: str = Form("grn")):
```

**Impact:**  
- Sales CSV uploads through the frontend are processed as GRN data
- This overwrites existing GRN SKU profiles with sales data format
- Complete data loss of SKU profiles
- Sales outlier detection never works via the UI

---

## 🟠 HIGH SEVERITY BUGS

### BUG #3: SKU Profiles Completely Overwritten on Upload (No Merge, No Confirmation)

**Severity:** High  
**Type:** Data Loss / UX Issue  
**Affected Area:** GRN Upload flow

**Description:**  
Every time a user uploads a new GRN CSV, ALL existing SKU profiles are completely replaced — both in memory and in the database. There is:
- No merge with existing data
- No confirmation dialog ("This will replace all existing data")
- No undo capability
- No backup of previous profiles

**Evidence:**
```
Upload GRN: "Successfully loaded 3 SKUs from 8 rows."
  → sku_profiles dict now contains only 3 SKUs (was 15,853)
  → Previous 15,853 profiles permanently lost
```

**Impact:**  
- Accidental uploads can destroy the entire dataset
- Users cannot incrementally build profiles
- Production data can be lost with a single wrong upload

---

### BUG #4: Sales Export Buttons Enabled Despite No Sales Data

**Severity:** High  
**Type:** Frontend UX Issue  
**Affected Area:** Header buttons (Sales Outliers, Loss Summary)

**Description:**  
The **Sales Outliers** and **Loss Summary** buttons are enabled and clickable even when no sales data has been uploaded. Clicking them triggers a JavaScript `alert()` with an error message, which is poor UX.

**Evidence:**
```json
GET /export_sales_outliers → {"status":"error","message":"No sales data available."}
GET /export_sales_loss_summary → {"status":"error","message":"No sales data available."}
```

**Fix:** These buttons should check `sales_outliers` and `sales_loss` progress values from `export_progress` endpoint and show disabled state with appropriate tooltip when -1.

---

## 🟡 MEDIUM SEVERITY BUGS

### BUG #5: Frontend `file_type` State Does Not Sync With Backend After Upload

**Severity:** Medium  
**Type:** State Management Issue  
**Affected Area:** Upload section UI

**Description:**  
The frontend `fileType` state stays as "grn" or "sales" based on the toggle button, but since BUG #2 causes the backend to always process as GRN, the template download switches between GRN/Sales templates while the actual upload always processes as GRN — creating confusion.

---

### BUG #6: Predict Endpoint Does Not Handle Edge Cases

**Severity:** Medium  
**Type:** API Robustness  
**Affected Area:** `/predict_uom` endpoint

**Description:**
- Empty SKU code returns generic error message instead of "SKU code is required"
- Negative price values are accepted without validation
- Very large numbers (>10^15) could cause float precision issues

**Evidence:**
```
POST /predict_uom {"sku_code":"","input_price":0}
→ {"status":"error","message":"Manual Review Required: SKU not found or insufficient historical data."}

POST /predict_uom {"sku_code":"TEST","input_price":-100}
→ {"status":"error","message":"Manual Review Required: SKU not found or insufficient historical data."}
```

---

### BUG #7: No Validation on CSV Content Before Processing

**Severity:** Medium  
**Type:** Data Validation  
**Affected Area:** `/upload_data` endpoint

**Description:**  
The backend accepts any CSV file and attempts to process it. If the CSV doesn't have the expected columns (e.g., `SKU Code`, `Price`), it silently fails with "Skipped chunk: missing 'Price' or 'SKU Code' columns" but still overwrites existing profiles with empty data.

**Evidence:**
```
Upload sales CSV as GRN → "Successfully loaded 0 SKUs from 5 rows"
  → "Saving SKU profiles to database..." (saves empty profiles!)
  → Previous 15,853 profiles destroyed
```

---

## 🔵 LOW SEVERITY BUGS / UI ISSUES

### BUG #8: Page Title Shows Generic "frontend" Instead of App Name

**Severity:** Low  
**Type:** UX  
**Description:** The browser tab title shows "frontend" (from Vite config) instead of "Smart GRN Entry" or similar.

### BUG #9: CSS File Contains Unused Styles

**Severity:** Low  
**Type:** Code Quality  
**Description:** `App.css` contains styles (`.counter`, `.hero`, `#center`, etc.) that are from the Vite boilerplate and not used anywhere in the actual app. Only `index.css` with Tailwind import is needed.

### BUG #10: Upload Progress Polling Uses `setTimeout` Instead of `clearTimeout`

**Severity:** Low  
**Type:** Resource Leak  
**Description:** In `App.jsx`, the `pollUploadStatus` callback uses `setTimeout` without properly cleaning up on component unmount. The `pollTimeout` ref is used but cleanup could be more robust.

### BUG #11: No Loading Indicator for Export Buttons

**Severity:** Low  
**Type:** UX  
**Description:** When `isCheckingExports` is true (initial state -2), buttons show disabled with no explanation. A tooltip or brief loading state would improve UX.

### BUG #12: `@app.on_event("startup")` is Deprecated

**Severity:** Low  
**Type:** Deprecation  
**Description:** FastAPI recommends using `lifespan` context manager instead of `@app.on_event("startup")` and `@app.on_event("shutdown")`.

---

## 🧪 TEST SUMMARY

| Test Case | Status | Notes |
|-----------|--------|-------|
| Backend health check | ✅ PASS | Returns correct status |
| Startup logs | ✅ PASS | Data loaded from DB cache |
| Export progress | ❌ FAIL | Returns stale defaults (BUG #1) |
| GRN template download | ⚠️ PARTIAL | Works via API but button disabled on UI |
| GRN outliers export | ⚠️ PARTIAL | Works via API but button disabled on UI |
| SKU prediction (valid SKU) | ✅ PASS | Returns correct UOM and CF |
| SKU prediction (invalid SKU) | ✅ PASS | Returns "Manual Review Required" |
| GRN file upload (form data) | ❌ FAIL | file_type not parsed (BUG #2) |
| Sales file upload (query param) | ✅ PASS | Workaround works |
| Sales file upload (form data) | ❌ FAIL | Defaults to GRN type (BUG #2) |
| Export sales outliers (no data) | ✅ PASS | Returns proper error message |
| Export sales loss (no data) | ✅ PASS | Returns proper error message |
| Sales template download | ✅ PASS | Returns correct CSV |
| Upload status polling | ✅ PASS | Returns progress correctly |
| Frontend renders correctly | ⚠️ PARTIAL | Buttons incorrectly disabled |
| CORS handling | ✅ PASS | Frontend on Vercel can reach backend on Render |

---

## 📋 RECOMMENDED FIX PRIORITY

1. **BUG #1** (Critical) — Add missing `global` declarations in `load_startup_data_async()`
2. **BUG #2** (Critical) — Add `Form(...)` annotation to `file_type` parameter
3. **BUG #3** (High) — Add confirmation dialog and backup before overwriting profiles
4. **BUG #4** (High) — Disable Sales export buttons when no sales data exists
5. **BUG #7** (Medium) — Validate CSV structure before processing/overwriting
6. **BUG #6** (Medium) — Add input validation to predict endpoint
7. **BUG #5** (Medium) — Fix frontend state sync
8. **BUG #8-12** (Low) — Cleanup and polish