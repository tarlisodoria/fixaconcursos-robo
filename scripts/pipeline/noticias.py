"""Notícias de concursos a partir das páginas oficiais das bancas (sem copiar portais de notícia).

Cada notícia é um FATO publicado pela banca (concurso novo, inscrições abertas, edital, retificação, locais de prova,
gabarito, resultado…), escrito por modelo de texto fixo, com link para a fonte oficial. Sem IA: custo zero e nada inventado.
O id é estável (banca + concurso + arquivo), então rodar todo dia não duplica e não "requenta" notícia velha.
"""
import json, re, time, unicodedata
from datetime import datetime, timedelta, timezone

import supa
from uf import inferir_uf

JANELA_DIAS = 21  # publicações mais antigas que isso não viram notícia (evita inundar na 1ª execução)
BRT = timezone(timedelta(hours=-3))


def _sa(t):
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()


def _agora():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ o que a publicação é
REGRAS = [  # (regex sem acento, tipo, relevância); a 1ª que casar vale. Abertura só conta no INÍCIO da descrição
    # rotina: atos sobre candidatos específicos, isenção, recursos, atendimento especial
    (r"isencao|recurso|deferid|indeferid|pessoas? com deficiencia|condic(ao|oes) especia|atendimento especial|heteroidentifica|vlibras"
     r"|nome social|judicial|sub judice|desligamento|eliminacao|reintegracao|regularizacao|exclusao|sorteio|lista de presenca", "publicacao", 1),
    (r"retifica", "publicacao", 2),
    (r"^(edital (n\S*\s*)?0*1\b(?![\d/])|edital de abertura|abertura\b)", "inscricoes", 3),
    (r"prorroga|reabertura", "inscricoes", 3),
    (r"gabarito (oficial )?definitivo|gabaritos? definitivos?", "gabarito", 3),
    (r"gabarito|padrao (preliminar )?de resposta", "gabarito", 2),
    (r"homologa|resultado final (do concurso|do certame|da selecao)", "resultado", 3),
    (r"resultado|classificacao|aprovados|convocacao", "resultado", 2),
    (r"loca(l|is) de prova|cartao (de )?(informacao|confirmacao)|consulta.*local|data (da|das) prova|cronograma", "prova", 3),
]


def classificar(desc):
    d = _sa(re.sub(r"^\s*\d{2}/\d{2}/\d{4}\s*[-–]\s*", "", desc or "").strip())
    for rx, tipo, rel in REGRAS:
        if re.search(rx, d):
            return tipo, rel
    return "publicacao", 2


def _limpar_desc(desc):
    d = re.sub(r"^\s*\d{2}/\d{2}/\d{4}\s*[-–]\s*", "", desc or "").strip()   # AOCP põe a data no começo
    d = re.sub(r"\s+", " ", d)
    return d[:220]


def _brl(v):
    v = (v or "").strip()
    return v if re.search(r"[1-9]", v) else None  # "R$ 0,00" ou "R$" vazio = banca não informou


def _orgao_titulo(nome):
    """'PM AL 26' -> 'PM AL'; 'IFPA - INSTITUTO FEDERAL…' fica como está, só sem caixa alta gritante."""
    n = re.sub(r"\s+", " ", re.sub(r"(?<!\S)(19|20)?\d{2}(?!\S)", " ", (nome or "").replace("_", " "))).strip()  # tira o ano ("CGU 26 AFFC")
    if n.isupper() and len(n) > 12:
        partes = n.split(" - ", 1)
        if len(partes) == 2 and len(partes[0]) <= 12:
            return f"{partes[0]} – {_titulo(partes[1])}"
        return _titulo(n)
    return n


MINUSCULAS = {"de", "da", "do", "das", "dos", "e", "em", "para", "a", "o"}


def _titulo(t):
    """'SECRETARIA DE ESTADO DA BAHIA - BA' -> 'Secretaria de Estado da Bahia - BA' (siglas de até 3 letras ficam)."""
    out = []
    for i, w in enumerate(t.split()):
        lw = w.lower()
        if i and lw in MINUSCULAS:
            out.append(lw)
        elif len(w) <= 2 and w.isalpha():
            out.append(w)
        else:
            out.append(w[:1] + w[1:].lower())
    return " ".join(out)


# ------------------------------------------------------------------ fichas dos concursos (página /concurso/[slug])
FICHAS = []  # preenchida pelas funções de cada banca na mesma passada das notícias (um detalhe por concurso)
CAMPOS_FICHA = ("id", "slug", "banca", "sigla", "orgao", "fase", "vagas", "salario_max", "periodo_inscricao", "inicio_inscricao",
                "fim_inscricao", "logo_url", "resumo", "cargos", "links", "atualizado_em", "ultima_publicacao")

