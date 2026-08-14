import streamlit as st
import pandas as pd
import json
import numpy as np

# --- Configuration ---
st.set_page_config(page_title="Invoice JSON Generator", layout="wide")

# --- Helper Functions ---
def safe_float(value):
    """Safely convert a value to float, return 0 if not possible."""
    if pd.isna(value):
        return 0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0

def process_invoice_data(file):
    """Reads the Excel file, processes B2B/B2C logic, and builds the JSON."""
    try:
        # 1. Read Summary sheet (header is in row 0)
        df_summary = pd.read_excel(file, sheet_name="Consolidated Summary", header=0)
        df_summary.columns = df_summary.columns.str.strip()
        df_summary = df_summary.replace({np.nan: None})
        
        # 2. Read Item Details sheet (raw, without headers - headers are in rows 3-4)
        df_items_raw = pd.read_excel(file, sheet_name="Item Details", header=None)
        
    except ValueError as e:
        st.error(f"Error loading sheets. Please ensure both 'Consolidated Summary' and 'Item Details' exist. Details: {e}")
        return None, None

    # Parse Item Details sheet
    # Structure: Invoice header rows (with TRN NO.) followed by item detail rows
    item_invoices = {}
    current_invoice = None
    
    for i in range(6, len(df_items_raw)):  # Data starts after header rows
        row = df_items_raw.iloc[i]
        trn_no = row[2]
        amount = row[16]
        item_name = row[1]
        
        # Check if this is an invoice header row (has TRN NO. like S0/999)
        if pd.notna(trn_no) and isinstance(trn_no, str) and trn_no.startswith('S0/'):
            current_invoice = trn_no
            item_invoices[current_invoice] = {
                'invoice_no_raw': trn_no,
                'patient_name': row[3] if pd.notna(row[3]) else None,
                'doctor_name': row[4] if pd.notna(row[4]) else None,
                'address': row[8] if pd.notna(row[8]) else None,
                'round_off': safe_float(row[15]),
                'total_amount': safe_float(amount),
                'items': []
            }
        # Check if this is an item row (has item name, no TRN NO.)
        elif current_invoice and pd.notna(item_name) and isinstance(item_name, str) and item_name != 'ITEM NAME':
            item = {
                'item_code': row[0] if pd.notna(row[0]) else None,
                'item_name': item_name,
                'batch': row[2] if pd.notna(row[2]) else None,
                'expiry': row[5] if pd.notna(row[5]) else None,
                'pack': row[6] if pd.notna(row[6]) else None,
                'qty': safe_float(row[7]),
                'free': safe_float(row[9]),
                'gst_percent': safe_float(row[10]),
                'rate': safe_float(row[11]),
                'amount': safe_float(row[12]),
                'discount': safe_float(row[13]),
                'taxable_amount': safe_float(row[14]),
                'net_amount': safe_float(row[15]),
                'hsn_code': row[17] if pd.notna(row[17]) else None,
                'gst_amount': safe_float(row[18]),
                'cat_dis_percent': safe_float(row[19]),
                'category': row[20] if pd.notna(row[20]) else None,
                'inc_srate': safe_float(row[21]),
                'dis_percent': safe_float(row[22]),
            }
            item_invoices[current_invoice]['items'].append(item)

    # Process Summary and match with Item Details
    final_json_data = []
    validation_log = []

    for index, row in df_summary.iterrows():
        invoice_no = row.get('INV.NO')
        
        # Skip header rows and total/summary rows at the bottom
        if not invoice_no or invoice_no == 'INV.NO' or str(invoice_no).startswith('Total'):
            continue
        
        # Also skip pure numeric rows (these are summary/total rows)
        if isinstance(invoice_no, (int, float)):
            continue
            
        gst_no = row.get('GST NO')
        expected_total = safe_float(row.get('Bill Amt.', 0))
        party_name = row.get('PARTY NAME')
        
        # Determine B2B vs B2C: If GST No exists and is a valid GST string
        is_b2b = bool(gst_no and isinstance(gst_no, str) and len(str(gst_no).strip()) > 5)
        customer_type = "B2B" if is_b2b else "B2C"
        
        # Convert invoice number format to match Item Details
        # Summary format: S0-26-999 -> Item Details format: S0/999
        inv_parts = str(invoice_no).split('-')
        if len(inv_parts) >= 3:
            item_inv_key = f"S0/{inv_parts[-1]}"
        else:
            item_inv_key = invoice_no
        
        # Get matching items from Item Details
        item_data = item_invoices.get(item_inv_key, {})
        items = item_data.get('items', [])
        
        # Calculate sum from items
        calculated_sum = sum(safe_float(item.get('net_amount', 0)) for item in items)
        
        # Validation: Check if calculated sum aligns with summary bill amount
        # Using a tolerance of 1.0 to account for rounding issues
        is_valid = abs(calculated_sum - expected_total) <= 1.0
        
        invoice_record = {
            "invoice_number": invoice_no,
            "customer_type": customer_type,
            "gst_number": gst_no if is_b2b else None,
            "party_name": party_name,
            "summary_bill_amount": expected_total,
            "summary_details": {
                "inv_date": row.get('INV. DATE'),
                "party_code": row.get('Party Code'),
                "rnd_amt": row.get('Rnd Amt'),
                "other_amt": row.get('Other Amt.'),
                "gst_5_amt": row.get('GST 5 Amt.'),
                "cgst_2_5": row.get('CGST 2.5'),
                "sgst_2_5": row.get('SGST 2.5'),
                "state_code": row.get('StateCode'),
            },
            "items": items,
            "item_details_meta": {
                "patient_name": item_data.get('patient_name'),
                "doctor_name": item_data.get('doctor_name'),
                "address": item_data.get('address'),
                "round_off": item_data.get('round_off'),
                "items_calculated_total": calculated_sum,
            },
            "is_validated": is_valid
        }
        
        final_json_data.append(invoice_record)

        if not is_valid:
            validation_log.append({
                "Invoice": invoice_no,
                "Item Key": item_inv_key,
                "Summary Total": expected_total,
                "Items Calculated Sum": round(calculated_sum, 2),
                "Difference": round(abs(calculated_sum - expected_total), 2),
                "Items Count": len(items)
            })

    return final_json_data, validation_log


