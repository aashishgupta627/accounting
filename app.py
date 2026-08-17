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
from sheet_sampler import serialize_raw_grid, sample_join_keys, build_prompt_a, build_prompt_b, build_prompt_c

# Kept as plain strings here (mirrors schema_detection_prompts.md) so the app
# can assemble copy-ready prompt text without parsing markdown at runtime.
PROMPT_A_SYSTEM = """You map raw spreadsheet grids to a fixed schema for accounting-voucher import.
You will be shown the first ~20-25 rows of an Excel sheet as (row, col): value
pairs. The sheet mixes voucher header rows with inventory-entry (item) rows.

You must map columns to this CLOSED set of canonical field names only —
never invent a field name, never use one not in this list:

Voucher-level: DATE, VOUCHERNUMBER, PARTYNAME, PARTYGSTIN, NARRATION,
ROUNDOFFAMOUNT, BILLAMOUNT

Inventory-entry: STOCKITEMNAME, BATCHNAME, EXPIRYDATE, ACTUALQTY, FREEQTY,
RATE, GSTRATE, AMOUNT, DISCOUNT, TAXABLEVALUE, GSTAMOUNT, HSNCODE, NETAMOUNT

If a canonical field has no matching column in this sheet, omit it from the
map rather than guessing.

Voucher-level fields on an invoice-header row can appear in TWO different
ways — check the raw grid carefully for which one applies:
1. Separate columns — each field sits in its own column index. Use "fields":
   {"CANONICAL_NAME": col_index, ...}.
2. One merged cell — Excel merged cells often show up as a single long
   string in one column with every other column NaN on that row. If you see
   this pattern, use "blob_extract" instead: {"CANONICAL_NAME": "<regex with
   a (?P<v>...) named group>", ...}. Never use both on the same marker.

IMPORTANT for merged-cell blobs: leading/trailing whitespace and internal
spacing inside the blob is unpredictable (it comes from padded Excel
columns being concatenated). Never anchor a pattern to the start of the
string with "^" unless you also allow for leading whitespace (e.g. use
"^\\s*..." not "^..."). Prefer the narrowest pattern that uniquely
identifies the row — usually just the voucher-number token itself (e.g.
"PB/\\d+") — over a pattern that tries to match the whole line's shape.
The same applies inside blob_extract sub-patterns: extract each field with
a minimal, unanchored regex rather than a full-line template.

Respond with JSON only, matching the schema given."""

PROMPT_A_USER_TEMPLATE = """Sheet name: {sheet_name}
Paired summary sheet: {summary_sheet_name}

Raw grid (row_index, col_index: value), rows 0-{n}:
{raw_grid_dump}

Return JSON exactly in this shape:
{{
  "sheet_type": "item_details",
  "header_rows": [<row indices that are column-header labels>],
  "data_start_row": <first row index containing real voucher/item data>,
  "invoice_block_marker": {{
    "column": <col index that holds the voucher number, or the single blob column>,
    "pattern": "<regex that matches only voucher-number values in that column>",
    "fields": {{"<canonical voucher field>": <col index>, ...}},
    "blob_extract": {{"<canonical voucher field>": "<regex with (?P<v>...) group>", ...}}
  }},
  "item_row_column_map": {{"<canonical inventory field>": <col index>, ...}},
  "skip_row_rules": [{{"column": <idx>, "equals": "<literal value to skip, e.g. TOTAL:>"}}],
  "confidence": <0-1 float, your own estimate of how sure you are>
}}"""

PROMPT_B_USER_TEMPLATE = """Sheet name: {sheet_name}
Raw grid (row_index, col_index: value), rows 0-{n}:
{raw_grid_dump}

Map columns to this CLOSED set only: VOUCHERNUMBER, PARTYNAME, PARTYGSTIN,
BILLAMOUNT, ROUNDOFFAMOUNT, STATECODE. Omit any field with no matching column.

Return JSON exactly in this shape:
{{
  "sheet_type": "consolidated_summary",
  "header_row": <row index>,
  "column_map": {{"<canonical field>": <col index>, ...}},
  "confidence": <0-1 float>
}}"""

PROMPT_C_USER_TEMPLATE = """Sample VOUCHERNUMBER values from Consolidated Summary: {summary_keys}
Sample voucher-number values from Item Details: {detail_keys}

Determine how a Consolidated Summary key maps to its Item Details counterpart.
Respond with JSON using ONLY one of these transform types:

{{"type": "identity"}}
{{"type": "strip_prefix", "prefix": "<string>"}}
{{"type": "regex_extract", "from": "summary", "pattern": "<regex with one capture group>", "template": "<output template using {{1}} for the captured group>"}}

Return only the JSON object, nothing else."""

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

if uploaded is not None:
    with st.expander("1a. Generate detection prompts (copy into your LLM call, paste the JSON result below)", expanded=False):
        item_df_preview = pd.read_excel(uploaded, sheet_name=item_sheet_name, header=None)
        summary_df_preview = pd.read_excel(uploaded, sheet_name=summary_sheet_name, header=None)

        n_rows_item = st.slider("Item Details rows to sample", 10, 40, 25)
        n_rows_summary = st.slider("Summary rows to sample", 5, 30, 15)

        prompt_a = build_prompt_a(
            item_sheet_name, summary_sheet_name, item_df_preview,
            PROMPT_A_SYSTEM, PROMPT_A_USER_TEMPLATE, n_rows=n_rows_item,
        )
        prompt_b = build_prompt_b(summary_sheet_name, summary_df_preview, PROMPT_B_USER_TEMPLATE, n_rows=n_rows_summary)

        tab1, tab2, tab3 = st.tabs(["Prompt A — Item Details", "Prompt B — Summary", "Prompt C — join transform"])
        with tab1:
            st.caption("System prompt")
            st.code(prompt_a["system"], language="text")
            st.caption("User prompt")
            st.code(prompt_a["user"], language="text")
        with tab2:
            st.caption("User prompt")
            st.code(prompt_b["user"], language="text")
        with tab3:
            st.caption("Column + header row for the voucher number in each sheet (used only to sample keys)")
            cc1, cc2, cc3 = st.columns(3)
            summary_key_col = cc1.number_input("Summary voucher-number column", min_value=0, value=1, key="ck_summary")
            detail_key_col = cc2.number_input("Item Details voucher-number column", min_value=0, value=1, key="ck_detail")
            header_rows_to_skip = cc3.number_input("Rows to skip (header rows)", min_value=0, value=1, key="ck_skip")
            s_keys = sample_join_keys(summary_df_preview.iloc[header_rows_to_skip:, summary_key_col], n=8)
            d_keys = sample_join_keys(item_df_preview.iloc[header_rows_to_skip:, detail_key_col], n=8)
            prompt_c = build_prompt_c(s_keys, d_keys, PROMPT_C_USER_TEMPLATE)
            st.code(prompt_c["user"], language="text")

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