# "Em andamento" só enquanto o concurso dá sinal de vida: publicação oficial recente ou inscrição encerrada há pouco.
# A banca deixa processos antigos na lista de "em andamento" por anos (ex.: concurso de 2014 ainda listado);
# sem publicação há SILENCIO_DIAS e com inscrição encerrada há mais de INSCRICAO_DIAS, a ficha sai das listas (fase encerrado).
SILENCIO_DIAS = 180
LISTA_PARCIAL = {"FCC", "FADESP"}  # bancas cuja página lida não lista todos os concursos ativos (FADESP: só Novos/Abertos)
INSCRICAO_DIAS = 365


def _ativo(fim_inscricao, ultima_publicacao):
    agora = _agora()
    datas = []
    for v, dias in ((ultima_publicacao, SILENCIO_DIAS), (fim_inscricao, INSCRICAO_DIAS)):
        if v:
            try:
                datas.append(datetime.fromisoformat(v) > agora - timedelta(days=dias))
            except ValueError:
                pass
    return any(datas) if datas else True  # sem nenhuma data, não dá para afirmar que acabou


def _slug(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:80]


def _ficha(**k):
    f = {c: k.get(c) for c in CAMPOS_FICHA}
    f["cargos"] = [re.sub(r"\s+", " ", c).strip() for c in (k.get("cargos") or []) if c and c.strip()][:60]
    f["links"] = [l for l in (k.get("links") or []) if l.get("url")]
    f["atualizado_em"] = _agora().isoformat()
    if f["fase"] == "andamento" and not _ativo(f.get("fim_inscricao"), f.get("ultima_publicacao")):
        f["fase"] = "encerrado"
    FICHAS.append(f)


