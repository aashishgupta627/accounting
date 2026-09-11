"""
Mapping-driven deterministic parser.

Takes a MAPPING (per-file column addresses into the fixed canonical schema
— see validate_schema.py's module docstring for the schema/mapping
distinction) + the raw DataFrame(s), and produces nested invoice JSON. A
new vendor sharing an already-known layout needs a new mapping, never new
code here.

No LLM calls happen here. This module is intentionally boring.

EXTRACTION-STAGE SKIP RULE
--------------------------
Some books exports contain ledger blocks that are not real counterparties
— most commonly a Tally 'CANCEL' ledger group with no GSTIN. These rows
cannot be used downstream (Tally export, HSN summary, GST reconciliation),
because there is no recipient to attribute them to. They are dropped at
parse time (both here and in build_invoices for two_sheet_joined), and
returned separately as a `skipped` list so the caller (orchestrator ->
app.py) can surface them to the user.

Skip rule: a voucher is skipped iff it has NO resolvable GSTIN AND its
party name matches a known sentinel ledger name. Both conditions are
required — a genuine B2C customer with no GSTIN must survive. Sentinel
set defaults to {"CANCEL"} and is overridable per mapping via the
optional `skip_party_sentinels` key.
"""
import re
import pandas as pd
from dataclasses import dataclass, field
from validate_schema import apply_transform, extract_blob_fields, validate_join_transform

NUMERIC_LINE_FIELDS = {
    "ACTUALQTY", "RATE", "GSTRATE", "AMOUNT", "DISCOUNT",
    "TAXABLEVALUE", "GSTAMOUNT", "NETAMOUNT", "CGSTAMOUNT", "SGSTAMOUNT",
    "IGSTAMOUNT", "CESSAMOUNT",
}

_GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]$")

DEFAULT_SKIP_PARTY_SENTINELS = {"CANCEL"}


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


def clean_gstin(value):
    s = safe_str(value)
    if s is None:
        return None
    return s if _GSTIN_RE.match(s.upper()) else None


def _extract_extra(row, extra_fields: dict) -> dict:
    if not extra_fields:
        return {}
    out = {}
    for literal_name, idx in extra_fields.items():
        val = safe_str(row.iloc[idx])
        if val is not None:
            out[literal_name] = val
    return out


# ---------------------------------------------------------------------------
# Extraction-stage skip rule — see module docstring.
# ---------------------------------------------------------------------------

def _skip_sentinels_for(mapping: dict) -> set:
    raw = mapping.get("skip_party_sentinels")
    if raw is None:
        return DEFAULT_SKIP_PARTY_SENTINELS
    return {str(s).strip().upper() for s in raw}


def _is_skippable_party(party_name, party_gstin, sentinels: set) -> bool:
    """Skip iff no GSTIN AND party name is a known sentinel. A real B2C
    customer (no GSTIN, real name) must survive."""
    if party_gstin:
        return False
    name = (party_name or "").strip().upper()
    return name in sentinels


def _skip_record(doc_type, doc_no, party_name, doc_value, taxable_value,
                  reason, source_lines=None) -> dict:
    return {
        "doc_type": doc_type or "",
        "doc_no": doc_no or "",
        "party_name": party_name or "",
        "doc_value": round(float(doc_value or 0.0), 2),
        "taxable_value": round(float(taxable_value or 0.0), 2),
        "reason": reason,
        "source_lines": source_lines or 0,
    }


# ---------------------------------------------------------------------------
# two_sheet_joined item-side parsing (unchanged except signature note)
# ---------------------------------------------------------------------------

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


_GROUPED_VOUCHER_LEVEL_FIELDS = ("PARTYNAME", "PARTYGSTIN", "VOUCHERDATE", "PARTYSTATECODE")


def _derive_rate(row, rate_bucket_columns: dict):
    if not rate_bucket_columns:
        return None
    for rate, idx in rate_bucket_columns.items():
        val = row.iloc[idx]
        if pd.notna(val) and safe_float(val) != 0:
            return float(rate)
    return 0.0


def _derive_voucher_type(voucher_no: str, rule: dict):
    if not rule:
        return None
    pattern = rule.get("pattern")
    if pattern and voucher_no and re.search(pattern, voucher_no):
        return rule.get("match_value")
    return rule.get("default")


