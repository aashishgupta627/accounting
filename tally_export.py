"""
Tally Export Module - Generates Accounting Voucher rows from invoice JSON

Handles both Sales and Purchase vouchers with B2B/B2C classification.

Reads VOUCHERDATE / PARTYSTATECODE from the invoice JSON (renamed from
DATE / STATECODE — see validate_schema.py's module docstring).

Credit Notes and Debit Notes are NOT a separate code path here. Upstream
(generic_parser.resolve_voucher_type) a Credit Note is just a Sales
voucher whose BILLAMOUNT/tax_breakup came through negative, and a Debit
Note is a Purchase voucher the same way — the sign survives all the way
from the source file. Every ledger row below is built from a *signed*
amount via _signed_amount_fields(), which flips the row's Dr/Cr side (and
always writes a positive magnitude) whenever that signed amount is
negative. This is the standard Tally convention for a reversing voucher:
flip Dr<->Cr with positive amounts, not the same side with a negative
number (most Tally importers don't treat a negative Dr as an implicit
credit).
"""

from dataclasses import dataclass, field
import pandas as pd
from typing import List, Dict, Optional, Tuple

@dataclass
class TallyExportConfig:
    home_state_is_ut: bool = True
    country: str = "India"
    round_off_ledger_name: str = "Round Off"
    balance_tolerance: float = 0.02

HSN_SAC_DETAILS_FIXED = "Specify details here"

COMMON_COLUMNS = [
    "Voucher Date", "Reference No.", "Voucher Type Name", "Voucher Number",
    "Buyer/Supplier - Address", "Buyer/Supplier - Pincode",
    "Ledger Name", "IGST Rate", "CGST Rate", "SGST/UTGST Rate",
    "Ledger Amount", "Ledger Amount Dr/Cr",
    "Item Name", "Billed Quantity", "Item Rate", "Item Rate per",
    "HSN/SAC Details", "HSN/SAC",
    "Voucher Narration", "Change Mode",
]
B2B_IDENTITY_COLUMN = "Buyer/Supplier - GSTIN/UIN"
B2C_IDENTITY_COLUMN = "Buyer/Supplier - GST Registration Type"
TRAILING_COLUMNS = ["Buyer/Supplier - Country"]

B2B_COLUMNS = COMMON_COLUMNS + [B2B_IDENTITY_COLUMN] + TRAILING_COLUMNS
B2C_COLUMNS = COMMON_COLUMNS + [B2C_IDENTITY_COLUMN] + TRAILING_COLUMNS


@dataclass
class TallyExportReport:
    voucher_type: str = ""
    mode: str = ""
    total_invoices_in: int = 0
    skipped_not_validated: List[str] = field(default_factory=list)
    skipped_no_tax_breakup: List[str] = field(default_factory=list)
    balance_mismatches: List[Dict] = field(default_factory=list)
    gstin_state_mismatches: List[Dict] = field(default_factory=list)
    vouchers_written: int = 0
    rows_written: int = 0


def fmt_rate(x: float) -> str:
    if float(x) == int(x):
        return str(int(x))
    return str(round(x, 2)).rstrip("0").rstrip(".")


def split_state(statecode):
    if not statecode:
        return None, None
    s = str(statecode)
    if "-" in s:
        code, name = s.split("-", 1)
        return code.strip(), name.strip()
    return None, s.strip()


def _signed_amount_fields(amount: float, normal_side: str) -> Tuple[float, str]:
    """Turn a *signed* ledger amount into (magnitude, Dr/Cr side).

    `normal_side` is the side this ledger sits on for an ordinary,
    positive-amount voucher of this type — e.g. 'Dr' for the party row on
    a Sales voucher, 'Cr' for the Sales/tax ledgers on that same voucher.
    A negative `amount` means this row belongs to a reversal (Credit Note
    on the Sales side, Debit Note on the Purchase side) and flips to the
    opposite side, using the absolute value as the magnitude. See the
    module docstring for why this is done per-row from the sign rather
    than as a separate "is this a Credit Note" branch.
    """
    flipped = "Cr" if normal_side == "Dr" else "Dr"
    side = normal_side if amount >= 0 else flipped
    return round(abs(amount), 2), side


