"""
Testing harness for the schema-driven pipeline.

Workflow (LLM call intentionally NOT wired in yet — schema JSON is pasted in
by hand, exactly what an LLM detection call would return, so every other
stage can be tested independently first):

  1. Upload an Excel file (Purchase or Sales export)
  2. Paste/edit the Item Details schema, the Summary schema, and the join
     transform as JSON
  3. Run Layer A -> pass/fail + stats for each schema, shown immediately
  4. If both pass, run the deterministic parser -> Layer B report
  5. Preview + download the resulting JSON

Run with: streamlit run streamlit_app.py
"""
import json
import streamlit as st
import pandas as pd

from validate_schema import validate_and_decide, validate_join_transform
from generic_parser import parse_item_details, parse_summary, build_invoices

st.set_page_config(page_title="Schema-driven invoice extractor", layout="wide")
st.title("Schema-driven invoice extractor — test harness")
st.caption(
    "Paste the schema JSON an LLM detection call would produce (or a hand-written "
    "one) and test Layer A validation + the deterministic parser against a real file."
)

EXAMPLE_SCHEMAS = {
    "Purchase": {
        "item_details": {
            "sheet_type": "item_details", "header_rows": [0, 1], "data_start_row": 3,
            "invoice_block_marker": {
                "column": 0, "pattern": r"PB/\d+",
                "blob_extract": {
                    "DATE": r"^\s*(?P<v>\d{2}-\w{3}-\d{2})",
                    "VOUCHERNUMBER": r"(?P<v>PB/\d+)",
                    "PARTYNAME": r"PB/\d+\s+(?P<v>.+?)\s+User",
                },
            },
            "item_row_column_map": {
                "STOCKITEMNAME": 2, "BATCHNAME": 5, "EXPIRYDATE": 7, "ACTUALQTY": 8,
                "FREEQTY": 9, "RATE": 10, "GSTRATE": 19, "AMOUNT": 21, "DISCOUNT": 13,
                "GSTAMOUNT": 22, "HSNCODE": 23,
            },
            "skip_row_rules": [{"column": 2, "equals": "TOTAL:"}],
            "confidence": 0.9,
        },
        "summary": {
            "sheet_type": "consolidated_summary", "header_row": 0,
            "column_map": {
                "VOUCHERNUMBER": 1, "PARTYGSTIN": 2, "PARTYNAME": 6,
                "BILLAMOUNT": 7, "ROUNDOFFAMOUNT": 8,
            },
            "confidence": 0.93,
        },
        "transform": {"type": "identity"},
    },
    "Sales": {
        "item_details": {
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
        "summary": {
            "sheet_type": "consolidated_summary", "header_row": 0,
            "column_map": {
                "VOUCHERNUMBER": 1, "PARTYNAME": 3, "PARTYGSTIN": 4,
                "BILLAMOUNT": 6, "ROUNDOFFAMOUNT": 7,
            },
            "confidence": 0.93,
        },
        "transform": {"type": "regex_extract", "pattern": r"-(\d+)$", "template": "S0/{1}"},
    },
}

with st.sidebar:
    st.header("1. Input")
    uploaded = st.file_uploader("Excel file", type=["xlsx", "xls"])
    doc_type = st.radio("Load example schema for", ["Purchase", "Sales", "(blank)"], index=2)
    item_sheet_name = st.text_input("Item Details sheet name", value="Item Details")
    summary_sheet_name = st.text_input("Consolidated Summary sheet name", value="Consolidated Summary")

if doc_type in EXAMPLE_SCHEMAS:
    default_item = json.dumps(EXAMPLE_SCHEMAS[doc_type]["item_details"], indent=2)
    default_summary = json.dumps(EXAMPLE_SCHEMAS[doc_type]["summary"], indent=2)
    default_transform = json.dumps(EXAMPLE_SCHEMAS[doc_type]["transform"], indent=2)
else:
    default_item = "{}"
    default_summary = "{}"
    default_transform = '{"type": "identity"}'

st.header("2. Schema (paste what the LLM detection call would return)")
col1, col2, col3 = st.columns(3)
with col1:
    item_schema_text = st.text_area("Item Details schema", value=default_item, height=380)
with col2:
    summary_schema_text = st.text_area("Consolidated Summary schema", value=default_summary, height=380)
with col3:
    transform_text = st.text_area("Join transform", value=default_transform, height=150)
    st.caption("type: identity | strip_prefix | regex_extract")

run = st.button("Run Layer A -> parse -> Layer B", type="primary", disabled=uploaded is None)

if run and uploaded is not None:
    try:
        item_schema = json.loads(item_schema_text)
        summary_schema = json.loads(summary_schema_text)
        transform = json.loads(transform_text)
    except json.JSONDecodeError as e:
        st.error(f"Invalid JSON: {e}")
        st.stop()

    item_df = pd.read_excel(uploaded, sheet_name=item_sheet_name, header=None)
    summary_df = pd.read_excel(uploaded, sheet_name=summary_sheet_name, header=None)

    st.header("3. Layer A — schema validation")
    la_col, lb_col = st.columns(2)

    with la_col:
        st.subheader("Item Details schema")
        ok_item, r_item = validate_and_decide(item_schema, item_df.iloc[:25])
        (st.success if ok_item else st.error)(f"{'PASSED' if ok_item else 'FAILED'}")
        if r_item.failures:
            for f in r_item.failures:
                st.write(f"- {f}")
        with st.expander("stats"):
            st.json(r_item.stats)

    with lb_col:
        st.subheader("Consolidated Summary schema")
        ok_summary, r_summary = validate_and_decide(summary_schema, summary_df.iloc[:15])
        (st.success if ok_summary else st.error)(f"{'PASSED' if ok_summary else 'FAILED'}")
        if r_summary.failures:
            for f in r_summary.failures:
                st.write(f"- {f}")
        with st.expander("stats"):
            st.json(r_summary.stats)

    if not (ok_item and ok_summary):
        st.warning("Fix the schema above before parsing — Layer A caught a real problem, not a false alarm.")
        st.stop()

    st.header("4. Parse (deterministic, no LLM)")
    vouchers = parse_item_details(item_df, item_schema)
    summary_rows = parse_summary(summary_df, summary_schema)
    invoices, report = build_invoices(summary_rows, vouchers, transform)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Summary rows", report.total_summary_rows)
    m2.metric("Join match rate", f"{report.join_match_rate:.1%}")
    m3.metric("Matched to items", report.matched_invoices)
    m4.metric("Reconciled", report.reconciled_invoices)
    m5.metric("Mismatched", report.mismatched_invoices)

    if report.join_match_rate < 0.9:
        st.error("Join match rate below 90% — the transform likely doesn't fit this file. Check the transform JSON.")

    if report.mismatch_detail:
        st.subheader("Mismatched invoices (items sum + round-off vs bill amount)")
        st.dataframe(pd.DataFrame(report.mismatch_detail), use_container_width=True, hide_index=True)

    st.header("5. Result")
    json_str = json.dumps(invoices, indent=2, default=str)
    st.download_button("Download JSON", data=json_str, file_name="invoices.json", mime="application/json")
    with st.expander(f"Preview ({min(5, len(invoices))} of {len(invoices)} invoices)", expanded=False):
        st.json(invoices[:5])
