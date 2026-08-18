"""
Layer A validation: is a detected schema sane enough to cache and run
deterministically against the full file?

This never calls an LLM. It only checks the schema's shape against a small
sample DataFrame (the same rows shown to the detector) and reports pass/fail
with concrete reasons, so a bad schema gets caught before it silently runs
against thousands of rows.
"""
import re
import pandas as pd
from dataclasses import dataclass, field

NUMERIC_ITEM_FIELDS = {
    "ACTUALQTY", "FREEQTY", "RATE", "GSTRATE", "AMOUNT",
    "DISCOUNT", "TAXABLEVALUE", "GSTAMOUNT", "NETAMOUNT",
    "CGSTAMOUNT", "SGSTAMOUNT", "IGSTAMOUNT", "CESSAMOUNT",
}

VOUCHER_FIELDS = {
    "DATE", "VOUCHERNUMBER", "PARTYNAME", "PARTYGSTIN",
    "NARRATION", "ROUNDOFFAMOUNT", "BILLAMOUNT",
}
ITEM_FIELDS = {
    "STOCKITEMNAME", "BATCHNAME", "EXPIRYDATE", "ACTUALQTY", "FREEQTY",
    "RATE", "GSTRATE", "AMOUNT", "DISCOUNT", "TAXABLEVALUE", "GSTAMOUNT",
    "HSNCODE", "NETAMOUNT", "CGSTAMOUNT", "SGSTAMOUNT", "IGSTAMOUNT", "CESSAMOUNT",
}
LINE_IDENTIFIER_FIELDS = {"STOCKITEMNAME", "HSNCODE"}
SUMMARY_FIELDS = {
    "VOUCHERNUMBER", "PARTYNAME", "PARTYGSTIN",
    "BILLAMOUNT", "ROUNDOFFAMOUNT", "STATECODE",
}
GROUPED_BLOCK_FIELDS = ITEM_FIELDS | {"DATE", "PARTYNAME", "PARTYGSTIN", "VOUCHERNUMBER"}

TRANSFORM_TYPES = {"identity", "strip_prefix", "regex_extract"}

MIN_CONFIDENCE = 0.8
MIN_NUMERIC_PARSE_RATE = 0.9
MIN_MARKER_MATCHES = 1
MIN_JOIN_MATCH_RATE = 0.9


@dataclass
class ValidationResult:
    passed: bool
    failures: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def add_failure(self, msg):
        self.passed = False
        self.failures.append(msg)


def _col_ok(idx, n_cols):
    return isinstance(idx, int) and 0 <= idx < n_cols


def apply_transform(key, transform: dict):
    """Shared by validation and the parser, so the join logic can never
    diverge between the two. Returns None if the transform can't apply
    to this key (e.g. regex didn't match)."""
    if key is None:
        return None
    ttype = transform.get("type")
    if ttype == "identity":
        return key
    if ttype == "strip_prefix":
        prefix = transform.get("prefix", "")
        return key[len(prefix):] if key.startswith(prefix) else key
    if ttype == "regex_extract":
        m = re.search(transform.get("pattern", ""), key)
        if not m:
            return None
        return transform.get("template", "{1}").replace("{1}", m.group(1))
    return None


def extract_blob_fields(text, blob_extract: dict):
    """Apply named-group regexes (each with a `(?P<v>...)` group) to a single
    merged-cell string and return {canonical_field: extracted_value}. Fields
    whose pattern doesn't match are simply omitted."""
    if text is None:
        return {}
    text = str(text)
    out = {}
    for field_name, pattern in blob_extract.items():
        try:
            m = re.search(pattern, text)
        except re.error:
            continue
        if not m:
            continue
        if "v" in (m.groupdict() or {}):
            out[field_name] = m.group("v")
        elif m.groups():
            out[field_name] = m.group(1)
        else:
            out[field_name] = m.group(0)
    return out


