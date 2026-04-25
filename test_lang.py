LANG_LEVEL_OPTIONS = ['native', 'C2', 'C1', 'B2', 'B1', 'A2', 'A1']


def _ensure_language_rows(rows):
    normed = []
    for r in rows or []:
        if isinstance(r, dict) and (r.get('lang') or r.get('level')):
            lvl_raw = (r.get('level') or 'B2').strip()
            lvl = 'native' if lvl_raw.lower() == 'native' else lvl_raw.upper()
            if lvl != 'native' and lvl not in ('C2', 'C1', 'B2', 'B1', 'A2', 'A1'):
                lvl = 'B2'
            normed.append({'lang': (r.get('lang') or '').lower(), 'level': lvl})
    if not normed:
        normed.append({'lang': '', 'level': 'B2'})
    return normed


test_input = [
    {'lang': 'fr', 'level': 'native'},
    {'lang': 'en', 'level': 'C1'},
    {'lang': 'pt', 'level': 'B2'},
    {'lang': 'es', 'level': 'B2'},
]
result = _ensure_language_rows(test_input)
for r in result:
    print(r, '-> valid in selectbox?', r['level'] in LANG_LEVEL_OPTIONS)
