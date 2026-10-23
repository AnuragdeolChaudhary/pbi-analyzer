"""Rule-based (zero-cost) DAX explainer: plain-English meaning, dependencies, quality flags, Gold-layer advice."""
import re
from collections import Counter
import pandas as pd

# ====================================================================== text helpers
def _mask(s):
    """Blank the contents of "strings", 'table names' and [column names] (same length)."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in '"\'':
            q = c; out.append(c); i += 1
            while i < n:
                if s[i] == q:
                    if i + 1 < n and s[i + 1] == q:
                        out.append('__'); i += 2; continue
                    break
                out.append('_'); i += 1
            if i < n:
                out.append(q); i += 1
        elif c == '[':
            out.append('['); i += 1
            while i < n:
                if s[i] == ']':
                    if i + 1 < n and s[i + 1] == ']':
                        out.append('__'); i += 2; continue
                    break
                out.append('_'); i += 1
            if i < n:
                out.append(']'); i += 1
        else:
            out.append(c); i += 1
    return ''.join(out)


def _strip_comments(s):
    m = _mask(s)
    chars = list(s)
    for mt in re.finditer(r'//[^\n]*|--[^\n]*|/\*.*?\*/', m, re.S):
        for i in range(mt.start(), mt.end()):
            if chars[i] != '\n':
                chars[i] = ' '
    return ''.join(chars)


def _clean(expr):
    return re.sub(r'\s+', ' ', _strip_comments(expr or '')).strip()


def _match_paren(masked, start):
    depth = 0
    for i in range(start, len(masked)):
        if masked[i] == '(':
            depth += 1
        elif masked[i] == ')':
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_args(s):
    m = _mask(s)
    parts, depth, last = [], 0, 0
    for i, c in enumerate(m):
        if c in '({':
            depth += 1
        elif c in ')}':
            depth -= 1
        elif c == ',' and depth == 0:
            parts.append(s[last:i].strip()); last = i + 1
    tail = s[last:].strip()
    if tail or parts:
        parts.append(tail)
    return parts


_REF = re.compile(r"(?:'((?:[^']|'')+)'|([A-Za-z_][\w]*))\[((?:[^\]]|\]\])+)\]")
_MEAS = re.compile(r"(?<![\w'\]])\[((?:[^\]]|\]\])+)\]")


def _s(v):
    """String value, treating None/NaN as empty."""
    return '' if v is None or (isinstance(v, float) and v != v) else str(v)


def _short(t, n=70):
    t = re.sub(r'\s+', ' ', t.strip())
    return t if len(t) <= n else t[:n - 1] + '…'


# ====================================================================== operators
_BIN = [  # lowest precedence first
    ('||', 'OR'), ('&&', 'AND'),
    ('<>', 'is not equal to'), ('>=', 'is at least'), ('<=', 'is at most'), ('=', 'equals'), ('>', 'is greater than'), ('<', 'is less than'),
    ('&', 'joined with'), ('+', 'plus'), ('-', 'minus'), ('*', 'multiplied by'), ('/', 'divided by'), ('^', 'to the power of'),
]
_LEVELS = [['||'], ['&&'], ['<>', '>=', '<=', '=', '>', '<'], ['&'], ['+', '-'], ['*', '/'], ['^']]
_OPWORD = dict(_BIN)


def _find_op(masked):
    """Return (index, operator) of the top-level binary operator to split on, or None."""
    n = len(masked)
    depth = 0
    pos = {}
    i = 0
    while i < n:
        c = masked[i]
        if c in '({':
            depth += 1
        elif c in ')}':
            depth -= 1
        elif depth == 0:
            two = masked[i:i + 2]
            if two in ('||', '&&', '<>', '>=', '<='):
                pos.setdefault(two, []).append(i); i += 2; continue
            if c in '=<>&+-*/^':
                if c == '-' or c == '+':
                    prev = masked[:i].rstrip()
                    if not prev or prev[-1] in '(,=<>&+-*/^|' or re.search(r'\b(?:RETURN|IN|AND|OR|NOT)$', prev):
                        i += 1; continue
                pos.setdefault(c, []).append(i)
        i += 1
    for lvl in _LEVELS:
        cands = [(p, op) for op in lvl for p in pos.get(op, [])]
        if cands:
            return max(cands)  # rightmost → left-associative
    return None


# ====================================================================== translator
def _is_compound(txt):
    return bool(re.search(r'recalculated with changed filters|\b(plus|minus|multiplied by|divided by|joined with|equals|is greater than|is less than|is at least|is at most|is not equal to|AND|OR)\b', txt))


def _wrap(txt):
    return f'({txt})' if _is_compound(txt) else txt


def _ref_text(m):
    tbl = m.group(1) or m.group(2)
    col = m.group(3).replace(']]', ']')
    return f"{tbl.replace(chr(39) * 2, chr(39))}[{col}]"


def _tr(expr, depth=0):
    e = (expr or '').strip()
    if not e:
        return ''
    if depth > 25:
        return _short(e, 60)
    m = _mask(e)

    # VAR ... RETURN
    if re.match(r'VAR\b', m, re.I):
        return _tr_var(e, m, depth)

    # strip wrapping parentheses
    if e.startswith('(') and _match_paren(m, 0) == len(e) - 1:
        return _tr(e[1:-1], depth + 1)

    # NOT
    mn = re.match(r'NOT\s+(.*)$', e, re.I | re.S)
    if mn and not re.match(r'NOT\s*\(', e, re.I):
        return f'NOT ({_tr(mn.group(1), depth + 1)})'

    # IN operator
    mi = re.search(r'\s+IN\s+', m, re.I)
    if mi and _find_op(m) is None:
        return f'{_tr(e[:mi.start()], depth + 1)} is one of {_tr(e[mi.end():], depth + 1)}'

    # binary operator
    op = _find_op(m)
    if op:
        i, o = op
        l, r = e[:i if len(o) == 1 else i], e[i + len(o):]
        lt, rt = _tr(e[:i], depth + 1), _tr(e[i + len(o):], depth + 1)
        if o in ('+', '-', '*', '/', '^'):
            return f'{_wrap(lt)} {_OPWORD[o]} {_wrap(rt)}'
        return f'{lt} {_OPWORD[o]} {rt}'

    # list literal {...}
    if e.startswith('{') and e.endswith('}'):
        return '{' + ', '.join(_tr(a, depth + 1) for a in _split_args(e[1:-1])) + '}'

    # function call spanning whole expression
    mc = re.match(r'([A-Za-z_][\w.]*)\s*\(', e)
    if mc and _match_paren(m, mc.end() - 1) == len(e) - 1:
        fn = mc.group(1).upper()
        args = _split_args(e[mc.end():-1])
        h = HANDLERS.get(fn)
        if h:
            try:
                return h(args, depth)
            except Exception:
                pass
        return f'{mc.group(1)}(' + ', '.join(_tr(a, depth + 1) for a in args) + ')'

    # table[column]
    mr = _REF.fullmatch(e)
    if mr:
        return _ref_text(mr)
    mm = re.fullmatch(r'\[((?:[^\]]|\]\])+)\]', e)
    if mm:
        return f'[{mm.group(1)}]'
    if re.fullmatch(r"'(?:[^']|'')+'", e):
        return e[1:-1]
    return e


def _tr_var(e, m, depth):
    # split into VAR blocks and the RETURN at top level
    d, marks = 0, []
    for mt in re.finditer(r'\b(VAR|RETURN)\b', m, re.I):
        if m[:mt.start()].count('(') - m[:mt.start()].count(')') == 0:
            marks.append((mt.start(), mt.group(1).upper()))
    steps, result = [], ''
    for k, (pos, kind) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(e)
        body = e[pos + len(kind):end].strip()
        if kind == 'VAR':
            nm, _, val = body.partition('=')
            steps.append(f'{nm.strip()} = {_tr(val, depth + 1)}')
        else:
            result = _tr(body, depth + 1)
    if not result:
        return '; '.join(steps)
    lines = [f'Step {i}: {s}' for i, s in enumerate(steps, 1)]
    return '\n'.join(lines + [f'Result: {result}'])


def _a(args, i, depth, default=''):
    return _tr(args[i], depth + 1) if len(args) > i and args[i] != '' else default


def _fname(raw):
    m = re.match(r'([A-Za-z_][\w.]*)\s*\(', raw.strip())
    return m.group(1).upper() if m else ''


# ---------------------------------------------------------------- handlers: (args(raw list), depth) -> text
def _agg(word):
    return lambda a, d: f'the {word} of {_a(a, 0, d)}'


def h_calculate(a, d):
    base = _a(a, 0, d)
    filt = []
    for raw in a[1:]:
        f = _fname(raw)
        t = _tr(raw, d + 1)
        if f in ('ALL', 'ALLEXCEPT', 'ALLSELECTED', 'REMOVEFILTERS', 'USERELATIONSHIP', 'CROSSFILTER', 'KEEPFILTERS', 'TREATAS', 'FILTER', 'VALUES',
                 'DATESYTD', 'DATESMTD', 'DATESQTD', 'SAMEPERIODLASTYEAR', 'DATEADD', 'DATESBETWEEN', 'DATESINPERIOD', 'PARALLELPERIOD',
                 'PREVIOUSMONTH', 'PREVIOUSYEAR', 'PREVIOUSQUARTER', 'PREVIOUSDAY', 'NEXTMONTH', 'NEXTYEAR', 'NEXTQUARTER', 'NEXTDAY'):
            filt.append(t)
        elif _find_op(_mask(raw)):
            filt.append(f'keep only rows where {t}')
        else:
            filt.append(t)
    if not filt:
        return base
    return f'{base}, recalculated with changed filters: ' + '; '.join(f'({i}) {x}' for i, x in enumerate(filt, 1))


def h_filter(a, d):
    if _fname(a[0]) == 'ALL' and len(a) > 1:
        inner = ', '.join(_tr(x, d + 1) for x in _split_args(a[0][a[0].index('(') + 1:-1]))
        return f'all rows of {inner} (ignoring current filters) where {_a(a, 1, d)}'
    return f'the rows of {_a(a, 0, d)} where {_a(a, 1, d)}'


def h_countrows(a, d):
    t = _a(a, 0, d)
    if t.startswith('the rows of '):
        return 'the number of ' + t[4:]
    if t.startswith('all rows of '):
        return 'the number of ' + t
    return f'the number of rows in {t}'


def h_all(a, d):
    return 'ignoring every filter on ' + (', '.join(_tr(x, d + 1) for x in a) if a else 'everything')


def h_allexcept(a, d):
    return f'ignoring all filters on {_a(a, 0, d)} except those on ' + ', '.join(_tr(x, d + 1) for x in a[1:])


def h_divide(a, d):
    alt = f', otherwise {_a(a, 2, d)}' if len(a) > 2 else ', otherwise blank'
    return f'{_wrap(_a(a, 0, d))} divided by {_wrap(_a(a, 1, d))} (safe: when the denominator is 0 or blank{alt})'


def h_if(a, d):
    s = f'if {_a(a, 0, d)} then {_a(a, 1, d)}'
    if len(a) > 2:
        s += f' otherwise {_a(a, 2, d)}'
    return s


def h_switch(a, d):
    first = a[0].strip().upper() if a else ''
    parts, i = [], 1
    while i + 1 < len(a):
        v = _a(a, i, d)
        parts.append(f'{v}: {_a(a, i + 1, d)}' if first != 'TRUE()' else f'if {v} → {_a(a, i + 1, d)}')
        i += 2
    s = ('pick the first matching case: ' if first == 'TRUE()' else f'look at {_a(a, 0, d)} and return: ') + '; '.join(parts)
    if i < len(a):
        s += f'; otherwise {_a(a, i, d)}'
    return s


def _iter(word):
    return lambda a, d: f'for each row of {_a(a, 0, d)}, work out {_a(a, 1, d)}, then take the {word} of the results'


def _ti(word):
    return lambda a, d: f'{word} of {_a(a, 0, d)}' + (f' (using {_a(a, 1, d)})' if len(a) > 1 else '')


def h_dateadd(a, d):
    return f'{_a(a, 0, d)} shifted by {_a(a, 1, d)} {_a(a, 2, d).lower()}(s)'


def h_selectedvalue(a, d):
    return f'the single value currently selected in {_a(a, 0, d)}' + (f' (or {_a(a, 1, d)} if none or several)' if len(a) > 1 else '')


def h_related(a, d):
    return f'{_a(a, 0, d)} looked up from the related table'


def h_userel(a, d):
    return f'using the inactive relationship between {_a(a, 0, d)} and {_a(a, 1, d)}'


def h_rankx(a, d):
    return f'rank of each row of {_a(a, 0, d)} by {_a(a, 1, d)}' + (f' ({_a(a, 3, d)})' if len(a) > 3 else '')


def h_topn(a, d):
    return f'the top {_a(a, 0, d)} rows of {_a(a, 1, d)} by {_a(a, 2, d)}' if len(a) > 2 else f'the top {_a(a, 0, d)} rows of {_a(a, 1, d)}'


def h_values(a, d):
    return f'the distinct values of {_a(a, 0, d)}'


def h_text(name):
    return lambda a, d: f'{name}(' + ', '.join(_tr(x, d + 1) for x in a) + ')'


def h_blank(a, d):
    return 'blank'


def h_concatx(a, d):
    return f'for each row of {_a(a, 0, d)}, take {_a(a, 1, d)} and join the results' + (f' with {_a(a, 2, d)}' if len(a) > 2 else '')


def h_lookup(a, d):
    return f'look up {_a(a, 0, d)} where ' + ' and '.join(f'{_a(a, i, d)} = {_a(a, i + 1, d)}' for i in range(1, len(a) - 1, 2))


def h_addcols(a, d):
    return f'{_a(a, 0, d)} with extra columns: ' + ', '.join(f'{_a(a, i, d)} = {_a(a, i + 1, d)}' for i in range(1, len(a) - 1, 2))


def h_summarize(a, d):
    return f'{_a(a, 0, d)} grouped by ' + ', '.join(_tr(x, d + 1) for x in a[1:])


def h_calendar(a, d):
    return f'a list of dates from {_a(a, 0, d)} to {_a(a, 1, d)}'


HANDLERS = {
    'SUM': _agg('total'), 'AVERAGE': _agg('average'), 'MIN': lambda a, d: f'the smallest of {", ".join(_tr(x, d + 1) for x in a)}',
    'MAX': lambda a, d: f'the largest of {", ".join(_tr(x, d + 1) for x in a)}', 'MEDIAN': _agg('median'),
    'COUNT': lambda a, d: f'the number of non-blank numeric values in {_a(a, 0, d)}',
    'COUNTA': lambda a, d: f'the number of non-blank values in {_a(a, 0, d)}',
    'COUNTBLANK': lambda a, d: f'the number of blank values in {_a(a, 0, d)}',
    'COUNTROWS': h_countrows,
    'DISTINCTCOUNT': lambda a, d: f'the number of different values in {_a(a, 0, d)}',
    'DISTINCTCOUNTNOBLANK': lambda a, d: f'the number of different non-blank values in {_a(a, 0, d)}',
    'CALCULATE': h_calculate, 'CALCULATETABLE': h_calculate, 'FILTER': h_filter,
    'ALL': h_all, 'ALLNOBLANKROW': h_all, 'ALLEXCEPT': h_allexcept, 'REMOVEFILTERS': h_all,
    'ALLSELECTED': lambda a, d: 'ignoring filters inside the visual but keeping what the user selected on ' + (', '.join(_tr(x, d + 1) for x in a) or 'the report'),
    'KEEPFILTERS': lambda a, d: f'adding to (not replacing) existing filters: {_a(a, 0, d)}',
    'VALUES': h_values, 'DISTINCT': h_values, 'DIVIDE': h_divide,
    'IF': h_if, 'IFERROR': lambda a, d: f'{_a(a, 0, d)}, or {_a(a, 1, d)} if that gives an error', 'SWITCH': h_switch,
    'AND': lambda a, d: f'{_a(a, 0, d)} AND {_a(a, 1, d)}', 'OR': lambda a, d: f'{_a(a, 0, d)} OR {_a(a, 1, d)}',
    'NOT': lambda a, d: f'NOT ({_a(a, 0, d)})',
    'ISBLANK': lambda a, d: f'{_a(a, 0, d)} is blank', 'BLANK': h_blank, 'TRUE': lambda a, d: 'true', 'FALSE': lambda a, d: 'false',
    'COALESCE': lambda a, d: 'the first non-blank of ' + ', '.join(_tr(x, d + 1) for x in a),
    'SUMX': _iter('total'), 'AVERAGEX': _iter('average'), 'MAXX': _iter('largest value'), 'MINX': _iter('smallest value'),
    'COUNTX': _iter('count'), 'RANKX': h_rankx, 'TOPN': h_topn, 'CONCATENATEX': h_concatx,
    'SELECTEDVALUE': h_selectedvalue, 'HASONEVALUE': lambda a, d: f'exactly one value is selected in {_a(a, 0, d)}',
    'ISFILTERED': lambda a, d: f'{_a(a, 0, d)} is filtered', 'ISINSCOPE': lambda a, d: f'{_a(a, 0, d)} is a level shown in the visual',
    'RELATED': h_related, 'RELATEDTABLE': lambda a, d: f'the related rows of {_a(a, 0, d)}',
    'USERELATIONSHIP': h_userel, 'LOOKUPVALUE': h_lookup,
    'TOTALYTD': _ti('year-to-date'), 'TOTALMTD': _ti('month-to-date'), 'TOTALQTD': _ti('quarter-to-date'),
    'DATESYTD': lambda a, d: f'dates from the start of the year to the latest date in {_a(a, 0, d)}',
    'DATESMTD': lambda a, d: f'dates from the start of the month to the latest date in {_a(a, 0, d)}',
    'DATESQTD': lambda a, d: f'dates from the start of the quarter to the latest date in {_a(a, 0, d)}',
    'SAMEPERIODLASTYEAR': lambda a, d: f'the same period one year earlier ({_a(a, 0, d)})',
    'DATEADD': h_dateadd, 'PARALLELPERIOD': h_dateadd,
    'DATESBETWEEN': lambda a, d: f'dates in {_a(a, 0, d)} from {_a(a, 1, d)} to {_a(a, 2, d)}',
    'DATESINPERIOD': lambda a, d: f'the {_a(a, 3, d).lower()}s ({_a(a, 2, d)}) after/before {_a(a, 1, d)} in {_a(a, 0, d)}',
    'PREVIOUSMONTH': lambda a, d: f'the previous month of {_a(a, 0, d)}', 'PREVIOUSYEAR': lambda a, d: f'the previous year of {_a(a, 0, d)}',
    'PREVIOUSQUARTER': lambda a, d: f'the previous quarter of {_a(a, 0, d)}', 'PREVIOUSDAY': lambda a, d: f'the previous day of {_a(a, 0, d)}',
    'NEXTMONTH': lambda a, d: f'the next month of {_a(a, 0, d)}', 'NEXTYEAR': lambda a, d: f'the next year of {_a(a, 0, d)}',
    'EOMONTH': lambda a, d: f'the last day of the month, {_a(a, 1, d)} month(s) from {_a(a, 0, d)}',
    'TODAY': lambda a, d: "today's date", 'NOW': lambda a, d: 'the current date and time',
    'YEAR': lambda a, d: f'the year of {_a(a, 0, d)}', 'MONTH': lambda a, d: f'the month of {_a(a, 0, d)}',
    'DAY': lambda a, d: f'the day of {_a(a, 0, d)}', 'FORMAT': lambda a, d: f'{_a(a, 0, d)} converted to text in format {_a(a, 1, d)}',
    'ROUND': lambda a, d: f'{_a(a, 0, d)} rounded to {_a(a, 1, d)} decimals', 'ABS': lambda a, d: f'the absolute value of {_a(a, 0, d)}',
    'INT': lambda a, d: f'{_a(a, 0, d)} rounded down to a whole number', 'VALUE': lambda a, d: f'{_a(a, 0, d)} converted to a number',
    'LEFT': lambda a, d: f'the first {_a(a, 1, d, "1")} characters of {_a(a, 0, d)}', 'RIGHT': lambda a, d: f'the last {_a(a, 1, d, "1")} characters of {_a(a, 0, d)}',
    'UPPER': lambda a, d: f'{_a(a, 0, d)} in capitals', 'LOWER': lambda a, d: f'{_a(a, 0, d)} in lower case', 'TRIM': lambda a, d: f'{_a(a, 0, d)} without extra spaces',
    'CONCATENATE': lambda a, d: f'{_a(a, 0, d)} joined with {_a(a, 1, d)}',
    'ADDCOLUMNS': h_addcols, 'SUMMARIZE': h_summarize, 'SELECTCOLUMNS': lambda a, d: f'{_a(a, 0, d)} keeping only the columns you name',
    'CALENDAR': h_calendar, 'CALENDARAUTO': lambda a, d: 'a date list that automatically covers every date found in the model',
    'GENERATESERIES': lambda a, d: f'a series of numbers from {_a(a, 0, d)} to {_a(a, 1, d)} step {_a(a, 2, d, "1")}',
    'DATATABLE': lambda a, d: 'a small table typed in by hand (hard-coded values)',
    'MAXA': lambda a, d: f'the largest of {_a(a, 0, d)}', 'UNION': lambda a, d: 'the rows of ' + ' stacked with '.join(_tr(x, d + 1) for x in a),
    'EARLIER': lambda a, d: f'the value of {_a(a, 0, d)} from the outer row', 'TREATAS': lambda a, d: f'apply the values of {_a(a, 0, d)} as a filter on {_a(a, 1, d)}',
    'CROSSFILTER': lambda a, d: f'changing the filter direction between {_a(a, 0, d)} and {_a(a, 1, d)} to {_a(a, 2, d)}',
    'DATE': lambda a, d: f'the date {_a(a, 0, d)}-{_a(a, 1, d)}-{_a(a, 2, d)}',
    'FIRSTDATE': lambda a, d: f'the first date in {_a(a, 0, d)}', 'LASTDATE': lambda a, d: f'the last date in {_a(a, 0, d)}',
    'USERPRINCIPALNAME': lambda a, d: 'the e-mail/login of the current user', 'USERNAME': lambda a, d: 'the login name of the current user',
}
_ITERATORS = {'SUMX', 'AVERAGEX', 'MAXX', 'MINX', 'COUNTX', 'RANKX', 'CONCATENATEX', 'FILTER', 'ADDCOLUMNS', 'GENERATE'}
_TIME = {'TOTALYTD', 'TOTALMTD', 'TOTALQTD', 'DATESYTD', 'DATESMTD', 'DATESQTD', 'SAMEPERIODLASTYEAR', 'DATEADD', 'PARALLELPERIOD',
         'DATESBETWEEN', 'DATESINPERIOD', 'PREVIOUSMONTH', 'PREVIOUSYEAR', 'PREVIOUSQUARTER', 'PREVIOUSDAY', 'NEXTMONTH', 'NEXTYEAR',
         'NEXTQUARTER', 'NEXTDAY', 'EOMONTH', 'FIRSTDATE', 'LASTDATE', 'OPENINGBALANCEMONTH', 'CLOSINGBALANCEMONTH'}
_CTX = {'ALL', 'ALLEXCEPT', 'ALLSELECTED', 'REMOVEFILTERS', 'KEEPFILTERS', 'USERELATIONSHIP', 'CROSSFILTER', 'TREATAS', 'ALLNOBLANKROW'}
_SIMPLE_AGG = {'SUM', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'COUNTA', 'COUNTROWS', 'DISTINCTCOUNT', 'MEDIAN'}


# ====================================================================== analysis
def _analyse(expr, measure_names, own_name=None):
    clean = _clean(expr)
    masked = _mask(clean)
    nostr = re.sub(r'"(?:[^"]|"")*"', '""', clean)
    funcs = Counter(f.upper() for f in re.findall(r'\b([A-Za-z_][\w.]*)\s*\(', masked))
    funcs.pop('VAR', None)
    # column references
    cols = []
    for mt in _REF.finditer(nostr):
        r = _ref_text(mt)
        if r not in cols:
            cols.append(r)
    rest = _REF.sub(' ', nostr)
    meas = []
    for mt in _MEAS.finditer(rest):
        nm = mt.group(1).replace(']]', ']')
        if nm in measure_names and nm != own_name and nm not in meas:
            meas.append(nm)
    tables = []
    for c in cols:
        t = c.split('[', 1)[0]
        if t not in tables:
            tables.append(t)
    # also bare table names used as arguments, e.g. SUMX(Sales, ...)
    for mt in re.finditer(r"\b(?:COUNTROWS|SUMX|AVERAGEX|MAXX|MINX|COUNTX|FILTER|ADDCOLUMNS|CALCULATETABLE)\(\s*('(?:[^']|'')+'|[A-Za-z_]\w*)\s*[,)]", nostr, re.I):
        t = mt.group(1).strip("'")
        if t not in tables and not t.upper() in HANDLERS:
            tables.append(t)
    depth = mx = 0
    for c in masked:
        if c == '(':
            depth += 1; mx = max(mx, depth)
        elif c == ')':
            depth -= 1
    calls = sum(funcs.values())
    score = calls + 2 * mx + len(clean) // 250
    level = 'Low' if score < 8 else ('Medium' if score < 20 else 'High')
    return dict(clean=clean, nostr=nostr, funcs=funcs, cols=cols, tables=tables, measures=meas, depth=mx, calls=calls,
                complexity=level, score=score)


def _pattern(an):
    f = an['funcs']; names = set(f)
    top = re.match(r'([A-Za-z_][\w.]*)\s*\(', an['clean'])
    topfn = top.group(1).upper() if top else ''
    if names & _TIME:
        return 'Time intelligence'
    if 'RANKX' in names or 'TOPN' in names:
        return 'Ranking / Top N'
    if topfn == 'DIVIDE' or (not names and '/' in an['clean']) or ('DIVIDE' in names and topfn in ('DIVIDE', 'VAR', '')):
        return 'Ratio / percentage'
    if names & {'SUMX', 'AVERAGEX', 'MAXX', 'MINX', 'COUNTX'}:
        return 'Row-by-row calculation (iterator)'
    if 'CALCULATE' in names or 'CALCULATETABLE' in names or 'FILTER' in names:
        return 'Filtered / context-changing calculation'
    if names & {'SWITCH', 'IF'}:
        return 'Conditional logic'
    if names & {'FORMAT', 'CONCATENATEX'} or topfn in ('FORMAT',):
        return 'Text / formatting'
    if topfn in _SIMPLE_AGG and len(names) == 1:
        return 'Simple aggregation'
    if not names and an['measures']:
        return 'Combination of other measures'
    if names <= {'SELECTEDVALUE', 'HASONEVALUE', 'ISFILTERED', 'ISINSCOPE', 'VALUES'}:
        return 'Selection / parameter'
    return 'Calculation'


def _flags(an, desc=''):
    """(severity, message) quality & performance findings."""
    c, f, names = an['nostr'], an['funcs'], set(an['funcs'])
    fl = []
    if re.search(r'FILTER\s*\(\s*ALL(?:SELECTED|EXCEPT)?\s*\(', c, re.I):
        fl.append(('High', 'FILTER(ALL(...)) scans a whole table row by row. If the test is on one column, use a plain filter inside CALCULATE (e.g. Table[Col] = value) or REMOVEFILTERS(Table[Col]).'))
    elif re.search(r"FILTER\s*\(\s*(?:'[^']+'|[A-Za-z_]\w*)\s*,", c, re.I) and 'CALCULATE' in names:
        fl.append(('Medium', 'FILTER over a whole table inside CALCULATE. A Boolean filter such as Table[Col] = value is faster.'))
    if re.search(r'COUNTROWS\s*\(\s*FILTER\s*\(', c, re.I):
        fl.append(('Medium', 'COUNTROWS(FILTER(...)) can be written as CALCULATE(COUNTROWS(table), condition), which is faster.'))
    for mt in re.finditer(r"\b(SUMX|AVERAGEX|MAXX|MINX|COUNTX)\s*\(\s*('(?:[^']|'')+'|[A-Za-z_]\w*)\s*,\s*(.*)", c, re.I):
        body = mt.group(3)
        if re.fullmatch(r"[\w'\s\[\]\*\+\-/\.\(\)]*", body[:80]) and not re.search(r'\b(CALCULATE|RELATED|FILTER)\b', body[:200], re.I) and '*' in body[:120]:
            fl.append(('Medium', f'Iterator over {mt.group(2).strip(chr(39))} multiplies columns row by row. Pre-compute that column in the Gold table and use SUM().'))
            break
    else:
        pass
    if names & {'SUMX', 'AVERAGEX', 'MAXX', 'MINX', 'COUNTX'} and not any('Iterator' in m for _, m in fl):
        fl.append(('Low', 'Uses an iterator (…X function). Fine on small tables, but check speed if the table is a large fact table.'))
    if re.search(r'(?<![\w\]\)])/(?!/)', re.sub(r'\bDIVIDE\b', '', c)) or re.search(r'[\]\)\w]\s*/\s*[\[\(\w]', c):
        if 'DIVIDE' not in names:
            fl.append(('Low', 'Uses "/" for division. DIVIDE(a, b) handles divide-by-zero safely.'))
    nested_if = len(re.findall(r'\bIF\s*\(', c, re.I))
    if nested_if >= 3:
        fl.append(('Medium', f'{nested_if} IF() calls. A SWITCH(TRUE(), …) is easier to read and maintain.'))
    if names & {'IFERROR', 'ISERROR'}:
        fl.append(('Medium', 'IFERROR/ISERROR hides data problems and can be slow. Fix the cause or use DIVIDE / COALESCE.'))
    if 'DISTINCTCOUNT' in names:
        fl.append(('Info', 'DISTINCTCOUNT is one of the most expensive aggregations on high-cardinality columns. Check the column in the Statistics tab.'))
    if 'FORMAT' in names:
        fl.append(('Low', 'FORMAT() returns text, which is slower and cannot be sorted or charted as a number. Prefer a format string on the measure.'))
    if 'EARLIER' in names or 'EARLIEST' in names:
        fl.append(('Low', 'EARLIER is legacy syntax. Use VAR instead.'))
    if names & {'LOOKUPVALUE', 'RELATED'}:
        fl.append(('Medium', 'RELATED/LOOKUPVALUE look up one row at a time. Bring the column into the table upstream (Gold layer join).'))
    if f.get('CALCULATE', 0) >= 3:
        fl.append(('Medium', f'{f["CALCULATE"]} CALCULATE calls in one measure. Split into helper measures or use VARs.'))
    if re.search(r'\bALL\s*\(\s*(?:\'[^\']+\'|[A-Za-z_]\w*)\s*\)', c, re.I) and not any(m.startswith('FILTER(ALL') for _, m in fl):
        fl.append(('Low', 'ALL(Table) clears filters on every column. If you only need one column, use ALL(Table[Column]) / REMOVEFILTERS(Table[Column]).'))
    if 'SUMMARIZE' in names and re.search(r'SUMMARIZE\s*\([^)]*,\s*"', c, re.I):
        fl.append(('Medium', 'SUMMARIZE with calculated columns is slow/unsafe. Use ADDCOLUMNS(SUMMARIZE(...), …).'))
    # repeated sub-expressions
    calls = re.findall(r'[A-Z][A-Z\.]+\([^()]{25,}\)', c.upper())
    rep = [k for k, v in Counter(calls).items() if v >= 2]
    if rep and 'VAR' not in c.upper():
        fl.append(('Medium', 'The same sub-expression appears more than once. Store it in a VAR to evaluate it only once.'))
    if names & _TIME:
        fl.append(('Info', 'Time intelligence needs a continuous Date table marked as "Date table". Build dim_date once in the Gold layer.'))
    if not fl:
        fl.append(('OK', 'No issues detected by the rule checks.'))
    order = {'High': 0, 'Medium': 1, 'Low': 2, 'Info': 3, 'OK': 4}
    return sorted(fl, key=lambda x: order[x[0]])


def _gold(an, pattern, flags):
    names = set(an['funcs'])
    msgs, where = [], 'Keep in semantic model'
    if names & {'RELATED', 'LOOKUPVALUE'}:
        msgs.append('Do the lookup as a join in the Gold table, then aggregate with a simple SUM.'); where = 'Keep, but move part upstream'
    if any('Pre-compute' in m for _, m in flags):
        msgs.append('Create the row-level result as a column in the Gold fact table, then use SUM().'); where = 'Keep, but move part upstream'
    if names & _TIME:
        msgs.append('Keep as a measure. Make sure Gold provides one clean dim_date (continuous dates, year/quarter/month columns).')
    if pattern == 'Simple aggregation' and not msgs:
        msgs.append('Good measure: keep as is. The Gold fact table should expose the column at the right grain.')
    if pattern == 'Ratio / percentage' and not msgs:
        msgs.append('Keep as a measure (ratios must be calculated after aggregation). Make sure numerator and denominator are additive columns in Gold.')
    if pattern == 'Conditional logic' and not msgs and '""' in an['nostr']:
        msgs.append('The IF/SWITCH returns text labels (bands/categories). Create that label as a column in the Gold dimension instead, so it can be used in slicers and axes.'); where = 'Keep, but move part upstream'
    if 'SUMMARIZE' in names or 'ADDCOLUMNS' in names:
        msgs.append('Heavy table logic: consider pre-building an aggregate table in Gold.'); where = 'Keep, but move part upstream'
    if not msgs:
        msgs.append('Keep as a measure: business logic that depends on user selection belongs in the semantic layer.')
    return where, ' '.join(msgs)


_CTXWORD = {'ALL': 'Removes filters (ALL)', 'ALLNOBLANKROW': 'Removes filters (ALL)', 'REMOVEFILTERS': 'Removes filters (REMOVEFILTERS)',
            'ALLEXCEPT': 'Removes all filters except some (ALLEXCEPT)', 'ALLSELECTED': 'Keeps only the user\'s selection (ALLSELECTED)',
            'KEEPFILTERS': 'Adds to existing filters (KEEPFILTERS)', 'USERELATIONSHIP': 'Uses an inactive relationship (USERELATIONSHIP)',
            'CROSSFILTER': 'Changes filter direction (CROSSFILTER)', 'TREATAS': 'Applies values as a filter (TREATAS)'}


def _filter_ctx(an):
    hits = sorted({_CTXWORD[f] for f in an['funcs'] if f in _CTXWORD})
    if an['funcs'].get('CALCULATE') and not hits:
        hits = ['Adds / overrides filters (CALCULATE)']
    return '; '.join(hits) if hits else 'None: follows the filters of the visual and slicers'



def _purpose(name, an, pattern, used_by):
    cols = an['cols']; first = cols[0] if cols else 'its inputs'
    deps = an['measures']
    tbl = ', '.join(an['tables']) if an['tables'] else 'other measures only'
    n = name
    if pattern == 'Simple aggregation':
        t = f'Base building block: adds up / counts {first} once, so every visual and every other measure uses the same definition of "{n}".'
    elif pattern == 'Ratio / percentage':
        lead = f'KPI: expresses {deps[0]} relative to {deps[1]}' if len(deps) >= 2 else 'KPI: a ratio, rate or percentage'
        if 'DIVIDE' in an['funcs']:
            t = lead + ', so reports can compare performance independent of size. DIVIDE avoids errors when the denominator is zero.'
        else:
            t = lead + '. Ratios must be calculated after totals are known, so they cannot be stored as a column.'
    elif pattern == 'Time intelligence':
        t = 'Compares or accumulates a value over time (year-to-date, same period last year, moving periods). Requires a continuous Date table.'
    elif pattern == 'Filtered / context-changing calculation':
        t = 'Answers a specific business question: the value for a defined subset of the data, or ignoring the filters users select.'
    elif pattern == 'Row-by-row calculation (iterator)':
        t = 'Needs a calculation on every row (for example quantity × price) before adding up, which a plain SUM cannot do.'
    elif pattern == 'Conditional logic':
        t = 'Applies a business rule: returns a label or value depending on a condition.'
    elif pattern == 'Combination of other measures':
        t = 'Combines existing measures into a new figure (for example difference or sum), re-using their definitions.'
    elif pattern == 'Ranking / Top N':
        t = 'Ranks or limits items (for example top customers).'
    elif pattern == 'Selection / parameter':
        t = 'Reads what the user selected (slicer / what-if) and feeds it into other calculations.'
    elif pattern == 'Text / formatting':
        t = 'Builds text for titles, labels or tooltips.'
    elif deps and cols:
        t = (f'Combines {", ".join(deps)} with values read directly from {", ".join(cols)}, typically to compare actual against '
             'another source (variance / difference).')
    else:
        t = 'Custom calculation combining several functions.'
    t += f' Reads from: {tbl}.'
    t += (f' Re-used by {len(used_by)} other measure(s): changing it changes them too.' if used_by
          else ' It is a final measure shown directly in reports (no other measure depends on it).')
    return t


# ====================================================================== public API
def explain_measures(measures_df, columns_stats=None):
    """Return (measures_explained_df, dependencies_df)."""
    if measures_df is None or measures_df.empty:
        return pd.DataFrame(), pd.DataFrame()
    names = set(measures_df['Name'].astype(str))
    rows = []
    for _, r in measures_df.iterrows():
        name, expr = str(r['Name']), str(r.get('Expression', '') or '')
        an = _analyse(expr, names, name)
        pat = _pattern(an)
        fl = _flags(an)
        where, gold = _gold(an, pat, fl)
        try:
            text = _tr(an['clean'])
        except Exception as ex:
            text = f'(could not translate: {ex})'
        text = text[:1].upper() + text[1:] if text else text
        rows.append({
            'Table': _s(r.get('TableName', '')), 'Measure': name, 'Display folder': _s(r.get('DisplayFolder', '')),
            'Description (in model)': _s(r.get('Description', '')), 'DAX formula': expr.strip(),
            'What it calculates (plain English)': text, 'Pattern': pat, 'Complexity': an['complexity'],
            'Uses columns': ', '.join(an['cols']), 'Uses tables': ', '.join(an['tables']),
            'Depends on measures': ', '.join(an['measures']), 'Filter-context changes': _filter_ctx(an),
            'Worst finding': fl[0][0], 'Findings': ' | '.join(f'[{s}] {m}' for s, m in fl if s != 'OK') or 'No issues detected.',
            'Gold layer: where it belongs': where, 'Gold layer advice': gold,
            '_flags': fl,
        })
    df = pd.DataFrame(rows)
    used_by = {n: [] for n in names}
    for _, r in df.iterrows():
        for d in [x for x in r['Depends on measures'].split(', ') if x]:
            used_by.setdefault(d, []).append(r['Measure'])
    deps = {r['Measure']: [x for x in r['Depends on measures'].split(', ') if x] for _, r in df.iterrows()}

    def level(n, seen=()):
        if n in seen or not deps.get(n):
            return 0
        return 1 + max(level(d, seen + (n,)) for d in deps[n])
    df['Used by measures'] = df['Measure'].map(lambda n: ', '.join(sorted(used_by.get(n, []))))
    df['Dependency level'] = df['Measure'].map(level)
    direct = {r['Measure']: [t for t in r['Uses tables'].split(', ') if t] for _, r in df.iterrows()}

    def all_tables(n, seen=()):
        if n in seen:
            return []
        out = list(direct.get(n, []))
        for d in deps.get(n, []):
            for t in all_tables(d, seen + (n,)):
                if t not in out:
                    out.append(t)
        return out
    df['Tables used (incl. via other measures)'] = df['Measure'].map(lambda n: ', '.join(all_tables(n)))

    def by_table(cols_text):
        g = {}
        for c in [x for x in cols_text.split(', ') if x]:
            g.setdefault(c.split('[', 1)[0], []).append(c.split('[', 1)[1].rstrip(']') if '[' in c else c)
        return '\n'.join(f'{t}: {", ".join(cs)}' for t, cs in g.items())
    df['Columns by table'] = df['Uses columns'].map(by_table)
    df['Why it is used (purpose)'] = df.apply(lambda r: _purpose(r['Measure'], {'cols': [x for x in r['Uses columns'].split(', ') if x],
        'measures': [x for x in r['Depends on measures'].split(', ') if x], 'tables': [x for x in r['Uses tables'].split(', ') if x],
        'funcs': Counter(re.findall(r'\b([A-Z]+)\s*\(', r['DAX formula'].upper()))}, r['Pattern'],
        [x for x in r['Used by measures'].split(', ') if x]), axis=1)
    df['Cross-table'] = df['Uses tables'].map(lambda t: 'Yes: needs a relationship between ' + t.replace(', ', ' and ') if len([x for x in t.split(', ') if x]) > 1 else 'No')
    dep_df = df[['Table', 'Measure', 'Dependency level', 'Depends on measures', 'Used by measures']].copy()
    dep_df['Role'] = dep_df.apply(lambda r: 'Base measure' if not r['Depends on measures'] else ('Final / top-level measure' if not r['Used by measures'] else 'Intermediate measure'), axis=1)
    return df, dep_df


def _col_size(stats, table, col):
    if stats is None or stats.empty or 'ColumnName' not in stats.columns:
        return ''
    s = stats[(stats['TableName'] == table) & (stats['ColumnName'] == col)]
    if s.empty:
        return ''
    r = s.iloc[0]
    parts = []
    if 'Cardinality' in s.columns:
        parts.append(f'{int(r["Cardinality"]):,} distinct values')
    if 'DataSize' in s.columns:
        parts.append(f'{int(r["DataSize"]) / 1024:,.0f} KB')
    return ', '.join(parts)


def explain_calc_columns(cc_df, measure_names=(), stats=None):
    if cc_df is None or cc_df.empty:
        return pd.DataFrame()
    rows = []
    for _, r in cc_df.iterrows():
        expr = str(r.get('Expression', '') or '')
        an = _analyse(expr, set(measure_names))
        try:
            text = _tr(an['clean'])
        except Exception:
            text = ''
        names = set(an['funcs'])
        why = ['Calculated columns are stored in the model, increase its size and are recalculated at every refresh.']
        if 'RELATED' in names:
            why.append('Uses RELATED: this is a join, so do it in the Gold table.')
        if names & {'IF', 'SWITCH'}:
            why.append('Conditional bucketing: do it with CASE WHEN in SQL/Spark.')
        if names & {'LEFT', 'RIGHT', 'MID', 'FORMAT', 'UPPER', 'LOWER', 'CONCATENATE', 'TRIM'} or '&' in an['clean']:
            why.append('Text manipulation is cheaper upstream.')
        if names & {'YEAR', 'MONTH', 'DAY', 'DATE', 'EOMONTH'}:
            why.append('Date parts belong in the Gold dim_date.')
        rows.append({'Table': r['TableName'], 'Column': r['ColumnName'], 'DAX formula': expr.strip(),
                     'What it calculates (plain English)': (text[:1].upper() + text[1:]) if text else '',
                     'Uses columns': ', '.join(an['cols']), 'Size in model': _col_size(stats, r['TableName'], r['ColumnName']),
                     'Gold layer: where it belongs': 'Move upstream (Silver/Gold)', 'Why / what to do': ' '.join(why)})
    return pd.DataFrame(rows)


def explain_calc_tables(ct_df):
    if ct_df is None or ct_df.empty:
        return pd.DataFrame()
    rows = []
    for _, r in ct_df.iterrows():
        expr = str(r.get('Expression', '') or '')
        an = _analyse(expr, set())
        names = set(an['funcs'])
        if names & {'CALENDAR', 'CALENDARAUTO'}:
            kind, adv, where = 'Date table', 'Create one shared dim_date in the Gold layer (SQL/Spark) and mark it as the Date table.', 'Move upstream (Silver/Gold)'
        elif 'DATATABLE' in names:
            kind, adv, where = 'Hard-coded lookup table', 'Store it as a real dimension/seed table in the lakehouse or warehouse.', 'Move upstream (Silver/Gold)'
        elif 'GENERATESERIES' in names:
            kind, adv, where = 'Number series (parameter / what-if)', 'Usually fine to keep in the model (it is a slicer helper).', 'Fine in semantic model'
        elif names & {'SUMMARIZE', 'SUMMARIZECOLUMNS', 'GROUPBY', 'ADDCOLUMNS'}:
            kind, adv, where = 'Aggregation table', 'Build this aggregate in the Gold layer so refresh and queries are faster.', 'Move upstream (Silver/Gold)'
        elif names & {'DISTINCT', 'VALUES', 'UNION', 'SELECTCOLUMNS'}:
            kind, adv, where = 'Dimension derived from other tables', 'Create a proper dimension table in Gold with its own key.', 'Move upstream (Silver/Gold)'
        else:
            kind, adv, where = 'Other calculated table', 'Review and, if it is data preparation, move it to the Gold layer.', 'Review'
        try:
            text = _tr(an['clean'])
        except Exception:
            text = ''
        rows.append({'Table': r['TableName'], 'Type': kind, 'DAX formula': expr.strip(),
                     'What it creates (plain English)': (text[:1].upper() + text[1:]) if text else '',
                     'Gold layer: where it belongs': where, 'Why / what to do': adv})
    return pd.DataFrame(rows)