def validate_item_details_schema(schema: dict, sample_df: pd.DataFrame) -> ValidationResult:
    """sample_df: raw grid (header=None) covering the same rows shown to the LLM."""
    result = ValidationResult(passed=True)
    n_cols = sample_df.shape[1]
    n_rows = sample_df.shape[0]

    if schema.get("sheet_type") != "item_details":
        result.add_failure(f"unexpected sheet_type: {schema.get('sheet_type')}")
        return result

    if schema.get("confidence", 0) < MIN_CONFIDENCE:
        result.add_failure(f"confidence {schema.get('confidence')} below {MIN_CONFIDENCE}")

    # header_rows / data_start_row in range
    for r in schema.get("header_rows", []):
        if not (0 <= r < n_rows):
            result.add_failure(f"header row {r} out of range (0-{n_rows-1})")
    data_start = schema.get("data_start_row")
    if data_start is None or not (0 <= data_start < n_rows):
        result.add_failure(f"data_start_row {data_start} out of range")

    # invoice_block_marker
    marker = schema.get("invoice_block_marker", {})
    marker_col = marker.get("column")
    pattern = marker.get("pattern")
    marker_fields = marker.get("fields", {})
    blob_extract = marker.get("blob_extract", {})

    compiled = None
    if not _col_ok(marker_col, n_cols):
        result.add_failure(f"invoice_block_marker.column {marker_col} out of range")
    else:
        try:
            compiled = re.compile(pattern) if pattern else None
        except re.error as e:
            result.add_failure(f"invoice_block_marker.pattern invalid regex: {e}")
        if compiled:
            col_vals = sample_df.iloc[:, marker_col].astype(str)
            matches = col_vals.str.contains(compiled, na=False).sum()
            result.stats["marker_matches"] = int(matches)
            if matches < MIN_MARKER_MATCHES:
                result.add_failure(
                    f"invoice_block_marker.pattern matched 0 rows in sample "
                    f"(col {marker_col}, pattern {pattern!r})"
                )

    if marker_fields and blob_extract:
        result.add_failure(
            "invoice_block_marker has both fields and blob_extract — use fields "
            "when voucher-level data sits in separate columns, blob_extract when "
            "it's one merged-cell string. Not both."
        )

    unknown_marker_fields = set(marker_fields) - VOUCHER_FIELDS
    if unknown_marker_fields:
        result.add_failure(f"invoice_block_marker.fields uses non-canonical keys: {unknown_marker_fields}")
    for f, idx in marker_fields.items():
        if not _col_ok(idx, n_cols):
            result.add_failure(f"invoice_block_marker.fields[{f}] column {idx} out of range")

    unknown_blob_fields = set(blob_extract) - VOUCHER_FIELDS
    if unknown_blob_fields:
        result.add_failure(f"invoice_block_marker.blob_extract uses non-canonical keys: {unknown_blob_fields}")
    if blob_extract and compiled is not None and _col_ok(marker_col, n_cols):
        marker_rows = sample_df[sample_df.iloc[:, marker_col].astype(str).str.contains(compiled, na=False)]
        for f, subpattern in blob_extract.items():
            try:
                sub_compiled = re.compile(subpattern)
            except re.error as e:
                result.add_failure(f"invoice_block_marker.blob_extract[{f}] invalid regex: {e}")
                continue
            # Plain Python loop, not pandas .apply().sum(): on some pandas
            # versions, summing an EMPTY Series backed by the newer "str"
            # dtype returns '' instead of 0, which then breaks int(''). A
            # zero-row match here is itself meaningful (worth reporting),
            # not a state that should crash Layer A.
            blob_values = marker_rows.iloc[:, marker_col].astype(str).tolist()
            sub_matches = sum(1 for v in blob_values if sub_compiled.search(v))
            result.stats[f"blob_extract_matches.{f}"] = sub_matches
            if len(marker_rows) > 0 and sub_matches == 0:
                result.add_failure(
                    f"invoice_block_marker.blob_extract[{f}] matched 0 of "
                    f"{len(marker_rows)} sample invoice-header rows"
                )

    # item_row_column_map
    item_map = schema.get("item_row_column_map", {})
    unknown_item_fields = set(item_map) - ITEM_FIELDS
    if unknown_item_fields:
        result.add_failure(f"item_row_column_map uses non-canonical keys: {unknown_item_fields}")

    for f, idx in item_map.items():
        if not _col_ok(idx, n_cols):
            result.add_failure(f"item_row_column_map[{f}] column {idx} out of range")

    line_id_field = schema.get("line_identifier_field", "STOCKITEMNAME")
    if line_id_field not in LINE_IDENTIFIER_FIELDS:
        result.add_failure(
            f"line_identifier_field must be one of {LINE_IDENTIFIER_FIELDS}, got {line_id_field!r}"
        )
    elif line_id_field not in item_map:
        result.add_failure(f"item_row_column_map missing declared line_identifier_field {line_id_field}")

    # numeric field sanity: sample rows below data_start_row, EXCLUDING rows
    # that match the invoice_block_marker (those are header rows, not items,
    # even though they can share the same column positions as item fields).
    if data_start is not None and 0 <= data_start < n_rows:
        item_rows = sample_df.iloc[data_start:]
        if _col_ok(marker_col, n_cols) and compiled is not None:
            is_marker_row = item_rows.iloc[:, marker_col].astype(str).str.contains(compiled, na=False)
            item_rows = item_rows[~is_marker_row]
        for f in NUMERIC_ITEM_FIELDS & set(item_map):
            idx = item_map[f]
            if not _col_ok(idx, n_cols):
                continue
            col = item_rows.iloc[:, idx]
            non_null = col.dropna()
            if len(non_null) == 0:
                continue
            parseable = pd.to_numeric(non_null, errors="coerce").notna().sum()
            rate = parseable / len(non_null)
            result.stats[f"numeric_parse_rate.{f}"] = round(rate, 3)
            if rate < MIN_NUMERIC_PARSE_RATE:
                result.add_failure(
                    f"field {f} (col {idx}) only {rate:.0%} numeric-parseable, "
                    f"expected >= {MIN_NUMERIC_PARSE_RATE:.0%}"
                )

    # skip_row_rules structural check only
    for rule in schema.get("skip_row_rules", []):
        if not _col_ok(rule.get("column"), n_cols):
            result.add_failure(f"skip_row_rules column {rule.get('column')} out of range")

    return result


