"""
Tally Export Module - Generates Accounting Voucher rows from invoice JSON

Handles both Sales and Purchase vouchers with B2B/B2C classification.

Reads VOUCHERDATE / PARTYSTATECODE from the invoice JSON (renamed from
DATE / STATECODE — see validate_schema.py's module docstring).
"""

from dataclasses import dataclass, field
import pandas as pd
from typing import List, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------

@dataclass
class TallyExportConfig:
    home_state_is_ut: bool = True          # True -> UTGST naming, False -> SGST naming
    country: str = "India"
    round_off_ledger_name: str = "Round Off"
    balance_tolerance: float = 0.02        # rupees; Dr/Cr mismatches beyond this are flagged

# Output column layout — matches the sample files exactly
COMMON_COLUMNS = [
    "Voucher Date", "Reference No.", "Voucher Type Name", "Voucher Number",
    "Buyer/Supplier - Address", "Buyer/Supplier - Pincode",
    "Ledger Name", "IGST Rate", "CGST Rate", "SGST/UTGST Rate",
    "Ledger Amount", "Ledger Amount Dr/Cr",
    "Item Name", "Billed Quantity", "Item Rate", "Item Rate per",
    "Voucher Narration", "Change Mode",
]
B2B_IDENTITY_COLUMN = "Buyer/Supplier - GSTIN/UIN"
B2C_IDENTITY_COLUMN = "Buyer/Supplier - GST Registration Type"
TRAILING_COLUMNS = ["Buyer/Supplier - Country", "Buyer/Supplier - State", "Buyer/Supplier - Place of Supply"]

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


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def fmt_rate(x: float) -> str:
    """5 -> '5', 2.5 -> '2.5', 9.0 -> '9'."""
    if float(x) == int(x):
        return str(int(x))
    return str(round(x, 2)).rstrip("0").rstrip(".")


def split_state(statecode):
    """'04-Chandigarh' -> ('04', 'Chandigarh'). Falls back gracefully."""
    if not statecode:
        return None, None
    s = str(statecode)
    if "-" in s:
        code, name = s.split("-", 1)
        return code.strip(), name.strip()
    return None, s.strip()


