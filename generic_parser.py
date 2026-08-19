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


def parse_invoice_date(raw_date):
    """Normalizes a supplier invoice date to ISO (YYYY-MM-DD) and derives
    the GST return period fields from it: MONTH ('YYYY-MM') and
    FINANCIALYEAR ('YYYY-YY', Indian convention: April of year N through
    March of year N+1). Returns (invoice_date, month, financial_year).
    If the date can't be parsed, the raw value is kept as-is (so the data
    isn't silently dropped) and month/financial_year come back as None."""
    if raw_date is None:
        return None, None, None
    dt = pd.to_datetime(str(raw_date), dayfirst=True, errors="coerce")
    if pd.isna(dt):
        return safe_str(raw_date), None, None
    invoice_date = dt.strftime("%Y-%m-%d")
    month = f"{dt.year:04d}-{dt.month:02d}"
    fy_start = dt.year if dt.month >= 4 else dt.year - 1
    financial_year = f"{fy_start}-{str(fy_start + 1)[-2:]}"
    return invoice_date, month, financial_year


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

        line_id_field = schema.get("line_identifier_field", "STOCKITEMNAME")
        line_id_col = item_map.get(line_id_field)
        line_id_val = safe_str(row.iloc[line_id_col]) if line_id_col is not None else None
        if not line_id_val:
            continue

        # Structural guard against TOTAL:/TOTAL :/GRAND TOTAL:-style rows
        # (and any other subtotal/footer line, however it's worded): a
        # genuine item line always has a recorded quantity, even if it's
        # 0. Footer/total rows leave every per-item column blank and only
        # populate the summed AMOUNT/GSTAMOUNT columns. Checking this
        # structurally -- rather than matching TOTAL text -- means it
        # doesn't depend on exact wording or whitespace (so it also
        # catches inconsistencies like a stray space before the colon),
        # and it won't misfire on a real product whose name happens to
        # start with "Total" (that item would still carry a real
        # quantity). skip_row_rules remains available for any other
        # vendor-specific row-skip conditions a schema needs.
        qty_col = item_map.get("ACTUALQTY")
        if qty_col is not None and pd.isna(row.iloc[qty_col]):
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
    header rows and any footer/subtotal/summary rows -- wherever in the
    sheet they occur."""
    header_row = schema["header_row"]
    col_map = schema["column_map"]
    voucher_col = col_map["VOUCHERNUMBER"]
    party_col = col_map.get("PARTYNAME")

    rows = []
    for i in range(header_row + 1, len(df_raw)):
        row = df_raw.iloc[i]
        voucher_no = row.iloc[voucher_col]

        if pd.isna(voucher_no):
            continue
        voucher_no = str(voucher_no).strip()
        if not voucher_no:
            continue
        if voucher_no.lower().startswith("total"):
            # e.g. "Total :" / "Total No. of Invoice : N" -- a sheet-level
            # or per-block aggregate row, not an invoice. Skip it (don't
            # break/stop): some exports place subtotal rows mid-sheet
            # with real invoices continuing below them, so treating this
            # as an end-of-data marker would silently drop real data.
            continue
        if voucher_no.replace(".", "", 1).isdigit():
            # pure-numeric rows are footer/count lines, not voucher numbers
            continue

        # Structural guard against the SUMMARY block that follows the
        # "Total :" row (e.g. "TAXABLE VALUE", "TAX VALUE", "EXEMPTED
        # VALUE", "GST CESS VALUE"): none of those lines start with
        # "total", so the check above misses them, and their exact
        # wording isn't guaranteed to stay the same across exports. What
        # IS always true: a genuine invoice row has a party attached to
        # it, and a sheet-level aggregate row never does. Checking that
        # structurally catches the whole footer block regardless of
        # label text or where it sits in the sheet.
        if party_col is not None and pd.isna(row.iloc[party_col]):
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


def parse_grouped_blocks(df_filled: pd.DataFrame, schema: dict) -> list:
    """df_filled: output of ingest.forward_fill_blocks() — already flat,
    already forward-filled, header/footer rows already dropped. Groups
    consecutive rows sharing VOUCHERNUMBER into one invoice (a voucher can
    span multiple HSN lines, as in GST-2627-001292 spanning 3 HSN codes)."""
    col_map = schema["column_map"]
    voucher_col = col_map["VOUCHERNUMBER"]

    NUMERIC = {
        "ACTUALQTY", "AMOUNT", "TAXABLEVALUE", "CGSTAMOUNT", "SGSTAMOUNT",
        "IGSTAMOUNT", "CESSAMOUNT", "GSTAMOUNT", "GSTRATE", "RATE", "NETAMOUNT", "DISCOUNT",
    }

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
            line[f] = safe_float(val) if f in NUMERIC else safe_str(val)
        invoices[voucher_no]["lines"].append(line)

    result = [invoices[k] for k in order]
    for inv in result:
        # VOUCHERNUMBER in this layout is already the invoice number
        # itself (no separate internal-vs-supplier split like Purchase's
        # PB/xxx), so MONTH/FINANCIALYEAR are derived straight from the
        # invoice's own DATE column.
        invoice_date, month, financial_year = parse_invoice_date(inv.get("DATE"))
        if invoice_date is not None:
            inv["DATE"] = invoice_date
        inv["MONTH"] = month
        inv["FINANCIALYEAR"] = financial_year

    return result


@dataclass
class GroupedBlocksReport:
    total_invoices: int = 0
    reconciled_invoices: int = 0
    mismatched_invoices: int = 0
    mismatch_detail: list = field(default_factory=list)


def reconcile_grouped_blocks(invoices: list, tolerance: float = 1.0) -> GroupedBlocksReport:
    """No join step needed here (single sheet) — the check instead is:
    does sum(TAXABLEVALUE + GST components) across an invoice's lines land
    on a sane total? There's no separate 'Tot-Amt.' summary row to compare
    against per invoice in this file (Tot-Amt. is itself per-HSN-line, same
    grain as the lines), so this checks the invoice's own internal
    consistency: taxable + tax components == line's stated Tot-Amt., summed
    across lines. Any input file with an actual separate voucher-level
    total would compare against that instead — same pattern as build_invoices."""
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

        invoice_number = row.get("INVOICENUMBER") or detail.get("INVOICENUMBER")
        raw_invoice_date = row.get("INVOICEDATE") or detail.get("DATE") or detail.get("INVOICEDATE")
        invoice_date, month, financial_year = parse_invoice_date(raw_invoice_date)

        invoices.append({
            "VOUCHERNUMBER": voucher_no,
            "INVOICENUMBER": invoice_number,
            "INVOICEDATE": invoice_date,
            "MONTH": month,
            "FINANCIALYEAR": financial_year,
            "PARTYNAME": row.get("PARTYNAME") or detail.get("PARTYNAME"),
            "PARTYGSTIN": row.get("PARTYGSTIN"),
            "BILLAMOUNT": expected_total,
            "ROUNDOFFAMOUNT": round_off,
            "items": items,
            "items_calculated_total": round(calculated_sum, 2),
            "is_validated": is_valid if items else None,
        })

    return invoices, report
