"""
Unified test harness — one app, three layout families, dispatched through
orchestrator.py exactly the way production would.

Run with: streamlit run app.py
"""

import io
import json
import streamlit as st
import pandas as pd

from validate_schema import validate_and_decide, apply_transform
from ingest import normalize_sheet, forward_fill_blocks
from orchestrator import run_two_sheet_joined, run_single_sheet_grouped_blocks
from tally_export import (
    generate_tally_sales_export, 
    generate_tally_purchase_export,
    TallyExportConfig,
)
from hsn_summary import generate_all_hsn_summaries, HSNValidationReport

st.set_page_config(page_title="Invoice extractor — mapping test harness", layout="wide")
st.title("Invoice extractor — mapping test harness")
st.caption(
    "One fixed canonical schema, three layout families, per-file mappings pasted in "
    "(exactly what an LLM detection call would return) until that call is wired in."
)


# ---------------------------------------------------------------------------
# Helper function for displaying export results (defined BEFORE use)
# ---------------------------------------------------------------------------

def _display_export_results(result: tuple, mode: str, voucher_type: str):
    """Display Tally export results for a single mode (B2B or B2C)."""
    df_out, rpt = result
    
    st.subheader(f"{mode} — {rpt.vouchers_written} voucher(s), {rpt.rows_written} row(s)")
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Invoices in", rpt.total_invoices_in)
    c2.metric("Skipped (not validated)", len(rpt.skipped_not_validated))
    c3.metric("Skipped (no tax breakup)", len(rpt.skipped_no_tax_breakup))
    c4.metric("Balance mismatches", len(rpt.balance_mismatches))
    
    if rpt.balance_mismatches:
        st.error("Some vouchers do not balance Dr = Cr — review before importing to Tally.")
        st.dataframe(pd.DataFrame(rpt.balance_mismatches), use_container_width=True, hide_index=True)
    
    if rpt.gstin_state_mismatches:
        with st.expander(f"{mode}: data-quality flags ({len(rpt.gstin_state_mismatches)})"):
            st.dataframe(pd.DataFrame(rpt.gstin_state_mismatches), use_container_width=True, hide_index=True)
    
    if rpt.skipped_not_validated:
        with st.expander(f"{mode}: skipped — is_validated != True ({len(rpt.skipped_not_validated)})"):
            st.write(rpt.skipped_not_validated)
    
    if not df_out.empty:
        st.dataframe(df_out, use_container_width=True, hide_index=True)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_out.to_excel(writer, sheet_name="Accounting Voucher", index=False)
        st.download_button(
            f"Download {mode} Tally {voucher_type} vouchers (.xlsx)",
            data=buf.getvalue(),
            file_name=f"Tally{voucher_type}Vouchers_{mode}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_{voucher_type}_{mode}",
        )
    else:
        st.info(f"No {mode} {voucher_type} vouchers to export.")


# ---------------------------------------------------------------------------
# Example mappings for the three real files already validated end-to-end.
# ---------------------------------------------------------------------------

