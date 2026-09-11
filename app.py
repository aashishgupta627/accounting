"""
Unified test harness — one app, three layout families, dispatched through
orchestrator.py exactly the way production would, plus a GST Sales
Reconciliation tab (Books <-> GSTR-1 portal).

Run with: streamlit run app.py
"""

import io
import json
import os
import tempfile

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

# Mappings live here so both app.py and the CLI (run_reconciliation.py)
# import the same source of truth.
from app_examples import (
    EXAMPLES,
    SALES_TWO_SHEET_EXAMPLE,
    GST_SUMMARY_DAILY_EXAMPLE,
)

# GST Sales Reconciliation dependencies.
from adapter_books import invoices_to_canonical
from reconcile import reconcile, ReconciliationConfig
from export_excel import export as export_reconciliation
import parse_portal_json
import parse_portal_excel

st.set_page_config(page_title="Invoice extractor — mapping test harness", layout="wide")
st.title("Invoice extractor — mapping test harness")
st.caption(
    "One fixed canonical schema, three layout families, per-file mappings pasted in "
    "(exactly what an LLM detection call would return) until that call is wired in. "
    "Plus a GST Sales Reconciliation tab (Books ↔ GSTR-1 portal)."
)

# A reversing voucher (Credit Note / Debit Note) runs through the same
# Dr/Cr engine as the voucher type it reverses -- tally_export.py flips
# Dr/Cr from the signed BILLAMOUNT/tax_breakup values, it doesn't need
# its own code path. This just says which engine to dispatch through.
_REVERSAL_DISPATCH_TYPE = {"Credit Note": "Sales", "Debit Note": "Purchase"}


# ---------------------------------------------------------------------------
# Helper: display Tally export results for a single mode (B2B or B2C)
# ---------------------------------------------------------------------------

def _display_export_results(result: tuple, mode: str, voucher_type_label: str, key_prefix: str,
                             reversal_label: str = None):
    """reversal_label: e.g. 'Credit Note' when this export is a combined
    Sales + Credit Note run, or 'Debit Note' for Purchase + Debit Note.
    When given, the reversal-type rows (identified by their own "Voucher
    Type Name" column -- no separate invoice list needed) are written to
    both the main sheet (so Tally gets one file to import) *and* a second
    sheet of the same workbook (so a reviewer can see just the
    reversals) -- see request point 1."""
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

        reversal_df = None
        if reversal_label and "Voucher Type Name" in df_out.columns:
            reversal_df = df_out[df_out["Voucher Type Name"] == reversal_label]
            if reversal_df.empty:
                reversal_df = None

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_out.to_excel(writer, sheet_name="Accounting Voucher", index=False)
            if reversal_df is not None:
                # Excel sheet names are capped at 31 characters.
                reversal_df.to_excel(writer, sheet_name=reversal_label[:31], index=False)

        st.download_button(
            f"Download {mode} Tally {voucher_type_label} vouchers (.xlsx)",
            data=buf.getvalue(),
            file_name=f"Tally{voucher_type_label.replace(' ', '').replace('+', '')}Vouchers_{mode}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key_prefix}_dl_{mode}",
        )

        if reversal_df is not None:
            with st.expander(f"{mode}: {reversal_label} rows only ({len(reversal_df)})"):
                st.caption(
                    f"Same rows as in the '{reversal_label}' sheet of the download above — "
                    f"shown here too for a quick on-screen check."
                )
                st.dataframe(reversal_df, use_container_width=True, hide_index=True)
    else:
        st.info(f"No {mode} {voucher_type_label} vouchers to export.")


# ---------------------------------------------------------------------------
# Helper: HSN summary + Tally export sections, shared by every layout family
# now that all three produce the same canonical invoice shape (items,
# VOUCHERTYPE, BILLAMOUNT, tax_breakup, is_validated).
# ---------------------------------------------------------------------------

