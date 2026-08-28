import pandas as pd
def _is_blank(value):
    if pd.isna(value):
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False
def normalize_sheet(df_raw):
    def clean_cell(v):
        if _is_blank(v):
            return pd.NA
        if isinstance(v, str):
            return v.strip()
        return v
    return df_raw.apply(lambda col: col.map(clean_cell))
def forward_fill_blocks(df, schema):
    header_marker = schema["block_header_marker"]
    present_cols = header_marker["columns_present"]
    blank_cols = header_marker.get("columns_blank", [])
    footer_marker = schema["block_footer_marker"]
    footer_col = footer_marker["column"]
    footer_contains = footer_marker["contains"]
    forward_fill_cols = schema["forward_fill_columns"]
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
            continue
        if all(_is_blank(v) for v in row):
            continue
        record = row.copy()
        for field_name, col_idx in forward_fill_cols.items():
            if _is_blank(record.iloc[col_idx]):
                record.iloc[col_idx] = current_fill[field_name]
        out_rows.append(list(record) + [block_id])
    result = pd.DataFrame(out_rows, columns=list(df.columns) + ["_block_id"])
    return result
