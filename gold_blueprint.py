"""Fabric Gold-layer blueprint: table plan, draft PySpark (from M steps) and DAX -> Gold migration plan. Rule-based, zero cost."""
import json
import re
import pandas as pd

import m_explainer as M
import dax_explainer as D


# ============================================================ helpers
def snake(s):
    s = re.sub(r'[^0-9A-Za-z]+', '_', str(s)).strip('_')
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', s)
    return re.sub(r'_+', '_', s).lower() or 'col'


def py(s):
    return json.dumps(str(s), ensure_ascii=False)


def _bt(name):
    return '`' + str(name).replace('`', '``') + '`'


def _sqlstr(s):
    return "'" + s.replace("'", "''") + "'"


def _todo(e, ctx, kind='M'):
    if ctx is not None:
        ctx.setdefault('todos', []).append(e)
    return f'TODO_TRANSLATE({_short(e)})'



def _brackets(t):
    """T-SQL [name] -> Spark `name`."""
    return re.sub(r'\[([^\]]+)\]', lambda m: '`' + m.group(1) + '`', t)


def _short(e, n=60):
    return re.sub(r'\s+', ' ', e)[:n].replace('(', '[').replace(')', ']').replace("'", '').replace('"', '')


_SPARK_T = {'Text': 'string', 'Decimal number': 'double', 'Whole number': 'bigint', 'Date': 'date', 'Date/Time': 'timestamp',
            'Date/Time/Timezone': 'timestamp', 'True/False': 'boolean', 'Fixed decimal (currency)': 'decimal(19,4)',
            'Percentage': 'double', 'Binary': 'binary'}


def _depth_at(m, pos):
    seg = m[:pos]
    return seg.count('(') + seg.count('[') + seg.count('{') - seg.count(')') - seg.count(']') - seg.count('}')


def _split_kw(e, m, kw):
    parts, last = [], 0
    for mt in re.finditer(rf'\b{kw}\b', m, re.I):
        if _depth_at(m, mt.start()) == 0:
            parts.append(e[last:mt.start()]); last = mt.end()
    parts.append(e[last:])
    return parts if len(parts) > 1 else None


def _paren_end(m, start):
    return D._match_paren(m, start)


# ============================================================ M expression -> Spark SQL expression
_MF = {
    'Text.Upper': 'upper({0})', 'Text.Lower': 'lower({0})', 'Text.Trim': 'trim({0})', 'Text.Length': 'length({0})',
    'Text.Start': 'left({0}, {1})', 'Text.End': 'right({0}, {1})', 'Text.Replace': 'replace({0}, {1}, {2})',
    'Text.Proper': 'initcap({0})', 'Text.From': 'cast({0} AS string)', 'Number.From': 'cast({0} AS double)',
    'Number.Round': 'round({0}, {1})', 'Number.Abs': 'abs({0})', 'Number.Power': 'power({0}, {1})',
    'Date.Year': 'year({0})', 'Date.Month': 'month({0})', 'Date.Day': 'day({0})', 'Date.From': 'to_date({0})',
    'Date.EndOfMonth': 'last_day({0})', 'Date.AddDays': 'date_add({0}, {1})', 'DateTime.LocalNow': 'current_timestamp()',
    'Text.Contains': 'instr({0}, {1}) > 0', 'Text.StartsWith': "{0} LIKE concat({1}, '%')",
    'Text.EndsWith': "{0} LIKE concat('%', {1})", 'Text.PadStart': 'lpad({0}, {1}, {2})',
}


def m_sql(expr, ctx=None, depth=0):
    e = re.sub(r'\s+', ' ', (expr or '').strip())
    e = re.sub(r'^each\s+', '', e)
    e = re.sub(r'^\(\w+\)\s*=>\s*', '', e)
    if not e:
        return 'NULL'
    if depth > 30:
        return _todo(e, ctx)
    m = D._mask(e)
    R = lambda x: m_sql(x, ctx, depth + 1)
    if e.startswith('(') and _paren_end(m, 0) == len(e) - 1:
        return '(' + R(e[1:-1]) + ')'
    if re.match(r'if\b', m):
        dp = nest = 0; tp = ep = None
        for mt in re.finditer(r'\b(?:if|then|else)\b|[()\[\]{}]', m):
            t = mt.group(0)
            if t in '([{': dp += 1; continue
            if t in ')]}': dp -= 1; continue
            if dp: continue
            if t == 'if': nest += 1
            elif t == 'then' and nest == 1 and tp is None: tp = mt
            elif t == 'else':
                nest -= 1
                if nest == 0: ep = mt; break
        if tp and ep:
            return f'CASE WHEN {R(e[2:tp.start()])} THEN {R(e[tp.end():ep.start()])} ELSE {R(e[ep.end():])} END'
    for kw in ('or', 'and'):
        parts = _split_kw(e, m, kw)
        if parts:
            return f' {kw.upper()} '.join(R(p) for p in parts)
    mn = re.match(r'not\s+(.*)$', e, re.S)
    if mn:
        return f'NOT ({R(mn.group(1))})'
    op = D._find_op(m)
    if op:
        i, o = op
        l, r = R(e[:i]), R(e[i + len(o):])
        if o in ('=', '<>'):
            neg = 'NOT ' if o == '<>' else ''
            if r == 'NULL':
                return f'{l} IS {neg}NULL'
            if l == 'NULL':
                return f'{r} IS {neg}NULL'
            return f'{l} <=> {r}' if o == '=' else f'NOT ({l} <=> {r})'
        o = {'&': '||', '^': '**'}.get(o, o)
        return f'POWER({l}, {r})' if o == '**' else f'{l} {o} {r}'
    mc = re.match(r'([A-Za-z_][\w.]*)\s*\(', e)
    if mc and _paren_end(m, mc.end() - 1) == len(e) - 1:
        fn, args = mc.group(1), D._split_args(e[mc.end():-1])
        if fn == 'List.Contains' and len(args) >= 2 and args[0].strip().startswith('{'):
            return f'{R(args[1])} IN (' + ', '.join(R(x) for x in D._split_args(args[0].strip()[1:-1])) + ')'
        if fn in _MF:
            try:
                return _MF[fn].format(*[R(x) for x in args])
            except IndexError:
                pass
        return _todo(e, ctx)
    if re.fullmatch(r'_?\[[^\]]+\]', m):
        return _bt(e[e.index('[') + 1:-1])
    if re.fullmatch(r'"(?:[^"]|"")*"', e):
        return _sqlstr(e[1:-1].replace('""', '"'))
    if re.fullmatch(r'-?\d+(\.\d+)?', e):
        return e
    if e == 'null':
        return 'NULL'
    if e in ('true', 'false'):
        return e.upper()
    if e.startswith('{') and e.endswith('}'):
        return '(' + ', '.join(R(x) for x in D._split_args(e[1:-1])) + ')'
    if re.fullmatch(r'#"(?:[^"]|"")*"', e):
        return _bt(M._unq(e))
    return _todo(e, ctx)


