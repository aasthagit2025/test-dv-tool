import streamlit as st
import pandas as pd
import pyreadstat
import io

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

    # --- Load Rules ---
    rules_df = pd.read_excel(rules_file)

    # --- Validation Report Logic ---
    report = []
    for _, rule in rules_df.iterrows():
        q = rule["Question"]
        check_type = rule["Check_Type"]
        condition = rule["Condition"]

        if check_type == "Straightliner":
            # ✅ Handle multiple columns separated by commas
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
            continue  # Skip normal "Question not found" check for Straightliner

        # --- Normal Single-Column Checks ---
        if q not in df.columns and check_type not in ["Skip", "SkipRange", "Straightliner", "Multi-Select"]:
            report.append({"RespondentID": None, "Question": q, "Check_Type": check_type, "Issue": "Question not found in dataset"})
            continue

        if check_type == "Missing":
            missing = df[q].isna().sum()
            if missing > 0:
                offenders = df.loc[df[q].isna(), "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Missing", "Issue": "Value is missing"})

        elif check_type == "Range":
            try:
                min_val, max_val = map(float, condition.split("-"))
                offenders = df.loc[~df[q].between(min_val, max_val), "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Range", "Issue": f"Value out of range ({min_val}-{max_val})"})
            except:
                report.append({"RespondentID": None, "Question": q, "Check_Type": "Range", "Issue": "Invalid range condition"})

        elif check_type == "Skip":
            try:
                cond_parts = condition.split("then")
                if_part, then_part = cond_parts[0].strip(), cond_parts[1].strip()
                if_q, if_val = if_part.replace("If", "").strip().split("=")
                then_q = then_part.split()[0]
                subset = df[df[if_q.strip()] == int(if_val.strip())]
                offenders = subset.loc[subset[then_q].notna(), "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Skip", "Issue": "Answered but should be blank"})
            except:
                report.append({"RespondentID": None, "Question": q, "Check_Type": "Skip", "Issue": "Invalid skip rule format"})

        elif check_type == "SkipRange":
            try:
                # Format: If Q2=2 then Q3 1-99999
                cond_parts = condition.split("then")
                if_part, then_part = cond_parts[0].strip(), cond_parts[1].strip()

                # Parse IF part
                if_q, if_val = if_part.replace("If", "").strip().split("=")
                if_q, if_val = if_q.strip(), int(if_val.strip())

                # Parse THEN part
                then_tokens = then_part.split()
                then_q = then_tokens[0].strip()

                # Extract valid range (e.g., "1-99999")
                valid_range = None
                if len(then_tokens) > 1:
                    try:
                        min_val, max_val = map(float, then_tokens[1].split("-"))
                        valid_range = (min_val, max_val)
                    except:
                        pass

                for idx, row in df.iterrows():
                    rid = row["RespondentID"]

                    if row[if_q] == if_val:
                        # Must be within range (if defined)
                        if pd.isna(row[then_q]):
                            report.append({
                                "RespondentID": rid,
                                "Question": q,
                                "Check_Type": "SkipRange",
                                "Issue": "Missing but required"
                            })
                        elif valid_range and not (valid_range[0] <= row[then_q] <= valid_range[1]):
                            report.append({
                                "RespondentID": rid,
                                "Question": q,
                                "Check_Type": "SkipRange",
                                "Issue": f"Out of range ({valid_range[0]}-{valid_range[1]})"
                            })
                    else:
                        # Should be blank
                        if pd.notna(row[then_q]):
                            report.append({
                                "RespondentID": rid,
                                "Question": q,
                                "Check_Type": "SkipRange",
                                "Issue": "Answered but should be blank"
                            })

            except Exception as e:
                report.append({
                    "RespondentID": None,
                    "Question": q,
                    "Check_Type": "SkipRange",
                    "Issue": f"Invalid SkipRange rule format ({e})"
                })

        elif check_type == "Multi-Select":
            related_cols = [col for col in df.columns if col.startswith(q)]
            for col in related_cols:
                offenders = df.loc[~df[col].isin([0, 1]), "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": col, "Check_Type": "Multi-Select", "Issue": "Invalid value (not 0/1)"})
            if len(related_cols) > 0:
                offenders = df.loc[df[related_cols].sum(axis=1) == 0, "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": q, "Check_Type": "Multi-Select", "Issue": "No options selected"})

        elif check_type == "OpenEnd_Junk":
            junk = df[q].astype(str).str.len() < 3
            offenders = df.loc[junk, "RespondentID"]
            for rid in offenders:
                report.append({"RespondentID": rid, "Question": q, "Check_Type": "OpenEnd_Junk", "Issue": "Open-end looks like junk/low-effort"})

        elif check_type == "Duplicate":
            duplicate_ids = df[df.duplicated(subset=[q], keep=False)]["RespondentID"]
            for rid in duplicate_ids:
                report.append({"RespondentID": rid, "Question": q, "Check_Type": "Duplicate", "Issue": "Duplicate value found"})

    report_df = pd.DataFrame(report)

    st.write("### Validation Report (detailed by Respondent)")
    st.dataframe(report_df)

    # --- ✅ Download Report ---
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        report_df.to_excel(writer, index=False, sheet_name="Validation Report")

    st.download_button(
        label="Download Validation Report",
        data=output.getvalue(),
        file_name="validation_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
