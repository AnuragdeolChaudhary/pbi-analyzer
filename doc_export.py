"""Build a professionally formatted Word (.docx) document from the Power Query explanation."""
import io
from datetime import datetime
import pandas as pd
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

NAVY, BLUE, GREY = '1F3864', '2E74B5', '595959'
FILL = {'Move upstream (Silver/Gold)': 'FCE4D6', 'Fine in semantic model': 'E2EFDA',
        'Not needed': 'EDEDED', 'Source': 'DDEBF7', 'Review': 'FFF2CC'}
_CONNECT = ('Connect to source', 'Navigate to object')
PAGE_W = 6.9  # usable inches (Letter, 0.8" margins)


# ------------------------------------------------------------------ low-level helpers
def _shade(cell, hex_fill):
    tcPr = cell._tc.get_or_add_tcPr()
    for old in tcPr.findall(qn('w:shd')):
        tcPr.remove(old)
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear'); shd.set(qn('w:color'), 'auto'); shd.set(qn('w:fill'), hex_fill)
    tcPr.append(shd)


def _cell_margins(table, top=50, bottom=50, left=90, right=90):
    tblPr = table._tbl.tblPr
    mar = OxmlElement('w:tblCellMar')
    for k, v in (('top', top), ('left', left), ('bottom', bottom), ('right', right)):
        e = OxmlElement(f'w:{k}'); e.set(qn('w:w'), str(v)); e.set(qn('w:type'), 'dxa'); mar.append(e)
    tblPr.append(mar)


def _borders(table, color='BFBFBF'):
    tblPr = table._tbl.tblPr
    b = OxmlElement('w:tblBorders')
    for k in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        e = OxmlElement(f'w:{k}')
        e.set(qn('w:val'), 'single'); e.set(qn('w:sz'), '4'); e.set(qn('w:space'), '0'); e.set(qn('w:color'), color)
        b.append(e)
    tblPr.append(b)


def _fix_widths(table, widths_in):
    """Fixed layout + dual widths (tblW, gridCol, tcW) so Word, LibreOffice and Google Docs agree."""
    table.autofit = False
    tblPr = table._tbl.tblPr
    lay = OxmlElement('w:tblLayout'); lay.set(qn('w:type'), 'fixed'); tblPr.append(lay)
    for old in tblPr.findall(qn('w:tblW')):
        tblPr.remove(old)
    w = OxmlElement('w:tblW'); w.set(qn('w:w'), str(int(sum(widths_in) * 1440))); w.set(qn('w:type'), 'dxa'); tblPr.append(w)
    grid = table._tbl.tblGrid
    for gc, wi in zip(grid.findall(qn('w:gridCol')), widths_in):
        gc.set(qn('w:w'), str(int(wi * 1440)))
    for row in table.rows:
        for cell, wi in zip(row.cells, widths_in):
            cell.width = Inches(wi)


def _row_flags(row, header=False, cant_split=True):
    trPr = row._tr.get_or_add_trPr()
    if cant_split:
        trPr.append(OxmlElement('w:cantSplit'))
    if header:
        trPr.append(OxmlElement('w:tblHeader'))


def _write(cell, text, bold=False, color=None, size=9, italic=False, font=None, align=None):
    cell.text = ''
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.space_before = Pt(0)
    if align:
        p.alignment = align
    r = p.add_run(str(text) if text is not None else '')
    r.bold, r.italic = bold, italic
    r.font.size = Pt(size)
    if font:
        r.font.name = font
    if color:
        r.font.color.rgb = RGBColor.from_string(color)
    return p


def _keep_next(cell):
    for p in cell.paragraphs:
        p.paragraph_format.keep_with_next = True


def _para(doc, text='', size=10, bold=False, italic=False, color=None, after=6, before=0, align=None, style=None):
    p = doc.add_paragraph(style=style)
    p.paragraph_format.space_after = Pt(after)
    p.paragraph_format.space_before = Pt(before)
    if align:
        p.alignment = align
    if text:
        r = p.add_run(text)
        r.bold, r.italic, r.font.size = bold, italic, Pt(size)
        if color:
            r.font.color.rgb = RGBColor.from_string(color)
    return p


