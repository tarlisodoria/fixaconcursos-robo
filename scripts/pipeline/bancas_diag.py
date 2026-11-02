"""Diagnóstico das bancas que ainda não têm robô: dá para ingerir as provas de forma educada e oficial?

Para cada banca: lê o robots.txt, abre a página inicial (só se o robots permitir), procura a área de concursos,
conta links de PDF (provas, gabaritos, editais) e detecta bloqueio anti-robô (Cloudflare e afins).
Nada é contornado: bloqueio ou robots proibindo = a banca fica de fora (ou entra só por envio manual no Admin).
Resultado em robo_diagnostico (etapa 'diagnostico'), um resumo por banca, para decidir a ordem dos próximos robôs.
"""
import os, re, tempfile, time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

import supa

UA = "FixaConcursosBot/1.0 (+https://www.fixaconcursos.com.br; coleta de provas publicas para estudo)"

# banca -> página inicial oficial (o robô acha a área de concursos a partir dela)
BANCAS = {
    "FGV": "https://conhecimento.fgv.br/concursos",
    "FCC": "https://www.concursosfcc.com.br/",
    "VUNESP": "https://www.vunesp.com.br/",
    "CESGRANRIO": "https://www.cesgranrio.org.br/",
    "IBFC": "https://www.ibfc.org.br/",
    "QUADRIX": "https://www.quadrix.org.br/",
    "IDECAN": "https://www.idecan.org.br/",
    "CONSULPLAN": "https://www.institutoconsulplan.org.br/",
    "IADES": "https://www.iades.com.br/",
    "FUNDATEC": "https://www.fundatec.org.br/",
    "OBJETIVA": "https://www.objetivas.com.br/",
    "IBADE": "https://www.ibade.org.br/",
    "SELECON": "https://selecon.org.br/",
    "FADESP": "https://portalfadesp.org.br/?page_id=704",  # www.fadesp.org.br não resolve; concursos ficam no portal
    "FUMARC": "https://www.fumarc.com.br/SitePages/Home.aspx",
    "INSTITUTO ACCESS": "https://www.institutoaccess.org.br/",
    "AVANCA SP": "https://avancasp.org.br/",
    "COPEVE-UFAL": "https://copeve.ufal.br/",
}

