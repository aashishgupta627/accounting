"""
Tally Export Module - Generates Accounting Voucher rows from invoice JSON

Handles both Sales and Purchase vouchers with B2B/B2C classification.
"""

from dataclasses import dataclass, field
import pandas as pd
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

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

# HSN columns that will be added for each ledger entry
HSN_COLUMNS = [
    "HSN/SAC Details: Specify Details Here",  # Fixed value "Specify Details Here"
    "HSN/SAC: Provide the HSN Code here",     # Actual HSN code
]

B2B_IDENTITY_COLUMN = "Buyer/Supplier - GSTIN/UIN"
B2C_IDENTITY_COLUMN = "Buyer/Supplier - GST Registration Type"
TRAILING_COLUMNS = ["Buyer/Supplier - Country"]

# Full column sets with HSN columns
B2B_COLUMNS = COMMON_COLUMNS + [B2B_IDENTITY_COLUMN] + TRAILING_COLUMNS + HSN_COLUMNS
B2C_COLUMNS = COMMON_COLUMNS + [B2C_IDENTITY_COLUMN] + TRAILING_COLUMNS + HSN_COLUMNS


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


def has_gstin(invoice: Dict) -> bool:
    """Check if the invoice has a GSTIN."""
    gstin = invoice.get("PARTYGSTIN")
    return bool(gstin and str(gstin).strip())


def has_tax(invoice: Dict) -> bool:
    """
    Check if the invoice has any tax (GST rate > 0 with amounts > 0).
    
    Checks tax_breakup at invoice level.
    """
    tax_breakup = invoice.get("tax_breakup") or []
    for bucket in tax_breakup:
        rate = bucket.get("GSTRATE", 0)
        if rate > 0:
            taxable = float(bucket.get("TAXABLEVALUE") or 0.0)
            cgst = float(bucket.get("CGSTAMOUNT") or 0.0)
            sgst = float(bucket.get("SGSTAMOUNT") or 0.0)
            igst = float(bucket.get("IGSTAMOUNT") or 0.0)
            cess = float(bucket.get("CESSAMOUNT") or 0.0)
            
            if taxable > 0 or cgst > 0 or sgst > 0 or igst > 0 or cess > 0:
                return True
    return False


def classify_purchase_invoice(invoice: Dict) -> str:
    """
    Classify a purchase invoice as B2B or B2C.
    
    B2B: Has tax (GST rate > 0 with amounts > 0)
    B2C: No GSTIN AND no tax (all GST rates are 0 or no tax amounts)
    """
    # If there's tax, it's B2B regardless of GSTIN
    if has_tax(invoice):
        return "B2B"
    # If no tax and no GSTIN, it's B2C
    if not has_gstin(invoice):
        return "B2C"
    # If no tax but has GSTIN, it's B2B (registered dealer with 0% supply)
    return "B2B"


def classify_sales_invoice(invoice: Dict) -> str:
    """
    Classify a sales invoice as B2B or B2C.
    
    B2B: Has GSTIN
    B2C: No GSTIN (unregistered consumer)
    """
    return "B2B" if has_gstin(invoice) else "B2C"