def _grid(doc, widths, header, rows, size=8.5, header_fill=NAVY, zebra=True, fills=None):
    t = doc.add_table(rows=1, cols=len(widths))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    _borders(t); _cell_margins(t)
    for c, h in zip(t.rows[0].cells, header):
        _write(c, h, bold=True, color='FFFFFF', size=size); _shade(c, header_fill)
    _row_flags(t.rows[0], header=True)
    for i, row in enumerate(rows):
        cells = t.add_row().cells
        for j, (c, v) in enumerate(zip(cells, row)):
            _write(c, v, size=size)
            f = (fills or {}).get((i, j)) or ('F5F8FC' if zebra and i % 2 else None)
            if f:
                _shade(c, f)
        _row_flags(t.rows[-1])
    _fix_widths(t, widths)
    return t


def _field(paragraph, instr):
    r = paragraph.add_run()
    for kind, text in (('begin', None), (None, instr), ('end', None)):
        if kind:
            e = OxmlElement('w:fldChar'); e.set(qn('w:fldCharType'), kind)
        else:
            e = OxmlElement('w:instrText'); e.set(qn('xml:space'), 'preserve'); e.text = text
        r._r.append(e)
    r.font.size = Pt(8)


# ------------------------------------------------------------------ document sections
def _setup(doc, title):
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.PORTRAIT
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.left_margin = sec.right_margin = Inches(0.8)
    sec.top_margin, sec.bottom_margin = Inches(0.8), Inches(0.75)
    st = doc.styles['Normal']
    st.font.name = 'Calibri'; st.font.size = Pt(10)
    st.element.rPr.rFonts.set(qn('w:eastAsia'), 'Calibri')
    for name, size, color, before, after in (('Heading 1', 16, NAVY, 18, 6), ('Heading 2', 12.5, BLUE, 12, 4), ('Heading 3', 11, NAVY, 8, 3)):
        h = doc.styles[name]
        h.font.name, h.font.size, h.font.bold = 'Calibri', Pt(size), True
        h.font.color.rgb = RGBColor.from_string(color)
        h.element.rPr.rFonts.set(qn('w:ascii'), 'Calibri'); h.element.rPr.rFonts.set(qn('w:hAnsi'), 'Calibri')
        h.paragraph_format.space_before, h.paragraph_format.space_after = Pt(before), Pt(after)
        h.paragraph_format.keep_with_next = True
    hp = sec.header.paragraphs[0]
    r = hp.add_run(title); r.font.size = Pt(8); r.font.color.rgb = RGBColor.from_string(GREY)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = fp.add_run('Page '); r.font.size = Pt(8)
    _field(fp, 'PAGE')


def _cover(doc, file_name, n_tables, n_steps, n_measures=0):
    _para(doc, 'Power Query & DAX Documentation', size=24, bold=True, color=NAVY, after=2, before=30)
    p = _para(doc, f'Model: {file_name}', size=13, color=BLUE, after=2)
    _para(doc, f'Generated on {datetime.now():%d %B %Y}  ·  {n_tables} query table(s)  ·  {n_steps} transformation step(s)  ·  {n_measures} DAX measure(s)', size=9.5, color=GREY, after=12)
    pPr = p._p.get_or_add_pPr()
    bd = OxmlElement('w:pBdr'); b = OxmlElement('w:bottom')
    b.set(qn('w:val'), 'single'); b.set(qn('w:sz'), '8'); b.set(qn('w:space'), '4'); b.set(qn('w:color'), BLUE)
    bd.append(b); pPr.append(bd)


