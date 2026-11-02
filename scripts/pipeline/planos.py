"""Plano de estudo automático a partir do edital (pedido pelo aluno na página do concurso).

Fluxo: o aluno escolhe o cargo e pede o plano (tabela pedidos_plano) -> nesta etapa o robô baixa o edital de abertura,
separa o quadro de provas e o conteúdo programático do cargo, e a IA devolve disciplinas (questões/pontos) e tópicos
já ligados à árvore universal de assuntos. Com isso o concurso de estudo herda as questões do banco pelo assunto
(lib/dados.ts ligarPorAssunto). Um plano por (concurso da vitrine, cargo): o 2º aluno que pedir o mesmo recebe na hora.
"""
import hashlib, json, re, time, unicodedata

import supa

MAX_POR_RODADA = 5
MAX_CATALOGO = 15        # planos novos do catálogo por rodada (cada um é uma leitura de edital por IA, ~US$ 0,11)
MAX_CARGOS_CONCURSO = 40 # concursos com centenas de cargos (prefeituras, professores) entram pelos 40 primeiros
_EDITAIS = {}            # texto do edital já baixado nesta rodada
UA = {"User-Agent": "FixaConcursos-robo/1.0 (+https://www.fixaconcursos.com.br)"}


def _sa(t):
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()


def _slug(t, n=40):
    return re.sub(r"[^a-z0-9]+", "-", _sa(t)).strip("-")[:n].strip("-")


def id_plano(vitrine_id, cargo):
    """Nome curto: igual ao de antes ('auto-cebraspe-...-psicologo'). Nome longo ganha um hash: 'Professor – Matemática'
    e 'Professor – Física' não podem cair no mesmo plano por terem os 30 primeiros caracteres iguais."""
    base = f"auto-{_slug(vitrine_id.replace(':', '-'), 30)}-"
    completo = _slug(cargo, 200)
    if len(completo) <= 30:
        return base + completo
    return base + _slug(cargo, 23) + "-" + hashlib.sha1(_sa(cargo).strip().encode()).hexdigest()[:6]


def _normalizado(texto):
    """Texto sem acento e minúsculo + mapa de posição para o original (para recortar trechos)."""
    out, mapa = [], []
    for i, ch in enumerate(texto):
        n = _sa(ch)
        out.append(n); mapa.extend([i] * len(n))
    return "".join(out), mapa


RX_QUADRO = re.compile(r"quadro (?:de|das) provas|total de questoes|das fases do concurso|distribuicao (?:das|de) questoes|"
                       r"quantidade de questoes|n[ou]?\.? ?de questoes")
RX_CP = r"conteudos? programaticos?|objetos? de avaliacao"
MARCA_ANEXO = "\n\n===== ANEXO PUBLICADO À PARTE: CONTEÚDO PROGRAMÁTICO =====\n"


def _inicio_conteudo(norm):
    """Onde começa o conteúdo programático: o anexo publicado à parte; senão o título do anexo dentro do edital
    (o último, porque o corpo do edital costuma citar o anexo antes: 'conforme Anexo III - Conteúdo Programático, sendo...')."""
    i = norm.find(_sa(MARCA_ANEXO).strip())
    if i >= 0:
        return i
    titulos = [m for m in re.finditer(r"anexo\s+[ivx\d]+\s*[-–:.]?\s*(?:d[oa]s?\s+)?(?:" + RX_CP + ")", norm)
               if not re.match(r"[^\n]{0,15},\s*sendo|[^\n]{0,15}\s+deste edital", norm[m.end():m.end() + 40])]
    if titulos:
        return titulos[-1].start()
    m = list(re.finditer(RX_CP, norm[int(len(norm) * 0.3):]))
    return int(len(norm) * 0.3) + m[0].start() if m else None