def validate_summary_schema(schema: dict, sample_df: pd.DataFrame) -> ValidationResult:
    result = ValidationResult(passed=True)
    n_cols = sample_df.shape[1]
    n_rows = sample_df.shape[0]

    if schema.get("sheet_type") != "consolidated_summary":
        result.add_failure(f"unexpected sheet_type: {schema.get('sheet_type')}")
        return result

    if schema.get("confidence", 0) < MIN_CONFIDENCE:
        result.add_failure(f"confidence {schema.get('confidence')} below {MIN_CONFIDENCE}")

    header_row = schema.get("header_row")
    if header_row is None or not (0 <= header_row < n_rows):
        result.add_failure(f"header_row {header_row} out of range")

    col_map = schema.get("column_map", {})
    unknown = set(col_map) - SUMMARY_FIELDS
    if unknown:
        result.add_failure(f"column_map uses non-canonical keys: {unknown}")
    for f, idx in col_map.items():
        if not _col_ok(idx, n_cols):
            result.add_failure(f"column_map[{f}] column {idx} out of range")

    if "VOUCHERNUMBER" not in col_map:
        result.add_failure("column_map missing required field VOUCHERNUMBER")
    if "BILLAMOUNT" not in col_map:
        result.add_failure("column_map missing required field BILLAMOUNT")

    return result


def validate_join_transform(transform: dict, summary_keys: list, detail_keys: list,
                             min_match_rate: float = MIN_JOIN_MATCH_RATE) -> ValidationResult:
    """Quick pre-check on sample or full-file keys. Reused as-is for Layer B
    (call it again post-parse with the complete key lists)."""
    result = ValidationResult(passed=True)
    ttype = transform.get("type")
    if ttype not in TRANSFORM_TYPES:
        result.add_failure(f"unknown transform type: {ttype}")
        return result

    detail_set = set(detail_keys)
    mapped = [apply_transform(k, transform) for k in summary_keys]
    matches = sum(1 for m in mapped if m in detail_set)
    rate = matches / len(summary_keys) if summary_keys else 0
    result.stats["match_rate"] = round(rate, 3)
    result.stats["matched"] = matches
    result.stats["total_summary_keys"] = len(summary_keys)
    if rate < min_match_rate:
        result.add_failure(
            f"join transform matched {rate:.0%} of keys, expected >= {min_match_rate:.0%}"
        )
    return result


