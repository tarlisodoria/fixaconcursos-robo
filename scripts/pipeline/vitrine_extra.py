"""Concursos de outras bancas na vitrine (página de notícias/concursos), lidos só das páginas que o robots.txt libera.

FCC (concursosfcc.com.br): o robots.txt proíbe /concursos/ e PDFs para robôs. Lemos SÓ a página inicial (liberada), que
  lista "Inscrições abertas" e "Últimos gabaritos e resultados". A ficha leva o link oficial; o aluno abre no navegador.
ABCP (abconcursospublicos.org): robots.txt libera tudo; a página inicial lista Em breve / Abertos / Em andamento / Encerrados.
  As páginas de detalhe ficam em abcp.selecao.net.br (que proíbe /uploads/): também só a lista.
FADESP (portalfadesp.org.br, WordPress): listas "Novos Concursos" e "Concursos Abertos"; a data da última atualização de cada
  página vem da API pública do WordPress (?rest_route=/wp/v2/pages/<id>, campo modified). Seleções de pós-graduação ficam de fora.
FGV (conhecimento.fgv.br/concursos): bloco "em andamento" da lista paginada (os "realizados" ficam de fora); exames de
  conselho (/exames/) não são concurso. A lista não traz datas.
FGV lista TODOS os ativos (quem sai da lista é encerrado em gravar_fichas); a FADESP só mostra Novos/Abertos e está em
  noticias.LISTA_PARCIAL, como a FCC. Nas duas, qualquer falha de leitura aborta a banca inteira (nada gravado).
  Notícia "Novo concurso" só a partir da 2ª leitura da banca e só para id que ainda não existia em vitrine_concursos
  (_novos_na_vitrine): a 1ª leitura traria dezenas de "novos" de uma vez.

Sem IA, custo zero. Se a página mudar e o leitor achar 0 concursos, grava um trecho do HTML em robo_diagnostico
(etapa 'vitrine') para ajustar o leitor sem adivinhar.
"""
import re, time
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.robotparser import RobotFileParser

import requests

import supa
from bancas_diag import UA

BRT = timezone(timedelta(hours=-3))

# Região do TRT -> UF da sede (a ficha mostra o estado certo sem depender do edital)
TRT_UF = {1: "RJ", 2: "SP", 3: "MG", 4: "RS", 5: "BA", 6: "PE", 7: "CE", 8: "PA", 9: "PR", 10: "DF", 11: "AM", 12: "SC", 13: "PB",
          14: "RO", 15: "SP", 16: "MA", 17: "ES", 18: "GO", 19: "AL", 20: "SE", 21: "RN", 22: "PI", 23: "MT", 24: "MS"}

RX_TAG = re.compile(r"<[^>]+>")
RX_DATA = re.compile(r"\b(\d{2})/(\d{2})/(20\d{2})\b")


def _txt(h):
    h = re.sub(r"<[^>]*$", " ", h or "")  # tag cortada no fim do trecho
    return re.sub(r"\s+", " ", unescape(RX_TAG.sub(" ", h))).strip()