# ============================================================ DAX expression -> Spark SQL expression (row level)
def dax_sql(expr, ctx, home, depth=0):
    e = re.sub(r'\s+', ' ', (expr or '').strip())
    if not e:
        return 'NULL'
    if depth > 30:
        return _todo(e, ctx, 'DAX')
    m = D._mask(e)
    R = lambda x: dax_sql(x, ctx, home, depth + 1)
    if re.match(r'VAR\b', m, re.I):
        return _todo('VAR/RETURN formula', ctx, 'DAX')
    if e.startswith('(') and _paren_end(m, 0) == len(e) - 1:
        return '(' + R(e[1:-1]) + ')'
    mn = re.match(r'NOT\s+(.*)$', e, re.I | re.S)
    if mn and not re.match(r'NOT\s*\(', e, re.I):
        return f'NOT ({R(mn.group(1))})'
    mi = re.search(r'\s+IN\s+', m, re.I)
    if mi and D._find_op(m) is None:
        lst = e[mi.end():].strip()
        return f'{R(e[:mi.start()])} IN (' + ', '.join(R(x) for x in D._split_args(lst[1:-1])) + ')' if lst.startswith('{') else _todo(e, ctx, 'DAX')
    op = D._find_op(m)
    if op:
        i, o = op
        o2 = {'&&': 'AND', '||': 'OR', '&': '||'}.get(o, o)
        l, r = R(e[:i]), R(e[i + len(o):])
        if o in ('=', '<>') and 'NULL' in (l, r):
            return f'{r if l == "NULL" else l} IS {"NOT " if o == "<>" else ""}NULL'
        return f'{l} {o2} {r}'
    mc = re.match(r'([A-Za-z_][\w.]*)\s*\(', e)
    if mc and _paren_end(m, mc.end() - 1) == len(e) - 1:
        fn, a = mc.group(1).upper(), D._split_args(e[mc.end():-1])
        g = lambda i, d='NULL': R(a[i]) if len(a) > i and a[i] != '' else d
        if fn == 'IF':
            return f'CASE WHEN {g(0)} THEN {g(1)} ELSE {g(2)} END'
        if fn == 'SWITCH':
            first = a[0].strip().upper() if a else ''
            parts, i = [], 1
            while i + 1 < len(a):
                parts.append(f'WHEN {R(a[i])} THEN {R(a[i + 1])}' if first == 'TRUE()' else f'WHEN {g(0)} = {R(a[i])} THEN {R(a[i + 1])}')
                i += 2
            return 'CASE ' + ' '.join(parts) + (f' ELSE {R(a[i])}' if i < len(a) else '') + ' END'
        if fn == 'DIVIDE':
            return f'CASE WHEN ({g(1)}) = 0 OR ({g(1)}) IS NULL THEN {g(2)} ELSE ({g(0)}) / ({g(1)}) END'
        if fn == 'RELATED' and a:
            mr = D._REF.fullmatch(a[0].strip())
            if mr and ctx is not None:
                tbl, col = (mr.group(1) or mr.group(2)), mr.group(3)
                ctx.setdefault('related', {})[(tbl, col)] = f'{snake(tbl)}_{snake(col)}'
                return _bt(f'{snake(tbl)}_{snake(col)}')
            return _todo(e, ctx, 'DAX')
        simple = {'UPPER': 'upper({0})', 'LOWER': 'lower({0})', 'TRIM': 'trim({0})', 'LEN': 'length({0})', 'LEFT': 'left({0}, {1})',
                  'RIGHT': 'right({0}, {1})', 'MID': 'substring({0}, {1}, {2})', 'YEAR': 'year({0})', 'MONTH': 'month({0})',
                  'DAY': 'day({0})', 'DATE': 'make_date({0}, {1}, {2})', 'ROUND': 'round({0}, {1})', 'ABS': 'abs({0})',
                  'INT': 'floor({0})', 'VALUE': 'cast({0} AS double)', 'ISBLANK': '({0}) IS NULL', 'COALESCE': 'coalesce({all})',
                  'TODAY': 'current_date()', 'NOW': 'current_timestamp()', 'CONCATENATE': 'concat({0}, {1})',
                  'EOMONTH': 'last_day(add_months({0}, {1}))', 'FORMAT': 'date_format({0}, {1})', 'BLANK': 'NULL',
                  'TRUE': 'TRUE', 'FALSE': 'FALSE', 'AND': '({0}) AND ({1})', 'OR': '({0}) OR ({1})', 'NOT': 'NOT ({0})'}
        if fn in simple:
            try:
                return simple[fn].format(*[R(x) for x in a], all=', '.join(R(x) for x in a))
            except IndexError:
                pass
        return _todo(e, ctx, 'DAX')
    mr = D._REF.fullmatch(e)
    if mr:
        tbl, col = (mr.group(1) or mr.group(2)), mr.group(3).replace(']]', ']')
        if home and tbl != home and ctx is not None:
            ctx.setdefault('foreign', set()).add((tbl, col))
        return _bt(col)
    mm = re.fullmatch(r'\[((?:[^\]]|\]\])+)\]', e)
    if mm:
        return _bt(mm.group(1))
    if re.fullmatch(r'"(?:[^"]|"")*"', e):
        return _sqlstr(e[1:-1].replace('""', '"'))
    if re.fullmatch(r'-?\d+(\.\d+)?', e):
        return e
    return _todo(e, ctx, 'DAX')


# ============================================================ PySpark code for one Power Query table
_HOW = {'Inner': 'inner', 'LeftOuter': 'left', 'RightOuter': 'right', 'FullOuter': 'full', 'LeftAnti': 'left_anti'}


def _lit(x):
    """Python literal from an M literal, or None if not a simple literal."""
    x = x.strip()
    if x == 'null':
        return 'None'
    if re.fullmatch(r'-?\d+(\.\d+)?', x):
        return x
    if re.fullmatch(r'"(?:[^"]|"")*"', x):
        return py(M._unq(x))
    return None


