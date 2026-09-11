"""
Core reconciliation engine. Pure function of two CanonicalDoc lists in,
a result dict out -- no file I/O here.

  1. Block both sides by recipient GSTIN.
  2. Within each GSTIN block, exact doc_no match:
     - All MATCH_FIELDS within tolerance -> Exact Match
     - Any field outside tolerance      -> Partial Match (Correction Required)
  3. Fallback within same GSTIN: same doc_type + taxable_value within
     tolerance + doc_date within date_tolerance_days -> Partial Match with
     a doc-number-mismatch note.
  4. Anything still unmatched -> Books Only / Portal Only.

CANCEL/unresolved-GSTIN rows are removed upstream at extraction time
(see generic_parser.py) and never reach this function.
"""

from dataclasses import dataclass, field
from datetime import datetime
from schema import MATCH_FIELDS


@dataclass
class ReconciliationConfig:
    value_tolerance: float = 1.0
    date_tolerance_days: int = 3


@dataclass
class MatchRow:
    status: str
    books_doc: object = None
    portal_doc: object = None
    mismatch_fields: list = field(default_factory=list)
    mismatch_note: str = ""


def _within_tol(a, b, tol):
    return abs((a or 0) - (b or 0)) <= tol


def _date_diff_days(d1, d2):
    if not d1 or not d2:
        return None
    try:
        return abs((datetime.fromisoformat(d1) - datetime.fromisoformat(d2)).days)
    except Exception:
        return None


def _compare_fields(books_doc, portal_doc, tol):
    mismatches = []
    for f in MATCH_FIELDS:
        bv = getattr(books_doc, f)
        pv = getattr(portal_doc, f)
        if not _within_tol(bv, pv, tol):
            mismatches.append({
                "field": f,
                "books_value": round(bv, 2),
                "portal_value": round(pv, 2),
                "diff": round((bv or 0) - (pv or 0), 2),
            })
    return mismatches


def reconcile(books_docs, portal_docs, config: ReconciliationConfig = None):
    config = config or ReconciliationConfig()

    books_by_gstin = {}
    for d in books_docs:
        books_by_gstin.setdefault(d.gstin, {})[d.doc_no] = d
    portal_by_gstin = {}
    for d in portal_docs:
        portal_by_gstin.setdefault(d.gstin, {})[d.doc_no] = d

    exact_match, partial_match = [], []
    matched_books_keys, matched_portal_keys = set(), set()

    all_gstins = set(books_by_gstin) | set(portal_by_gstin)

    for gstin in all_gstins:
        b_map = books_by_gstin.get(gstin, {})
        p_map = portal_by_gstin.get(gstin, {})
        common_doc_nos = set(b_map) & set(p_map)
        for doc_no in common_doc_nos:
            b_doc, p_doc = b_map[doc_no], p_map[doc_no]
            mismatches = _compare_fields(b_doc, p_doc, config.value_tolerance)
            key_b, key_p = (gstin, doc_no, "b"), (gstin, doc_no, "p")
            if mismatches:
                partial_match.append(MatchRow(
                    status="Partial Match",
                    books_doc=b_doc, portal_doc=p_doc,
                    mismatch_fields=mismatches,
                    mismatch_note="Amount mismatch: " + ", ".join(m["field"] for m in mismatches),
                ))
            else:
                exact_match.append(MatchRow(status="Exact Match", books_doc=b_doc, portal_doc=p_doc))
            matched_books_keys.add(key_b)
            matched_portal_keys.add(key_p)

    remaining_books = [d for d in books_docs if (d.gstin, d.doc_no, "b") not in matched_books_keys]
    remaining_portal = [d for d in portal_docs if (d.gstin, d.doc_no, "p") not in matched_portal_keys]

    used_portal_ids = set()
    for b_doc in remaining_books:
        candidates = [
            p_doc for p_doc in remaining_portal
            if p_doc.gstin == b_doc.gstin
            and id(p_doc) not in used_portal_ids
            and p_doc.doc_type == b_doc.doc_type
            and _within_tol(b_doc.taxable_value, p_doc.taxable_value, config.value_tolerance)
        ]
        best = None
        best_dd = None
        for c in candidates:
            dd = _date_diff_days(b_doc.doc_date, c.doc_date)
            if dd is not None and dd > config.date_tolerance_days:
                continue
            if best is None or (dd or 0) < (best_dd or 999999):
                best, best_dd = c, dd
        if best is not None:
            used_portal_ids.add(id(best))
            mismatches = _compare_fields(b_doc, best, config.value_tolerance)
            note = f"Doc number mismatch: books '{b_doc.doc_no_raw}' vs portal '{best.doc_no_raw}'"
            if mismatches:
                note += "; also amount mismatch: " + ", ".join(m["field"] for m in mismatches)
            partial_match.append(MatchRow(
                status="Partial Match",
                books_doc=b_doc, portal_doc=best,
                mismatch_fields=mismatches,
                mismatch_note=note,
            ))
            matched_books_keys.add((b_doc.gstin, b_doc.doc_no, "b"))
            matched_portal_keys.add((best.gstin, best.doc_no, "p"))

    books_only = [
        MatchRow(status="Books Only", books_doc=d)
        for d in books_docs if (d.gstin, d.doc_no, "b") not in matched_books_keys
    ]
    portal_only = [
        MatchRow(status="Portal Only", portal_doc=d)
        for d in portal_docs if (d.gstin, d.doc_no, "p") not in matched_portal_keys
    ]

    return {
        "exact_match": exact_match,
        "partial_match": partial_match,
        "books_only": books_only,
        "portal_only": portal_only,
    }


def summarize(result):
    return {k: {"count": len(v)} for k, v in result.items()}
