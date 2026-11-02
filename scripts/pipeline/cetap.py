"""Cliente da CETAP (www.cetapnet.com.br, plataforma ProSeleta): lista concursos e acha cadernos + gabarito definitivo.

Fluxo público (o mesmo do candidato no navegador):
  1. GET /index/3/?pg=N        -> concursos finalizados (também /index/1/ em andamento e /index/abertos/)
  2. GET /informacoes/{id}/    -> página do concurso com todos os anexos em PDF (CDN anexos.cdn.selecao.net.br):
       "GABARITO OFICIAL DEFINITIVO ..."  (PDF com texto: CARGO 01: NOME / PROVA 1 / 01: A ...)
       "CARGO 01 (TIPO 1)" ou "Cargo 01 - Tipo A"  (cadernos; em geral PDF escaneado -> extração por IA)
Boa educação: User-Agent identificado, 1 requisição por vez, pausa entre requisições.
"""
import io, re, time
import requests
from bs4 import BeautifulSoup

BASE = "https://www.cetapnet.com.br"
UA = "FixaConcursosBot/1.0 (+https://www.fixaconcursos.com.br; coleta de provas publicas para estudo)"
PAUSA = 2.0
LISTAS = {"FINISHED": "/index/3/", "IN_PROGRESS": "/index/1/", "SUBSCRIBE": "/index/abertos/"}


def sessao():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    return s


def _get(s, url, **kw):
    time.sleep(PAUSA)
    r = s.get(url if url.startswith("http") else BASE + url, timeout=90, **kw)
    r.raise_for_status()
    return r


def _html(s, url):
    r = _get(s, url)
    r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else (r.apparent_encoding or "iso-8859-1")
    return BeautifulSoup(r.text, "html.parser")


def _itens(soup):
    out = []
    for li in soup.select("li.item"):
        a = li.find("a", href=re.compile(r"/informacoes/\d+/"))
        if not a:
            continue
        cid = re.search(r"/informacoes/(\d+)/", a["href"]).group(1)
        texto = " ".join(li.get_text(" ").split())
        titulo = re.sub(r"^(Concurso Público|Processo Seletivo)\s+", "", texto, flags=re.I)
        titulo = re.split(r"\s+Inscrições de\s+", titulo)[0].strip()
        insc = re.search(r"Inscrições de (\d{2}/\d{2}/(\d{4}))", texto)
        out.append({"id": cid, "nome": titulo, "edital": titulo, "dataProva": insc.group(2) if insc else None, "cargos": ""})
    return out


def listar(s, max_paginas=20):
    vistos, out = set(), []
    for status, caminho in LISTAS.items():
        for pg in range(1, max_paginas + 1):
            soup = _html(s, f"{caminho}?&ord=&dir=&pg={pg}&q=&pp=10")
            itens = [i for i in _itens(soup) if i["id"] not in vistos]
            if not itens:
                break  # a paginação volta ao início quando acaba
            for i in itens:
                vistos.add(i["id"]); i["status"] = status; out.append(i)
    return out


def diagnosticar(s):
    """Quando a listagem volta vazia ou dá erro: o que o site respondeu em cada página (para gravar em robo_diagnostico)."""
    out = []
    for nome, caminho in [("inicio", "/"), *[(k, f"{v}?&ord=&dir=&pg=1&q=&pp=10") for k, v in LISTAS.items()]]:
        linha = {"banca": "CETAP", "etapa": f"vigiar:{nome}", "url": BASE + caminho}
        try:
            time.sleep(PAUSA)
            r = s.get(BASE + caminho, timeout=60, allow_redirects=True)
            html = r.text or ""
            soup = BeautifulSoup(html, "html.parser")
            titulo = soup.title.get_text(" ", strip=True)[:200] if soup.title else None
            links = len(set(re.findall(r"/informacoes/(\d+)/", html)))
            sinais = [x for x in ("cloudflare", "captcha", "access denied", "forbidden", "just a moment", "attention required", "bloquead")
                      if x in html.lower()]
            classes = sorted({c for tag in soup.find_all(["li", "div", "article"], class_=True) for c in tag.get("class", [])
                              if re.search(r"item|concurso|card|lista", c, re.I)})[:15]
            linha.update(status_http=r.status_code, bytes=len(r.content), titulo=titulo, itens=len(soup.select("li.item")),
                         detalhe=f"url final: {r.url} | links /informacoes/: {links} | sinais: {sinais} | classes: {classes} | início: "
                                 + re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:600])
        except Exception as e:  # noqa: BLE001
            linha.update(detalhe=f"ERRO {type(e).__name__}: {e}"[:1000])
        out.append(linha)
    return out


