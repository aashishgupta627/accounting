"""
Schema-driven deterministic parser.

Takes (item_details schema, summary schema, join transform) + the two raw
DataFrames, and produces the same kind of nested invoice JSON the original
script produced — but with every column position and the join rule read
from the schema instead of hardcoded, so a new vendor layout only requires
a new schema, not a code change.

No LLM calls happen here. This module is intentionally boring.
"""
import pandas as pd
from dataclasses import dataclass, field
from validate_schema import apply_transform, extract_blob_fields, validate_join_transform


def safe_float(value):
    if pd.isna(value):
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def safe_str(value):
    if pd.isna(value):
        return None
    s = str(value).strip()
    return s if s else None


def parse_item_details(df_raw: pd.DataFrame, schema: dict) -> dict:
    """Returns {voucher_key: {voucher-level fields..., 'items': [...]}}."""
    marker = schema["invoice_block_marker"]
    marker_col = marker["column"]
    pattern = marker["pattern"]
    fields_map = marker.get("fields", {})
    blob_extract = marker.get("blob_extract", {})
    item_map = schema["item_row_column_map"]
    skip_rules = schema.get("skip_row_rules", [])
    data_start = schema["data_start_row"]

    marker_series = df_raw.iloc[:, marker_col].astype(str)
    is_marker_row = marker_series.str.contains(pattern, na=False, regex=True)

    vouchers = {}
    current_key = None

    for i in range(data_start, len(df_raw)):
        row = df_raw.iloc[i]

        if _row_matches_skip_rule(row, skip_rules):
            continue

        if is_marker_row.iloc[i]:
            raw_val = row.iloc[marker_col]
            if fields_map:
                voucher_fields = {f: safe_str(row.iloc[idx]) for f, idx in fields_map.items()}
            else:
                voucher_fields = extract_blob_fields(raw_val, blob_extract)

            key = voucher_fields.get("VOUCHERNUMBER") or safe_str(raw_val)
            current_key = key
            vouchers[current_key] = {**voucher_fields, "items": []}
            continue

        if current_key is None:
            continue

        item_name_col = item_map.get("STOCKITEMNAME")
        item_name = safe_str(row.iloc[item_name_col]) if item_name_col is not None else None
        if not item_name:
            continue

        item = {}
        for field_name, idx in item_map.items():
            val = row.iloc[idx]
            if field_name in {
                "ACTUALQTY", "FREEQTY", "RATE", "GSTRATE", "AMOUNT",
                "DISCOUNT", "TAXABLEVALUE", "GSTAMOUNT", "NETAMOUNT",
            }:
                item[field_name] = safe_float(val)
            else:
                item[field_name] = safe_str(val)
        vouchers[current_key]["items"].append(item)

    return vouchers


def _row_matches_skip_rule(row, skip_rules):
    for rule in skip_rules:
        col = rule.get("column")
        expected = rule.get("equals")
        if col is not None and col < len(row):
            val = row.iloc[col]
            if pd.notna(val) and str(val).strip() == expected:
                return True
    return False


def parse_summary(df_raw: pd.DataFrame, schema: dict) -> list:
    """Returns a list of row dicts keyed by canonical field name, skipping
    header/footer/junk rows (blank, repeated header text, or non-voucher
    total/summary lines)."""
    header_row = schema["header_row"]
    col_map = schema["column_map"]
    voucher_col = col_map["VOUCHERNUMBER"]

    rows = []
    for i in range(header_row + 1, len(df_raw)):
        row = df_raw.iloc[i]
        voucher_no = row.iloc[voucher_col]

        if pd.isna(voucher_no):
            continue
        voucher_no = str(voucher_no).strip()
        if not voucher_no or voucher_no.lower().startswith("total"):
            continue
        if voucher_no.replace(".", "", 1).isdigit():
            # pure-numeric rows are footer/count lines, not voucher numbers
            continue

        record = {}
        for field_name, idx in col_map.items():
            val = row.iloc[idx]
            record[field_name] = safe_float(val) if field_name in {"BILLAMOUNT", "ROUNDOFFAMOUNT"} else safe_str(val)
        rows.append(record)

    return rows


def _item_net(item: dict) -> float:
    """Prefer an explicit NETAMOUNT column; otherwise derive it as
    AMOUNT + GSTAMOUNT (pre-tax + tax); otherwise fall back to AMOUNT alone.
    Which case applies depends entirely on what the schema's
    item_row_column_map actually contains for that vendor's layout."""
    if "NETAMOUNT" in item and item["NETAMOUNT"]:
        return item["NETAMOUNT"]
    if "AMOUNT" in item and "GSTAMOUNT" in item:
        return item["AMOUNT"] + item["GSTAMOUNT"]
    return item.get("AMOUNT", 0.0)


@dataclass
class LayerBReport:
    join_match_rate: float = 0.0
    total_summary_rows: int = 0
    matched_invoices: int = 0
    reconciled_invoices: int = 0
    mismatched_invoices: int = 0
    mismatch_detail: list = field(default_factory=list)


def build_invoices(summary_rows: list, item_vouchers: dict, transform: dict,
                    tolerance: float = 1.0):
    """Joins Summary rows to Item Details vouchers via the transform, builds
    the final nested JSON, and produces a Layer B report so the whole file's
    trustworthiness is visible at a glance — not just per-row noise."""
    detail_keys = list(item_vouchers.keys())
    summary_keys = [r.get("VOUCHERNUMBER") for r in summary_rows if r.get("VOUCHERNUMBER")]

    join_check = validate_join_transform(transform, summary_keys, detail_keys)
    report = LayerBReport(
        join_match_rate=join_check.stats.get("match_rate", 0.0),
        total_summary_rows=len(summary_rows),
    )

    invoices = []
    for row in summary_rows:
        voucher_no = row.get("VOUCHERNUMBER")
        mapped_key = apply_transform(voucher_no, transform)
        detail = item_vouchers.get(mapped_key, {})
        items = detail.get("items", [])

        if items:
            report.matched_invoices += 1

        calculated_sum = sum(_item_net(i) for i in items)
        round_off = row.get("ROUNDOFFAMOUNT") or detail.get("ROUNDOFFAMOUNT") or 0.0
        round_off = safe_float(round_off)
        adjusted_sum = calculated_sum + round_off
        expected_total = safe_float(row.get("BILLAMOUNT"))
        is_valid = abs(adjusted_sum - expected_total) <= tolerance

        if items:
            if is_valid:
                report.reconciled_invoices += 1
            else:
                report.mismatched_invoices += 1
                report.mismatch_detail.append({
                    "VOUCHERNUMBER": voucher_no,
                    "expected": expected_total,
                    "calculated": round(adjusted_sum, 2),
                    "difference": round(abs(adjusted_sum - expected_total), 2),
                })

        invoices.append({
            "VOUCHERNUMBER": voucher_no,
            "PARTYNAME": row.get("PARTYNAME") or detail.get("PARTYNAME"),
            "PARTYGSTIN": row.get("PARTYGSTIN"),
            "BILLAMOUNT": expected_total,
            "ROUNDOFFAMOUNT": round_off,
            "items": items,
            "items_calculated_total": round(calculated_sum, 2),
            "is_validated": is_valid if items else None,
        })

    return invoices, report