EXAMPLES = {
    "Purchase (two_sheet_joined)": {
        "layout_type": "two_sheet_joined",
        "item_mapping": {
            "sheet_type": "item_details", "header_rows": [0, 1], "data_start_row": 3,
            "invoice_block_marker": {
                "column": 0, "pattern": r"[A-Z]{2,4}/\d+",
                "blob_extract": {
                    "DATE": r"(?P<v>\d{2}-[A-Za-z]{3}-\d{2})",
                    "VOUCHERNUMBER": r"(?P<v>[A-Z]{2,4}/\d+)",
                    "PARTYNAME": r"[A-Z]{2,4}/\d+\s+(?P<v>.+?)\s+User",
                },
            },
            "item_row_column_map": {
                "STOCKITEMNAME": 2, "BATCHNAME": 5, "EXPIRYDATE": 7, "ACTUALQTY": 8,
                "FREEQTY": 9, "RATE": 10, "GSTRATE": 19, "AMOUNT": 21, "DISCOUNT": 13,
                "GSTAMOUNT": 22, "HSNCODE": 23,
            },
            "extra_fields": {"MARGIN1": 25, "MARGIN2": 26, "COST": 27},
            "skip_row_rules": [
                {"column": 2, "equals_normalized": "TOTAL:"},
                {"column": 2, "equals_normalized": "GRAND TOTAL:"},
            ],
            "confidence": 0.9,
            "voucher_type": "Purchase",
        },
        "summary_mapping": {
            "sheet_type": "consolidated_summary", "header_row": 0,
            "voucher_type": "Purchase",
            "footer_marker": {"column": 0, "equals_normalized": "Total :"},
            "voucher_number_pattern": r"^[A-Z]{2,4}/\d+$",
            "column_map": {
                "DATE": 3, "VOUCHERNUMBER": 1, "PARTYGSTIN": 2, "PARTYNAME": 6,
                "BILLAMOUNT": 7, "ROUNDOFFAMOUNT": 8, "STATECODE": 32,
                "REFERENCENUMBER": 4, "REFERENCEDATE": 0,
            },
            "tax_rate_breakup": [
                {"GSTRATE": 5, "TAXABLEVALUE": 10, "CGSTAMOUNT": 11, "SGSTAMOUNT": 12, "IGSTAMOUNT": 13},
                {"GSTRATE": 12, "TAXABLEVALUE": 14, "CGSTAMOUNT": 15, "SGSTAMOUNT": 16, "IGSTAMOUNT": 17},
                {"GSTRATE": 18, "TAXABLEVALUE": 18, "CGSTAMOUNT": 19, "SGSTAMOUNT": 20, "IGSTAMOUNT": 21},
                {"GSTRATE": 28, "TAXABLEVALUE": 22, "CGSTAMOUNT": 23, "SGSTAMOUNT": 24, "IGSTAMOUNT": 25},
                {"GSTRATE": 40, "TAXABLEVALUE": 26, "CGSTAMOUNT": 27, "SGSTAMOUNT": 28, "IGSTAMOUNT": 29},
                {"GSTRATE": 0, "TAXABLEVALUE": 30},
            ],
            "confidence": 0.93,
        },
        "transform": {"type": "identity"},
        "item_sheet_name": "Item Details",
        "summary_sheet_name": "Consolidated Summary",
    },
    "Sales (two_sheet_joined)": {
        "layout_type": "two_sheet_joined",
        "item_mapping": {
            "sheet_type": "item_details", "header_rows": [0, 1], "data_start_row": 2,
            "invoice_block_marker": {
                "column": 2, "pattern": r"S0/\d+",
                "fields": {
                    "VOUCHERNUMBER": 2, "DATE": 0, "PARTYNAME": 3,
                    "ROUNDOFFAMOUNT": 15, "BILLAMOUNT": 16,
                },
            },
            "item_row_column_map": {
                "STOCKITEMNAME": 1, "BATCHNAME": 3, "EXPIRYDATE": 5, "ACTUALQTY": 8,
                "FREEQTY": 9, "GSTRATE": 10, "RATE": 11, "AMOUNT": 12, "DISCOUNT": 13,
                "TAXABLEVALUE": 14, "NETAMOUNT": 15, "HSNCODE": 17, "GSTAMOUNT": 18,
            },
            "skip_row_rules": [],
            "confidence": 0.92,
            "voucher_type": "Sales",
        },
        "summary_mapping": {
            "sheet_type": "consolidated_summary", "header_row": 0,
            "voucher_type": "Sales",
            "footer_marker": {"column": 0, "equals_normalized": "Total :"},
            "voucher_number_pattern": r"^S0-\d+-\d+$",
            "column_map": {
                "DATE": 0, "VOUCHERNUMBER": 1, "PARTYNAME": 3, "PARTYGSTIN": 4,
                "BILLAMOUNT": 6, "ROUNDOFFAMOUNT": 7, "STATECODE": 32,
            },
            "tax_rate_breakup": [
                {"GSTRATE": 5, "TAXABLEVALUE": 9, "CGSTAMOUNT": 10, "SGSTAMOUNT": 11, "IGSTAMOUNT": 12},
                {"GSTRATE": 12, "TAXABLEVALUE": 13, "CGSTAMOUNT": 14, "SGSTAMOUNT": 15, "IGSTAMOUNT": 16},
                {"GSTRATE": 18, "TAXABLEVALUE": 17, "CGSTAMOUNT": 18, "SGSTAMOUNT": 19, "IGSTAMOUNT": 20},
                {"GSTRATE": 28, "TAXABLEVALUE": 21, "CGSTAMOUNT": 22, "SGSTAMOUNT": 23, "IGSTAMOUNT": 24},
                {"GSTRATE": 40, "TAXABLEVALUE": 25, "CGSTAMOUNT": 26, "SGSTAMOUNT": 27, "IGSTAMOUNT": 28},
                {"GSTRATE": 0, "TAXABLEVALUE": 29},
            ],
            "confidence": 0.93,
        },
        "transform": {"type": "regex_extract", "pattern": r"-(\d+)$", "template": "S0/{1}"},
        "item_sheet_name": "Item Details",
        "summary_sheet_name": "Consolidated Summary",
    },
    "GST Summary (single_sheet_grouped_blocks)": {
        "layout_type": "single_sheet_grouped_blocks",
        "ingest_mapping": {
            "block_header_marker": {"columns_present": [0, 1], "columns_blank": [2, 3]},
            "block_footer_marker": {"column": 0, "contains": "Total"},
            "forward_fill_columns": {"PARTYNAME": 0, "PARTYGSTIN": 1},
        },
        "grouped_mapping": {
            "sheet_type": "single_sheet_grouped_blocks",
            "line_identifier_field": "HSNCODE",
            "column_map": {
                "PARTYNAME": 0, "PARTYGSTIN": 1, "DATE": 2, "VOUCHERNUMBER": 3, "HSNCODE": 4,
                "ACTUALQTY": 5, "AMOUNT": 6, "TAXABLEVALUE": 7, "CGSTAMOUNT": 8,
                "SGSTAMOUNT": 9, "IGSTAMOUNT": 10, "CESSAMOUNT": 11, "GSTAMOUNT": 12,
            },
            "extra_fields": {"Is-Cash": 27},
            "confidence": 0.9,
        },
        "sheet_name": "ORIGINAL",
        "header_row": 6,
    },
}