def _overview(doc, steps, sources):
    doc.add_heading('1. Overview', level=1)
    _para(doc, ('This document explains, in plain English, every Power Query (M) transformation in the model: where the data '
                'comes from, what each step does, how the table looked BEFORE the step and what it became AFTER it, and which '
                'steps should move into a Silver/Gold layer to build a lean semantic model.'), after=8)
    core = steps[~steps['Operation'].isin(_CONNECT)]
    up = steps[steps['Gold layer: where it belongs'] == 'Move upstream (Silver/Gold)']
    rows = []
    for t in steps['Table'].unique():
        s = steps[steps['Table'] == t]
        c = s[~s['Operation'].isin(_CONNECT)]
        src = sources[sources['Table'] == t].iloc[0]
        rows.append([t, src['Source type'], len(c),
                     int((s['Gold layer: where it belongs'] == 'Move upstream (Silver/Gold)').sum()),
                     int((s['Gold layer: where it belongs'] == 'Not needed').sum())])
    rows.append(['TOTAL', '', len(core), len(up), int((steps['Gold layer: where it belongs'] == 'Not needed').sum())])
    fills = {(len(rows) - 1, j): 'DDEBF7' for j in range(5)}
    _grid(doc, [1.7, 2.5, 0.9, 0.95, 0.85], ['Table', 'Source', 'Steps', 'Move upstream', 'No effect'], rows, fills=fills)
    _para(doc, '', after=2)
    doc.add_heading('How to read the step blocks', level=3)
    for label, txt in (('Before / After', 'What the affected part of the table looked like before the step and what it became.'),
                       ('Rows impact', 'Whether the step keeps, removes or multiplies rows. Exact row counts cannot be read from M code.'),
                       ('Columns', 'Column count before → after when the full column list can be traced from the M code.'),
                       ('Gold layer', 'Where the logic should live: move upstream (SQL/Spark), keep in the semantic model, or delete.')):
        p = _para(doc, after=2)
        r = p.add_run(f'{label}: '); r.bold, r.font.size = True, Pt(9.5)
        r = p.add_run(txt); r.font.size = Pt(9.5)


def _sources(doc, sources):
    doc.add_heading('2. Data sources', level=1)
    rows = [[r['Table'], r['Source type'], r['Source detail'] or '—', r['Object'] or '—'] for _, r in sources.iterrows()]
    _grid(doc, [1.3, 1.7, 2.7, 1.2], ['Table', 'Source type', 'Server / file / URL', 'Object'], rows)
    sql = sources[sources['Custom SQL'].astype(bool)]
    for _, r in sql.iterrows():
        doc.add_heading(f'Custom SQL used by {r["Table"]}', level=3)
        p = _para(doc, r['Custom SQL'], size=8, after=6)
        for run in p.runs:
            run.font.name = 'Consolas'
        _shade_par(p)


def _shade_par(p, fill='F2F2F2'):
    pPr = p._p.get_or_add_pPr()
    shd = OxmlElement('w:shd'); shd.set(qn('w:val'), 'clear'); shd.set(qn('w:color'), 'auto'); shd.set(qn('w:fill'), fill)
    pPr.append(shd)


def _todo(doc, steps):
    doc.add_heading('3. Gold layer action list', level=1)
    todo = steps[steps['Gold layer: where it belongs'] == 'Move upstream (Silver/Gold)']
    if todo.empty:
        _para(doc, 'No steps need to move upstream.'); return
    _para(doc, f'{len(todo)} step(s) are better done upstream (SQL / Spark / Fabric) than inside Power Query.', after=6).paragraph_format.keep_with_next = True
    rows = [[r['Table'], r['#'], r['Operation'], r['Why / what to do']] for _, r in todo.iterrows()]
    _grid(doc, [1.2, 0.35, 1.6, 3.75], ['Table', '#', 'Step type', 'What to do instead'], rows)
    nef = steps[steps['Gold layer: where it belongs'] == 'Not needed']
    if len(nef):
        doc.add_heading('Steps that can simply be deleted', level=3)
        _grid(doc, [1.2, 0.35, 1.6, 3.75], ['Table', '#', 'Step type', 'Reason'],
              [[r['Table'], r['#'], r['Operation'], r['Why / what to do']] for _, r in nef.iterrows()])


