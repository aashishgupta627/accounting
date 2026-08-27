"""
HSN Summary Generator - Creates HSN-wise summary reports from invoice JSON data.

Generates reports in the same format as the sample CSV files:
- B2B HSN summary
- B2C HSN summary

Includes validation to ensure HSN summary totals match invoice totals.
Uses the same B2B/B2C classification logic as tally_export.py for consistency.
"""

import pandas as pd
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass, field

# Import classification functions from tally_export for consistency
from tally_export import (
    classify_sales_invoice,
    classify_purchase_invoice,
    has_gstin,
    has_tax,
    split_b2b_b2c_sales,
    split_b2b_b2c_purchase,
)


@dataclass
class HSNValidationReport:
    """Report of HSN summary validation results."""
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


def get_items(invoice: Dict) -> List[Dict]:
    """Get items from invoice, handling both 'items' and 'lines' keys."""
    items = invoice.get("items") or invoice.get("lines")
    if items is None:
        return []
    return items


def is_interstate(invoice: Dict) -> bool:
    """
    Determine if invoice is interstate (IGST) or intrastate (CGST+SGST).
    Returns: True if interstate (has IGST), False if intrastate (has CGST+SGST)
    """
    tax_breakup = invoice.get("tax_breakup", [])
    for bucket in tax_breakup:
        if float(bucket.get("IGSTAMOUNT") or 0.0) > 0:
            return True
    return False