# ------------------------------------------------------------------ Cebraspe
def cebraspe_noticias(s, mod):
    out, limite = [], _agora() - timedelta(days=JANELA_DIAS)
    for e in mod.listar_eventos(s):
        st = e.get("eventoStatus") or ""
        if st == "encerrado":
            continue
        eid = e["eventoURL"]
        try:
            det = mod.detalhe(s, eid)
        except Exception as ex:  # noqa: BLE001
            print(f"  noticias cebraspe {eid}: {ex}"); continue
        orgao = det.get("eventoNomeCompleto") or _orgao_titulo(det.get("eventoNomeAbreviado") or eid)
        sigla = _orgao_titulo(det.get("eventoNomeAbreviado") or eid)
        pagina = f"https://www.cebraspe.org.br/concursos/{eid}"
        vagas = str(det.get("eventoTotalVagas") or "").strip() or None
        sal = _brl(det.get("strEventoSalarioMaximo"))
        periodo = (det.get("periodoInscricao") or "").strip() or None
        m_fim = re.search(r"at[eé]\s+(\d{2})/(\d{2})/(\d{4})(?:\D+(\d{1,2})[:h](\d{2}))?", periodo or "")
        fim = datetime(int(m_fim.group(3)), int(m_fim.group(2)), int(m_fim.group(1)), int(m_fim.group(4) or 23), int(m_fim.group(5) or 59), tzinfo=BRT).isoformat() if m_fim else None
        dados = {k: v for k, v in {"vagas": vagas, "salario_max": sal, "periodo_inscricao": periodo, "fim_inscricao": fim, "sigla": sigla}.items() if v}
        cifras = ", ".join(x for x in [(f"{int(vagas):,}".replace(",", ".") + (" vaga" if vagas == "1" else " vagas")) if vagas and vagas.isdigit() and int(vagas) > 0 else None,
                                       f"salários de até {sal}" if sal else None] if x)
        base = {"banca": "CEBRASPE", "orgao": orgao, "concurso_ref": f"cebraspe:{eid}", "dados": dados}
        m_ini = re.search(r"de\s+(\d{2})/(\d{2})/(\d{4})", periodo or "", re.I)
        arqs = [a for a in det.get("arquivosEdital") or [] if (a.get("nomeArquivo") or "").lower().endswith(".pdf")]
        datas_arq = []
        for a in [*(det.get("arquivosEdital") or []), *(det.get("arquivosGabarito") or [])]:
            dt = _data_qualquer(a.get("dataArquivoObj") or a.get("dataArquivo"))
            if dt:
                datas_arq.append(dt)
        abertura = sorted((a for a in arqs if classificar(a.get("descricaoArquivo") or "")[0] == "inscricoes" and "retifica" not in _sa(a.get("descricaoArquivo"))),
                          key=lambda a: a.get("dataArquivoObj") or "")
        _ficha(id=f"cebraspe:{eid}", slug=f"{_slug(eid)}-cebraspe", banca="CEBRASPE", sigla=sigla, orgao=det.get("eventoNomeCompleto"),
               fase={"novos": "em_breve", "inscricoes-abertas": "inscricoes"}.get(st, "andamento"),
               vagas=int(vagas) if vagas and vagas.isdigit() and int(vagas) > 0 else None, salario_max=sal, periodo_inscricao=periodo,
               inicio_inscricao=datetime(int(m_ini.group(3)), int(m_ini.group(2)), int(m_ini.group(1)), tzinfo=BRT).isoformat() if m_ini else None,
               fim_inscricao=fim, cargos=[c.get("area") or "" for c in det.get("eventoCargos") or []],
               ultima_publicacao=max(datas_arq).isoformat() if datas_arq else None,
               links=[*([{"rotulo": "Edital de abertura", "url": mod.url_arquivo(eid, abertura[0]["nomeArquivo"]), "tipo": "edital"}] if abertura else []),
                      *([{"rotulo": "Fazer a inscrição", "url": pagina, "tipo": "inscricao"}] if st == "inscricoes-abertas" else []),
                      {"rotulo": "Página do concurso no Cebraspe", "url": pagina, "tipo": "banca"}])
        if st == "novos":
            out.append({**base, "id": f"cebraspe:{eid}:fase:novos", "tipo": "novo", "relevancia": 3, "fonte_url": pagina,
                        "titulo": f"Concurso {sigla} tem página aberta no Cebraspe", "publicado_em": _agora().isoformat(),
                        "resumo": (f"{cifras}. " if cifras else "") + "O Cebraspe abriu a página do concurso; o edital e o calendário saem por lá."})
        if st == "inscricoes-abertas":
            out.append({**base, "id": f"cebraspe:{eid}:fase:inscricoes", "tipo": "inscricoes", "relevancia": 3, "fonte_url": pagina,
                        "titulo": f"Inscrições abertas: {sigla}" + (f" ({cifras})" if cifras else ""), "publicado_em": _agora().isoformat(),
                        "resumo": f"Período de inscrição: {periodo[:1].lower() + periodo[1:]}." if periodo else "Confira o período de inscrição na página oficial."})
            if fim and timedelta(0) < datetime.fromisoformat(fim) - _agora() <= timedelta(days=3):
                out.append({**base, "id": f"cebraspe:{eid}:ultimos-dias", "tipo": "ultimos_dias", "relevancia": 3, "fonte_url": pagina,
                            "titulo": f"Últimos dias: inscrições para {sigla} terminam em {datetime.fromisoformat(fim):%d/%m}",
                            "resumo": f"Prazo final: {datetime.fromisoformat(fim):%d/%m/%Y às %Hh%M} (horário de Brasília).", "publicado_em": _agora().isoformat()})
        for a in det.get("arquivosEdital") or []:
            nome, desc = a.get("nomeArquivo") or "", a.get("descricaoArquivo") or ""
            if not nome.lower().endswith(".pdf") or "vlibras" in _sa(desc):
                continue
            try:
                quando = datetime.fromisoformat(a["dataArquivoObj"]).replace(tzinfo=BRT)
            except Exception:  # noqa: BLE001
                continue
            if quando < limite:
                continue
            tipo, rel = classificar(desc)
            out.append({**base, "id": f"cebraspe:{eid}:arq:{nome}", "tipo": tipo, "relevancia": rel, "fonte_url": mod.url_arquivo(eid, nome),
                        "titulo": f"{sigla}: {_limpar_desc(desc)}", "resumo": None, "publicado_em": quando.isoformat()})
    return out


# ------------------------------------------------------------------ AOCP
GENERICO = re.compile(r"edital|abertura|concurso|processo seletivo|\bpss\b|prefeitura|retifica|\banexo\b|^n[º°o.]", re.I)


# trecho que NÃO é cargo (documento, fase, ato administrativo...). Nome de pessoa também não pode virar cargo:
# por isso o cargo só é lido do edital de abertura de verdade (ver _eh_abertura) e precisa parecer um cargo.
NAO_CARGO = re.compile(r"resultado|recurso|portaria|comunicado|prorroga|modelo|declara|decis|preliminar|suplente|classifica|termo|"
                       r"cumprimento|\blista\b|convoca|homolog|gabarito|\baviso\b|errata|cronograma|inscri|isen|\blocais?\b|\bato\b|reabert|"
                       r"\bpos\b|retifica|anexo|formulario|requerimento|cotas|pcd|heteroidentifica", re.I)
RX_SIGLA = re.compile(r"^[A-Z][A-Z0-9]{1,11}(?:[/ -][A-Z]{2,})?\.?$")


