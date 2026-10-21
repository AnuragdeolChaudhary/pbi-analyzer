import io
import streamlit as st
import pandas as pd
from parsers import parse_file

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

# ---- tabs
tabs = st.tabs(["Tables & Columns", "Relationships", "Power Query (M)", "DAX Measures",
                "Calculated Columns/Tables", "Statistics"])

with tabs[0]:
    st.dataframe(data["tables"], use_container_width=True)
    st.subheader("Columns")
    cols = data["columns"]
    if len(cols) and "TableName" in cols:
        pick = st.selectbox("Filter by table", ["All"] + sorted(cols["TableName"].unique().tolist()))
        cols = cols if pick == "All" else cols[cols["TableName"] == pick]
    st.dataframe(cols, use_container_width=True)

with tabs[1]:
    st.dataframe(data["relationships"], use_container_width=True)

with tabs[2]:
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

with tabs[3]:
    ms = data["measures"]
    st.dataframe(ms, use_container_width=True)
    for _, row in ms.iterrows():
        with st.expander(f"{row['TableName']} → {row['Name']}"):
            st.code(row["Expression"], language="sql")

with tabs[4]:
    st.subheader("Calculated columns")
    st.dataframe(data["calculated_columns"], use_container_width=True)
    st.subheader("Calculated tables")
    st.dataframe(data["calculated_tables"], use_container_width=True)

with tabs[5]:
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
        for key, label in [("tables", "Tables"), ("columns", "Columns"),
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