# --- Streamlit UI ---
st.title("📄 Invoice to JSON Processor")
st.markdown("""
This app reads a consolidated summary and item details from an Excel file, applies B2B/B2C logic based on GST presence, validates item totals against the bill amount, and generates a nested JSON file.

**Supported format:** The Excel file must have two sheets:
1. **Consolidated Summary** - with columns: INV.NO, PARTY NAME, GST NO, Bill Amt., etc.
2. **Item Details** - with invoice header rows (TRN NO.) followed by item rows
""")

st.divider()

# File Uploader
uploaded_file = st.file_uploader("Upload your Excel file (.xlsx)", type=["xlsx", "xls"])

if uploaded_file is not None:
    st.info("File uploaded successfully. Processing...")
    
    with st.spinner('Building JSON and validating totals...'):
        json_data, val_errors = process_invoice_data(uploaded_file)
        
    if json_data is not None:
        b2b_count = sum(1 for inv in json_data if inv['customer_type'] == 'B2B')
        b2c_count = sum(1 for inv in json_data if inv['customer_type'] == 'B2C')
        
        st.success(f"Successfully processed {len(json_data)} invoices! (B2B: {b2b_count}, B2C: {b2c_count})")
        
        # --- Display Validation Results ---
        if val_errors:
            st.warning(f"⚠️ Validation Mismatches Found ({len(val_errors)} invoices)")
            with st.expander("View Validation Errors"):
                st.dataframe(pd.DataFrame(val_errors), use_container_width=True)
        else:
            st.success("✅ All invoice item totals perfectly matched their summary bill amounts!")

        # --- Display and Download JSON ---
        st.subheader("Generated JSON")
        
        # Convert Python dictionary to formatted JSON string
        json_string = json.dumps(json_data, indent=4, default=str)
        
        # Download Button
        st.download_button(
            label="⬇️ Download JSON File",
            data=json_string,
            file_name="processed_invoices.json",
            mime="application/json"
        )
        
        # Preview
        with st.expander("Preview JSON Data", expanded=False):
            st.json(json_data[:5] if len(json_data) > 5 else json_data)  # Show first 5 invoices
