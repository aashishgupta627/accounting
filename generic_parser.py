"""
Mapping-driven deterministic parser.

Takes a MAPPING (per-file column addresses into the fixed canonical schema
— see validate_schema.py's module docstring for the schema/mapping
distinction) + the raw DataFrame(s), and produces nested invoice JSON. A
new vendor sharing an already-known layout needs a new mapping, never new
code here.

No LLM calls happen here. This module is intentionally boring.
"""
import re
import pandas as pd
from dataclasses import dataclass, field
from validate_schema import apply_transform, extract_blob_fields, validate_join_transform

NUMERIC_LINE_FIELDS = {
    "ACTUALQTY", "FREEQTY", "RATE", "GSTRATE", "AMOUNT", "DISCOUNT",
    "TAXABLEVALUE", "GSTAMOUNT", "NETAMOUNT", "CGSTAMOUNT", "SGSTAMOUNT",
    "IGSTAMOUNT", "CESSAMOUNT",
}


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


def _extract_extra(row, extra_fields: dict) -> dict:
    """extra_fields: {literal_header_text: col_idx}. Values are captured
    as-is (best-effort string), never validated against the canonical
    schema — this is exactly the vendor-specific overflow bucket."""
    if not extra_fields:
        return {}
    out = {}
    for literal_name, idx in extra_fields.items():
        val = safe_str(row.iloc[idx])
        if val is not None:
            out[literal_name] = val
    return out


def parse_item_details(df_raw: pd.DataFrame, mapping: dict) -> dict:
    """Returns {voucher_key: {voucher-level fields..., 'items': [...]}}."""
    marker = mapping["invoice_block_marker"]
    marker_col = marker["column"]
    pattern = marker["pattern"]
    fields_map = marker.get("fields", {})
    blob_extract = marker.get("blob_extract", {})
    item_map = mapping["item_row_column_map"]
    extra_fields = mapping.get("extra_fields", {})
    skip_rules = mapping.get("skip_row_rules", [])
    data_start = mapping["data_start_row"]

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

        line_id_field = mapping.get("line_identifier_field", "STOCKITEMNAME")
        line_id_col = item_map.get(line_id_field)
        line_id_val = safe_str(row.iloc[line_id_col]) if line_id_col is not None else None
        if not line_id_val:
            continue

        numeric_cols_in_map = [idx for f, idx in item_map.items() if f in NUMERIC_LINE_FIELDS]
        if numeric_cols_in_map and not any(pd.notna(row.iloc[idx]) for idx in numeric_cols_in_map):
            continue

        item = {}
        for field_name, idx in item_map.items():
            val = row.iloc[idx]
            item[field_name] = safe_float(val) if field_name in NUMERIC_LINE_FIELDS else safe_str(val)
        extra = _extract_extra(row, extra_fields)
        if extra:
            item["extra"] = extra
        vouchers[current_key]["items"].append(item)

    return vouchers


def _normalize_for_match(text) -> str:
    return "".join(str(text).upper().split())


def _row_matches_skip_rule(row, skip_rules):
    for rule in skip_rules:
        col = rule.get("column")
        if col is None or col >= len(row):
            continue
        val = row.iloc[col]
        if pd.isna(val):
            continue
        if "equals" in rule and str(val).strip() == rule["equals"]:
            return True
        if "equals_normalized" in rule and _normalize_for_match(val) == _normalize_for_match(rule["equals_normalized"]):
            return True
    return False


def parse_summary(df_raw: pd.DataFrame, mapping: dict) -> list:
    header_row = mapping["header_row"]
    col_map = mapping["column_map"]
    extra_fields = mapping.get("extra_fields", {})
    tax_rate_breakup = mapping.get("tax_rate_breakup", [])
    footer_marker = mapping.get("footer_marker")
    voucher_number_pattern = mapping.get("voucher_number_pattern")
    compiled_vnp = re.compile(voucher_number_pattern) if voucher_number_pattern else None
    voucher_col = col_map["VOUCHERNUMBER"]

    header_voucher_label = None
    if 0 <= header_row < len(df_raw):
        raw_header_val = df_raw.iloc[header_row].iloc[voucher_col]
        if pd.notna(raw_header_val):
            header_voucher_label = _normalize_for_match(raw_header_val)

    rows = []
    for i in range(header_row + 1, len(df_raw)):
        row = df_raw.iloc[i]

        if footer_marker is not None:
            fm_col = footer_marker["column"]
            fm_val = row.iloc[fm_col] if fm_col < len(row) else None
            if pd.notna(fm_val) and _normalize_for_match(fm_val) == _normalize_for_match(footer_marker["equals_normalized"]):
                break

        voucher_no = row.iloc[voucher_col]

        if pd.isna(voucher_no):
            continue
        voucher_no = str(voucher_no).strip()
        if not voucher_no:
            continue

        if compiled_vnp is not None:
            if not compiled_vnp.match(voucher_no):
                continue
        else:
            if voucher_no.lower().startswith("total"):
                continue
            if voucher_no.replace(".", "", 1).isdigit():
                continue
            if header_voucher_label is not None and _normalize_for_match(voucher_no) == header_voucher_label:
                continue

        record = {}
        for field_name, idx in col_map.items():
            val = row.iloc[idx]
            record[field_name] = safe_float(val) if field_name in {"BILLAMOUNT", "ROUNDOFFAMOUNT", "CESSAMOUNT"} else safe_str(val)

        if tax_rate_breakup:
            buckets = []
            for bucket_map in tax_rate_breakup:
                rate = bucket_map["GSTRATE"]
                taxable = safe_float(row.iloc[bucket_map["TAXABLEVALUE"]]) if "TAXABLEVALUE" in bucket_map else 0.0
                if not taxable:
                    continue
                bucket = {"GSTRATE": rate, "TAXABLEVALUE": taxable}
                for f in ("CGSTAMOUNT", "SGSTAMOUNT", "IGSTAMOUNT", "CESSAMOUNT"):
                    if f in bucket_map:
                        bucket[f] = safe_float(row.iloc[bucket_map[f]])
                buckets.append(bucket)
            record["tax_breakup"] = buckets

        extra = _extract_extra(row, extra_fields)
        if extra:
            record["extra"] = extra
        rows.append(record)

    return rows


