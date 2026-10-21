"""Rule-based (zero-cost) explainer: turns Power Query M code into plain-English steps."""
import re
from collections import Counter
import pandas as pd

_STR = re.compile(r'"(?:[^"]|"")*"')
_KW = re.compile(r'\b(let|in)\b')
_CALL = re.compile(r'^([A-Za-z_][\w.]*)\s*\(')


# ======================================================== low-level text helpers
def _mask(s):
    """Replace string contents with '_' (same length) so brackets/commas inside strings are ignored."""
    out, i, n, in_str = [], 0, len(s), False
    while i < n:
        c = s[i]
        if in_str:
            if c == '"':
                if i + 1 < n and s[i + 1] == '"':
                    out.append('__'); i += 2; continue
                in_str = False; out.append('"')
            else:
                out.append('_')
        else:
            out.append(c)
            if c == '"':
                in_str = True
        i += 1
    return ''.join(out)


def _blank_comments(s):
    m = _mask(s)
    chars = list(s)
    for mt in re.finditer(r'//[^\n]*|/\*.*?\*/', m, re.S):
        for i in range(mt.start(), mt.end()):
            if chars[i] != '\n':
                chars[i] = ' '
    return ''.join(chars)


def _split_top(orig, masked, sep=',', lo=0, hi=None):
    hi = len(orig) if hi is None else hi
    events = {m.start(): (1 if m.group(1) == 'let' else -1) for m in _KW.finditer(masked, lo, hi)}
    depth, start, i, parts = 0, lo, lo, []
    while i < hi:
        if i in events:
            depth += events[i]
            i += 3 if events[i] == 1 else 2
            continue
        c = masked[i]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(orig[start:i].strip()); start = i + 1
        i += 1
    parts.append(orig[start:hi].strip())
    return [p for p in parts if p != '']


def _split(s, sep=','):
    return _split_top(s, _mask(s), sep)


def _unq(s):
    s = s.strip()
    m = re.fullmatch(r'#?"((?:[^"]|"")*)"', s, re.S)
    return m.group(1).replace('""', '"') if m else s


def _is_lit(s):
    return re.fullmatch(r'"(?:[^"]|"")*"', s.strip(), re.S) is not None


def _inner(s):
    s = s.strip()
    return s[1:-1] if s.startswith('{') and s.endswith('}') else s


def _items(s):
    s = s.strip()
    return _split(_inner(s)) if s.startswith('{') else ([s] if s else [])


def _short(t, n=60):
    t = re.sub(r'\s+', ' ', t.strip())
    return t if len(t) <= n else t[:n - 1] + '…'


def _cols(arg):
    a = arg.strip()
    if a.startswith('{'):
        return [_unq(it) if (_is_lit(it) or it.startswith('#"')) else f'<{_short(it, 40)}>' for it in _items(a)]
    if _is_lit(a):
        return [_unq(a)]
    return [f'<dynamic list: {_short(a, 40)}>'] if a else []


def _fmt(items, limit=8):
    items = list(items)
    s = ', '.join(items[:limit])
    return s + (f' … (+{len(items) - limit} more)' if len(items) > limit else '')


def g(a, i, default=''):
    return a[i] if len(a) > i else default


def _outside(text, fn):
    out, last = [], 0
    for m in _STR.finditer(text):
        out.append(fn(text[last:m.start()])); out.append(m.group(0)); last = m.end()
    out.append(fn(text[last:]))
    return ''.join(out)


def _pretty(expr, maxlen=260):
    t = re.sub(r'\s+', ' ', expr.strip())
    t = re.sub(r'^(?:\(\w+\)\s*=>|each)\s+', '', t)
    t = re.sub(r'#"((?:[^"]|"")*)"', lambda m: m.group(1).replace('""', '"'), t)
    t = re.sub(r'Text\.Contains\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*(?:,[^()]*)?\)', r'\1 contains \2', t)
    t = re.sub(r'Text\.StartsWith\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)', r'\1 starts with \2', t)
    t = re.sub(r'Text\.EndsWith\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)', r'\1 ends with \2', t)
    t = re.sub(r'List\.Contains\(\s*(\{[^}]*\})\s*,\s*([^,()]+?)\s*\)', r'\2 is one of \1', t)

    def fix(seg):
        seg = re.sub(r'_?\[([^\]]+)\]', r'\1', seg)
        seg = seg.replace('<>', ' ≠ ')
        for w, r in (('and', 'AND'), ('or', 'OR'), ('not', 'NOT'), ('null', 'empty'),
                     ('if', 'IF'), ('then', 'THEN'), ('else', 'ELSE')):
            seg = re.sub(rf'\b{w}\b', r, seg)
        return seg
    t = re.sub(r'\s+', ' ', _outside(t, fix)).strip()
    return t if len(t) <= maxlen else t[:maxlen - 1] + '…'