def _step_block(doc, r):
    is_src = r['Category'] == 'Source & connection'
    gold = r['Gold layer: where it belongs']
    items = [('What it does', r['What it does (plain English)'], {}),
             ('Why it was done', r['Why it was done (likely reason)'] or '—', {}),
             ('M code (this step)', r['M code'].strip(), {'mono': True, 'fill': 'F2F2F2'})]
    if not is_src:
        items += [('Before', r['Before this step'], {}), ('After', r['After this step'], {'fill': 'F3FAF0'}),
                  ('Rows impact', r['Rows impact'], {}), ('Columns', r['Columns'], {})]
        if r['Columns known after step']:
            items.append(('Columns now', r['Columns known after step'], {}))
    else:
        items.append(('Result', r['After this step'], {'fill': 'F3FAF0'}))
    items.append(('Gold layer', f'{gold}.  {r["Why / what to do"]}', {'fill': FILL.get(gold, 'FFFFFF')}))
    if r['Query folding']:
        items.append(('Query folding', r['Query folding'] + ': later steps may run inside Power BI instead of the source database.', {}))
    _kv_block(doc, f'Step {r["#"]}  ·  {r["Step name"]}  ·  {r["Operation"]}', items)


def _table_section(doc, idx, table, steps, summary, final_schema):
    doc.add_heading(f'{idx}. Table: {table}', level=1)
    doc.add_heading('Summary', level=2)
    _para(doc, summary, after=6)
    s = steps[steps['Table'] == table]
    doc.add_heading('Transformation timeline', level=2)
    rows = [[r['#'], r['Step name'], r['Operation'], r['Rows impact'], r['Columns']] for _, r in s.iterrows()]
    fills = {}
    for i, (_, r) in enumerate(s.iterrows()):
        if 'MUCH fewer' in r['Rows impact'] or 'MORE rows' in r['Rows impact']:
            fills[(i, 3)] = 'FFF2CC'
    _grid(doc, [0.35, 1.7, 1.45, 2.2, 1.2], ['#', 'Step name', 'Operation', 'Rows impact', 'Columns'], rows, size=8, fills=fills)
    _para(doc, 'Highlighted rows impact = the table granularity (what one row represents) changes at that step.', size=8, italic=True, color=GREY, after=4)
    doc.add_heading('Step-by-step: before and after', level=2)
    for _, r in s.iterrows():
        _step_block(doc, r)
    if final_schema is not None:
        fs = final_schema[final_schema['Table'] == table]
        if len(fs):
            doc.add_heading('Final table in the semantic model', level=2)
            _para(doc, f'{len(fs)} column(s) are loaded into the model after all steps above.', after=4)
            _grid(doc, [0.4, 3.6, 2.9], ['#', 'Column', 'Data type'],
                  [[i + 1, c, t] for i, (c, t) in enumerate(zip(fs['Column'], fs['Type']))], size=8.5)


def _appendix(doc, power_query, tables):
    doc.add_page_break()
    doc.add_heading('Appendix: Original M code', level=1)
    for t in tables:
        code = power_query.loc[power_query['TableName'] == t, 'Expression']
        if code.empty:
            continue
        doc.add_heading(t, level=3)
        for line in str(code.iloc[0]).splitlines() or ['']:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0); p.paragraph_format.space_before = Pt(0)
            r = p.add_run(line.replace('\t', '    ') or ' ')
            r.font.name, r.font.size = 'Consolas', Pt(7.5)
            _shade_par(p)


def _notes(doc):
    doc.add_heading('Notes and limitations', level=1)
    for txt in ('DAX explanations are rule-based translations: complex or unusual formulas may read awkwardly, so always compare with the formula shown. Measure usage in report visuals is not checked.',
                'The explanation is generated automatically from the M code (rule-based). Unusual or custom M functions are reported as "Other" and should be reviewed manually.',
                'Row counts are not stored in M code, so row effects are described qualitatively (fewer / more / same).',
                'Column lists are traced from the M code. When the source columns are not named in the code, the column count shows as "not traceable" until a step (such as Keep columns or Group by) defines the full list.',
                'Query-folding flags are indicative: always confirm with "View Native Query" in Power Query.'):
        p = doc.add_paragraph(style='List Bullet'); r = p.add_run(txt); r.font.size = Pt(9.5)