def recortar(texto, cargo, limite=95_000):
    """Pedaços do edital que interessam ao plano: começo, quadro de provas, conhecimentos gerais e a parte do cargo."""
    norm, mapa = _normalizado(texto)
    orig = lambda i: mapa[min(i, len(mapa) - 1)] if mapa else 0  # noqa: E731
    pedacos = [(0, 12_000)]
    m = RX_QUADRO.search(norm)
    if m:
        pedacos.append((orig(m.start()) - 3_000, orig(m.start()) + 16_000))
    ini = _inicio_conteudo(norm)
    if ini is not None:
        o = orig(ini)
        if len(texto) - o <= 60_000:
            pedacos.append((o, len(texto)))  # anexo curto: vai inteiro
        else:
            pedacos.append((o, o + 14_000))
            for k, mg in enumerate(re.finditer(r"conhecimentos (?:gerais|basicos|comuns)", norm[ini:])):
                if k >= 3:
                    break
                p = orig(ini + mg.start()); pedacos.append((p - 300, p + 7_000))
            alvo = _sa(re.sub(r"\s*\(.*?\)", "", cargo)).strip()
            achou = 0
            for mc in re.finditer(re.escape(alvo) if alvo else r"$^", norm[ini:]):
                p = orig(ini + mc.start()); pedacos.append((p - 500, p + 15_000)); achou += 1
                if achou >= 2:
                    break
            palavras = [w for w in re.split(r"\W+", alvo) if len(w) > 3][:4]
            if not achou and palavras:  # nome do cargo quebrado em linhas ou escrito diferente no anexo
                mc = re.compile(r"[\s\S]{0,80}".join(re.escape(w) for w in palavras)).search(norm, ini)
                if mc:
                    p = orig(mc.start()); pedacos.append((p - 500, p + 15_000))
    pedacos = sorted((max(0, a), min(len(texto), b)) for a, b in pedacos)
    juntos = []
    for a, b in pedacos:
        if juntos and a <= juntos[-1][1]:
            juntos[-1] = (juntos[-1][0], max(juntos[-1][1], b))
        else:
            juntos.append((a, b))
    return "\n[...]\n".join(texto[a:b] for a, b in juntos)[:limite]


def _lista_assuntos():
    linhas = supa.select("assuntos", {"select": "id,nome,disciplina", "order": "ordem"})
    return {l["id"]: l for l in linhas}, "\n".join(f"{l['id']} — {l['nome']}" for l in linhas)


PROMPT = """Você monta planos de estudo a partir de editais de concurso público brasileiro.
Recebe trechos do edital e o CARGO escolhido. Devolva SÓ um JSON (sem markdown) com o que está ESCRITO no edital para esse cargo:
{
 "cargo_encontrado": true/false (o conteúdo programático do cargo está nos trechos?),
 "data_prova": "AAAA-MM-DD" ou null,
 "duracao_horas": número ou null,
 "disciplinas": [{"codigo": "PT", "nome": "Língua Portuguesa", "bloco": "gerais" | "especificos", "questoes": n ou null, "pontos": n ou null}],
 "topicos": [{"disciplina": "PT", "titulo": "texto do item do conteúdo programático, resumido em até 160 caracteres", "assuntos": ["ids da lista"]}]
}
Regras:
- disciplinas = as da prova objetiva DESTE cargo, com número de questões e pontos do quadro de provas (null se o quadro não estiver nos trechos).
  codigo: 2 a 4 letras maiúsculas, único (PT, RL, INF, LE, CE, TI...).
- topicos: um por item/assunto do conteúdo programático do cargo (agrupe itens muito curtos da mesma linha), na ordem do edital, no máximo 70.
- assuntos: 0 a 3 ids da LISTA DE ASSUNTOS abaixo que correspondem ao tópico. Use SÓ ids da lista; se nenhum corresponder, [].
- Não invente conteúdo que não está nos trechos. Se o cargo não aparece no conteúdo programático, "cargo_encontrado": false e topicos dos conhecimentos gerais que estiverem lá.

LISTA DE ASSUNTOS (id — nome):
"""


def _json(txt):
    m = re.search(r"\{.*\}", txt or "", re.S)
    return json.loads(m.group(0)) if m else None


