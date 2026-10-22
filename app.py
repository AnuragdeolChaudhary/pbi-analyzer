import io
import pandas as pd
import streamlit as st

from parsers import parse_file
from m_explainer import explain_all
from dax_explainer import explain_measures, explain_calc_columns, explain_calc_tables
from doc_export import build_docx
from doc_preview import build_sections

st.set_page_config(page_title="Power BI Model Analyzer", page_icon="📊", layout="wide")
st.title("📊 Power BI Model Analyzer")
st.caption("Upload a .pbix or .pbit to get a plain-English explanation of every Power Query step and DAX measure, "
           "with Gold-layer advice. Files are processed in memory and never stored.")

with st.sidebar:
    st.header("Tips")
    st.markdown(
        "- **.pbit** = no data, tiny, safest to upload.\n"
        "  In Power BI Desktop: *File → Export → Power BI template*.\n"
        "- **.pbix** also gives column size/cardinality statistics."
    )
    include_m = st.checkbox("Include original M code in Word document", value=True)

uploaded = st.file_uploader("Upload your Power BI file", type=["pbix", "pbit"])
if not uploaded:
    st.info("Upload a file to begin.")
    st.stop()
raw, fname = uploaded.getvalue(), uploaded.name


@st.cache_data(show_spinner="Reading your model and writing the explanations…")
def analyse(name: str, raw: bytes):
    buf = io.BytesIO(raw)
    buf.name = name
    data = parse_file(buf)
    steps, sources, summ = explain_all(data["power_query"])
    meas, deps = explain_measures(data["measures"])
    names = list(data["measures"]["Name"]) if len(data["measures"]) else []
    cc = explain_calc_columns(data["calculated_columns"], names, data.get("statistics"))
    ct = explain_calc_tables(data["calculated_tables"])
    dax = dict(measures=meas, dependencies=deps, calc_columns=cc, calc_tables=ct)
    cols = data["columns"]
    tcol = next((c for c in ("DataType", "PandasDataType") if c in cols.columns), None)
    final_schema = None
    if tcol and "TableName" in cols:
        final_schema = cols[["TableName", "ColumnName", tcol]].rename(
            columns={"TableName": "Table", "ColumnName": "Column", tcol: "Type"})
    return data, steps, sources, summ, dax, final_schema


@st.cache_data(show_spinner="Building the Word document…")
def make_docx(name: str, raw: bytes, include_m: bool) -> bytes:
    data, steps, sources, summ, dax, fs = analyse(name, raw)
    return build_docx(name, steps, sources, summ, data["power_query"], fs, include_m, dax)


def build_excel(data, steps, sources, summ, dax) -> bytes:
    buf = io.BytesIO()
    sheets = [
        ("PQ_Summary", summ), ("PQ_Sources", sources),
        ("PQ_Steps_Explained", steps.drop(columns=["M code"], errors="ignore") if len(steps) else steps),
        ("DAX_Measures_Explained", dax["measures"].drop(columns=["_flags"], errors="ignore")),
        ("DAX_Dependencies", dax["dependencies"]), ("DAX_CalcColumns", dax["calc_columns"]), ("DAX_CalcTables", dax["calc_tables"]),
        ("Tables", data["tables"]), ("Columns", data["columns"]), ("Relationships", data["relationships"]),
        ("PowerQuery_M", data["power_query"]), ("Parameters", data["parameters_and_queries"]),
        ("DAX_Raw", data["measures"]), ("Statistics", data["statistics"]),
    ]
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for label, df in sheets:
            if df is not None and len(df):
                df.to_excel(xw, sheet_name=label[:31], index=False)
    return buf.getvalue()


try:
    data, steps_df, sources_df, summ_df, dax, final_schema = analyse(fname, raw)
except Exception as e:
    st.error(f"Could not read this file: {e}")
    st.stop()

meas_df = dax["measures"]
UP = "Move upstream (Silver/Gold)"

# ---------------------------------------------------------------- summary + downloads (top of page)
c = st.columns(6)
c[0].metric("Tables", len(data["tables"]))
c[1].metric("Relationships", len(data["relationships"]))
c[2].metric("Power Query steps", int((~steps_df["Operation"].isin(["Connect to source", "Navigate to object"])).sum()) if len(steps_df) else 0)
c[3].metric("DAX measures", len(data["measures"]))
c[4].metric("Calculated columns", len(data["calculated_columns"]))
c[5].metric("Can move to Gold", int((steps_df["Gold layer: where it belongs"] == UP).sum()) if len(steps_df) else 0)
if data["model_size"]:
    st.caption(f"Model size: {data['model_size'] / 1_048_576:,.1f} MB")

