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
        gstin=normal