# ------------------------------------------------------------------ DAX sections
SEV_FILL = {'High': 'F8CBAD', 'Medium': 'FFE699', 'Low': 'FFF2CC', 'Info': 'DDEBF7', 'OK': 'E2EFDA'}


def _kv_block(doc, title, items, header_fill=NAVY):
    """items: list of (label, text, opts) where opts may hold fill / mono."""
    t = doc.add_table(rows=1, cols=2)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    _borders(t); _cell_margins(t)
    h = t.rows[0].cells[0].merge(t.rows[0].cells[1])
    _write(h, title, bold=True, color='FFFFFF', size=9.5); _shade(h, header_fill); _keep_next(h); _row_flags(t.rows[0], cant_split=False)
    for label, val, opts in items:
        cells = t.add_row().cells
        _write(cells[0], label, bold=True, size=8.5, color=NAVY); _shade(cells[0], 'EAF0F8')
        _write(cells[1], val, size=8 if opts.get('mono') else 9, font='Consolas' if opts.get('mono') else None)
        if opts.get('fill'):
            _shade(cells[1], opts['fill'])
        _row_flags(t.rows[-1], cant_split=not opts.get('mono'))
        _keep_next(cells[0])
    _fix_widths(t, [1.25, PAGE_W - 1.25])
    _para(doc, '', after=4)