def _agg_spark(expr):
    e = re.sub(r'^each\s+', '', re.sub(r'\s+', ' ', expr.strip()))
    mm = re.fullmatch(r'List\.(Sum|Average|Max|Min)\(\s*_?\[([^\]]+)\]\s*\)', e)
    if mm:
        return f'F.{ {"Sum": "sum", "Average": "avg", "Max": "max", "Min": "min"}[mm.group(1)] }(F.col({py(mm.group(2))}))'
    if re.fullmatch(r'Table\.RowCount\(\s*_\s*\)', e):
        return 'F.count(F.lit(1))'
    mm = re.fullmatch(r'List\.Count\(\s*List\.Distinct\(\s*_?\[([^\]]+)\]\s*\)\s*\)', e)
    if mm:
        return f'F.countDistinct(F.col({py(mm.group(1))}))'
    mm = re.fullmatch(r'List\.(?:Non[Nn]ull)?Count\(\s*_?\[([^\]]+)\]\s*\)', e)
    if mm:
        return f'F.count(F.col({py(mm.group(1))}))'
    return None


_TF = {'Text.Trim': 'F.trim({c})', 'Text.Upper': 'F.upper({c})', 'Text.Lower': 'F.lower({c})', 'Text.Proper': 'F.initcap({c})',
       'Text.Clean': 'F.regexp_replace({c}, "[\\\\x00-\\\\x1F]", "")', 'Text.From': '{c}.cast("string")', 'Number.From': '{c}.cast("double")'}


