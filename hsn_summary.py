import pandas as pd
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass, field

@dataclass
class HSNValidationReport:
    mode: str = ""
    total_invoices: int = 0
    total_invoice_value: float = 0.0
    total_hsn_value: float = 0.0
    total_taxable_value: float = 0.0
    total_tax_amount: float = 0.0
    difference: float = 0.0
    is_valid: bool = True
    mismatched_invoices: List[Dict] = field(default_factory=list)
    missing_hsn_invoices: List[Dict] = field(default_factory=list)

def get_items(invoice):
    items = invoice.get("items") or invoice.get("lines")
    return items if items is not None else []

def is_interstate(invoice):
    for bucket in invoice.get("tax_breakup", []):
        if float(bucket.get("IGSTAMOUNT") or 0.0) > 0:
            return True
    return False

def generate_hsn_summary(invoices, mode="B2B", voucher_type="Sales", validate=True):
    filtered_invoices = []
    total_invoice_value = 0.0
    for inv in invoices:
        has_gstin = bool(inv.get("PARTYGSTIN") and str(inv.get("PARTYGSTIN")).strip())
        if (mode == "B2B" and has_gstin) or (mode == "B2C" and not has_gstin):
            if inv.get("is_validated") is True:
                filtered_invoices.append(inv)
                bill_amount = float(inv.get("BILLAMOUNT") or 0.0)
                round_off = float(inv.get("ROUNDOFFAMOUNT") or 0.0)
                total_invoice_value += bill_amount - round_off

    hsn_data = defaultdict(lambda: {
        "Description": "", "UQC": "OTH-OTHERS", "Total_Quantity": 0.0, "Total_Value": 0.0,
        "Taxable_Value": 0.0, "IGST_Amount": 0.0, "CGST_Amount": 0.0, "SGST_Amount": 0.0,
        "Cess_Amount": 0.0, "Rate": 0.0, "Rates": set(), "Invoice_Numbers": set(),
    })
    mismatched_invoices = []
    missing_hsn_invoices = []

    for inv in filtered_invoices:
        invoice_no = inv.get("VOUCHERNUMBER")
        items = get_items(inv)
        interstate = is_interstate(inv)
        bill_amount = float(inv.get("BILLAMOUNT") or 0.0)
        round_off = float(inv.get("ROUNDOFFAMOUNT") or 0.0)
        invoice_base_total = bill_amount - round_off
        if not items:
            missing_hsn_invoices.append({"VOUCHERNUMBER": invoice_no, "issue": "No line items found"})
            continue
        has_hsn = False
        for item in items:
            hsn = item.get("HSNCODE")
            if not hsn:
                continue
            has_hsn = True
            amount = float(item.get("NETAMOUNT") or item.get("AMOUNT") or 0.0)
            taxable_value = float(item.get("TAXABLEVALUE") or 0.0)
            gst_amount = float(item.get("GSTAMOUNT") or 0.0)
            quantity = float(item.get("ACTUALQTY") or 0.0)
            rate = float(item.get("GSTRATE") or 0.0)
            if interstate:
                igst_amount, cgst_amount, sgst_amount = gst_amount, 0.0, 0.0
            else:
                cgst_amount = sgst_amount = gst_amount / 2
                igst_amount = 0.0
            description = item.get("STOCKITEMNAME") or ""
            data = hsn_data[hsn]
            data["Description"] = description or data["Description"]
            data["Total_Quantity"] += quantity
            data["Total_Value"] += amount
            data["Taxable_Value"] += taxable_value
            data["IGST_Amount"] += igst_amount
            data["CGST_Amount"] += cgst_amount
            data["SGST_Amount"] += sgst_amount
            data["Rates"].add(rate)
            data["Invoice_Numbers"].add(invoice_no)
        if has_hsn and validate:
            hsn_invoice_total = sum(hsn_data[h]["Total_Value"] for h in hsn_data if invoice_no in hsn_data[h]["Invoice_Numbers"])
            if abs(round(invoice_base_total, 2) - round(hsn_invoice_total, 2)) > 0.02:
                mismatched_invoices.append({
                    "VOUCHERNUMBER": invoice_no, "BASE_TOTAL": round(invoice_base_total, 2),
                    "HSN_AGGREGATED": round(hsn_invoice_total, 2),
                })
        if not has_hsn:
            missing_hsn_invoices.append({"VOUCHERNUMBER": invoice_no, "issue": "No HSN codes found"})

    rows = []
    total_hsn_value = 0.0
    for hsn, data in hsn_data.items():
        rate = max(data["Rates"]) if data["Rates"] else 0
        rows.append({
            "HSN": hsn, "Description": data["Description"][:50], "UQC": data["UQC"],
            "Total Quantity": round(data["Total_Quantity"], 2), "Total Value": round(data["Total_Value"], 2),
            "Taxable Value": round(data["Taxable_Value"], 2),
            "Integrated Tax Amount": round(data["IGST_Amount"], 2),
            "Central Tax Amount": round(data["CGST_Amount"], 2),
            "State/UT Tax Amount": round(data["SGST_Amount"], 2),
            "Cess Amount": round(data["Cess_Amount"], 2), "Rate": rate,
        })
        total_hsn_value += round(data["Total_Value"], 2)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("HSN").reset_index(drop=True)
    difference = round(total_invoice_value, 2) - round(total_hsn_value, 2)
    validation_report = None
    if validate:
        validation_report = HSNValidationReport(
            mode=mode, total_invoices=len(filtered_invoices),
            total_invoice_value=round(total_invoice_value, 2), total_hsn_value=round(total_hsn_value, 2),
            difference=difference, is_valid=abs(difference) < 0.01,
            mismatched_invoices=mismatched_invoices, missing_hsn_invoices=missing_hsn_invoices,
        )
    return df, validation_report

def generate_all_hsn_summaries(invoices, voucher_type="Sales", validate=True):
    return {
        "B2B": generate_hsn_summary(invoices, "B2B", voucher_type, validate),
        "B2C": generate_hsn_summary(invoices, "B2C", voucher_type, validate),
    }