def _eh_abertura(nome):
    """'09/10/2026 - Edital de Abertura nº 01/2026 - ...' sim; 'Modelo de declaração (anexo do Edital de Abertura)', retificação, reabertura não."""
    d = _sa(re.sub(r"^\s*\d{2}/\d{2}/\d{4}\s*[-–]?\s*", "", nome or ""))
    return bool(re.match(r"edital\b[^-–]{0,40}\babertura\b", d)) and not re.search(r"retifica|reabertura|resultado|convoca|modelo|anexo", d)


def _eh_abertura_folgado(nome):
    """Plano B: 'Edital nº 01/2026 - HU-UFPA - Abertura', 'Edital 001/2026 de abertura...' (sem retificação, resultado, anexo...)."""
    d = _sa(re.sub(r"^\s*\d{2}/\d{2}/\d{4}\s*[-–]?\s*", "", nome or ""))
    if re.search(r"retifica|reabertura|resultado|convoca|modelo|anexo|homolog|gabarito|local|prorroga|isen|errata|aviso", d):
        return False
    return bool(re.match(r"edital\b", d) and (re.search(r"\babertura\b", d) or re.match(r"edital\s*(n[ºo°.]*\s*)?0*1\s*/\s*\d{2,4}\b", d)))


def _data_qualquer(v):
    """'2026-10-13T10:00:00', '13/10/2026 10:00' ou '2026-10-13' -> datetime (Brasília); None se não reconhecer."""
    v = str(v or "")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", v) or None
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, tzinfo=BRT)
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", v)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 12, tzinfo=BRT)
    return None


def _partes_abertura(nome, orgao):
    """-> (cargo, sigla) tirados do nome do edital de abertura.
    '09/10/2026 - Edital de Abertura - PSS Prefeitura Curitiba - Contador nº 10/2026' -> ('Contador', None)
    '... - Edital de Abertura - Concurso Público Nº 001/2026 - SESA/AP' -> (None, 'SESA/AP')"""
    cargo = sigla = None
    for seg in re.split(r"\s+[-–]\s+", re.sub(r"^\s*\d{2}/\d{2}/\d{4}\s*", "", nome or "")):
        seg = re.sub(r"\s*n[º°o.]*\s*\d+\s*/\s*\d{2,4}\s*$", "", seg, flags=re.I).strip()
        if not seg or GENERICO.search(seg) or _sa(seg) in _sa(orgao):
            continue
        if RX_SIGLA.match(seg):
            sigla = sigla or seg.rstrip(".")
        elif not cargo and 3 <= len(seg) <= 60 and seg[0].isupper() and not NAO_CARGO.search(_sa(seg)) and not re.search(r"\d{3,}", seg):
            cargo = seg
    return cargo, sigla


def _resumir(t, n=240):
    t = re.sub(r"\s+", " ", t or "").strip()
    if len(t) <= n:
        return t or None
    corte = t[:n]
    fim = max(corte.rfind(". "), corte.rfind("; "))
    return (corte[:fim + 1] if fim > n * 0.5 else corte[:corte.rfind(" ")] + "…")
RX_DATA = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})")


