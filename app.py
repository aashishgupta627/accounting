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

def find_data_start_row(df_items_raw):
    """Dynamically find where actual invoice data starts in Item Details sheet."""
    for i in range(len(df_items_raw)):
        row = df_items_raw.iloc[i]
        vals = [str(v).strip().upper() if pd.notna(v) else '' for v in row]
        # Look for the item sub-header row containing 'ITEM CODE' and 'ITEM NAME'
        if 'ITEM CODE' in vals and 'ITEM NAME' in vals:
            return i + 1  # Data starts after this header row
    # Fallback: look for first row with S0/ pattern
    for i in range(len(df_items_raw)):
        row = df_items_raw.iloc[i]
        for val in row:
            if pd.notna(val) and isinstance(val, str) and val.startswith('S0/'):
                return i
    return 0

def process_invoice_data(file):
    """Reads the Excel file, processes B2B/B2C logic, and builds the JSON."""
    try:
        # 1. Read Summary sheet (header is in row 0)
        df_summary = pd.read_excel(file, sheet_name="Consolidated Summary", header=0)
        df_summary.columns = df_summary.columns.str.strip()
        df_summary = df_summary.replace({np.nan: None})

        # 2. Read Item Details sheet (raw, without headers - headers are in rows 0-1)
        df_items_raw = pd.read_excel(file, sheet_name="Item Details", header=None)

    except ValueError as e:
        st.error(f"Error loading sheets. Please ensure both 'Consolidated Summary' and 'Item Details' exist. Details: {e}")
        return None, None, None, None, None

    # --- Dynamically find data start row ---
    data_start_row = find_data_start_row(df_items_raw)
    st.info(f"Item Details data starts at row index {data_start_row} (dynamically detected)")

    # Parse Item Details sheet
    # Structure: Invoice header rows (with TRN NO.) followed by item detail rows
    item_invoices = {}
    current_invoice = None

    for i in range(data_start_row, len(df_items_raw)):
        row = df_items_raw.iloc[i]
        trn_no = row[2]
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
                'total_amount': safe_float(row[16]),
                'items': []
            }
        # Check if this is an item row (has item name, no TRN NO.)
        elif current_invoice and pd.notna(item_name) and isinstance(item_name, str) and item_name.strip() != '' and item_name != 'ITEM NAME':
            item = {
                'item_code': str(row[0]) if pd.notna(row[0]) else None,
                'item_name': item_name,
                'batch': str(row[3]) if pd.notna(row[3]) else None,
                'expiry': str(row[5]) if pd.notna(row[5]) else None,
                'pack': str(row[7]) if pd.notna(row[7]) else None,
                'qty': safe_float(row[8]),
                'free': safe_float(row[9]),
                'gst_percent': safe_float(row[10]),
                'rate': safe_float(row[11]),
                'amount': safe_float(row[12]),
                'discount': safe_float(row[13]),
                'taxable_amount': safe_float(row[14]),
                'net_amount': safe_float(row[15]),
                'hsn_code': str(row[17]) if pd.notna(row[17]) else None,
                'gst_amount': safe_float(row[18]),
                'cat_dis_percent': safe_float(row[19]),
                'category': str(row[20]) if pd.notna(row[20]) else None,
                'inc_srate': safe_float(row[21]),
                'dis_percent': safe_float(row[22]),
            }
            item_invoices[current_invoice]['items'].append(item)

    # Process Summary and match with Item Details
    final_json_data = []
    validation_log = []

    # For HSN-wise summaries
    b2b_hsn_summary = {}
    b2c_hsn_summary = {}

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
        round_off = item_data.get('round_off', 0)
        adjusted_sum = calculated_sum + round_off

        # Validation: Check if adjusted sum aligns with summary bill amount
        # Using a tolerance of 1.0 to account for rounding issues
        is_valid = abs(adjusted_sum - expected_total) <= 1.0

        # HSN-wise aggregation
        for item in items:
            hsn = item.get('hsn_code')
            taxable = safe_float(item.get('taxable_amount', 0))
            gst_amt = safe_float(item.get('gst_amount', 0))
            net_amt = safe_float(item.get('net_amount', 0))

            if hsn:
                hsn_key = str(hsn)
                if is_b2b:
                    if hsn_key not in b2b_hsn_summary:
                        b2b_hsn_summary[hsn_key] = {'taxable_value': 0, 'gst_amount': 0, 'net_amount': 0, 'count': 0}
                    b2b_hsn_summary[hsn_key]['taxable_value'] += taxable
                    b2b_hsn_summary[hsn_key]['gst_amount'] += gst_amt
                    b2b_hsn_summary[hsn_key]['net_amount'] += net_amt
                    b2b_hsn_summary[hsn_key]['count'] += 1
                else:
                    if hsn_key not in b2c_hsn_summary:
                        b2c_hsn_summary[hsn_key] = {'taxable_value': 0, 'gst_amount': 0, 'net_amount': 0, 'count': 0}
                    b2c_hsn_summary[hsn_key]['taxable_value'] += taxable
                    b2c_hsn_summary[hsn_key]['gst_amount'] += gst_amt
                    b2c_hsn_summary[hsn_key]['net_amount'] += net_amt
                    b2c_hsn_summary[hsn_key]['count'] += 1

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
                "adjusted_total": adjusted_sum,
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
                "Round Off": round(round_off, 2),
                "Adjusted Sum": round(adjusted_sum, 2),
                "Difference": round(abs(adjusted_sum - expected_total), 2),
                "Items Count": len(items)
            })

    return final_json_data, validation_log, b2b_hsn_summary, b2c_hsn_summary, df_items_raw


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
        json_data, val_errors, b2b_hsn, b2c_hsn, df_items_raw = process_invoice_data(uploaded_file)

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

        # --- Total Taxable Value Report ---
        st.subheader("📊 Sales Summary Reports")

        total_taxable = 0
        total_gst = 0
        total_net = 0

        for inv in json_data:
            for item in inv['items']:
                total_taxable += safe_float(item.get('taxable_amount', 0))
                total_gst += safe_float(item.get('gst_amount', 0))
                total_net += safe_float(item.get('net_amount', 0))

        col1, col2, col3 = st.columns(3)
        col1.metric("Total Taxable Value", f"₹{total_taxable:,.2f}")
        col2.metric("Total GST Amount", f"₹{total_gst:,.2f}")
        col3.metric("Total Net Amount", f"₹{total_net:,.2f}")

        # --- B2B HSN-wise Summary ---
        if b2b_hsn:
            st.markdown("#### 🏢 B2B HSN-wise Sales Summary")
            b2b_df = pd.DataFrame([
                {
                    'HSN Code': hsn,
                    'Taxable Value': data['taxable_value'],
                    'GST Amount': data['gst_amount'],
                    'Net Amount': data['net_amount'],
                    'Item Count': data['count']
                }
                for hsn, data in sorted(b2b_hsn.items())
            ])
            b2b_df.loc[len(b2b_df)] = {
                'HSN Code': 'TOTAL',
                'Taxable Value': b2b_df['Taxable Value'].sum(),
                'GST Amount': b2b_df['GST Amount'].sum(),
                'Net Amount': b2b_df['Net Amount'].sum(),
                'Item Count': b2b_df['Item Count'].sum()
            }
            st.dataframe(b2b_df, use_container_width=True, hide_index=True)

        # --- B2C HSN-wise Summary ---
        if b2c_hsn:
            st.markdown("#### 🛒 B2C HSN-wise Sales Summary")
            b2c_df = pd.DataFrame([
                {
                    'HSN Code': hsn,
                    'Taxable Value': data['taxable_value'],
                    'GST Amount': data['gst_amount'],
                    'Net Amount': data['net_amount'],
                    'Item Count': data['count']
                }
                for hsn, data in sorted(b2c_hsn.items())
            ])
            b2c_df.loc[len(b2c_df)] = {
                'HSN Code': 'TOTAL',
                'Taxable Value': b2c_df['Taxable Value'].sum(),
                'GST Amount': b2c_df['GST Amount'].sum(),
                'Net Amount': b2c_df['Net Amount'].sum(),
                'Item Count': b2c_df['Item Count'].sum()
            }
            st.dataframe(b2c_df, use_container_width=True, hide_index=True)

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