def gen_table_code(table, steps, src, ctx):
    """steps: list of dicts with 'Step name','M code','Operation'. Returns (code, translated, manual)."""
    out, ok, manual = [], 0, 0
    tv = ctx['var']
    seen = []
    pend = {}
    stype = (src or {}).get('Source type', '')
    sdetail = (src or {}).get('Source detail', '')
    custom_sql = (src or {}).get('Custom SQL', '')
    has_df = False

    def add(*ls):
        out.extend(ls)

    for idx, st in enumerate(steps):
        name, expr, op = st['Step name'], st['M code'].strip(), st['Operation']
        fn, a = M._call(expr)
        add(f'# --- Step {idx + 1}: {name}  [{op}]')
        auto = True
        c = {'todos': []}
        try:
            if op in ('Connect to source', 'Custom SQL query') and not has_df:
                if op == 'Custom SQL query' or custom_sql:
                    add(f'# Source query (T-SQL, runs on the source database): rewrite it as Spark SQL or recreate it as a view:')
                    for ln in (custom_sql or '').splitlines() or ['']:
                        add('#   ' + ln)
                if stype == 'Another query':
                    add(f'df = {tv(sdetail)}'); has_df = True
                else:
                    add(f'# Source: {stype} {sdetail}'.rstrip(), '# Land this source in the Lakehouse Bronze layer first (Data Pipeline copy, Mirroring or Dataflow Gen2).')
                    base = snake(table)
                    if any(k in stype for k in ('SQL', 'Postgre', 'MySQL', 'Oracle', 'Snowflake', 'Redshift', 'HANA', 'Teradata', 'Databricks', 'BigQuery')):
                        add(f'df = spark.read.table("bronze.{base}")'); has_df = True
                        sq = re.sub(r'\s+', ' ', (custom_sql or '').strip())
                        mq = re.match(r'select (.+?) from ([\w\.\[\]"`]+)(?: where (.+?))?(?: order by .*)?;?$', sq, re.I)
                        if mq:
                            add('# Translated from the custom SQL (verify against the original):')
                            if mq.group(3):
                                add('df = df.filter(' + py(_brackets(mq.group(3))) + ')  # WHERE clause first, then the column list')
                            cols_ = [x.strip() for x in mq.group(1).split(',')]
                            if '*' not in mq.group(1) and not any('(' in x for x in cols_):
                                add('df = df.selectExpr(' + ', '.join(py(_brackets(x)) for x in cols_) + ')')
                    elif 'Excel' in stype or 'CSV' in stype:
                        fname = re.split(r'[\\/ ]\(', sdetail)[0]
                        stem = snake(re.sub(r'\.\w+$', '', fname.split(' (')[0]))
                        add(f'df = spark.read.option("header", True).option("inferSchema", True).csv("Files/{stem}.csv")  # save the Excel sheet as CSV/Parquet when landing it'); has_df = True
                    else:
                        add(f'df = spark.read.table("bronze.{base}")  # TODO: confirm landed table name'); has_df = True
            elif op == 'Connect to source':
                add('# (additional source connection: handled above)')
            elif op == 'Navigate to object':
                add('# (the object selected here is the Bronze table/file read above)')
            elif fn == 'Table.PromoteHeaders':
                add('# Headers are handled when reading the file (option header=True).')
            elif fn == 'Table.TransformColumnTypes':
                for it in M._items(M.g(a, 1)):
                    p = M._split(M._inner(it))
                    if len(p) >= 2:
                        t = _SPARK_T.get(M._type_label(p[1]))
                        if t:
                            add(f'df = df.withColumn({py(M._unq(p[0]))}, F.col({py(M._unq(p[0]))}).cast("{t}"))')
            elif fn == 'Table.RenameColumns':
                for it in M._items(M.g(a, 1)):
                    p = M._split(M._inner(it))
                    if len(p) >= 2:
                        add(f'df = df.withColumnRenamed({py(M._unq(p[0]))}, {py(M._unq(p[1]))})')
            elif fn == 'Table.RemoveColumns':
                add(f'df = df.drop({", ".join(py(x) for x in M._cols(M.g(a, 1)))})')
            elif fn == 'Table.SelectColumns':
                add(f'df = df.select({", ".join(py(x) for x in M._cols(M.g(a, 1)))})')
            elif fn == 'Table.ReorderColumns':
                add('# Column order has no effect on the model: skipped.')
            elif fn == 'Table.SelectRows':
                add(f'df = df.filter({py(m_sql(M.g(a, 1), c))})')
            elif fn == 'Table.Distinct':
                add('df = df.dropDuplicates([' + ', '.join(py(x) for x in M._cols(a[1])) + '])' if len(a) > 1 else 'df = df.dropDuplicates()')
            elif fn == 'Table.Sort':
                add('# Sorting is not needed in a Gold Delta table (the model does not keep row order): skipped.')
            elif fn == 'Table.FirstN' and re.fullmatch(r'\d+', M.g(a, 1).strip()):
                add(f'df = df.limit({M.g(a, 1).strip()})')
            elif fn == 'Table.AddColumn':
                nm = M._unq(M.g(a, 1)); t = _SPARK_T.get(M._type_label(a[3])) if len(a) > 3 else None
                sql = m_sql(M.g(a, 2), c)
                add(f'df = df.withColumn({py(nm)}, F.expr({py(sql)})' + (f'.cast("{t}")' if t else '') + ')')
            elif fn == 'Table.AddIndexColumn':
                start = M.g(a, 2, '0').strip()
                add(f'df = df.withColumn({py(M._unq(M.g(a, 1, chr(34) + "Index" + chr(34))))}, F.row_number().over(Window.orderBy(F.monotonically_increasing_id())) - 1 + {start})')
            elif fn == 'Table.DuplicateColumn':
                add(f'df = df.withColumn({py(M._unq(M.g(a, 2)))}, F.col({py(M._unq(M.g(a, 1)))}))')
            elif fn == 'Table.SplitColumn':
                srcc = M._unq(M.g(a, 1)); new = M._cols(a[3]) if len(a) > 3 else []
                delim = M._STR.findall(M.g(a, 2))
                if new and delim and 'SplitTextByDelimiter' in M.g(a, 2):
                    add(f'_parts = F.split(F.col({py(srcc)}), {py(re.escape(M._unq(delim[0])))})')
                    for i, n in enumerate(new):
                        add(f'df = df.withColumn({py(n)}, _parts.getItem({i}))')
                    add(f'df = df.drop({py(srcc)})')
                else:
                    auto = False; add(f'# TODO (manual): split {srcc}: {M._short(M.g(a, 2))}')
            elif fn == 'Table.CombineColumns':
                srcs = M._cols(M.g(a, 1)); delim = M._STR.findall(M.g(a, 2))
                add(f'df = df.withColumn({py(M._unq(M.g(a, 3)))}, F.concat_ws({py(M._unq(delim[0])) if delim else py("")}, ' + ', '.join(f'F.col({py(x)})' for x in srcs) + '))')
                add(f'df = df.drop({", ".join(py(x) for x in srcs)})')
            elif fn == 'Table.ReplaceValue':
                old, new = _lit(M.g(a, 1)), _lit(M.g(a, 2))
                cols = M._cols(a[4]) if len(a) > 4 else []
                if old == 'None' and new is not None and cols:
                    add(f'df = df.fillna({{{", ".join(f"{py(x)}: {new}" for x in cols)}}})')
                elif cols and old is not None and new is not None:
                    for x in cols:
                        if 'ReplaceText' in M.g(a, 3):
                            add(f'df = df.withColumn({py(x)}, F.regexp_replace(F.col({py(x)}), {py(re.escape(json.loads(old)))}, {new}))')
                        else:
                            add(f'df = df.withColumn({py(x)}, F.when(F.col({py(x)}) == {old}, {new}).otherwise(F.col({py(x)})))')
                else:
                    auto = False; add(f'# TODO (manual): replace values: {M._short(expr)}')
            elif fn in ('Table.FillDown', 'Table.FillUp'):
                frame = 'Window.unboundedPreceding, 0' if fn == 'Table.FillDown' else '0, Window.unboundedFollowing'
                add(f'_w = Window.orderBy(F.monotonically_increasing_id()).rowsBetween({frame})  # TODO: order by a real sort column')
                for x in M._cols(M.g(a, 1)):
                    add(f'df = df.withColumn({py(x)}, F.last({py(x)}, ignorenulls=True).over(_w))' if fn == 'Table.FillDown'
                        else f'df = df.withColumn({py(x)}, F.first({py(x)}, ignorenulls=True).over(_w))')
            elif fn == 'Table.TransformColumns':
                for it in M._items(M.g(a, 1)):
                    p = M._split(M._inner(it))
                    if len(p) >= 2 and p[1].strip() in _TF:
                        cx = f'F.col({py(M._unq(p[0]))})'
                        add(f'df = df.withColumn({py(M._unq(p[0]))}, {_TF[p[1].strip()].format(c=cx)})')
                    else:
                        auto = False; add(f'# TODO (manual): transform {M._unq(p[0]) if p else ""}: {M._short(p[1] if len(p) > 1 else "")}')
            elif fn in ('Table.NestedJoin', 'Table.FuzzyNestedJoin'):
                right = M._tname(M.g(a, 2)); newc = M._unq(M.g(a, 4))
                km = re.search(r'JoinKind\.(\w+)', M.g(a, 5)); kind = km.group(1) if km else 'LeftOuter'
                later = any('ExpandTableColumn' in s['M code'] and newc in s['M code'] for s in steps[idx + 1:])
                lk, rk = M._cols(M.g(a, 1)), M._cols(M.g(a, 3))
                if later and kind in _HOW:
                    pend[newc] = (right, lk, rk, kind)
                    add(f'# Join with {right} ({kind}) is applied together with the column expansion in the next step.')
                elif kind in _HOW:
                    sel = ', '.join(f'F.col({py(k)}).alias("__k{i}")' for i, k in enumerate(rk))
                    cond = ' & '.join(f'(F.col({py(l)}) == F.col("__k{i}"))' for i, l in enumerate(lk))
                    add(f'df = df.join({tv(right)}.select({sel}), on={cond}, how="{_HOW[kind]}").drop(' + ', '.join(f'"__k{i}"' for i in range(len(rk))) + ')')
                else:
                    auto = False; add(f'# TODO (manual): join kind {kind} with {right}')
            elif fn == 'Table.ExpandTableColumn':
                nested = M._unq(M.g(a, 1))
                if nested in pend:
                    right, lk, rk, kind = pend.pop(nested)
                    cols = M._cols(M.g(a, 2)); newn = M._cols(a[3]) if len(a) > 3 else cols
                    sel = ', '.join([f'F.col({py(k)}).alias("__k{i}")' for i, k in enumerate(rk)] +
                                    [f'F.col({py(cn)}).alias({py(nn)})' for cn, nn in zip(cols, newn)])
                    cond = ' & '.join(f'(F.col({py(l)}) == F.col("__k{i}"))' for i, l in enumerate(lk))
                    add(f'_r = {tv(right)}.select({sel})',
                        f'df = df.join(_r, on={cond}, how="{_HOW[kind]}").drop(' + ', '.join(f'"__k{i}"' for i in range(len(rk))) + ')')
                else:
                    auto = False; add(f'# TODO (manual): expand {nested}')
            elif fn == 'Table.Combine':
                items = [M._tname(x) for x in M._items(M.g(a, 0))]
                for it in items:
                    if it in [s['Step name'] for s in steps[:idx]]:
                        continue
                    add(f'df = df.unionByName({tv(it)}, allowMissingColumns=True)')
            elif fn == 'Table.Group':
                keys = M._cols(M.g(a, 1)); aggs = []
                for it in M._items(M.g(a, 2)):
                    p = M._split(M._inner(it))
                    sp = _agg_spark(p[1]) if len(p) > 1 else None
                    if sp:
                        aggs.append(f'{sp}.alias({py(M._unq(p[0]))})')
                    else:
                        auto = False; aggs.append(f'F.lit(None).alias({py(M._unq(p[0]))})  # TODO: {M._short(p[1] if len(p) > 1 else "")}')
                add(f'df = df.groupBy({", ".join(py(k) for k in keys)}).agg(' + ', '.join(aggs) + ')' if keys else 'df = df.agg(' + ', '.join(aggs) + ')')
            elif fn == 'Table.UnpivotOtherColumns':
                add(f'df = df.unpivot([{", ".join(py(k) for k in M._cols(M.g(a, 1)))}], None, {py(M._unq(M.g(a, 2)))}, {py(M._unq(M.g(a, 3)))})')
            elif fn == 'Table.Unpivot':
                vals = M._cols(M.g(a, 1))
                add(f'_vals = [{", ".join(py(v) for v in vals)}]',
                    f'df = df.unpivot([c for c in df.columns if c not in _vals], _vals, {py(M._unq(M.g(a, 2)))}, {py(M._unq(M.g(a, 3)))})')
            elif fn == 'Table.Pivot':
                attr, val = M._unq(M.g(a, 2)), M._unq(M.g(a, 3)); agg = M.g(a, 4).replace('List.', '').lower() or 'sum'
                agg = {'average': 'avg'}.get(agg, agg)
                add(f'df = df.groupBy(*[c for c in df.columns if c not in ({py(attr)}, {py(val)})]).pivot({py(attr)}).agg(F.{agg}({py(val)}))')
            elif fn in ('Table.Buffer', 'Table.RemoveRowsWithErrors', 'Table.ReplaceErrorValues'):
                add('# Not needed in Spark (or handle bad rows with try_cast / a quarantine table).')
            else:
                auto = False
                add(f'# TODO (manual): {op}: {M._short(expr, 120)}')
        except Exception as ex:
            auto = False
            add(f'# TODO (manual): could not translate ({ex}): {M._short(expr, 100)}')
        if c['todos']:
            auto = False
        ok += 1 if auto else 0
        manual += 0 if auto else 1
        seen.append(name)
    return '\n'.join(out), ok, manual


