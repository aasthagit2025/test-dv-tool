import streamlit as st
import pandas as pd
import pyreadstat
import io
import re

st.title("Survey Data Validation Tool")

# --- File Upload ---
data_file = st.file_uploader("Upload your survey data file (CSV, Excel, or SPSS)", type=["csv", "xlsx", "sav"])
rules_file = st.file_uploader("Upload validation rules (Excel)", type=["xlsx"])

if data_file and rules_file:
    # --- Load Data ---
    if data_file.name.endswith(".csv"):
        df = pd.read_csv(data_file)
    elif data_file.name.endswith(".xlsx"):
        df = pd.read_excel(data_file)
    elif data_file.name.endswith(".sav"):
        df, meta = pyreadstat.read_sav(data_file)
    else:
        st.error("Unsupported file type")
        st.stop()

    # Ensure RespondentID exists
    if "RespondentID" not in df.columns:
        df.insert(0, "RespondentID", range(1, len(df) + 1))

    # --- Load Rules ---
    rules_df = pd.read_excel(rules_file)

    # --- Validation Report Logic ---
    report = []
    for _, rule in rules_df.iterrows():
        q = rule["Question"]
        check_type = rule["Check_Type"]
        condition = str(rule.get("Condition", "")).strip()

        if q not in df.columns and check_type not in ["Multi-Select", "Straightliner"]:
            report.append({"RespondentID": "-", "Question": q, "Check_Type": check_type, "Issue": "Question not found"})
            continue

        # 1. Missing (Required)
        if check_type == "Missing":
            missing_ids = df[df[q].isna()]["RespondentID"].tolist()
            for rid in missing_ids:
                report.append({"RespondentID": rid, "Question": q, "Check_Type": "Missing", "Issue": "Missing value"})

        # 2. Range
        elif check_type == "Range":
            try:
                min_val, max_val = map(int, condition.split("-"))
                invalid = df[~df[q].between(min_val, max_val)]
                for rid in invalid["RespondentID"]:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Range",
                                   "Issue": f"Out of range (Allowed {min_val}-{max_val})"})
            except:
                report.append({"RespondentID": "-", "Question": q, "Check_Type": "Range", "Issue": "Invalid range rule"})

        # 3. Skip / Dependency
        elif check_type == "Skip":
            try:
                cond_parts = condition.split("then")
                if_part, then_part = cond_parts[0].strip(), cond_parts[1].strip()
                if_q, if_val = if_part.replace("If", "").strip().split("=")
                then_q = then_part.split()[0]
                subset = df[df[if_q.strip()] == int(if_val.strip())]
                invalid = subset[subset[then_q].notna()]
                for rid in invalid["RespondentID"]:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Skip",
                                   "Issue": f"Invalid skip logic (If {if_q}={if_val}, {then_q} should be blank)"})
            except Exception as e:
                report.append({"RespondentID": "-", "Question": q, "Check_Type": "Skip",
                               "Issue": f"Invalid skip rule format ({e})"})

        # 4. Multi-Select
        elif check_type == "Multi-Select":
            related_cols = [col for col in df.columns if col.startswith(q)]
            for col in related_cols:
                invalid = df[~df[col].isin([0, 1])]
                for rid in invalid["RespondentID"]:
                    report.append({"RespondentID": rid, "Question": col, "Check_Type": "Multi-Select",
                                   "Issue": "Invalid value (not 0/1)"})
            if len(related_cols) > 0:
                zero_sum = df[df[related_cols].sum(axis=1) == 0]
                for rid in zero_sum["RespondentID"]:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Multi-Select",
                                   "Issue": "Selected none of the options"})

        # 5. Straightliner
        elif check_type == "Straightliner":
            related_cols = [col for col in df.columns if col.startswith(q)]
            if len(related_cols) > 1:
                straightliners = df[df[related_cols].nunique(axis=1) == 1]
                for rid in straightliners["RespondentID"]:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Straightliner",
                                   "Issue": "Straightliner pattern detected"})

        # 6. Open-end Junk / AI-like
        elif check_type == "OpenEnd_Junk":
            junk = df[df[q].astype(str).str.len() < 3]
            for rid in junk["RespondentID"]:
                report.append({"RespondentID": rid, "Question": q, "Check_Type": "OpenEnd_Junk",
                               "Issue": "Too short / junk response"})

            ai_like = df[df[q].astype(str).str.contains(r"(As an AI|ChatGPT|I am an AI|language model)", case=False, na=False)]
            for rid in ai_like["RespondentID"]:
                report.append({"RespondentID": rid, "Question": q, "Check_Type": "OpenEnd_AI",
                               "Issue": "AI-generated looking response"})

        # 7. Duplicate Respondent IDs
        elif check_type == "Duplicate":
            duplicate_ids = df[df.duplicated(subset=[q])]["RespondentID"].tolist()
            for rid in duplicate_ids:
                report.append({"RespondentID": rid, "Question": q, "Check_Type": "Duplicate",
                               "Issue": "Duplicate ID"})

    # --- Final Report ---
    report_df = pd.DataFrame(report)

    st.write("### Validation Report")
    if not report_df.empty:
        st.dataframe(report_df)
    else:
        st.success("No validation issues found ✅")

    # --- Download Validation Report ---
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        report_df.to_excel(writer, index=False, sheet_name="Validation Report")
    st.download_button(
        label="Download Validation Report",
        data=output.getvalue(),
        file_name="validation_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
