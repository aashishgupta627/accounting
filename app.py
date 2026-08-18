"""
Unified test harness — one app, three layout families, dispatched through
orchestrator.py exactly the way production would.

TERMINOLOGY (see validate_schema.py's module docstring for the full
version): the CANONICAL SCHEMA (VOUCHER_FIELDS / ITEM_FIELDS /
SUMMARY_FIELDS) is fixed and never changes. What you paste into the text
areas below is a MAPPING — per-file column addresses into that fixed
schema, plus an open extra_fields bucket for anything vendor-specific that
isn't part of the schema at all.

Run with: streamlit run app.py
"""
import json
import streamlit as st
import pandas as pd

from validate_schema import validate_and_decide, apply_transform
from ingest import normalize_sheet, forward_fill_blocks
from orchestrator import run_two_sheet_joined, run_single_sheet_grouped_blocks

st.set_page_config(page_title="Invoice extractor — mapping test harness", layout="wide")
st.title("Invoice extractor — mapping test harness")
st.caption(
    "One fixed canonical schema, three layout families, per-file mappings pasted in "
    "(exactly what an LLM detection call would return) until that call is wired in."
)

# ---------------------------------------------------------------------------
# Example mappings for the three real files already validated end-to-end.
# ---------------------------------------------------------------------------

EXAMPLES = {
    "Purchase (two_sheet_joined)": {
        "layout_type": "two_sheet_joined",
        "item_mapping": {
            "sheet_type": "item_details", "header_rows": [0, 1], "data_start_row": 3,
            "invoice_block_marker": {
                "column": 0, "pattern": r"PB/\d+",
                "blob_extract": {
                    "DATE": r"(?P<v>\d{2}-[A-Za-z]{3}-\d{2})",
                    "VOUCHERNUMBER": r"(?P<v>PB/\d+)",
                    "PARTYNAME": r"PB/\d+\s+(?P<v>.+?)\s+User",
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
        },
        "summary_mapping": {
            "sheet_type": "consolidated_summary", "header_row": 0,
            "voucher_type": "Purchase",
            "footer_marker": {"column": 0, "equals_normalized": "Total :"},
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
        },
        "summary_mapping": {
            "sheet_type": "consolidated_summary", "header_row": 0,
            "voucher_type": "Sales",
            "footer_marker": {"column": 0, "equals_normalized": "Total :"},
            "column_map": {
                "DATE": 0, "VOUCHERNUMBER": 1, "PARTYNAME": 3, "PARTYGSTIN": 4,
                "BILLAMOUNT": 6, "ROUNDOFFAMOUNT": 7, "STATECODE": 32,
            },
            "tax_rate_breakup": [
                {"GSTRATE": 5, "TAXABLEVALUE": 9, "CGSTAMOUNT": 10, "SGSTAMOUNT": 11, "IGSTAMOUNT": 12},
                {"GSTRATE": 12, "TAXABLEVALUE": 13, "CGSTAMOUNT": 14, "SGSTAMOUNT": 15, "IGSTAMOUNT": 16},
                {"GSTRATE": 18, "TAXABLEVALUE": 17, "CGSTAMOUNT": 18, "SGSTAMOUNT": 19, "IGSTAMOUNT": 20},
                {"GSTRATE": 28, "TAXABLEVALUE": 21, "CGSTAMOUNT": 22, "SGSTAMOUNT": 23, "IGSTAMOUNT": 24},
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

        if report.join_match_rate < 0.9:
            st.error("Join match rate below 90% — check the transform JSON.")
            with st.expander("Debug: sample keys from both sides", expanded=True):
                vouchers = None  # already consumed inside run_two_sheet_joined; recompute for display
                from generic_parser import parse_item_details, parse_summary
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
        st.download_button("Download JSON", data=json_str, file_name="invoices.json", mime="application/json")
        with st.expander(f"Preview ({min(5, len(res.invoices))} of {len(res.invoices)})"):
            st.json(res.invoices[:5])

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
# SINGLE_SHEET_FLAT (no sample file yet — stub UI, same shape as the others)
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