_NEGATIVE_VOUCHER_TYPE = {"Sales": "Credit Note", "Purchase": "Debit Note"}


def resolve_voucher_type(base_type, amount, pattern_type=None, tolerance=0.01):
    sign_type = None
    if base_type in _NEGATIVE_VOUCHER_TYPE and amount is not None:
        if amount < -tolerance:
            sign_type = _NEGATIVE_VOUCHER_TYPE[base_type]
        elif amount > tolerance:
            sign_type = base_type

    if pattern_type and sign_type and pattern_type != sign_type:
        return pattern_type, True
    return pattern_type or sign_type or base_type, False


def _amount_sign_consistent(amount, tax_breakup, tolerance=0.01):
    if not tax_breakup or amount is None or abs(amount) <= tolerance:
        return True
    expected_negative = amount < 0
    for bucket in tax_breakup:
        taxable = bucket.get("TAXABLEVALUE") or 0.0
        if abs(taxable) <= tolerance:
            continue
        if (taxable < 0) != expected_negative:
            return False
    return True


def _build_tax_breakup(items: list) -> list:
    buckets = {}
    order = []
    for item in items:
        rate = item.get("GSTRATE")
        if rate is None:
            continue
        if rate not in buckets:
            buckets[rate] = {
                "GSTRATE": rate, "TAXABLEVALUE": 0.0, "CGSTAMOUNT": 0.0,
                "SGSTAMOUNT": 0.0, "IGSTAMOUNT": 0.0, "CESSAMOUNT": 0.0,
            }
            order.append(rate)
        b = buckets[rate]
        b["TAXABLEVALUE"] += item.get("TAXABLEVALUE") or 0.0
        b["CGSTAMOUNT"] += item.get("CGSTAMOUNT") or 0.0
        b["SGSTAMOUNT"] += item.get("SGSTAMOUNT") or 0.0
        b["IGSTAMOUNT"] += item.get("IGSTAMOUNT") or 0.0
        b["CESSAMOUNT"] += item.get("CESSAMOUNT") or 0.0
    return [
        {k: (round(v, 2) if isinstance(v, float) else v) for k, v in buckets[r].items()}
        for r in order
    ]


def _validate_grouped_invoice(items: list, tolerance: float = 1.0) -> bool:
    for item in items:
        taxable = item.get("TAXABLEVALUE") or 0.0
        tax = (
            (item.get("CGSTAMOUNT") or 0.0) + (item.get("SGSTAMOUNT") or 0.0)
            + (item.get("IGSTAMOUNT") or 0.0) + (item.get("CESSAMOUNT") or 0.0)
        )
        stated = _item_net(item)
        if stated and abs((taxable + tax) - stated) > tolerance:
            return False
    return True


