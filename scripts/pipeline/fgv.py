"""Cliente da FGV Conhecimento (conhecimento.fgv.br): lista concursos e acha caderno (Tipo 1) + gabarito definitivo.

Estrutura vista pela sonda (out/2026):
  - lista: /concursos?page=0,1,2... -> links /concursos/<slug> (ex.: tjro21, aleam25)
  - página do concurso: todos os PDFs na mesma página; caderno por cargo em "Tipo 1..4" (mesma prova embaralhada:
    basta o Tipo 1), às vezes o link tem o nome do cargo; um único "Gabarito Oficial Definitivo" para todos os cargos.
  - concursos com vários editais têm subpáginas (/pcrj21/01, /02...).
  - arquivos em conhecimento.fgv.br/sites/default/files/concursos/ ou netstorage.fgv.br/<slug>/
Educação: robots.txt respeitado, User-Agent identificado, uma requisição por vez com pausa.
"""
import html as htmlmod, re, time, unicodedata
from urllib.parse import urljoin, urlparse, unquote
from urllib.robotparser import RobotFileParser

import requests

BASE = "https://conhecimento.fgv.br"
UA = "FixaConcursosBot/1.0 (+https://www.fixaconcursos.com.br; coleta de provas publicas para estudo)"
PAUSA = 2.0
MAX_PAGINAS_LISTA = 30