def anexos(s, cid):
    soup = _html(s, f"/informacoes/{cid}/")
    out = []
    for a in soup.find_all("a", href=re.compile(r"\.pdf($|\?)", re.I)):
        t = " ".join(a.get_text(" ").split())
        if t:
            out.append((t, a["href"]))
    return out


RX_CADERNO = re.compile(r"^CARGO\s*(\d+)\s*(?:\(\s*TIPO\s*([0-9A-Z])\s*\)|-\s*TIPO\s*([0-9A-Z])|:\s*([^0-9].*?))?\s*(\d{2}/\d{2}/\d{4})?$", re.I)


def classificar_anexos(lista):
    """-> (url do gabarito definitivo ou None, {cargo_cod: [(tipo, url, rotulo)]})"""
    gab_def = gab_pre = None
    cadernos = {}
    for t, url in lista:
        tu = t.upper()
        if "GABARITO" in tu and "RECURSO" not in tu and "RESPOSTA" not in tu:
            if "DEFINITIV" in tu:
                gab_def = gab_def or url
            elif "PRELIMINAR" in tu:
                gab_pre = gab_pre or url
            continue
        m = RX_CADERNO.match(t.strip())
        if m:
            cod = m.group(1).zfill(2)
            tipo = (m.group(2) or m.group(3) or "1").upper()
            cadernos.setdefault(cod, []).append((tipo, url, t))
    return gab_def or gab_pre, cadernos, bool(gab_def)


def baixar(s, url):
    return _get(s, url).content


RX_CARGO = re.compile(r"CARGO\s*(\d+)\s*:\s*(.+?)(?=\s+(?:PROVA|TIPO)\s*[0-9A-Z]\b)", re.I | re.S)
RX_PROVA = re.compile(r"(?:PROVA|TIPO)\s*([0-9A-Z])\b(.*?)(?=(?:PROVA|TIPO)\s*[0-9A-Z]\b|CARGO\s*\d+\s*:|$)", re.I | re.S)
RX_ITEM = re.compile(r"\b(\d{1,3})\s*[:\-–]\s*([A-E]|X|\*|NULA|ANULADA|-)\b", re.I)


def parse_gabarito(pdf_bytes):
    """Gabarito oficial CETAP (PDF com texto) -> {cargo_cod: {"nome": str, "provas": {tipo: {n: letra|None}}}}"""
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        txt = "\n".join((p.extract_text() or "") for p in pdf.pages)
    return parse_gabarito_texto(txt)


def parse_gabarito_texto(txt):
    txt = re.sub(r"[ \t]+", " ", txt)
    blocos = list(re.finditer(r"CARGO\s*(\d+)\s*:", txt, re.I))
    out = {}
    for i, b in enumerate(blocos):
        trecho = txt[b.start(): blocos[i + 1].start() if i + 1 < len(blocos) else len(txt)]
        mc = RX_CARGO.search(trecho)
        nome = " ".join((mc.group(2) if mc else trecho[b.end() - b.start():][:80]).split())
        provas = {}
        for mp in RX_PROVA.finditer(trecho):
            itens = {int(n): (v.upper() if v.upper() in "ABCDE" and len(v) == 1 else None) for n, v in RX_ITEM.findall(mp.group(2))}
            if itens:
                provas[mp.group(1).upper()] = itens
        if provas:
            out[b.group(1).zfill(2)] = {"nome": nome, "provas": provas}
    return out


def tipo_equivalente(tipo, provas):
    """Caderno 'Tipo A' x gabarito 'PROVA 1': casa letra com número pela ordem."""
    if tipo in provas:
        return tipo
    if tipo.isalpha() and str(ord(tipo) - 64) in provas:
        return str(ord(tipo) - 64)
    if tipo.isdigit() and chr(64 + int(tipo)) in provas:
        return chr(64 + int(tipo))
    return next(iter(provas)) if len(provas) == 1 else None