d1, d2, _ = st.columns([2, 2, 3])
try:
    d1.download_button("📄 Download Word document (Power Query + DAX)", make_docx(fname, raw, include_m),
                       file_name=f"{fname.rsplit('.', 1)[0]}_Documentation.docx", type="primary",
                       mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
except Exception as e:
    d1.warning(f"Could not build the Word document: {e}")
d2.download_button("⬇️ Download Excel", build_excel(data, steps_df, sources_df, summ_df, dax),
                   file_name=f"{fname.rsplit('.', 1)[0]}_model_doc.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

tabs = st.tabs(["📄 Document preview", "🔍 Power Query: what was done", "🧮 DAX: what it does",
                "Tables & Columns", "Relationships", "Power Query (raw M)", "Statistics"])

# ---------------------------------------------------------------- 0. in-app document preview
with tabs[0]:
    st.caption("This is the same content as the Word document, readable right here.")
    sections = build_sections(steps_df, sources_df, summ_df, dax)
    if not sections:
        st.write("Nothing to show yet.")
    with st.container(height=700):
        for i, (title, md) in enumerate(sections):
            with st.expander(title, expanded=(i < 2)):
                st.markdown(md)

# ---------------------------------------------------------------- 1. Power Query
with tabs[1]:
    if steps_df.empty:
        st.write("No Power Query transformations found.")
    else:
        core = steps_df[~steps_df["Operation"].isin(["Connect to source", "Navigate to object"])]
        st.subheader("1. Where each table gets its data")
        st.dataframe(sources_df, width="stretch", hide_index=True)
        st.subheader("2. What kinds of transformations were used")
        if len(core):
            st.dataframe(core.groupby(["Table", "Category"]).size().unstack(fill_value=0), width="stretch")
        st.subheader("3. Step-by-step explanation per table")
        show = ["#", "Step name", "What it does (plain English)", "Before this step", "After this step", "Rows impact", "Columns",
                "Gold layer: where it belongs", "Why / what to do", "Query folding"]
        for _, r in summ_df.iterrows():
            t = r["Table"]
            with st.expander(f"📄 {t}", expanded=len(summ_df) == 1):
                st.info(r["Summary"])
                st.dataframe(steps_df[steps_df["Table"] == t][show], width="stretch", hide_index=True)
                if st.checkbox("Show raw M code", key=f"raw_{t}"):
                    st.code(data["power_query"].loc[data["power_query"]["TableName"] == t, "Expression"].iloc[0], language="text")
        st.subheader("4. Gold layer to-do list")
        st.dataframe(steps_df[steps_df["Gold layer: where it belongs"] == UP][["Table", "#", "Operation", "What it does (plain English)", "Why / what to do"]],
                     width="stretch", hide_index=True)

# ---------------------------------------------------------------- 2. DAX
with tabs[2]:
    if meas_df.empty and dax["calc_columns"].empty and dax["calc_tables"].empty:
        st.write("No DAX measures, calculated columns or calculated tables found.")
    if not meas_df.empty:
        hi = int((meas_df["Worst finding"] == "High").sum())
        m1, m2, m3 = st.columns(3)
        m1.metric("Measures", len(meas_df)); m2.metric("With high-severity findings", hi)
        m3.metric("High complexity", int((meas_df["Complexity"] == "High").sum()))
        st.subheader("1. Measure catalogue")
        cat_cols = ["Table", "Measure", "Pattern", "Complexity", "Worst finding", "Depends on measures", "Gold layer: where it belongs"]
        f1, f2 = st.columns(2)
        pat = f1.multiselect("Filter by pattern", sorted(meas_df["Pattern"].unique()))
        sev = f2.multiselect("Filter by worst finding", ["High", "Medium", "Low", "Info", "OK"])
        view = meas_df
        if pat: view = view[view["Pattern"].isin(pat)]
        if sev: view = view[view["Worst finding"].isin(sev)]
        st.dataframe(view[cat_cols], width="stretch", hide_index=True)

        st.subheader("2. Explain one measure")
        pick = st.selectbox("Choose a measure", view["Measure"].tolist() or meas_df["Measure"].tolist())
        r = meas_df[meas_df["Measure"] == pick].iloc[0]
        st.code(r["DAX formula"], language="sql")
        st.markdown("**In plain English**")
        st.info(r["What it calculates (plain English)"])
        st.markdown(f"**Pattern:** {r['Pattern']}  ·  **Complexity:** {r['Complexity']}  \n"
                    f"**Uses columns:** {r['Uses columns'] or '—'}  \n"
                    f"**Depends on:** {r['Depends on measures'] or '— (base measure)'}  ·  **Used by:** {r['Used by measures'] or '—'}  \n"
                    f"**Filter context:** {r['Filter-context changes']}")
        for sv, msg in r["_flags"]:
            (st.error if sv == "High" else st.warning if sv in ("Medium", "Low") else st.success if sv == "OK" else st.info)(f"**{sv}:** {msg}")
        st.markdown(f"**Gold layer:** {r['Gold layer: where it belongs']}. {r['Gold layer advice']}")

        st.subheader("3. Dependency map")
        st.dataframe(dax["dependencies"].sort_values(["Dependency level", "Measure"], ascending=[False, True]), width="stretch", hide_index=True)
    if not dax["calc_columns"].empty:
        st.subheader("4. Calculated columns (move these to the Gold layer)")
        st.dataframe(dax["calc_columns"], width="stretch", hide_index=True)
    if not dax["calc_tables"].empty:
        st.subheader("5. Calculated tables")
        st.dataframe(dax["calc_tables"], width="stretch", hide_index=True)

# ---------------------------------------------------------------- remaining tabs
with tabs[3]:
    st.dataframe(data["tables"], width="stretch")
    st.subheader("Columns")
    cols = data["columns"]
    if len(cols) and "TableName" in cols:
        pick_t = st.selectbox("Filter by table", ["All"] + sorted(cols["TableName"].unique().tolist()))
        cols = cols if pick_t == "All" else cols[cols["TableName"] == pick_t]
    st.dataframe(cols, width="stretch")
with tabs[4]:
    st.dataframe(data["relationships"], width="stretch")
with tabs[5]:
    for _, row in data["power_query"].iterrows():
        with st.expander(f"📄 {row['TableName']}"):
            st.code(row["Expression"], language="text")
    if len(data["parameters_and_queries"]):
        st.subheader("Parameters / shared queries")
        st.dataframe(data["parameters_and_queries"], width="stretch")
with tabs[6]:
    stats = data["statistics"]
    if stats is None or stats.empty:
        st.write("Column statistics are only available for .pbix uploads.")
    else:
        sort_col = "DataSize" if "DataSize" in stats.columns else stats.columns[0]
        st.dataframe(stats.sort_values(sort_col, ascending=False), width="stretch")