RX_CF = re.compile(r"just a moment|cf-browser-verification|challenge-platform|attention required|ddos protection|captcha", re.I)
RX_AREA = re.compile(r"concurso|processo|selec|edita|prova|andamento|encerrad", re.I)
RX_PDF = re.compile(r"""href=["']([^"']+\.pdf)(?:\?[^"']*)?["']""", re.I)
RX_LINK = re.compile(r"""<a\b[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
RX_TITULO = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


# Servidor que não envia o certificado intermediário: o navegador completa a cadeia sozinho, o requests (certifi) não.
# Juntamos o intermediário oficial (baixado do endereço AIA do próprio certificado) ao certifi. A verificação continua ligada.
INTERMEDIARIOS = {"www.fumarc.com.br": "globalsign-rsa-ov-ssl-ca-2018.pem", "fumarc.com.br": "globalsign-rsa-ov-ssl-ca-2018.pem"}
_BUNDLES = {}


def verificacao(url):
    """Valor do verify= do requests para a URL: True (certifi) ou um bundle certifi + intermediário."""
    pem = INTERMEDIARIOS.get(urlparse(url).netloc.lower())
    if not pem:
        return True
    if pem not in _BUNDLES:
        import certifi
        with open(certifi.where(), encoding="utf-8") as f:
            base = f.read()
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", pem), encoding="utf-8") as f:
            extra = f.read()
        fd, caminho = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(base + "\n" + extra)
        _BUNDLES[pem] = caminho
    return _BUNDLES[pem]


def _get(s, url):
    try:
        r = s.get(url, timeout=25, allow_redirects=True, verify=verificacao(url))
        return r.status_code, r.text if "text" in r.headers.get("content-type", "") or "html" in r.headers.get("content-type", "") else "", r
    except requests.RequestException as e:
        return None, str(e)[:200], None


def _robots(s, base):
    rp = RobotFileParser()
    st, txt, _ = _get(s, urljoin(base, "/robots.txt"))
    if st == 200:
        rp.parse(txt.splitlines())
    else:
        rp.parse([])  # sem robots.txt = tudo permitido
    return rp, st


def diagnosticar_banca(s, banca, inicio):
    base = f"{urlparse(inicio).scheme}://{urlparse(inicio).netloc}"
    rp, st_robots = _robots(s, base)
    info = {"banca": banca, "etapa": "diagnostico", "url": inicio, "status_http": None, "bytes": 0, "titulo": None, "itens": 0, "detalhe": ""}
    if not rp.can_fetch(UA, inicio):
        info["detalhe"] = "veredito=BLOQUEADA (robots.txt proíbe a página inicial)"
        return info
    st, html, r = _get(s, inicio)
    info["status_http"], info["bytes"] = st, len(html or "")
    if st is None:
        info["detalhe"] = f"veredito=FORA DO AR ou endereço errado ({html})"
        return info
    m = RX_TITULO.search(html or "")
    info["titulo"] = re.sub(r"\s+", " ", m.group(1)).strip()[:150] if m else None
    if st in (403, 429, 503) or RX_CF.search(html[:5000] if html else ""):
        info["detalhe"] = f"veredito=ANTI-ROBÔ (HTTP {st}{', desafio Cloudflare' if RX_CF.search(html or '') else ''}); só envio manual"
        return info
    if st >= 400:
        info["detalhe"] = f"veredito=ERRO HTTP {st} (conferir endereço)"
        return info
    # links da área de concursos no mesmo site
    final = r.url if r is not None else inicio
    dominio = urlparse(final).netloc
    areas = []
    for href, texto in RX_LINK.findall(html):
        u = urljoin(final, href.strip())
        if urlparse(u).netloc.endswith(dominio.replace("www.", "")) and RX_AREA.search(re.sub(r"<[^>]+>", " ", texto) + " " + href):
            if u not in areas and u != final:
                areas.append(u)
    pdfs = set(urljoin(final, p) for p in RX_PDF.findall(html))
    visitadas = []
    for u in areas[:3]:  # no máximo 3 páginas internas, com pausa
        if not rp.can_fetch(UA, u):
            continue
        time.sleep(2)
        st2, html2, _ = _get(s, u)
        visitadas.append(f"{st2} {u[:90]}")
        if st2 == 200 and html2:
            pdfs |= set(urljoin(u, p) for p in RX_PDF.findall(html2))
    info["itens"] = len(pdfs)
    tipos = {k: sum(1 for p in pdfs if re.search(k, p, re.I)) for k in ("prova|caderno", "gabarito", "edital")}
    js = len(html) < 3000 or "__NEXT_DATA__" in html or "ng-app" in html or 'id="root"' in html or 'id="app"' in html
    if pdfs:
        veredito = "VIÁVEL (PDFs acessíveis)"
    elif js:
        veredito = "INVESTIGAR (site montado por JavaScript; procurar a API pública que ele usa)"
    else:
        veredito = "INVESTIGAR (área de concursos sem PDF direto nas primeiras páginas)"
    info["detalhe"] = (f"veredito={veredito} | robots.txt HTTP {st_robots} | {len(areas)} links de área de concursos | "
                       f"PDFs: {len(pdfs)} (provas/cadernos {tipos['prova|caderno']}, gabaritos {tipos['gabarito']}, editais {tipos['edital']}) | "
                       f"visitadas: {'; '.join(visitadas) or 'nenhuma'} | exemplos: {' '.join(sorted(pdfs)[:3])}")[:2000]
    return info


def rodar():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    linhas = []
    for banca, inicio in BANCAS.items():
        try:
            info = diagnosticar_banca(s, banca, inicio)
        except Exception as e:  # noqa: BLE001  (uma banca com problema não para as outras)
            info = {"banca": banca, "etapa": "diagnostico", "url": inicio, "status_http": None, "bytes": 0, "titulo": None, "itens": 0,
                    "detalhe": f"veredito=ERRO no diagnóstico ({e})"[:2000]}
        print(f"diagnostico {banca}: {info['detalhe'][:160]}")
        linhas.append(info)
        time.sleep(2)
    try:
        supa.upsert("robo_diagnostico", linhas)
    except Exception as e:  # noqa: BLE001
        print(f"diagnostico: não gravou no banco ({e})")