def _expr_hint(expr):
    hints = []
    e = expr
    stripped = _outside(e, lambda s: s)
    if re.search(r'\bif\b.*\bthen\b', e, re.S): hints.append('conditional IF/ELSE logic')
    if re.search(r'\bText\.|&', stripped): hints.append('text manipulation')
    if re.search(r'\b(Date|DateTime|Duration)\.', e): hints.append('date calculation')
    if re.search(r'[\]\w)]\s*[+\-*/]\s*[\[\w(]', _outside(e, lambda s: s)) and 'Text.' not in e: hints.append('arithmetic')
    if re.search(r'\b(Web\.Contents|Sql\.|Excel\.Workbook|File\.Contents)', e): hints.append('calls an external source per row (slow)')
    if '=>' in e and 'each' not in e[:6]: hints.append('custom function')
    return hints


_TYPES = {'text': 'Text', 'number': 'Decimal number', 'date': 'Date', 'datetime': 'Date/Time',
          'datetimezone': 'Date/Time/Timezone', 'time': 'Time', 'logical': 'True/False', 'any': 'Any',
          'binary': 'Binary', 'duration': 'Duration', 'int64.type': 'Whole number', 'int32.type': 'Whole number',
          'int16.type': 'Whole number', 'int8.type': 'Whole number', 'currency.type': 'Fixed decimal (currency)',
          'percentage.type': 'Percentage', 'single.type': 'Decimal number', 'double.type': 'Decimal number',
          'decimal.type': 'Decimal number'}


def _type_label(tok):
    t = re.sub(r'^type\s+', '', tok.strip().lower()).replace('nullable ', '')
    return _TYPES.get(t, tok.strip())


# ======================================================== parsing a query
def _parse_let(code):
    """Return [(step_name, expression)] for a let...in query, or None if it is not one."""
    code = _blank_comments(code or '').strip()
    masked = _mask(code)
    if not masked.startswith('let'):
        return None
    depth, in_pos = 0, None
    for m in _KW.finditer(masked):
        depth += 1 if m.group(1) == 'let' else -1
        if depth == 0:
            in_pos = m.start(); break
    if in_pos is None:
        in_pos = len(code)
    steps = []
    for p in _split_top(code, masked, ',', 3, in_pos):
        pm = _mask(p)
        eq = pm.find('=')
        if eq < 0:
            continue
        steps.append((_unq(p[:eq].strip()), p[eq + 1:].strip()))
    return steps


def _call(expr):
    m = _CALL.match(expr)
    if not m:
        return None, []
    masked = _mask(expr)
    depth, end, start = 0, len(expr), m.end() - 1
    for i in range(start, len(expr)):
        if masked[i] == '(':
            depth += 1
        elif masked[i] == ')':
            depth -= 1
            if depth == 0:
                end = i; break
    inner = expr[start + 1:end]
    return m.group(1), _split(inner)


# ======================================================== sources
SRC = {
    'Sql.Database': ('SQL Server / Azure SQL / Synapse', True), 'Sql.Databases': ('SQL Server', True),
    'PostgreSQL.Database': ('PostgreSQL', True), 'MySQL.Database': ('MySQL', True), 'Oracle.Database': ('Oracle', True),
    'Snowflake.Databases': ('Snowflake', True), 'GoogleBigQuery.Database': ('Google BigQuery', True),
    'AmazonRedshift.Database': ('Amazon Redshift', True), 'SapHana.Database': ('SAP HANA', True),
    'Teradata.Database': ('Teradata', True), 'Databricks.Catalogs': ('Databricks', True),
    'Databricks.Contents': ('Databricks', True), 'Lakehouse.Contents': ('Fabric Lakehouse', True),
    'Fabric.Warehouse': ('Fabric Warehouse', True), 'AzureDataExplorer.Contents': ('Azure Data Explorer', True),
    'Odbc.DataSource': ('ODBC database', True), 'Odbc.Query': ('ODBC database', True),
    'OleDb.DataSource': ('OLE DB database', True),
    'Excel.Workbook': ('Excel file', False), 'Excel.CurrentWorkbook': ('Excel table (this workbook)', False),
    'Csv.Document': ('CSV / text file', False), 'Json.Document': ('JSON', False), 'Xml.Tables': ('XML', False),
    'Pdf.Tables': ('PDF', False), 'Parquet.Document': ('Parquet file', False), 'Access.Database': ('Access database', False),
    'Web.Contents': ('Web / API', False), 'Web.Page': ('Web page', False), 'Web.BrowserContents': ('Web page', False),
    'SharePoint.Files': ('SharePoint files', False), 'SharePoint.Tables': ('SharePoint list', False),
    'SharePoint.Contents': ('SharePoint', False), 'Folder.Files': ('Folder (all files inside)', False),
    'Folder.Contents': ('Folder', False), 'OData.Feed': ('OData feed', False),
    'AzureStorage.DataLake': ('Azure Data Lake Storage', False), 'AzureStorage.Blobs': ('Azure Blob Storage', False),
    'AnalysisServices.Database': ('Analysis Services / Power BI semantic model', False),
    'PowerBI.Dataflows': ('Power BI Dataflow', False), 'Salesforce.Data': ('Salesforce', False),
    'Salesforce.Reports': ('Salesforce', False), 'CommonDataService.Database': ('Dataverse', False),
    'GoogleAnalytics.Accounts': ('Google Analytics', False),
}
_DB_FN = {'Sql.Database', 'Sql.Databases', 'PostgreSQL.Database', 'MySQL.Database', 'Oracle.Database',
          'AmazonRedshift.Database', 'SapHana.Database', 'Teradata.Database', 'GoogleBigQuery.Database'}


