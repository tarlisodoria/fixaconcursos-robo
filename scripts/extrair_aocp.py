"""Extrai questões de cadernos AOCP (2 colunas) para JSON."""
import pdfplumber, re, json, sys, unicodedata
from pathlib import Path

SECOES = [
 (r'^L[íi]ngua Portuguesa$', 'PT'),
 (r'^Legisla[çc][ãa]o', 'LE'),
 (r'^Racioc[íi]nio L[óo]gico', 'RL'),
 (r'^No[çc][õo]es de Gest[ãa]o', 'GP'),
 (r'^Gest[ãa]o P[úu]blica', 'GP'),
 (r'Inform[áa]tica', 'INF'),
 (r'^Conhecimentos Espec[íi]ficos', 'TI'),
 (r'^Conhecimentos Gerais', None),
]

def bold(fn): return 'Bold' in fn

def linhas_coluna(page, x0, x1, top, bottom):
    crop = page.crop((x0, top, x1, bottom))
    ws = crop.extract_words(extra_attrs=['fontname', 'size'], keep_blank_chars=False, x_tolerance=1.5)
    ul = [r for r in page.rects if (r['bottom'] - r['top']) < 2 and (r['x1'] - r['x0']) > 3]
    for w in ws:
        w['u'] = any(r['top'] >= w['bottom'] - 2 and r['top'] <= w['bottom'] + 3 and r['x0'] < w['x1'] - 1 and r['x1'] > w['x0'] + 1 for r in ul)
    ws.sort(key=lambda w: (round(w['top']), w['x0']))
    linhas, atual, ytop = [], [], None
    for w in ws:
        if ytop is None or abs(w['top'] - ytop) > 3:
            if atual: linhas.append(atual)
            atual, ytop = [w], w['top']
        else:
            atual.append(w)
    if atual: linhas.append(atual)
    out = []
    for ln in linhas:
        ln.sort(key=lambda w: w['x0'])
        out.append({'words': ln, 'x0': ln[0]['x0'], 'top': ln[0]['top']})
    return out

def render(ln, enunciado_bold_ok=False):
    """Texto da linha; marca **negrito** só quando a linha mistura negrito e normal."""
    ws = ln['words']
    allb = all(bold(w['fontname']) for w in ws)
    def dest(w): return w.get('u') or (bold(w['fontname']) and not allb)
    if not any(dest(w) for w in ws):
        return ' '.join(w['text'] for w in ws), allb
    parts, inb = [], False
    for w in ws:
        b = dest(w)
        t = w['text']
        if b and not inb: t = '**' + t; inb = True
        elif not b and inb: parts[-1] += '**'; inb = False
        parts.append(t)
    if inb: parts[-1] += '**'
    return ' '.join(parts), False

def extrair(pdf):
    p = pdfplumber.open(pdf)
    seq = []  # (tipo, texto, meta)
    for i, pg in enumerate(p.pages):
        if i == 0: continue
        W, H = pg.width, pg.height
        top, bottom = 50, H - 60
        txt_pg = pg.extract_text() or ''
        if 'RASCUNHO' in txt_pg.upper() and len(txt_pg) < 300: continue
        ws_all = [w for w in pg.extract_words() if top < w['top'] < bottom]
        best, split = None, W / 2
        for x in range(int(W * 0.40), int(W * 0.60), 2):
            c = sum(1 for w in ws_all if w['x0'] < x < w['x1'])
            if best is None or c < best: best, split = c, x
        cols = [(0, W)] if best > 3 else [(0, split), (split, W)]
        for (a, b) in cols:
            for ln in linhas_coluna(pg, a, b, top, bottom):
                t, allb = render(ln)
                w0 = ln['words'][0]
                isnum = (len(ln['words']) == 1 and re.fullmatch(r'\d{1,2}', t) and bold(w0['fontname']) and w0['size'] > 10.5)
                seq.append(('num' if isnum else 'txt', t, {'bold': allb, 'size': w0['size']}))
    return seq

def normal_sec(t):
    for rx, cod in SECOES:
        if re.search(rx, t.strip(), re.I): return cod, True
    return None, False

def parse(seq, total):
    questoes, sec, buf_texto = {}, None, []
    cur, esperado, texto_base = None, 1, None
    def fecha():
        nonlocal cur
        if cur:
            questoes[cur['numero']] = cur
        cur = None
    for tipo, t, meta in seq:
        if tipo == 'txt':
            cod, ok = normal_sec(t)
            if meta['bold'] and meta['size'] > 13 and len(t) < 70:
                if not ok: cod = t
                if cod: sec = cod
                fecha(); buf_texto = []; continue
        if tipo == 'num' and int(t) == esperado:
            fecha()
            if buf_texto and sum(len(x) for x in buf_texto) > 250:
                texto_base = '\n'.join(buf_texto).strip()
            buf_texto = []
            cur = {'numero': int(t), 'disciplina': sec, 'texto_base': texto_base if sec == 'PT' else None,
                   'enunciado': [], 'alternativas': {}, '_alt': None}
            esperado += 1
            continue
        if cur is None:
            buf_texto.append(t); continue
        m = re.match(r'^\(([A-E])\)\s*(.*)$', t)
        if m:
            cur['_alt'] = m.group(1); cur['alternativas'][m.group(1)] = m.group(2); continue
        if cur['_alt']:
            # depois da (E), texto não-negrito longo pode ser novo texto-base
            if cur['_alt'] == 'E' and meta['bold'] and not t.startswith('('):
                fecha(); buf_texto = [t]; continue
            if cur['_alt'] == 'E' and len(cur['alternativas']['E']) > 0 and re.match(r'^(Texto|Leia|TEXTO)', t):
                fecha(); buf_texto = [t]; continue
            cur['alternativas'][cur['_alt']] += ' ' + t
        else:
            cur['enunciado'].append(t)
    fecha()
    out = []
    for n in range(1, total + 1):
        q = questoes.get(n)
        if not q:
            out.append({'numero': n, 'erro': 'não encontrada'}); continue
        q['enunciado'] = re.sub(r'\s+', ' ', ' '.join(q['enunciado'])).strip()
        for k in q['alternativas']: q['alternativas'][k] = re.sub(r'\s+', ' ', q['alternativas'][k]).strip()
        q.pop('_alt')
        if len(q['alternativas']) != 5: q['aviso'] = f"{len(q['alternativas'])} alternativas"
        out.append(q)
    return out

if __name__ == '__main__':
    pdf = sys.argv[1]; total = int(sys.argv[2])
    seq = extrair(pdf)
    qs = parse(seq, total)
    json.dump(qs, open(sys.argv[3], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    from collections import Counter
    print(Counter(q.get('disciplina') for q in qs), 'erros', sum('erro' in q for q in qs), 'avisos', sum('aviso' in q for q in qs))
