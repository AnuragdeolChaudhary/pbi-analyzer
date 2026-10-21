"""Extract Power BI model metadata from .pbix (via PBIXRay) or .pbit (plain JSON)."""
import json
import zipfile
import tempfile
import os
import pandas as pd


def _empty(cols):
    return pd.DataFrame(columns=cols)


def _decode(raw: bytes) -> str:
    """DataModelSchema in a .pbit is usually UTF-16 LE; fall back to UTF-8."""
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    raise ValueError("Cannot decode DataModelSchema")


def _join(expr):
    return "\n".join(expr) if isinstance(expr, list) else (expr or "")


# ---------------------------------------------------------------- PBIT
def parse_pbit(file_obj) -> dict:
    with zipfile.ZipFile(file_obj) as z:
        names = {n.lower(): n for n in z.namelist()}
        if "datamodelschema" not in names:
            raise ValueError("DataModelSchema not found - is this a valid .pbit file?")
        schema = json.loads(_decode(z.read(names["datamodelschema"])))

    model = schema.get("model", {})
    tables, cols, measures, calc_cols, calc_tables, pq = [], [], [], [], [], []

    for t in model.get("tables", []):
        tname = t["name"]
        tables.append({"TableName": tname, "IsHidden": t.get("isHidden", False)})
        for c in t.get("columns", []):
            if c.get("type") == "rowNumber":
                continue
            cols.append({
                "TableName": tname, "ColumnName": c["name"],
                "DataType": c.get("dataType"), "IsHidden": c.get("isHidden", False),
                "SummarizeBy": c.get("summarizeBy"), "SourceColumn": c.get("sourceColumn"),
            })
            if c.get("type") == "calculated":
                calc_cols.append({"TableName": tname, "ColumnName": c["name"],
                                  "Expression": _join(c.get("expression"))})
        for m in t.get("measures", []):
            measures.append({
                "TableName": tname, "Name": m["name"],
                "Expression": _join(m.get("expression")),
                "FormatString": m.get("formatString"),
                "DisplayFolder": m.get("displayFolder"),
                "Description": m.get("description"),
            })
        for p in t.get("partitions", []):
            src = p.get("source", {})
            if src.get("type") == "m":
                pq.append({"TableName": tname, "Expression": _join(src.get("expression"))})
            elif src.get("type") == "calculated":
                calc_tables.append({"TableName": tname, "Expression": _join(src.get("expression"))})

    rels = []
    for r in model.get("relationships", []):
        rels.append({
            "FromTable": r.get("fromTable"), "FromColumn": r.get("fromColumn"),
            "ToTable": r.get("toTable"), "ToColumn": r.get("toColumn"),
            "IsActive": r.get("isActive", True),
            "FromCardinality": r.get("fromCardinality", "many"),
            "ToCardinality": r.get("toCardinality", "one"),
            "CrossFilteringBehavior": r.get("crossFilteringBehavior", "oneDirection"),
        })

    exprs = [{"Name": e["name"], "Expression": _join(e.get("expression"))}
             for e in model.get("expressions", [])]

    return {
        "tables": pd.DataFrame(tables) if tables else _empty(["TableName", "IsHidden"]),
        "columns": pd.DataFrame(cols) if cols else _empty(["TableName", "ColumnName"]),
        "relationships": pd.DataFrame(rels) if rels else _empty(["FromTable", "FromColumn", "ToTable", "ToColumn"]),
        "power_query": pd.DataFrame(pq) if pq else _empty(["TableName", "Expression"]),
        "parameters_and_queries": pd.DataFrame(exprs) if exprs else _empty(["Name", "Expression"]),
        "measures": pd.DataFrame(measures) if measures else _empty(["TableName", "Name", "Expression"]),
        "calculated_columns": pd.DataFrame(calc_cols) if calc_cols else _empty(["TableName", "ColumnName", "Expression"]),
        "calculated_tables": pd.DataFrame(calc_tables) if calc_tables else _empty(["TableName", "Expression"]),
        "statistics": None,
        "model_size": None,
    }


# ---------------------------------------------------------------- PBIX
def parse_pbix(file_obj) -> dict:
    from pbixray import PBIXRay  # imported lazily so .pbit works without it

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pbix") as tmp:
        tmp.write(file_obj.read())
        path = tmp.name
    try:
        m = PBIXRay(path)

        def get(attr, cols):
            try:
                v = getattr(m, attr)
                return v if isinstance(v, pd.DataFrame) else pd.DataFrame(v)
            except Exception:
                return _empty(cols)

        try:
            tables = pd.DataFrame({"TableName": list(m.tables)})
        except Exception:
            tables = _empty(["TableName"])

        try:
            size = m.size
        except Exception:
            size = None

        return {
            "tables": tables,
            "columns": get("schema", ["TableName", "ColumnName", "PandasDataType"]),
            "relationships": get("relationships", ["FromTableName", "FromColumnName", "ToTableName", "ToColumnName"]),
            "power_query": get("power_query", ["TableName", "Expression"]),
            "parameters_and_queries": get("m_parameters", ["ParameterName", "Description", "Expression"]),
            "measures": get("dax_measures", ["TableName", "Name", "Expression"]),
            "calculated_columns": get("dax_columns", ["TableName", "ColumnName", "Expression"]),
            "calculated_tables": get("dax_tables", ["TableName", "Expression"]),
            "statistics": get("statistics", ["TableName", "ColumnName", "Cardinality"]),
            "model_size": size,
        }
    finally:
        os.unlink(path)


def parse_file(uploaded) -> dict:
    name = uploaded.name.lower()
    if name.endswith(".pbit"):
        return parse_pbit(uploaded)
    if name.endswith(".pbix"):
        return parse_pbix(uploaded)
    raise ValueError("Please upload a .pbix or .pbit file")
