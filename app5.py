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

    for _, rule in rules_df.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [c.strip() for c in str(rule["Check_Type"]).split(";")]
        conditions = [c.strip() for c in str(rule["Condition"]).split(";")]

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

                    # remove leading 'If' (case-insensitive)
                    if if_part.lower().startswith("if"):
                        conds_text = if_part[2:].strip()
                    else:
                        conds_text = if_part

                    # Parse logical structure: support OR / AND (case-insensitive)
                    # We'll split top-level on OR, then on AND for each group.
                    or_groups = re.split(r'\s+or\s+', conds_text, flags=re.IGNORECASE)
                    mask = pd.Series(False, index=df.index)   # <<--- FIX: start with False (was True before)
                    for or_group in or_groups:
                        and_parts = re.split(r'\s+and\s+', or_group, flags=re.IGNORECASE)
                        sub_mask = pd.Series(True, index=df.index)
                        for part in and_parts:
                            part = part.strip()
                            # Normalize operators
                            part = part.replace("<>", "!=")
                            # support != and =
                            if "!=" in part:
                                col, val = [p.strip() for p in part.split("!=", 1)]
                                # compare as string to be robust to types
                                sub_mask &= df[col].astype(str).str.strip() != str(val)
                            elif "=" in part:
                                col, val = [p.strip() for p in part.split("=", 1)]
                                sub_mask &= df[col].astype(str).str.strip() == str(val)
                            else:
                                raise ValueError(f"Unsupported condition operator in '{part}'")
                        mask |= sub_mask   # OR together the groups

                    # Now parse the then_part: support "should be blank" or "should be answered"
                    then_q = then_part.split()[0].strip()
                    should_be_blank = "blank" in then_part.lower()

                    # If then_q doesn't exist, report dataset-level issue
                    if then_q not in df.columns:
                        report.append({
                            "RespondentID": None,
                            "Question": q,
                            "Check_Type": "Skip",
                            "Issue": f"Skip condition references missing target variable '{then_q}'"
                        })
                        continue

                    if should_be_blank:
                        # offenders: condition true AND target is answered (not null / not empty)
                        offenders = df.loc[mask & df[then_q].notna() & (df[then_q].astype(str).str.strip() != ""), "RespondentID"]
                        for rid in offenders:
                            report.append({"RespondentID": rid, "Question": q,
                                           "Check_Type": "Skip",
                                           "Issue": "Answered but should be blank"})
                        # satisfied: condition true AND target is blank -> exclude these from range/missing
                        satisfied = df.loc[mask & (df[then_q].isna() | (df[then_q].astype(str).str.strip() == "")), "RespondentID"].tolist()
                        skip_pass_ids = skip_pass_ids.union(set(satisfied))
                    else:
                        # should be answered: condition true AND target blank => offender
                        offenders = df.loc[mask & (df[then_q].isna() | (df[then_q].astype(str).str.strip() == "")), "RespondentID"]
                        for rid in offenders:
                            report.append({"RespondentID": rid, "Question": q,
                                           "Check_Type": "Skip",
                                           "Issue": "Blank but should be answered"})

                except Exception:
                    report.append({"RespondentID": None, "Question": q,
                                   "Check_Type": "Skip",
                                   "Issue": f"Invalid skip rule format ({condition})"})

            elif check_type == "Multi-Select":
                related_cols = [col for col in df.columns if col.startswith(q)]
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

