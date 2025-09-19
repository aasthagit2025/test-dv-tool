import streamlit as st
import pandas as pd
import pyreadstat
import io
import re

st.title("📊 Survey Data Validation Tool")

# --- File Upload ---
data_file = st.file_uploader("Upload your survey data file (CSV, Excel, or SPSS)", type=["csv", "xlsx", "sav"])
rules_file = st.file_uploader("Upload validation rules (Excel)", type=["xlsx"])

if data_file and rules_file:
    # --- Load Data ---
    if data_file.name.endswith(".csv"):
        df = pd.read_csv(data_file, encoding_errors="ignore")
    elif data_file.name.endswith(".xlsx"):
        df = pd.read_excel(data_file)
    elif data_file.name.endswith(".sav"):
        df, meta = pyreadstat.read_sav(data_file)
    else:
        st.error("Unsupported file type")
        st.stop()

    # --- Load Rules ---
    rules_df = pd.read_excel(rules_file)

    # --- Validation Report Logic ---
    report = []
    skip_pass_ids = set()  # store respondents where skip was satisfied

    # Utility: expand column ranges like "Q3_1 to Q3_13"
    def expand_range(expr, df_cols):
        expr = expr.strip()
        if "to" in expr:
            start, end = [x.strip() for x in expr.split("to")]
            base = re.match(r"([A-Za-z0-9_]+?)(\d+)$", start)
            base2 = re.match(r"([A-Za-z0-9_]+?)(\d+)$", end)
            if base and base2 and base.group(1) == base2.group(1):
                prefix = base.group(1)
                start_num, end_num = int(base.group(2)), int(base2.group(2))
                return [f"{prefix}{i}" for i in range(start_num, end_num + 1) if f"{prefix}{i}" in df_cols]
        return [expr] if expr in df_cols else []

    # Utility: get all dataset columns starting with prefix
    def expand_prefix(prefix, df_cols):
        return [c for c in df_cols if c.startswith(prefix)]

    for _, rule in rules_df.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [c.strip() for c in str(rule["Check_Type"]).split(";")]
        conditions = [c.strip() for c in str(rule["Condition"]).split(";")]

        for i, check_type in enumerate(check_types):
            condition = conditions[i] if i < len(conditions) else None

            # --- Straightliner ---
            if check_type == "Straightliner":
                related_cols = []
                if q in df.columns:  # exact column
                    related_cols = [q]
                else:  # prefix
                    related_cols = expand_prefix(q, df.columns)

                if len(related_cols) > 1:
                    straightliners = df[related_cols].nunique(axis=1)
                    offenders = df.loc[straightliners == 1, "RespondentID"]
                    for rid in offenders:
                        report.append({
                            "RespondentID": rid,
                            "Question": ",".join(related_cols),
                            "Check_Type": "Straightliner",
                            "Issue": "Same response across all items"
                        })
                else:
                    report.append({
                        "RespondentID": None,
                        "Question": q,
                        "Check_Type": "Straightliner",
                        "Issue": "No matching columns for straightliner"
                    })
                continue

            # --- Single-Column Checks ---
            if check_type != "Multi-Select" and q not in df.columns and not any(c.startswith(q) for c in df.columns):
                report.append({
                    "RespondentID": None,
                    "Question": q,
                    "Check_Type": check_type,
                    "Issue": "Question not found in dataset"
                })
                continue

            if check_type == "Missing":
                missing = df[q].isna().sum()
                if missing > 0:
                    offenders = df.loc[df[q].isna(), "RespondentID"]
                    for rid in offenders:
                        if rid not in skip_pass_ids:
                            report.append({"RespondentID": rid, "Question": q,
                                           "Check_Type": "Missing", "Issue": "Value is missing"})

            elif check_type == "Range":
                try:
                    if "-" not in str(condition):
                        raise ValueError("Not a valid range format")
                    min_val, max_val = map(float, condition.split("-"))
                    mask = ~df[q].between(min_val, max_val)
                    offenders = df.loc[mask & (~df["RespondentID"].isin(skip_pass_ids)), "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": q,
                                       "Check_Type": "Range",
                                       "Issue": f"Value out of range ({min_val}-{max_val})"})
                except Exception:
                    report.append({"RespondentID": None, "Question": q,
                                   "Check_Type": "Range",
                                   "Issue": f"Invalid range condition ({condition})"})

            elif check_type == "Skip":
                try:
                    if "then" not in str(condition):
                        raise ValueError("Not a valid skip format")

                    if_part, then_part = condition.split("then", 1)
                    if_part, then_part = if_part.strip(), then_part.strip()

                    if if_part.lower().startswith("if"):
                        conds_text = if_part[2:].strip()
                    else:
                        conds_text = if_part

                    # --- Build condition mask ---
                    or_groups = re.split(r'\s+or\s+', conds_text, flags=re.IGNORECASE)
                    mask = pd.Series(False, index=df.index)
                    for or_group in or_groups:
                        and_parts = re.split(r'\s+and\s+', or_group, flags=re.IGNORECASE)
                        sub_mask = pd.Series(True, index=df.index)
                        for part in and_parts:
                            part = part.strip().replace("<>", "!=")
                            for op in ["<=", ">=", "!=", "<>", "<", ">", "="]:
                                if op in part:
                                    col, val = [p.strip() for p in part.split(op, 1)]
                                    if op in ["<=", ">=", "<", ">"]:
                                        val = float(val)
                                        col_vals = pd.to_numeric(df[col], errors="coerce")
                                        if op == "<=": sub_mask &= col_vals <= val
                                        elif op == ">=": sub_mask &= col_vals >= val
                                        elif op == "<": sub_mask &= col_vals < val
                                        elif op == ">": sub_mask &= col_vals > val
                                    elif op in ["!=", "<>"]:
                                        sub_mask &= df[col].astype(str).str.strip() != str(val)
                                    elif op == "=":
                                        sub_mask &= df[col].astype(str).str.strip() == str(val)
                                    break
                        mask |= sub_mask

                    # --- Parse THEN part ---
                    if "to" in then_part:  # handle ranges
                        target_cols = expand_range(then_part.split()[0] + " " + then_part.split()[1] + " " + then_part.split()[2], df.columns)
                    elif then_part.endswith("_"):
                        target_cols = expand_prefix(then_part.split()[0], df.columns)
                    else:
                        target_cols = [then_part.split()[0].strip()]

                    should_be_blank = "blank" in then_part.lower()

                    for then_q in target_cols:
                        if then_q not in df.columns:
                            report.append({
                                "RespondentID": None, "Question": q,
                                "Check_Type": "Skip",
                                "Issue": f"Skip condition references missing variable '{then_q}'"
                            })
                            continue

                        blank_mask = df[then_q].isna() | (df[then_q].astype(str).str.strip() == "")
                        not_blank_mask = ~blank_mask

                        if should_be_blank:
                            offenders = df.loc[mask & not_blank_mask, "RespondentID"]
                            for rid in offenders:
                                report.append({"RespondentID": rid, "Question": then_q,
                                               "Check_Type": "Skip",
                                               "Issue": "Answered but should be blank"})
                            satisfied = df.loc[mask & blank_mask, "RespondentID"].tolist()
                            skip_pass_ids = skip_pass_ids.union(set(satisfied))
                        else:
                            offenders = df.loc[mask & blank_mask, "RespondentID"]
                            for rid in offenders:
                                report.append({"RespondentID": rid, "Question": then_q,
                                               "Check_Type": "Skip",
                                               "Issue": "Blank but should be answered"})
                except Exception as e:
                    report.append({"RespondentID": None, "Question": q,
                                   "Check_Type": "Skip",
                                   "Issue": f"Invalid skip rule format ({condition}) -> {e}"})

            elif check_type == "Multi-Select":
                related_cols = expand_prefix(q, df.columns)
                for col in related_cols:
                    offenders = df.loc[~df[col].isin([0, 1]), "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": col,
                                       "Check_Type": "Multi-Select",
                                       "Issue": "Invalid value (not 0/1)"})
                if len(related_cols) > 0:
                    offenders = df.loc[df[related_cols].fillna(0).sum(axis=1) == 0, "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": q,
                                       "Check_Type": "Multi-Select",
                                       "Issue": "No options selected"})

            elif check_type == "OpenEnd_Junk":
                junk = df[q].astype(str).str.len() < 3
                offenders = df.loc[junk, "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": q,
                                   "Check_Type": "OpenEnd_Junk",
                                   "Issue": "Open-end looks like junk/low-effort"})

            elif check_type == "Duplicate":
                duplicate_ids = df[df.duplicated(subset=[q], keep=False)]["RespondentID"]
                for rid in duplicate_ids:
                    report.append({"RespondentID": rid, "Question": q,
                                   "Check_Type": "Duplicate",
                                   "Issue": "Duplicate value found"})

    # --- Create Report ---
    report_df = pd.DataFrame(report)

    st.write("### Validation Report (detailed by Respondent)")
    st.dataframe(report_df)

    # --- Download Report ---
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        report_df.to_excel(writer, index=False, sheet_name="Validation Report")

    st.download_button(
        label="Download Validation Report",
        data=output.getvalue(),
        file_name="validation_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