def _dax_section(doc, idx, dax):
    ms = dax.get('measures')
    if ms is None or ms.empty:
        return idx
    doc.add_page_break()
    doc.add_heading(f'{idx}. DAX measures', level=1)
    _para(doc, (f'The model contains {len(ms)} DAX measure(s). Each one is explained in plain English below, together with '
                'the columns it uses, the measures it depends on, how it changes the filter context, quality / performance findings '
                'and where the logic should live in a Gold-layer design.'), after=8)
    # overview: pattern + findings
    pc = ms['Pattern'].value_counts()
    _grid(doc, [4.9, 2.0], ['Pattern', 'Measures'], [[k, int(v)] for k, v in pc.items()], size=8.5)
    _para(doc, '', after=2)
    sev = ms['Worst finding'].value_counts()
    _grid(doc, [4.9, 2.0], ['Worst finding per measure', 'Measures'],
          [[k, int(sev.get(k, 0))] for k in ('High', 'Medium', 'Low', 'Info', 'OK')], size=8.5,
          fills={(i, 0): SEV_FILL[k] for i, k in enumerate(('High', 'Medium', 'Low', 'Info', 'OK'))}, zebra=False)

    doc.add_heading('Measure catalogue', level=2)
    rows = [[r['Measure'], r['Table'], r['Pattern'], r['Complexity'], r['Worst finding'],
             r['Depends on measures'] or '—'] for _, r in ms.iterrows()]
    fills = {(i, 4): SEV_FILL.get(r['Worst finding']) for i, (_, r) in enumerate(ms.iterrows())}
    _grid(doc, [1.45, 0.9, 1.65, 0.8, 0.7, 1.4], ['Measure', 'Table', 'Pattern', 'Complexity', 'Findings', 'Depends on'], rows, size=8, fills=fills)

    deps = dax.get('dependencies')
    if deps is not None and len(deps) and (deps['Depends on measures'].astype(bool).any()):
        doc.add_heading('Dependency map', level=2)
        _para(doc, 'Level 0 = base measure (uses columns only). A higher level means the measure is built on other measures.', size=9, italic=True, color=GREY, after=4).paragraph_format.keep_with_next = True
        d = deps.sort_values(['Dependency level', 'Measure'], ascending=[False, True])
        _grid(doc, [1.5, 0.6, 1.3, 1.75, 1.75], ['Measure', 'Level', 'Role', 'Depends on', 'Used by'],
              [[r['Measure'], r['Dependency level'], r['Role'], r['Depends on measures'] or '—', r['Used by measures'] or '—'] for _, r in d.iterrows()], size=8)

    top = [(r['Measure'], sv, m) for _, r in ms.iterrows() for sv, m in r['_flags'] if sv in ('High', 'Medium')]
    if top:
        doc.add_heading('Findings to fix first', level=2)
        _grid(doc, [1.5, 0.7, 4.7], ['Measure', 'Severity', 'Finding'], [[a, b, c] for a, b, c in top], size=8,
              fills={(i, 1): SEV_FILL[b] for i, (a, b, c) in enumerate(top)}, zebra=False)

    doc.add_heading('Measure details', level=2)
    for _, r in ms.iterrows():
        fl = '\n'.join(f'[{sv}] {m}' for sv, m in r['_flags'])
        tabs_txt = (f"Directly: {r['Uses tables'] or '— (other measures only)'}\nIncluding via other measures: {r['Tables used (incl. via other measures)'] or '—'}")
        items = [('DAX formula', r['DAX formula'], {'mono': True, 'fill': 'F2F2F2'}),
                 ('In plain English', r['What it calculates (plain English)'], {}),
                 ('Why it is used', r['Why it is used (purpose)'], {'fill': 'FFFBEA'}),
                 ('Tables used', tabs_txt, {}),
                 ('Columns by table', r['Columns by table'] or '—', {}),
                 ('Cross-table?', r['Cross-table'], {}),
                 ('Pattern', f"{r['Pattern']}  ·  Complexity: {r['Complexity']}", {}),
                 ('Depends on', r['Depends on measures'] or '— (base measure)', {}),
                 ('Used by', r['Used by measures'] or '— (not used by other measures)', {}),
                 ('Filter context', r['Filter-context changes'], {}),
                 ('Findings', fl, {'fill': SEV_FILL.get(r['Worst finding'])}),
                 ('Gold layer', f"{r['Gold layer: where it belongs']}.  {r['Gold layer advice']}",
                  {'fill': 'FCE4D6' if r['Gold layer: where it belongs'] != 'Keep in semantic model' else 'E2EFDA'})]
        if r['Description (in model)']:
            items.insert(2, ('Description in model', r['Description (in model)'], {}))
        folder = f"  ·  folder: {r['Display folder']}" if r['Display folder'] else ''
        _kv_block(doc, f"{r['Measure']}  ·  table: {r['Table']}{folder}", items)
    return idx + 1


def _calc_sections(doc, idx, dax):
    cc, ct = dax.get('calc_columns'), dax.get('calc_tables')
    if cc is not None and len(cc):
        doc.add_page_break()
        doc.add_heading(f'{idx}. Calculated columns', level=1)
        _para(doc, (f'{len(cc)} calculated column(s) are computed inside the model. Each is stored in memory and recalculated at every '
                    'refresh, so they are the first candidates to move into the Gold layer.'), after=6)
        for _, r in cc.iterrows():
            items = [('DAX formula', r['DAX formula'], {'mono': True, 'fill': 'F2F2F2'}),
                     ('In plain English', r['What it calculates (plain English)'], {}),
                     ('Uses columns', r['Uses columns'] or '—', {})]
            if r['Size in model']:
                items.append(('Size in model', r['Size in model'], {}))
            items.append(('Gold layer', f"{r['Gold layer: where it belongs']}.  {r['Why / what to do']}", {'fill': 'FCE4D6'}))
            _kv_block(doc, f"{r['Table']}[{r['Column']}]", items)
        idx += 1
    if ct is not None and len(ct):
        doc.add_heading(f'{idx}. Calculated tables', level=1)
        for _, r in ct.iterrows():
            items = [('DAX formula', r['DAX formula'], {'mono': True, 'fill': 'F2F2F2'}),
                     ('In plain English', r['What it creates (plain English)'], {}),
                     ('Type', r['Type'], {}),
                     ('Gold layer', f"{r['Gold layer: where it belongs']}.  {r['Why / what to do']}",
                      {'fill': 'FCE4D6' if r['Gold layer: where it belongs'].startswith('Move') else 'E2EFDA'})]
            _kv_block(doc, r['Table'], items)
        idx += 1
    return idx