def montar(vitrine, cargo, assuntos, lista_txt):
    """-> (concurso_id, n_topicos, n_ligacoes). Levanta exceção com mensagem legível se não der."""
    import requests
    import ia
    from noticias import _texto_pdf
    url = next((l["url"] for l in vitrine.get("links") or [] if l.get("tipo") == "edital"), None)
    url_cp = next((l["url"] for l in vitrine.get("links") or [] if l.get("tipo") == "conteudo"), None)
    if not url:
        raise ValueError("este concurso ainda não tem edital de abertura publicado")
    chave = (url, url_cp)
    if chave not in _EDITAIS:
        r = requests.get(url, timeout=120, headers=UA)
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            raise ValueError("o edital não está em PDF legível")
        texto = _texto_pdf(r.content, limite=3_000_000)
        if len(texto) < 3000:
            raise ValueError("o PDF do edital é imagem (sem texto)")
        if url_cp:
            rc = requests.get(url_cp, timeout=120, headers=UA)
            if rc.status_code == 200 and rc.content.startswith(b"%PDF"):
                texto += MARCA_ANEXO + _texto_pdf(rc.content, limite=3_000_000)
        _EDITAIS[chave] = texto
    texto = _EDITAIS[chave]
    trecho = recortar(texto, cargo)
    resp, parada = ia.chamar("plano", f"CARGO: {cargo}\nCONCURSO: {vitrine['sigla']} – {vitrine.get('orgao') or ''}\n\nTRECHOS DO EDITAL:\n{trecho}",
                             max_tokens=12000, system=PROMPT + lista_txt)
    d = _json(resp)
    if not isinstance(d, dict):
        raise ValueError(f"leitura do edital falhou ({parada})")

    disc = {}
    for x in d.get("disciplinas") or []:
        cod = re.sub(r"[^A-Z]", "", str(x.get("codigo") or "").upper())[:4]
        if len(cod) < 2 or cod in disc:
            continue
        q = x.get("questoes") if isinstance(x.get("questoes"), (int, float)) and x["questoes"] > 0 else None
        p = x.get("pontos") if isinstance(x.get("pontos"), (int, float)) and x["pontos"] > 0 else None
        disc[cod] = {"nome": str(x.get("nome") or cod)[:80], "bloco": "especificos" if x.get("bloco") == "especificos" else "gerais",
                     "questoes": int(q or p or 10), "pontos": float(p or q or 10), **({} if q or p else {"estimado": True})}
    topicos = []
    for t in d.get("topicos") or []:
        cod = re.sub(r"[^A-Z]", "", str(t.get("disciplina") or "").upper())[:4]
        titulo = re.sub(r"\s+", " ", str(t.get("titulo") or "")).strip()[:200]
        if cod in disc and len(titulo) >= 3:
            topicos.append((cod, titulo, [a for a in (t.get("assuntos") or [])[:3] if a in assuntos]))
    topicos = topicos[:80]
    usadas = {c for c, _, _ in topicos}
    disc = {k: v for k, v in disc.items() if k in usadas}
    if len(topicos) < 3 or not disc:
        raise ValueError("não encontrei o conteúdo programático deste cargo no edital")

    cid = id_plano(vitrine["id"], cargo)
    pref = "X" + hashlib.sha1(cid.encode()).hexdigest()[:5].upper()
    data = str(d.get("data_prova") or vitrine.get("data_prova") or "")
    data_prova = f"{data}T12:00:00+00:00" if re.fullmatch(r"20\d\d-\d\d-\d\d", data) else None
    dur = d.get("duracao_horas") if isinstance(d.get("duracao_horas"), (int, float)) and 0 < d["duracao_horas"] <= 10 else 4
    supa.upsert("concursos", [{
        "id": cid, "nome": f"{vitrine['sigla'].split(' – ')[0]} – {cargo}"[:120], "orgao": vitrine.get("orgao"), "banca": vitrine["banca"],
        "data_prova": data_prova, "ativo": True,
        "estrutura": {"cargos": [{"id": "cargo", "nome": cargo}], "escala": round(sum(v["pontos"] for v in disc.values()), 2), "regras": {},
                      "disciplinas": disc, "duracao_horas": dur, "permite_ambos": False,
                      "automatico": True, "vitrine_id": vitrine["id"], "uf": vitrine.get("uf"), "vitrine_slug": vitrine.get("slug"), "fonte_edital": url, "fonte_conteudo": url_cp,
                      "cargo_encontrado": bool(d.get("cargo_encontrado"))}}], on_conflict="id")
    # refaz do zero (pedido repetido depois de um erro, ou edital retificado)
    supa.delete("topico_assuntos", {"topico_id": f"like.{pref}-*"})
    supa.delete("topicos", {"concurso_id": f"eq.{cid}"})
    linhas, lig, cont = [], [], {}
    for cod, titulo, ass in topicos:
        cont[cod] = cont.get(cod, 0) + 1
        tid = f"{pref}-{cod}{cont[cod]:02d}"
        linhas.append({"id": tid, "concurso_id": cid, "disciplina": cod, "titulo": titulo, "incidencia": {"cargo": 1}, "referencias": []})
        lig += [{"topico_id": tid, "assunto_id": a} for a in dict.fromkeys(ass)]
    supa.upsert("topicos", linhas, on_conflict="id")
    supa.upsert("topico_assuntos", lig)
    return cid, len(linhas), len(lig)