# ============================================================ model / table plan
def _norm_rels(df):
    out = []
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        g = lambda *ks: next((r[k] for k in ks if k in r.index and pd.notna(r[k])), None)
        ft, fc, tt, tc = g('FromTable', 'FromTableName'), g('FromColumn', 'FromColumnName'), g('ToTable', 'ToTableName'), g('ToColumn', 'ToColumnName')
        card = str(g('Cardinality') or '')
        fcard, tcard = str(g('FromCardinality') or 'many'), str(g('ToCardinality') or 'one')
        many, one, mc, oc = ft, tt, fc, tc
        if card in ('1:M', 'One to many') or (fcard == 'one' and tcard == 'many'):
            many, one, mc, oc = tt, ft, tc, fc
        out.append(dict(many=many, many_col=mc, one=one, one_col=oc, active=bool(g('IsActive') if g('IsActive') is not None else True),
                        both=str(g('CrossFilteringBehavior') or '').lower().startswith(('both', 'bothdir'))))
    return out


def plan_tables(data, steps, sources, dax):
    tables = list(data['tables']['TableName']) if len(data['tables']) else []
    rels = _norm_rels(data.get('relationships'))
    many_in = {r['many'] for r in rels}; one_in = {r['one'] for r in rels}
    cc = dax.get('calc_tables')
    calc_dates = set(cc[cc['Type'] == 'Date table']['Table']) if cc is not None and len(cc) else set()
    ms = dax.get('measures')
    meas_tables = set()
    if ms is not None and len(ms):
        for _, r in ms.iterrows():
            meas_tables |= {t for t in r['Tables used (incl. via other measures)'].split(', ') if t}
    merged_into, appended = {}, {}
    if steps is not None and len(steps):
        for _, r in steps.iterrows():
            fn, a = M._call(r['M code'].strip())
            if fn in ('Table.NestedJoin', 'Table.FuzzyNestedJoin', 'Table.Join'):
                merged_into.setdefault(M._tname(M.g(a, 2)), set()).add(r['Table'])
            elif fn == 'Table.Combine':
                for it in M._items(M.g(a, 0)):
                    appended.setdefault(M._tname(it), set()).add(r['Table'])
    cols = data['columns']
    rows = []
    for t in tables:
        n_cols = int((cols['TableName'] == t).sum()) if len(cols) else 0
        isdate = t in calc_dates or bool(re.search(r'^(dim_?)?(date|calendar)$', t, re.I))
        base = re.sub(r'^(fact|dim)[_ ]?', '', t, flags=re.I)
        if isdate:
            role, keep, why, gold = 'Date dimension', 'Keep', 'Replace the DAX calendar with ONE shared Gold dim_date (marked as Date table).', 'gold.dim_date'
        elif t in many_in and t not in one_in:
            role, keep, why, gold = 'Fact', 'Keep', 'Many-side of relationships: the measurable events. Build as a Gold fact table at the reporting grain.', f'gold.fact_{snake(base)}'
        elif t in one_in and t not in many_in:
            role, keep, why, gold = 'Dimension', 'Keep', 'One-side of relationships: descriptive attributes. Build as a Gold dimension with a unique key.', f'gold.dim_{snake(base)}'
        elif t in one_in and t in many_in:
            role, keep, why, gold = 'Dimension (snowflaked)', 'Merge into parent', 'Sits between two tables (snowflake). Flatten it into the dimension/fact that uses it to get a simple star schema.', f'(merge into parent)'
        elif t in merged_into:
            role, keep = 'Lookup / staging', 'Remove from model'
            why, gold = f'Only used to enrich {", ".join(sorted(merged_into[t]))} through a merge. Do the join in Gold and drop this table from the model.', f'gold.dim_{snake(base)}'
        elif t in appended:
            role, keep, why, gold = 'Staging (appended)', 'Remove from model', f'Only appended into {", ".join(sorted(appended[t]))}. Union it in Gold.', '(union in Gold)'
        elif t in meas_tables:
            fact_like = ms is not None and any(re.search(rf'\b(SUM|AVERAGE|COUNT|MIN|MAX)\w*\(\s*{re.escape(t)}\[', str(f)) for f in ms['DAX formula'])
            role = 'Fact (not related)' if fact_like else 'Disconnected, used in DAX'
            keep, why = 'Review', ('Used by measures but has no relationship. Add it to the star schema (relationship on a shared key) or fold its columns into a related Gold table.')
            gold = f'gold.fact_{snake(base)}' if fact_like else f'gold.dim_{snake(base)}'
        else:
            role, keep, why, gold = 'Unused / disconnected', 'Candidate to remove', 'No relationships and no measure reads it. Remove it from the model unless a report uses it directly.', '—'
        rows.append({'Table': t, 'Role': role, 'In Fabric semantic model': keep, 'Why': why, 'Gold object': gold, 'Columns today': n_cols})
    return pd.DataFrame(rows), rels