def aocp_noticias(s, mod):
    out, limite = [], _agora() - timedelta(days=JANELA_DIAS)
    for c in mod.listar(s):
        st = c.get("status")
        if st not in ("NEW", "SUBSCRIBE", "IN_PROGRESS", "RESULT_AVAILABLE"):
            continue
        try:
            det = mod.detalhe(s, c["id"])
        except Exception as ex:  # noqa: BLE001
            print(f"  noticias aocp {c['id']}: {ex}"); continue
        orgao = _orgao_titulo(det.get("nome") or c.get("nome"))
        sigla = orgao.split(" – ")[0]
        pubs = det.get("publicacoes") or []
        # edital de abertura = o MAIS ANTIGO cujo nome começa com "Edital ... Abertura" (não anexo, modelo, retificação, resultado)
        aberturas = sorted((p for p in pubs if p.get("url") and _eh_abertura(p.get("nome"))),
                           key=lambda p: (lambda m: (m.group(3), m.group(2), m.group(1)) if m else ("9999",))(RX_DATA.match(p.get("nome") or "")))
        if not aberturas:  # nome fora do padrão ("Edital nº 01 - EBSERH - Abertura..."): o mais antigo com "edital" + "abertura", senão o Edital nº 01
            aberturas = sorted((p for p in pubs if p.get("url") and _eh_abertura_folgado(p.get("nome"))),
                               key=lambda p: (lambda m: (m.group(3), m.group(2), m.group(1)) if m else ("9999",))(RX_DATA.match(p.get("nome") or "")))
        abertura = aberturas[0]["url"] if aberturas else None
        datas_pub = [datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 12, tzinfo=BRT)
                     for m in (RX_DATA.match(p.get("nome") or "") for p in pubs) if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31]
        # conteúdo programático publicado à parte (a AOCP costuma soltar o Anexo em arquivo separado)
        cps = [p for p in pubs if p.get("url") and re.search(r"conteudos? programaticos?|objetos? de avaliacao", _sa(p.get("nome")))
               and not re.search(r"retifica|errata|resultado", _sa(p.get("nome")))]
        conteudo = cps[-1]["url"] if cps else None
        pagina = abertura or "https://www.institutoaocp.org.br/concursos/status/inscricoes-abertas"
        # vários processos do mesmo órgão (ex.: PSS de Curitiba): o cargo do edital de abertura diferencia o título
        cargo, sigla_ab = _partes_abertura(aberturas[0].get("nome") if aberturas else "", orgao)
        if sigla_ab and (len(sigla) > 25 or sigla == orgao):  # "Secretaria de Estado ... do Amapá - AP" vira "SESA/AP"
            sigla = sigla_ab
        quem = f"{orgao} – {cargo}" if cargo else orgao
        base = {"banca": "AOCP", "orgao": orgao, "concurso_ref": f"aocp:{c['id']}"}
        fim = det.get("dataFinalInscricao")
        try:
            fim_dt = datetime.strptime(fim, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRT) if fim else None
        except ValueError:
            fim_dt = None
        ini = det.get("dataInicioInscricao")
        periodo = None
        if fim_dt:
            periodo = (f"de {datetime.strptime(ini, '%Y-%m-%d %H:%M:%S'):%d/%m} " if ini else "") + f"até {fim_dt:%d/%m/%Y às %Hh%M}"
        dados = {k: v for k, v in {"periodo_inscricao": periodo, "fim_inscricao": fim_dt.isoformat() if fim_dt else None, "sigla": sigla}.items() if v}
        chamada = _resumir(det.get("chamada"))
        m_vagas = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+)\s*(?:\([^)]{3,60}\)\s*)?vagas", det.get("chamada") or "", re.I)
        vagas = int(m_vagas.group(1).replace(".", "")) if m_vagas else None
        form = next((l.get("url") for l in det.get("links") or [] if re.search(r"inscri", _sa(l.get("nome"))) and l.get("url")), None)
        fase = {"NEW": "em_breve", "SUBSCRIBE": "inscricoes"}.get(st, "andamento")
        if fase == "inscricoes" and fim_dt and fim_dt < _agora():
            fase = "andamento"
        _ficha(id=f"aocp:{c['id']}", slug="-".join(x for x in [_slug(sigla), _slug(cargo) if cargo else "", "aocp", str(c["id"])] if x),
               banca="AOCP", sigla=sigla + (f" – {cargo}" if cargo else ""), vagas=vagas if vagas and vagas < 200000 else None, orgao=orgao.split(" – ", 1)[-1] if " – " in orgao else orgao, fase=fase,
               periodo_inscricao=periodo, fim_inscricao=fim_dt.isoformat() if fim_dt else None,
               inicio_inscricao=datetime.strptime(ini, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRT).isoformat() if ini and fim_dt else None,
               logo_url=det.get("logo") if str(det.get("logo") or "").startswith("https://") else None,
               ultima_publicacao=max(datas_pub).isoformat() if datas_pub else None,
               resumo=_resumir(det.get("chamada"), 700), cargos=[cargo] if cargo else [],
               links=[*([{"rotulo": "Edital de abertura", "url": abertura, "tipo": "edital"}] if abertura else []),
                      *([{"rotulo": "Conteúdo programático", "url": conteudo, "tipo": "conteudo"}] if conteudo and conteudo != abertura else []),
                      *([{"rotulo": "Fazer a inscrição", "url": form, "tipo": "inscricao"}] if form and fase == "inscricoes" else []),
                      {"rotulo": "Concursos no site do Instituto AOCP", "url": "https://www.institutoaocp.org.br/concursos/status/" +
                       {"em_breve": "novos", "inscricoes": "inscricoes-abertas"}.get(fase, "em-andamento"), "tipo": "banca"}])
        if st == "NEW" and not (fim_dt and fim_dt < _agora()):  # "novo" com inscrição já encerrada é página esquecida na banca
            out.append({**base, "id": f"aocp:{c['id']}:fase:novo", "tipo": "novo", "relevancia": 3, "fonte_url": pagina, "dados": dados,
                        "titulo": f"Novo concurso no Instituto AOCP: {quem}", "resumo": chamada, "publicado_em": _agora().isoformat()})
        if st == "SUBSCRIBE" and fim_dt and fim_dt > _agora():
            out.append({**base, "id": f"aocp:{c['id']}:fase:inscricoes", "tipo": "inscricoes", "relevancia": 3, "fonte_url": pagina, "dados": dados,
                        "titulo": f"Inscrições abertas: {quem}", "publicado_em": _agora().isoformat(),
                        "resumo": f"Inscrições {periodo}." + (f" {chamada}" if chamada else "")})
            if fim_dt - _agora() <= timedelta(days=3):
                out.append({**base, "id": f"aocp:{c['id']}:ultimos-dias", "tipo": "ultimos_dias", "relevancia": 3, "fonte_url": pagina, "dados": dados,
                            "titulo": f"Últimos dias: inscrições para {sigla}{f' ({cargo})' if cargo else ''} terminam em {fim_dt:%d/%m}", "publicado_em": _agora().isoformat(),
                            "resumo": f"Prazo final: {fim_dt:%d/%m/%Y às %Hh%M} (horário de Brasília)."})
        for p in pubs:
            m = RX_DATA.match(p.get("nome") or "")
            if not m or not p.get("url"):
                continue
            quando = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 12, tzinfo=BRT)
            if quando < limite:
                continue
            tipo, rel = classificar(p["nome"])
            out.append({**base, "id": f"aocp:{c['id']}:pub:{p.get('id') or p['url'][-60:]}", "tipo": tipo, "relevancia": rel, "fonte_url": p["url"],
                        "dados": {"sigla": sigla}, "titulo": f"{sigla}: {_limpar_desc(p['nome'])}", "resumo": None, "publicado_em": quando.isoformat()})
    return out