def _item_net(item: dict) -> float:
    if "NETAMOUNT" in item and item["NETAMOUNT"]:
        return item["NETAMOUNT"]
    if "AMOUNT" in item and "GSTAMOUNT" in item:
        return item["AMOUNT"] + item["GSTAMOUNT"]
    return item.get("AMOUNT", 0.0)


def parse_grouped_blocks(df_filled: pd.DataFrame, mapping: dict) -> list:
    col_map = mapping["column_map"]
    extra_fields = mapping.get("extra_fields", {})
    voucher_col = col_map["VOUCHERNUMBER"]

    invoices = {}
    order = []
    for i in range(len(df_filled)):
        row = df_filled.iloc[i]
        voucher_no = row.iloc[voucher_col]
        if pd.isna(voucher_no):
            continue
        voucher_no = str(voucher_no).strip()

        if voucher_no not in invoices:
            invoices[voucher_no] = {"VOUCHERNUMBER": voucher_no, "lines": []}
            for f in ("PARTYNAME", "PARTYGSTIN", "DATE"):
                if f in col_map:
                    invoices[voucher_no][f] = safe_str(row.iloc[col_map[f]])
            order.append(voucher_no)

        line = {}
        for f, idx in col_map.items():
            if f in {"VOUCHERNUMBER", "PARTYNAME", "PARTYGSTIN", "DATE"}:
                continue
            val = row.iloc[idx]
            line[f] = safe_float(val) if f in NUMERIC_LINE_FIELDS else safe_str(val)
        extra = _extract_extra(row, extra_fields)
        if extra:
            line["extra"] = extra
        invoices[voucher_no]["lines"].append(line)

    return [invoices[k] for k in order]


@dataclass
class GroupedBlocksReport:
    total_invoices: int = 0
    reconciled_invoices: int = 0
    mismatched_invoices: int = 0
    mismatch_detail: list = field(default_factory=list)


def reconcile_grouped_blocks(invoices: list, tolerance: float = 1.0) -> GroupedBlocksReport:
    report = GroupedBlocksReport(total_invoices=len(invoices))
    for inv in invoices:
        ok = True
        for line in inv["lines"]:
            taxable = line.get("TAXABLEVALUE", 0.0)
            tax = (
                line.get("CGSTAMOUNT", 0.0) + line.get("SGSTAMOUNT", 0.0)
                + line.get("IGSTAMOUNT", 0.0) + line.get("CESSAMOUNT", 0.0)
            )
            stated_total = line.get("AMOUNT", 0.0)
            if stated_total and abs((taxable + tax) - stated_total) > tolerance:
                ok = False
                report.mismatch_detail.append({
                    "VOUCHERNUMBER": inv["VOUCHERNUMBER"],
                    "HSNCODE": line.get("HSNCODE"),
                    "taxable_plus_tax": round(taxable + tax, 2),
                    "stated_amount": stated_total,
                    "difference": round(abs((taxable + tax) - stated_total), 2),
                })
        if ok:
            report.reconciled_invoices += 1
        else:
            report.mismatched_invoices += 1
    return report


@dataclass
class LayerBReport:
    join_match_rate: float = 0.0
    total_summary_rows: int = 0
    matched_invoices: int = 0
    reconciled_invoices: int = 0
    mismatched_invoices: int = 0
    mismatch_detail: list = field(default_factory=list)


def build_invoices(summary_rows: list, item_vouchers: dict, transform: dict,
                    tolerance: float = 1.0, voucher_type: str = None):
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

        invoice = {
            "VOUCHERTYPE": voucher_type,
            "VOUCHERNUMBER": voucher_no,
            "REFERENCENUMBER": row.get("REFERENCENUMBER"),
            "REFERENCEDATE": row.get("REFERENCEDATE"),
            "DATE": row.get("DATE") or detail.get("DATE"),
            "PARTYNAME": row.get("PARTYNAME") or detail.get("PARTYNAME"),
            "PARTYGSTIN": row.get("PARTYGSTIN"),
            "STATECODE": row.get("STATECODE"),
            "BILLAMOUNT": expected_total,
            "ROUNDOFFAMOUNT": round_off,
            "tax_breakup": row.get("tax_breakup", []),
            "items": items,
            "items_calculated_total": round(calculated_sum, 2),
            "is_validated": is_valid if items else None,
        }
        if row.get("extra"):
            invoice["extra"] = row["extra"]
        invoices.append(invoice)

    return invoices, report
