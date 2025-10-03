import pandas as pd

# ---------------------------
# Helper functions
# ---------------------------

def expand_prefix(prefix, df_cols):
    """
    Expands a prefix like 'Q1_' into all matching dataframe columns
    Example: expand_prefix("Q1_", df.columns) -> ['Q1_1', 'Q1_2', ...]
    """
    return [c for c in df_cols if c.startswith(prefix)]


def check_range(series, condition):
    """Validate if all values are within given numeric range, e.g. '1-5'"""
    try:
        low, high = map(int, condition.split("-"))
        invalid = series[~series.isna() & ~series.between(low, high)]
        return invalid.index.tolist()
    except Exception as e:
        return [f"Error parsing range '{condition}': {e}"]


def check_skip(df, condition):
    """
    Validate skip logic of type:
    if VAR=VALUE then TARGET should be answered
    """
    try:
        cond = condition.lower().replace("then", "").replace("should be answered", "").strip()
        # Example: "if Segment_7=1 ITQ1_r1"
        if cond.startswith("if "):
            cond = cond[3:]

        parts = cond.split()
        if len(parts) < 2:
            return [f"Invalid skip condition: {condition}"]

        lhs, target = parts[0], parts[1]
        var, val = lhs.split("=")
        var, val = var.strip(), val.strip()

        # rows where var == val but target is missing
        invalid = df[(df[var].astype(str) == val) & (df[target].isna())]
        return invalid.index.tolist()
    except Exception as e:
        return [f"Error parsing skip '{condition}': {e}"]


def check_missing(series, _condition=None):
    """Check for missing values"""
    return series[series.isna()].index.tolist()


# ---------------------------
# Main validation engine
# ---------------------------

def run_validations(df, rules_df):
    all_errors = []

    for _, rule in rules_df.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [c.strip() for c in str(rule["Check_Type"]).split(";")]
        conditions = [c.strip() for c in str(rule.get("Condition", "")).split(";")]

        # pad conditions if fewer than check_types
        if len(conditions) < len(check_types):
            conditions += [conditions[-1]] * (len(check_types) - len(conditions))

        for i, check_type in enumerate(check_types):
            condition = conditions[i] if i < len(conditions) else None

            # handle Range check
            if check_type == "Range":
                if q in df.columns:
                    errors = check_range(df[q], condition)
                    if errors:
                        all_errors.append((q, "Range", errors))
                else:
                    matches = expand_prefix(q, df.columns)
                    for col in matches:
                        errors = check_range(df[col], condition)
                        if errors:
                            all_errors.append((col, "Range", errors))

            # handle Skip check
            elif check_type == "Skip":
                errors = check_skip(df, condition)
                if errors:
                    all_errors.append((q, "Skip", errors))

            # handle Missing check
            elif check_type == "Missing":
                if q in df.columns:
                    errors = check_missing(df[q])
                    if errors:
                        all_errors.append((q, "Missing", errors))
                else:
                    matches = expand_prefix(q, df.columns)
                    for col in matches:
                        errors = check_missing(df[col])
                        if errors:
                            all_errors.append((col, "Missing", errors))

            # Unknown check
            else:
                all_errors.append((q, check_type, [f"Unknown check type {check_type}"]))

    return all_errors


# ---------------------------
# Example usage
# ---------------------------

if __name__ == "__main__":
    # Example survey dataframe
    df = pd.DataFrame({
        "RespondentID": [1, 2, 3, 4],
        "Segment_7": [1, 0, 1, 1],
        "ITQ1_r1": [1, 6, None, 3],
        "ITQ2_r1": [2, 3, 99, None],
        "ITQ5_r1": [1, 2, None, 1],
        "ITQ5x1": [5, None, None, None],
        "ITQ10x1_r1": [4, None, 2, 5],
        "ITQ11": [None, None, None, 1],
    })

    # Example rules dataframe (simulate reading from Excel)
    rules_df = pd.DataFrame([
        {"Question": "ITQ1_r1", "Check_Type": "Range;Skip", "Condition": "1-5;if Segment_7=1 then ITQ1_r1 should be answered"},
        {"Question": "ITQ2_r1", "Check_Type": "Range;Skip", "Condition": "1-5;if Segment_7=1 then ITQ2_r1 should be answered"},
        {"Question": "ITQ5_r1", "Check_Type": "Range;Skip", "Condition": "1-2;if Segment_7=1 then ITQ5_r1 should be answered"},
        {"Question": "ITQ5x1", "Check_Type": "Skip", "Condition": "if ITQ5_r1=1 then ITQ5x1 should be answered"},
        {"Question": "ITQ10x1_r1", "Check_Type": "Range;Skip", "Condition": "1-5;if Segment_7=1 then ITQ10x1_r1 should be answered"},
        {"Question": "ITQ11", "Check_Type": "Skip", "Condition": "if ITQ10x1_r1>=3 then ITQ11 should be answered"},
    ])

    errors = run_validations(df, rules_df)

    for e in errors:
        print(e)
