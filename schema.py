"""
Canonical schema for GST sales reconciliation (Books <-> GSTR-1 Portal).

Every parser (books / portal-JSON / portal-Excel) normalizes its source into
a list of CanonicalDoc records. The reconciler only ever operates on this
shape, so a new source format = a new parser module, not a new reconciler.
"""

from dataclasses import dataclass, field
from typing import Optional


# Fields compared during matching (in rupees). Keep this list in one place
# so the reconciler, the tolerance check, and the Excel diff columns all
# stay in sync.
MATCH_FIELDS = ["doc_value", "taxable_value", "igst", "cgst", "sgst", "cess"]


@dataclass
class CanonicalDoc:
    source: str                # 'books' or 'portal'
    doc_type: str               # 'Invoice' | 'Credit Note' | 'Debit Note'
    gstin: str                  # recipient GSTIN, normalized (upper, stripped)
    party_name: Optional[str] = None
    doc_no: str = ""             # normalized doc/invoice/note number
    doc_no_raw: str = ""         # original, for display
    doc_date: Optional[str] = None   # ISO 'YYYY-MM-DD' or None if unparseable
    pos: Optional[str] = None    # place of supply code, e.g. '03'
    doc_value: float = 0.0
    taxable_value: float = 0.0
    igst: float = 0.0
    cgst: float = 0.0
    sgst: float = 0.0
    cess: float = 0.0
    rate: Optional[float] = None     # blended/primary rate, informational only
    irn: Optional[str] = None
    line_count: int = 1          # how many source rows/items were aggregated into this doc
    extra: dict = field(default_factory=dict)  # anything source-specific, for debugging

    def key(self):
        """Primary match key: recipient GSTIN + doc number."""
        return (self.gstin, self.doc_no)

    def as_dict(self):
        d = {
            "Doc Type": self.doc_type,
            "GSTIN": self.gstin,
            "Party Name": self.party_name or "",
            "Doc No": self.doc_no_raw or self.doc_no,
            "Doc Date": self.doc_date or "",
            "Place of Supply": self.pos or "",
            "Doc Value": round(self.doc_value, 2),
            "Taxable Value": round(self.taxable_value, 2),
            "IGST": round(self.igst, 2),
            "CGST": round(self.cgst, 2),
            "SGST": round(self.sgst, 2),
            "CESS": round(self.cess, 2),
        }
        return d


def normalize_gstin(gstin) -> str:
    if gstin is None:
        return ""
    return str(gstin).strip().upper()


def normalize_doc_no(doc_no) -> str:
    """
    Normalize invoice/note numbers for matching:
    - strip whitespace, uppercase
    - collapse repeated internal whitespace
    - strip a leading apostrophe some exports add to force text
    """
    if doc_no is None:
        return ""
    s = str(doc_no).strip().upper().lstrip("'")
    s = " ".join(s.split())
    return s
