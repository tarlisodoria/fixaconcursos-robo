"""Cliente do Instituto AOCP: lista concursos (API pública do site) e baixa caderno + gabarito definitivo.

Fluxo público (o mesmo que o candidato usa no navegador):
  1. GET  https://link.institutoaocp.org.br/api/concursos            -> lista (id, nome, status, edital, cargos...)
  2. GET  https://link.institutoaocp.org.br/api/concursos/{id}       -> detalhe com "links" (inclui "Visualizar Cadernos de Questões e Gabarito Definitivo")
  3. GET  open-link?identificador=...  (abre sessão e redireciona para visualizar-prova-gabarito-definitivo-public/index.jsp?concurso={id})
  4. POST index.jsp  cargo={cod}&tipo_prova={n}  -> página com <iframe> do PDF (URL assinada, expira em minutos) + tabela "Questão Gabarito"
Boa educação: User-Agent identificado, 1 requisição por vez, pausa entre requisições.
"""
import re, time
import requests
from bs4 import BeautifulSoup

BASE = "https://link.institutoaocp.org.br"
UA = "FixaConcursosBot/1.0 (+https://www.fixaconcursos.com.br; coleta de provas publicas para estudo)"
PAUSA = 2.0


def sessao():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    return s


def _get(s, url, **kw):
    time.sleep(PAUSA)
    r = s.get(url, timeout=60, **kw)
    r.raise_for_status()
    return r


def listar(s):
    return _get(s, f"{BASE}/api/concursos").json()


def detalhe(s, cid):
    j = _get(s, f"{BASE}/api/concursos/{cid}").json()
    return j[0] if isinstance(j, list) and j else j


def links_cadernos(det):
    """Todos os links de cadernos (alguns concursos separam, ex.: "Informática Perfil 2" e "Demais cargos")."""
    out = []
    for l in det.get("links") or []:
        nome = (l.get("nome") or l.get("titulo") or "").lower()
        if ("caderno" in nome or ("prova" in nome and "gabarito" in nome)) and l.get("url"):
            out.append(l["url"])
    return out


def link_cadernos(det):
    ls = links_cadernos(det)
    return ls[0] if ls else None


def abrir_cadernos(s, url):
    """Abre a página de cadernos (cria a sessão). Retorna (url_form, [(cod, nome)], [tipos])."""
    r = _get(s, url, allow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form")
    acao = requests.compat.urljoin(r.url, form.get("action")) if form and form.get("action") else r.url
    cargos = [(o.get("value"), re.sub(r"\s+-\s*$", "", o.get_text(" ", strip=True))) for o in soup.select("select[name=cargo] option") if o.get("value")]
    tipos = [o.get("value") for o in soup.select("select[name=tipo_prova] option") if o.get("value")]
    return acao, cargos, tipos


RX_GAB = re.compile(r"(\d{1,3})\s+([A-E]|\*+|X|-|ANULAD[AO]|NULA|Anulad[ao]|Nula)(?=\s|$)")


def parse_gabarito(texto):
    """'Questão Gabarito 1 C 2 B ...' -> {1:'C', 2:'B', ...}; anuladas viram None."""
    i = texto.find("Gabarito")
    trecho = texto[i:] if i >= 0 else texto
    out = {}
    for n, g in RX_GAB.findall(trecho):
        n = int(n)
        if n in out or (out and n != max(out) + 1):
            continue  # mantém só a sequência 1..N
        out[n] = g if g in "ABCDE" and len(g) == 1 else None
    return out


def ver_prova(s, acao, cargo, tipo="1"):
    """Retorna (url_pdf, gabarito{n: letra|None})."""
    time.sleep(PAUSA)
    r = s.post(acao, data={"cargo": cargo, "tipo_prova": tipo}, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    pdf = None
    for tag in soup.find_all(["iframe", "embed", "object", "a"]):
        u = tag.get("src") or tag.get("data") or tag.get("href") or ""
        if ".pdf" in u.lower():
            pdf = requests.compat.urljoin(r.url, u); break
    for t in soup(["script", "style"]):
        t.decompose()
    texto = re.sub(r"\s+", " ", soup.get_text(" "))
    return pdf, parse_gabarito(texto)


def baixar(s, url):
    return _get(s, url).content


if __name__ == "__main__":
    exemplo = "Ver prova Questão Gabarito 1 C 2 B 3 * 4 E 5 A © Todos direitos reservados."
    print(parse_gabarito(exemplo))