def _arg_text(x):
    x = x.strip()
    if not x or x.startswith('['):
        return ''
    return _unq(x) if _is_lit(x) else f'{_short(x, 30)} (parameter)'


def _source(fn, args, expr):
    stype, is_db = SRC[fn]
    detail, sql = '', None
    if fn in _DB_FN:
        s, d = _arg_text(g(args, 0)), _arg_text(g(args, 1))
        detail = f'server {s}' + (f', database {d}' if d else '')
        m = re.search(r'Query\s*=\s*("(?:[^"]|"")*")', expr, re.S)
        if m:
            sql = _unq(m.group(1))
    else:
        lits = [_unq(x) for x in _STR.findall(expr)]
        if lits:
            path = lits[0]
            name = re.split(r'[\\/]', path.rstrip('\\/'))[-1]
            detail = f'{name} ({path})' if name and name != path else path
        else:
            m = re.search(r'Contents\(\s*([^,)]+)', expr)
            detail = f'path/URL from {m.group(1).strip()}' if m else ''
    return dict(stype=stype, detail=detail, is_db=is_db, sql=sql)


def _nav(expr):
    m = re.match(r'^(.+?)\{\s*(.+?)\s*\}\s*\[(\w+)\]$', expr.strip(), re.S)
    if not m:
        return None
    key = m.group(2)
    kv = {k: v.replace('""', '"') for k, v in re.findall(r'(\w+)\s*=\s*"((?:[^"]|"")*)"', key)}
    if kv.get('Schema') and kv.get('Item'):
        return f"table {kv['Schema']}.{kv['Item']}"
    kind = kv.get('Kind', '').lower()
    if kv.get('Item'):
        return f"{kind or 'item'} \"{kv['Item']}\""
    if kv.get('Name'):
        return f"\"{kv['Name']}\""
    if re.fullmatch(r'\d+', key.strip()):
        return f'item #{int(key) + 1} of the source (e.g. first file/sheet)'
    return 'an object inside the source'


# ======================================================== step handlers: (operation, category, description)
C_SRC, C_TYPE, C_COL, C_ROW, C_CLEAN, C_NEW, C_COMB, C_SHAPE, C_SORT, C_OTH = (
    'Source & connection', 'Data types', 'Column selection', 'Row filtering', 'Cleaning',
    'New columns', 'Combining tables', 'Reshaping', 'Sorting', 'Other')


def h_types(a):
    pairs = []
    for it in _items(g(a, 1)):
        p = _split(_inner(it))
        if len(p) >= 2:
            pairs.append(f'{_unq(p[0])} → {_type_label(p[1])}')
    return 'Change data types', C_TYPE, f'Sets the data type of {len(pairs)} column(s): {_fmt(pairs)}'


def h_rename(a):
    pairs = []
    for it in _items(g(a, 1)):
        p = _split(_inner(it))
        if len(p) >= 2:
            pairs.append(f'{_unq(p[0])} → {_unq(p[1])}')
    return 'Rename columns', C_COL, f'Renames {len(pairs)} column(s): {_fmt(pairs)}'


def h_remove_cols(a):
    c = _cols(g(a, 1))
    return 'Remove columns', C_COL, f'Removes {len(c)} column(s): {_fmt(c)}'


def h_select_cols(a):
    c = _cols(g(a, 1))
    return 'Keep columns', C_COL, f'Keeps only {len(c)} column(s), everything else is dropped: {_fmt(c)}'


def h_reorder(a):
    return 'Reorder columns', C_COL, 'Changes the left-to-right order of columns (no effect on the data).'


def h_filter(a):
    return 'Filter rows', C_ROW, f'Keeps only rows where: {_pretty(g(a, 1))}'


def h_row_errors(a):
    c = _cols(g(a, 1)) if len(a) > 1 else []
    return 'Remove error rows', C_CLEAN, 'Removes rows that contain errors' + (f' in: {_fmt(c)}' if c else '') + '.'


