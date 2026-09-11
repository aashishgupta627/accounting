"""
Example mappings shared by app.py (Streamlit harness) and the CLI
(run_reconciliation.py). Keeping them here means a mapping edit lives in
exactly one place.
"""

# ---------------------------------------------------------------------------
# Example mappings for the two real files already validated end-to-end
# against Sales_July.xlsx and Purchase_July.xlsx (317/317 and 1181/1181
# invoices reconciled, 0 Dr/Cr balance mismatches on either Tally export),
# plus GST_Summary_Daily-II_july_2026.xls (single_sheet_grouped_blocks,
# 550/550 invoices reconciled — 435 Sales + 115 Credit Note, 0 mismatches,
# totals tie out exactly to the sheet's own Grand Total row: qty +8060,
# Tot-Amt 10094369.72, Taxable-Amt 8554948.18).
#
# SCHEMA NOTE: DATE -> VOUCHERDATE, STATECODE -> PARTYSTATECODE (see
# validate_schema.py's module docstring). BATCHNAME/EXPIRYDATE/FREEQTY are
# no longer core item fields — they're captured via extra_fields on both
# item mappings below, same as MARGIN1/MARGIN2/COST already were.
#
# COLUMN-OFFSET FIXES (found by testing against the real files, not just
# the column-shape samples): Purchase item mapping's DISCOUNT was pointing
# at "DIS%" (col 13, a percentage) instead of "DIS AMT" (col 14, the real
# currency amount) — now col 14. MARGIN1/MARGIN2/COST were off by one
# column (25/26/27, where 25 is actually "CATEGORY") — now 26/27/28. Both
# mappings' header_row/data_start_row were also pointing above the real
# 5-row report letterhead in the actual monthly exports — now header_row=5
# (summary) / header_rows=[4,5], data_start_row=6 (items) for both Sales
# and Purchase.
#
# GST SUMMARY MAPPING NOTES (single_sheet_grouped_blocks):
#   - block_header_marker uses columns_present=[0] only (not [0,1] — B2C
#     account blocks have a blank GSTIN cell at the header row, so
#     requiring col 1 non-blank would drop those blocks). columns_blank
#     now includes col 5 (Tot-Qty.) alongside col 2 (Date): real account
#     header rows have it blank, but "Total :" / "Grand Total :" subtotal
#     rows have col 0 non-blank AND col 2 blank too — col 5 is what
#     actually distinguishes a header row from a subtotal row here.
#   - GSTRATE has no dedicated column; it's derived from which of the six
#     S.(0%)..S.(28%) rate-bucket columns is non-zero per row (confirmed:
#     exactly one is, on every row) via rate_bucket_columns.
#   - ACTUALQTY is stored negative for normal sales and positive for
#     credit notes in this export (inverted vs. accounting convention) —
#     sign_flip_fields corrects it. Confirmed against the real file: every
#     other amount column (Tot-Amt/Taxable-Amt/CGST/SGST/IGST/GST-Amt) is
#     already the sane way round — positive for a normal sale row,
#     negative for a CRN- row — no flip needed there.
#   - VOUCHERTYPE is derived from the voucher-number prefix: CRN- ->
#     "Credit Note", everything else -> "Sales". sign_base_type: "Sales"
#     cross-checks that against the sign of the invoice's own BILLAMOUNT
#     once it's computed (see generic_parser.resolve_voucher_type) — on
#     this file the two signals agree on every row checked.
#   - AMOUNT is mapped to the same column as TAXABLEVALUE (col 7,
#     Taxable-Amt.) since this vendor's "Amount" concept for HSN-grouping
#     purposes is the taxable value, not the tax-inclusive Tot-Amt.
#     NETAMOUNT carries Tot-Amt. (col 6, tax-inclusive) instead.
#   - PARTYGSTIN gets sanity-checked against a real GSTIN shape by
#     generic_parser.clean_gstin() — this file puts a bare state
#     abbreviation ("PB") in the GSTIN column for B2C rows instead of
#     leaving it blank; without that check those rows would be
#     misclassified as B2B.
# ---------------------------------------------------------------------------