# ------------------------------------------------------------------ Fabric Gold blueprint
def _code_block(doc, text, size=7.5):
    for line in str(text).splitlines() or ['']:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0); p.paragraph_format.space_before = Pt(0)
        r = p.add_run(line.replace('\\t', '    ') or ' ')
        r.font.name, r.font.size = 'Consolas', Pt(size)
        _shade_par(p)


def _gold_section(doc, idx, gold):
    if not gold:
        return idx
    tp, mp, cp = gold['tables'], gold['measures'], gold['calc_columns']
    doc.add_page_break()
    doc.add_heading(f'{idx}. Fabric Gold layer blueprint', level=1)
    _para(doc, ('Target design: ALL data transformations and row-level business logic live in the Fabric Gold layer (Lakehouse/Warehouse). '
                'The Power BI semantic model then contains only a small star schema and simple DAX.'), after=4)
    for txt in ('Power Query steps become Gold-layer code (draft PySpark is generated below).',
                'Calculated columns and row-by-row DAX become real columns in the Gold tables.',
                'Only simple measures stay in DAX: aggregations, ratios, standard time intelligence and "ignore filter" patterns that depend on user selections.',
                'Helper, lookup and unused tables are removed from the model: fewer tables, smaller model, faster refresh.'):
        p = doc.add_paragraph(style='List Bullet'); r = p.add_run(txt); r.font.size = Pt(9.5)

    doc.add_heading('Target model: which tables stay', level=2)
    keep = int(tp['In Fabric semantic model'].isin(['Keep', 'Review', 'Merge into parent']).sum())
    _para(doc, f'{len(tp)} table(s) in the model today → about {keep} after the Gold layer (Keep/Review). '
               f'{len(tp) - keep} table(s) are removed or merged.', after=4).paragraph_format.keep_with_next = True
    fill = {'Keep': 'E2EFDA', 'Review': 'FFF2CC', 'Remove from model': 'F8CBAD', 'Candidate to remove': 'F8CBAD', 'Merge into parent': 'FCE4D6'}
    rows = [[r['Table'], r['Role'], r['In Fabric semantic model'], r['Gold object'], r['Why']] for _, r in tp.iterrows()]
    _grid(doc, [1.05, 1.1, 1.0, 1.2, 2.55], ['Table', 'Role', 'In model?', 'Gold object', 'Why'], rows, size=8,
          fills={(i, 2): fill.get(r['In Fabric semantic model']) for i, (_, r) in enumerate(tp.iterrows())}, zebra=False)

    if mp is not None and len(mp):
        doc.add_heading('Measures: what stays in Power BI and what moves to Gold', level=2)
        stay = int(mp['Gold layer action'].str.startswith(('Keep', 'Simplify')).sum())
        _para(doc, f'{stay} of {len(mp)} measure(s) stay as simple DAX; {len(mp) - stay} need Gold-layer work.', after=4).paragraph_format.keep_with_next = True
        afill = {'Keep': 'E2EFDA', 'Move': 'FCE4D6', 'Simplify': 'FFF2CC', 'Redesign': 'F8CBAD', 'Review': 'F8CBAD'}
        rows = [[r['Measure'], r['Gold layer action'], r['Simple DAX in Power BI'], r['Effort']] for _, r in mp.iterrows()]
        _grid(doc, [1.35, 1.65, 3.2, 0.7], ['Measure', 'Gold layer action', 'Simple DAX left in Power BI', 'Effort'], rows, size=8,
              fills={(i, 1): afill.get(r['Gold layer action'].split(' ')[0]) for i, (_, r) in enumerate(mp.iterrows())}, zebra=False)
        mv = mp[mp['Gold layer action'] != 'Keep as is']
        if len(mv):
            doc.add_heading('Gold-layer work per measure', level=3)
            for _, r in mv.iterrows():
                items = [('Current DAX', gold['formulas'].get(r['Measure'], ''), {'mono': True, 'fill': 'F2F2F2'}),
                         ('Gold layer work', r['Gold layer work'], {}),
                         ('Simple DAX after', r['Simple DAX in Power BI'], {'mono': True, 'fill': 'E2EFDA'})]
                if r['Gold code (PySpark)']:
                    items.append(('Gold code (PySpark)', r['Gold code (PySpark)'], {'mono': True, 'fill': 'F2F2F2'}))
                if r['Risk / check']:
                    items.append(('Check', r['Risk / check'], {'fill': 'FFF2CC'}))
                _kv_block(doc, f"{r['Measure']}  ·  {r['Gold layer action']}  ·  effort: {r['Effort']}", items)

    if cp is not None and len(cp):
        doc.add_heading('Calculated columns → Gold columns', level=2)
        for _, r in cp.iterrows():
            _kv_block(doc, f"{r['Table']}[{r['Column']}]  ·  {r['Status']}",
                      [('Current DAX', r['DAX formula'], {'mono': True, 'fill': 'F2F2F2'}),
                       ('Gold code (PySpark)', r['Gold code (PySpark)'], {'mono': True, 'fill': 'F2F2F2'}),
                       ('Joins needed', r['Joins needed'], {})])

    doc.add_heading('Draft PySpark per table (Fabric notebook)', level=2)
    _para(doc, 'Generated from the Power Query steps. Review before running: replace Bronze table names with your own and compare row counts with the original report.', size=9, italic=True, color=GREY, after=4)
    st = gold.get('stats')
    if st is not None and len(st):
        _grid(doc, [2.5, 2.2, 2.2], ['Table', 'Steps translated automatically', 'Steps needing manual work'],
              [[r['Table'], r['Steps translated automatically'], r['Steps needing manual work']] for _, r in st.iterrows()], size=8.5)
        _para(doc, '', after=2)
    for t, code in gold['code'].items():
        doc.add_heading(t, level=3)
        _code_block(doc, code)
    return idx + 1


