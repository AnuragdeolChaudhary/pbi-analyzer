import io
import streamlit as st
import pandas as pd
from parsers import parse_file
from m_explainer import explain_all

st.set_page_config(page_title="Power BI Model Analyzer", page_icon="📊", layout="wide")
st.title("📊 Power BI Model Analyzer")
st.caption("Upload a .pbix or .pbit to extract tables, relationships, Power Query (M) and DAX. "
           "Files are processed in memory and never stored.")

with st.sidebar:
    st.header("Tips")
    st.markdown(
        "- **.pbit** = no data, tiny, safest to upload.\n"
        "  In Power BI Desktop: *File → Export → Power BI template*.\n"
        "- **.pbix** also gives column size/cardinality stats for performance work."
    )

uploaded = st.file_uploader("Upload your Power BI file", type=["pbix", "pbit"])
if not uploaded:
    st.info("Upload a file to begin.")
    st.stop()

try:
    with st.spinner("Reading model..."):
        data = parse_file(uploaded)
except Exception as e:
    st.error(f"Could not read this file: {e}")
    st.stop()

# ---- summary metrics
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Tables", len(data["tables"]))
c2.metric("Relationships", len(data["relationships"]))
c3.metric("Power Query tables", len(data["power_query"]))
c4.metric("DAX measures", len(data["measures"]))
c5.metric("Calculated columns", len(data["calculated_columns"]))
if data["model_size"]:
    st.caption(f"Model size: {data['model_size'] / 1_048_576:,.1f} MB")

# ---- explain Power Query in plain English
steps_df, sources_df, summ_df = explain_all(data["power_query"])
data["pq_summary"], data["pq_sources"], data["pq_steps"] = summ_df, sources_df, steps_df.drop(columns=["M code"], errors="ignore") if len(steps_df) else steps_df

# ---- tabs
tabs = st.tabs(["🔍 Power Query: what was done", "Tables & Columns", "Relationships", "Power Query (raw M)",
                "DAX Measures", "Calculated Columns/Tables", "Statistics"])

with tabs[0]:
    if steps_df.empty:
        st.write("No Power Query transformations found.")
    else:
        UP = "Move upstream (Silver/Gold)"
        core = steps_df[steps_df["Category"] != "Source & connection"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Transformation steps", len(core))
        m2.metric("Can move to Gold layer", int((steps_df["Gold layer: where it belongs"] == UP).sum()))
        m3.metric("Steps with no effect", int((steps_df["Gold layer: where it belongs"] == "Not needed").sum()))

        st.subheader("1. Where each table gets its data")
        st.dataframe(sources_df, use_container_width=True, hide_index=True)

        st.subheader("2. What kinds of transformations were used")
        if len(core):
            st.dataframe(core.groupby(["Table", "Category"]).size().unstack(fill_value=0), use_container_width=True)

        st.subheader("3. Step-by-step explanation per table")
        show_cols = ["#", "Step name", "What it does (plain English)", "Gold layer: where it belongs", "Why / what to do", "Query folding"]
        for _, r in summ_df.iterrows():
            t = r["Table"]
            with st.expander(f"📄 {t}", expanded=len(summ_df) == 1):
                st.info(r["Summary"])
                st.dataframe(steps_df[steps_df["Table"] == t][show_cols], use_container_width=True, hide_index=True)
                if st.checkbox("Show raw M code", key=f"raw_{t}"):
                    st.code(data["power_query"].loc[data["power_query"]["TableName"] == t, "Expression"].iloc[0], language="text")

        st.subheader("4. Gold layer to-do list")
        todo = steps_df[steps_df["Gold layer: where it belongs"] == UP][["Table", "#", "Operation", "What it does (plain English)", "Why / what to do"]]
        st.dataframe(todo, use_container_width=True, hide_index=True)
        data["gold_todo"] = todo

with tabs[1]:
    st.dataframe(data["tables"], use_container_width=True)
    st.subheader("Columns")
    cols = data["columns"]
    if len(cols) and "TableName" in cols:
        pick = st.selectbox("Filter by table", ["All"] + sorted(cols["TableName"].unique().tolist()))
        cols = cols if pick == "All" else cols[cols["TableName"] == pick]
    st.dataframe(cols, use_container_width=True)

with tabs[2]:
    st.dataframe(data["relationships"], use_container_width=True)

with tabs[3]:
    pq = data["power_query"]
    if pq.empty:
        st.write("No Power Query expressions found.")
    for _, row in pq.iterrows():
        with st.expander(f"📄 {row['TableName']}"):
            st.code(row["Expression"], language="text")
    extra = data["parameters_and_queries"]
    if len(extra):
        st.subheader("Parameters / shared queries")
        st.dataframe(extra, use_container_width=True)

with tabs[4]:
    ms = data["measures"]
    st.dataframe(ms, use_container_width=True)
    for _, row in ms.iterrows():
        with st.expander(f"{row['TableName']} → {row['Name']}"):
            st.code(row["Expression"], language="sql")

with tabs[5]:
    st.subheader("Calculated columns")
    st.dataframe(data["calculated_columns"], use_container_width=True)
    st.subheader("Calculated tables")
    st.dataframe(data["calculated_tables"], use_container_width=True)

with tabs[6]:
    stats = data["statistics"]
    if stats is None or stats.empty:
        st.write("Column statistics are only available for .pbix uploads.")
    else:
        sort_col = "DataSize" if "DataSize" in stats.columns else stats.columns[0]
        st.dataframe(stats.sort_values(sort_col, ascending=False), use_container_width=True)

# ---- Excel export
def build_excel(d) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for key, label in [("pq_summary", "PQ_Summary"), ("pq_sources", "PQ_Sources"), ("pq_steps", "PQ_Steps_Explained"),
                           ("gold_todo", "Gold_Layer_Todo"), ("tables", "Tables"), ("columns", "Columns"),
                           ("relationships", "Relationships"), ("power_query", "PowerQuery_M"),
                           ("parameters_and_queries", "Parameters"), ("measures", "DAX_Measures"),
                           ("calculated_columns", "Calc_Columns"), ("calculated_tables", "Calc_Tables"),
                           ("statistics", "Statistics")]:
            df = d.get(key)
            if df is not None and not df.empty:
                df.to_excel(xw, sheet_name=label[:31], index=False)
    return buf.getvalue()

st.divider()
st.download_button("⬇️ Download full documentation (Excel)", build_excel(data),
                   file_name=f"{uploaded.name.rsplit('.', 1)[0]}_model_doc.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