def _render_downstream_exports(invoices: list, voucher_type: str, key_prefix: str,
                                home_state_is_ut: bool, round_off_ledger_name: str,
                                display_label: str = None, reversal_label: str = None):
    """voucher_type: internal dispatch value, must be 'Sales' or 'Purchase'
    (selects which generate_tally_*_export function runs — Tally export
    only has these two Dr/Cr shapes). `invoices` is expected to already be
    the *combined* list for this side (e.g. Sales + Credit Note invoices
    together) — a Credit/Debit Note doesn't get its own export call, it
    rides through in the same generate_tally_*_export() run as the
    voucher type it reverses, since tally_export.py flips Dr/Cr from the
    signed BILLAMOUNT/tax_breakup values already on each invoice. This is
    what puts Sales and Credit Note (or Purchase and Debit Note) vouchers
    into one Tally file, per request point 1.

    display_label: what the user sees in captions/buttons/filenames.
    reversal_label: e.g. 'Credit Note'/'Debit Note' — passed through to
    _display_export_results so the reversal-type rows also land on their
    own sheet in the same workbook."""
    display_label = display_label or voucher_type

    st.header("4.5 HSN Summary Reports")
    st.caption(
        "Generate HSN-wise summary reports for B2B and B2C sales. "
        "Only invoices with is_validated = True are included. "
        "Reports include validation to ensure totals match invoice data."
    )

    if voucher_type == "Sales":
        if st.button("Generate HSN Summaries", type="primary", key=f"{key_prefix}_gen_hsn_btn"):
            summaries = generate_all_hsn_summaries(invoices, voucher_type, validate=True)
            st.session_state[f"{key_prefix}_hsn_summaries"] = summaries

        if st.session_state.get(f"{key_prefix}_hsn_summaries") is not None:
            summaries = st.session_state[f"{key_prefix}_hsn_summaries"]

            for mode, (df, validation_report) in summaries.items():
                st.subheader(f"{mode} HSN Summary")

                if df.empty:
                    st.info(f"No {mode} data available")
                    continue

                if validation_report:
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Total Invoices", validation_report.total_invoices)
                    col2.metric("Invoice Total", f"₹{validation_report.total_invoice_value:,.2f}")
                    col3.metric("HSN Total", f"₹{validation_report.total_hsn_value:,.2f}")
                    col4.metric("Difference", f"₹{validation_report.difference:,.2f}")

                    if validation_report.is_valid:
                        st.success("✅ Validation PASSED: HSN totals match invoice totals")
                    else:
                        st.error(
                            f"❌ Validation FAILED: HSN totals do not match invoice totals "
                            f"(difference: ₹{validation_report.difference:,.2f})"
                        )

                    if validation_report.mismatched_invoices:
                        with st.expander(f"⚠️ Mismatched invoices ({len(validation_report.mismatched_invoices)})"):
                            st.dataframe(
                                pd.DataFrame(validation_report.mismatched_invoices),
                                use_container_width=True, hide_index=True,
                            )

                    if validation_report.missing_hsn_invoices:
                        with st.expander(f"⚠️ Invoices without HSN codes ({len(validation_report.missing_hsn_invoices)})"):
                            st.dataframe(
                                pd.DataFrame(validation_report.missing_hsn_invoices),
                                use_container_width=True, hide_index=True,
                            )

                total_value = df["Total Value"].sum()
                total_taxable = df["Taxable Value"].sum()
                total_tax = (
                    df["Integrated Tax Amount"].sum()
                    + df["Central Tax Amount"].sum()
                    + df["State/UT Tax Amount"].sum()
                )

                c1, c2, c3 = st.columns(3)
                c1.metric(f"{mode} - Total Value", f"₹{total_value:,.2f}")
                c2.metric(f"{mode} - Taxable Value", f"₹{total_taxable:,.2f}")
                c3.metric(f"{mode} - Total Tax", f"₹{total_tax:,.2f}")

                st.dataframe(df, use_container_width=True, hide_index=True)

                col1, col2 = st.columns(2)

                csv = df.to_csv(index=False)
                col1.download_button(
                    f"Download {mode} HSN Summary (.csv)",
                    data=csv,
                    file_name=f"hsn_{mode.lower()}.csv",
                    mime="text/csv",
                    key=f"{key_prefix}_dl_hsn_{mode}",
                )

                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                    df.to_excel(writer, sheet_name=f"HSN_{mode}", index=False)
                col2.download_button(
                    f"Download {mode} HSN Summary (.xlsx)",
                    data=buf.getvalue(),
                    file_name=f"hsn_{mode.lower()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"{key_prefix}_dl_hsn_excel_{mode}",
                )
    else:
        st.info(f"HSN summary generation is currently only implemented for Sales vouchers. Voucher type: {voucher_type}")

    st.header("5. Tally Voucher Export")

    config = TallyExportConfig(
        home_state_is_ut=home_state_is_ut,
        round_off_ledger_name=round_off_ledger_name,
    )

    if voucher_type == "Sales":
        st.caption(
            f"Splits invoices into B2B / B2C (by whether PARTYGSTIN is present), builds "
            f"Tally-importable 'Accounting Voucher' rows for these {display_label} entries. "
            f"Dr: Party, Cr: Sales + Output tax ledgers (flipped for a Credit Note, whose "
            f"BILLAMOUNT/tax_breakup arrive negative). Sales and Credit Note vouchers are "
            f"written to the same 'Accounting Voucher' sheet for Tally import, with Credit "
            f"Note rows repeated on their own sheet in the same file for review. "
            f"B2C rows use a generic 'Customer' ledger name instead of the individual party. "
            f"Only invoices with is_validated = True are exported."
        )

        export_clicked = st.button(f"Prepare Tally {display_label} vouchers", type="primary", key=f"{key_prefix}_prep_tally_sales_btn")
        if export_clicked:
            st.session_state[f"{key_prefix}_tally_export_results"] = generate_tally_sales_export(invoices, config)

        tally_results = st.session_state.get(f"{key_prefix}_tally_export_results")
        if tally_results is not None:
            for mode in ("B2B", "B2C"):
                _display_export_results(tally_results[mode], mode, display_label, key_prefix, reversal_label=reversal_label)

    elif voucher_type == "Purchase":
        st.caption(
            "Generates Tally-importable 'Accounting Voucher' rows for purchase invoices. "
            "B2B: Dr = Purchase ledger + Input tax ledgers, Cr = Supplier (full amount). "
            "B2C: Dr = Purchase GST 0% (full amount), Cr = Supplier (full amount). "
            "Flipped for a Debit Note, whose BILLAMOUNT/tax_breakup arrive negative. "
            "Purchase and Debit Note vouchers are written to the same 'Accounting Voucher' "
            "sheet for Tally import, with Debit Note rows repeated on their own sheet in the "
            "same file for review. B2C rows use a generic 'Customer' ledger name instead of "
            "the individual party. Only invoices with is_validated = True are exported."
        )

        export_clicked = st.button(f"Prepare Tally {display_label} vouchers", type="primary", key=f"{key_prefix}_prep_tally_purchase_btn")
        if export_clicked:
            st.session_state[f"{key_prefix}_tally_export_results"] = generate_tally_purchase_export(invoices, config)

        tally_results = st.session_state.get(f"{key_prefix}_tally_export_results")
        if tally_results is not None:
            for mode in ("B2B", "B2C"):
                _display_export_results(tally_results[mode], mode, display_label, key_prefix, reversal_label=reversal_label)

    else:
        st.info(f"Tally export not yet implemented for voucher_type={voucher_type!r}")