# ------------------------------------------------------------------ public API
def build_docx(file_name, steps, sources, summaries, power_query, final_schema=None, include_m_code=True, dax=None, gold=None) -> bytes:
    doc = Document()
    for z in doc.settings.element.findall(qn('w:zoom')):
        z.set(qn('w:percent'), '100')
    _setup(doc, f'Model documentation · {file_name}')
    has_pq = steps is not None and len(steps) > 0
    dax = dax or {}
    ms = dax.get('measures')
    n_meas = 0 if ms is None else len(ms)
    tables = list(steps['Table'].unique()) if has_pq else []
    n_core = int((~steps['Operation'].isin(_CONNECT)).sum()) if has_pq else 0
    _cover(doc, file_name, len(tables), n_core, n_meas)
    if has_pq:
        _overview(doc, steps, sources)
        _sources(doc, sources)
        _todo(doc, steps)
        for i, t in enumerate(tables, 4):
            doc.add_page_break()
            summ = summaries.loc[summaries['Table'] == t, 'Summary'].iloc[0]
            _table_section(doc, i, t, steps, summ, final_schema)
    else:
        doc.add_heading('Power Query', level=1)
        _para(doc, 'No Power Query (M) transformations were found in this file.')
    nxt = 4 + len(tables)
    nxt = _dax_section(doc, nxt, dax)
    nxt = _calc_sections(doc, nxt, dax)
    nxt = _gold_section(doc, nxt, gold)
    if has_pq and include_m_code:
        _appendix(doc, power_query, tables)
    _notes(doc)
    buf = io.BytesIO(); doc.save(buf)
    return buf.getvalue()