def split_b2b_b2c(invoices: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Split invoices into B2B and B2C based on PARTYGSTIN presence."""
    b2b, b2c = [], []
    for inv in invoices:
        gstin = inv.get("PARTYGSTIN")
        if gstin and str(gstin).strip():
            b2b.append(inv)
        else:
            b2c.append(inv)
    return b2b, b2c


def get_tax_rates(tax_breakup: List[Dict], rate: float) -> Dict:
    """Get CGST, SGST, IGST rates for a given GSTRATE."""
    for bucket in tax_breakup:
        if bucket.get("GSTRATE") == rate:
            return {
                "cgst_rate": float(bucket.get("CGST_RATE") or 0),
                "sgst_rate": float(bucket.get("SGST_RATE") or 0),
                "igst_rate": float(bucket.get("IGST_RATE") or 0),
            }
    return {"cgst_rate": 0, "sgst_rate": 0, "igst_rate": 0}


def _base_sales_row(invoice: Dict, mode: str, config: TallyExportConfig) -> Dict:
    """Base row with common fields for sales vouchers."""
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
        "Buyer/Supplier - State": state_name,
        "Buyer/Supplier - Place of Supply": state_name,
    }

    if mode == "B2B":
        row[B2B_IDENTITY_COLUMN] = invoice.get("PARTYGSTIN")
    else:
        row[B2C_IDENTITY_COLUMN] = "Unregistered/Consumer"

    return row


def _base_purchase_row(invoice: Dict, mode: str, config: TallyExportConfig) -> Dict:
    """Base row with common fields for purchase vouchers."""
    _, state_name = split_state(invoice.get("PARTYSTATECODE"))

    # For purchases, Reference No. and Voucher Number should be the same
    # Using REFERENCENUMBER from the invoice if available, otherwise VOUCHERNUMBER
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
        "Buyer/Supplier - State": state_name,
        "Buyer/Supplier - Place of Supply": state_name,
    }

    if mode == "B2B":
        row[B2B_IDENTITY_COLUMN] = invoice.get("PARTYGSTIN")
    else:
        row[B2C_IDENTITY_COLUMN] = "Unregistered/Consumer"

    return row


# ---------------------------------------------------------------------------
# Sales Voucher Export
# ---------------------------------------------------------------------------

SALES_LEDGER_OVERRIDES = {
    5: "Local Sales  GST 5%",   # double space — confirmed from sample
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
    """component: 'CGST' | 'SGST' | 'UTGST' | 'IGST'."""
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
    """Build Tally rows for a sales invoice. Dr: Party, Cr: Sales + Tax ledgers."""
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

    # 1. Dr: Party ledger (full bill amount)
    dr_row = dict(base)
    dr_row["Ledger Name"] = party_name
    dr_row["Ledger Amount"] = round(bill_amount, 2)
    dr_row["Ledger Amount Dr/Cr"] = "Dr"
    rows.append(dr_row)

    total_cr = 0.0

    # 2. Cr: Sales + tax ledgers
    for bucket in tax_breakup:
        rate = bucket.get("GSTRATE", 0)
        taxable = float(bucket.get("TAXABLEVALUE") or 0.0)
        cgst = float(bucket.get("CGSTAMOUNT") or 0.0)
        sgst = float(bucket.get("SGSTAMOUNT") or 0.0)
        igst = float(bucket.get("IGSTAMOUNT") or 0.0)
        cess = float(bucket.get("CESSAMOUNT") or 0.0)

        interstate = igst > 0

        if igst > 0 and (cgst > 0 or sgst > 0):
            report.gstin_state_mismatches.append({
                "VOUCHERNUMBER": voucher_no,
                "GSTRATE": rate,
                "issue": "both IGST and CGST/SGST populated on the same rate bucket",
            })

        if taxable:
            r = dict(base)
            r["Ledger Name"] = sales_ledger_name(rate, interstate)
            r["Ledger Amount"] = round(taxable, 2)
            r["Ledger Amount Dr/Cr"] = "Cr"
            rows.append(r)
            total_cr += taxable

        if interstate:
            if igst:
                r = dict(base)
                r["Ledger Name"] = output_tax_ledger_name("IGST", rate)
                r["Ledger Amount"] = round(igst, 2)
                r["Ledger Amount Dr/Cr"] = "Cr"
                rows.append(r)
                total_cr += igst
        else:
            if cgst:
                r = dict(base)
                r["Ledger Name"] = output_tax_ledger_name("CGST", rate)
                r["Ledger Amount"] = round(cgst, 2)
                r["Ledger Amount Dr/Cr"] = "Cr"
                rows.append(r)
                total_cr += cgst
            if sgst:
                component = "UTGST" if config.home_state_is_ut else "SGST"
                r = dict(base)
                r["Ledger Name"] = output_tax_ledger_name(component, rate)
                r["Ledger Amount"] = round(sgst, 2)
                r["Ledger Amount Dr/Cr"] = "Cr"
                rows.append(r)
                total_cr += sgst

        if cess:
            r = dict(base)
            r["Ledger Name"] = "Output CESS"
            r["Ledger Amount"] = round(cess, 2)
            r["Ledger Amount Dr/Cr"] = "Cr"
            rows.append(r)
            total_cr += cess

    # 3. Round Off
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

    # 4. Balance check
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
    """Generate Tally rows for sales invoices."""
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


# ---------------------------------------------------------------------------
# Purchase Voucher Export
# ---------------------------------------------------------------------------

PURCHASE_LEDGER_OVERRIDES = {
    5: "GST PURCHASE@5%",   # matching the sample exactly
    18: "GST PURCHASE@18%",
}
ZERO_RATE_PURCHASE_LEDGER = "GST PURCHASE@0%"


def purchase_ledger_name(rate: float, interstate: bool) -> str:
    """Returns the purchase ledger name for the given rate."""
    if rate == 0:
        return ZERO_RATE_PURCHASE_LEDGER
    if interstate:
        return f"PURCHASE IGST @{fmt_rate(rate)}%"
    return PURCHASE_LEDGER_OVERRIDES.get(rate, f"GST PURCHASE@{fmt_rate(rate)}%")


def input_tax_ledger_name(component: str, rate: float) -> str:
    """component: 'CGST' | 'SGST' | 'UTGST' | 'IGST'."""
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
    """
    Build Tally rows for a purchase invoice matching the sample format.

    Dr: Purchase ledger + Input tax ledgers (B2B) or Purchase GST 0% (B2C)
    Cr: Supplier ledger (full amount)

    Note: Uses Reference No. and Reference Date from the invoice.
    """
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

    # Get reference number and reference date for the voucher
    ref_no = invoice.get("REFERENCENUMBER") or invoice.get("VOUCHERNUMBER")
    ref_date = invoice.get("REFERENCEDATE") or invoice.get("VOUCHERDATE")

    # 1. Cr: Supplier ledger (full bill amount)
    cr_row = dict(base)
    cr_row["Ledger Name"] = party_name
    cr_row["Ledger Amount"] = round(bill_amount, 2)
    cr_row["Ledger Amount Dr/Cr"] = "Cr"
    rows.append(cr_row)

    total_dr = 0.0

    # 2. Dr: Purchase + tax ledgers
    if mode == "B2C":
        # B2C: No input tax, just purchase at 0%
        r = dict(base)
        r["Ledger Name"] = ZERO_RATE_PURCHASE_LEDGER
        r["Ledger Amount"] = round(bill_amount, 2)
        r["Ledger Amount Dr/Cr"] = "Dr"
        rows.append(r)
        total_dr += bill_amount
    else:
        # B2B: Full tax breakdown
        for bucket in tax_breakup:
            rate = bucket.get("GSTRATE", 0)
            taxable = float(bucket.get("TAXABLEVALUE") or 0.0)
            cgst = float(bucket.get("CGSTAMOUNT") or 0.0)
            sgst = float(bucket.get("SGSTAMOUNT") or 0.0)
            igst = float(bucket.get("IGSTAMOUNT") or 0.0)
            cess = float(bucket.get("CESSAMOUNT") or 0.0)

            interstate = igst > 0

            if igst > 0 and (cgst > 0 or sgst > 0):
                report.gstin_state_mismatches.append({
                    "VOUCHERNUMBER": voucher_no,
                    "GSTRATE": rate,
                    "issue": "both IGST and CGST/SGST populated on the same rate bucket",
                })

            # Get tax rates for the rate columns
            tax_rates = get_tax_rates(tax_breakup, rate)

            # Purchase ledger (Dr)
            if taxable > 0:
                r = dict(base)
                r["Ledger Name"] = purchase_ledger_name(rate, interstate)
                r["IGST Rate"] = tax_rates.get("igst_rate", 0)
                r["CGST Rate"] = tax_rates.get("cgst_rate", 0)
                r["SGST/UTGST Rate"] = tax_rates.get("sgst_rate", 0)
                r["Ledger Amount"] = round(taxable, 2)
                r["Ledger Amount Dr/Cr"] = "Dr"
                rows.append(r)
                total_dr += taxable

            # Input tax ledgers (Dr)
            if interstate:
                if igst:
                    r = dict(base)
                    r["Ledger Name"] = input_tax_ledger_name("IGST", rate)
                    r["IGST Rate"] = tax_rates.get("igst_rate", 0)
                    r["Ledger Amount"] = round(igst, 2)
                    r["Ledger Amount Dr/Cr"] = "Dr"
                    rows.append(r)
                    total_dr += igst
            else:
                if cgst:
                    r = dict(base)
                    r["Ledger Name"] = input_tax_ledger_name("CGST", rate)
                    r["CGST Rate"] = tax_rates.get("cgst_rate", 0)
                    r["Ledger Amount"] = round(cgst, 2)
                    r["Ledger Amount Dr/Cr"] = "Dr"
                    rows.append(r)
                    total_dr += cgst
                if sgst:
                    component = "UTGST" if config.home_state_is_ut else "SGST"
                    r = dict(base)
                    r["Ledger Name"] = input_tax_ledger_name(component, rate)
                    r["SGST/UTGST Rate"] = tax_rates.get("sgst_rate", 0)
                    r["Ledger Amount"] = round(sgst, 2)
                    r["Ledger Amount Dr/Cr"] = "Dr"
                    rows.append(r)
                    total_dr += sgst

            if cess:
                r = dict(base)
                r["Ledger Name"] = "INPUT CESS"
                r["Ledger Amount"] = round(cess, 2)
                r["Ledger Amount Dr/Cr"] = "Dr"
                rows.append(r)
                total_dr += cess

    # 3. Round Off
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
        # For purchases: if Cr > Dr (bill rounded down), Cr Round Off; if Dr > Cr, Dr Round Off
        r["Ledger Amount Dr/Cr"] = "Dr" if residual > 0 else "Cr"
        rows.append(r)

    # 4. Balance check
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
    """Generate Tally rows for purchase invoices."""
    config = config or TallyExportConfig()
    b2b_invoices, b2c_invoices = split_b2b_b2c(invoices)

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