def h_distinct(a):
    if len(a) > 1:
        return 'Remove duplicates', C_CLEAN, f'Removes duplicate rows based on: {_fmt(_cols(a[1]))}'
    return 'Remove duplicates', C_CLEAN, 'Removes duplicate rows (comparing all columns).'


def h_sort(a):
    out = []
    for it in _items(g(a, 1)):
        if it.startswith('{'):
            p = _split(_inner(it))
            out.append(f"{_unq(p[0])} ({'descending' if len(p) > 1 and 'Descending' in p[1] else 'ascending'})")
        else:
            out.append(f'{_unq(it)} (ascending)')
    return 'Sort rows', C_SORT, f'Sorts rows by: {_fmt(out)}'


def _n(s):
    return s.strip() if re.fullmatch(r'\d+', s.strip()) else f'<{_short(s, 30)}>'


def h_firstn(a):
    return 'Keep top/bottom rows', C_ROW, f'Keeps only the first {_n(g(a, 1))} rows.'


def h_lastn(a):
    return 'Keep top/bottom rows', C_ROW, f'Keeps only the last {_n(g(a, 1))} rows.'


def h_skip(a):
    return 'Remove top/bottom rows', C_ROW, f'Removes the first {_n(g(a, 1, "1"))} row(s).'


def h_removelast(a):
    return 'Remove top/bottom rows', C_ROW, f'Removes the last {_n(g(a, 1, "1"))} row(s).'


def h_range(a):
    return 'Keep top/bottom rows', C_ROW, f'Keeps {_n(g(a, 2))} rows starting after the first {_n(g(a, 1))} row(s).'


def h_removerows(a):
    return 'Remove top/bottom rows', C_ROW, f'Removes {_n(g(a, 2, "1"))} row(s) starting after the first {_n(g(a, 1))}.'


def h_promote(a):
    return 'Promote headers', C_CLEAN, 'Uses the first row of data as the column headers.'


def h_demote(a):
    return 'Demote headers', C_CLEAN, 'Turns the column headers back into the first row of data.'


def h_addcol(a):
    name = _unq(g(a, 1))
    expr = g(a, 2)
    hints = _expr_hint(expr)
    typ = f' [{_type_label(a[3])}]' if len(a) > 3 else ''
    return 'Add custom column', C_NEW, (f'Creates new column "{name}"{typ} = {_pretty(expr)}'
                                         + (f'  (uses {", ".join(hints)})' if hints else ''))


def h_index(a):
    return 'Add index column', C_NEW, (f'Adds a row-number column "{_unq(g(a, 1, "Index"))}" '
                                       f'starting at {g(a, 2, "0")}, step {g(a, 3, "1")}.')


def h_dup(a):
    return 'Duplicate column', C_NEW, f'Copies column "{_unq(g(a, 1))}" into new column "{_unq(g(a, 2))}".'


_SPLIT = {'SplitTextByDelimiter': 'by delimiter', 'SplitTextByEachDelimiter': 'by each delimiter in turn',
          'SplitTextByAnyDelimiter': 'by any of several delimiters', 'SplitTextByPositions': 'at fixed positions',
          'SplitTextByLengths': 'by lengths', 'SplitTextByRepeatedLengths': 'every N characters',
          'SplitTextByCharacterTransition': 'where characters change type (letters ↔ digits)'}


def h_split(a):
    sp = g(a, 2)
    m = re.search(r'Splitter\.(\w+)', sp)
    how = _SPLIT.get(m.group(1), m.group(1) if m else 'with a splitter')
    delim = _STR.findall(sp)
    d = f' (delimiter {delim[0]})' if delim else ''
    new = f' into: {_fmt(_cols(a[3]))}' if len(a) > 3 else ''
    return 'Split column', C_NEW, f'Splits column "{_unq(g(a, 1))}" {how}{d}{new}.'


def h_combine_cols(a):
    delim = _STR.findall(g(a, 2))
    return 'Combine columns', C_NEW, (f'Joins columns {_fmt(_cols(g(a, 1)))} into one column "{_unq(g(a, 3))}"'
                                      + (f' using delimiter {delim[0]}' if delim else '') + '.')


def h_replace(a):
    def v(x):
        x = x.strip()
        return 'empty (null)' if x == 'null' else ('empty text' if x == '""' else _pretty(x))
    kind = 'text occurrences' if 'ReplaceText' in g(a, 3) else 'whole cell values'
    c = _cols(g(a, 4)) if len(a) > 4 else []
    return 'Replace values', C_CLEAN, f'Replaces {v(g(a, 1))} with {v(g(a, 2))} ({kind})' + (f' in: {_fmt(c)}' if c else '') + '.'


def h_fill(direction):
    def h(a):
        return 'Fill down/up', C_CLEAN, (f'Fills empty cells {direction} with the nearest non-empty value in: {_fmt(_cols(g(a, 1)))}')
    return h