def split_b2b_b2c_purchase(invoices: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Split purchase invoices into B2B and B2C based on tax presence."""
    b2b, b2c = [], []
    for inv in invoices:
        if inv.get("is_validated") is not True:
            continue
        if classify_purchase_invoice(inv) == "B2B":
            b2b.append(inv)
        else:
            b2c.append(inv)
    return b2b, b2c


def split_b2b_b2c_sales(invoices: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Split sales invoices into B2B and B2C based on GSTIN presence."""
    b2b, b2c = [], []
    for inv in invoices:
        if inv.get("is_validated") is not True:
            continue
        if classify_sales_invoice(inv) == "B2B":
            b2b.append(inv)
        else:
            b2c.append(inv)
    return b2b, b2c


def aggregate_by_hsn(invoice: Dict) -> Dict:
    """
    Aggregate line items by HSN code within an invoice.
    
    Uses the invoice-level tax_breakup to distribute tax amounts by HSN.
    Returns: {hsn_code: {"taxable": sum, "cgst": sum, "sgst": sum, "igst": sum, "cess": sum, "gstrate": rate}}
    """
    # Get the tax breakup from the invoice
    tax_breakup = invoice.get("tax_breakup") or []
    
    # If no tax breakup, return empty
    if not tax_breakup:
        return {}
    
    # Get items
    items = invoice.get("items") or invoice.get("lines") or []
    
    # Group items by HSN and calculate total taxable value per HSN
    hsn_taxable = defaultdict(float)
    hsn_gstrate = defaultdict(float)
    hsn_items = defaultdict(list)
    
    for item in items:
        hsn = item.get("HSNCODE")
        if not hsn:
            continue
        
        taxable = float(item.get("TAXABLEVALUE") or 0.0)
        gstrate = float(item.get("GSTRATE") or 0.0)
        
        hsn_taxable[hsn] += taxable
        if gstrate > 0:
            hsn_gstrate[hsn] = gstrate
        hsn_items[hsn].append(item)
    
    # If no HSNs found, use a single default HSN
    if not hsn_taxable:
        # Use a default HSN code
        hsn_taxable["DEFAULT"] = float(invoice.get("BILLAMOUNT") or 0.0)
        hsn_gstrate["DEFAULT"] = tax_breakup[0].get("GSTRATE", 0) if tax_breakup else 0
        hsn_items["DEFAULT"] = items
    
    # Now distribute the tax from tax_breakup across HSNs based on taxable value
    hsn_aggregates = {}
    
    # Calculate total taxable across all HSNs
    total_taxable = sum(hsn_taxable.values())
    
    if total_taxable == 0:
        # If no taxable value, distribute equally
        total_taxable = len(hsn_taxable)
    
    for hsn, taxable in hsn_taxable.items():
        gstrate = hsn_gstrate.get(hsn, 0)
        
        # If no rate from items, try to get from tax_breakup
        if gstrate == 0 and tax_breakup:
            gstrate = tax_breakup[0].get("GSTRATE", 0)
        
        # Find matching tax bucket for this rate
        cgst = 0.0
        sgst = 0.0
        igst = 0.0
        cess = 0.0
        
        for bucket in tax_breakup:
            bucket_rate = bucket.get("GSTRATE", 0)
            if bucket_rate == gstrate:
                # Calculate proportional tax based on this HSN's taxable value
                bucket_taxable = float(bucket.get("TAXABLEVALUE") or 0.0)
                if bucket_taxable > 0:
                    proportion = taxable / bucket_taxable
                    cgst = float(bucket.get("CGSTAMOUNT") or 0.0) * proportion
                    sgst = float(bucket.get("SGSTAMOUNT") or 0.0) * proportion
                    igst = float(bucket.get("IGSTAMOUNT") or 0.0) * proportion
                    cess = float(bucket.get("CESSAMOUNT") or 0.0) * proportion
                break
        
        # If no matching rate found, use proportional distribution of first bucket
        if not (cgst or sgst or igst) and tax_breakup:
            bucket = tax_breakup[0]
            bucket_taxable = float(bucket.get("TAXABLEVALUE") or 0.0)
            if bucket_taxable > 0:
                proportion = taxable / bucket_taxable
                cgst = float(bucket.get("CGSTAMOUNT") or 0.0) * proportion
                sgst = float(bucket.get("SGSTAMOUNT") or 0.0) * proportion
                igst = float(bucket.get("IGSTAMOUNT") or 0.0) * proportion
                cess = float(bucket.get("CESSAMOUNT") or 0.0) * proportion
                gstrate = bucket.get("GSTRATE", 0)
        
        hsn_aggregates[hsn] = {
            "taxable": taxable,
            "cgst": cgst,
            "sgst": sgst,
            "igst": igst,
            "cess": cess,
            "gstrate": gstrate,
        }
    
    return hsn_aggregates


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


def _base_sales_row(invoice: Dict, mode: str, config: TallyExportConfig, hsn_code: str = None) -> Dict:
    """Base row with common fields for sales vouchers."""
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
        "HSN/SAC Details: Specify Details Here": "Specify Details Here",
        "HSN/SAC: Provide the HSN Code here": hsn_code if hsn_code else None,
    }
    
    if mode == "B2B":
        row[B2B_IDENTITY_COLUMN] = invoice.get("PARTYGSTIN")
    else:
        row[B2C_IDENTITY_COLUMN] = "Unregistered/Consumer"
    
    return row