def split_b2b_b2c(invoices: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    b2b, b2c = [], []
    for inv in invoices:
        gstin = inv.get("PARTYGSTIN")
        if gstin and str(gstin).strip():
            b2b.append(inv)
        else:
            b2c.append(inv)
    return b2b, b2c

def split_b2b_b2c_purchase(invoices: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    b2b, b2c = [], []
    for inv in invoices:
        gstin = inv.get("PARTYGSTIN")
        if gstin and str(gstin).strip():
            b2b.append(inv)
            continue
        tax_breakup = inv.get("tax_breakup") or []
        if tax_breakup:
            b2b.append(inv)
        else:
            b2c.append(inv)
    return b2b, b2c

def get_tax_rates(tax_breakup: List[Dict], rate: float) -> Dict:
    for bucket in tax_breakup:
        if bucket.get("GSTRATE") == rate:
            return {
                "cgst_rate": float(bucket.get("CGST_RATE") or 0),
                "sgst_rate": float(bucket.get("SGST_RATE") or 0),
                "igst_rate": float(bucket.get("IGST_RATE") or 0),
            }
    return {"cgst_rate": 0, "sgst_rate": 0, "igst_rate": 0}


def _base_sales_row(invoice: Dict, mode: str, config: TallyExportConfig) -> Dict:
    _, state_name = split_state(invoice.get("PARTYSTATECODE"))

    row = {
        "Voucher Date": invoice.get("VOUCHERDATE"),
        "Reference No.": None,
        "Voucher Type Name": invoice.get("VOUCHERTYPE") or "Sales",
        "Voucher Number": invoice.get("VOUCHERNUMBER"),
        "Buyer/Supplier - Address": None,
        "Buyer/Supplier - Pincode": None,
        "Ledger Name": None,
        "IGST Rate": None,
        "CGST Rate": None,
        "SGST/UTGST Rate": None,
        "Ledger Amount": None,
        "Ledger Amount Dr/Cr": None,
        "Item Name": None,
        "Billed Quantity": None,
        "Item Rate": None,
        "Item Rate per": None,
        "Voucher Narration": None,
        "Change Mode": "Accounting Invoice",
        "Buyer/Supplier - Bill to/from": invoice.get("PARTYNAME"),
        "Buyer/Supplier - Country": config.country,
    }

    if mode == "B2B":
        row[B2B_IDENTITY_COLUMN] = invoice.get("PARTYGSTIN")
    else:
        row[B2C_IDENTITY_COLUMN] = "Unregistered/Consumer"

    return row


def _base_purchase_row(invoice: Dict, mode: str, config: TallyExportConfig) -> Dict:
    _, state_name = split_state(invoice.get("PARTYSTATECODE"))

    ref_no = invoice.get("REFERENCENUMBER") or invoice.get("VOUCHERNUMBER")
    voucher_no = invoice.get("VOUCHERNUMBER")
    voucher_date = invoice.get("REFERENCEDATE") or invoice.get("VOUCHERDATE")

    row = {
        "Voucher Date": voucher_date,
        "Reference No.": ref_no,
        "Voucher Type Name": invoice.get("VOUCHERTYPE") or "Purchase",
        "Voucher Number": voucher_no,
        "Buyer/Supplier - Address": None,
        "Buyer/Supplier - Pincode": None,
        "Ledger Name": None,
        "IGST Rate": None,
        "CGST Rate": None,
        "SGST/UTGST Rate": None,
        "Ledger Amount": None,
        "Ledger Amount Dr/Cr": None,
        "Item Name": None,
        "Billed Quantity": None,
        "Item Rate": None,
        "Item Rate per": None,
        "Voucher Narration": None,
        "Change Mode": "Accounting Invoice",
        "Buyer/Supplier - Bill to/from": invoice.get("PARTYNAME"),
        "Buyer/Supplier - Country": config.country,
    }

    if mode == "B2B":
        row[B2B_IDENTITY_COLUMN] = invoice.get("PARTYGSTIN")
    else:
        row[B2C_IDENTITY_COLUMN] = "Unregistered/Consumer"

    return row

def group_items_by_hsn(items: List[Dict], rate: Optional[float] = None, value_field: str = "AMOUNT") -> List[Tuple[Optional[str], float]]:
    groups: Dict[Optional[str], float] = {}
    order: List[Optional[str]] = []
    for item in items:
        if rate is not None and float(item.get("GSTRATE") or 0) != float(rate):
            continue
        hsn = item.get("HSNCODE")
        hsn = str(hsn).strip() if hsn not in (None, "") else None
        amt = float(item.get(value_field) or 0.0)
        if hsn not in groups:
            groups[hsn] = 0.0
            order.append(hsn)
        groups[hsn] += amt
    return [(hsn, groups[hsn]) for hsn in order]


def _hsn_groups_reconciled(items: List[Dict], target_total: float, rate: Optional[float] = None, value_field: str = "AMOUNT") -> List[Tuple[Optional[str], float]]:
    groups = group_items_by_hsn(items, rate=rate, value_field=value_field)
    if not groups:
        return [(None, target_total)]
    diff = target_total - sum(amt for _, amt in groups)
    if abs(diff) > 0.004:
        hsn, amt = groups[-1]
        groups[-1] = (hsn, amt + diff)
    return groups


SALES_LEDGER_OVERRIDES = {
    5: "Local Sales  GST 5%",
    18: "Local Sales GST 18%",
}
ZERO_RATE_SALES_LEDGER = "Local Sales 0%"


def sales_ledger_name(rate: float, interstate: bool) -> str:
    if rate == 0:
        return ZERO_RATE_SALES_LEDGER
    if interstate:
        return f"Interstate Sales GST {fmt_rate(rate)}%"
    return SALES_LEDGER_OVERRIDES.get(rate, f"Local Sales GST {fmt_rate(rate)}%")


def output_tax_ledger_name(component: str, rate: float) -> str:
    if component == "IGST":
        return f"Output IGST {fmt_rate(rate)}%"
    half = rate / 2
    return f"Output {component} {fmt_rate(half)}%"


def build_sales_voucher_rows(
    invoice: Dict,
    mode: str,
    config: TallyExportConfig,
    report: TallyExportReport
) -> List[Dict]:
    voucher_no = invoice.get("VOUCHERNUMBER")

    if invoice.get("is_validated") is not True:
        report.skipped_not_validated.append(voucher_no)
        return []

    tax_breakup = invoice.get("tax_breakup") or []
    if not tax_breakup:
        report.skipped_no_tax_breakup.append(voucher_no)
        return []

    base = _base_sales_row(invoice, mode, config)
    rows = []

    bill_amount = float(invoice.get("BILLAMOUNT") or 0.0)
    party_name = invoice.get("PARTYNAME")

    dr_row = dict(base)
    dr_row["Ledger Name"] = party_name
    dr_amt, dr_side = _signed_amount_fields(bill_amount, "Dr")
    dr_row["Ledger Amount"] = dr_amt
    dr_row["Ledger Amount Dr/Cr"] = dr_side
    rows.append(dr_row)

    total_cr = 0.0  # kept as *signed* running total, same arithmetic as before —
                     # only the row-level magnitude/side (above/below) changed.

    for bucket in tax_breakup:
        rate = bucket.get("GSTRATE", 0)
        taxable = float(bucket.get("TAXABLEVALUE") or 0.0)
        cgst = float(bucket.get("CGSTAMOUNT") or 0.0)
        sgst = float(bucket.get("SGSTAMOUNT") or 0.0)
        igst = float(bucket.get("IGSTAMOUNT") or 0.0)
        cess = float(bucket.get("CESSAMOUNT") or 0.0)

        interstate = igst != 0

        # was `igst > 0 and (cgst > 0 or sgst > 0)` -- a Credit Note's
        # bucket has all three negative, which that comparison would
        # silently miss. Same class of bug as the interstate = igst != 0
        # fix elsewhere in this codebase.
        if igst != 0 and (cgst != 0 or sgst != 0):
            report.gstin_state_mismatches.append({
                "VOUCHERNUMBER": voucher_no,
                "GSTRATE": rate,
                "issue": "both IGST and CGST/SGST populated on the same rate bucket",
            })

        if taxable:
            ledger_name = sales_ledger_name(rate, interstate)
            for hsn, amt in _hsn_groups_reconciled(invoice.get("items") or [], taxable, rate=rate, value_field="TAXABLEVALUE"):
                if not amt:
                    continue
                r = dict(base)
                r["Ledger Name"] = ledger_name
                r_amt, r_side = _signed_amount_fields(amt, "Cr")
                r["Ledger Amount"] = r_amt
                r["Ledger Amount Dr/Cr"] = r_side
                if hsn:
                    r["HSN/SAC Details"] = HSN_SAC_DETAILS_FIXED
                    r["HSN/SAC"] = hsn
                rows.append(r)
                total_cr += amt

        if interstate:
            if igst:
                r = dict(base)
                r["Ledger Name"] = output_tax_ledger_name("IGST", rate)
                r_amt, r_side = _signed_amount_fields(igst, "Cr")
                r["Ledger Amount"] = r_amt
                r["Ledger Amount Dr/Cr"] = r_side
                rows.append(r)
                total_cr += igst
        else:
            if cgst:
                r = dict(base)
                r["Ledger Name"] = output_tax_ledger_name("CGST", rate)
                r_amt, r_side = _signed_amount_fields(cgst, "Cr")
                r["Ledger Amount"] = r_amt
                r["Ledger Amount Dr/Cr"] = r_side
                rows.append(r)
                total_cr += cgst
            if sgst:
                component = "UTGST" if config.home_state_is_ut else "SGST"
                r = dict(base)
                r["Ledger Name"] = output_tax_ledger_name(component, rate)
                r_amt, r_side = _signed_amount_fields(sgst, "Cr")
                r["Ledger Amount"] = r_amt
                r["Ledger Amount Dr/Cr"] = r_side
                rows.append(r)
                total_cr += sgst

        if cess:
            r = dict(base)
            r["Ledger Name"] = "Output CESS"
            r_amt, r_side = _signed_amount_fields(cess, "Cr")
            r["Ledger Amount"] = r_amt
            r["Ledger Amount Dr/Cr"] = r_side
            rows.append(r)
            total_cr += cess

    residual = bill_amount - total_cr
    reported_round_off = float(invoice.get("ROUNDOFFAMOUNT") or 0.0)

    if abs(residual - reported_round_off) > 1.0:
        report.gstin_state_mismatches.append({
            "VOUCHERNUMBER": voucher_no,
            "issue": f"computed round-off residual ({residual:.2f}) differs from invoice's own ROUNDOFFAMOUNT ({reported_round_off:.2f})",
        })

    if abs(residual) > 0.004:
        r = dict(base)
        r["Ledger Name"] = config.round_off_ledger_name
        r["Ledger Amount"] = round(abs(residual), 2)
        r["Ledger Amount Dr/Cr"] = "Dr" if residual < 0 else "Cr"
        rows.append(r)

    dr_total = sum(r["Ledger Amount"] for r in rows if r["Ledger Amount Dr/Cr"] == "Dr")
    cr_total = sum(r["Ledger Amount"] for r in rows if r["Ledger Amount Dr/Cr"] == "Cr")

    if abs(dr_total - cr_total) > config.balance_tolerance:
        report.balance_mismatches.append({
            "VOUCHERNUMBER": voucher_no,
            "dr_total": round(dr_total, 2),
            "cr_total": round(cr_total, 2),
            "difference": round(abs(dr_total - cr_total), 2),
        })

    report.vouchers_written += 1
    report.rows_written += len(rows)
    return rows


def generate_tally_sales_export(
    invoices: List[Dict],
    config: Optional[TallyExportConfig] = None
) -> Dict[str, Tuple[pd.DataFrame, TallyExportReport]]:
    config = config or TallyExportConfig()
    b2b_invoices, b2c_invoices = split_b2b_b2c(invoices)

    results = {}
    for mode, inv_list, columns in (
        ("B2B", b2b_invoices, B2B_COLUMNS),
        ("B2C", b2c_invoices, B2C_COLUMNS),
    ):
        report = TallyExportReport(voucher_type="Sales", mode=mode, total_invoices_in=len(inv_list))
        all_rows = []
        for inv in inv_list:
            all_rows.extend(build_sales_voucher_rows(inv, mode, config, report))
        df = pd.DataFrame(all_rows, columns=columns) if all_rows else pd.DataFrame(columns=columns)
        results[mode] = (df, report)

    return results


PURCHASE_LEDGER_OVERRIDES = {
    5: "GST PURCHASE@5%",
    18: "GST PURCHASE@18%",
}
ZERO_RATE_PURCHASE_LEDGER = "GST PURCHASE@0%"


def purchase_ledger_name(rate: float, interstate: bool) -> str:
    if rate == 0:
        return ZERO_RATE_PURCHASE_LEDGER
    if interstate:
        return f"PURCHASE IGST @{fmt_rate(rate)}%"
    return PURCHASE_LEDGER_OVERRIDES.get(rate, f"GST PURCHASE@{fmt_rate(rate)}%")


def input_tax_ledger_name(component: str, rate: float) -> str:
    if component == "IGST":
        return f"INPUT IGST @{fmt_rate(rate)}%"
    half = rate / 2
    return f"INPUT {component} {fmt_rate(half)}%"


def build_purchase_voucher_rows(
    invoice: Dict,
    mode: str,
    config: TallyExportConfig,
    report: TallyExportReport
) -> List[Dict]:
    voucher_no = invoice.get("VOUCHERNUMBER")

    if invoice.get("is_validated") is not True:
        report.skipped_not_validated.append(voucher_no)
        return []

    tax_breakup = invoice.get("tax_breakup") or []
    if not tax_breakup:
        report.skipped_no_tax_breakup.append(voucher_no)
        return []

    base = _base_purchase_row(invoice, mode, config)
    rows = []

    bill_amount = float(invoice.get("BILLAMOUNT") or 0.0)
    party_name = invoice.get("PARTYNAME")

    ref_no = invoice.get("REFERENCENUMBER") or invoice.get("VOUCHERNUMBER")
    ref_date = invoice.get("REFERENCEDATE") or invoice.get("VOUCHERDATE")

    cr_row = dict(base)
    cr_row["Ledger Name"] = party_name
    cr_amt, cr_side = _signed_amount_fields(bill_amount, "Cr")
    cr_row["Ledger Amount"] = cr_amt
    cr_row["Ledger Amount Dr/Cr"] = cr_side
    rows.append(cr_row)

    total_dr = 0.0  # signed running total, same arithmetic as before

    if mode == "B2C":
        for hsn, amt in _hsn_groups_reconciled(invoice.get("items") or [], bill_amount, rate=None, value_field="AMOUNT"):
            if not amt:
                continue
            r = dict(base)
            r["Ledger Name"] = ZERO_RATE_PURCHASE_LEDGER
            r_amt, r_side = _signed_amount_fields(amt, "Dr")
            r["Ledger Amount"] = r_amt
            r["Ledger Amount Dr/Cr"] = r_side
            if hsn:
                r["HSN/SAC Details"] = HSN_SAC_DETAILS_FIXED
                r["HSN/SAC"] = hsn
            rows.append(r)
            total_dr += amt
    else:
        for bucket in tax_breakup:
            rate = bucket.get("GSTRATE", 0)
            taxable = float(bucket.get("TAXABLEVALUE") or 0.0)
            cgst = float(bucket.get("CGSTAMOUNT") or 0.0)
            sgst = float(bucket.get("SGSTAMOUNT") or 0.0)
            igst = float(bucket.get("IGSTAMOUNT") or 0.0)
            cess = float(bucket.get("CESSAMOUNT") or 0.0)

            interstate = igst != 0

            # was `igst > 0 and (cgst > 0 or sgst > 0)` -- see the same
            # fix in build_sales_voucher_rows above.
            if igst != 0 and (cgst != 0 or sgst != 0):
                report.gstin_state_mismatches.append({
                    "VOUCHERNUMBER": voucher_no,
                    "GSTRATE": rate,
                    "issue": "both IGST and CGST/SGST populated on the same rate bucket",
                })

            tax_rates = get_tax_rates(tax_breakup, rate)

            # was `if taxable > 0:` -- a Debit Note's bucket has a
            # negative taxable value, which that comparison would drop
            # the entire ledger row for instead of writing it out flipped.
            if taxable != 0:
                ledger_name = purchase_ledger_name(rate, interstate)
                for hsn, amt in _hsn_groups_reconciled(invoice.get("items") or [], taxable, rate=rate, value_field="TAXABLEVALUE"):
                    if not amt:
                        continue
                    r = dict(base)
                    r["Ledger Name"] = ledger_name
                    r["IGST Rate"] = tax_rates.get("igst_rate", 0)
                    r["CGST Rate"] = tax_rates.get("cgst_rate", 0)
                    r["SGST/UTGST Rate"] = tax_rates.get("sgst_rate", 0)
                    r_amt, r_side = _signed_amount_fields(amt, "Dr")
                    r["Ledger Amount"] = r_amt
                    r["Ledger Amount Dr/Cr"] = r_side
                    if hsn:
                        r["HSN/SAC Details"] = HSN_SAC_DETAILS_FIXED
                        r["HSN/SAC"] = hsn
                    rows.append(r)
                    total_dr += amt

            if interstate:
                if igst:
                    r = dict(base)
                    r["Ledger Name"] = input_tax_ledger_name("IGST", rate)
                    r["IGST Rate"] = tax_rates.get("igst_rate", 0)
                    r_amt, r_side = _signed_amount_fields(igst, "Dr")
                    r["Ledger Amount"] = r_amt
                    r["Ledger Amount Dr/Cr"] = r_side
                    rows.append(r)
                    total_dr += igst
            else:
                if cgst:
                    r = dict(base)
                    r["Ledger Name"] = input_tax_ledger_name("CGST", rate)
                    r["CGST Rate"] = tax_rates.get("cgst_rate", 0)
                    r_amt, r_side = _signed_amount_fields(cgst, "Dr")
                    r["Ledger Amount"] = r_amt
                    r["Ledger Amount Dr/Cr"] = r_side
                    rows.append(r)
                    total_dr += cgst
                if sgst:
                    component = "UTGST" if config.home_state_is_ut else "SGST"
                    r = dict(base)
                    r["Ledger Name"] = input_tax_ledger_name(component, rate)
                    r["SGST/UTGST Rate"] = tax_rates.get("sgst_rate", 0)
                    r_amt, r_side = _signed_amount_fields(sgst, "Dr")
                    r["Ledger Amount"] = r_amt
                    r["Ledger Amount Dr/Cr"] = r_side
                    rows.append(r)
                    total_dr += sgst

            if cess:
                r = dict(base)
                r["Ledger Name"] = "INPUT CESS"
                r_amt, r_side = _signed_amount_fields(cess, "Dr")
                r["Ledger Amount"] = r_amt
                r["Ledger Amount Dr/Cr"] = r_side
                rows.append(r)
                total_dr += cess

    residual = bill_amount - total_dr
    reported_round_off = float(invoice.get("ROUNDOFFAMOUNT") or 0.0)

    if abs(residual - reported_round_off) > 1.0:
        report.gstin_state_mismatches.append({
            "VOUCHERNUMBER": voucher_no,
            "issue": f"computed round-off residual ({residual:.2f}) differs from invoice's own ROUNDOFFAMOUNT ({reported_round_off:.2f})",
        })

    if abs(residual) > 0.004:
        r = dict(base)
        r["Ledger Name"] = config.round_off_ledger_name
        r["Ledger Amount"] = round(abs(residual), 2)
        r["Ledger Amount Dr/Cr"] = "Dr" if residual > 0 else "Cr"
        rows.append(r)

    dr_total = sum(r["Ledger Amount"] for r in rows if r["Ledger Amount Dr/Cr"] == "Dr")
    cr_total = sum(r["Ledger Amount"] for r in rows if r["Ledger Amount Dr/Cr"] == "Cr")

    if abs(dr_total - cr_total) > config.balance_tolerance:
        report.balance_mismatches.append({
            "VOUCHERNUMBER": voucher_no,
            "dr_total": round(dr_total, 2),
            "cr_total": round(cr_total, 2),
            "difference": round(abs(dr_total - cr_total), 2),
        })

    report.vouchers_written += 1
    report.rows_written += len(rows)
    return rows


def generate_tally_purchase_export(
    invoices: List[Dict],
    config: Optional[TallyExportConfig] = None
) -> Dict[str, Tuple[pd.DataFrame, TallyExportReport]]:
    config = config or TallyExportConfig()
    b2b_invoices, b2c_invoices = split_b2b_b2c_purchase(invoices)

    results = {}
    for mode, inv_list, columns in (
        ("B2B", b2b_invoices, B2B_COLUMNS),
        ("B2C", b2c_invoices, B2C_COLUMNS),
    ):
        report = TallyExportReport(voucher_type="Purchase", mode=mode, total_invoices_in=len(inv_list))
        all_rows = []
        for inv in inv_list:
            all_rows.extend(build_purchase_voucher_rows(inv, mode, config, report))

        df = pd.DataFrame(all_rows, columns=columns) if all_rows else pd.DataFrame(columns=columns)
        results[mode] = (df, report)

    return results