class Sessao:
    """requests + robots.txt de cada domínio + pausa entre páginas."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        self.robots = {}

    def pode(self, url):
        dom = re.match(r"https?://[^/]+", url).group(0)
        if dom not in self.robots:
            rp = RobotFileParser()
            try:
                r = self.s.get(f"{dom}/robots.txt", timeout=25)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self.robots[dom] = rp
        return self.robots[dom].can_fetch(UA, url)

    def html(self, url):
        if not self.pode(url):
            raise RuntimeError(f"robots.txt proíbe {url}")
        time.sleep(1)
        r = self.s.get(url, timeout=40)
        r.raise_for_status()
        if re.search(r"just a moment|cf-browser-verification|challenge-platform", r.text[:5000], re.I):
            raise RuntimeError("página de desafio anti-robô (não contornamos)")
        r.encoding = r.apparent_encoding or r.encoding
        return r.text


def sessao():
    return Sessao()


def _diagnostico(banca, url, html, itens, detalhe):
    try:
        supa.upsert("robo_diagnostico", [{"banca": banca, "etapa": "vitrine", "url": url, "status_http": 200, "bytes": len(html or ""),
                                          "titulo": _txt((re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S) or [None, ""])[1])[:200],
                                          "itens": itens, "detalhe": detalhe[:20000]}])
    except Exception as e:  # noqa: BLE001
        print(f"  vitrine {banca}: diagnóstico não gravado ({e})")


def _secoes(html, rx):
    """[(posição, nome da seção)] na ordem em que aparecem no HTML (texto entre tags)."""
    return [(m.start(), m.group(1)) for m in re.finditer(rf">\s*({rx})\s*<", html, re.I)]


def _secao_em(secoes, pos):
    atual = None
    for p, nome in secoes:
        if p > pos:
            break
        atual = nome
    return atual


def _data(texto):
    m = RX_DATA.search(texto or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 12, tzinfo=BRT)
    except ValueError:
        return None


def _sigla_orgao(nome):
    """Sigla curta para o card. 'Tribunal Regional do Trabalho da 8ª Região' -> 'TRT8/PA'."""
    n = re.sub(r"\s+", " ", nome or "").strip()
    m = re.search(r"(?:TRT|Tribunal Regional do Trabalho)\D{0,12}(\d{1,2})\s*[ªa°º]?", n, re.I)
    if m and int(m.group(1)) in TRT_UF:
        return f"TRT{int(m.group(1))}/{TRT_UF[int(m.group(1))]}"
    m = re.search(r"\(([A-Z][A-Z0-9/-]{1,14})\)", n) or re.match(r"([A-Z][A-Z0-9]{1,11}(?:[/-][A-Z]{2})?)\b(?=\s*[-–]|\s*$)", n)
    if m:
        return m.group(1)
    if n.isupper() and len(n) > 12:
        import noticias as N
        n = N._titulo(n)
    return n[:60]


# ------------------------------------------------------------------ FCC
FCC = "https://www.concursosfcc.com.br/"
RX_FCC = re.compile(r"""<a\b[^>]*href=["']([^"']*concursos/([a-z0-9]+)/index\.html?)["'][^>]*>(.*?)</a>""", re.I | re.S)


def fcc_noticias(s, mod=None):
    import noticias as N
    html = s.html(FCC)
    # títulos das seções em qualquer lugar do HTML (com ou sem entidades: &ccedil; &otilde; &Uacute;), sem exigir >texto<
    secoes = [(m.start(), "inscricoes" if m.group(0)[:1].lower() == "i" else "gabaritos") for m in re.finditer(
        r"inscri(?:ç|&ccedil;|&#231;|c)(?:õ|&otilde;|&#245;|o)es\s+(?:abertas|em\s+andamento)|(?:ú|&uacute;|&#250;|&Uacute;|&#218;|u)ltimos\s+gabaritos",
        html, re.I)]
    if not any(n == "inscricoes" for _, n in secoes):
        # sem achar a seção, não dá para saber o que é inscrição e o que é gabarito: grava o HTML e não publica nada errado
        _diagnostico("FCC", FCC, html, 0, "seção 'Inscrições abertas' não encontrada na página inicial | " + html[:15000])
        print("  vitrine FCC: seções não reconhecidas (HTML gravado em robo_diagnostico); nada gravado"); return []
    achados = list(RX_FCC.finditer(html))
    if not achados:
        _diagnostico("FCC", FCC, html, 0, "nenhum link /concursos/<código>/index.html na página inicial | " + html[:15000])
        print("  vitrine FCC: 0 concursos (HTML gravado em robo_diagnostico)"); return []
    out, vistos = [], set()
    for k, m in enumerate(achados):
        url, cod, nome = m.group(1), m.group(2).lower(), _txt(m.group(3))
        if not nome or cod in vistos:
            continue
        vistos.add(cod)
        if not url.startswith("http"):
            url = FCC + url.lstrip("/")
        fim = achados[k + 1].start() if k + 1 < len(achados) else min(len(html), m.end() + 600)
        fim = min([fim] + [p for p, _ in secoes if p > m.end()])  # o texto do item acaba no título da próxima seção
        depois = _txt(html[m.end():fim])[:200]  # cargo/modalidade logo após o link
        antes = _txt(html[max(0, m.start() - 200):m.start()])
        quando = _data(depois) or _data(antes[-40:])
        secao = _sa(_secao_em(secoes, m.start()) or "")
        fase = "inscricoes" if "inscri" in secao else "andamento"
        sigla = _sigla_orgao(nome)
        orgao = N._titulo(nome) if nome.isupper() else nome
        cargo = re.sub(r"\b\d{2}/\d{2}/20\d{2}\b", "", depois).strip(" -–:|")[:120] or None
        if cargo and re.search(r"javascript|navegador", cargo, re.I):
            cargo = None
        my = re.search(r"(\d)(\d{2})$", cod)
        ano = 2000 + int(my.group(2)) if my else None
        ref = f"fcc:{cod}"
        N._ficha(id=ref, slug="-".join(x for x in [N._slug(sigla), "fcc", cod] if x), banca="FCC", sigla=sigla, orgao=orgao, fase=fase,
                 cargos=[cargo] if cargo else [], resumo=(f"{orgao}{f' – {cargo}' if cargo else ''}. " +
                                                          ("Inscrições abertas no site da Fundação Carlos Chagas." if fase == "inscricoes"
                                                           else "Gabaritos e resultados publicados no site da Fundação Carlos Chagas.")),
                 ultima_publicacao=quando.isoformat() if quando else None,
                 links=[{"rotulo": "Página do concurso na FCC (edital, inscrição, gabaritos)", "url": url, "tipo": "banca"}])
        base = {"banca": "FCC", "orgao": orgao, "concurso_ref": ref, "dados": {"sigla": sigla, **({"ano": ano} if ano else {})}}
        if fase == "inscricoes":
            out.append({**base, "id": f"{ref}:fase:inscricoes", "tipo": "inscricoes", "relevancia": 3, "fonte_url": url,
                        "titulo": f"Inscrições abertas: {sigla}{f' – {cargo}' if cargo else ''}", "publicado_em": N._agora().isoformat(),
                        "resumo": "Edital, prazos e inscrição na página oficial da Fundação Carlos Chagas."})
        elif "gabarit" in secao:  # notícia de gabarito só para o que está na seção de gabaritos
            dia = (quando or N._agora()).strftime("%Y-%m-%d")
            out.append({**base, "id": f"{ref}:gabres:{dia}", "tipo": "gabarito", "relevancia": 2, "fonte_url": url,
                        "titulo": f"{sigla}: gabaritos e resultados publicados{f' ({cargo})' if cargo else ''}",
                        "publicado_em": (quando or N._agora()).isoformat(), "resumo": None})
    print(f"  vitrine FCC: {len(vistos)} concurso(s) na página inicial")
    return out


# ------------------------------------------------------------------ ABCP
ABCP = "https://abconcursospublicos.org/"
RX_ABCP = re.compile(r"""<a\b[^>]*href=["'](https?://abcp\.selecao\.net\.br/informacoes/(\d+)/?)["'][^>]*>(.*?)</a>""", re.I | re.S)
RX_IMG = re.compile(r"""<img\b[^>]*src=["'](https://[^"']+)["']""", re.I)
FASE_ABCP = [("breve", "em_breve"), ("inscri", "inscricoes"), ("andamento", "andamento"), ("encerrad", "encerrado")]


def abcp_noticias(s, mod=None):
    import noticias as N
    html = s.html(ABCP)
    secoes = _secoes(html, r"Em Breve|Abertos? para Inscri[çc][ãa]o|Inscri[çc][õo]es Abertas|Em Andamento|Encerrados?")
    itens = {}
    for m in RX_ABCP.finditer(html):
        url, i, texto = m.group(1), m.group(2), _txt(m.group(3))
        it = itens.setdefault(i, {"url": url, "textos": [], "pos": m.start(), "logo": None})
        if texto and texto not in it["textos"]:
            it["textos"].append(texto)
        if not it["logo"]:
            img = RX_IMG.search(m.group(3)) or RX_IMG.search(html[max(0, m.start() - 600):m.start()])
            it["logo"] = img.group(1) if img else None
    if not itens:
        _diagnostico("ABCP", ABCP, html, 0, "nenhum link abcp.selecao.net.br/informacoes/<id> | " + html[:15000])
        print("  vitrine ABCP: 0 concursos (HTML gravado em robo_diagnostico)"); return []
    out = []
    for i, it in itens.items():
        titulo = next((t for t in it["textos"] if re.search(r"concurso|processo|sele", t, re.I)), it["textos"][0] if it["textos"] else f"Processo {i}")
        orgao = next((t for t in it["textos"] if t != titulo), "") or titulo
        secao = _sa(_secao_em(secoes, it["pos"]) or "")
        fase = next((f for chave, f in FASE_ABCP if chave in secao), "encerrado")
        if fase == "encerrado":
            continue  # a vitrine mostra só o que está vivo; encerrados antigos não interessam
        cargo = re.sub(r"^(concurso p[uú]blico|processo seletivo( simplificado)?)\s*[-–]?\s*(n[º°o.]?\s*)?\d+/\d{4}\s*[-–]?\s*", "", titulo, flags=re.I).strip() or None
        m_ano = re.search(r"/(20\d{2})", titulo)
        cidade = re.sub(r"^(prefeitura|c[aâ]mara)( municipal)?( da est[aâ]ncia tur[ií]stica| da est[aâ]ncia| do munic[ií]pio)?\s+(de|da|do)\s+", "", orgao, flags=re.I)
        sigla = (cidade if cidade != orgao else _sigla_orgao(orgao))[:50]
        ref = f"abcp:{i}"
        N._ficha(id=ref, slug="-".join(x for x in [N._slug(sigla), N._slug(cargo or "")[:30], "abcp", i] if x), banca="ABCP",
                 sigla=sigla + (f" – {cargo}" if cargo else ""), orgao=orgao, fase=fase, cargos=[cargo] if cargo else [],
                 resumo=f"{orgao}: {titulo}.", logo_url=it["logo"],
                 links=[{"rotulo": "Página do concurso na ABCP (edital, inscrição, provas)", "url": it["url"], "tipo": "banca"}])
        base = {"banca": "ABCP", "orgao": orgao, "concurso_ref": ref, "dados": {"sigla": sigla, **({"ano": int(m_ano.group(1))} if m_ano else {})}}
        if fase in ("em_breve", "inscricoes"):
            out.append({**base, "id": f"{ref}:fase:{fase}", "tipo": "novo" if fase == "em_breve" else "inscricoes", "relevancia": 3,
                        "fonte_url": it["url"], "publicado_em": N._agora().isoformat(), "resumo": None,
                        "titulo": f"{'Novo concurso' if fase == 'em_breve' else 'Inscrições abertas'}: {orgao} – {cargo or titulo}"})
    print(f"  vitrine ABCP: {len(itens)} processo(s) na página inicial, {len(out)} notícia(s)")
    return out


RX_TIPO_PROC = re.compile(r"^\s*(concurso p[uú]blico|processo seletivo( p[uú]blico| simplificado)?|sele[çc][ãa]o p[uú]blica)\s*"
                          r"(n[º°o.]?\s*[\d/]+\s*)?(para|da|do|de)\s+((a|o|as|os)\s+)?", re.I)


def _orgao_do_titulo(titulo):
    """'Concurso Público para a Prefeitura de Macaé' -> 'Prefeitura de Macaé'; ano entre parênteses sai.
    Sem 'para/da/do' depois do tipo ('Concurso Público Nacional Unificado 2') o título fica inteiro."""
    import noticias as N
    t = RX_TIPO_PROC.sub("", re.sub(r"\s*[\(–-]\s*20\d{2}\s*\)?\s*$", "", titulo or "")).strip(" -–") or titulo
    return N._titulo(t) if t.isupper() else t


def _novos_na_vitrine(banca, ids):
    """Ids que ainda não existem em vitrine_concursos. Vazio na 1ª leitura da banca (nenhuma ficha dela ainda) ou se a consulta
    falhar: sem certeza de que é novo, não anuncia. Roda antes de gravar_fichas, então ainda vê o estado da rodada anterior."""
    try:
        existentes = {r["id"] for r in supa.select("vitrine_concursos", {"select": "id", "banca": f"eq.{banca}"})}
    except Exception as e:  # noqa: BLE001
        print(f"  vitrine {banca}: sem notícia de concurso novo ({e})"); return set()
    return set(ids) - existentes if existentes else set()


def _noticia_novo(banca, ref, sigla, orgao, url, resumo):
    """Id '<ref>:novo' (e não ':fase:novo'): noticias.gerar oculta ':fase:novo' que some na rodada seguinte, e esta notícia
    só é emitida uma vez (na rodada seguinte o concurso já está na vitrine)."""
    import noticias as N
    return {"id": f"{ref}:novo", "banca": banca, "orgao": orgao, "concurso_ref": ref, "tipo": "novo", "relevancia": 3,
            "fonte_url": url, "publicado_em": N._agora().isoformat(), "titulo": f"Novo concurso: {sigla}",
            "resumo": resumo, "dados": {"sigla": sigla}}


def _sigla_lista(orgao):
    """Sigla no fim do nome ('Secretaria do Estado da Fazenda – SEFA' -> 'SEFA'); senão a regra geral."""
    m = re.search(r"\s[–-]\s*([A-Z][A-Z0-9/]{1,14})$", orgao or "")
    return m.group(1) if m else _sigla_orgao(orgao)


# ------------------------------------------------------------------ FADESP
FADESP = "https://portalfadesp.org.br/"
FADESP_LISTAS = [("?page_id=33856", "novos", "Novos Concursos"), ("?page_id=33858", "abertos", "Concursos Abertos")]
RX_WP_PAGE = re.compile(r"""<a\b[^>]*href=["']https?://portalfadesp\.org\.br/\?page_id=(\d+)["'][^>]*>(.*?)</a>""", re.I | re.S)
RX_FORA_FADESP = re.compile(r"mestrado|doutorado|especializa|p[óo]s-gradua|resid[êe]ncia|PROFICAM|bolsa", re.I)
PAGINAS_FIXAS_FADESP = {"704", "33856", "33858", "329", "39595"}  # menu, as próprias listas, homologados, política de cookies


def _conteudo_wp(html):
    """Só o miolo da página (o menu e o rodapé do WordPress repetem links)."""
    html = re.sub(r"(?is)<(script|style|header|footer|nav)\b.*?</\1>", " ", html)
    m = re.search(r"""<(?:main|article)\b.*?</(?:main|article)>""", html, re.I | re.S)
    return m.group(0) if m else html


def fadesp_noticias(s, mod=None):
    import json
    import noticias as N
    itens, paginas = {}, []
    for caminho, secao, rotulo in FADESP_LISTAS:
        url = FADESP + caminho
        html = s.html(url)  # erro aqui derruba a banca inteira (gerar trata): não gravamos lista pela metade
        paginas.append((url, html))
        for m in RX_WP_PAGE.finditer(_conteudo_wp(html)):
            pid, titulo = m.group(1), _txt(m.group(2)).replace("​", "").strip()
            if pid in PAGINAS_FIXAS_FADESP or not titulo or RX_FORA_FADESP.search(titulo):
                continue
            itens.setdefault(pid, {"titulo": titulo, "secao": secao, "rotulo": rotulo})
    if not itens:
        url, html = paginas[-1]
        _diagnostico("FADESP", url, html, 0, "nenhum link ?page_id=<id> de concurso nas listas Novos/Abertos | " + html[:15000])
        print("  vitrine FADESP: 0 concursos (HTML gravado em robo_diagnostico)"); return []
    novos, out = _novos_na_vitrine("FADESP", [f"fadesp:{pid}" for pid in itens]), []
    for pid, it in itens.items():
        pagina = f"{FADESP}?page_id={pid}"
        modificado = None
        try:  # data da última atualização da página do concurso (API pública do WordPress)
            d = json.loads(s.html(f"{FADESP}?rest_route=/wp/v2/pages/{pid}&_fields=modified"))
            modificado = datetime.fromisoformat(d["modified"]).replace(tzinfo=BRT).isoformat() if d.get("modified") else None
        except Exception as e:  # noqa: BLE001  (sem a data a ficha ainda vale; a faxina só não usa o silêncio)
            print(f"  vitrine FADESP {pid}: sem data ({e})")
        orgao = _orgao_do_titulo(it["titulo"])
        m_ano = re.search(r"\b(20\d{2})\b", it["titulo"])
        sigla = _sigla_lista(orgao)
        N._ficha(id=f"fadesp:{pid}", slug="-".join(x for x in [N._slug(sigla)[:50], m_ano.group(1) if m_ano else "", "fadesp", pid] if x),
                 banca="FADESP", sigla=sigla, orgao=orgao, fase="andamento", cargos=[], ultima_publicacao=modificado,
                 resumo=f"{it['titulo']}. Listado em \"{it['rotulo']}\" no portal da FADESP.",
                 links=[{"rotulo": "Página do concurso na FADESP (edital, inscrição, resultados)", "url": pagina, "tipo": "banca"}])
        # página parada há muito tempo que só agora apareceu na lista não é concurso novo (a ficha já saiu encerrada)
        if f"fadesp:{pid}" in novos and N.FICHAS[-1]["fase"] != "encerrado":
            out.append(_noticia_novo("FADESP", f"fadesp:{pid}", sigla, orgao, pagina, f"{it['titulo']}. Edital e inscrição no portal da FADESP."))
    print(f"  vitrine FADESP: {len(itens)} concurso(s) nas listas Novos/Abertos" + (f", {len(out)} novo(s)" if out else ""))
    return out


# ------------------------------------------------------------------ FGV
FGV = "https://conhecimento.fgv.br"
FGV_MAX_PAGINAS = 15
RX_FGV_ITEM = re.compile(r"""<div class="views-row">.*?<a href="(/[^"#?]+)"[^>]*>(.*?)</a>""", re.I | re.S)


def fgv_noticias(s, mod=None):
    import noticias as N
    itens, ultimo_html = {}, ""
    for p in range(FGV_MAX_PAGINAS):
        url = f"{FGV}/concursos" + (f"?page={p}" if p else "")
        html = s.html(url)  # erro aqui derruba a banca inteira (gerar trata): não gravamos lista pela metade
        ultimo_html = html
        a, r = html.find("view-id-concursos_em_andamento"), html.find("view-id-concursos_realizados")
        if a < 0:
            break
        bloco = html[a:r if r > a else len(html)]
        novos = 0
        for m in RX_FGV_ITEM.finditer(bloco):
            caminho, titulo = m.group(1).rstrip("/"), _txt(m.group(2))
            if caminho.startswith("/exames/") or caminho in itens:
                continue
            itens[caminho] = titulo; novos += 1
        if not novos:  # página além do fim do bloco "em andamento"
            break
    if not itens:
        _diagnostico("FGV", f"{FGV}/concursos", ultimo_html, 0, "bloco 'concursos em andamento' sem itens | " + ultimo_html[:15000])
        print("  vitrine FGV: 0 concursos (HTML gravado em robo_diagnostico)"); return []
    cod_de = {c: (m.group(1) if (m := re.fullmatch(r"/concursos/([\w.-]+)", c)) else c.strip("/").replace("/", "-")) for c in itens}
    novos, out = _novos_na_vitrine("FGV", [f"fgv:{c}" for c in cod_de.values()]), []
    for caminho, titulo in itens.items():
        cod = cod_de[caminho]
        orgao = _orgao_do_titulo(titulo)
        sigla = _sigla_lista(orgao)
        N._ficha(id=f"fgv:{cod}", slug="-".join(x for x in [N._slug(sigla)[:50], "fgv", N._slug(cod)] if x), banca="FGV",
                 sigla=sigla, orgao=orgao, fase="andamento", cargos=[], resumo=f"{titulo}. Em andamento no site da FGV Conhecimento.",
                 links=[{"rotulo": "Página do concurso na FGV (edital, inscrição, provas e gabaritos)", "url": FGV + caminho, "tipo": "banca"}])
        if f"fgv:{cod}" in novos:
            out.append(_noticia_novo("FGV", f"fgv:{cod}", sigla, orgao, FGV + caminho, f"{titulo}. Edital e inscrição no site da FGV Conhecimento."))
    print(f"  vitrine FGV: {len(itens)} concurso(s) em andamento" + (f", {len(out)} novo(s)" if out else ""))
    return out


def _sa(t):
    import unicodedata
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