def validate_grouped_blocks_schema(schema: dict, sample_df: pd.DataFrame) -> ValidationResult:
    """sample_df here is the already-normalized, already-forward-filled
    detail-row table (output of ingest.forward_fill_blocks), not the raw
    sheet — block structure has already been resolved by that point, so
    this only needs to check the flat column_map, same shape of check as
    validate_summary_schema."""
    result = ValidationResult(passed=True)
    n_cols = sample_df.shape[1]

    if schema.get("sheet_type") != "single_sheet_grouped_blocks":
        result.add_failure(f"unexpected sheet_type: {schema.get('sheet_type')}")
        return result

    if schema.get("confidence", 0) < MIN_CONFIDENCE:
        result.add_failure(f"confidence {schema.get('confidence')} below {MIN_CONFIDENCE}")

    col_map = schema.get("column_map", {})
    unknown = set(col_map) - GROUPED_BLOCK_FIELDS
    if unknown:
        result.add_failure(f"column_map uses non-canonical keys: {unknown}")
    for f, idx in col_map.items():
        if not _col_ok(idx, n_cols):
            result.add_failure(f"column_map[{f}] column {idx} out of range")

    if "VOUCHERNUMBER" not in col_map:
        result.add_failure("column_map missing required field VOUCHERNUMBER")

    line_id_field = schema.get("line_identifier_field", "HSNCODE")
    if line_id_field not in LINE_IDENTIFIER_FIELDS:
        result.add_failure(f"line_identifier_field must be one of {LINE_IDENTIFIER_FIELDS}")
    elif line_id_field not in col_map:
        result.add_failure(f"column_map missing declared line_identifier_field {line_id_field}")

    numeric_present = NUMERIC_ITEM_FIELDS & set(col_map)
    for f in numeric_present:
        idx = col_map[f]
        if not _col_ok(idx, n_cols):
            continue
        non_null = sample_df.iloc[:, idx].dropna()
        if len(non_null) == 0:
            continue
        parseable = pd.to_numeric(non_null, errors="coerce").notna().sum()
        rate = parseable / len(non_null)
        result.stats[f"numeric_parse_rate.{f}"] = round(rate, 3)
        if rate < MIN_NUMERIC_PARSE_RATE:
            result.add_failure(f"field {f} (col {idx}) only {rate:.0%} numeric-parseable")

    # forward_fill_columns / block markers are consumed by ingest.py before
    # this ever runs, but a structurally broken one would mean the sample
    # never went through fill correctly — spot-check PARTYNAME/PARTYGSTIN
    # (if mapped) are fully populated post-fill, since a leftover blank
    # here means the block boundaries were wrong upstream.
    for f in {"PARTYNAME", "PARTYGSTIN"} & set(col_map):
        idx = col_map[f]
        if _col_ok(idx, n_cols):
            blank_rate = sample_df.iloc[:, idx].isna().mean()
            result.stats[f"post_fill_blank_rate.{f}"] = round(blank_rate, 3)
            if blank_rate > 0.05:
                result.add_failure(
                    f"{f} (col {idx}) still {blank_rate:.0%} blank after forward-fill — "
                    f"check block_header_marker / forward_fill_columns"
                )

    return result


def validate_and_decide(schema: dict, sample_df: pd.DataFrame):
    """Entry point: returns (should_cache: bool, result: ValidationResult).
    For single_sheet_grouped_blocks, sample_df must already be the output
    of ingest.forward_fill_blocks(), not the raw sheet."""
    if schema.get("sheet_type") == "item_details":
        r = validate_item_details_schema(schema, sample_df)
    elif schema.get("sheet_type") == "consolidated_summary":
        r = validate_summary_schema(schema, sample_df)
    elif schema.get("sheet_type") == "single_sheet_grouped_blocks":
        r = validate_grouped_blocks_schema(schema, sample_df)
    else:
        r = ValidationResult(passed=False, failures=["unrecognized sheet_type"])
    return r.passed, r
