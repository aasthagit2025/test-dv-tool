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
    name = data_file.name.lower()
    try:
        if name.endswith(".csv"):
            # keep_default_na=False -> 'NA' stays as string, not parsed to NaN
            df = pd.read_csv(data_file, encoding_errors="ignore", keep_default_na=False)
        elif name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(data_file, keep_default_na=False)
        elif name.endswith(".sav"):
            df, meta = pyreadstat.read_sav(data_file)
        else:
            st.error("Unsupported file type")
            st.stop()
    except Exception as e:
        st.error(f"Failed to read data file: {e}")
        st.stop()

    # Ensure RespondentID exists
    if "RespondentID" not in df.columns:
        df.insert(0, "RespondentID", range(1, len(df) + 1))

    # --- Load Rules ---
    try:
        rules_df = pd.read_excel(rules_file)
    except Exception as e:
        st.error(f"Failed to read rules file: {e}")
        st.stop()

    # --- Validation Report Logic ---
    report = []
    skip_pass_ids = set()  # store respondents where skip was satisfied

    for _, rule in rules_df.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [c.strip() for c in str(rule.get("Check_Type", "")).split(";") if c.strip() != ""]
        conditions = [c.strip() for c in str(rule.get("Condition", "")).split(";")]

        for i, check_type in enumerate(check_types):
            condition = conditions[i] if i < len(conditions) else None

            # --- Straightliner ---
            if check_type == "Straightliner":
                related_cols = [col.strip() for col in q.split(",")]
                related_cols = [col for col in related_cols if col in df.columns]
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
                        "Issue": "Question(s) not found in dataset"
                    })
                continue

            # --- Single-Column Checks ---
            if q not in df.columns:
                report.append({
                    "RespondentID": None,
                    "Question": q,
                    "Check_Type": check_type,
                    "Issue": "Question not found in dataset"
                })
                continue

            if check_type == "Missing":
                missing_mask = df[q].isna() | (df[q].astype(str).str.strip() == "")
                offenders = df.loc[missing_mask & (~df["RespondentID"].isin(skip_pass_ids)), "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Missing", "Issue": "Value is missing"})

            elif check_type == "Range":
                try:
                    if "-" not in str(condition):
                        raise ValueError("Not a valid range format")
                    min_val, max_val = map(float, condition.split("-"))
                    mask = df[q].astype(float).between(min_val, max_val)  # True if within
                    offenders = df.loc[~mask & (~df["RespondentID"].isin(skip_pass_ids)), "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": q, "Check_Type": "Range",
                                       "Issue": f"Value out of range ({min_val}-{max_val})"})
                except Exception:
                    report.append({"RespondentID": None, "Question": q, "Check_Type": "Range",
                                   "Issue": f"Invalid range condition ({condition})"})

            elif check_type == "Skip":
                try:
                    cond_text = str(condition or "")

                    # --- support both 'then' and no-then formats ---
                    if re.search(r'(?i)\bthen\b', cond_text):
                        if_part, then_part = re.split(r'(?i)\bthen\b', cond_text, maxsplit=1)
                        if_part, then_part = if_part.strip(), then_part.strip()
                        # if then_part begins with variable + rest, we'll extract target below
                        # e.g. "Q4c should be answered" or "Q4c should be blank"
                        then_q_candidate = then_part.split()[0].strip()
                        then_q = then_q_candidate
                    else:
                        # find target variable (a Q... token) that is followed by "should"
                        m = re.search(r'(?i)\b(Q[\w_]+)\b(?=.*\bshould\b)', cond_text)
                        if not m:
                            raise ValueError("Invalid skip format: cannot locate target variable and 'should'")
                        then_q = m.group(1)
                        left, right = cond_text.split(then_q, 1)
                        if_part = left.strip()
                        then_part = right.strip()

                    # remove leading 'If' from if_part
                    if if_part.lower().startswith("if"):
                        conds_text = if_part[2:].strip()
                    else:
                        conds_text = if_part

                    # --- Build boolean mask from conditions supporting or/and and operators ---
                    or_groups = re.split(r'\s+or\s+', conds_text, flags=re.IGNORECASE)
                    mask = pd.Series(False, index=df.index)
                    for or_group in or_groups:
                        and_parts = re.split(r'\s+and\s+', or_group, flags=re.IGNORECASE)
                        sub_mask = pd.Series(True, index=df.index)
                        for part in and_parts:
                            part = part.strip().replace("<>", "!=")
                            handled = False
                            # check operators longest-first to prevent split problems
                            for op in ["<=", ">=", "!=", "<", ">", "="]:
                                if op in part:
                                    col, val = [p.strip() for p in part.split(op, 1)]
                                    # numeric comparisons for <, <=, >, >=
                                    if op in ["<=", ">=", "<", ">"]:
                                        col_vals = pd.to_numeric(df[col], errors="coerce")
                                        val_num = float(val)
                                        if op == "<=":
                                            sub_mask &= col_vals <= val_num
                                        elif op == ">=":
                                            sub_mask &= col_vals >= val_num
                                        elif op == "<":
                                            sub_mask &= col_vals < val_num
                                        elif op == ">":
                                            sub_mask &= col_vals > val_num
                                    else:
                                        # equality / inequality as string compare (robust)
                                        if op in ["!=",]:
                                            sub_mask &= df[col].astype(str).str.strip() != str(val)
                                        elif op == "=":
                                            sub_mask &= df[col].astype(str).str.strip() == str(val)
                                    handled = True
                                    break
                            if not handled:
                                raise ValueError(f"Unsupported operator in '{part}'")
                        mask |= sub_mask

                    # --- Parse THEN part and decide blank vs answered ---
                    should_be_blank = "blank" in then_part.lower()

                    if then_q not in df.columns:
                        report.append({
                            "RespondentID": None, "Question": q, "Check_Type": "Skip",
                            "Issue": f"Skip condition references missing variable '{then_q}'"
                        })
                        continue

                    # Define blank = NaN or empty string ONLY (treat literal "NA" as answered)
                    blank_mask = df[then_q].isna() | (df[then_q].astype(str).str.strip() == "")
                    not_blank_mask = ~blank_mask

                    if should_be_blank:
                        # condition true AND answered => offender
                        offenders = df.loc[mask & not_blank_mask, "RespondentID"]
                        for rid in offenders:
                            report.append({"RespondentID": rid, "Question": q, "Check_Type": "Skip",
                                           "Issue": "Answered but should be blank"})
                        # condition true AND blank => mark as satisfied (exclude from Range/Missing)
                        satisfied = df.loc[mask & blank_mask, "RespondentID"].tolist()
                        skip_pass_ids = skip_pass_ids.union(set(satisfied))
                    else:
                        # condition true AND blank => offender (should be answered)
                        offenders = df.loc[mask & blank_mask, "RespondentID"]
                        for rid in offenders:
                            report.append({"RespondentID": rid, "Question": q, "Check_Type": "Skip",
                                           "Issue": "Blank but should be answered"})
                except Exception as e:
                    report.append({"RespondentID": None, "Question": q, "Check_Type": "Skip",
                                   "Issue": f"Invalid skip rule format ({condition}) - {e}"})

            elif check_type == "Multi-Select":
                related_cols = [col for col in df.columns if col.startswith(q)]
                for col in related_cols:
                    offenders = df.loc[~df[col].isin([0, 1]), "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": col,
                                       "Check_Type": "Multi-Select", "Issue": "Invalid value (not 0/1)"})
                if len(related_cols) > 0:
                    offenders = df.loc[df[related_cols].fillna(0).sum(axis=1) == 0, "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": q,
                                       "Check_Type": "Multi-Select", "Issue": "No options selected"})

            elif check_type == "OpenEnd_Junk":
                s = df[q].astype(str).fillna("")
                mask_junk = (s.str.len() < 3) | s.str.lower().str.contains(r'\b(lorem|asdf|qwer)\b', na=False)
                offenders = df.loc[mask_junk, "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "OpenEnd_Junk",
                                   "Issue": "Open-end looks like junk/low-effort"})

            elif check_type == "Duplicate":
                duplicate_ids = df[df.duplicated(subset=[q], keep=False)]["RespondentID"]
                for rid in duplicate_ids:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Duplicate",
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
