"""
Builds the {raw_grid_dump} text the detection prompts (Prompt A/B) expect,
and the sample key lists Prompt C expects.

The row/column indices printed here MUST match the indices the parser later
uses (i.e. the position in a DataFrame read with pandas.read_excel(...,
header=None)) — a schema is only correct if the LLM's row/col numbers refer
to the same grid the parser will walk. This module is the single place that
produces that view, so both stay in sync.
"""
import datetime
import pandas as pd

MAX_CELL_CHARS = 200  # merged-cell blobs can be long; keep them intact but bounded


def _format_cell(value):
    if pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime.date, datetime.datetime)):
        text = value.strftime("%Y-%m-%d")
    else:
        text = str(value)
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS] + "…"
    return text


def serialize_raw_grid(df_raw: pd.DataFrame, n_rows: int = 25) -> str:
    """Read the sheet with header=None before calling this, so row 0 in the
    output is row 0 of the real grid the parser will use — not a header-
    adjusted index.

    Blank cells are omitted from a row's line entirely (rather than printed
    as null) to keep the dump compact; a row with no non-blank cells is
    still printed with an empty value list, so row numbers stay contiguous
    and the LLM can still see exactly which rows are blank.
    """
    rows_out = []
    n = min(n_rows, len(df_raw))
    for r in range(n):
        cells = []
        for c in range(df_raw.shape[1]):
            val = _format_cell(df_raw.iat[r, c])
            if val is not None:
                cells.append(f"{c}: {val!r}")
        rows_out.append(f"row {r}: " + (", ".join(cells) if cells else "(blank)"))
    return "\n".join(rows_out)


def sample_join_keys(key_series: pd.Series, n: int = 8, exclude=None) -> list:
    """Pull up to n distinct, non-null sample values for Prompt C. exclude
    is an optional set/list of literal values to drop (e.g. a repeated
    header label like 'INV.NO' that isn't a real key)."""
    exclude = set(exclude or [])
    vals = (
        key_series.dropna()
        .astype(str)
        .str.strip()
    )
    vals = vals[~vals.isin(exclude) & (vals != "")]
    return vals.drop_duplicates().head(n).tolist()


def build_prompt_a(sheet_name: str, summary_sheet_name: str, df_raw: pd.DataFrame,
                    system_prompt: str, user_template: str, n_rows: int = 25) -> dict:
    """Assembles the ready-to-send system + user text for Prompt A (Item
    Details detection). system_prompt / user_template come from
    schema_detection_prompts.md — kept as plain strings there so this stays
    a formatting step, not a place prompt wording lives."""
    grid = serialize_raw_grid(df_raw, n_rows=n_rows)
    user = user_template.format(
        sheet_name=sheet_name,
        summary_sheet_name=summary_sheet_name,
        n=min(n_rows, len(df_raw)) - 1,
        raw_grid_dump=grid,
    )
    return {"system": system_prompt, "user": user}


def build_prompt_b(sheet_name: str, df_raw: pd.DataFrame,
                    user_template: str, n_rows: int = 15) -> dict:
    """Prompt B (Consolidated Summary detection) — summary sheets are
    shorter-headered so fewer sample rows are usually enough; override
    n_rows if a given file's header block runs deeper."""
    grid = serialize_raw_grid(df_raw, n_rows=n_rows)
    user = user_template.format(
        sheet_name=sheet_name,
        n=min(n_rows, len(df_raw)) - 1,
        raw_grid_dump=grid,
    )
    return {"user": user}


def build_prompt_c(summary_keys: list, detail_keys: list, user_template: str) -> dict:
    """Prompt C (join transform). Pass already-sampled key lists (see
    sample_join_keys) — kept separate so callers can control exactly which
    keys get shown, e.g. excluding a header row's literal label."""
    user = user_template.format(
        summary_keys=summary_keys,
        detail_keys=detail_keys,
    )
    return {"user": user}