def parse_grouped_blocks(df_filled: pd.DataFrame, mapping: dict):
    """Returns (invoices, skipped). `skipped` lists extraction-stage-drop
    records — see module docstring (CANCEL ledger blocks, etc.)."""
    col_map = mapping["column_map"]
    extra_fields = mapping.get("extra_fields", {})
    voucher_col = col_map["VOUCHERNUMBER"]
    rate_bucket_columns = mapping.get("rate_bucket_columns", {})
    sign_flip_fields = set(mapping.get("sign_flip_fields", []))
    voucher_type_rule = mapping.get("voucher_type_rule")
    sign_base_type = (voucher_type_rule or {}).get("sign_base_type")
    sentinels = _skip_sentinels_for(mapping)

    invoices = {}
    order = []
    for i in range(len(df_filled)):
        row = df_filled.iloc[i]
        voucher_no = row.iloc[voucher_col]
        if pd.isna(voucher_no):
            continue
        voucher_no = str(voucher_no).strip()

        if voucher_no not in invoices:
            invoices[voucher_no] = {
                "VOUCHERTYPE": _derive_voucher_type(voucher_no, voucher_type_rule),
                "VOUCHERNUMBER": voucher_no,
                "items": [],
            }
            for f in _GROUPED_VOUCHER_LEVEL_FIELDS:
                if f in col_map:
                    raw = row.iloc[col_map[f]]
                    invoices[voucher_no][f] = clean_gstin(raw) if f == "PARTYGSTIN" else safe_str(raw)
            order.append(voucher_no)

        line = {}
        for f, idx in col_map.items():
            if f in {"VOUCHERNUMBER"} | set(_GROUPED_VOUCHER_LEVEL_FIELDS):
                continue
            val = row.iloc[idx]
            is_numeric = f in NUMERIC_LINE_FIELDS
            parsed = safe_float(val) if is_numeric else safe_str(val)
            if is_numeric and f in sign_flip_fields and parsed:
                parsed = -parsed
            line[f] = parsed
        line.setdefault("STOCKITEMNAME", None)
        if rate_bucket_columns:
            line["GSTRATE"] = _derive_rate(row, rate_bucket_columns)
        extra = _extract_extra(row, extra_fields)
        if extra:
            line["extra"] = extra
        invoices[voucher_no]["items"].append(line)

    result = []
    skipped = []
    for k in order:
        inv = invoices[k]
        items = inv["items"]
        inv["BILLAMOUNT"] = round(sum(_item_net(i) for i in items), 2)
        inv["ROUNDOFFAMOUNT"] = 0.0
        inv["tax_breakup"] = _build_tax_breakup(items)
        inv["is_validated"] = _validate_grouped_invoice(items)

        if sign_base_type:
            resolved_type, mismatch = resolve_voucher_type(
                sign_base_type, inv["BILLAMOUNT"], pattern_type=inv["VOUCHERTYPE"]
            )
            inv["VOUCHERTYPE"] = resolved_type
            if mismatch or not _amount_sign_consistent(inv["BILLAMOUNT"], inv["tax_breakup"]):
                inv.setdefault("extra", {})["voucher_type_flag"] = (
                    "VOUCHERNUMBER pattern and BILLAMOUNT sign disagree" if mismatch
                    else "tax_breakup bucket sign inconsistent with BILLAMOUNT"
                )

        # Extraction-stage skip: no GSTIN AND party-name sentinel (CANCEL).
        if _is_skippable_party(inv.get("PARTYNAME"), inv.get("PARTYGSTIN"), sentinels):
            skipped.append(_skip_record(
                doc_type=inv.get("VOUCHERTYPE"),
                doc_no=inv.get("VOUCHERNUMBER"),
                party_name=inv.get("PARTYNAME"),
                doc_value=inv.get("BILLAMOUNT", 0.0),
                taxable_value=sum(b.get("TAXABLEVALUE", 0.0) for b in inv.get("tax_breakup", [])),
                reason=f"party name matches skip sentinel {inv.get('PARTYNAME')!r} and no resolvable GSTIN",
                source_lines=len(items),
            ))
            continue

        result.append(inv)

    return result, skipped


@dataclass
class GroupedBlocksReport:
    total_invoices: int = 0
    reconciled_invoices: int = 0
    mismatched_invoices: int = 0
    mismatch_detail: list = field(default_factory=list)