def generate_hsn_summary(
    invoices: List[Dict],
    mode: str = "B2B",
    voucher_type: str = "Sales",
    validate: bool = True
) -> Tuple[pd.DataFrame, Optional[HSNValidationReport]]:
    """
    Generate HSN-wise summary from invoice data.
    
    Uses BILLAMOUNT - ROUNDOFFAMOUNT as the base total for each invoice.
    This matches the sum of item NETAMOUNTs.
    
    Classification logic matches tally_export.py:
    - Sales B2B: Has GSTIN
    - Sales B2C: No GSTIN
    - Purchase B2B: Has tax (GST rate > 0) OR has GSTIN
    - Purchase B2C: No GSTIN AND no tax
    """
    
    # Filter invoices by mode using the appropriate classification
    filtered_invoices = []
    total_invoice_value = 0.0
    total_invoice_taxable = 0.0
    total_invoice_tax = 0.0
    
    for inv in invoices:
        # Skip non-validated invoices
        if inv.get("is_validated") is not True:
            continue
        
        # Determine if this invoice belongs to the requested mode
        if voucher_type == "Sales":
            inv_mode = classify_sales_invoice(inv)
        else:  # Purchase
            inv_mode = classify_purchase_invoice(inv)
        
        if inv_mode != mode:
            continue
        
        filtered_invoices.append(inv)
        
        # Use BILLAMOUNT - ROUNDOFFAMOUNT as the base total
        bill_amount = float(inv.get("BILLAMOUNT") or 0.0)
        round_off = float(inv.get("ROUNDOFFAMOUNT") or 0.0)
        base_total = bill_amount - round_off
        total_invoice_value += base_total
        
        # Calculate taxable and tax from tax_breakup
        tax_breakup = inv.get("tax_breakup", [])
        for bucket in tax_breakup:
            total_invoice_taxable += float(bucket.get("TAXABLEVALUE") or 0.0)
            total_invoice_tax += (
                float(bucket.get("CGSTAMOUNT") or 0.0) +
                float(bucket.get("SGSTAMOUNT") or 0.0) +
                float(bucket.get("IGSTAMOUNT") or 0.0) +
                float(bucket.get("CESSAMOUNT") or 0.0)
            )
    
    # Aggregate by HSN code
    hsn_data = defaultdict(lambda: {
        "Description": "",
        "UQC": "OTH-OTHERS",
        "Total_Quantity": 0.0,
        "Total_Value": 0.0,  # Sum of NETAMOUNTs from items
        "Taxable_Value": 0.0,
        "IGST_Amount": 0.0,
        "CGST_Amount": 0.0,
        "SGST_Amount": 0.0,
        "Cess_Amount": 0.0,
        "Rate": 0.0,
        "Rates": set(),
        "Invoice_Numbers": set(),
    })
    
    # For validation
    mismatched_invoices = []
    missing_hsn_invoices = []
    
    for inv in filtered_invoices:
        invoice_no = inv.get("VOUCHERNUMBER")
        items = get_items(inv)
        interstate = is_interstate(inv)
        
        # Get the base total from the invoice
        bill_amount = float(inv.get("BILLAMOUNT") or 0.0)
        round_off = float(inv.get("ROUNDOFFAMOUNT") or 0.0)
        invoice_base_total = bill_amount - round_off
        
        if not items:
            missing_hsn_invoices.append({
                "VOUCHERNUMBER": invoice_no,
                "PARTYNAME": inv.get("PARTYNAME"),
                "BILLAMOUNT": bill_amount,
                "ROUNDOFFAMOUNT": round_off,
                "BASE_TOTAL": invoice_base_total,
                "issue": "No line items found"
            })
            continue
        
        # Track if this invoice has any HSN codes
        has_hsn = False
        invoice_hsn_total = 0.0
        invoice_taxable_total = 0.0
        invoice_gst_total = 0.0
        
        for item in items:
            # Get HSN
            hsn = item.get("HSNCODE") or item.get("HSN") or item.get("HSN_CODE")
            if not hsn:
                continue
            
            has_hsn = True
            
            # Get values - use NETAMOUNT for Total Value
            amount = float(item.get("NETAMOUNT") or item.get("AMOUNT") or 0.0)
            taxable_value = float(item.get("TAXABLEVALUE") or item.get("Taxable_Value") or 0.0)
            gst_amount = float(item.get("GSTAMOUNT") or item.get("GST_Amount") or 0.0)
            quantity = float(item.get("ACTUALQTY") or item.get("Actual_Quantity") or 0.0)
            rate = float(item.get("GSTRATE") or item.get("GST_Rate") or 0.0)
            
            invoice_hsn_total += amount
            invoice_taxable_total += taxable_value
            invoice_gst_total += gst_amount
            
            # Split GST based on interstate or intrastate
            if interstate:
                # Interstate: All GST goes to IGST
                igst_amount = gst_amount
                cgst_amount = 0.0
                sgst_amount = 0.0
            else:
                # Intrastate: Split equally between CGST and SGST
                cgst_amount = gst_amount / 2
                sgst_amount = gst_amount / 2
                igst_amount = 0.0
            
            # Get description
            description = item.get("STOCKITEMNAME") or item.get("Item_Name") or ""
            
            # Aggregate by HSN
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
        
        # Validate this invoice's totals
        if has_hsn and validate:
            hsn_invoice_total = sum(
                hsn_data[hsn]["Total_Value"] 
                for hsn in hsn_data 
                if invoice_no in hsn_data[hsn]["Invoice_Numbers"]
            )
            
            # Round both to 2 decimals for comparison
            invoice_base_total_rounded = round(invoice_base_total, 2)
            hsn_invoice_total_rounded = round(hsn_invoice_total, 2)
            
            if abs(invoice_base_total_rounded - hsn_invoice_total_rounded) > 0.02:
                mismatched_invoices.append({
                    "VOUCHERNUMBER": invoice_no,
                    "PARTYNAME": inv.get("PARTYNAME"),
                    "BILLAMOUNT": bill_amount,
                    "ROUNDOFFAMOUNT": round_off,
                    "BASE_TOTAL": invoice_base_total_rounded,
                    "HSN_AGGREGATED": hsn_invoice_total_rounded,
                    "DIFFERENCE": round(invoice_base_total_rounded - hsn_invoice_total_rounded, 2),
                })
        
        if not has_hsn:
            missing_hsn_invoices.append({
                "VOUCHERNUMBER": invoice_no,
                "PARTYNAME": inv.get("PARTYNAME"),
                "BILLAMOUNT": bill_amount,
                "ROUNDOFFAMOUNT": round_off,
                "BASE_TOTAL": invoice_base_total,
                "issue": "No HSN codes found in line items"
            })
    
    # Build DataFrame
    rows = []
    total_hsn_value = 0.0
    total_hsn_taxable = 0.0
    total_hsn_tax = 0.0
    
    for hsn, data in hsn_data.items():
        rates = data["Rates"]
        rate = max(rates) if rates else 0
        
        rows.append({
            "HSN": hsn,
            "Description": data["Description"][:50] if data["Description"] else "",
            "UQC": data["UQC"],
            "Total Quantity": round(data["Total_Quantity"], 2),
            "Total Value": round(data["Total_Value"], 2),
            "Taxable Value": round(data["Taxable_Value"], 2),
            "Integrated Tax Amount": round(data["IGST_Amount"], 2),
            "Central Tax Amount": round(data["CGST_Amount"], 2),
            "State/UT Tax Amount": round(data["SGST_Amount"], 2),
            "Cess Amount": round(data["Cess_Amount"], 2),
            "Rate": rate,
        })
        
        total_hsn_value += round(data["Total_Value"], 2)
        total_hsn_taxable += round(data["Taxable_Value"], 2)
        total_hsn_tax += (
            round(data["IGST_Amount"], 2) + 
            round(data["CGST_Amount"], 2) + 
            round(data["SGST_Amount"], 2) + 
            round(data["Cess_Amount"], 2)
        )
    
    # Sort by HSN
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("HSN").reset_index(drop=True)
    
    # Calculate the difference
    total_invoice_value_rounded = round(total_invoice_value, 2)
    total_hsn_value_rounded = round(total_hsn_value, 2)
    difference = total_invoice_value_rounded - total_hsn_value_rounded
    
    # Create validation report
    validation_report = None
    if validate:
        validation_report = HSNValidationReport(
            mode=mode,
            total_invoices=len(filtered_invoices),
            total_invoice_value=total_invoice_value_rounded,
            total_hsn_value=total_hsn_value_rounded,
            total_taxable_value=round(total_hsn_taxable, 2),
            total_tax_amount=round(total_hsn_tax, 2),
            difference=difference,
            is_valid=abs(difference) < 0.01,
            mismatched_invoices=mismatched_invoices,
            missing_hsn_invoices=missing_hsn_invoices
        )
    
    return df, validation_report


def generate_hsn_summary_b2b(
    invoices: List[Dict], 
    voucher_type: str = "Sales",
    validate: bool = True
) -> Tuple[pd.DataFrame, Optional[HSNValidationReport]]:
    """Generate B2B HSN summary with validation."""
    return generate_hsn_summary(invoices, mode="B2B", voucher_type=voucher_type, validate=validate)


def generate_hsn_summary_b2c(
    invoices: List[Dict], 
    voucher_type: str = "Sales",
    validate: bool = True
) -> Tuple[pd.DataFrame, Optional[HSNValidationReport]]:
    """Generate B2C HSN summary with validation."""
    return generate_hsn_summary(invoices, mode="B2C", voucher_type=voucher_type, validate=validate)


def generate_all_hsn_summaries(
    invoices: List[Dict], 
    voucher_type: str = "Sales",
    validate: bool = True
) -> Dict[str, Tuple[pd.DataFrame, Optional[HSNValidationReport]]]:
    """Generate both B2B and B2C HSN summaries with validation."""
    return {
        "B2B": generate_hsn_summary_b2b(invoices, voucher_type, validate),
        "B2C": generate_hsn_summary_b2c(invoices, voucher_type, validate),
    }
