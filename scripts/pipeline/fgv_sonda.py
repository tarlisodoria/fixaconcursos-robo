"""Sonda da FGV: mapeia onde ficam caderno de prova e gabarito em cada página de concurso. NÃO grava questões.

Respeita o robots.txt, pausa de 2 s entre páginas, no máximo ~30 páginas por rodada. Só HEAD nos PDFs (não baixa).
Resultado em robo_diagnostico (banca FGV, etapa 'sonda'): uma linha por concurso + uma de resumo,
com os textos dos links e exemplos de URL, para desenhar o robô definitivo.
"""
import re, time
from collections import Counter
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

import supa
from bancas_diag import UA

BASE = "https://conhecimento.fgv.br"
INICIO = f"{BASE}/concursos"
MAX_CONCURSOS = 25

RX_LINK = re.compile(r"""<a\b[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
RX_TITULO = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _txt(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def classe(url, texto):
    t = f"{texto} {url.rsplit('/', 1)[-1]}".lower()
    if re.search(r"gabarito", t):
        return "gabarito_def" if re.search(r"defin|final|p[oó]s.?recurso", t) else "gabarito"
    if re.search(r"\bprova|caderno|tipo\s*\d|\btipo\b", t) and not re.search(r"resultado|convoca|local de prova|data", t):
        return "prova"
    if re.search(r"edital", t):
        return "edital"
    return "outro"


class Sonda:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        self.rp = RobotFileParser()
        r = self.s.get(f"{BASE}/robots.txt", timeout=25)
        self.rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        self.paginas = 0

    def get(self, url):
        if not self.rp.can_fetch(UA, url):
            return None, "robots.txt proíbe"
        if self.paginas:
            time.sleep(2)
        self.paginas += 1
        try:
            r = self.s.get(url, timeout=30)
        except requests.RequestException as e:
            return None, str(e)[:150]
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        return r.text, None

    def links(self, url, html):
        for href, texto in RX_LINK.findall(html):
            u = urljoin(url, href.strip())
            if urlparse(u).netloc.endswith("fgv.br"):
                yield u, _txt(texto)


def rodar():
    sd = Sonda()
    html, erro = sd.get(INICIO)
    if not html:
        print(f"fgv_sonda: lista de concursos não abriu ({erro})")
        supa.upsert("robo_diagnostico", [{"banca": "FGV", "etapa": "sonda", "url": INICIO, "detalhe": f"lista não abriu: {erro}"}])
        return
    # páginas de concursos (/concursos/<slug> ou páginas próprias como /cpnu2) e listas de encerrados
    concursos, listas = [], []
    for u, t in sd.links(INICIO, html):
        cam = urlparse(u).path.rstrip("/")
        if re.search(r"encerrad|anterior|realizad", f"{t} {cam}", re.I) or "page=" in u:
            if u not in listas:
                listas.append(u)
        elif re.fullmatch(r"/concursos/[\w-]+", cam) and u not in concursos:
            concursos.append(u)
    for lst in listas[:3]:  # concursos encerrados têm prova e gabarito publicados: priorizar
        h, _ = sd.get(lst)
        for u, _t in sd.links(lst, h or ""):
            cam = urlparse(u).path.rstrip("/")
            if re.fullmatch(r"/concursos/[\w-]+", cam) and u not in concursos:
                concursos.insert(0, u)
    print(f"fgv_sonda: {len(concursos)} páginas de concurso, {len(listas)} listas extras ({' '.join(listas[:3])})")

    linhas, total, exemplos_pdf = [], Counter(), []
    for url in concursos[:MAX_CONCURSOS]:
        h, erro = sd.get(url)
        if not h:
            linhas.append({"banca": "FGV", "etapa": "sonda", "url": url, "detalhe": f"não abriu: {erro}"}); continue
        m = RX_TITULO.search(h)
        pdfs = {}
        sub = []
        for u, t in sd.links(url, h):
            if re.search(r"\.pdf(\?|$)", u, re.I):
                pdfs[u] = (classe(u, t), t[:80])
            elif urlparse(u).path.startswith(urlparse(url).path + "/") and u not in sub:
                sub.append(u)  # abas/subpáginas do concurso (ex.: .../provas)
        c = Counter(k for k, _ in pdfs.values())
        total.update(c)
        provas = [(u, t) for u, (k, t) in pdfs.items() if k == "prova"]
        gabs = [(u, t) for u, (k, t) in pdfs.items() if k.startswith("gabarito")]
        exemplos_pdf += [u for u, _ in provas[:1]]
        det = (f"PDFs {len(pdfs)}: provas {c['prova']}, gabaritos {c['gabarito']}, gabaritos definitivos {c['gabarito_def']}, "
               f"editais {c['edital']}, outros {c['outro']} | subpáginas: {' '.join(sub[:5]) or 'nenhuma'} | "
               f"provas ex.: {' || '.join(f'{t} -> {u}' for u, t in provas[:3])} | "
               f"gabaritos ex.: {' || '.join(f'{t} -> {u}' for u, t in gabs[:3])} | "
               f"outros textos: {' | '.join(sorted({t for k, t in pdfs.values() if k == 'outro'})[:8])}")
        linhas.append({"banca": "FGV", "etapa": "sonda", "url": url, "status_http": 200, "bytes": len(h),
                       "titulo": _txt(m.group(1))[:150] if m else None, "itens": c["prova"], "detalhe": det[:3000]})
        print(f"fgv_sonda {url.rsplit('/', 1)[-1]}: provas {c['prova']}, gabaritos {c['gabarito'] + c['gabarito_def']}, subpáginas {len(sub)}")

    # um PDF de prova: só HEAD (tamanho e tipo), sem baixar
    cabec = []
    for u in exemplos_pdf[:3]:
        if sd.rp.can_fetch(UA, u):
            time.sleep(2)
            try:
                r = sd.s.head(u, timeout=25, allow_redirects=True)
                cabec.append(f"{r.status_code} {r.headers.get('content-type')} {r.headers.get('content-length')} {u}")
            except requests.RequestException as e:
                cabec.append(f"erro {e} {u}"[:200])
    resumo = (f"concursos visitados {min(len(concursos), MAX_CONCURSOS)} de {len(concursos)} | PDFs por tipo: {dict(total)} | "
              f"HEAD de provas: {' ; '.join(cabec) or 'nenhuma prova achada'} | listas: {' '.join(listas[:5])}")
    linhas.append({"banca": "FGV", "etapa": "sonda", "url": INICIO, "itens": total["prova"], "titulo": "RESUMO", "detalhe": resumo[:3000]})
    print(f"fgv_sonda: {resumo[:300]}")
    cols = ("banca", "etapa", "url", "status_http", "bytes", "titulo", "itens", "detalhe")
    supa.upsert("robo_diagnostico", [{k: l.get(k) for k in cols} for l in linhas])  # mesmas chaves em todas as linhas