RX_A = re.compile(r"""<a\b[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
RX_TIPO = re.compile(r"(?:^|[\s_\-(])tipo[\s_\-]*0?(\d)\b", re.I)
# PDFs que não são caderno de prova objetiva
RX_NAO_PROVA = re.compile(r"espelho|padr[aã]o.{0,4}respost|discursiv|reda[cç][aã]o|\boral\b|senten[cç]a|pe[cç]a pr[aá]tica|"
                          r"rela[cç][aã]o de candidatos|resultado|convoca|edital|retifica|comunicado|gabarito|cronograma|"
                          r"banca da prova|requerimento|anexo|mapa|demanda|termos|aviso|isen[cç]|heteroiden|per[ií]cia|"
                          r"t[ií]tulos|recurso\b|locais de prova|local de prova|sub.?judice|\bnotas?\b|\bata\b|portaria|homologa|matr[ií]cula|chamada|vestibular", re.I)

_rp = None


def _txt(s):
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def sessao():
    global _rp
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    if _rp is None:
        _rp = RobotFileParser()
        try:
            r = s.get(f"{BASE}/robots.txt", timeout=30)
            _rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            _rp.parse([])
    return s


def permitido(url):
    return _rp is None or _rp.can_fetch(UA, url)


def _get(s, url, **kw):
    if not permitido(url):
        raise RuntimeError(f"robots.txt não permite {url}")
    time.sleep(PAUSA)
    r = s.get(url, timeout=90, **kw)
    r.raise_for_status()
    return r


def listar(s):
    """Concursos da lista paginada. Formato do vigia: id, nome, edital, status, cargos, dataProva."""
    vistos, out = set(), []
    for p in range(MAX_PAGINAS_LISTA):
        h = _get(s, f"{BASE}/concursos?page={p}").text
        novos = 0
        for href, texto in RX_A.findall(h):
            cam = urlparse(urljoin(BASE, href)).path.rstrip("/")
            m = re.fullmatch(r"/concursos/([\w-]+)", cam)
            if not m or m.group(1) in vistos:
                continue
            vistos.add(m.group(1)); novos += 1
            nome = _txt(texto)
            out.append({"id": m.group(1), "nome": nome or m.group(1), "edital": None, "status": None, "cargos": [], "dataProva": None})
        if not novos:
            break
    # nomes vazios (link só com imagem): fica o slug; o nome oficial vem do <title> no processamento
    return out


def _ano(slug_conc, titulo=""):
    m = re.search(r"(?:^|\D)(\d{2})$", slug_conc) or re.search(r"(20\d{2})", titulo or "")
    if not m:
        return None
    v = int(m.group(1))
    return v if v > 2000 else 2000 + v


def pagina(s, cid):
    """-> (titulo, html) da página do concurso + subpáginas (concursos com vários editais), já concatenadas."""
    url = f"{BASE}/concursos/{cid}"
    h = _get(s, url).text
    m = re.search(r"<title[^>]*>(.*?)</title>", h, re.I | re.S)
    titulo = _txt(m.group(1)).split(" | ")[0] if m else cid
    partes = [(url, h)]
    subs = []
    for href, _t in RX_A.findall(h):
        u = urljoin(url, href)
        if urlparse(u).path.rstrip("/").startswith(f"/concursos/{cid}/") and u not in subs:
            subs.append(u)
    for u in subs[:8]:
        try:
            partes.append((u, _get(s, u).text))
        except Exception:  # noqa: BLE001
            pass
    return titulo, partes


def _nome_do_arquivo(url):
    base = unquote(url.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
    base = RX_TIPO.sub(" ", base)
    base = re.sub(r"_\d$", "", base)  # sufixo de duplicata do Drupal (_0, _1)
    return re.sub(r"[_\-]+", " ", base).strip()


def cadernos(partes):
    """Cadernos de prova objetiva, um por cargo (o menor tipo). -> [{cod, cargo, url, tipo}]"""
    achados = {}
    for url_pag, h in partes:
        fim_anterior = 0  # noqa
        for m in RX_A.finditer(h):
            href, texto = m.group(1), _txt(m.group(2))
            u = urljoin(url_pag, href.strip())
            if not re.search(r"\.pdf(\?|$)", u, re.I):
                continue
            contexto = _txt(h[max(fim_anterior, m.start() - 1500):m.start()])[-160:]
            fim_anterior = m.end()
            arq = _nome_do_arquivo(u)
            alvo = f"{texto} {unquote(u.rsplit('/', 1)[-1])}"
            if RX_NAO_PROVA.search(alvo):
                continue
            mt = RX_TIPO.search(f" {texto}") or RX_TIPO.search(" " + unquote(u.rsplit("/", 1)[-1]).replace(".pdf", ""))
            if not mt:
                continue  # sem "Tipo N" não dá para saber se é caderno
            tipo = mt.group(1)
            # nome do cargo: o texto do link (se não for só "Tipo 1"), senão o que vem antes dele na página, senão o arquivo
            if not re.fullmatch(r"tipo\s*\d", texto, re.I) and len(texto) > 4:
                cargo = texto
            else:
                cargo = re.split(r"Tipo\s*\d", contexto, flags=re.I)[0].strip(" -–:|") or arq
                cargo = cargo[-120:]
            cod = re.sub(r"\s+", "-", arq.lower())[:150]
            if RX_NAO_PROVA.search(cargo) and not re.search(r"prova objetiva|caderno", cargo, re.I):
                cargo = arq
            atual = achados.get(cod)
            if atual is None or int(tipo) < int(atual["tipo"]):
                achados[cod] = {"cod": cod, "cargo": limpar_cargo(cargo) or arq, "arquivo": arq, "url": u, "tipo": tipo, "parte": url_pag}
    return list(achados.values())


def gabarito_definitivo(partes, parte=None):
    """URL do gabarito oficial definitivo da prova objetiva (o retificado, se houver). None se ainda não saiu.
    parte: a subpágina do caderno (concursos com vários editais têm um gabarito por edital); sem gabarito lá, vale o da página principal."""
    def cands_de(lista):
        cands = []
        for url_pag, h in lista:
            for href, texto in RX_A.findall(h):
                u = urljoin(url_pag, href.strip())
                t = _txt(texto)
                alvo = f"{t} {unquote(u.rsplit('/', 1)[-1])}".lower()
                if ".pdf" not in u.lower() or "gabarito" not in alvo or not re.search(r"defin", alvo):
                    continue
                if re.search(r"discursiv|resultado|recurso", alvo):
                    continue
                nota = (3 if "gabarito oficial definitivo" in t.lower() else 1) + (1 if "retific" in alvo or "atualizad" in alvo else 0) - (1 if "comunicado" in alvo else 0)
                cands.append((nota, len(cands), u))
        return cands
    cands = cands_de([p for p in partes if p[0] == parte]) if parte else []
    cands = cands or cands_de(partes[:1]) or cands_de(partes)
    return max(cands)[2] if cands else None


def limpar_cargo(c):
    c = re.sub(r"^\s*(\d{2}/\d{2}/\d{4}\s*)?", "", c or "")
    c = re.sub(r"^(Provas?( Escrita)? Objetivas?|Provas|N[ií]vel (Superior|M[eé]dio|Fundamental)\s*:?)\s*", "", c, flags=re.I)
    c = re.sub(r"^(Provas?( Escrita)? Objetivas?|N[ií]vel (Superior|M[eé]dio|Fundamental)\s*:?)\s*", "", c, flags=re.I)
    return c.strip(" -–:|")


def _norm(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


RX_CAB_TIPO = re.compile(r"^(.*?)[\s\-–]*TIPO\s*(\d)\s*$", re.I)


def parse_gabarito(texto):
    """Layout FGV: linha "<Cargo> - TIPO n", depois pares de linhas (números / letras). Anulada = "*".
    -> {(cargo_normalizado, tipo): (cargo_original, {numero: letra|None})}"""
    out, atual, nums = {}, None, None
    for linha in (texto or "").splitlines():
        l = linha.strip()
        if not l:
            continue
        m = RX_CAB_TIPO.match(l)
        if m and not re.fullmatch(r"[\d\s]+", m.group(1)) and len(m.group(1)) > 3:
            atual = (_norm(m.group(1)), m.group(2)); out.setdefault(atual, (m.group(1).strip(" -–"), {})); nums = None; continue
        toks = l.split()
        if atual and toks and all(t.isdigit() for t in toks):
            nums = [int(t) for t in toks]; continue
        if atual and nums and toks and all(re.fullmatch(r"[A-Ea-e*X#]|NULA|ANULADA", t, re.I) for t in toks) and len(toks) == len(nums):
            for n, t in zip(nums, toks):
                out[atual][1][n] = t.upper() if t.upper() in "ABCDE" and len(t) == 1 else None
            nums = None
    return {k: v for k, v in out.items() if len(v[1]) >= 10}


def achar_no_gabarito(gabs, cargo, arquivo, tipo):
    """Cargo do caderno -> respostas do mesmo cargo e tipo no gabarito. Só aceita par seguro:
    nome quase igual (Jaccard >= 0.75) ou todas as palavras do cargo do gabarito presentes no nome do arquivo.
    Empate entre dois cargos diferentes = None (melhor ficar sem do que pôr o gabarito errado)."""
    vazias = {"tipo", "de", "da", "do", "das", "dos", "e", "em", "a", "o", "area"}
    alvo = set(_norm(cargo).split()) - vazias
    arq = _norm(arquivo).replace(" ", "")
    notas = []
    for (nome, t), (orig, resp) in gabs.items():
        if t != str(tipo):
            continue
        pal = set(nome.split()) - vazias
        if not pal:
            continue
        j = len(pal & alvo) / len(pal | alvo) if alvo else 0.0
        na = 0.9 if all(w in arq for w in pal) else 0.0
        notas.append((max(j, na), len(pal), orig, resp))
    notas.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if not notas or notas[0][0] < 0.75:
        return None
    if len(notas) > 1 and notas[1][0] == notas[0][0] and notas[1][1] == notas[0][1]:
        return None
    return notas[0][2], notas[0][3]


def baixar(s, url):
    r = _get(s, url)
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"não é PDF: {url}")
    return r.content


def paginas_do_gabarito(texto_paginas, cargo, arquivo, limite=45000):
    """O gabarito traz todos os cargos: manda para a IA só as páginas que mais citam o cargo."""
    if sum(len(t) for t in texto_paginas) <= limite:
        return "\n".join(texto_paginas)
    palavras = {w for w in re.findall(r"\w{4,}", f"{cargo} {arquivo}".lower()) if w not in ("tipo", "prova", "cargo", "nivel", "superior", "medio")}
    codigos = set(re.findall(r"\b[a-z]{1,3}[sm]d?\s?-?\d{3}\b|\b[a-z]{2,4}-[a-z]{2,4}\b", f"{cargo} {arquivo}".lower()))
    def nota(t):
        tl = t.lower()
        return sum(3 for c in codigos if c in tl) + sum(1 for w in palavras if w in tl)
    ordem = sorted(range(len(texto_paginas)), key=lambda i: -nota(texto_paginas[i]))
    escolhidas, total = [], 0
    for i in ordem:
        if total + len(texto_paginas[i]) > limite:
            break
        escolhidas.append(i); total += len(texto_paginas[i])
    return "\n".join(texto_paginas[i] for i in sorted(escolhidas))