# ------------------------------------------------------------------ gravar
def gravar_fichas(banca):
    """Fichas da banca: atualiza as ativas e marca como encerrado o que saiu da lista de ativos."""
    fichas = {f["id"]: f for f in FICHAS if f["banca"] == banca}
    if not fichas:
        return
    try:
        alvos = {r["id"]: r["concurso_alvo"] for r in supa.select("ingestao_concursos", {"select": "id,concurso_alvo", "concurso_alvo": "not.is.null"})}
        # UF: deduzida da sigla/órgão; depois que o edital é lido por IA, vale a UF do edital (não é sobrescrita)
        lidos = {r["id"]: r["uf"] for r in supa.select("vitrine_concursos", {"select": "id,uf", "banca": f"eq.{banca}", "edital_lido": "not.is.null"})}
        linhas = [{**f, "concurso_alvo": alvos.get(f["id"]) if not str(alvos.get(f["id"]) or "").startswith("gerada") else None,
                   "uf": lidos[f["id"]] if f["id"] in lidos else inferir_uf(f["sigla"], f["orgao"])} for f in fichas.values()]
        vistos = {f["slug"] for f in linhas}
        # slug repetido (dois processos com o mesmo nome) ganha o id da banca no fim
        usados = {}
        for f in linhas:
            if f["slug"] in usados:
                f["slug"] = f"{f['slug']}-{f['id'].split(':', 1)[1].lower()}"[:120]
            usados[f["slug"]] = 1
        supa.upsert("vitrine_concursos", linhas, on_conflict="id")
        antigos = supa.select("vitrine_concursos", {"select": "id,fase,atualizado_em", "banca": f"eq.{banca}", "fase": "neq.encerrado"})
        for r in antigos:
            if r["id"] in fichas:
                continue
            if banca in LISTA_PARCIAL:
                # a página da banca só mostra parte dos concursos (FCC: inscrições abertas e últimos gabaritos); sumir de lá
                # não é encerrar. Inscrição sumida vira "em andamento"; sem aparecer por SILENCIO_DIAS, encerra.
                velho = (r.get("atualizado_em") or "") < (_agora() - timedelta(days=SILENCIO_DIAS)).isoformat()
                novo = "encerrado" if velho else ("andamento" if r["fase"] in ("inscricoes", "em_breve") else None)
                if novo and novo != r["fase"]:
                    supa.update("vitrine_concursos", {"id": f"eq.{r['id']}"}, {"fase": novo})
                continue
            supa.update("vitrine_concursos", {"id": f"eq.{r['id']}"}, {"fase": "encerrado"})
        print(f"concursos {banca}: {len(linhas)} ficha(s) atualizada(s)" + (f", {len(vistos)} slugs" if vistos else ""))
    except Exception as e:  # noqa: BLE001
        print(f"concursos {banca}: ERRO ao gravar fichas {e}")