_FN = {'Text.Trim': 'trims spaces', 'Text.Clean': 'removes non-printable characters', 'Text.Upper': 'converts to UPPERCASE',
       'Text.Lower': 'converts to lowercase', 'Text.Proper': 'Capitalizes Each Word', 'Text.From': 'converts to text',
       'Number.From': 'converts to number', 'Date.From': 'converts to date', 'Text.Replace': 'replaces text',
       'Number.Round': 'rounds the number', 'Text.PadStart': 'pads with leading characters', 'Text.Start': 'takes left characters'}


def h_transform(a):
    out = []
    for it in _items(g(a, 1)):
        p = _split(_inner(it))
        if len(p) >= 2:
            f = p[1].strip()
            out.append(f'{_unq(p[0])} ({_FN.get(f, "custom logic: " + _pretty(f, 80))})')
    return 'Transform column values', C_CLEAN, f'Changes values in place: {_fmt(out)}'


def h_colnames(a):
    return 'Rename columns', C_COL, f'Renames all columns using a rule: {_pretty(g(a, 1), 80)}'


_JOIN = {'Inner': 'Inner join (only rows that match in both)',
         'LeftOuter': 'Left outer join (all rows from this table + matches)',
         'RightOuter': 'Right outer join (all rows from the other table + matches)',
         'FullOuter': 'Full outer join (all rows from both)',
         'LeftAnti': 'Left anti join (rows from this table with NO match)',
         'RightAnti': 'Right anti join (rows from the other table with NO match)'}


def _join_kind(s):
    m = re.search(r'JoinKind\.(\w+)', s or '')
    return _JOIN.get(m.group(1), m.group(1)) if m else _JOIN['LeftOuter']


def _tname(s):
    s = s.strip()
    return _unq(s) if re.fullmatch(r'#?"(?:[^"]|"")*"|[\w.]+', s) else _short(s, 50)


def h_nested_join(fuzzy=False):
    def h(a):
        k1, k2 = _cols(g(a, 1)), _cols(g(a, 3))
        on = ', '.join(f'{x} = {y}' for x, y in zip(k1, k2))
        return ('Merge tables (join)', C_COMB,
                f'Merges{" (fuzzy match)" if fuzzy else ""} with query "{_tname(g(a, 2))}": {_join_kind(g(a, 5))} on {on}. '
                f'Matching rows are stored in column "{_unq(g(a, 4))}" (usually expanded in the next step).')
    return h


def h_join(a):
    on = ', '.join(f'{x} = {y}' for x, y in zip(_cols(g(a, 1)), _cols(g(a, 3))))
    return 'Merge tables (join)', C_COMB, f'Joins with query "{_tname(g(a, 2))}": {_join_kind(g(a, 4))} on {on}.'


def h_expand_table(a):
    return 'Expand merged columns', C_COMB, (f'Brings columns {_fmt(_cols(g(a, 2)))} from the merged table in column '
                                              f'"{_unq(g(a, 1))}" into this table.')


def h_expand_record(a):
    return 'Expand merged columns', C_COMB, f'Expands record column "{_unq(g(a, 1))}" into: {_fmt(_cols(g(a, 2)))}'


def h_expand_list(a):
    return 'Expand list to rows', C_SHAPE, f'Expands list column "{_unq(g(a, 1))}" into rows (other values repeat for each list item).'


def h_combine(a):
    names = [_tname(x) for x in _items(g(a, 0))]
    return 'Append tables (union)', C_COMB, f'Stacks these queries on top of each other (UNION): {_fmt(names)}'


_AGG = {'Sum': 'Sum', 'Average': 'Average', 'Max': 'Max', 'Min': 'Min', 'Median': 'Median',
        'StandardDeviation': 'Std. deviation'}


def _agg(expr):
    e = re.sub(r'^each\s+', '', re.sub(r'\s+', ' ', expr.strip()))
    m = re.fullmatch(r'List\.(Sum|Average|Max|Min|Median|StandardDeviation)\(\s*_?\[([^\]]+)\]\s*\)', e)
    if m: return f'{_AGG[m.group(1)]} of {m.group(2)}'
    if re.fullmatch(r'Table\.RowCount\(\s*_\s*\)', e): return 'Count of rows'
    m = re.fullmatch(r'List\.(?:Non)?[Nn]?[Cc]ount\(\s*_?\[([^\]]+)\]\s*\)', e)
    if m: return f'Count of {m.group(1)}'
    if 'List.Distinct' in e and 'Count' in e:
        m = re.search(r'\[([^\]]+)\]', e)
        return f'Distinct count of {m.group(1) if m else "values"}'
    if e == '_': return 'all rows of the group (nested table)'
    return _pretty(e, 90)


