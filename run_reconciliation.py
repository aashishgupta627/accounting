"""
End-to-end CLI entry point for GST Sales Reconciliation.

Books side is parsed through the CAKube extraction pipeline (orchestrator)
using the appropriate layout mapping -- either `single_sheet_grouped_blocks`
(GST Summary Daily) or `two_sheet_joined` (Sales_July style). The pipeline
output is already CANCEL-free (see generic_parser.py module docstring); the
adapter just reshapes it into CanonicalDoc for the reconciler.

Portal side is auto-detected: JSON (returns_*.json) vs portal auto-populated
Excel (b2b, sez, de + cdnr sheets).

Usage:
    python3 run_reconciliation.py \
        --books <path> --books-layout single_sheet_grouped_blocks|two_sheet_joined \
        --portal <path> --out output.xlsx \
        [--tolerance 1.0] [--date-tolerance-days 3]
"""

import argparse
import os

import pandas as pd

import parse_portal_json
import parse_portal_excel
from adapter_books import invoices_to_canonical
from reconcile import reconcile, ReconciliationConfig
from export_excel import export
from orchestrator import (
    run_single_sheet_grouped_blocks,
    run_two_sheet_joined,
)

# The two example mappings the Streamlit app already carries, so the CLI
# can run without any mapping JSON on disk.
from app_examples import GST_SUMMARY_DAILY_EXAMPLE, SALES_TWO_SHEET_EXAMPLE


def load_portal(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".json":
        return parse_portal_json.parse_all(filepath)
    return parse_portal_excel.parse_all(filepath)


def parse_books(filepath, layout):
    if layout == "single_sheet_grouped_blocks":
        ex = GST_SUMMARY_DAILY_EXAMPLE
        raw = pd.read_excel(filepath, sheet_name=ex["sheet_name"], header=None)
        res = run_single_sheet_grouped_blocks(
            raw, ex["ingest_mapping"], ex["grouped_mapping"], header_row=ex["header_row"],
        )
    elif layout == "two_sheet_joined":
        ex = SALES_TWO_SHEET_EXAMPLE
        item_df = pd.read_excel(filepath, sheet_name=ex["item_sheet_name"], header=None)
        summary_df = pd.read_excel(filepath, sheet_name=ex["summary_sheet_name"], header=None)
        res = run_two_sheet_joined(item_df, summary_df, ex["item_mapping"], ex["summary_mapping"], ex["transform"])
    else:
        raise ValueError(f"unsupported books layout: {layout!r}")

    if not res.layer_a_ok:
        raise RuntimeError(f"Layer A failed: {res.layer_a_failures}")

    return res


def run(books_path, books_layout, portal_path, out_path,
        value_tolerance=1.0, date_tolerance_days=3):
    res = parse_books(books_path, books_layout)
    books_docs = invoices_to_canonical(res.invoices)

    portal_docs, b2c_totals, meta = load_portal(portal_path)

    config = ReconciliationConfig(
        value_tolerance=value_tolerance,
        date_tolerance_days=date_tolerance_days,
    )
    result = reconcile(books_docs, portal_docs, config)

    export(result, config, out_path, meta=meta)

    return {
        "out_path": out_path,
        "counts": {k: len(v) for k, v in result.items()},
        "books_skipped": res.skipped_invoices,
        "meta": meta,
        "b2c_totals": b2c_totals,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--books", required=True)
    ap.add_argument("--books-layout", required=True,
                    choices=["single_sheet_grouped_blocks", "two_sheet_joined"])
    ap.add_argument("--portal", required=True)
    ap.add_argument("--out", default="output/gst_reconciliation.xlsx")
    ap.add_argument("--tolerance", type=float, default=1.0)
    ap.add_argument("--date-tolerance-days", type=int, default=3)
    args = ap.parse_args()

    out = run(
        args.books, args.books_layout, args.portal, args.out,
        args.tolerance, args.date_tolerance_days,
    )
    print("Meta:", out["meta"])
    print("Counts:", out["counts"])
    print("Books skipped (extraction stage):", len(out["books_skipped"]))
    if out["b2c_totals"]:
        print("B2C totals (portal, aggregate only):", out["b2c_totals"])
    print("Written to:", out["out_path"])