def reconcile_grouped_blocks(invoices: list, tolerance: float = 1.0) -> GroupedBlocksReport:
    report = GroupedBlocksReport(total_invoices=len(invoices))
    for inv in invoices:
        items = inv.get("items", inv.get("lines", []))
        ok = True
        for item in items:
            taxable = item.get("TAXABLEVALUE") or 0.0
            tax = (
                (item.get("CGSTAMOUNT") or 0.0) + (item.get("SGSTAMOUNT") or 0.0)
                + (item.get("IGSTAMOUNT") or 0.0) + (item.get("CESSAMOUNT") or 0.0)
            )
            stated_total = _item_net(item)
            if stated_total and abs((taxable + tax) - stated_total) > tolerance:
                ok = False
                report.mismatch_detail.append({
                    "VOUCHERNUMBER": inv["VOUCHERNUMBER"],
                    "HSNCODE": item.get("HSNCODE"),
                    "taxable_plus_tax": round(taxable + tax, 2),
                    "stated_amount": round(stated_total, 2),
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
    voucher_type_flags: list = field(default_factory=list)


def build_invoices(summary_rows: list, item_vouchers: dict, transform: dict,
                    tolerance: float = 1.0, voucher_type: str = None,
                    skip_sentinels: set = None):
    """Returns (invoices, report, skipped). Same extraction-stage skip rule
    as parse_grouped_blocks: no GSTIN AND party name in sentinels."""
    sentinels = skip_sentinels if skip_sentinels is not None else DEFAULT_SKIP_PARTY_SENTINELS

    detail_keys = list(item_vouchers.keys())
    summary_keys = [r.get("VOUCHERNUMBER") for r in summary_rows if r.get("VOUCHERNUMBER")]

    join_check = validate_join_transform(transform, summary_keys, detail_keys)
    report = LayerBReport(
        join_match_rate=join_check.stats.get("match_rate", 0.0),
        total_summary_rows=len(summary_rows),
    )

    invoices = []
    skipped = []
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
        expected_total = safe_float(row.get("BILLAMOUNT"))
        tax_breakup = row.get("tax_breakup", [])

        is_valid = None
        adjusted_sum = None
        matched_via = None
        if items:
            adjusted_sum = calculated_sum + round_off
            is_valid = abs(adjusted_sum - expected_total) <= tolerance
            matched_via = "items"
        elif tax_breakup:
            tax_breakup_total = sum(
                (b.get("TAXABLEVALUE") or 0.0) + (b.get("CGSTAMOUNT") or 0.0)
                + (b.get("SGSTAMOUNT") or 0.0) + (b.get("IGSTAMOUNT") or 0.0)
                + (b.get("CESSAMOUNT") or 0.0)
                for b in tax_breakup
            )
            adjusted_sum = tax_breakup_total + round_off
            is_valid = abs(adjusted_sum - expected_total) <= tolerance
            matched_via = "summary tax_breakup"

        if is_valid is not None:
            if is_valid:
                report.reconciled_invoices += 1
            else:
                report.mismatched_invoices += 1
                report.mismatch_detail.append({
                    "VOUCHERNUMBER": voucher_no,
                    "expected": expected_total,
                    "calculated": round(adjusted_sum, 2),
                    "difference": round(abs(adjusted_sum - expected_total), 2),
                    "matched_via": matched_via,
                })

        resolved_type, type_mismatch = resolve_voucher_type(voucher_type, expected_total)
        sign_ok = _amount_sign_consistent(expected_total, tax_breakup)
        if type_mismatch or not sign_ok:
            report.voucher_type_flags.append({
                "VOUCHERNUMBER": voucher_no,
                "base_type": voucher_type,
                "resolved_type": resolved_type,
                "BILLAMOUNT": expected_total,
                "reason": "pattern/sign disagree" if type_mismatch else "tax_breakup sign inconsistent",
            })

        party_name = row.get("PARTYNAME") or detail.get("PARTYNAME")
        party_gstin = row.get("PARTYGSTIN") or detail.get("PARTYGSTIN")

        invoice = {
            "VOUCHERTYPE": resolved_type,
            "VOUCHERNUMBER": voucher_no,
            "REFERENCENUMBER": row.get("REFERENCENUMBER"),
            "REFERENCEDATE": row.get("REFERENCEDATE"),
            "VOUCHERDATE": row.get("VOUCHERDATE") or detail.get("VOUCHERDATE"),
            "PARTYNAME": party_name,
            "PARTYGSTIN": party_gstin,
            "PARTYSTATECODE": row.get("PARTYSTATECODE"),
            "BILLAMOUNT": expected_total,
            "ROUNDOFFAMOUNT": round_off,
            "tax_breakup": tax_breakup,
            "items": items,
            "items_calculated_total": round(calculated_sum, 2),
            "is_validated": is_valid,
        }
        if row.get("extra"):
            invoice["extra"] = row["extra"]

        if _is_skippable_party(party_name, party_gstin, sentinels):
            skipped.append(_skip_record(
                doc_type=resolved_type,
                doc_no=voucher_no,
                party_name=party_name,
                doc_value=expected_total,
                taxable_value=sum(b.get("TAXABLEVALUE", 0.0) for b in tax_breakup),
                reason=f"party name matches skip sentinel {party_name!r} and no resolvable GSTIN",
                source_lines=len(items),
            ))
            continue

        invoices.append(invoice)

    return invoices, report, skipped