# ============================================================ DAX -> Gold plan
_SIMPLE = re.compile(r"^(SUM|AVERAGE|MIN|MAX|COUNT|COUNTA|DISTINCTCOUNT)\(\s*(?:'([^']+)'|(\w+))\[([^\]]+)\]\s*\)$", re.I)
_TI = {'TOTALYTD', 'TOTALMTD', 'TOTALQTD', 'DATESYTD', 'DATESMTD', 'DATESQTD', 'SAMEPERIODLASTYEAR', 'DATEADD', 'PARALLELPERIOD',
       'DATESBETWEEN', 'DATESINPERIOD', 'PREVIOUSMONTH', 'PREVIOUSYEAR', 'PREVIOUSQUARTER', 'NEXTMONTH', 'NEXTYEAR'}


def _simple_agg(f):
    m = _SIMPLE.match(f.strip())
    if m:
        return m.group(1).upper(), (m.group(2) or m.group(3)), m.group(4)
    m = re.match(r"^COUNTROWS\(\s*(?:'([^']+)'|(\w+))\s*\)$", f.strip(), re.I)
    return ('COUNTROWS', m.group(1) or m.group(2), None) if m else None


def plan_measures(dax, ctx):
    ms = dax.get('measures')
    if ms is None or ms.empty:
        return pd.DataFrame()
    formulas = {r['Measure']: D._clean(r['DAX formula']) for _, r in ms.iterrows()}
    rows = []
    for _, r in ms.iterrows():
        name, f = r['Measure'], formulas[r['Measure']]
        funcs = {k.upper() for k in re.findall(r'\b([A-Za-z_][\w.]*)\s*\(', D._mask(f))} - {'VAR'}
        res = dict(action='Review manually', work='Logic is too custom to classify automatically.', code='', dax=r['DAX formula'], effort='High', risk='')
        sa = _simple_agg(f)
        if sa:
            res.update(action='Keep as is', work='Already a simple aggregation. Make sure the Gold table exposes this column at the right grain.',
                       effort='Low')
        elif funcs <= {'DIVIDE', 'SUM', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'COUNTROWS', 'DISTINCTCOUNT', 'BLANK'} and not (funcs & {'CALCULATE'}):
            res.update(action='Keep as is', work='Simple arithmetic / ratio on other measures. Ratios must stay in the semantic model because they depend on user selections.',
                       effort='Low')
        elif (funcs & _TI) and not (funcs & {'SUMX', 'AVERAGEX', 'FILTER', 'RELATED'}):
            res.update(action='Keep (standard time intelligence)', effort='Low',
                       work='Keep the standard pattern. Requires Gold dim_date: continuous dates, one row per day, marked as Date table in the model.',
                       risk='Needs a proper Date table or results will be wrong.')
        elif re.match(r'^(SUMX|AVERAGEX|MAXX|MINX|COUNTX)\(', f, re.I) and f.count('(') >= 1:
            mt = re.match(r"^(SUMX|AVERAGEX|MAXX|MINX|COUNTX)\(\s*(?:'([^']+)'|(\w+))\s*,(.*)\)$", f, re.I | re.S)
            if mt:
                fx, tbl, body = mt.group(1).upper(), (mt.group(2) or mt.group(3)), mt.group(4)
                c = {'todos': []}
                sql = dax_sql(body, c, tbl)
                newc = snake(name)
                agg = {'SUMX': 'SUM', 'AVERAGEX': 'AVERAGE', 'MAXX': 'MAX', 'MINX': 'MIN', 'COUNTX': 'COUNT'}[fx]
                if not c['todos']:
                    ctx['extra'].setdefault(tbl, []).append((newc, sql, f'measure [{name}]', c.get('related', {})))
                    res.update(action='Move row logic to Gold column', effort='Low' if not c.get('related') else 'Medium',
                               work=f'Compute the row-level result as column "{newc}" in the Gold table for {tbl}, then aggregate it with a plain {agg}.',
                               dax=f'{agg}({tbl}[{newc}])', code=f'df = df.withColumn("{newc}", F.expr({py(sql)}))')
                else:
                    res.update(action='Move row logic to Gold column (manual)', work='Row-level iterator: build the per-row value in Gold. The expression needs manual translation.', effort='Medium')
        elif re.match(r'^CALCULATE\(', f, re.I):
            a = D._split_args(f[f.index('(') + 1:-1])
            base_f = formulas.get(a[0].strip()[1:-1], a[0]) if re.fullmatch(r'\[[^\]]+\]', a[0].strip()) else a[0]
            sb = _simple_agg(base_f)
            conds, ok, why_not = [], True, ''
            tbl = sb[1] if sb else None
            for flt in a[1:]:
                fu = flt.strip()
                mf = re.match(r'^FILTER\(\s*(?:ALL\(\s*(?:\'([^\']+)\'|(\w+))\s*\)|(?:\'([^\']+)\'|(\w+)))\s*,(.*)\)$', fu, re.I | re.S)
                if mf:
                    cond = mf.group(5)
                    if mf.group(1) or mf.group(2):
                        res['risk'] = 'FILTER(ALL(table)) also ignores slicer filters on that table; a flag column does not. Confirm the report logic.'
                elif D._find_op(D._mask(fu)) and not re.match(r'^[A-Z]+\(', fu):
                    cond = fu
                else:
                    ok = False; why_not = fu[:40]; break
                conds.append(cond)
            if sb and ok and conds and tbl:
                c = {'todos': []}
                cond_sql = ' AND '.join(f'({dax_sql(x, c, tbl)})' for x in conds)
                if not c['todos'] and not c.get('foreign'):
                    fnx, _, col = sb
                    newc = snake(name)
                    val = '1' if fnx == 'COUNTROWS' else _bt(col)
                    sql = f'CASE WHEN {cond_sql} THEN {val} END'
                    out_agg = {'COUNTROWS': 'COUNT'}.get(fnx, fnx)
                    ctx['extra'].setdefault(tbl, []).append((newc, sql, f'measure [{name}]', {}))
                    res.update(action='Move filter logic to Gold column', effort='Low',
                               work=f'Create flag-based column "{newc}" in the Gold table for {tbl} (value only where the condition is true), then aggregate it.',
                               dax=f'{out_agg}({tbl}[{newc}])', code=f'df = df.withColumn("{newc}", F.expr({py(sql)}))')
                    res['risk'] = res['risk'] or 'Same result when slicers filter only the displayed rows. Check measures that must ignore slicers.'
                else:
                    res.update(action='Move filter logic to Gold column (manual)', work='Condition spans other tables or needs manual translation.', effort='Medium')
            elif funcs & {'ALL', 'ALLEXCEPT', 'ALLSELECTED', 'REMOVEFILTERS'}:
                res.update(action='Keep (filter-context logic)', effort='Low',
                           work='Percent-of-total / "ignore filter" patterns depend on what the user selected, so they cannot be pre-computed. Keep as a small, simple measure.')
            elif funcs & _TI:
                res.update(action='Keep (standard time intelligence)', effort='Low', work='Keep the standard pattern on a Gold dim_date.')
        elif (funcs & {'ALL', 'ALLEXCEPT', 'ALLSELECTED', 'REMOVEFILTERS'}) and funcs <= {'DIVIDE', 'CALCULATE', 'SUM', 'AVERAGE', 'COUNT', 'COUNTROWS', 'DISTINCTCOUNT', 'MIN', 'MAX', 'ALL', 'ALLEXCEPT', 'ALLSELECTED', 'REMOVEFILTERS', 'VALUES'}:
            res.update(action='Keep (filter-context logic)', effort='Low',
                       work='Percent-of-total / "ignore filter" patterns depend on what the user selected, so they cannot be pre-computed. Keep as a small, simple measure.')
        elif funcs & {'IF', 'SWITCH'}:
            if re.search(r'BLANK\(\)', f) and '/' in f:
                alt = ''
                mv = re.search(r'IF\(\s*(\w+)\s*=\s*0\s*,\s*BLANK\(\)\s*,\s*\(\s*(\w+)\s*-\s*(\w+)\s*\)\s*/\s*(\w+)\s*\)', f)
                vars_ = dict(re.findall(r'VAR\s+(\w+)\s*=\s*(\[[^\]]+\])', f))
                if mv and mv.group(1) == mv.group(3) == mv.group(4) and mv.group(2) in vars_ and mv.group(3) in vars_:
                    alt = f'DIVIDE({vars_[mv.group(2)]} - {vars_[mv.group(3)]}, {vars_[mv.group(3)]})'
                res.update(action='Simplify DAX', effort='Low', work='Replace IF(x = 0, BLANK(), a / b) with DIVIDE. Keep as a measure: it depends on other measures.',
                           dax=alt or 'DIVIDE(numerator, denominator)')
            elif re.search(r'\[[^\]]+\]', re.sub(r'\w+\[[^\]]+\]', '', f)) and ('"' in f):
                res.update(action='Redesign (aggregate in Gold)', effort='High',
                           work='The label depends on a measure (an aggregated value), so it cannot be a row-level column. Either pre-aggregate in a Gold table at the reporting grain and put the band there, or keep this one measure.',
                           risk='Banding on aggregated values changes with the filter level.')
            else:
                c = {'todos': []}
                sql = dax_sql(f, c, None)
                if not c['todos']:
                    res.update(action='Move row logic to Gold column', effort='Low', work=f'Create "{snake(name)}" as a CASE expression in Gold.', code=f'df = df.withColumn("{snake(name)}", F.expr({py(sql)}))')
        rows.append({'Measure': name, 'Table': r['Table'], 'Current pattern': r['Pattern'], 'Gold layer action': res['action'],
                     'Gold layer work': res['work'], 'Simple DAX in Power BI': res['dax'], 'Gold code (PySpark)': res['code'],
                     'Effort': res['effort'], 'Risk / check': res['risk']})
    return pd.DataFrame(rows)


