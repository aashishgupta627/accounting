import streamlit as st
import pandas as pd
import json
import numpy as np

# --- Configuration ---
st.set_page_config(page_title="Invoice JSON Generator", layout="wide")

# --- Helper Functions ---
def process_invoice_data(file):
    """Reads the Excel file, processes B2B/B2C logic, and builds the JSON."""
    try:
        # 1. Read the specific sheets
        df_summary = pd.read_excel(file, sheet_name="Consolidated Summary")
        df_items = pd.read_excel(file, sheet_name="Item Details")
        
        # Strip whitespace from column names just in case
        df_summary.columns = df_summary.columns.str.strip()
        df_items.columns = df_items.columns.str.strip()

        # Clean NaN values to None for better JSON formatting
        df_summary = df_summary.replace({np.nan: None})
        df_items = df_items.replace({np.nan: None})

    except ValueError as e:
        st.error(f"Error loading sheets. Please ensure both 'Consolidated Summary' and 'Item Details' exist. Details: {e}")
        return None, None

    # Replace these with your actual column names if they differ
    INV_KEY_SUMMARY = "Invoice Number"
    INV_KEY_ITEMS = "Invoice Number"
    GST_COL = "GST No"
    TOTAL_BILL_COL = "Total Bill Amount"
    ITEM_AMOUNT_COL = "Item Amount"
    
    final_json_data = []
    validation_log = []

    # 2. Iterate over the Consolidated Summary
    for index, row in df_summary.iterrows():
        invoice_no = row.get(INV_KEY_SUMMARY)
        if not invoice_no:
            continue
            
        gst_no = row.get(GST_COL)
        expected_total = float(row.get(TOTAL_BILL_COL, 0.0) or 0.0)
        
        # Determine B2B vs B2C logic
        # If GST No exists and is a string of reasonable length, it's B2B
        is_b2b = bool(gst_no and isinstance(gst_no, str) and len(gst_no.strip()) > 5)
        customer_type = "B2B" if is_b2b else "B2C"

        # Build parent JSON structure
        invoice_record = {
            "invoice_number": invoice_no,
            "customer_type": customer_type,
            "gst_number": gst_no if is_b2b else None,
            "summary_bill_amount": expected_total,
            "other_summary_details": {k: v for k, v in row.items() if k not in [INV_KEY_SUMMARY, GST_COL, TOTAL_BILL_COL]},
            "items": []
        }

        # 3. Dive into Item Details sheet
        matching_items = df_items[df_items[INV_KEY_ITEMS] == invoice_no]
        
        calculated_sum = 0.0
        
        for _, item_row in matching_items.iterrows():
            item_amount = float(item_row.get(ITEM_AMOUNT_COL, 0.0) or 0.0)
            calculated_sum += item_amount
            
            # Remove the invoice key from the item details to avoid redundancy
            item_dict = {k: v for k, v in item_row.items() if k != INV_KEY_ITEMS}
            invoice_record["items"].append(item_dict)

        # 4. Validation: Check if calculated sum aligns with summary bill amount
        # Using a small tolerance (e.g., 0.01) to account for floating point/rounding issues
        is_valid = abs(calculated_sum - expected_total) <= 0.01
        
        invoice_record["is_validated"] = is_valid
        final_json_data.append(invoice_record)

        if not is_valid:
            validation_log.append({
                "Invoice": invoice_no,
                "Summary Total": expected_total,
                "Items Calculated Sum": calculated_sum,
                "Difference": round(abs(calculated_sum - expected_total), 2)
            })

    return final_json_data, validation_log


# --- Streamlit UI ---
st.title("📄 Invoice to JSON Processor")
st.markdown("""
This app reads a consolidated summary and item details from an Excel file, applies B2B/B2C logic based on GST presence, validates item totals against the bill amount, and generates a nested JSON file.
""")

st.divider()

# File Uploader
uploaded_file = st.file_uploader("Upload your Excel file (.xlsx)", type=["xlsx", "xls"])

if uploaded_file is not None:
    st.info("File uploaded successfully. Processing...")
    
    with st.spinner('Building JSON and validating totals...'):
        json_data, val_errors = process_invoice_data(uploaded_file)
        
    if json_data is not None:
        st.success(f"Successfully processed {len(json_data)} invoices!")
        
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
        json_string = json.dumps(json_data, indent=4)
        
        # Download Button
        st.download_button(
            label="⬇️ Download JSON File",
            data=json_string,
            file_name="processed_invoices.json",
            mime="application/json"
        )
        
        # Preview
        with st.expander("Preview JSON Data", expanded=False):
            st.json(json_data)