def processar(maximo=MAX_POR_RODADA):
    import ia
    try:
        pend = supa.select("pedidos_plano", {"select": "id,user_id,vitrine_id,cargo", "status": "eq.pendente", "order": "criado_em"})
    except Exception as e:  # noqa: BLE001  (tabela ainda não criada / fora do ar: não derruba a rodada)
        print(f"planos: ERRO ao listar pedidos {e}"); return
    if not pend:
        print("planos: nenhum pedido"); return
    if not ia.disponivel():
        print(f"planos: {len(pend)} pedido(s) esperando — IA desligada ou sem crédito"); return
    grupos = {}
    for p in pend:
        grupos.setdefault((p["vitrine_id"], p["cargo"]), []).append(p)
    existentes = {c["id"] for c in supa.select("concursos", {"select": "id", "id": "like.auto-*"})}
    assuntos, lista_txt = _lista_assuntos()
    feitos = 0
    for (vid, cargo), pedidos in grupos.items():
        cid = id_plano(vid, cargo)
        ids = ",".join(str(p["id"]) for p in pedidos)
        try:
            if cid not in existentes:
                if feitos >= maximo:
                    continue
                feitos += 1
                v = supa.select("vitrine_concursos", {"select": "id,slug,sigla,orgao,banca,uf,links,data_prova", "id": f"eq.{vid}"})
                if not v:
                    raise ValueError("concurso não encontrado")
                cid, nt, nl = montar(v[0], cargo, assuntos, lista_txt)
                existentes.add(cid)
                print(f"planos: {cid} montado ({nt} tópicos, {nl} ligações com o banco)")
            supa.update("pedidos_plano", {"id": f"in.({ids})"}, {"status": "pronto", "concurso_id": cid, "erro": None, "atualizado_em": _agora()})
            supa.upsert("avisos", [{"user_id": p["user_id"], "tipo": "concurso", "questao_id": None, "comentario_id": None, "autor_nome": None,
                                    "trecho": f"Seu plano de estudo para {cargo} está pronto", "link": f"/concursos?plano={cid}"} for p in pedidos])
        except Exception as e:  # noqa: BLE001  (um edital ruim não para os outros pedidos)
            msg = str(e)[:300] if isinstance(e, ValueError) else "falha temporária ao ler o edital; peça de novo"
            print(f"planos: {vid} / {cargo}: ERRO {e}")
            supa.update("pedidos_plano", {"id": f"in.({ids})"}, {"status": "erro", "erro": msg, "atualizado_em": _agora()})
        time.sleep(1)


def _agora():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ catálogo automático
_SIGLAS = {"ti": "tecnologia informacao", "adm": "administrativo", "aux": "auxiliar", "tec": "tecnico"}


def _palavras(t):
    t = re.sub(r"\b(ti|adm|aux|tec)\b", lambda m: _SIGLAS[m.group(1)], _sa(t))
    return [w for w in re.findall(r"[a-z]{3,}", t) if w not in {"cargo", "area", "nivel", "dos", "das"}]


def _coberto(cargo, nomes):
    """Cargo já atendido por um plano montado à mão no mesmo concurso (ex.: IFPA – Técnico de TI)?
    Mesmo cargo-base (1ª palavra: técnico, analista...) e quase as mesmas palavras."""
    p = _palavras(cargo)
    for n in nomes:
        q = _palavras(n)
        if p and q and p[0] == q[0] and len(set(p) & set(q)) / len(set(p) | set(q)) >= 0.6:
            return True
    return False


def _cargos_do_edital(v):
    vistos, out = set(), []
    for c in ((v.get("edital") or {}).get("cargos") or []):
        nome = re.sub(r"\s+", " ", str((c or {}).get("nome") if isinstance(c, dict) else c or "")).strip(" -–:;.")[:160]
        k = _sa(nome)
        if len(nome) >= 3 and k not in vistos:
            vistos.add(k); out.append(nome)
    return out[:MAX_CARGOS_CONCURSO]