def gerar(bancas):
    """bancas: [(nome, módulo, função)]. Notícia nova entra com a data do fato; a que já existe só tem o texto corrigido
    (título, tipo, relevância, resumo, dados), sem mudar a data, para não "subir" de novo na lista."""
    total = 0
    for nome, mod, fn in bancas:
        try:
            s = mod.sessao()
            itens = fn(s, mod)
        except Exception as e:  # noqa: BLE001  (uma banca fora do ar não derruba as outras)
            print(f"noticias {nome}: ERRO {e}"); continue
        for i in itens:
            i.setdefault("dados", {})
            i.setdefault("resumo", None)
        unicos = {i["id"]: i for i in itens}
        # a data original volta junto: o upsert do Postgres exige todas as colunas obrigatórias, e assim ela não muda
        vistos = {r["id"]: r["publicado_em"] for r in supa.select("noticias", {"select": "id,publicado_em", "banca": f"eq.{nome}"})}
        novas = [i for i in unicos.values() if i["id"] not in vistos]
        corrigir = [{**i, "publicado_em": vistos[i["id"]]} for i in unicos.values() if i["id"] in vistos]
        try:
            supa.upsert("noticias", novas, on_conflict="id", ignorar=True)
            supa.upsert("noticias", corrigir, on_conflict="id")
        except Exception as e:  # noqa: BLE001  (notícia nunca derruba a rodada do robô)
            print(f"noticias {nome}: ERRO ao gravar {e}"); continue
        # "concurso novo" que a banca já não lista como novo sai do ar (vira inscrições abertas, em andamento…)
        sumiram = [v for v in vistos if v.endswith(":fase:novo") or v.endswith(":fase:novos")]
        for v in sumiram:
            if v not in unicos:
                supa.update("noticias", {"id": f"eq.{v}"}, {"oculta": True})
        # avisar quem segue o concurso (dado de aluno) é da etapa 'alunos', só no repositório privado (rodar.alunos)
        total += len(novas)
        print(f"noticias {nome}: {len(novas)} nova(s), {len(corrigir)} revisada(s), de {len(itens)} fato(s) encontrados")
        gravar_fichas(nome)
        time.sleep(1)
    return total


# ------------------------------------------------------------------ acompanhar concurso
def avisar_seguidores(novas):
    """Quem acompanha o concurso recebe um aviso (sino) a cada publicação nova da banca."""
    por_conc = {}
    for n in novas:
        if n.get("concurso_ref") and n.get("tipo") != "novo":
            por_conc.setdefault(n["concurso_ref"], []).append(n)
    if not por_conc:
        return
    try:
        ids = ",".join(f'"{c}"' for c in por_conc)
        slugs = {r["id"]: r["slug"] for r in supa.select("vitrine_concursos", {"select": "id,slug", "id": f"in.({ids})"})}
        seg = supa.select("concurso_seguidores", {"select": "user_id,concurso_id", "concurso_id": f"in.({ids})"})
        avisos = [{"user_id": s_["user_id"], "tipo": "concurso", "questao_id": None, "comentario_id": None, "autor_nome": None,
                   "trecho": n["titulo"][:200], "link": f"/concurso/{slugs[s_['concurso_id']]}" if s_["concurso_id"] in slugs else "/noticias"}
                  for s_ in seg for n in por_conc[s_["concurso_id"]][:3]]
        if avisos:
            supa.upsert("avisos", avisos)
            print(f"acompanhar: {len(avisos)} aviso(s) para {len({a['user_id'] for a in avisos})} pessoa(s)")
    except Exception as e:  # noqa: BLE001  (aviso nunca derruba as notícias)
        print(f"acompanhar: ERRO {e}")


# ------------------------------------------------------------------ resumo do edital (IA)
PROMPT_EDITAL = """Você lê editais de concurso público brasileiro e devolve SÓ um JSON (sem comentários, sem markdown) com o que estiver ESCRITO no edital.
Campo sem informação no texto = null (ou lista vazia). Não invente nem estime.
{
 "uf": "sigla da UF onde ficam as vagas (ex.: PA); 'BR' se as vagas são no país todo",
 "cidades": ["cidades de lotação/vagas, no máximo 15"],
 "escolaridade": ["fundamental" | "medio" | "tecnico" | "superior" — os níveis exigidos pelos cargos],
 "salario_min": "menor remuneração inicial, formato R$ 1.234,56",
 "salario_max": "maior remuneração inicial, formato R$ 1.234,56",
 "taxa": "valor(es) da taxa de inscrição, ex.: R$ 90,00 (médio) e R$ 120,00 (superior)",
 "data_prova": "AAAA-MM-DD da prova objetiva (a primeira, se houver mais de uma)",
 "vagas": número total de vagas imediatas (inteiro),
 "cargos": [{"nome": "...", "escolaridade": "medio|superior|...", "vagas": "texto curto, ex.: 12 + CR", "salario": "R$ ..."}],
 "etapas": ["Prova objetiva", "Prova discursiva", "Títulos", "TAF"...],
 "disciplinas": ["disciplinas cobradas na prova objetiva (conhecimentos gerais e específicos), sem repetir, no máximo 25"]
}"""

