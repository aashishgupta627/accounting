"""
Stage 0: ingestion & normalization.

Runs BEFORE any layout-specific parsing. Two jobs, kept separate:

1. normalize_sheet() — layout-agnostic cleanup every sheet gets regardless
   of family: blank-ish cells (NaN, '', whitespace-only strings) all become
   real NaN, so every later "is this blank" check behaves the same way
   whether the source used NaN or padded spaces for empty cells.

2. forward_fill_blocks() — specific to the "single_sheet_grouped_blocks"
   family (a block-header row states a field once, e.g. Account + GST No.,
   followed by several detail rows where that field is blank). Fills the
   value down, tags each row's block id, and drops header/footer rows from
   the output — so the parser downstream just sees a flat table of detail
   rows with every column already populated, no different from a native
   single_sheet_flat file.
"""
import pandas as pd


def _is_blank(value) -> bool:
    if pd.isna(value):
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def normalize_sheet(df_raw: pd.DataFrame) -> pd.DataFrame:
    """Whitespace-only and empty-string cells become real NaN; string cells
    are stripped. Row/column positions are preserved exactly, so schema
    indices detected against the raw sheet still line up against this
    output."""
    def clean_cell(v):
        if _is_blank(v):
            return pd.NA
        if isinstance(v, str):
            return v.strip()
        return v

    return df_raw.apply(lambda col: col.map(clean_cell))


def forward_fill_blocks(df: pd.DataFrame, schema: dict) -> pd.DataFrame:
    """schema must be a 'single_sheet_grouped_blocks' schema (see
    validate_schema.py). Call normalize_sheet() first.

    Returns a new DataFrame containing ONLY detail rows, with every column
    in forward_fill_columns populated on every row, and a '_block_id'
    column added (0-indexed, increments at each block header) purely for
    debugging/traceability — the parser doesn't need to use it.
    """
    header_marker = schema["block_header_marker"]
    present_cols = header_marker["columns_present"]
    blank_cols = header_marker.get("columns_blank", [])
    footer_marker = schema["block_footer_marker"]
    footer_col = footer_marker["column"]
    footer_contains = footer_marker["contains"]
    forward_fill_cols = schema["forward_fill_columns"]  # {canonical_field: col_idx}

    out_rows = []
    current_fill = {f: None for f in forward_fill_cols}
    block_id = -1

    for i in range(len(df)):
        row = df.iloc[i]

        is_header = (
            all(not _is_blank(row.iloc[c]) for c in present_cols)
            and all(_is_blank(row.iloc[c]) for c in blank_cols)
        )
        if is_header:
            block_id += 1
            for field_name, col_idx in forward_fill_cols.items():
                current_fill[field_name] = row.iloc[col_idx]
            continue

        footer_val = row.iloc[footer_col]
        if not _is_blank(footer_val) and footer_contains.lower() in str(footer_val).lower():
            continue  # block or grand-total footer row — not data

        if all(_is_blank(v) for v in row):
            continue  # fully blank spacer row

        record = row.copy()
        for field_name, col_idx in forward_fill_cols.items():
            if _is_blank(record.iloc[col_idx]):
                record.iloc[col_idx] = current_fill[field_name]
        out_rows.append(list(record) + [block_id])

    result = pd.DataFrame(out_rows, columns=list(df.columns) + ["_block_id"])
    return result