def h_group(a):
    keys = _cols(g(a, 1))
    aggs = []
    for it in _items(g(a, 2)):
        p = _split(_inner(it))
        if len(p) >= 2:
            aggs.append(f'{_unq(p[0])} = {_agg(p[1])}')
    by = f'Groups rows by {_fmt(keys)}' if keys else 'Collapses the whole table into one row'
    return 'Group by', C_SHAPE, f'{by} and calculates: {_fmt(aggs)}'


def h_unpivot_other(a):
    return 'Unpivot', C_SHAPE, (f'Turns all columns EXCEPT {_fmt(_cols(g(a, 1)))} into rows. New columns: '
                                f'"{_unq(g(a, 2))}" (old column names) and "{_unq(g(a, 3))}" (their values).')


def h_unpivot(a):
    return 'Unpivot', C_SHAPE, (f'Turns columns {_fmt(_cols(g(a, 1)))} into rows. New columns: '
                                f'"{_unq(g(a, 2))}" (old column names) and "{_unq(g(a, 3))}" (their values).')


def h_pivot(a):
    agg = g(a, 4).replace('List.', '') or 'aggregate'
    return 'Pivot', C_SHAPE, (f'Turns the values of "{_unq(g(a, 2))}" into column headers, filled with '
                              f'{_unq(g(a, 3))} ({agg}).')


def h_transpose(a):
    return 'Transpose', C_SHAPE, 'Flips the table: rows become columns and columns become rows.'


def h_buffer(a):
    return 'Buffer table', C_OTH, 'Loads the whole table into memory before continuing (Table.Buffer).'


def h_replace_err(a):
    return 'Replace errors', C_CLEAN, f'Replaces error values with a fixed value: {_short(g(a, 1), 80)}'


def h_manual(a):
    return 'Manual data', C_SRC, 'Table is typed in by hand / hard-coded inside the query.'


def h_fromlist(a):
    return 'Convert list to table', C_SHAPE, 'Converts a list (e.g. API/JSON result) into a table.'


def h_native(a):
    q = _unq(g(a, 1))
    return 'Custom SQL query', C_SRC, f'Runs a hand-written query on the source: {_short(q, 200)}'


HANDLERS = {
    'Table.TransformColumnTypes': h_types, 'Table.RenameColumns': h_rename, 'Table.RemoveColumns': h_remove_cols,
    'Table.SelectColumns': h_select_cols, 'Table.ReorderColumns': h_reorder, 'Table.SelectRows': h_filter,
    'Table.RemoveRowsWithErrors': h_row_errors, 'Table.Distinct': h_distinct, 'Table.Sort': h_sort,
    'Table.FirstN': h_firstn, 'Table.LastN': h_lastn, 'Table.Skip': h_skip, 'Table.RemoveFirstN': h_skip,
    'Table.RemoveLastN': h_removelast, 'Table.Range': h_range, 'Table.RemoveRows': h_removerows,
    'Table.PromoteHeaders': h_promote, 'Table.DemoteHeaders': h_demote, 'Table.AddColumn': h_addcol,
    'Table.AddIndexColumn': h_index, 'Table.DuplicateColumn': h_dup, 'Table.SplitColumn': h_split,
    'Table.CombineColumns': h_combine_cols, 'Table.ReplaceValue': h_replace, 'Table.FillDown': h_fill('downward'),
    'Table.FillUp': h_fill('upward'), 'Table.TransformColumns': h_transform, 'Table.TransformColumnNames': h_colnames,
    'Table.NestedJoin': h_nested_join(), 'Table.FuzzyNestedJoin': h_nested_join(True), 'Table.Join': h_join,
    'Table.ExpandTableColumn': h_expand_table, 'Table.ExpandRecordColumn': h_expand_record,
    'Table.ExpandListColumn': h_expand_list, 'Table.Combine': h_combine, 'Table.Group': h_group,
    'Table.UnpivotOtherColumns': h_unpivot_other, 'Table.Unpivot': h_unpivot, 'Table.Pivot': h_pivot,
    'Table.Transpose': h_transpose, 'Table.Buffer': h_buffer, 'Table.ReplaceErrorValues': h_replace_err,
    'Table.FromRows': h_manual, 'Table.FromColumns': h_manual, 'Table.FromRecords': h_manual,
    'Table.FromList': h_fromlist, 'Value.NativeQuery': h_native,
}