EXAMPLES = {
    "Purchase (two_sheet_joined)": {
        "layout_type": "two_sheet_joined",
        "item_mapping": {
            "sheet_type": "item_details", "header_rows": [4, 5], "data_start_row": 6,
            "invoice_block_marker": {
                "column": 0, "pattern": r"[A-Z]{2,4}/\d+",
                "blob_extract": {
                    "VOUCHERDATE": r"(?P<v>\d{2}-[A-Za-z]{3}-\d{2})",
                    "VOUCHERNUMBER": r"(?P<v>[A-Z]{2,4}/\d+)",
                    "PARTYNAME": r"[A-Z]{2,4}/\d+\s+(?P<v>.+?)\s+User",
                },
            },
            "item_row_column_map": {
                "STOCKITEMNAME": 2, "ACTUALQTY": 8,
                "RATE": 10, "GSTRATE": 19, "AMOUNT": 21, "DISCOUNT": 14,
                "GSTAMOUNT": 22, "HSNCODE": 23,
            },
            "extra_fields": {"BATCHNAME": 5, "EXPIRYDATE": 7, "FREEQTY": 9, "MARGIN1": 26, "MARGIN2": 27, "COST": 28},
            "skip_row_rules": [
                {"column": 2, "equals_normalized": "TOTAL:"},
                {"column": 2, "equals_normalized": "GRAND TOTAL:"},
            ],
            "confidence": 0.9,
            "voucher_type": "Purchase",
        },
        "summary_mapping": {
            "sheet_type": "consolidated_summary", "header_row": 5,
            "voucher_type": "Purchase",
            "footer_marker": {"column": 0, "equals_normalized": "Total :"},
            "voucher_number_pattern": r"^[A-Z]{2,4}/\d+$",
            "column_map": {
                "VOUCHERDATE": 3, "VOUCHERNUMBER": 1, "PARTYGSTIN": 2, "PARTYNAME": 6,
                "BILLAMOUNT": 7, "ROUNDOFFAMOUNT": 8, "PARTYSTATECODE": 32,
                "REFERENCENUMBER": 4, "REFERENCEDATE": 0,
            },
            "tax_rate_breakup": [
                {"GSTRATE": 5, "TAXABLEVALUE": 10, "CGSTAMOUNT": 11, "SGSTAMOUNT": 12, "IGSTAMOUNT": 13},
                {"GSTRATE": 12, "TAXABLEVALUE": 14, "CGSTAMOUNT": 15, "SGSTAMOUNT": 16, "IGSTAMOUNT": 17},
                {"GSTRATE": 18, "TAXABLEVALUE": 18, "CGSTAMOUNT": 19, "SGSTAMOUNT": 20, "IGSTAMOUNT": 21},
                {"GSTRATE": 28, "TAXABLEVALUE": 22, "CGSTAMOUNT": 23, "SGSTAMOUNT": 24, "IGSTAMOUNT": 25},
                {"GSTRATE": 40, "TAXABLEVALUE": 26, "CGSTAMOUNT": 27, "SGSTAMOUNT": 28, "IGSTAMOUNT": 29},
                {"GSTRATE": 0, "TAXABLEVALUE": 30},
            ],
            "confidence": 0.93,
        },
        "transform": {"type": "identity"},
        "item_sheet_name": "Item Details",
        "summary_sheet_name": "Consolidated Summary",
    },
    "Sales (two_sheet_joined)": {
        "layout_type": "two_sheet_joined",
        "item_mapping": {
            "sheet_type": "item_details", "header_rows": [4, 5], "data_start_row": 6,
            "invoice_block_marker": {
                "column": 2, "pattern": r"S0/\d+",
                "fields": {
                    "VOUCHERNUMBER": 2, "VOUCHERDATE": 0, "PARTYNAME": 3,
                    "ROUNDOFFAMOUNT": 15, "BILLAMOUNT": 16,
                },
            },
            "item_row_column_map": {
                "STOCKITEMNAME": 1, "ACTUALQTY": 8,
                "GSTRATE": 10, "RATE": 11, "AMOUNT": 12, "DISCOUNT": 13,
                "TAXABLEVALUE": 14, "NETAMOUNT": 15, "HSNCODE": 17, "GSTAMOUNT": 18,
            },
            "extra_fields": {"BATCHNAME": 3, "EXPIRYDATE": 5, "FREEQTY": 9},
            "skip_row_rules": [],
            "confidence": 0.92,
            "voucher_type": "Sales",
        },
        "summary_mapping": {
            "sheet_type": "consolidated_summary", "header_row": 5,
            "voucher_type": "Sales",
            "footer_marker": {"column": 0, "equals_normalized": "Total :"},
            "voucher_number_pattern": r"^S0-\d+-\d+$",
            "column_map": {
                "VOUCHERDATE": 0, "VOUCHERNUMBER": 1, "PARTYNAME": 3, "PARTYGSTIN": 4,
                "BILLAMOUNT": 6, "ROUNDOFFAMOUNT": 7, "PARTYSTATECODE": 32,
            },
            "tax_rate_breakup": [
                {"GSTRATE": 5, "TAXABLEVALUE": 9, "CGSTAMOUNT": 10, "SGSTAMOUNT": 11, "IGSTAMOUNT": 12},
                {"GSTRATE": 12, "TAXABLEVALUE": 13, "CGSTAMOUNT": 14, "SGSTAMOUNT": 15, "IGSTAMOUNT": 16},
                {"GSTRATE": 18, "TAXABLEVALUE": 17, "CGSTAMOUNT": 18, "SGSTAMOUNT": 19, "IGSTAMOUNT": 20},
                {"GSTRATE": 28, "TAXABLEVALUE": 21, "CGSTAMOUNT": 22, "SGSTAMOUNT": 23, "IGSTAMOUNT": 24},
                {"GSTRATE": 40, "TAXABLEVALUE": 25, "CGSTAMOUNT": 26, "SGSTAMOUNT": 27, "IGSTAMOUNT": 28},
                {"GSTRATE": 0, "TAXABLEVALUE": 29},
            ],
            "confidence": 0.93,
        },
        "transform": {"type": "regex_extract", "pattern": r"-(\d+)$", "template": "S0/{1}"},
        "item_sheet_name": "Item Details",
        "summary_sheet_name": "Consolidated Summary",
    },
    "GST Summary Daily (single_sheet_grouped_blocks)": {
        "layout_type": "single_sheet_grouped_blocks",
        "ingest_mapping": {
            "block_header_marker": {"columns_present": [0], "columns_blank": [2, 5]},
            "block_footer_marker": {"column": 0, "contains": "Total"},
            "forward_fill_columns": {"PARTYNAME": 0, "PARTYGSTIN": 1},
        },
        "grouped_mapping": {
            "sheet_type": "single_sheet_grouped_blocks",
            "line_identifier_field": "HSNCODE",
            "column_map": {
                "PARTYNAME": 0, "PARTYGSTIN": 1, "VOUCHERDATE": 2, "VOUCHERNUMBER": 3,
                "HSNCODE": 4, "ACTUALQTY": 5, "NETAMOUNT": 6,
                "TAXABLEVALUE": 7, "AMOUNT": 7,
                "CGSTAMOUNT": 8, "SGSTAMOUNT": 9, "IGSTAMOUNT": 10, "CESSAMOUNT": 11,
                "GSTAMOUNT": 12,
            },
            "rate_bucket_columns": {0: 13, 3: 14, 5: 15, 12: 16, 18: 17, 28: 18},
            "sign_flip_fields": ["ACTUALQTY"],
            "voucher_type_rule": {
                "pattern": "^CRN", "match_value": "Credit Note", "default": "Sales",
                "sign_base_type": "Sales",
            },
            "extra_fields": {"Is-Cash": 27},
            "confidence": 0.95,
        },
        "sheet_name": "ORIGINAL",
        "header_row": 6,
    },
}

# Convenience aliases for the CLI and reconciliation tab.
SALES_TWO_SHEET_EXAMPLE = EXAMPLES["Sales (two_sheet_joined)"]
GST_SUMMARY_DAILY_EXAMPLE = EXAMPLES["GST Summary Daily (single_sheet_grouped_blocks)"]
