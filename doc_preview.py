"""Markdown version of the documentation, shown inside the app (so no download is needed to read it)."""
import pandas as pd


def _cell(v, n=260):
    t = str(v if v is not None else '').replace('|', '\\|').replace('\r', ' ').replace('\n', '; ')
    return t if len(t) <= n else t[:n - 1] + '…'


def _table(df, cols, n=260):
    if df is None or len(df) == 0:
        return '_(none)_\n'
    out = ['| ' + ' | '.join(cols) + ' |', '|' + '---|' * len(cols)]
    for _, r in df.iterrows():
        out.append('| ' + ' | '.join(_cell(r[c], n) for c in cols) + ' |')
    return '\n'.join(out) + '\n'


def build_sections(steps, sources, summaries, dax, gold=None):
    """Return [(title, markdown)] mirroring the Word document."""
    sec = []
    if steps is not None and len(steps):
        up = steps[steps['Gold layer: where it belongs'] == 'Move upstream (Silver/Gold)']
        sec.append(('1. Data sources', _table(sources, ['Table', 'Source type', 'Source detail', 'Object', 'Custom SQL'], 200)))
        sec.append((f'2. Gold layer action list ({len(up)} steps to move upstream)',
                    _table(up, ['Table', '#', 'Operation', 'Why / what to do'])))
        for _, sr in summaries.iterrows():
            t = sr['Table']
            s = steps[steps['Table'] == t]
            md = [f'**Summary:** {sr["Summary"]}\n', '**Timeline**\n',
                  _table(s, ['#', 'Step name', 'Operation', 'Rows impact', 'Columns']), '\n**Step by step: before and after**\n']
            for _, r in s.iterrows():
                md.append(f'**Step {r["#"]} · {r["Step name"]} · {r["Operation"]}**\n')
                md.append(f'- **What it does:** {r["What it does (plain English)"]}')
                md.append(f'- **Why it was done:** {r["Why it was done (likely reason)"]}')
                md.append(f'\n```\n{r["M code"].strip()}\n```\n')
                if r['Category'] != 'Source & connection':
                    md.append(f'- **Before:** {r["Before this step"]}')
                    md.append(f'- **After:** {r["After this step"]}')
                    md.append(f'- **Rows impact:** {r["Rows impact"]}  ·  **Columns:** {r["Columns"]}')
                md.append(f'- **Gold layer:** {r["Gold layer: where it belongs"]}. {r["Why / what to do"]}')
                if r['Query folding']:
                    md.append(f'- **Query folding:** {r["Query folding"]}')
                md.append('')
            sec.append((f'Power Query · {t}', '\n'.join(md)))
    ms = (dax or {}).get('measures')
    if ms is not None and len(ms):
        sec.append((f'DAX measures overview ({len(ms)})',
                    _table(ms, ['Measure', 'Table', 'Pattern', 'Complexity', 'Worst finding', 'Depends on measures'])))
        for tname, grp in ms.groupby('Table', sort=False):
            md = []
            for _, r in grp.iterrows():
                md.append(f'#### {r["Measure"]}')
                md.append(f'```\n{r["DAX formula"]}\n```')
                md.append(f'- **In plain English:** {r["What it calculates (plain English)"].replace(chr(10), "  \n  ")}')
                md.append(f'- **Pattern:** {r["Pattern"]} · **Complexity:** {r["Complexity"]}')
                md.append(f'- **Why it is used:** {r["Why it is used (purpose)"]}')
                md.append(f'- **Tables used:** directly {r["Uses tables"] or "— (other measures only)"}; including via other measures: {r["Tables used (incl. via other measures)"] or "—"}')
                md.append(f'- **Columns by table:** {r["Columns by table"].replace(chr(10), " · ") or "—"}  ·  **Cross-table:** {r["Cross-table"]}')
                md.append(f'- **Depends on:** {r["Depends on measures"] or "— (base measure)"}  ·  **Used by:** {r["Used by measures"] or "—"}')
                md.append(f'- **Filter context:** {r["Filter-context changes"]}')
                md.append('- **Findings:**')
                for sv, m in r['_flags']:
                    md.append(f'    - `{sv}` {m}')
                md.append(f'- **Gold layer:** {r["Gold layer: where it belongs"]}. {r["Gold layer advice"]}\n')
            sec.append((f'DAX measures · {tname}', '\n'.join(md)))
    cc = (dax or {}).get('calc_columns')
    if cc is not None and len(cc):
        md = []
        for _, r in cc.iterrows():
            md.append(f'#### {r["Table"]}[{r["Column"]}]')
            md.append(f'```\n{r["DAX formula"]}\n```')
            md.append(f'- **In plain English:** {r["What it calculates (plain English)"]}')
            if r['Size in model']:
                md.append(f'- **Size in model:** {r["Size in model"]}')
            md.append(f'- **Gold layer:** {r["Gold layer: where it belongs"]}. {r["Why / what to do"]}\n')
        sec.append((f'Calculated columns ({len(cc)})', '\n'.join(md)))
    ct = (dax or {}).get('calc_tables')
    if ct is not None and len(ct):
        md = []
        for _, r in ct.iterrows():
            md.append(f'#### {r["Table"]} ({r["Type"]})')
            md.append(f'```\n{r["DAX formula"]}\n```')
            md.append(f'- **In plain English:** {r["What it creates (plain English)"]}')
            md.append(f'- **Gold layer:** {r["Gold layer: where it belongs"]}. {r["Why / what to do"]}\n')
        sec.append((f'Calculated tables ({len(ct)})', '\n'.join(md)))
    if gold:
        tp, mp, cp = gold['tables'], gold['measures'], gold['calc_columns']
        keep = int(tp['In Fabric semantic model'].isin(['Keep', 'Review', 'Merge into parent']).sum())
        sec.append((f'Fabric Gold blueprint · target model ({len(tp)} tables today → about {keep})',
                    _table(tp, ['Table', 'Role', 'In Fabric semantic model', 'Gold object', 'Why'])))
        if mp is not None and len(mp):
            md = [_table(mp, ['Measure', 'Gold layer action', 'Simple DAX in Power BI', 'Effort']), '']
            for _, r in mp[mp['Gold layer action'] != 'Keep as is'].iterrows():
                md.append(f'#### {r["Measure"]} · {r["Gold layer action"]}')
                md.append(f'- **Gold layer work:** {r["Gold layer work"]}')
                md.append(f'- **Simple DAX after:** `{r["Simple DAX in Power BI"]}`')
                if r['Gold code (PySpark)']:
                    md.append(f'\n```python\n{r["Gold code (PySpark)"]}\n```\n')
                if r['Risk / check']:
                    md.append(f'- **Check:** {r["Risk / check"]}')
            sec.append(('Fabric Gold blueprint · measures', '\n'.join(md)))
        if cp is not None and len(cp):
            md = []
            for _, r in cp.iterrows():
                md.append(f'#### {r["Table"]}[{r["Column"]}] · {r["Status"]}')
                md.append(f'```python\n{r["Gold code (PySpark)"]}\n```')
            sec.append(('Fabric Gold blueprint · calculated columns', '\n'.join(md)))
        for t, code in gold['code'].items():
            sec.append((f'Fabric Gold blueprint · PySpark · {t}', f'```python\n{code}\n```'))
    return sec
