import streamlit as st
import pandas as pd
import re
import json
import io

st.set_page_config(
    page_title="Purchase Book Converter & GST Formatter", 
    page_icon="📊", 
    layout="wide"
)

st.title("📊 Purchase Book Parser & GST Formatter")
st.write(
    "Upload your raw purchase book Excel file to split by taxability "
    "(Taxable, Exempted, CESS) and download cleaned Excel & JSON formats."
)

uploaded_file = st.file_uploader("Upload Raw Purchase Book (.xlsx / .xls)", type=["xlsx", "xls"])

if uploaded_file is not None:
    try:
        xls = pd.ExcelFile(uploaded_file)
        sheet_name = st.selectbox("Select Sheet", xls.sheet_names)
        df = pd.read_excel(xls, sheet_name=sheet_name)
        
        invoices = []
        current_inv = None

        for idx, row in df.iterrows():
            col0 = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
            col2 = str(row.iloc[2]) if pd.notna(row.iloc[2]) else ""

            # Invoice Header Detection
            if ("PB/" in col0 or "TRN" in col0 or re.search(r'\d{2}-[A-Za-z]{3}-\d{2}', col0)) and "FROM:" not in col0 and "PURCHASE_BOOK" not in col0:
                match = re.search(r'(\d{2}-[A-Za-z]{3}-\d{2})\s+(PB/\d+|\S+)\s+(.*?)\s+(User\s*:.*)?$', col0.strip())
                if match:
                    date, trn_no, party_name, user = match.groups()
                    current_inv = {
                        'date': date,
                        'voucher_no': trn_no,
                        'supplier_name': party_name.strip(),
                        'user': user.strip() if user else "",
                        'items': [],
                        'taxable_value': 0.0,
                        'tax_value': 0.0,
                        'exempted_value': 0.0,
                        'gst_cess_value': 0.0,
                        'total_amount': 0.0
                    }
                    invoices.append(current_inv)

            elif current_inv is not None:
                if col2 == "TOTAL:":
                    taxable, tax, exempt = 0.0, 0.0, 0.0
                    for item in current_inv['items']:
                        if item['gst_pct'] > 0:
                            taxable += item['amount']
                            tax += item['gst_amt']
                        else:
                            exempt += item['amount']

                    current_inv['taxable_value'] = round(taxable, 2)
                    current_inv['tax_value'] = round(tax, 2)
                    current_inv['exempted_value'] = round(exempt, 2)
                    current_inv['total_amount'] = round(taxable + tax + exempt, 2)

                elif col2 not in ['GRAND TOTAL:', 'ITEM NAME', ''] and pd.notna(row.iloc[2]):
                    gst_pct = float(pd.to_numeric(row.iloc[19], errors='coerce') or 0.0)
                    amount = float(pd.to_numeric(row.iloc[21], errors='coerce') or 0.0)
                    gst_amt = float(pd.to_numeric(row.iloc[22], errors='coerce') or 0.0)

                    item = {
                        'item_name': col2,
                        'qty': float(pd.to_numeric(row.iloc[8], errors='coerce') or 0.0),
                        'p_rate': float(pd.to_numeric(row.iloc[10], errors='coerce') or 0.0),
                        'gst_pct': gst_pct,
                        'amount': amount,
                        'gst_amt': gst_amt,
                        'hsn': str(row.iloc[23]) if pd.notna(row.iloc[23]) else ""
                    }
                    current_inv['items'].append(item)

        # Build Summary DF
        summary_data = []
        for inv in invoices:
            summary_data.append({
                'Date': inv['date'],
                'Voucher No': inv['voucher_no'],
                'Supplier Name': inv['supplier_name'],
                'Taxable Value': inv['taxable_value'],
                'Tax Value': inv['tax_value'],
                'Exempted Value': inv['exempted_value'],
                'GST Cess Value': inv['gst_cess_value'],
                'Total Amount': inv['total_amount']
            })

        summary_df = pd.DataFrame(summary_data)

        st.success(f"Successfully processed {len(summary_df)} invoices!")
        
        # Display Summary Metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Taxable Value", f"₹{summary_df['Taxable Value'].sum():,.2f}")
        c2.metric("Total Tax Value", f"₹{summary_df['Tax Value'].sum():,.2f}")
        c3.metric("Total Exempted Value", f"₹{summary_df['Exempted Value'].sum():,.2f}")
        c4.metric("Grand Total Amount", f"₹{summary_df['Total Amount'].sum():,.2f}")

        st.subheader("Invoice Level Preview")
        st.dataframe(summary_df, use_container_width=True)

        # Export Buttons
        col_ex, col_js = st.columns(2)

        # Excel Buffer
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            summary_df.to_excel(writer, index=False, sheet_name='Summary_Register')
        excel_buffer.seek(0)

        col_ex.download_button(
            label="📥 Download Clean Excel",
            data=excel_buffer,
            file_name="Converted_Purchase_Book.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        # JSON Buffer
        json_str = json.dumps(invoices, indent=4)
        col_js.download_button(
            label="📥 Download JSON Format",
            data=json_str,
            file_name="Converted_Purchase_Book.json",
            mime="application/json"
        )

    except Exception as e:
        st.error(f"Error processing file: {e}")
