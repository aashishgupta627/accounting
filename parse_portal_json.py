"""
Parses the GSTR-1 'returns_*.json' export from the GST portal into
CanonicalDoc records (B2B invoices + CDNR credit/debit notes).

B2C is intentionally NOT itemized here -- portal JSON's b2cs section
(if present) is aggregate-only by design, matched separately at
total level (see b2c_totals.py).
"""

import json
from schema import CanonicalDoc, normalize_gstin, normalize_doc_no


def _parse_date(d):
    """Portal dates are 'DD-MM-YYYY' -> ISO 'YYYY-MM-DD'."""
    if not d:
        return None
    try:
        dd, mm, yyyy = d.split("-")
        return f"{yyyy}-{mm}-{dd}"
    except Exception:
        return None


def _sum_items(itms):
    taxable = igst = cgst = sgst = cess = 0.0
    rate = None
    for it in itms:
        det = it.get("itm_det", {})
        taxable += float(det.get("txval", 0) or 0)
        igst += float(det.get("iamt", 0) or 0)
        cgst += float(det.get("camt", 0) or 0)
        sgst += float(det.get("samt", 0) or 0)
        cess += float(det.get("csamt", 0) or 0)
        if rate is None:
            rate = det.get("rt")
    return taxable, igst, cgst, sgst, cess, rate


def parse_b2b(data, party_names=None):
    """data['b2b'] -> list of CanonicalDoc (doc_type='Invoice')."""
    docs = []
    party_names = party_names or {}
    for party in data.get("b2b", []):
        ctin = normalize_gstin(party.get("ctin"))
        for inv in party.get("inv", []):
            taxable, igst, cgst, sgst, cess, rate = _sum_items(inv.get("itms", []))
            docs.append(CanonicalDoc(
                source="portal",
                doc_type="Invoice",
                gstin=ctin,
                party_name=party_names.get(ctin),
                doc_no=normalize_doc_no(inv.get("inum")),
                doc_no_raw=inv.get("inum", ""),
                doc_date=_parse_date(inv.get("idt")),
                pos=inv.get("pos"),
                doc_value=float(inv.get("val", 0) or 0),
                taxable_value=taxable,
                igst=igst, cgst=cgst, sgst=sgst, cess=cess,
                rate=rate,
                irn=inv.get("irn"),
                line_count=len(inv.get("itms", [])),
                extra={"srctyp": inv.get("srctyp"), "flag": inv.get("flag")},
            ))
    return docs


def parse_cdnr(data, party_names=None):
    """data['cdnr'] -> list of CanonicalDoc (doc_type='Credit Note'/'Debit Note')."""
    docs = []
    party_names = party_names or {}
    NTTY_MAP = {"C": "Credit Note", "D": "Debit Note"}
    for party in data.get("cdnr", []):
        ctin = normalize_gstin(party.get("ctin"))
        for nt in party.get("nt", []):
            taxable, igst, cgst, sgst, cess, rate = _sum_items(nt.get("itms", []))
            docs.append(CanonicalDoc(
                source="portal",
                doc_type=NTTY_MAP.get(nt.get("ntty"), "Credit Note"),
                gstin=ctin,
                party_name=party_names.get(ctin),
                doc_no=normalize_doc_no(nt.get("nt_num")),
                doc_no_raw=nt.get("nt_num", ""),
                doc_date=_parse_date(nt.get("nt_dt")),
                pos=nt.get("pos"),
                doc_value=float(nt.get("val", 0) or 0),
                taxable_value=taxable,
                igst=igst, cgst=cgst, sgst=sgst, cess=cess,
                rate=rate,
                irn=nt.get("irn"),
                line_count=len(nt.get("itms", [])),
                extra={"srctyp": nt.get("srctyp"), "flag": nt.get("flag")},
            ))
    return docs


def parse_b2c_totals(data):
    """
    Aggregate-only B2C figures from the portal JSON, if present (b2cs section).
    Returns a dict of totals; empty dict if no b2cs section exists.
    """
    totals = {"taxable_value": 0.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    found = False
    for row in data.get("b2cs", []):
        found = True
        totals["taxable_value"] += float(row.get("txval", 0) or 0)
        totals["igst"] += float(row.get("iamt", 0) or 0)
        totals["cgst"] += float(row.get("camt", 0) or 0)
        totals["sgst"] += float(row.get("samt", 0) or 0)
        totals["cess"] += float(row.get("csamt", 0) or 0)
    return totals if found else {}


def load(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def parse_all(filepath, party_names=None):
    """Convenience: returns (invoice_and_note_docs, b2c_totals, meta)."""
    data = load(filepath)
    docs = parse_b2b(data, party_names) + parse_cdnr(data, party_names)
    b2c_totals = parse_b2c_totals(data)
    meta = {
        "gstin": data.get("gstin"),
        "period": data.get("fp"),
        "filing_type": data.get("filing_typ"),
    }
    return docs, b2c_totals, meta


if __name__ == "__main__":
    import sys
    docs, b2c, meta = parse_all(sys.argv[1] if len(sys.argv) > 1 else
                                 "/mnt/user-data/uploads/returns_09092026_R1_04AFAPM2122E1ZA_offline_others_0.json")
    print("Meta:", meta)
    print("Total portal docs:", len(docs))
    by_type = {}
    for d in docs:
        by_type[d.doc_type] = by_type.get(d.doc_type, 0) + 1
    print("By type:", by_type)
    print("B2C totals:", b2c)
    print("Sample:", docs[0].as_dict())