# ---------------------------------------------------------------------------
# Helper: extraction-stage skip report (CANCEL ledger blocks etc.)
# ---------------------------------------------------------------------------

def _render_skipped_invoices(skipped: list, key_prefix: str):
    """Extraction-stage skip report: CANCEL ledger blocks etc. that were
    dropped at parse time so they never reach Tally / HSN / reconciliation.
    Surfaced here so the user sees exactly what was excluded and why."""
    if not skipped:
        return
    total_value = sum(s.get("doc_value", 0.0) for s in skipped)
    st.warning(
        f"⚠️ {len(skipped)} voucher(s) skipped at extraction "
        f"(total value ₹{total_value:,.2f}). These are excluded from the "
        f"JSON, Tally export, HSN summary, and any downstream reconciliation."
    )
    with st.expander(f"Skipped vouchers ({len(skipped)})", expanded=False):
        st.dataframe(
            pd.DataFrame(skipped),
            use_container_width=True, hide_index=True,
        )


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_extract, tab_reconcile = st.tabs(["Extraction", "GST Reconciliation (Sales)"])


# ===========================================================================
# TAB 1 — EXTRACTION (existing behavior preserved)
# ===========================================================================

with tab_extract:
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
        st.caption("Used only by the Tally voucher export section (section 5 below).")
        home_state_is_ut = st.radio(
            "Your registered state is a...",
            ["Union Territory (UTGST)", "State (SGST)"],
            index=0,
            help="Controls whether intrastate tax is posted to 'UTGST' or 'SGST' ledgers.",
        ) == "Union Territory (UTGST)"
        round_off_ledger_name = st.text_input("Round Off ledger name", value="Round Off")

    example = EXAMPLES.get(example_choice)

    st.header("2. Mapping (paste what an LLM detection call would return)")

    # =======================================================================
    # TWO_SHEET_JOINED
    # =======================================================================
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
            st.session_state.pop("ts_hsn_summaries", None)
            st.session_state.pop("ts_tally_export_results", None)

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

            # Extraction-stage skip report (CANCEL ledger blocks etc.).
            _render_skipped_invoices(res.skipped_invoices, "ts")

            report = res.report
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Summary rows", report.total_summary_rows)
            m2.metric("Join match rate", f"{report.join_match_rate:.1%}")
            m3.metric("Matched to items", report.matched_invoices)
            m4.metric("Reconciled", report.reconciled_invoices)
            m5.metric("Mismatched", report.mismatched_invoices)

            no_items = [inv for inv in res.invoices if not inv.get("items")]
            if no_items:
                st.info(
                    f"{len(no_items)} invoice(s) have no matched items — e.g. a 'PR/...' "
                    f"purchase-return row recorded only in the Consolidated Summary sheet. "
                    f"These are still classified via BILLAMOUNT's sign (see the by-type sections "
                    f"below — a negative-total Purchase row now resolves to VOUCHERTYPE = "
                    f"'Debit Note') and validated against the summary row's own tax_breakup "
                    f"instead of being left permanently unvalidated; is_validated is null only "
                    f"if that tax_breakup is also missing. Review below to confirm."
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

            if report.voucher_type_flags:
                with st.expander(f"⚠️ VOUCHERTYPE sign/pattern flags ({len(report.voucher_type_flags)})"):
                    st.caption(
                        "Invoices where the BILLAMOUNT sign disagreed with the expected VOUCHERTYPE, "
                        "or where tax_breakup buckets don't all share BILLAMOUNT's sign. Review before "
                        "exporting to Tally — see generic_parser.resolve_voucher_type."
                    )
                    st.dataframe(pd.DataFrame(report.voucher_type_flags), use_container_width=True, hide_index=True)

            st.header("4. Result")
            payload = {
                "invoices": res.invoices,
                "_skipped": res.skipped_invoices,
            }
            json_str = json.dumps(payload, indent=2, default=str)
            st.download_button("Download JSON", data=json_str, file_name="invoices.json", mime="application/json", key="dl_json")
            with st.expander(f"Preview ({min(5, len(res.invoices))} of {len(res.invoices)})"):
                st.json(res.invoices[:5])

            by_type = {}
            for inv in res.invoices:
                by_type.setdefault(inv.get("VOUCHERTYPE") or "Unknown", []).append(inv)

            _PREVIEW_COLS = ["VOUCHERNUMBER", "PARTYNAME", "BILLAMOUNT", "is_validated"]
            for vt, invs in by_type.items():
                with st.expander(f"{vt} — {len(invs)} invoice(s)", expanded=False):
                    st.dataframe(
                        pd.DataFrame([{c: inv.get(c) for c in _PREVIEW_COLS} for inv in invs]),
                        use_container_width=True, hide_index=True,
                    )

            sales_side = by_type.get("Sales", []) + by_type.get("Credit Note", [])
            purchase_side = by_type.get("Purchase", []) + by_type.get("Debit Note", [])
            other_types = {vt: invs for vt, invs in by_type.items()
                           if vt not in ("Sales", "Credit Note", "Purchase", "Debit Note")}

            if sales_side:
                st.markdown("---")
                _render_downstream_exports(
                    sales_side, "Sales", key_prefix="ts_sales_side",
                    home_state_is_ut=home_state_is_ut, round_off_ledger_name=round_off_ledger_name,
                    display_label="Sales" + (" + Credit Note" if by_type.get("Credit Note") else ""),
                    reversal_label="Credit Note",
                )
            if purchase_side:
                st.markdown("---")
                _render_downstream_exports(
                    purchase_side, "Purchase", key_prefix="ts_purchase_side",
                    home_state_is_ut=home_state_is_ut, round_off_ledger_name=round_off_ledger_name,
                    display_label="Purchase" + (" + Debit Note" if by_type.get("Debit Note") else ""),
                    reversal_label="Debit Note",
                )
            for vt, invs in other_types.items():
                st.markdown("---")
                st.info(f"VOUCHERTYPE {vt!r} ({len(invs)} invoice(s)) has no matching Tally export engine.")

    # =======================================================================
    # SINGLE_SHEET_GROUPED_BLOCKS
    # =======================================================================
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
            st.caption(
                "Optional extras beyond column_map: rate_bucket_columns (derive GSTRATE from "
                "which rate-bucket column is non-zero), sign_flip_fields (negate a numeric "
                "field, e.g. when qty sign is inverted vs. accounting convention), "
                "voucher_type_rule (derive VOUCHERTYPE from a VOUCHERNUMBER pattern, optionally "
                "cross-checked against BILLAMOUNT's sign via voucher_type_rule.sign_base_type — "
                "'Sales' or 'Purchase')."
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

            st.session_state["gb_res"] = res
            st.session_state["gb_voucher_type_rule"] = grouped_mapping.get("voucher_type_rule")
            st.session_state.pop("gb_hsn_summaries", None)
            st.session_state.pop("gb_tally_export_results", None)

        if st.session_state.get("gb_res") is not None:
            res = st.session_state["gb_res"]

            st.header("3. Layer A")
            if not res.layer_a_ok:
                st.error("FAILED")
                for f in res.layer_a_failures:
                    st.write(f"- {f}")
                st.stop()
            st.success("PASSED")

            # Extraction-stage skip report.
            _render_skipped_invoices(res.skipped_invoices, "gb")

            report = res.report
            m1, m2, m3 = st.columns(3)
            m1.metric("Invoices parsed", report.total_invoices)
            m2.metric("Reconciled", report.reconciled_invoices)
            m3.metric("Mismatched", report.mismatched_invoices)

            if report.mismatch_detail:
                st.subheader("Mismatched lines (taxable + tax vs stated amount)")
                st.dataframe(pd.DataFrame(report.mismatch_detail), use_container_width=True, hide_index=True)

            voucher_types = sorted({inv.get("VOUCHERTYPE") for inv in res.invoices if inv.get("VOUCHERTYPE")})
            if voucher_types:
                vt_counts = pd.Series([inv.get("VOUCHERTYPE") for inv in res.invoices]).value_counts()
                st.caption("Voucher types: " + ", ".join(f"{k} ({v})" for k, v in vt_counts.items()))

            st.header("4. Result")
            payload = {
                "invoices": res.invoices,
                "_skipped": res.skipped_invoices,
            }
            json_str = json.dumps(payload, indent=2, default=str)
            st.download_button("Download JSON", data=json_str, file_name="invoices.json", mime="application/json", key="gb_dl_json")
            with st.expander(f"Preview ({min(5, len(res.invoices))} of {len(res.invoices)})"):
                st.json(res.invoices[:5])

            multi_item = [i for i in res.invoices if len(i.get("items", [])) > 1]
            if multi_item:
                with st.expander(f"Multi-HSN invoices ({len(multi_item)} found) — grouping sanity check"):
                    st.caption(
                        "Each HSN stays a separate item even when several share the same GST "
                        "rate within one invoice; tax_breakup aggregates by rate only."
                    )
                    st.json(multi_item[:3])

            flagged = [inv for inv in res.invoices if (inv.get("extra") or {}).get("voucher_type_flag")]
            if flagged:
                with st.expander(f"⚠️ VOUCHERTYPE sign/pattern flags ({len(flagged)})"):
                    st.caption(
                        "Invoices where the VOUCHERNUMBER pattern rule and BILLAMOUNT's sign "
                        "disagreed on VOUCHERTYPE, or where tax_breakup buckets don't all share "
                        "BILLAMOUNT's sign. Review before exporting to Tally."
                    )
                    st.dataframe(
                        pd.DataFrame([
                            {
                                "VOUCHERNUMBER": i["VOUCHERNUMBER"],
                                "VOUCHERTYPE": i["VOUCHERTYPE"],
                                "BILLAMOUNT": i["BILLAMOUNT"],
                                "flag": i["extra"]["voucher_type_flag"],
                            }
                            for i in flagged
                        ]),
                        use_container_width=True, hide_index=True,
                    )

            by_type = {}
            for inv in res.invoices:
                by_type.setdefault(inv.get("VOUCHERTYPE") or "Unknown", []).append(inv)

            _PREVIEW_COLS = ["VOUCHERNUMBER", "PARTYNAME", "BILLAMOUNT", "is_validated"]
            for vt, invs in by_type.items():
                with st.expander(f"{vt} — {len(invs)} invoice(s)", expanded=False):
                    st.dataframe(
                        pd.DataFrame([{c: inv.get(c) for c in _PREVIEW_COLS} for inv in invs]),
                        use_container_width=True, hide_index=True,
                    )

            sales_side = by_type.get("Sales", []) + by_type.get("Credit Note", [])
            purchase_side = by_type.get("Purchase", []) + by_type.get("Debit Note", [])
            other_types = {vt: invs for vt, invs in by_type.items()
                           if vt not in ("Sales", "Credit Note", "Purchase", "Debit Note")}

            if sales_side:
                st.markdown("---")
                _render_downstream_exports(
                    sales_side, "Sales", key_prefix="gb_sales_side",
                    home_state_is_ut=home_state_is_ut, round_off_ledger_name=round_off_ledger_name,
                    display_label="Sales" + (" + Credit Note" if by_type.get("Credit Note") else ""),
                    reversal_label="Credit Note",
                )
            if purchase_side:
                st.markdown("---")
                _render_downstream_exports(
                    purchase_side, "Purchase", key_prefix="gb_purchase_side",
                    home_state_is_ut=home_state_is_ut, round_off_ledger_name=round_off_ledger_name,
                    display_label="Purchase" + (" + Debit Note" if by_type.get("Debit Note") else ""),
                    reversal_label="Debit Note",
                )
            for vt, invs in other_types.items():
                st.markdown("---")
                st.info(f"VOUCHERTYPE {vt!r} ({len(invs)} invoice(s)) has no matching Tally export engine.")

    # =======================================================================
    # SINGLE_SHEET_FLAT
    # =======================================================================
    else:
        st.info(
            "No sample file confirms this layout yet. One row = one line item, with "
            "voucher-level fields (VOUCHERNUMBER, VOUCHERDATE, PARTYNAME, ...) repeated on every "
            "row belonging to that voucher. The parser (orchestrator.parse_single_sheet_flat) "
            "is written to the same pattern as the other two layouts but UNTESTED against a "
            "real export — paste a mapping below once you have a candidate file."
        )
        sheet_name = st.text_input("Sheet name", value="Sheet1")
        data_start_row = st.number_input("Data start row (0-based)", min_value=0, value=1)
        flat_text = st.text_area(
            "Flat-sheet mapping", height=300,
            value=json.dumps({
                "voucher_fields_column_map": {"VOUCHERNUMBER": 0, "VOUCHERDATE": 1, "PARTYNAME": 2},
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


# ===========================================================================
# TAB 2 — GST RECONCILIATION (SALES)
# ===========================================================================

with tab_reconcile:
    st.header("GST Sales Reconciliation — Books ↔ GSTR-1 Portal")
    st.caption(
        "Books side goes through the same extraction pipeline as the Extraction tab, "
        "using the baked-in example mapping for whichever layout your books file uses. "
        "CANCEL ledger blocks (no GSTIN) are dropped at extraction time and reported below "
        "— they never reach the reconciler. Portal side accepts either the GSTR-1 JSON "
        "(returns_*.json) or the portal's auto-populated Excel export."
    )

    rc1, rc2 = st.columns(2)
    with rc1:
        st.subheader("Books")
        books_file = st.file_uploader(
            "Books export (GST Summary Daily, or Sales two-sheet)",
            type=["xlsx", "xls"], key="recon_books",
        )
        books_layout = st.selectbox(
            "Books layout",
            ["single_sheet_grouped_blocks", "two_sheet_joined"],
            help=(
                "single_sheet_grouped_blocks: GST Summary Daily (party blocks, "
                "HSN-grained rows aggregated to invoice level).\n"
                "two_sheet_joined: Sales_July-style workbook with Item Details + "
                "Consolidated Summary sheets."
            ),
            key="recon_books_layout",
        )
    with rc2:
        st.subheader("Portal")
        portal_file = st.file_uploader(
            "GSTR-1 export (JSON or portal auto-populated Excel)",
            type=["json", "xlsx", "xls"], key="recon_portal",
        )

    oc1, oc2 = st.columns(2)
    value_tol = oc1.number_input("Match tolerance (₹)", min_value=0.0, value=1.0, step=0.5, key="recon_val_tol")
    date_tol = oc2.number_input("Date tolerance (days, fallback matching)", min_value=0, value=3, step=1, key="recon_date_tol")

    run_recon = st.button(
        "Run reconciliation", type="primary",
        disabled=(books_file is None or portal_file is None),
        key="recon_run",
    )

    if run_recon and books_file is not None and portal_file is not None:
        # --- Books -> extraction pipeline -> adapter ---
        with st.spinner("Parsing books through extraction pipeline…"):
            if books_layout == "single_sheet_grouped_blocks":
                ex = GST_SUMMARY_DAILY_EXAMPLE
                raw = pd.read_excel(books_file, sheet_name=ex["sheet_name"], header=None)
                res = run_single_sheet_grouped_blocks(
                    raw, ex["ingest_mapping"], ex["grouped_mapping"], header_row=ex["header_row"],
                )
            else:
                ex = SALES_TWO_SHEET_EXAMPLE
                item_df = pd.read_excel(books_file, sheet_name=ex["item_sheet_name"], header=None)
                summary_df = pd.read_excel(books_file, sheet_name=ex["summary_sheet_name"], header=None)
                res = run_two_sheet_joined(
                    item_df, summary_df,
                    ex["item_mapping"], ex["summary_mapping"], ex["transform"],
                )

            if not res.layer_a_ok:
                st.error("Books Layer A validation failed:")
                for f in res.layer_a_failures:
                    st.write(f"- {f}")
                st.stop()

            books_docs = invoices_to_canonical(res.invoices)

        # --- Portal -> JSON or Excel ---
        with st.spinner("Parsing portal export…"):
            ext = os.path.splitext(portal_file.name)[1].lower()
            if ext == ".json":
                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                    tf.write(portal_file.getvalue())
                    tmp_path = tf.name
                portal_docs, b2c_totals, meta = parse_portal_json.parse_all(tmp_path)
            else:
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
                    tf.write(portal_file.getvalue())
                    tmp_path = tf.name
                portal_docs, b2c_totals, meta = parse_portal_excel.parse_all(tmp_path)

        config = ReconciliationConfig(
            value_tolerance=float(value_tol),
            date_tolerance_days=int(date_tol),
        )
        result = reconcile(books_docs, portal_docs, config)

        # Persist for re-render on any subsequent widget interaction.
        st.session_state["recon_result"] = result
        st.session_state["recon_config"] = config
        st.session_state["recon_meta"] = meta
        st.session_state["recon_b2c"] = b2c_totals
        st.session_state["recon_books_skipped"] = res.skipped_invoices

    # --- Render (from session_state, so download button survives re-runs) ---
    if st.session_state.get("recon_result") is not None:
        result = st.session_state["recon_result"]
        config = st.session_state["recon_config"]
        meta = st.session_state["recon_meta"]
        b2c = st.session_state["recon_b2c"]
        skipped = st.session_state.get("recon_books_skipped", [])

        st.markdown("---")
        st.subheader("Books-side extraction skips")
        _render_skipped_invoices(skipped, "recon")

        st.subheader("Reconciliation summary")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Exact Match", len(result["exact_match"]))
        c2.metric("Partial Match", len(result["partial_match"]))
        c3.metric("Books Only", len(result["books_only"]))
        c4.metric("Portal Only", len(result["portal_only"]))

        def _sum_val(rows, side):
            return sum(getattr(r, side).doc_value for r in rows if getattr(r, side))

        v1, v2, v3, v4 = st.columns(4)
        v1.metric("Exact Match ₹", f"₹{_sum_val(result['exact_match'], 'books_doc'):,.2f}")
        v2.metric("Partial Match ₹", f"₹{_sum_val(result['partial_match'], 'books_doc'):,.2f}")
        v3.metric("Books Only ₹", f"₹{_sum_val(result['books_only'], 'books_doc'):,.2f}")
        v4.metric("Portal Only ₹", f"₹{_sum_val(result['portal_only'], 'portal_doc'):,.2f}")

        if meta:
            st.caption(
                f"Portal meta — GSTIN: {meta.get('gstin') or '—'} | "
                f"Period: {meta.get('period') or '—'} | "
                f"Filing type: {meta.get('filing_type') or '—'}"
            )
        if b2c:
            st.info(
                f"Portal B2C totals (aggregate only, informational): "
                f"Taxable ₹{b2c.get('taxable_value', 0):,.2f} | "
                f"IGST ₹{b2c.get('igst', 0):,.2f} | "
                f"CGST ₹{b2c.get('cgst', 0):,.2f} | "
                f"SGST ₹{b2c.get('sgst', 0):,.2f} | "
                f"CESS ₹{b2c.get('cess', 0):,.2f}"
            )

        if result["partial_match"]:
            with st.expander(f"Partial Match detail ({len(result['partial_match'])})"):
                st.dataframe(pd.DataFrame([
                    {
                        "Books Doc No": r.books_doc.doc_no_raw if r.books_doc else "",
                        "Portal Doc No": r.portal_doc.doc_no_raw if r.portal_doc else "",
                        "GSTIN": (r.books_doc or r.portal_doc).gstin,
                        "Fields": ", ".join(m["field"] for m in r.mismatch_fields),
                        "Note": r.mismatch_note,
                    }
                    for r in result["partial_match"]
                ]), use_container_width=True, hide_index=True)

        # Build the workbook in a temp path, then read into memory for the
        # download button (openpyxl writes to a path, not a file-like).
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
            tmp_out = tf.name
        export_reconciliation(result, config, tmp_out, meta=meta)
        with open(tmp_out, "rb") as fh:
            buf = io.BytesIO(fh.read())

        st.download_button(
            "Download reconciliation workbook (.xlsx)",
            data=buf.getvalue(),
            file_name="gst_sales_reconciliation.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="recon_dl",
        )
