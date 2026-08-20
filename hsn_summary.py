"""
HSN Summary Generator - Creates HSN-wise summary reports from invoice JSON data.

Generates reports in the same format as the sample CSV files:
- B2B HSN summary
- B2C HSN summary

Includes validation to ensure HSN summary totals match invoice totals.
"""

import pandas as pd
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass, field


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
    
    Args:
        invoices: List of invoice dictionaries from the JSON
        mode: "B2B" or "B2C"
        voucher_type: "Sales" or "Purchase"
        validate: If True, validates totals against invoice totals
    
    Returns:
        Tuple of (DataFrame with HSN summary, ValidationReport or None)
    """
    
    # Filter invoices by mode (B2B/B2C based on GSTIN presence)
    filtered_invoices = []
    total_invoice_value = 0.0
    total_invoice_taxable = 0.0
    total_invoice_tax = 0.0
    
    for inv in invoices:
        has_gstin = bool(inv.get("PARTYGSTIN") and str(inv.get("PARTYGSTIN")).strip())
        if (mode == "B2B" and has_gstin) or (mode == "B2C" and not has_gstin):
            if inv.get("is_validated") is True:
                filtered_invoices.append(inv)
                total_invoice_value += float(inv.get("BILLAMOUNT") or 0.0)
                
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
        "Total_Value": 0.0,  # NETAMOUNT (amount after discount, inclusive of tax)
        "Taxable_Value": 0.0,  # TAXABLEVALUE (amount without tax)
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
        interstate = is_interstate(inv)  # True = IGST, False = CGST+SGST
        
        if not items:
            missing_hsn_invoices.append({
                "VOUCHERNUMBER": invoice_no,
                "PARTYNAME": inv.get("PARTYNAME"),
                "BILLAMOUNT": inv.get("BILLAMOUNT"),
                "issue": "No line items found"
            })
            continue
        
        # Track if this invoice has any HSN codes
        has_hsn = False
        invoice_total_value = 0.0
        invoice_taxable_value = 0.0
        invoice_gst_amount = 0.0
        
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
            
            invoice_total_value += amount
            invoice_taxable_value += taxable_value
            invoice_gst_amount += gst_amount
            
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
        
        # Validate this invoice's totals against HSN aggregation
        if has_hsn and validate:
            hsn_invoice_total = sum(
                hsn_data[hsn]["Total_Value"] 
                for hsn in hsn_data 
                if invoice_no in hsn_data[hsn]["Invoice_Numbers"]
            )
            
            hsn_invoice_taxable = sum(
                hsn_data[hsn]["Taxable_Value"] 
                for hsn in hsn_data 
                if invoice_no in hsn_data[hsn]["Invoice_Numbers"]
            )
            
            if abs(invoice_total_value - hsn_invoice_total) > 0.01:
                mismatched_invoices.append({
                    "VOUCHERNUMBER": invoice_no,
                    "PARTYNAME": inv.get("PARTYNAME"),
                    "INVOICE_TOTAL": round(invoice_total_value, 2),
                    "HSN_AGGREGATED": round(hsn_invoice_total, 2),
                    "DIFFERENCE": round(invoice_total_value - hsn_invoice_total, 2),
                })
        
        if not has_hsn:
            missing_hsn_invoices.append({
                "VOUCHERNUMBER": invoice_no,
                "PARTYNAME": inv.get("PARTYNAME"),
                "BILLAMOUNT": inv.get("BILLAMOUNT"),
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
        
        total_hsn_value += data["Total_Value"]
        total_hsn_taxable += data["Taxable_Value"]
        total_hsn_tax += data["IGST_Amount"] + data["CGST_Amount"] + data["SGST_Amount"] + data["Cess_Amount"]
    
    # Sort by HSN
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("HSN").reset_index(drop=True)
    
    # Create validation report
    validation_report = None
    if validate:
        validation_report = HSNValidationReport(
            mode=mode,
            total_invoices=len(filtered_invoices),
            total_invoice_value=total_invoice_value,
            total_hsn_value=total_hsn_value,
            total_taxable_value=total_hsn_taxable,
            total_tax_amount=total_hsn_tax,
            difference=total_invoice_value - total_hsn_value,
            is_valid=abs(total_invoice_value - total_hsn_value) < 0.01,
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