# ======================================================== gold-layer guidance
UP = 'Move upstream (Silver/Gold)'
OK = 'Fine in semantic model'
NO = 'Not needed'
REV = 'Review'
SRCW = 'Source'
UP_WHY = {
    'Custom SQL query': 'Put this SQL in a view / Gold table instead of hiding it inside Power Query.',
    'Change data types': 'Define correct types in the Gold table schema so Power Query does not have to.',
    'Promote headers': 'Fix headers at ingestion (Bronze/Silver).',
    'Remove columns': 'Select only the needed columns in the Gold view: fewer columns = smaller, faster model.',
    'Keep columns': 'Select only the needed columns in the Gold view: fewer columns = smaller, faster model.',
    'Filter rows': 'Filter in SQL/Spark so less data is loaded and refresh is faster.',
    'Keep top/bottom rows': 'Handle at ingestion; check this is not a leftover test limit.',
    'Remove top/bottom rows': 'Handle at ingestion (skip junk header/footer rows in Bronze/Silver).',
    'Remove duplicates': 'De-duplicate in Silver (ROW_NUMBER / DISTINCT).',
    'Replace values': 'Clean data in Silver so every report gets the same clean values.',
    'Replace errors': 'Fix bad data at the source / Silver layer.',
    'Remove error rows': 'Fix bad data at the source / Silver layer.',
    'Fill down/up': 'Use a window function (LAST_VALUE … IGNORE NULLS) in SQL/Spark.',
    'Transform column values': 'Standardise text/number clean-up in Silver.',
    'Add custom column': 'Compute row-level logic in the Gold table (SQL/Spark) so it is reusable and refresh is faster.',
    'Add index column': 'Create a surrogate key in the Gold table (ROW_NUMBER / identity).',
    'Duplicate column': 'Create it in the Gold table.',
    'Split column': 'Split in SQL/Spark (SPLIT / SUBSTRING).',
    'Combine columns': 'Concatenate in SQL/Spark.',
    'Merge tables (join)': 'Do joins in SQL/Spark. Joins in Power Query are slow and often break query folding.',
    'Expand merged columns': 'Part of a merge: do the join in SQL/Spark.',
    'Append tables (union)': 'UNION the tables in Silver/Gold.',
    'Group by': 'Pre-aggregate in Gold, or replace with DAX measures if the detail rows are needed.',
    'Pivot': 'Pivot in SQL/Spark.', 'Unpivot': 'Unpivot in SQL/Spark (UNPIVOT / stack) at Silver level.',
    'Transpose': 'Reshape in SQL/Spark.', 'Convert list to table': 'Land API/JSON data in Bronze/Silver instead of parsing it inside Power BI.', 'Expand list to rows': 'Explode in SQL/Spark.',
    'Manual data': 'Create a proper dimension / seed table in the lakehouse or warehouse.',
    'Demote headers': 'Fix at ingestion.',
}
GUIDE = {op: (UP, why) for op, why in UP_WHY.items()}
GUIDE.update({
    'Connect to source': (SRCW, 'The Gold layer should be the source here (a curated table/view).'),
    'Navigate to object': (SRCW, 'Picks the table/sheet to load.'),
    'Rename columns': (OK, 'Business-friendly names can live in the Gold view or in the semantic model.'),
    'Reorder columns': (NO, 'Column order has no effect on the model. Safe to delete.'),
    'Sort rows': (NO, 'Sort order is not kept in the model; visuals sort themselves. Safe to delete (unless a later step depends on it).'),
    'Buffer table': (REV, 'Forces the whole table into memory; often unnecessary and can slow refresh.'),
})
FOLD = {'Add index column': 'Likely stops folding', 'Buffer table': 'Likely stops folding',
        'Transpose': 'Likely stops folding', 'Pivot': 'Likely stops folding', 'Fill down/up': 'Likely stops folding',
        'Add custom column': 'May stop folding', 'Merge tables (join)': 'May stop folding',
        'Split column': 'May stop folding', 'Transform column values': 'May stop folding',
        'Replace values': 'May stop folding', 'Expand list to rows': 'Likely stops folding'}
_HEAVY = {'Merge tables (join)', 'Group by', 'Pivot', 'Unpivot', 'Append tables (union)', 'Add custom column',
          'Custom SQL query', 'Transpose', 'Remove duplicates', 'Filter rows', 'Replace values', 'Fill down/up'}


# ======================================================== describe one step
def _describe(expr, seen):
    e = expr.strip()
    if e.startswith('#table('):
        op, cat, desc = h_manual([])
        return dict(op=op, cat=cat, desc=desc)
    fn, args = _call(e)
    if fn:
        if fn in SRC:
            if 'Binary.FromText' in e:
                op, cat, desc = h_manual([])
                return dict(op=op, cat=cat, desc=desc)
            s = _source(fn, args, e)
            desc = f"Connects to {s['stype']}" + (f": {s['detail']}" if s['detail'] else '') + '.'
            if s['sql']:
                desc += f" Uses a hand-written SQL query: {_short(s['sql'], 200)}"
            return dict(op='Custom SQL query' if s['sql'] else 'Connect to source', cat=C_SRC, desc=desc, source=s)
        if fn in HANDLERS:
            op, cat, desc = HANDLERS[fn](args)
            d = dict(op=op, cat=cat, desc=desc)
            if fn == 'Value.NativeQuery':
                d['source_sql'] = _unq(g(args, 1))
            return d
        short = fn.split('.', 1)[-1]
        return dict(op=f'Other: {short}', cat=C_OTH, desc=f'Applies {fn} (not specifically recognised, review in Power Query).')
    nav = _nav(e)
    if nav:
        return dict(op='Navigate to object', cat=C_SRC, desc=f'Picks {nav} from the source.', nav=nav)
    if re.fullmatch(r'#"(?:[^"]|"")*"|[A-Za-z_][\w.]*', e):
        name = _unq(e)
        if name in seen:
            return dict(op='No change', cat=C_OTH, desc=f'Re-uses the result of step "{name}" (no change).')
        if not seen:
            return dict(op='Connect to source', cat=C_SRC, desc=f'Starts from another query: "{name}".',
                        source=dict(stype='Another query', detail=name, is_db=False, sql=None))
        return dict(op='Reference another query', cat=C_COMB, desc=f'Reads from another query: "{name}".')
    return dict(op='Custom expression', cat=C_OTH, desc=f'Custom M expression: {_short(e, 150)}')