def _base_purchase_row(invoice: Dict, mode: str, config: TallyExportConfig, hsn_code: str = None) -> Dict:
    """Base row with common fields for purchase vouchers."""
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
        "HSN/SAC Details: Specify Details Here": "Specify Details Here",
        "HSN/SAC: Provide the HSN Code here": hsn_code if hsn_code else None,
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
    
    hsn_aggregates = aggregate_by_hsn(invoice)
    
    rows = []
    bill_amount = float(invoice.get("BILLAMOUNT") or 0.0)
    party_name = invoice.get("PARTYNAME")
    
    # Dr: Party ledger (no HSN)
    dr_row = _base_sales_row(invoice, mode, config)
    dr_row["Ledger Name"] = party_name
    dr_row["Ledger Amount"] = round(bill_amount, 2)
    dr_row["Ledger Amount Dr/Cr"] = "Dr"
    rows.append(dr_row)
    
    total_cr = 0.0
    
    # Cr: Sales + tax ledgers (aggregated by HSN)
    for hsn, hsn_data in hsn_aggregates.items():
        rate = hsn_data.get("gstrate", 0)
        taxable = hsn_data["taxable"]
        cgst = hsn_data["cgst"]
        sgst = hsn_data["sgst"]
        igst = hsn_data["igst"]
        cess = hsn_data["cess"]
        
        interstate = igst > 0
        
        # Sales ledger (Cr) - aggregated by HSN
        if taxable:
            r = _base_sales_row(invoice, mode, config, hsn_code=hsn)
            r["Ledger Name"] = sales_ledger_name(rate, interstate)
            r["Ledger Amount"] = round(taxable, 2)
            r["Ledger Amount Dr/Cr"] = "Cr"
            r["Item Name"] = f"HSN {hsn}"
            rows.append(r)
            total_cr += taxable
        
        # Output tax ledgers (Cr)
        if interstate:
            if igst:
                r = _base_sales_row(invoice, mode, config, hsn_code=hsn)
                r["Ledger Name"] = output_tax_ledger_name("IGST", rate)
                r["Ledger Amount"] = round(igst, 2)
                r["Ledger Amount Dr/Cr"] = "Cr"
                r["Item Name"] = f"HSN {hsn}"
                rows.append(r)
                total_cr += igst
        else:
            if cgst:
                r = _base_sales_row(invoice, mode, config, hsn_code=hsn)
                r["Ledger Name"] = output_tax_ledger_name("CGST", rate)
                r["Ledger Amount"] = round(cgst, 2)
                r["Ledger Amount Dr/Cr"] = "Cr"
                r["Item Name"] = f"HSN {hsn}"
                rows.append(r)
                total_cr += cgst
            if sgst:
                component = "UTGST" if config.home_state_is_ut else "SGST"
                r = _base_sales_row(invoice, mode, config, hsn_code=hsn)
                r["Ledger Name"] = output_tax_ledger_name(component, rate)
                r["Ledger Amount"] = round(sgst, 2)
                r["Ledger Amount Dr/Cr"] = "Cr"
                r["Item Name"] = f"HSN {hsn}"
                rows.append(r)
                total_cr += sgst
        
        if cess:
            r = _base_sales_row(invoice, mode, config, hsn_code=hsn)
            r["Ledger Name"] = "Output CESS"
            r["Ledger Amount"] = round(cess, 2)
            r["Ledger Amount Dr/Cr"] = "Cr"
            r["Item Name"] = f"HSN {hsn}"
            rows.append(r)
            total_cr += cess
    
    # Round Off
    residual = bill_amount - total_cr
    if abs(residual) > 0.004:
        r = _base_sales_row(invoice, mode, config)
        r["Ledger Name"] = config.round_off_ledger_name
        r["Ledger Amount"] = round(abs(residual), 2)
        r["Ledger Amount Dr/Cr"] = "Dr" if residual < 0 else "Cr"
        rows.append(r)
    
    # Balance check
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
    b2b_invoices, b2c_invoices = split_b2b_b2c_sales(invoices)
    
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
    5: "GST PURCHASE@5%",
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
    Build Tally rows for a purchase invoice.
    
    B2B: Dr Purchase ledger + Input tax ledgers, Cr Supplier
    B2C: Dr Purchase GST 0%, Cr Supplier
    """
    voucher_no = invoice.get("VOUCHERNUMBER")
    
    if invoice.get("is_validated") is not True:
        report.skipped_not_validated.append(voucher_no)
        return []
    
    tax_breakup = invoice.get("tax_breakup") or []
    if not tax_breakup:
        report.skipped_no_tax_breakup.append(voucher_no)
        return []
    
    # Aggregate by HSN
    hsn_aggregates = aggregate_by_hsn(invoice)
    
    rows = []
    bill_amount = float(invoice.get("BILLAMOUNT") or 0.0)
    party_name = invoice.get("PARTYNAME")
    
    # Cr: Supplier ledger (no HSN)
    cr_row = _base_purchase_row(invoice, mode, config)
    cr_row["Ledger Name"] = party_name
    cr_row["Ledger Amount"] = round(bill_amount, 2)
    cr_row["Ledger Amount Dr/Cr"] = "Cr"
    rows.append(cr_row)
    
    total_dr = 0.0
    
    # Dr: Purchase + tax ledgers
    if mode == "B2C":
        # B2C: No GSTIN and no tax - use full amount at 0%
        r = _base_purchase_row(invoice, mode, config)
        r["Ledger Name"] = ZERO_RATE_PURCHASE_LEDGER
        r["Ledger Amount"] = round(bill_amount, 2)
        r["Ledger Amount Dr/Cr"] = "Dr"
        rows.append(r)
        total_dr += bill_amount
    else:
        # B2B: Full tax breakdown aggregated by HSN
        for hsn, hsn_data in hsn_aggregates.items():
            rate = hsn_data.get("gstrate", 0)
            taxable = hsn_data["taxable"]
            cgst = hsn_data["cgst"]
            sgst = hsn_data["sgst"]
            igst = hsn_data["igst"]
            cess = hsn_data["cess"]
            
            interstate = igst > 0
            
            if igst > 0 and (cgst > 0 or sgst > 0):
                report.gstin_state_mismatches.append({
                    "VOUCHERNUMBER": voucher_no,
                    "HSNCODE": hsn,
                    "GSTRATE": rate,
                    "issue": "both IGST and CGST/SGST populated on the same HSN",
                })
            
            # Purchase ledger (Dr) - aggregated by HSN
            if taxable or rate == 0:
                r = _base_purchase_row(invoice, mode, config, hsn_code=hsn)
                r["Ledger Name"] = purchase_ledger_name(rate, interstate)
                r["Ledger Amount"] = round(taxable, 2)
                r["Ledger Amount Dr/Cr"] = "Dr"
                r["Item Name"] = f"HSN {hsn}"
                rows.append(r)
                total_dr += taxable
            
            # Input tax ledgers (Dr) - only if there's tax
            if interstate:
                if igst:
                    r = _base_purchase_row(invoice, mode, config, hsn_code=hsn)
                    r["Ledger Name"] = input_tax_ledger_name("IGST", rate)
                    r["Ledger Amount"] = round(igst, 2)
                    r["Ledger Amount Dr/Cr"] = "Dr"
                    r["Item Name"] = f"HSN {hsn}"
                    rows.append(r)
                    total_dr += igst
            else:
                if cgst:
                    r = _base_purchase_row(invoice, mode, config, hsn_code=hsn)
                    r["Ledger Name"] = input_tax_ledger_name("CGST", rate)
                    r["Ledger Amount"] = round(cgst, 2)
                    r["Ledger Amount Dr/Cr"] = "Dr"
                    r["Item Name"] = f"HSN {hsn}"
                    rows.append(r)
                    total_dr += cgst
                if sgst:
                    component = "UTGST" if config.home_state_is_ut else "SGST"
                    r = _base_purchase_row(invoice, mode, config, hsn_code=hsn)
                    r["Ledger Name"] = input_tax_ledger_name(component, rate)
                    r["Ledger Amount"] = round(sgst, 2)
                    r["Ledger Amount Dr/Cr"] = "Dr"
                    r["Item Name"] = f"HSN {hsn}"
                    rows.append(r)
                    total_dr += sgst
            
            if cess:
                r = _base_purchase_row(invoice, mode, config, hsn_code=hsn)
                r["Ledger Name"] = "INPUT CESS"
                r["Ledger Amount"] = round(cess, 2)
                r["Ledger Amount Dr/Cr"] = "Dr"
                r["Item Name"] = f"HSN {hsn}"
                rows.append(r)
                total_dr += cess
    
    # Round Off - balancing entry
    residual = bill_amount - total_dr
    
    if abs(residual) > 0.004:
        r = _base_purchase_row(invoice, mode, config)
        r["Ledger Name"] = config.round_off_ledger_name
        r["Ledger Amount"] = round(abs(residual), 2)
        # If residual > 0: Dr < Cr, need Dr Round Off
        # If residual < 0: Dr > Cr, need Cr Round Off
        r["Ledger Amount Dr/Cr"] = "Dr" if residual > 0 else "Cr"
        rows.append(r)
    
    # Balance check
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
