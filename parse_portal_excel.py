"""
Parses the portal's "GSTR-1 auto-populated" Excel export (sheets:
'b2b, sez, de' and 'cdnr') into CanonicalDoc records. This is the Excel
sibling of parse_portal_json.py -- same canonical output, so the
reconciler doesn't care which one was uploaded.
"""

import openpyxl
from datetime import datetime
from schema import CanonicalDoc, normalize_gstin, normalize_doc_no


def _find_header_row(ws, must_contain):
    """Scan the first ~10 rows for the header row containing `must_contain`."""
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True), start=1):
        if row and any(c == must_contain for c in row):
            return i
    raise ValueError(f"Header row with '{must_contain}' not found in sheet {ws.title}")


def _parse_date(d):
    if d is None or str(d).strip() == "":
        return None
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    s = str(d).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _num(v):
    if v is None or str(v).strip() == "":
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _rows_as_dicts(ws, header_row):
    headers = [c.value for c in ws[header_row]]
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            continue
        yield dict(zip(headers, row))


def parse_b2b_sez_de(wb):
    """Sheet 'b2b, sez, de' -> Invoice docs. One row = one invoice (already
    invoice-level; unlike the books export, portal Excel has no HSN split)."""
    if "b2b, sez, de" not in wb.sheetnames:
        return []
    ws = wb["b2b, sez, de"]
    header_row = _find_header_row(ws, "GSTIN/UIN of Recipient")
    docs = []
    for r in _rows_as_dicts(ws, header_row):
        gstin = r.get("GSTIN/UIN of Recipient")
        if not gstin:
            continue
        docs.append(CanonicalDoc(
            source="portal",
            doc_type="Invoice",
            gstin=normalize_gstin(gstin),
            party_name=r.get("Receiver Name"),
            doc_no=normalize_doc_no(r.get("Invoice number")),
            doc_no_raw=str(r.get("Invoice number", "")),
            doc_date=_parse_date(r.get("Invoice date")),
            pos=str(r.get("Place of Supply", "")).split(" - ")[0].strip() or None,
            doc_value=_num(r.get("Invoice value")),
            taxable_value=_num(r.get("Taxable Value")),
            igst=_num(r.get("Integrated Tax")),
            cgst=_num(r.get("Central Tax")),
            sgst=_num(r.get("State/UT Tax")),
            cess=_num(r.get("Cess Amount")),
            rate=_num(r.get("Rate")) or None,
            irn=r.get("IRN"),
            extra={"invoice_type": r.get("Invoice Type"), "status": r.get("E-invoice status")},
        ))
    return docs


def parse_cdnr(wb):
    """Sheet 'cdnr' -> Credit/Debit Note docs."""
    if "cdnr" not in wb.sheetnames:
        return []
    ws = wb["cdnr"]
    header_row = _find_header_row(ws, "GSTIN/UIN of Recipient")
    NOTE_TYPE_MAP = {"C": "Credit Note", "D": "Debit Note"}
    docs = []
    for r in _rows_as_dicts(ws, header_row):
        gstin = r.get("GSTIN/UIN of Recipient")
        if not gstin:
            continue
        docs.append(CanonicalDoc(
            source="portal",
            doc_type=NOTE_TYPE_MAP.get(r.get("Note Type"), "Credit Note"),
            gstin=normalize_gstin(gstin),
            party_name=r.get("Receiver Name"),
            doc_no=normalize_doc_no(r.get("Note Number")),
            doc_no_raw=str(r.get("Note Number", "")),
            doc_date=_parse_date(r.get("Note Date")),
            pos=str(r.get("Place of Supply", "")).split(" - ")[0].strip() or None,
            doc_value=_num(r.get("Note value")),
            taxable_value=_num(r.get("Taxable Value")),
            igst=_num(r.get("Integrated Tax")),
            cgst=_num(r.get("Central Tax")),
            sgst=_num(r.get("State/UT Tax")),
            cess=_num(r.get("Cess Amount")),
            rate=_num(r.get("Rate")) or None,
            irn=r.get("IRN"),
            extra={"note_supply_type": r.get("Note Supply Type"), "status": r.get("E-invoice status")},
        ))
    return docs


def parse_all(filepath):
    wb = openpyxl.load_workbook(filepath, data_only=True)
    docs = parse_b2b_sez_de(wb) + parse_cdnr(wb)
    return docs, {}, {}  # (docs, b2c_totals placeholder, meta placeholder)


if __name__ == "__main__":
    import sys
    fp = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/MARRIYA_EINV_04AFAPM2122E1ZA_2026-27.xlsx"
    docs, _, _ = parse_all(fp)
    print("Total portal(excel) docs:", len(docs))
    by_type = {}
    for d in docs:
        by_type[d.doc_type] = by_type.get(d.doc_type, 0) + 1
    print("By type:", by_type)
    print("Sample:", docs[0].as_dict())