# ======================================================== explain a query / whole model
def explain_query(table, code):
    parsed = _parse_let(code)
    steps = parsed if parsed is not None else [('Expression', (code or '').strip())]
    rows, seen = [], set()
    src = dict(stype='Unknown', detail='', is_db=False, sql=None)
    src_found, obj = False, None
    for i, (name, expr) in enumerate(steps, 1):
        d = _describe(expr, seen)
        if d.get('source') and not src_found:
            src, src_found = d['source'], True
        if d.get('nav') and obj is None:
            obj = d['nav']
        if d.get('source_sql') and not src['sql']:
            src['sql'] = d['source_sql']
        where, why = GUIDE.get(d['op'], (REV, 'Not specifically recognised, review manually.'))
        rows.append({'Table': table, '#': i, 'Step name': name, 'Operation': d['op'], 'Category': d['cat'],
                     'What it does (plain English)': d['desc'], 'Gold layer: where it belongs': where,
                     'Why / what to do': why, 'Query folding': FOLD.get(d['op'], ''), 'M code': expr})
        seen.add(name)

    if not src['is_db']:
        for r in rows:
            r['Query folding'] = ''
    df = pd.DataFrame(rows)

    core = df[df['Category'] != C_SRC]
    where_txt = src['stype'] + (f" ({src['detail']})" if src['detail'] else '') + (f', {obj}' if obj else '')
    text = f'Loads data from {where_txt}.'
    if src['sql']:
        text += ' The source uses a hand-written SQL query.'
    if len(core):
        cnt = Counter(core['Operation'])
        text += f' Then {len(core)} transformation step(s): ' + ', '.join(f'{op} ×{n}' for op, n in cnt.most_common()) + '.'
        up = core[core['Gold layer: where it belongs'] == UP]
        no = core[core['Gold layer: where it belongs'] == NO]
        if len(up):
            text += f' {len(up)} of them can be moved upstream into the Silver/Gold layer.'
        heavy = sorted({o for o in up['Operation'] if o in _HEAVY})
        if heavy:
            text += f' Heaviest: {", ".join(heavy)}.'
        if len(no):
            text += f' {len(no)} step(s) (sorting/reordering) have no real effect and can be deleted.'
        brk = core[core['Query folding'] == 'Likely stops folding']
        if len(brk):
            r = brk.iloc[0]
            text += (f' Query folding likely stops at step {r["#"]} ("{r["Step name"]}"): from there the work runs inside '
                     f'Power BI instead of the source database, which slows refresh.')
    else:
        text += ' No further transformations.'
    source_row = {'Table': table, 'Source type': src['stype'], 'Source detail': src['detail'],
                  'Object': obj or '', 'Custom SQL': src['sql'] or ''}
    return df, source_row, text


def explain_all(power_query_df):
    all_steps, sources, summaries = [], [], []
    if power_query_df is None or power_query_df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    for _, r in power_query_df.iterrows():
        t, code = r['TableName'], r['Expression']
        try:
            df, srow, text = explain_query(t, code)
        except Exception as ex:  # never crash the app on odd M
            df = pd.DataFrame([{'Table': t, '#': 1, 'Step name': 'n/a', 'Operation': 'Could not parse',
                                'Category': C_OTH, 'What it does (plain English)': f'Parser could not read this query ({ex}).',
                                'Gold layer: where it belongs': REV, 'Why / what to do': 'Review the raw M code.',
                                'Query folding': '', 'M code': code}])
            srow = {'Table': t, 'Source type': 'Unknown', 'Source detail': '', 'Object': '', 'Custom SQL': ''}
            text = 'Parser could not read this query. Check the raw M code.'
        all_steps.append(df); sources.append(srow); summaries.append({'Table': t, 'Summary': text})
    return pd.concat(all_steps, ignore_index=True), pd.DataFrame(sources), pd.DataFrame(summaries)