def catalogo(maximo=MAX_CATALOGO):
    """Monta, sem ninguém pedir, o plano de cada cargo dos concursos com inscrição aberta / edital em breve / prova
    marcada e edital já lido. A tela "Escolha seu concurso" mostra todos. Respeita o teto mensal de IA."""
    import ia
    from datetime import date
    hoje = date.today().isoformat()
    try:
        vit = supa.select("vitrine_concursos", {"select": "id,slug,sigla,orgao,banca,uf,links,data_prova,fase,vagas,edital,edital_lido",
                                                 "fase": "neq.encerrado", "edital_lido": "not.is.null"})
        cat = {(r["vitrine_id"], r["cargo"]): r for r in supa.select("planos_catalogo", {"select": "*"})}
        manuais = supa.select("concursos", {"select": "id,estrutura"})
    except Exception as e:  # noqa: BLE001
        print(f"catalogo: ERRO ao ler {e}"); return
    cobertos = {}
    for c in manuais:
        e = c.get("estrutura") or {}
        if e.get("vitrine_id") and not e.get("automatico"):
            cobertos.setdefault(e["vitrine_id"], []).extend(x.get("nome", "") for x in e.get("cargos") or [])
    elegiveis = [v for v in vit if v["fase"] in ("inscricoes", "em_breve") or (v.get("data_prova") or "") >= hoje]
    vivos = {v["id"] for v in elegiveis}

    # 1) registra os cargos novos (pendente) e marca os já cobertos por plano feito à mão
    novos = []
    for v in elegiveis:
        if not any(l.get("tipo") == "edital" for l in v.get("links") or []) or (v.get("edital") or {}).get("erro"):
            continue
        for cargo in _cargos_do_edital(v):
            if (v["id"], cargo) in cat:
                continue
            st = "ignorado" if _coberto(cargo, cobertos.get(v["id"], [])) else "pendente"
            novos.append({"vitrine_id": v["id"], "cargo": cargo, "status": st, "erro": "já existe plano montado à mão" if st == "ignorado" else None})
    if novos:
        supa.upsert("planos_catalogo", novos, on_conflict="vitrine_id,cargo", ignorar=True)
        for n in novos:
            cat[(n["vitrine_id"], n["cargo"])] = {**n, "tentativas": 0}
    # concurso que saiu da vitrine ativa: tira as linhas pendentes (os planos prontos ficam para quem já estuda)
    velhos = [k for k, r in cat.items() if r["status"] == "pendente" and k[0] not in vivos]
    for vid, cargo in velhos:
        supa.delete("planos_catalogo", {"vitrine_id": f"eq.{vid}", "cargo": f"eq.{cargo}"})

    # 2) monta os pendentes: inscrições abertas primeiro, depois os maiores concursos
    ordem = {v["id"]: ({"inscricoes": 0, "em_breve": 1}.get(v["fase"], 2), -(v.get("vagas") or 0)) for v in elegiveis}
    fila = sorted((r for k, r in cat.items() if r["status"] == "pendente" and k[0] in vivos and (r.get("tentativas") or 0) < 3),
                  key=lambda r: ordem.get(r["vitrine_id"], (9, 0)))
    existentes = {c["id"] for c in manuais}
    por_id = {v["id"]: v for v in elegiveis}
    assuntos, lista_txt = _lista_assuntos()
    feitos = prontos = tentados = 0
    for r in fila:
        if feitos >= maximo:
            break
        if not ia.disponivel():
            print("catalogo: IA indisponível ou teto mensal atingido; continua na próxima rodada"); break
        vid, cargo = r["vitrine_id"], r["cargo"]
        tentados += 1
        cid = id_plano(vid, cargo)
        filtro = {"vitrine_id": f"eq.{vid}", "cargo": f"eq.{cargo}"}
        try:
            if cid not in existentes:
                feitos += 1
                cid, nt, nl = montar(por_id[vid], cargo, assuntos, lista_txt)
                existentes.add(cid)
                print(f"catalogo: {cid} ({nt} tópicos, {nl} ligações)")
            supa.update("planos_catalogo", filtro, {"status": "pronto", "concurso_id": cid, "erro": None, "atualizado_em": _agora()})
            prontos += 1
        except Exception as e:  # noqa: BLE001  (um edital ruim não para os outros)
            definitivo = isinstance(e, ValueError)
            print(f"catalogo: {vid} / {cargo}: ERRO {e}")
            supa.update("planos_catalogo", filtro, {"status": "erro" if definitivo else "pendente", "erro": str(e)[:300],
                                                    "tentativas": (r.get("tentativas") or 0) + 1, "atualizado_em": _agora()})
        time.sleep(1)
    print(f"catalogo: {prontos} plano(s) prontos nesta rodada; {len(fila) - tentados} cargo(s) ainda na fila")