ESCOLARIDADES = {"fundamental", "medio", "tecnico", "superior"}


def _texto_pdf(conteudo, limite=110_000):
    import io
    import pdfplumber
    partes, n = [], 0
    with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
        for pg in pdf.pages:
            t = pg.extract_text() or ""
            partes.append(t); n += len(t)
            if n >= limite:
                break
    return "\n".join(partes)[:limite]


def _json_da_resposta(txt):
    m = re.search(r"\{.*\}", txt or "", re.S)
    return json.loads(m.group(0)) if m else None


def ler_editais(maximo=30):
    """Lê o edital de abertura de cada concurso ativo (uma vez por edital) e guarda o resumo na ficha."""
    import requests
    import ia
    if not ia.disponivel():
        print("editais: IA desligada ou sem crédito; os editais ficam para quando houver IA"); return
    try:
        fichas = supa.select("vitrine_concursos", {"select": "id,sigla,uf,fase,links,edital_lido,fim_inscricao,ultima_publicacao", "fase": "neq.encerrado"})
        # quem ainda dá para se inscrever primeiro; depois os em andamento mais recentes
        fichas.sort(key=lambda f: ({"inscricoes": 0, "em_breve": 1}.get(f["fase"], 2), "".join(chr(255 - ord(c)) for c in (f.get("ultima_publicacao") or f.get("fim_inscricao") or ""))))
    except Exception as e:  # noqa: BLE001
        print(f"editais: ERRO ao listar {e}"); return
    fila = []
    for f in fichas:
        url = next((l["url"] for l in f.get("links") or [] if l.get("tipo") == "edital"), None)
        if url and url != f.get("edital_lido"):
            fila.append((f, url))
    print(f"editais: {len(fila)} edital(is) para ler; lendo até {maximo}")
    for f, url in fila[:maximo]:
        try:
            r = requests.get(url, timeout=90, headers={"User-Agent": "FixaConcursos-robo/1.0 (+https://www.fixaconcursos.com.br)"})
            if r.status_code != 200 or not r.content.startswith(b"%PDF"):
                supa.update("vitrine_concursos", {"id": f"eq.{f['id']}"}, {"edital_lido": url, "edital": {"erro": f"não é PDF (HTTP {r.status_code})"}})
                print(f"  {f['sigla']}: edital não é PDF, pulado"); continue
            texto = _texto_pdf(r.content)
            if len(texto) < 1500:
                supa.update("vitrine_concursos", {"id": f"eq.{f['id']}"}, {"edital_lido": url, "edital": {"erro": "PDF sem texto (imagem)"}})
                print(f"  {f['sigla']}: PDF sem texto, pulado"); continue
            resp, _ = ia.chamar("edital", f"EDITAL ({f['sigla']}):\n\n{texto}", max_tokens=3000, system=PROMPT_EDITAL, rapido=True)
            d = _json_da_resposta(resp)
            if not isinstance(d, dict):
                print(f"  {f['sigla']}: resposta sem JSON, fica para a próxima"); continue
            uf = str(d.get("uf") or "").upper().strip()
            data = str(d.get("data_prova") or "")
            esc = [e for e in (str(x).lower().replace("é", "e") for x in d.get("escolaridade") or []) if e in ESCOLARIDADES]
            vals = {"edital_lido": url, "edital": {k: d.get(k) for k in ("cargos", "etapas", "disciplinas", "vagas", "salario_max")},
                    "cidades": [str(c)[:60] for c in (d.get("cidades") or [])][:15], "escolaridade": sorted(set(esc), key=["fundamental", "medio", "tecnico", "superior"].index),
                    "salario_min": d.get("salario_min") or None, "taxa": (str(d["taxa"])[:160] if d.get("taxa") else None),
                    "data_prova": data if re.fullmatch(r"20\d\d-\d\d-\d\d", data) else None}
            if re.fullmatch(r"[A-Z]{2}", uf):
                vals["uf"] = uf
            supa.update("vitrine_concursos", {"id": f"eq.{f['id']}"}, vals)
            print(f"  {f['sigla']}: edital lido (UF {vals.get('uf') or f.get('uf')}, {len(d.get('cargos') or [])} cargo(s), prova {vals['data_prova']})")
        except Exception as e:  # noqa: BLE001  (um edital ruim não para os outros)
            print(f"  {f['sigla']}: ERRO {e}")
        time.sleep(1)
