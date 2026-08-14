import streamlit as st
import pandas as pd
import json
import re
from io import BytesIO

st.set_page_config(page_title="GST & Sales Data Ingestion Engine", layout="wide")

st.title("⚡ Sales Data Ingestion & Normalization Engine")
st.write("Upload your raw sales files to extract clean, standardized data for Tally and GST Portal processing.")

# Standard column schema to prevent KeyError when files are empty
STANDARD_COLUMNS = [
    "invoice_no", "invoice_date", "party_name", "gstin", "is_b2c",
    "item_code", "item_name", "hsn_code", "quantity", "rate",
    "taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "gst_amount"
]

# --- SANITIZATION & HELPER FUNCTIONS ---
def clean_gstin(val):
    if pd.isna(val) or not str(val).strip():
        return None
    val = str(val).strip().upper()
    if re.match(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$', val):
        return val
    return None

def format_hsn(val):
    if pd.isna(val):
        return ""
    val_str = str(val).split('.')[0].strip()
    return val_str if val_str != "nan" else ""

def format_date(val):
    if pd.isna(val):
        return ""
    try:
        return pd.to_datetime(val).strftime('%Y-%m-%d')
    except:
        return str(val).strip()


# --- ADAPTER A: COMPANY 1 (NESTED ITEM FILE OR SUMMARY FILE) ---
def parse_nested_company_1(item_file, summary_file=None):
    xls_item = pd.ExcelFile(item_file)
    records = []

    # Case A1: Item details sheet exists
    if any('ITEM' in s.upper() for s in xls_item.sheet_names):
        sheet_name = next(s for s in xls_item.sheet_names if 'ITEM' in s.upper())
        df_raw = pd.read_excel(xls_item, sheet_name=sheet_name)
        
        header_idx = next((idx for idx, row in df_raw.iterrows() if row.astype(str).str.contains('ITEM CODE').any()), None)
        if header_idx is not None:
            df_data = df_raw.iloc[header_idx + 1:].copy()
            df_data.columns = [str(c).strip() for c in df_raw.iloc[header_idx].values]
        else:
            df_data = df_raw.copy()

        current_parent = {}
        for _, row in df_data.iterrows():
            col0 = str(row.iloc[0]).strip()
            
            # Parent header row check
            if re.match(r'^\d{4}-\d{2}-\d{2}', col0) or re.match(r'^\d{2}/\d{2}/\d{4}', col0):
                current_parent = {
                    "invoice_date": format_date(row.iloc[0]),
                    "invoice_no": str(row.iloc[2]).strip() if not pd.isna(row.iloc[2]) else "",
                    "party_name": str(row.iloc[3]).strip() if not pd.isna(row.iloc[3]) else "Cash",
                }
            elif col0 and col0.isdigit() and current_parent.get("invoice_no"):
                records.append({
                    "invoice_no": current_parent.get("invoice_no"),
                    "invoice_date": current_parent.get("invoice_date"),
                    "party_name": current_parent.get("party_name"),
                    "gstin": None,
                    "is_b2c": True,
                    "item_code": str(row.get('ITEM CODE', '')).strip(),
                    "item_name": str(row.get('ITEM NAME', '')).strip(),
                    "hsn_code": format_hsn(row.get('HSNCODE', '')),
                    "quantity": float(row.get('QTY.', 0) or 0),
                    "rate": float(row.get('RATE', 0) or 0),
                    "taxable_amount": float(row.get('TAXABLE Amt.', 0) or 0),
                    "cgst_amount": float(row.get('GST AMT.', 0) or 0) / 2,
                    "sgst_amount": float(row.get('GST AMT.', 0) or 0) / 2,
                    "igst_amount": 0.0,
                    "gst_amount": float(row.get('GST AMT.', 0) or 0)
                })

    # Case A2: Summary file only (e.g. CONSOLIDATED SALE BOOK)
    elif any('SALE BOOK' in s.upper() or 'CONSOLIDATED' in s.upper() for s in xls_item.sheet_names):
        sheet_name = next(s for s in xls_item.sheet_names if 'SALE' in s.upper() or 'CONSOLIDATED' in s.upper())
        df_raw = pd.read_excel(xls_item, sheet_name=sheet_name)
        header_idx = next((idx for idx, row in df_raw.iterrows() if row.astype(str).str.contains('INV.NO').any()), None)
        
        if header_idx is not None:
            df_data = df_raw.iloc[header_idx + 1:].copy()
            df_data.columns = [str(c).strip() for c in df_raw.iloc[header_idx].values]
            for _, row in df_data.iterrows():
                inv_no = str(row.get('INV.NO', '')).strip()
                if not inv_no or inv_no == 'nan':
                    continue
                gstin_val = clean_gstin(row.get('GST NO'))
                records.append({
                    "invoice_no": inv_no,
                    "invoice_date": format_date(row.get('INV. DATE')),
                    "party_name": str(row.get('PARTY NAME', '')).strip(),
                    "gstin": gstin_val,
                    "is_b2c": False if gstin_val else True,
                    "item_code": "",
                    "item_name": "",
                    "hsn_code": "",
                    "quantity": 1.0,
                    "rate": 0.0,
                    "taxable_amount": float(row.get('Bill Amt.', 0) or 0),
                    "cgst_amount": 0.0,
                    "sgst_amount": 0.0,
                    "igst_amount": 0.0,
                    "gst_amount": 0.0
                })

    # Map Summary GSTINs if summary file provided separately
    if summary_file and records:
        xls_sum = pd.ExcelFile(summary_file)
        sum_sheet = next((s for s in xls_sum.sheet_names if 'CONSOLIDATED' in s.upper() or 'SALE' in s.upper()), xls_sum.sheet_names[0])
        df_sum_raw = pd.read_excel(xls_sum, sheet_name=sum_sheet)
        s_idx = next((i for i, r in df_sum_raw.iterrows() if r.astype(str).str.contains('INV.NO').any()), None)
        if s_idx is not None:
            df_sum = df_sum_raw.iloc[s_idx + 1:].copy()
            df_sum.columns = [str(c).strip() for c in df_sum_raw.iloc[s_idx].values]
            gstin_map = dict(zip(df_sum['INV.NO'].astype(str).str.strip(), df_sum['GST NO']))
            for rec in records:
                gstin_val = clean_gstin(gstin_map.get(rec['invoice_no']))
                rec['gstin'] = gstin_val
                rec['is_b2c'] = False if gstin_val else True

    return pd.DataFrame(records) if records else pd.DataFrame(columns=STANDARD_COLUMNS)


# --- ADAPTER B: COMPANY 2 (FLAT INTEGRATED FILE) ---
def parse_flat_company_2(file):
    xls = pd.ExcelFile(file)
    sheet_name = 'DATA' if 'DATA' in xls.sheet_names else xls.sheet_names[0]
    df = pd.read_excel(xls, sheet_name=sheet_name)
    
    records = []
    for _, row in df.iterrows():
        inv_no = str(row.get('Bill-Nos.', '')).strip()
        if not inv_no or inv_no == 'nan':
            continue
            
        gstin_val = clean_gstin(row.get('GST No.'))
        records.append({
            "invoice_no": inv_no,
            "invoice_date": format_date(row.get('Date')),
            "party_name": str(row.get('Account', '')).strip(),
            "gstin": gstin_val,
            "is_b2c": False if gstin_val else True,
            "item_code": "",
            "item_name": "",
            "hsn_code": format_hsn(row.get('HSN \\ SAC')),
            "quantity": float(row.get('Tot-Qty.', 0) or 0),
            "rate": 0.0,
            "taxable_amount": float(row.get('Taxable-Amt.', 0) or 0),
            "cgst_amount": float(row.get('CGST-Amt.', 0) or 0),
            "sgst_amount": float(row.get('SGST-Amt.', 0) or 0),
            "igst_amount": float(row.get('IGST-Amt.', 0) or 0),
            "gst_amount": float(row.get('GST-Amt.', 0) or 0)
        })
    return pd.DataFrame(records) if records else pd.DataFrame(columns=STANDARD_COLUMNS)


# --- STREAMLIT UI LAYOUT ---
sidebar = st.sidebar
sidebar.header("📁 Ingestion Settings")
file_type = sidebar.radio("Select Adapter Mode:", ["Auto-Detect", "Company 1 (Split Summary/Item Files)", "Company 2 (Single Flat Data Sheet)"])

col1, col2 = st.columns(2)

with col1:
    primary_file = st.file_uploader("Upload Primary Sales File", type=["xlsx", "xls"])
with col2:
    summary_file = st.file_uploader("Upload Summary File (Optional - for Company 1)", type=["xlsx", "xls"])

if primary_file:
    st.divider()
    st.subheader("⚙️ Processing Ingestion Pipelines")
    
    with st.spinner("Parsing and normalizing file data..."):
        try:
            if file_type == "Auto-Detect":
                xls = pd.ExcelFile(primary_file)
                if 'DATA' in xls.sheet_names or 'B2B INVOICE' in xls.sheet_names:
                    st.info("Detected Layout: **Company 2 Single Flat Integrated File**")
                    parsed_df = parse_flat_company_2(primary_file)
                else:
                    st.info("Detected Layout: **Company 1 Split/Nested Data File**")
                    parsed_df = parse_nested_company_1(primary_file, summary_file)
            elif file_type == "Company 1 (Split Summary/Item Files)":
                parsed_df = parse_nested_company_1(primary_file, summary_file)
            else:
                parsed_df = parse_flat_company_2(primary_file)

            if parsed_df.empty or 'invoice_no' not in parsed_df.columns:
                st.warning("⚠️ Could not extract valid invoice records from the uploaded file. Please verify file format or adapter mode.")
            else:
                st.success(f"Successfully normalized **{len(parsed_df)} line items**!")
                
                # --- DATA METRICS & PREVIEW ---
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Total Invoices", parsed_df['invoice_no'].nunique())
                m2.metric("Total Taxable Amt", f"₹ {parsed_df['taxable_amount'].sum():,.2f}")
                m3.metric("B2B Records", len(parsed_df[~parsed_df['is_b2c']]))
                m4.metric("B2C Records", len(parsed_df[parsed_df['is_b2c']]))

                st.write("### Normalized Data Preview")
                st.dataframe(parsed_df.head(100), use_container_width=True)

                # --- EXPORT MODULE ---
                st.divider()
                st.subheader("📥 Export Clean Output")
                
                exp_col1, exp_col2 = st.columns(2)
                
                output_excel = BytesIO()
                with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
                    parsed_df.to_excel(writer, index=False, sheet_name='Normalized_Sales_Data')
                output_excel.seek(0)
                
                exp_col1.download_button(
                    label="📊 Download Clean Excel File",
                    data=output_excel,
                    file_name="Clean_Normalized_Sales_Data.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

                json_str = parsed_df.to_json(orient='records', indent=2)
                exp_col2.download_button(
                    label="📄 Download Canonical JSON",
                    data=json_str,
                    file_name="Canonical_Sales_Data.json",
                    mime="application/json"
                )

        except Exception as e:
            st.error(f"Error parsing file: {str(e)}")
