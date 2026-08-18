"""
Orchestrator: dispatches a mapping + its file to the right parser, based on
mapping["layout_type"]. This is the "which tool do we call" decision the
Prompt 0 classification call is meant to answer (see notes at bottom) — but
the set of tools itself is small and fixed, written once per family, not
once per vendor. A new vendor with an already-known layout_type needs a new
SCHEMA (from Prompt A/B/C), never new code here.

Currently supported layout_type values:

  "two_sheet_joined"          Summary sheet + Item Details sheet, joined by
                               a voucher-number key (Purchase/Sales files).
  "single_sheet_grouped_blocks"
                               One sheet, block-header rows (e.g. Account +
                               GST No. stated once) followed by several
                               detail/line rows with those fields blank,
                               needing forward-fill (GST Summary file).
  "single_sheet_flat"         One sheet, one row = one line item, with
                               voucher-level fields repeated on every row.
                               No sample file seen yet — parser stubbed
                               below per the same pattern, untested against
                               a real export.

Adding a genuinely new family (not just a new vendor) means writing one
new parse_<family>() function here and one new validate_<family>_mapping()
in validate_schema.py — everything else (Layer A/B, the Streamlit harness,
the prompt-generation helpers) stays as-is.
"""
import pandas as pd
from dataclasses import dataclass, field

from ingest import normalize_sheet, forward_fill_blocks
from validate_schema import validate_and_decide
from generic_parser import (
    parse_item_details, parse_summary, build_invoices,
    parse_grouped_blocks, reconcile_grouped_blocks,
    safe_float, safe_str,
)

SUPPORTED_LAYOUTS = {"two_sheet_joined", "single_sheet_grouped_blocks", "single_sheet_flat"}


@dataclass
class RunResult:
    layout_type: str
    invoices: list
    layer_a_ok: bool
    layer_a_failures: list = field(default_factory=list)
    report: object = None  # LayerBReport or GroupedBlocksReport, family-specific


def run_two_sheet_joined(item_df_raw, summary_df_raw, item_mapping, summary_mapping, transform):
    item_sample = item_df_raw.iloc[:25]
    summary_sample = summary_df_raw.iloc[:15]
    ok_item, r_item = validate_and_decide(item_mapping, item_sample)
    ok_summary, r_summary = validate_and_decide(summary_mapping, summary_sample)
    if not (ok_item and ok_summary):
        return RunResult(
            "two_sheet_joined", [], False,
            r_item.failures + r_summary.failures,
        )
    vouchers = parse_item_details(item_df_raw, item_mapping)
    summary_rows = parse_summary(summary_df_raw, summary_mapping)
    voucher_type = summary_mapping.get("voucher_type") or item_mapping.get("voucher_type")
    invoices, report = build_invoices(summary_rows, vouchers, transform, voucher_type=voucher_type)
    return RunResult("two_sheet_joined", invoices, True, [], report)


def run_single_sheet_grouped_blocks(sheet_df_raw, ingest_mapping, grouped_mapping, header_row=None):
    norm = normalize_sheet(sheet_df_raw)
    data_region = norm.iloc[header_row + 1:].reset_index(drop=True) if header_row is not None else norm
    filled = forward_fill_blocks(data_region, ingest_mapping)

    ok, r = validate_and_decide(grouped_mapping, filled.iloc[:60])
    if not ok:
        return RunResult("single_sheet_grouped_blocks", [], False, r.failures)

    invoices = parse_grouped_blocks(filled, grouped_mapping)
    report = reconcile_grouped_blocks(invoices)
    return RunResult("single_sheet_grouped_blocks", invoices, True, [], report)


def parse_single_sheet_flat(df_raw: pd.DataFrame, mapping: dict) -> list:
    """UNTESTED against a real file — written to the same shape as the
    other two parsers so it's ready the moment a matching sample shows up.
    One row = one line item; voucher-level fields (VOUCHERNUMBER, DATE,
    PARTYNAME, ...) repeat on every row belonging to that voucher, so
    grouping is just 'consecutive rows with the same VOUCHERNUMBER'."""
    voucher_fields_map = mapping["voucher_fields_column_map"]
    item_map = mapping["item_row_column_map"]
    extra_fields = mapping.get("extra_fields", {})
    line_id_field = mapping.get("line_identifier_field", "STOCKITEMNAME")
    voucher_col = voucher_fields_map["VOUCHERNUMBER"]
    data_start = mapping["data_start_row"]

    NUMERIC = {
        "ACTUALQTY", "FREEQTY", "RATE", "GSTRATE", "AMOUNT", "DISCOUNT",
        "TAXABLEVALUE", "GSTAMOUNT", "NETAMOUNT", "CGSTAMOUNT", "SGSTAMOUNT",
        "IGSTAMOUNT", "CESSAMOUNT",
    }

    invoices = {}
    order = []
    for i in range(data_start, len(df_raw)):
        row = df_raw.iloc[i]
        voucher_no = row.iloc[voucher_col]
        if pd.isna(voucher_no):
            continue
        voucher_no = str(voucher_no).strip()

        if voucher_no not in invoices:
            invoices[voucher_no] = {"VOUCHERNUMBER": voucher_no, "lines": []}
            for f, idx in voucher_fields_map.items():
                if f != "VOUCHERNUMBER":
                    invoices[voucher_no][f] = safe_str(row.iloc[idx])
            order.append(voucher_no)

        line_id_col = item_map.get(line_id_field)
        if line_id_col is None or pd.isna(row.iloc[line_id_col]):
            continue
        line = {f: (safe_float(row.iloc[idx]) if f in NUMERIC else safe_str(row.iloc[idx]))
                for f, idx in item_map.items()}
        if extra_fields:
            extra = {name: safe_str(row.iloc[idx]) for name, idx in extra_fields.items()
                      if safe_str(row.iloc[idx]) is not None}
            if extra:
                line["extra"] = extra
        invoices[voucher_no]["lines"].append(line)

    return [invoices[k] for k in order]