def plan_calc_columns(dax, ctx):
    cc = dax.get('calc_columns')
    if cc is None or cc.empty:
        return pd.DataFrame()
    rows = []
    for _, r in cc.iterrows():
        c = {'todos': []}
        sql = dax_sql(r['DAX formula'], c, r['Table'])
        rel = c.get('related', {})
        if c['todos'] or c.get('foreign'):
            code, status = f'# TODO (manual): {r["DAX formula"][:100]}', 'Manual translation needed'
        else:
            code, status = f'df = df.withColumn({py(r["Column"])}, F.expr({py(sql)}))', 'Auto-translated'
            ctx['extra'].setdefault(r['Table'], []).append((r['Column'], sql, f'calculated column {r["Table"]}[{r["Column"]}]', rel))
        rows.append({'Table': r['Table'], 'Column': r['Column'], 'DAX formula': r['DAX formula'], 'Status': status,
                     'Gold code (PySpark)': code, 'Joins needed': ', '.join(f'{t}[{c_}]' for (t, c_) in rel) or '—'})
    return pd.DataFrame(rows)


def dim_date_code(start='2020-01-01'):
    return (f'# Gold dim_date: one shared calendar for the whole model (replaces DAX CALENDAR tables)\n'
            f'df_dim_date = (spark.sql("SELECT explode(sequence(to_date(\'{start}\'), add_months(current_date(), 24), interval 1 day)) AS Date")\n'
            '    .withColumn("Year", F.year("Date")).withColumn("Quarter", F.quarter("Date"))\n'
            '    .withColumn("MonthNumber", F.month("Date")).withColumn("MonthName", F.date_format("Date", "MMMM"))\n'
            '    .withColumn("YearMonth", F.date_format("Date", "yyyy-MM")).withColumn("DayOfWeek", F.dayofweek("Date"))\n'
            '    .withColumn("IsWeekend", F.dayofweek("Date").isin(1, 7))\n'
            '    .withColumn("IsCurrentYear", F.year("Date") == F.year(F.current_date())))\n'
            'df_dim_date.write.mode("overwrite").option("overwriteSchema", "true").format("delta").saveAsTable("gold.dim_date")')