with st.sidebar:
    st.header("1. Input")
    uploaded = st.file_uploader("Excel file", type=["xlsx", "xls"])
    layout_choice = st.radio(
        "Layout family",
        ["two_sheet_joined", "single_sheet_grouped_blocks", "single_sheet_flat"],
        help=(
            "two_sheet_joined: Summary + Item Details sheets joined by a key.\n"
            "single_sheet_grouped_blocks: one sheet, block-header rows needing forward-fill.\n"
            "single_sheet_flat: one sheet, one row per line item — untested, no sample file yet."
        ),
    )
    example_choice = st.selectbox("Load example mapping", ["(blank)"] + list(EXAMPLES.keys()))

    st.header("2b. Tally export settings")
    st.caption("Used only by the Tally voucher export button (section 5 below).")
    home_state_is_ut = st.radio(
        "Your registered state is a...",
        ["Union Territory (UTGST)", "State (SGST)"],
        index=0,
        help="Controls whether intrastate tax is posted to 'UTGST' or 'SGST' ledgers.",
    ) == "Union Territory (UTGST)"
    round_off_ledger_name = st.text_input("Round Off ledger name", value="Round Off")

example = EXAMPLES.get(example_choice)

st.header("2. Mapping (paste what an LLM detection call would return)")

# ===========================================================================
# TWO_SHEET_JOINED
# ===========================================================================
if layout_choice == "two_sheet_joined":
    item_sheet_name = st.text_input(
        "Item Details sheet name",
        value=example["item_sheet_name"] if example and "item_sheet_name" in example else "Item Details",
    )
    summary_sheet_name = st.text_input(
        "Consolidated Summary sheet name",
        value=example["summary_sheet_name"] if example and "summary_sheet_name" in example else "Consolidated Summary",
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        item_text = st.text_area(
            "Item Details mapping", height=380,
            value=json.dumps(example["item_mapping"], indent=2) if example and "item_mapping" in example else "{}",
        )
    with col2:
        summary_text = st.text_area(
            "Consolidated Summary mapping", height=380,
            value=json.dumps(example["summary_mapping"], indent=2) if example and "summary_mapping" in example else "{}",
        )
    with col3:
        transform_text = st.text_area(
            "Join transform", height=150,
            value=json.dumps(example["transform"], indent=2) if example and "transform" in example else '{"type": "identity"}',
        )
        st.caption("type: identity | strip_prefix | regex_extract")

    run = st.button("Run", type="primary", disabled=uploaded is None)

    if run and uploaded is not None:
        try:
            item_mapping = json.loads(item_text)
            summary_mapping = json.loads(summary_text)
            transform = json.loads(transform_text)
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
            st.stop()

        item_df = pd.read_excel(uploaded, sheet_name=item_sheet_name, header=None)
        summary_df = pd.read_excel(uploaded, sheet_name=summary_sheet_name, header=None)
        res = run_two_sheet_joined(item_df, summary_df, item_mapping, summary_mapping, transform)
        voucher_type = summary_mapping.get("voucher_type") or item_mapping.get("voucher_type")

        st.session_state["ts_res"] = res
        st.session_state["ts_item_df"] = item_df
        st.session_state["ts_summary_df"] = summary_df
        st.session_state["ts_item_mapping"] = item_mapping
        st.session_state["ts_summary_mapping"] = summary_mapping
        st.session_state["ts_transform"] = transform
        st.session_state["ts_voucher_type"] = voucher_type
        st.session_state.pop("tally_export_results", None)
        st.session_state.pop("hsn_summaries", None)

    # Render from session_state
    if st.session_state.get("ts_res") is not None:
        res = st.session_state["ts_res"]
        item_mapping = st.session_state["ts_item_mapping"]
        summary_mapping = st.session_state["ts_summary_mapping"]
        transform = st.session_state["ts_transform"]
        voucher_type = st.session_state["ts_voucher_type"]

        st.header("3. Layer A")
        if not res.layer_a_ok:
            st.error("FAILED")
            for f in res.layer_a_failures:
                st.write(f"- {f}")
            st.stop()
        st.success("PASSED")

        report = res.report
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Summary rows", report.total_summary_rows)
        m2.metric("Join match rate", f"{report.join_match_rate:.1%}")
        m3.metric("Matched to items", report.matched_invoices)
        m4.metric("Reconciled", report.reconciled_invoices)
        m5.metric("Mismatched", report.mismatched_invoices)

        no_items = [inv for inv in res.invoices if inv["is_validated"] is None]
        if no_items:
            st.info(
                f"{len(no_items)} invoice(s) have no matched items (is_validated: null) — "
                f"e.g. return/credit vouchers recorded in Summary but with no Item Details "
                f"block of their own. Review below to confirm these are expected, not a gap."
            )
            with st.expander(f"No-items invoices ({len(no_items)})", expanded=False):
                st.dataframe(
                    pd.DataFrame([{"VOUCHERNUMBER": i["VOUCHERNUMBER"], "PARTYNAME": i["PARTYNAME"],
                                    "BILLAMOUNT": i["BILLAMOUNT"]} for i in no_items]),
                    use_container_width=True, hide_index=True,
                )

        if report.join_match_rate < 0.9:
            st.error("Join match rate below 90% — check the transform JSON.")
            with st.expander("Debug: sample keys from both sides", expanded=True):
                from generic_parser import parse_item_details, parse_summary
                item_df = st.session_state["ts_item_df"]
                summary_df = st.session_state["ts_summary_df"]
                vouchers = parse_item_details(item_df, item_mapping)
                summary_rows = parse_summary(summary_df, summary_mapping)
                summary_sample = [r.get("VOUCHERNUMBER") for r in summary_rows[:10]]
                mapped_sample = [{"summary_key": k, "transform_output": apply_transform(k, transform) if k else None}
                                  for k in summary_sample]
                dc1, dc2 = st.columns(2)
                dc1.dataframe(pd.DataFrame(mapped_sample), use_container_width=True, hide_index=True)
                dc2.write(list(vouchers.keys())[:10])

        if report.mismatch_detail:
            st.subheader("Mismatched invoices")
            st.dataframe(pd.DataFrame(report.mismatch_detail), use_container_width=True, hide_index=True)

        st.header("4. Result")
        json_str = json.dumps(res.invoices, indent=2, default=str)
        st.download_button("Download JSON", data=json_str, file_name="invoices.json", mime="application/json", key="dl_json")
        with st.expander(f"Preview ({min(5, len(res.invoices))} of {len(res.invoices)})"):
            st.json(res.invoices[:5])

        # -------------------------------------------------------------
        # 4.5 HSN Summary Export
        # -------------------------------------------------------------
        st.header("4.5 HSN Summary Reports")
        st.caption(
            "Generate HSN-wise summary reports for B2B and B2C sales. "
            "Only invoices with is_validated = True are included. "
            "Reports include validation to ensure totals match invoice data."
        )
        
        if voucher_type == "Sales":
            if st.button("Generate HSN Summaries", type="primary", key="gen_hsn_btn"):
                summaries = generate_all_hsn_summaries(res.invoices, voucher_type, validate=True)
                st.session_state["hsn_summaries"] = summaries
            
            if st.session_state.get("hsn_summaries") is not None:
                summaries = st.session_state["hsn_summaries"]
                
                for mode, (df, validation_report) in summaries.items():
                    st.subheader(f"{mode} HSN Summary")
                    
                    if df.empty:
                        st.info(f"No {mode} data available")
                        continue
                    
                    # Display validation results
                    if validation_report:
                        col1, col2, col3, col4 = st.columns(4)
                        col1.metric("Total Invoices", validation_report.total_invoices)
                        col2.metric("Invoice Total", f"₹{validation_report.total_invoice_value:,.2f}")
                        col3.metric("HSN Total", f"₹{validation_report.total_hsn_value:,.2f}")
                        col4.metric("Difference", f"₹{validation_report.difference:,.2f}")
                        
                        # Show validation status
                        if validation_report.is_valid:
                            st.success(f"✅ Validation PASSED: HSN totals match invoice totals")
                        else:
                            st.error(f"❌ Validation FAILED: HSN totals do not match invoice totals (difference: ₹{validation_report.difference:,.2f})")
                        
                        # Show details of mismatched invoices
                        if validation_report.mismatched_invoices:
                            with st.expander(f"⚠️ Mismatched invoices ({len(validation_report.mismatched_invoices)})"):
                                st.dataframe(
                                    pd.DataFrame(validation_report.mismatched_invoices),
                                    use_container_width=True,
                                    hide_index=True
                                )
                        
                        if validation_report.missing_hsn_invoices:
                            with st.expander(f"⚠️ Invoices without HSN codes ({len(validation_report.missing_hsn_invoices)})"):
                                st.dataframe(
                                    pd.DataFrame(validation_report.missing_hsn_invoices),
                                    use_container_width=True,
                                    hide_index=True
                                )
                    
                    # Display metrics
                    total_value = df["Total Value"].sum()
                    total_taxable = df["Taxable Value"].sum()
                    total_tax = df["Integrated Tax Amount"].sum() + df["Central Tax Amount"].sum() + df["State/UT Tax Amount"].sum()
                    
                    c1, c2, c3 = st.columns(3)
                    c1.metric(f"{mode} - Total Value", f"₹{total_value:,.2f}")
                    c2.metric(f"{mode} - Taxable Value", f"₹{total_taxable:,.2f}")
                    c3.metric(f"{mode} - Total Tax", f"₹{total_tax:,.2f}")
                    
                    # Show the table
                    st.dataframe(df, use_container_width=True, hide_index=True)
                    
                    # Download buttons
                    col1, col2 = st.columns(2)
                    
                    csv = df.to_csv(index=False)
                    col1.download_button(
                        f"Download {mode} HSN Summary (.csv)",
                        data=csv,
                        file_name=f"hsn_{mode.lower()}.csv",
                        mime="text/csv",
                        key=f"dl_hsn_{mode}",
                    )
                    
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                        df.to_excel(writer, sheet_name=f"HSN_{mode}", index=False)
                    col2.download_button(
                        f"Download {mode} HSN Summary (.xlsx)",
                        data=buf.getvalue(),
                        file_name=f"hsn_{mode.lower()}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"dl_hsn_excel_{mode}",
                    )
        else:
            st.info(f"HSN summary generation is currently only implemented for Sales vouchers. Voucher type: {voucher_type}")

        # -------------------------------------------------------------
        # 5. Tally Voucher Export (Sales OR Purchase)
        # -------------------------------------------------------------
        st.header("5. Tally Voucher Export")
        
        config = TallyExportConfig(
            home_state_is_ut=home_state_is_ut,
            round_off_ledger_name=round_off_ledger_name,
        )
        
        if voucher_type == "Sales":
            st.caption(
                "Splits invoices into B2B / B2C (by whether PARTYGSTIN is present), builds "
                "Tally-importable 'Accounting Voucher' rows. "
                "Dr: Party, Cr: Sales + Output tax ledgers. "
                "Only invoices with is_validated = True are exported."
            )
            
            export_clicked = st.button("Prepare Tally Sales vouchers", type="primary", key="prep_tally_sales_btn")
            if export_clicked:
                st.session_state["tally_export_results"] = generate_tally_sales_export(res.invoices, config)
            
            tally_results = st.session_state.get("tally_export_results")
            if tally_results is not None:
                for mode in ("B2B", "B2C"):
                    _display_export_results(tally_results[mode], mode, "Sales")
        
        elif voucher_type == "Purchase":
            st.caption(
                "Generates Tally-importable 'Accounting Voucher' rows for purchase invoices. "
                "B2B: Dr = Purchase ledger + Input tax ledgers, Cr = Supplier (full amount). "
                "B2C: Dr = Purchase GST 0% (full amount), Cr = Supplier (full amount). "
                "Only invoices with is_validated = True are exported."
            )
            
            export_clicked = st.button("Prepare Tally Purchase vouchers", type="primary", key="prep_tally_purchase_btn")
            if export_clicked:
                st.session_state["tally_export_results"] = generate_tally_purchase_export(res.invoices, config)
            
            tally_results = st.session_state.get("tally_export_results")
            if tally_results is not None:
                for mode in ("B2B", "B2C"):
                    _display_export_results(tally_results[mode], mode, "Purchase")
        
        else:
            st.info(f"Tally export not yet implemented for voucher_type={voucher_type!r}")

# ===========================================================================
# SINGLE_SHEET_GROUPED_BLOCKS
# ===========================================================================
elif layout_choice == "single_sheet_grouped_blocks":
    sheet_name = st.text_input("Sheet name", value=example["sheet_name"] if example else "ORIGINAL")
    header_row = st.number_input(
        "Header row index (0-based)", min_value=0,
        value=example["header_row"] if example else 0,
    )

    col1, col2 = st.columns(2)
    with col1:
        ingest_text = st.text_area(
            "Ingestion mapping (block markers + forward-fill columns)", height=300,
            value=json.dumps(example["ingest_mapping"], indent=2) if example and "ingest_mapping" in example else "{}",
        )
        st.caption("block_header_marker / block_footer_marker / forward_fill_columns")
    with col2:
        grouped_text = st.text_area(
            "Grouped-blocks mapping (column_map into the canonical schema)", height=300,
            value=json.dumps(example["grouped_mapping"], indent=2) if example and "grouped_mapping" in example else "{}",
        )

    run = st.button("Run", type="primary", disabled=uploaded is None)

    if run and uploaded is not None:
        try:
            ingest_mapping = json.loads(ingest_text)
            grouped_mapping = json.loads(grouped_text)
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
            st.stop()

        raw = pd.read_excel(uploaded, sheet_name=sheet_name, header=None)
        res = run_single_sheet_grouped_blocks(raw, ingest_mapping, grouped_mapping, header_row=header_row)

        st.header("3. Layer A")
        if not res.layer_a_ok:
            st.error("FAILED")
            for f in res.layer_a_failures:
                st.write(f"- {f}")
            st.stop()
        st.success("PASSED")

        report = res.report
        m1, m2, m3 = st.columns(3)
        m1.metric("Invoices parsed", report.total_invoices)
        m2.metric("Reconciled", report.reconciled_invoices)
        m3.metric("Mismatched", report.mismatched_invoices)

        if report.mismatch_detail:
            st.subheader("Mismatched lines (taxable + tax vs stated amount)")
            st.dataframe(pd.DataFrame(report.mismatch_detail), use_container_width=True, hide_index=True)

        st.header("4. Result")
        json_str = json.dumps(res.invoices, indent=2, default=str)
        st.download_button("Download JSON", data=json_str, file_name="invoices.json", mime="application/json")
        with st.expander(f"Preview ({min(5, len(res.invoices))} of {len(res.invoices)})"):
            st.json(res.invoices[:5])

        multi_line = [i for i in res.invoices if len(i["lines"]) > 1]
        if multi_line:
            with st.expander(f"Multi-HSN invoices ({len(multi_line)} found) — grouping sanity check"):
                st.json(multi_line[:3])

# ===========================================================================
# SINGLE_SHEET_FLAT
# ===========================================================================
else:
    st.info(
        "No sample file confirms this layout yet. One row = one line item, with "
        "voucher-level fields (VOUCHERNUMBER, DATE, PARTYNAME, ...) repeated on every "
        "row belonging to that voucher. The parser (orchestrator.parse_single_sheet_flat) "
        "is written to the same pattern as the other two layouts but UNTESTED against a "
        "real export — paste a mapping below once you have a candidate file."
    )
    sheet_name = st.text_input("Sheet name", value="Sheet1")
    data_start_row = st.number_input("Data start row (0-based)", min_value=0, value=1)
    flat_text = st.text_area(
        "Flat-sheet mapping", height=300,
        value=json.dumps({
            "voucher_fields_column_map": {"VOUCHERNUMBER": 0, "DATE": 1, "PARTYNAME": 2},
            "item_row_column_map": {"STOCKITEMNAME": 3, "ACTUALQTY": 4, "RATE": 5, "AMOUNT": 6},
            "line_identifier_field": "STOCKITEMNAME",
            "data_start_row": 1,
        }, indent=2),
    )
    run = st.button("Run", type="primary", disabled=uploaded is None)
    if run and uploaded is not None:
        try:
            flat_mapping = json.loads(flat_text)
            flat_mapping["data_start_row"] = data_start_row
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
            st.stop()
        from orchestrator import parse_single_sheet_flat
        df_raw = pd.read_excel(uploaded, sheet_name=sheet_name, header=None)
        invoices = parse_single_sheet_flat(df_raw, flat_mapping)
        st.write(f"{len(invoices)} invoices parsed (no Layer A/B wired in yet for this layout)")
        st.json(invoices[:5])
