"""
Adapter: pipeline invoice JSON -> CanonicalDoc for GST reconciliation.

Consumes the extraction stage's output verbatim (which is already
CANCEL-free — see generic_parser module docstring). Pure shape
transformation plus one convention fix:

  * Note-sign normalization: books (Tally) records Credit/Debit Notes as
    signed revenue reversals (negative BILLAMOUNT/tax). The GST portal
    always reports note values as positive magnitudes. Normalize to
    magnitude so a sign difference doesn't show up as a false Partial
    Match on every single note.

Field mapping (pipeline -> reconciler):
  VOUCHERTYPE             -> doc_type
  PARTYGSTIN              -> gstin (uppercase)
  PARTYNAME               -> party_name
  VOUCHERNUMBER           -> doc_no / doc_no_raw
  VOUCHERDATE             -> doc_date
  PARTYSTATECODE          -> pos
  BILLAMOUNT              -> doc_value
  sum(tax_breakup.TAXABLEVALUE) -> taxable_value
  sum(tax_breakup.CGSTAMOUNT)   -> cgst
  sum(tax_breakup.SGSTAMOUNT)   -> sgst
  sum(tax_breakup.IGSTAMOUNT)   -> igst
  sum(tax_breakup.CESSAMOUNT)   -> cess
"""

from typing import Iterable, List

from schema import CanonicalDoc, normalize_gstin, normalize_doc_no


_NOTE_TYPES = {"Credit Note", "Debit Note"}


def _sum_breakup(tax_breakup, field):
    return round(sum((b.get(field) or 0.0) for b in (tax_breakup or [])), 2)


def _to_doc(inv: dict) -> CanonicalDoc:
    tax_breakup = inv.get("tax_breakup") or []
    doc_type = inv.get("VOUCHERTYPE") or "Invoice"

    doc = CanonicalDoc(
        source="books",
        doc_type=doc_type,
        gstin=normalize_gstin(inv.get("PARTYGSTIN") or ""),
        party_name=inv.get("PARTYNAME"),
        doc_no=normalize_doc_no(inv.get("VOUCHERNUMBER")),
        doc_no_raw=str(inv.get("VOUCHERNUMBER") or ""),
        doc_date=inv.get("VOUCHERDATE"),
        pos=inv.get("PARTYSTATECODE"),
        doc_value=round(float(inv.get("BILLAMOUNT") or 0.0), 2),
        taxable_value=_sum_breakup(tax_breakup, "TAXABLEVALUE"),
        igst=_sum_breakup(tax_breakup, "IGSTAMOUNT"),
        cgst=_sum_breakup(tax_breakup, "CGSTAMOUNT"),
        sgst=_sum_breakup(tax_breakup, "SGSTAMOUNT"),
        cess=_sum_breakup(tax_breakup, "CESSAMOUNT"),
        line_count=len(inv.get("items") or []),
        extra={"is_validated": inv.get("is_validated")},
    )

    # Books->portal note-sign convention: portal reports note values as
    # positive magnitudes; books often keeps them signed negative.
    if doc.doc_type in _NOTE_TYPES:
        doc.doc_value = abs(doc.doc_value)
        doc.taxable_value = abs(doc.taxable_value)
        doc.igst = abs(doc.igst)
        doc.cgst = abs(doc.cgst)
        doc.sgst = abs(doc.sgst)
        doc.cess = abs(doc.cess)

    return doc


def invoices_to_canonical(invoices: Iterable[dict]) -> List[CanonicalDoc]:
    """Extraction JSON (already CANCEL-filtered) -> list of CanonicalDoc.

    Skips invoices that would be unusable for reconciliation (no GSTIN and
    no doc number). All other invoices pass through, matching by whatever
    key they have."""
    docs = []
    for inv in invoices:
        doc = _to_doc(inv)
        if not doc.gstin and not doc.doc_no:
            continue
        docs.append(doc)
    return docs