# ============================================================ build everything
def build_blueprint(data, steps, sources, dax, schema='gold'):
    ctx = {'extra': {}}
    tplan, rels = plan_tables(data, steps, sources, dax)
    mplan = plan_measures(dax, ctx)
    cplan = plan_calc_columns(dax, ctx)
    disp = {r['Table']: r for _, r in tplan.iterrows()}
    names = list(steps['Table'].unique()) if steps is not None and len(steps) else []

    def tv(n):
        return 'df_' + snake(n)
    ctx['var'] = tv
    # order: dependencies first
    deps = {t: set() for t in names}
    for _, r in (steps if len(names) else pd.DataFrame()).iterrows():
        fn, a = M._call(r['M code'].strip())
        if fn in ('Table.NestedJoin', 'Table.FuzzyNestedJoin', 'Table.Join'):
            d = M._tname(M.g(a, 2))
        elif fn == 'Table.Combine':
            for it in M._items(M.g(a, 0)):
                if M._tname(it) in deps: deps[r['Table']].add(M._tname(it))
            continue
        elif r['Operation'] == 'Connect to source' and 'Starts from another query' in r['What it does (plain English)']:
            d = re.search(r'"([^"]+)"', r['What it does (plain English)']).group(1)
        else:
            continue
        if d in deps and d != r['Table']:
            deps[r['Table']].add(d)
    ordered, left = [], list(names)
    while left:
        prog = False
        for t in list(left):
            if deps[t] <= set(ordered):
                ordered.append(t); left.remove(t); prog = True
        if not prog:
            ordered += left; break

    cells, code_by_table, stats = [], {}, []
    cells.append(('markdown', '# Gold layer: draft notebook\n\nGenerated from the Power Query (M) steps and DAX of the Power BI model. '
                  '**This is a draft:** review every step, replace the Bronze table names with your own, and test row counts against the original report.\n\n'
                  'Lines starting with `# TODO (manual)` could not be translated automatically.'))
    cells.append(('code', 'from pyspark.sql import functions as F, Window\n\nspark.sql("CREATE SCHEMA IF NOT EXISTS gold")  # remove if your Lakehouse has no schema support'))
    for t in ordered:
        s = steps[steps['Table'] == t].to_dict('records')
        srow = sources[sources['Table'] == t].iloc[0].to_dict() if len(sources) and (sources['Table'] == t).any() else {}
        body, ok, man = gen_table_code(t, s, srow, ctx)
        lines = [f'# ===== {t} =====', body]
        for (nm, sql, origin, rel) in ctx['extra'].get(t, []):
            for (rt, rc), alias in rel.items():
                rr = [x for x in rels if (x['many'] == t and x['one'] == rt)]
                if rr:
                    lines.append(f'# lookup for RELATED({rt}[{rc}])\n_l = {tv(rt)}.select(F.col({py(rr[0]["one_col"])}).alias("__rk"), F.col({py(rc)}).alias({py(alias)}))\n'
                                 f'df = df.join(_l, F.col({py(rr[0]["many_col"])}) == F.col("__rk"), "left").drop("__rk")')
                else:
                    lines.append(f'# TODO (manual): join {rt} to read {rc} (no relationship found in the model)')
            lines.append(f'df = df.withColumn({py(nm)}, F.expr({py(sql)}))  # from DAX: {origin}')
        lines.append(f'{tv(t)} = df')
        d = disp.get(t)
        if d is not None and d['In Fabric semantic model'] in ('Keep', 'Review', 'Merge into parent') and d['Gold object'].startswith('gold.'):
            lines.append(f'{tv(t)}.write.mode("overwrite").option("overwriteSchema", "true").format("delta").saveAsTable("{d["Gold object"]}")')
        else:
            lines.append('# Staging only: not written to the Gold schema (joined/unioned into another table).')
        code = '\n'.join(lines)
        code_by_table[t] = code
        stats.append({'Table': t, 'Steps translated automatically': ok, 'Steps needing manual work': man})
        cells.append(('markdown', f'## {t}'))
        cells.append(('code', code))
    cc = dax.get('calc_tables')
    if cc is not None and len(cc):
        for _, r in cc.iterrows():
            if r['Type'] == 'Date table':
                ms_ = re.search(r'DATE\(\s*(\d{4})\s*,\s*(\d+)\s*,\s*(\d+)', r['DAX formula'])
                st_ = f'{ms_.group(1)}-{int(ms_.group(2)):02d}-{int(ms_.group(3)):02d}' if ms_ else '2020-01-01'
                cells.append(('markdown', f'## dim_date (replaces calculated table "{r["Table"]}")'))
                cells.append(('code', dim_date_code(st_)))
                code_by_table[f'dim_date ({r["Table"]})'] = dim_date_code(st_)
            else:
                cells.append(('markdown', f'## {r["Table"]} ({r["Type"]})'))
                cells.append(('code', f'# TODO (manual): rebuild calculated table "{r["Table"]}" in Gold.\n# DAX: {r["DAX formula"][:200]}'))
    ms_ = dax.get('measures')
    formulas = {r['Measure']: r['DAX formula'] for _, r in ms_.iterrows()} if ms_ is not None and len(ms_) else {}
    return dict(tables=tplan, measures=mplan, calc_columns=cplan, cells=cells, code=code_by_table, stats=pd.DataFrame(stats),
                extra=ctx['extra'], formulas=formulas)


def to_ipynb(cells) -> bytes:
    nb = {"nbformat": 4, "nbformat_minor": 5,
          "metadata": {"language_info": {"name": "python"}, "kernel_info": {"name": "synapse_pyspark"},
                       "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark", "language": "Python"}},
          "cells": []}
    for i, (kind, src) in enumerate(cells):
        c = {"cell_type": kind, "metadata": {}, "id": f"cell{i}", "source": src.splitlines(True)}
        if kind == 'code':
            c.update({"execution_count": None, "outputs": []})
        nb["cells"].append(c)
    return json.dumps(nb, indent=1).encode('utf-8')


def to_py(cells) -> str:
    out = []
    for kind, src in cells:
        out.append('# %%\n' + (('# ' + src.replace('\n', '\n# ')) if kind == 'markdown' else src))
    return '\n\n'.join(out) + '\n'
