"""Pipeline de ingestão de provas oficiais (Instituto AOCP, Cebraspe; CETAP só por envio manual).

Uso:  python scripts/pipeline/rodar.py vigiar|processar|enriquecer|gerar|classificar|auditar|exportar|tudo
  vigiar     lista os concursos da banca e registra os novos em ingestao_concursos
  processar  para concursos marcados com interesse: baixa cadernos + gabarito definitivo, extrai as questões
             e coloca na fila de revisão (ingestao_questoes, status pendente)
  enriquecer (opcional, precisa de ANTHROPIC_API_KEY e ANTHROPIC_MODEL) sugere tópico do edital e escreve o comentário
  auditar    (IA) marca questões cuja lei/versão mudou depois da prova (desatualizadas)
  gerar      (IA) cria questões inéditas no estilo da banca para os tópicos do edital com poucas questões (vão para a fila)
  estruturar (IA) separa os textos de apoio em parágrafos como na prova (a IA só aponta linhas; as palavras são conferidas
             e a contagem é checada contra as questões que citam "no quinto parágrafo")
  classificar (IA) põe cada questão sem assunto na árvore universal (conteudo/assuntos/arvore.json)
  exportar   grava as questões aprovadas (questoes.via_ingestao) em conteudo/questoes/ingest/ para versionar no git
Nada é publicado para os alunos sem aprovação no Admin › Ingestão.
"""
import hashlib, json, os, re, sys, tempfile, traceback, unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
# raiz do repositório com conteudo/ (o exportar grava ali). No privado, o robô é baixado numa subpasta: FIXA_RAIZ aponta a raiz.
RAIZ = Path(os.environ.get("FIXA_RAIZ") or AQUI.parents[1]).resolve()
sys.path.insert(0, str(AQUI))
import aocp, cebraspe, cetap, fgv, ia, noticias, planos, supa, textos, vitrine_extra  # noqa: E402
from extrair import extrair_questoes, recortar_figuras  # noqa: E402
from gabarito_organizador import AVISO_PRELIMINAR, conferir_com_pdf, normalizar_gabarito_organizador, precisa_conferir, preliminar  # noqa: E402
from gabarito_texto import respostas_do_cargo  # noqa: E402
from ia_extrair import SemIA, limpar_carimbos  # noqa: E402

CFG = json.loads((AQUI / "config.json").read_text(encoding="utf-8"))
agora = lambda: datetime.now(timezone.utc).isoformat()


def slug(s, n=40):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:n].strip("-")


def texto_cargos(c):
    v = c.get("cargos")
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if v else ""


# ---------------------------------------------------------------- vigiar
def vigiar():
    _vigiar()
    noticiar()


def alunos():
    """Tudo que lê ou escreve dado de aluno/assinatura (só no repositório PRIVADO, ROBO_PRIVADO=1; o log do robô público é
    público): avisos de mensalidade, planos pedidos pelos alunos e aviso a quem segue um concurso com notícia nova."""
    if not supa.privado():
        sys.exit(MSG_SO_PRIVADO.format(etapa="alunos"))
    avisar_mensalidades()
    planos.processar()  # planos de estudo pedidos pelos alunos na página do concurso (até 5 por rodada)
    avisar_seguidores_desde_ultima()


def avisar_seguidores_desde_ultima(janela_max_h=48):
    """Notícias criadas desde a última etapa 'alunos' (o robô público as grava) -> aviso a quem segue o concurso."""
    try:
        ult = supa.select("robo_diagnostico", {"select": "criado_em", "etapa": "eq.alunos", "order": "criado_em.desc", "limit": "1"})
        desde = max(ult[0]["criado_em"] if ult else "", (datetime.now(timezone.utc) - timedelta(hours=janela_max_h)).isoformat())
        novas = supa.select("noticias", {"select": "id,tipo,titulo,concurso_ref", "criado_em": f"gt.{desde}", "oculta": "eq.false"})
        noticias.avisar_seguidores(novas)
        supa.upsert("robo_diagnostico", [{"banca": "ROBO", "etapa": "alunos", "url": None, "status_http": None, "bytes": None,
                                          "titulo": f"{len(novas)} notícia(s) nova(s) desde a última rodada", "itens": len(novas), "detalhe": None}])
    except Exception as e:  # noqa: BLE001  (aviso nunca derruba a rodada)
        print(f"acompanhar: ERRO {type(e).__name__}")


def noticiar():
    """Notícias do dia a partir das páginas oficiais (Cebraspe, Instituto AOCP, FCC e ABCP) e fichas da FADESP e da FGV."""
    n = noticias.gerar([("CEBRASPE", cebraspe, noticias.cebraspe_noticias), ("AOCP", aocp, noticias.aocp_noticias),
                        ("FCC", vitrine_extra, vitrine_extra.fcc_noticias), ("ABCP", vitrine_extra, vitrine_extra.abcp_noticias),
                        ("FADESP", vitrine_extra, vitrine_extra.fadesp_noticias), ("FGV", vitrine_extra, vitrine_extra.fgv_noticias)])
    print(f"noticias: {n} nova(s) no total")
    noticias.ler_editais()  # resumo do edital por IA (só edital ainda não lido; até 30 por rodada)
    # planos pedidos pelos alunos: etapa 'alunos' (repositório privado)
    planos.catalogo()  # catálogo: plano de cada cargo dos concursos abertos, montado sem ninguém pedir (até 15 por rodada)


def avisar_mensalidades():
    """Quem paga com Pix/boleto recebe aviso no sino 5 dias e 1 dia antes do vencimento (cartão é debitado sozinho).
    Cobrança desligada (config.cobranca_ativa = false, lançamento grátis): nenhum aviso de mensalidade."""
    try:
        try:
            chave = supa.select("config", {"select": "valor", "chave": "eq.cobranca_ativa"})
        except Exception:  # noqa: BLE001  (banco sem a tabela config = cobrança ativa, como antes)
            chave = None
        if chave and chave[0].get("valor") is False:
            print("mensalidades: cobrança desligada (acesso fundador); sem avisos"); return
        hoje = datetime.now(timezone.utc).date()
        for a in supa.select("assinaturas", {"select": "user_id,pago_ate,forma", "status": "eq.ativa", "pago_ate": "not.is.null"}):
            if a.get("forma") == "CREDIT_CARD":
                continue
            venc = datetime.fromisoformat(a["pago_ate"].replace("Z", "+00:00")).date() - timedelta(days=1)
            d = (venc - hoje).days
            if d in (5, 1):
                supa.upsert("avisos", [{"user_id": a["user_id"], "tipo": "concurso", "questao_id": None, "comentario_id": None, "autor_nome": None,
                                        "trecho": f"Sua mensalidade do FixaConcursos vence em {d} {'dia' if d == 1 else 'dias'} ({venc:%d/%m}). Pague por Pix ou boleto.",
                                        "link": "/assinatura"}])
                print(f"mensalidades: aviso de {d} dia(s) enviado")
    except Exception as e:  # noqa: BLE001  (aviso nunca derruba a rodada)
        print(f"mensalidades: ERRO {e}")


def diagnosticar(banca, mod, s, motivo):
    """Grava em robo_diagnostico o que o site da banca respondeu (dá para ver pelo banco, sem abrir o log do Actions)."""
    print(f"vigiar {banca}: {motivo}; gravando diagnóstico")
    if not hasattr(mod, "diagnosticar"):
        supa.upsert("robo_diagnostico", [{"banca": banca, "etapa": "vigiar", "detalhe": motivo[:1000]}]); return
    try:
        linhas = mod.diagnosticar(s)
        campos = ("banca", "etapa", "url", "status_http", "bytes", "titulo", "itens", "detalhe")
        linhas = [{k: l.get(k) for k in campos} for l in linhas]  # a API exige as mesmas colunas em todas as linhas
        for l in linhas:
            l["detalhe"] = f"{motivo} | {l.get('detalhe') or ''}"[:2000]
        supa.upsert("robo_diagnostico", linhas)
    except Exception as e:  # noqa: BLE001
        print(f"  diagnóstico falhou: {e}")


def _vigiar():
    # CETAP fora: o site bloqueia robôs (desafio do Cloudflare, 403). Provas da CETAP entram pelo envio manual no Admin.
    for nome, fn in (("AOCP", lambda: vigiar_banca("AOCP", aocp)), ("CEBRASPE", lambda: vigiar_banca("CEBRASPE", cebraspe)),
                     ("FGV", lambda: vigiar_banca("FGV", fgv, interesse_todos=True))):
        try:
            fn()
        except Exception as e:  # noqa: BLE001  (uma banca fora do ar não derruba as outras)
            print(f"vigiar {nome}: ERRO {e}"); traceback.print_exc()


def vigiar_banca(banca, mod, interesse_todos=False):
    """interesse_todos: a lista da banca não traz cargos (FGV); todo concurso entra na fila e o filtro de cargos
    é aplicado na página de cada um, no processar (sem IA quando nenhum cargo passa)."""
    s = mod.sessao()
    try:
        lista = mod.listar(s)
    except Exception as e:  # noqa: BLE001
        diagnosticar(banca, mod, s, f"listar falhou: {e}")
        raise
    if not lista:
        diagnosticar(banca, mod, s, "listar voltou vazio")
    pref = banca.lower()
    existentes = {r["id"] for r in supa.select("ingestao_concursos", {"select": "id", "banca": f"eq.{banca}"})}
    primeira_vez = not existentes
    linhas, novos = [], []
    for c in lista:
        cid = f"{pref}:{c['id']}"
        linha = {"id": cid, "banca": banca, "externo_id": str(c["id"]), "nome": c.get("nome") or "", "edital": c.get("edital"),
                 "status": c.get("status"), "cargos": texto_cargos(c)[:4000], "data_prova": str(c.get("dataProva") or "") or None,
                 "tipo_processo": c.get("tipoProcesso"), "atualizado_em": agora()}
        if cid not in existentes:
            linha.update(interesse=interesse_todos, concurso_alvo=None, filtro_cargos=None)
            novos.append(linha)
            if not primeira_vez:  # na 1ª execução só cataloga, sem marcar centenas de concursos antigos
                if not linha["cargos"] and hasattr(mod, "cargos_de"):
                    try:
                        linha["cargos"] = texto_cargos({"cargos": mod.cargos_de(s, c["id"])})[:4000]
                    except Exception:  # noqa: BLE001
                        pass
                alvo = f"{linha['nome']} {linha['cargos']}"
                for regra in CFG.get("auto", []):
                    if regra.get("bancas") and banca not in regra["bancas"]:
                        continue
                    if re.search(regra["regex"], alvo, re.I):
                        linha.update(interesse=True, concurso_alvo=regra.get("concurso_alvo"), filtro_cargos=regra.get("filtro_cargos"))
                        break
        linhas.append(linha)
    # upsert parcial: colunas de controle (interesse/alvo/filtro) só entram nas linhas novas
    novos_ids = {n["id"] for n in novos}
    supa.upsert("ingestao_concursos", [l for l in linhas if l["id"] in novos_ids], on_conflict="id")
    base = [{k: v for k, v in l.items() if k not in ("interesse", "concurso_alvo", "filtro_cargos")} for l in linhas if l["id"] not in novos_ids]
    supa.upsert("ingestao_concursos", base, on_conflict="id")
    print(f"vigiar: {len(lista)} concursos na {banca}, {len(novos)} novos" + (" (1ª execução: só catalogados)" if primeira_vez else ""))
    for n in novos[:20]:
        print("  novo:", n["id"], n["nome"][:70], "| interesse" if n.get("interesse") else "")


# ---------------------------------------------------------------- processar
def ano_de(conc):
    for campo in (conc.get("edital") or "", conc.get("data_prova") or "", conc.get("nome") or ""):
        m = re.search(r"(20\d{2})", campo)
        if m:
            return int(m.group(1))
    return None


# Prazos (o GitHub corta o job no timeout, e aí a etapa em andamento perde o registro de custo):
#   PRAZO_JOB_MIN       antes de cada etapa e dentro dos laços longos: passou daqui, o resto fica para a próxima rodada
#   PRAZO_PROCESSAR_MIN tempo do processar (cadernos novos); o job pesado ainda roda estruturar e gerar depois dele
# O workflow define os dois por job (rápido: 20 min de timeout; pesado: 40 min). Sem as variáveis: 40 min de job.
INICIO_JOB = __import__("time").time()


def _min_env(nome, padrao):
    try:
        return float(os.environ.get(nome) or padrao) * 60
    except ValueError:
        return padrao * 60


PRAZO_JOB = _min_env("PRAZO_JOB_MIN", 35)
PRAZO_PROCESSAR = _min_env("PRAZO_PROCESSAR_MIN", 22)
_inicio_processar = None

# Orçamento de tempo de cada etapa de IA dentro do job (PRAZO_<ETAPA>_MIN, em min): vira o prazo do ia.py durante a etapa;
# estourou, o laço para, o que já voltou é gravado e a rodada passa à próxima etapa. Em 28/10 o enriquecer levou 12 min e o
# figuras 6, e o job rápido pulou publicar, classificar_local e exportar (26 questões prontas ficaram sem publicar).
ORCAMENTO_MIN = {"enriquecer": 6, "figuras": 3, "classificar_fila": 2, "classificar": 3, "auditar": 2}
# Rápidas e sem IA: nunca são puladas por prazo. As etapas de IA antes do exportar deixam RESERVA_FIM_MIN (2) para ele.
OBRIGATORIAS = {"publicar", "classificar_local", "exportar"}
RESERVA_FIM = _min_env("RESERVA_FIM_MIN", 2)


def orcamento(nome):
    """Segundos que a etapa pode usar, ou None (só o prazo do job)."""
    return _min_env(f"PRAZO_{nome.upper()}_MIN", ORCAMENTO_MIN[nome]) if nome in ORCAMENTO_MIN else None


def job_sem_tempo():
    """O job já passou do prazo (PRAZO_JOB_MIN): parar no próximo ponto seguro."""
    import time
    return time.time() - INICIO_JOB > PRAZO_JOB


def tempo_esgotado():
    import time
    return job_sem_tempo() or (_inicio_processar is not None and time.time() - _inicio_processar > PRAZO_PROCESSAR)


# ---------------------------------------------------------------- sem IA: guardar agora, transcrever depois
# Sem crédito de IA o robô continua baixando cadernos e gabaritos oficiais. O que dá para ler sem IA (AOCP, CETAP com
# texto) vira questão normalmente; o resto fica no Storage com status "aguardando_ia" e volta sozinho para a fila quando
# a IA estiver disponível. O plano grátis do Supabase tem 1 GB: acima de STORAGE_LIMITE_MB o robô guarda só o endereço
# oficial do arquivo (status "so_link") e baixa de novo na hora de transcrever.
STORAGE_LIMITE = float(os.environ.get("STORAGE_LIMITE_MB") or 900) * 1024 * 1024
PENDENTE_IA = ("aguardando_ia", "so_link")
GUARDAR_POR_RODADA = int(os.environ.get("GUARDAR_POR_RODADA") or 60)  # ~40 MB/dia: o 1 GB grátis dura semanas, não um dia
_storage_uso = None
_guardados = 0


def rodada_cheia():
    """Sem IA: já guardou cadernos suficientes nesta rodada (o resto fica para amanhã)."""
    return not ia.disponivel("transcrever") and _guardados >= GUARDAR_POR_RODADA


def cabe_no_storage(n):
    global _storage_uso
    if _storage_uso is None:
        try:
            _storage_uso = float(supa.rpc("uso_storage_bytes", {}) or 0)
        except Exception as e:  # noqa: BLE001
            print(f"storage: não consegui medir o uso ({e}); seguindo sem limite nesta rodada"); _storage_uso = 0.0
    return _storage_uso + n <= STORAGE_LIMITE


def guardar_pdf(caminho, pdf):
    """Sobe o PDF se couber no limite. -> caminho salvo, ou None (fica só o endereço oficial em `origem`)."""
    global _storage_uso, _guardados
    if not cabe_no_storage(len(pdf)):
        return None
    supa.subir_arquivo("provas", caminho, pdf)
    _storage_uso += len(pdf)
    _guardados += 1
    return caminho


def pdf_de(antiga, baixar):
    """Prefere a cópia guardada no Storage (a banca pode tirar o arquivo do ar); senão baixa da banca."""
    if antiga and antiga.get("pdf_path"):
        try:
            return supa.baixar_arquivo("provas", antiga["pdf_path"])
        except Exception as e:  # noqa: BLE001
            print(f"    storage: {e}; baixando da banca")
    return baixar()


def guardar_para_ia(prova_id, salvo, motivo):
    supa.update("ingestao_provas", {"id": f"eq.{prova_id}"},
                {"status": "aguardando_ia" if salvo else "so_link", "erro": f"aguardando IA: {motivo}"[:500]})


def provas_do(conc_id):
    return supa.select("ingestao_provas", {"select": "cargo_cod,status,erro,pdf_path", "concurso_ref": f"eq.{conc_id}"})


def ja_feitas(provas):
    """Cadernos resolvidos: erro passageiro tenta de novo; guardado para a IA só volta quando houver IA."""
    com_ia = ia.disponivel("transcrever")
    return {p["cargo_cod"] for p in provas if not (p["status"] == "erro" and erro_passageiro(p.get("erro")))
            and not (p["status"] in PENDENTE_IA and com_ia)}


def max_cadernos(com_ia_max):
    """Com IA cada caderno leva minutos (limite baixo); sem IA é só baixar e guardar."""
    return com_ia_max if ia.disponivel("transcrever") else 15


def aguardando_com_pdf():
    """Concursos com caderno guardado no Storage esperando IA (aguardando_ia com pdf_path)."""
    return {p["concurso_ref"] for p in supa.select("ingestao_provas", {"select": "concurso_ref", "status": "eq.aguardando_ia", "pdf_path": "not.is.null"})}


def ia_para_cadernos():
    """Vale reabrir os cadernos guardados? -> (sim, motivo). Economia de minutos do Actions: sem IA ou com a cota diária dos
    modelos Flash já gasta, reler os cadernos só ocupava ~20 min do job pesado para nada."""
    if not ia.disponivel("transcrever"):
        return False, "sem IA para transcrever"
    if not ia.anthropic_ok() and ia.cota_pesada_esgotada():
        return False, "cota diária dos modelos Flash (transcrever) já esgotada"
    return True, ""


def fila_processar(limite):
    """Fila do processar. Prova ENVIADA pelo Admin com concurso-alvo vem primeiro (ex.: as 18 FCC do TRT8), na ordem do
    envio (o que já foi tentado vai para o fim dos envios: um envio com erro não trava os outros), antes dos concursos das
    bancas e das provas antigas reabertas (aguardando_ia de FGV/Cebraspe)."""
    envios = supa.select("ingestao_concursos", {"select": "*", "interesse": "eq.true", "processado_em": "is.null", "id": "like.envio:*",
                                                "concurso_alvo": "not.is.null", "order": "tentado_em.asc.nullsfirst,detectado_em.asc"})
    resto = supa.select("ingestao_concursos", {"select": "*", "interesse": "eq.true", "processado_em": "is.null",
                                               "order": "tentado_em.asc.nullsfirst,atualizado_em.desc", "limit": str(limite)})
    ids = {c["id"] for c in envios}
    return (envios + [c for c in resto if c["id"] not in ids])[:max(limite, len(envios))]


def reabrir_aguardando():
    refs = sorted({p["concurso_ref"] for p in supa.select("ingestao_provas", {"select": "concurso_ref", "status": "in.(aguardando_ia,so_link)", "limit": "5000"})})
    for i in range(0, len(refs), 50):
        lista = ",".join('"' + x.replace('"', '') + '"' for x in refs[i:i + 50])
        supa.update("ingestao_concursos", {"id": f"in.({lista})", "processado_em": "not.is.null"}, {"processado_em": None})
    if refs:
        print(f"processar: IA disponível — {len(refs)} concurso(s) com cadernos guardados voltam para a fila")


def processar(limite=None):
    import time
    global _inicio_processar
    _inicio_processar = time.time()
    guardados = aguardando_com_pdf()
    com_ia, motivo = ia_para_cadernos()
    if com_ia and guardados:
        reabrir_aguardando()
    elif not com_ia:
        print(f"processar: {motivo} — cadernos guardados ficam para depois; concursos novos seguem (AOCP e cadernos com texto viram questão sem IA)")
    limite = limite or (20 if com_ia else 40)
    pend = fila_processar(limite)
    if not com_ia:  # reaberto numa rodada anterior e não terminado: só voltaria a bater na IA
        pend = [c for c in pend if c["id"] not in guardados]
    if not pend:
        print("processar: nada a fazer (nenhum concurso novo" + ("" if com_ia else " e IA sem cota para os cadernos guardados") + ")"); return
    s = aocp.sessao()
    for conc in pend:
        if tempo_esgotado():
            print("processar: tempo da rodada esgotado, o resto fica para a próxima"); break
        if rodada_cheia():
            print(f"processar: {GUARDAR_POR_RODADA} cadernos guardados nesta rodada (limite para não lotar o Storage); o resto fica para amanhã"); break
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"tentado_em": agora()})  # rodízio da fila
        if conc["id"].startswith("envio:"):
            processar_envio(conc); continue
        if conc.get("banca") == "CETAP":
            processar_cetap(conc); continue
        if conc.get("banca") == "CEBRASPE":
            processar_cebraspe(conc); continue
        if conc.get("banca") == "FGV":
            processar_fgv(conc); continue
        log = []
        try:
            det = aocp.detalhe(s, conc["externo_id"])
            urls = aocp.links_cadernos(det)
            if not urls:
                encerrado = conc.get("status") in ("FINISHED", "RESULT_AVAILABLE")
                msg = "a AOCP não publica mais os cadernos deste concurso (só mantém os recentes) — desmarcado" if encerrado else "cadernos ainda não publicados"
                supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: {msg}", **({"interesse": False} if encerrado else {})})
                print(f"  {conc['id']}: sem link de cadernos"); continue
            for url in urls:
                acao, cargos, tipos = aocp.abrir_cadernos(s, url)
                rx = filtro_de(conc)
                escolhidos = [(c, n) for c, n in cargos if rx.search(n)]
                log.append(f"{len(cargos)} cargos, {len(escolhidos)} no filtro")
                ja = {(p["cargo_cod"]) for p in supa.select("ingestao_provas", {"select": "cargo_cod", "concurso_ref": f"eq.{conc['id']}"})}
                orgao = slug((conc["nome"] or "").split(" - ")[0], 20) or "orgao"
                ano = ano_de(conc)
                for cod, nome in escolhidos:
                    if cod in ja:
                        continue
                    tipo = tipos[0] if tipos else "1"
                    prova_id = None
                    try:
                        pdf_url, gab = aocp.ver_prova(s, acao, cod, tipo)
                        if not pdf_url or not gab:
                            raise RuntimeError("PDF ou gabarito não encontrado na página")
                        pdf = aocp.baixar(s, pdf_url)
                        caminho = guardar_pdf(f"aocp/{conc['externo_id']}/{cod}-tipo{tipo}.pdf", pdf)
                        prova = supa.upsert("ingestao_provas", [{"concurso_ref": conc["id"], "cargo_cod": cod, "cargo_nome": nome, "tipo_prova": tipo,
                                                                 "origem": {"url": pdf_url}, "pdf_path": caminho, "gabarito": {str(k): v for k, v in gab.items()}, "n_questoes": len(gab), "status": "baixada"}],
                                            on_conflict="concurso_ref,cargo_cod,tipo_prova", retornar=True)[0]
                        prova_id = prova["id"]
                        pref = f"aocp-{ano or 'sd'}-{orgao}-{slug(re.sub(r'^\d+\s*-\s*', '', nome), 30)}"
                        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                            f.write(pdf); f.flush()
                            qs = extrair_questoes(f.name, len(gab))
                            anexar_figuras(f.name, qs, pref)
                        linhas = []
                        for q in qs:
                            if "erro" in q:
                                continue
                            n = q["numero"]; g = gab.get(n)
                            avisos = [x for x in [q.get("aviso"), "depende de figura: transcrever" if q.get("figura") and not q.get("figura_ok") else None, "anulada (sem gabarito)" if g is None else None] if x]
                            linhas.append({"id": f"{pref}-q{n:02d}", "prova_id": prova_id, "numero": n, "banca": "AOCP", "ano": ano,
                                           "orgao": (conc["nome"] or "").split(" - ")[0][:60], "cargo": nome[:120], "disciplina_prova": q.get("disciplina"),
                                           "texto_base": q.get("texto_base"), "enunciado": q["enunciado"], "alternativas": q["alternativas"], "gabarito": g,
                                           "anulada": g is None, "pode_depender_de_imagem": bool(q.get("figura")), "pagina": q.get("pagina"),
                                           "avisos": avisos, "concurso_alvo": conc.get("concurso_alvo")})
                        faltando = [q["numero"] for q in qs if "erro" in q]
                        supa.upsert("ingestao_questoes", linhas, on_conflict="id")
                        supa.update("ingestao_provas", {"id": f"eq.{prova_id}"}, {"status": "extraida", "erro": f"não extraídas: {faltando}" if faltando else None})
                        log.append(f"{nome}: {len(linhas)}/{len(gab)} questões" + (f" (faltando {faltando})" if faltando else ""))
                    except Exception as e:  # noqa: BLE001
                        log.append(f"{nome}: ERRO {e}")
                        if prova_id:
                            supa.update("ingestao_provas", {"id": f"eq.{prova_id}"}, {"status": "erro", "erro": str(e)[:500]})
            supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"processado_em": agora(), "log": "\n".join(log)[:4000]})
        except Exception as e:  # noqa: BLE001
            supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: ERRO {e}"[:4000]})
            traceback.print_exc()
        print(f"  {conc['id']}: " + " | ".join(log))


def filtro_de(conc):
    return re.compile(conc.get("filtro_cargos") or CFG.get("filtro_por_alvo", {}).get(conc.get("concurso_alvo") or "") or CFG["filtro_cargos_padrao"], re.I)


def orgao_cetap(nome):
    m = re.search(r"C-\d+/(\w+)", nome or "")
    if m:
        return slug(m.group(1), 20)
    m = re.search(r"PREFEITURA MUNICIPAL DE ([^/(\-]+)", nome or "", re.I)
    if m:
        extra = re.search(r"\(([A-Z]{2,})\)", nome or "")
        return slug("pm-" + m.group(1) + ("-" + extra.group(1) if extra else ""), 30)
    m = re.search(r"^(.*?)\s+-\s+CONCURSO", nome or "", re.I)
    return slug(m.group(1) if m else nome, 24) or "orgao"


def processar_cetap(conc):
    from ia_extrair import extrair_por_ia
    s = cetap.sessao()
    log = []
    try:
        gab_url, cadernos, definitivo = cetap.classificar_anexos(cetap.anexos(s, conc["externo_id"]))
        if not gab_url or not cadernos:
            supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: cadernos ou gabarito não publicados no site da CETAP"})
            print(f"  {conc['id']}: sem cadernos/gabarito"); return
        gabs = cetap.parse_gabarito(cetap.baixar(s, gab_url))
        if not definitivo:
            log.append("atenção: só há gabarito PRELIMINAR")
        rx = filtro_de(conc)
        escolhidos = [(cod, gabs[cod]["nome"]) for cod in sorted(cadernos) if cod in gabs and rx.search(gabs[cod]["nome"])]
        log.append(f"{len(cadernos)} cargos com caderno, {len(escolhidos)} no filtro")
        provas = provas_do(conc["id"])
        antigas = {p["cargo_cod"]: p for p in provas}
        ja = ja_feitas(provas)
        orgao = orgao_cetap(conc["nome"])
        ano = ano_de(conc)
        for cod, nome in escolhidos:
            if cod in ja:
                continue
            tipo, url, _ = sorted(cadernos[cod])[0]
            gtipo = cetap.tipo_equivalente(tipo, gabs[cod]["provas"])
            prova_id = salvo = None
            try:
                if not gtipo:
                    raise RuntimeError(f"gabarito do tipo {tipo} não encontrado")
                gab = gabs[cod]["provas"][gtipo]
                antiga = antigas.get(cod)
                pdf = pdf_de(antiga, lambda: cetap.baixar(s, url))
                salvo = (antiga or {}).get("pdf_path") or guardar_pdf(f"cetap/{conc['externo_id']}/{cod}-tipo{tipo}.pdf", pdf)
                prova_id = supa.upsert("ingestao_provas", [{"concurso_ref": conc["id"], "cargo_cod": cod, "cargo_nome": nome, "tipo_prova": tipo,
                                                            "origem": {"url": url}, "pdf_path": salvo, "gabarito": {str(k): v for k, v in gab.items()}, "n_questoes": len(gab), "status": "baixada"}],
                                       on_conflict="concurso_ref,cargo_cod,tipo_prova", retornar=True)[0]["id"]
                try:
                    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                        f.write(pdf); f.flush()
                        qs = extrair_questoes(f.name, len(gab))
                except Exception:  # noqa: BLE001  (PDF só com imagens)
                    qs = []
                if sum(1 for q in qs if "erro" not in q) < len(gab) * 0.8:  # caderno escaneado: a IA transcreve
                    _, qs, parada = extrair_por_ia(pdf, len(gab), "CETAP")
                    for q in qs: q["figura_ok"] = True  # a IA descreve a figura no enunciado
                    log.append(f"{nome}: caderno digitalizado, transcrito por IA" + (" (resposta cortada)" if parada == "max_tokens" else ""))
                pref = f"cetap-{ano or 'sd'}-{orgao}-{slug(nome, 30)}"
                existentes = {q["id"] for q in supa.select("questoes", {"select": "id", "id": f"like.{pref}-q*"})}
                linhas, repetidas = [], 0
                for q in qs:
                    if "erro" in q:
                        continue
                    n = q["numero"]
                    if n not in gab:
                        continue
                    qid = f"{pref}-q{n:02d}"
                    if qid in existentes:
                        repetidas += 1; continue
                    g = gab.get(n)
                    avisos = [x for x in [q.get("aviso"), "depende de figura: transcrever" if q.get("figura") and not q.get("figura_ok") else None, "anulada (sem gabarito)" if g is None else None] if x]
                    linhas.append({"id": qid, "prova_id": prova_id, "numero": n, "banca": "CETAP", "ano": ano,
                                   "orgao": (conc["nome"] or "")[:60], "cargo": nome[:120], "disciplina_prova": q.get("disciplina"),
                                   "texto_base": q.get("texto_base"), "enunciado": q["enunciado"], "alternativas": q["alternativas"], "gabarito": g,
                                   "anulada": g is None, "pode_depender_de_imagem": bool(q.get("figura")), "pagina": q.get("pagina"),
                                   "avisos": avisos, "concurso_alvo": conc.get("concurso_alvo")})
                faltando = sorted(set(gab) - {q["numero"] for q in qs if "erro" not in q})
                supa.upsert("ingestao_questoes", linhas, on_conflict="id")
                supa.update("ingestao_provas", {"id": f"eq.{prova_id}"}, {"status": "extraida", "erro": f"não extraídas: {faltando}" if faltando else None})
                log.append(f"{nome}: {len(linhas)}/{len(gab)} questões na fila" + (f", {repetidas} já estavam no banco" if repetidas else "")
                           + (f" (faltando {faltando})" if faltando else ""))
            except SemIA as e:
                log.append(f"{nome}: caderno digitalizado guardado — {e}")
                if prova_id:
                    guardar_para_ia(prova_id, salvo, f"caderno digitalizado: {e}")
            except Exception as e:  # noqa: BLE001
                log.append(f"{nome}: ERRO {e}")
                if prova_id:
                    supa.update("ingestao_provas", {"id": f"eq.{prova_id}"}, {"status": "erro", "erro": str(e)[:500]})
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"processado_em": agora(), "log": "\n".join(log)[:4000]})
        print(f"  {conc['id']}: " + " | ".join(log))
    except Exception as e:  # noqa: BLE001
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: ERRO {e}"[:4000]})
        traceback.print_exc()



def processar_cebraspe(conc):
    """Cebraspe: API pública lista cadernos e gabaritos; a IA transcreve o caderno (múltipla escolha ou Certo/Errado),
    o gabarito vem sempre do PDF oficial definitivo."""
    from ia_extrair import extrair_por_ia
    s = cebraspe.sessao()
    log = []
    eid = conc["externo_id"]
    try:
        cads = cebraspe.cadernos(cebraspe.detalhe(s, eid))
        if not cads:
            supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: cadernos ainda não publicados no site do Cebraspe"})
            print(f"  {conc['id']}: sem cadernos"); return
        rx = filtro_de(conc)
        # caderno entra se algum cargo a que ele se aplica passa no filtro (conhecimentos básicos servem a vários cargos)
        # o filtro olha só a especialidade ("Analista de TI – Perfil: Engenharia Civil" não é TI)
        escolhidos = [c for c in cads if any(rx.search(cebraspe.especialidade(n)) for n in c["cargos"]) or (not c["cargos"] and rx.search(c["descricao"]))]
        sem_gab = [c["codigo"] for c in escolhidos if not c["gabarito"]]
        escolhidos = [c for c in escolhidos if c["gabarito"]]
        log.append(f"{len(cads)} cadernos, {len(escolhidos)} no filtro com gabarito definitivo" + (f"; sem definitivo ainda: {sem_gab}" if sem_gab else ""))
        # caderno com erro passageiro (API sobrecarregada, timeout) volta na próxima rodada; erro do PDF não
        provas = provas_do(conc["id"])
        antigas = {p["cargo_cod"]: p for p in provas}
        ja = ja_feitas(provas)
        limite_cad = max_cadernos(MAX_CADERNOS_CEBRASPE)
        m_ano = re.search(r"_(\d{2})$", eid)  # BCB_24: o ano do concurso está no código (eventoAno às vezes é o do edital)
        ano = 2000 + int(m_ano.group(1)) if m_ano else ano_de(conc)
        orgao = slug(re.sub(r"\s*\d{2,4}\b.*$", "", conc.get("nome") or eid), 20) or slug(eid, 20)
        nome_orgao = nome_orgao_cebraspe(conc.get("nome") or eid)
        passageiro = False
        feitos, adiado = 0, False
        for c in escolhidos:
            if c["codigo"] in ja:
                continue
            if feitos >= limite_cad or tempo_esgotado() or rodada_cheia():  # com IA cada caderno leva minutos: limita por rodada para o workflow não estourar o tempo
                adiado = True; break
            feitos += 1
            nome = (", ".join(c["cargos"]) or c["descricao"])[:120]
            prova_id = salvo = None
            try:
                gab = cebraspe.parse_gabarito(cebraspe.baixar(s, eid, c["gabarito"]))
                if len(gab) < 5:
                    raise RuntimeError(f"gabarito ilegível ({len(gab)} itens lidos)")
                fmt = cebraspe.formato(gab)
                antiga = antigas.get(c["codigo"])
                pdf = pdf_de(antiga, lambda: cebraspe.baixar(s, eid, c["caderno"]))
                salvo = (antiga or {}).get("pdf_path") or guardar_pdf(f"cebraspe/{eid}/{c['caderno']}", pdf)
                prova_id = supa.upsert("ingestao_provas", [{"concurso_ref": conc["id"], "cargo_cod": c["codigo"], "cargo_nome": nome, "tipo_prova": "1",
                                                            "origem": {"eid": eid, "caderno": c["caderno"], "gabarito": c["gabarito"]}, "pdf_path": salvo, "gabarito": {str(k): v for k, v in gab.items()}, "n_questoes": len(gab), "status": "baixada"}],
                                       on_conflict="concurso_ref,cargo_cod,tipo_prova", retornar=True)[0]["id"]
                _, qs, parada = extrair_por_ia(pdf, len(gab), "Cebraspe", max_tokens=64000, numeros=sorted(gab), formato=fmt)
                if parada == "max_tokens":
                    log.append(f"{c['codigo']}: resposta da IA cortada (caderno muito longo)")
                pref = f"cebraspe-{ano or 'sd'}-{orgao}-{slug(c['codigo'], 30)}"
                existentes = {q["id"] for q in supa.select("questoes", {"select": "id", "id": f"like.{pref}-q*"})}
                linhas, repetidas = [], 0
                for q in qs:
                    n = q["numero"]
                    if n not in gab:
                        continue
                    qid = f"{pref}-q{n:03d}"
                    if qid in existentes:
                        repetidas += 1; continue
                    g = gab.get(n)
                    avisos = [x for x in [q.get("aviso"), "anulada (sem gabarito)" if g is None else None] if x]
                    linhas.append({"id": qid, "prova_id": prova_id, "numero": n, "banca": "CEBRASPE", "ano": ano,
                                   "orgao": nome_orgao, "cargo": nome, "disciplina_prova": q.get("disciplina"),
                                   "texto_base": q.get("texto_base"), "enunciado": q["enunciado"], "alternativas": q["alternativas"], "gabarito": g,
                                   "anulada": g is None, "pode_depender_de_imagem": bool(q.get("figura")), "pagina": q.get("pagina"),
                                   "avisos": avisos, "concurso_alvo": conc.get("concurso_alvo")})
                faltando = sorted(set(gab) - {q["numero"] for q in qs})
                supa.upsert("ingestao_questoes", linhas, on_conflict="id")
                supa.update("ingestao_provas", {"id": f"eq.{prova_id}"}, {"status": "extraida", "erro": f"não extraídas: {faltando}" if faltando else None})
                log.append(f"{c['codigo']} ({'Certo/Errado' if fmt == 'ce' else 'A–E'}): {len(linhas)}/{len(gab)} na fila"
                           + (f", {repetidas} já no banco" if repetidas else "") + (f" (faltando {faltando[:10]})" if faltando else ""))
            except SemIA as e:
                if prova_id:
                    guardar_para_ia(prova_id, salvo, str(e))
                    log.append(f"{c['codigo']}: guardado para a IA ({str(e)[:120]})")
            except Exception as e:  # noqa: BLE001
                log.append(f"{c['codigo']}: ERRO {e}" + (" (tenta de novo na próxima rodada)" if erro_passageiro(str(e)) else ""))
                passageiro = passageiro or erro_passageiro(str(e))
                if prova_id:
                    supa.update("ingestao_provas", {"id": f"eq.{prova_id}"}, {"status": "erro", "erro": str(e)[:500]})
        if adiado:
            log.append(f"continua na próxima execução (máx. {limite_cad} cadernos por vez)")
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {**({} if adiado or passageiro else {"processado_em": agora()}), "log": "\n".join(log)[:4000]})
        print(f"  {conc['id']}: " + " | ".join(log))
    except Exception as e:  # noqa: BLE001
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: ERRO {e}"[:4000]})
        traceback.print_exc()


MAX_CADERNOS_CEBRASPE = 4
MAX_CADERNOS_FGV = 4


def orgao_fgv(titulo):
    t = re.sub(r"^(Concurso P[uú]blico|Processo Seletivo( Simplificado)?|Exame|Sele[cç][aã]o)\s+(para|do|da|de)\s+(o|a|os|as)?\s*", "", titulo or "", flags=re.I)
    return re.sub(r"\s+-\s+.*$", "", t).strip()[:60] or titulo[:60]


def processar_fgv(conc):
    """FGV: caderno Tipo 1 de cada cargo que passa no filtro + gabarito oficial definitivo (um PDF para todos os cargos).
    A IA transcreve o caderno e lê no gabarito só as respostas do cargo; o gabarito é sempre o documento oficial."""
    import io
    import pdfplumber
    from ia_extrair import extrair_por_ia, gabarito_por_ia
    s = fgv.sessao()
    cid = conc["externo_id"]
    log = []
    try:
        titulo, partes = fgv.pagina(s, cid)
        if (conc.get("nome") or cid) == cid and titulo:
            supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"nome": titulo[:300]})
        rx = filtro_de(conc)
        cads = fgv.cadernos(partes)
        escolhidos = [c for c in cads if rx.search(f"{c['cargo']} {c['arquivo']}")]
        if not escolhidos:
            msg = f"{len(cads)} cadernos, nenhum cargo no filtro" if cads else "nenhum caderno de prova objetiva publicado"
            # sem caderno ainda (concurso aberto): volta a olhar depois; sem cargo no filtro: encerra
            supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: {msg}", **({"processado_em": agora()} if cads else {})})
            print(f"  {conc['id']}: {msg}"); return
        if not any(fgv.gabarito_definitivo(partes, c["parte"]) for c in escolhidos):
            supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: {len(escolhidos)} cadernos no filtro; gabarito definitivo ainda não publicado"})
            print(f"  {conc['id']}: sem gabarito definitivo"); return
        log.append(f"{len(cads)} cadernos, {len(escolhidos)} no filtro")
        provas = provas_do(conc["id"])
        antigas = {p["cargo_cod"]: p for p in provas}
        ja = ja_feitas(provas)
        limite_cad = max_cadernos(MAX_CADERNOS_FGV)
        pendentes = [c for c in escolhidos if c["cod"] not in ja]
        gabaritos = {}  # url -> (pdf, páginas de texto, gabarito lido pelo layout FGV)

        def gabarito_de(url):
            if url not in gabaritos:
                gpdf = fgv.baixar(s, url)
                guardar_pdf(f"fgv/{cid}/{slug(url.rsplit('/', 1)[-1], 80)}.pdf", gpdf)
                with pdfplumber.open(io.BytesIO(gpdf)) as g:
                    pags = [(p.extract_text() or "") for p in g.pages]
                gabaritos[url] = (gpdf, pags, fgv.parse_gabarito("\n".join(pags)))
            return gabaritos[url]
        ano = fgv._ano(cid, titulo) or ano_de(conc)
        orgao = orgao_fgv(titulo)
        feitos, adiado, passageiro = 0, False, False
        for c in pendentes:
            if feitos >= limite_cad or tempo_esgotado() or rodada_cheia():
                adiado = True; break
            feitos += 1
            prova_id = salvo = None
            try:
                gurl = fgv.gabarito_definitivo(partes, c["parte"])
                if not gurl:
                    raise RuntimeError("gabarito definitivo desta parte ainda não publicado")
                gpdf, gpaginas, gabs = gabarito_de(gurl)
                par = fgv.achar_no_gabarito(gabs, c["cargo"], c["arquivo"], c["tipo"])
                gab, motivo_gab = None, "sem IA para ler o gabarito"
                if par:
                    achado, gab = par
                elif ia.disponivel("gabarito"):  # layout diferente: a IA lê só as páginas do cargo no documento oficial
                    try:
                        achado, gab = gabarito_por_ia(gpdf, fgv.paginas_do_gabarito(gpaginas, c["cargo"], c["arquivo"]),
                                                      c["arquivo"], c["cargo"], c["tipo"])
                        achado = f"IA: {achado}"
                    except SemIA as e:
                        gab, motivo_gab = None, str(e)
                if gab is not None and len(gab) < 10:
                    raise RuntimeError(f"gabarito do cargo não encontrado no definitivo ({len(gab)} respostas lidas)")
                antiga = antigas.get(c["cod"])
                pdf = pdf_de(antiga, lambda: fgv.baixar(s, c["url"]))
                salvo = (antiga or {}).get("pdf_path") or guardar_pdf(f"fgv/{cid}/{slug(c['cod'], 90)}-tipo{c['tipo']}.pdf", pdf)
                prova_id = supa.upsert("ingestao_provas", [{"concurso_ref": conc["id"], "cargo_cod": c["cod"], "cargo_nome": c["cargo"][:200], "tipo_prova": c["tipo"],
                                                            "origem": {"url": c["url"], "gabarito_url": gurl}, "pdf_path": salvo,
                                                            "gabarito": {str(k): v for k, v in gab.items()} if gab else None, "n_questoes": len(gab) if gab else None,
                                                            "status": "baixada"}],
                                       on_conflict="concurso_ref,cargo_cod,tipo_prova", retornar=True)[0]["id"]
                if gab is None:
                    raise SemIA(f"gabarito em layout diferente: a IA lê depois ({motivo_gab})")
                _, qs, parada = extrair_por_ia(pdf, len(gab), "FGV", max_tokens=64000, numeros=sorted(gab))
                if parada == "max_tokens":
                    log.append(f"{c['cargo'][:40]}: resposta da IA cortada (caderno muito longo)")
                pref = f"fgv-{ano or 'sd'}-{slug(cid, 16)}-{slug(c['cod'], 26)}-{hashlib.md5(c['cod'].encode()).hexdigest()[:5]}"  # hash: nomes longos não colidem
                existentes = {q["id"] for q in supa.select("questoes", {"select": "id", "id": f"like.{pref}-q*"})}
                linhas, repetidas = [], 0
                for q in qs:
                    n = q["numero"]
                    if n not in gab:
                        continue
                    qid = f"{pref}-q{n:02d}"
                    if qid in existentes:
                        repetidas += 1; continue
                    g = gab.get(n)
                    avisos = [x for x in [q.get("aviso"), "anulada (sem gabarito)" if g is None else None] if x]
                    linhas.append({"id": qid, "prova_id": prova_id, "numero": n, "banca": "FGV", "ano": ano, "orgao": orgao,
                                   "cargo": c["cargo"][:120], "disciplina_prova": q.get("disciplina"), "texto_base": q.get("texto_base"),
                                   "enunciado": q["enunciado"], "alternativas": q["alternativas"], "gabarito": g, "anulada": g is None,
                                   "pode_depender_de_imagem": bool(q.get("figura")), "pagina": q.get("pagina"), "avisos": avisos,
                                   "concurso_alvo": conc.get("concurso_alvo")})
                faltando = sorted(set(gab) - {q["numero"] for q in qs})
                supa.upsert("ingestao_questoes", linhas, on_conflict="id")
                supa.update("ingestao_provas", {"id": f"eq.{prova_id}"}, {"status": "extraida", "erro": f"não extraídas: {faltando}" if faltando else None})
                log.append(f"{c['cargo'][:50]} (gabarito: {str(achado)[:40]}): {len(linhas)}/{len(gab)} na fila"
                           + (f", {repetidas} já no banco" if repetidas else "") + (f" (faltando {faltando[:10]})" if faltando else ""))
            except SemIA as e:
                if prova_id:
                    guardar_para_ia(prova_id, salvo, str(e))
                    log.append(f"{c['cargo'][:50]}: guardado para a IA ({str(e)[:120]})")
            except Exception as e:  # noqa: BLE001
                log.append(f"{c['cargo'][:50]}: ERRO {e}" + (" (tenta de novo na próxima rodada)" if erro_passageiro(str(e)) else ""))
                passageiro = passageiro or erro_passageiro(str(e))
                if prova_id:
                    supa.update("ingestao_provas", {"id": f"eq.{prova_id}"}, {"status": "erro", "erro": str(e)[:500]})
        if adiado:
            log.append(f"continua na próxima execução (máx. {limite_cad} cadernos por vez)")
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {**({} if adiado or passageiro else {"processado_em": agora()}), "log": "\n".join(log)[:4000]})
        print(f"  {conc['id']}: " + " | ".join(log))
    except Exception as e:  # noqa: BLE001
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: ERRO {e}"[:4000]})
        traceback.print_exc()


def erro_passageiro(msg):
    """Erro que some sozinho (API sobrecarregada, limite de taxa, timeout): vale tentar de novo amanhã."""
    return bool(re.search(r"overloaded|\b529\b|\b429\b|rate.?limit|timed? ?out|timeout|\b50[234]\b|indispon|teto mensal", msg or "", re.I))


def nome_orgao_cebraspe(nome):
    """'BDMG_24' -> 'BDMG'; 'CTI RENATO ARCHER 2023' -> 'CTI RENATO ARCHER'; 'CNPQ 23 ANALISTA' -> 'CNPQ ANALISTA'."""
    n = re.sub(r"\b(?:19|20)?\d{2}\b", " ", (nome or "").replace("_", " "))
    return re.sub(r"\s+", " ", n).strip()[:60] or nome


def _texto_pdf(pdf):
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as g:
        return "\n".join((p.extract_text() or "") for p in g.pages)


def gabarito_do_pdf(gpdf, m, log, generico=False):
    """Gabarito do cargo lido do PDF oficial: leitura determinística (layout CETAP; com `generico`, também a do organizador,
    que lê FCC e afins); a IA só se ela falhar. -> {questão: letra ou None}"""
    from ia_extrair import gabarito_por_ia
    gab, txt = None, ""
    try:
        txt = _texto_pdf(gpdf)
        gabs = cetap.parse_gabarito_texto(txt)
        cod = (m.get("cargo_cod") or "").zfill(2) if m.get("cargo_cod") else None
        if cod and cod in gabs:
            t = cetap.tipo_equivalente(str(m.get("tipo") or "1").upper(), gabs[cod]["provas"])
            gab = gabs[cod]["provas"].get(t) if t else None
        if (not gab or len(gab) < 10) and generico:
            gab = {n: (None if v in "*X#" else v) for n, v in respostas_do_cargo(txt, m.get("cargo_cod"), m.get("tipo")).items()}
    except Exception:  # noqa: BLE001
        gab = None
    if not gab or len(gab) < 10:
        achado, gab = gabarito_por_ia(gpdf, txt, m.get("cargo_cod"), m["cargo"], m.get("tipo"))
        log.append(f"gabarito lido pela IA (cargo no documento: {achado})")
    return gab


def gabarito_do_envio(m, gpdf, log):
    """-> (gabarito, {questão: [avisos]}). Organizador completo e lido por texto: usado direto. Lido por OCR ou incompleto:
    conferido questão a questão com o PDF oficial. Preliminar: todas com aviso. JSON inválido: log e leitura do PDF."""
    payload, org = m.get("gabarito_respostas"), None
    if payload is not None:
        try:
            org = normalizar_gabarito_organizador(payload, m.get("cargo_cod"), m.get("tipo"))
        except ValueError as e:
            log.append(f"JSON do organizador inválido ({e}); gabarito lido do PDF oficial")
    avisos = {}
    if org is not None and not precisa_conferir(payload):
        gab = org
        log.append(f"gabarito carregado do organizador local ({len(gab)} respostas; fonte: {m.get('gabarito_fonte') or 'PDF enviado'})")
    else:
        gab = gabarito_do_pdf(gpdf, m, log, generico=org is not None)
        if org is not None:
            gab, avisos = conferir_com_pdf(org, gab or {})
            log.append(f"gabarito do organizador conferido com o PDF oficial: {len(avisos)} questão(ões) para revisar")
    if org is not None and preliminar(payload):
        for n in gab or {}:
            avisos.setdefault(n, []).append(AVISO_PRELIMINAR)
        log.append("gabarito PRELIMINAR: nenhuma questão desta prova é publicada sozinha")
    return gab, avisos


def processar_envio(conc):
    """Prova enviada pelo Admin (caderno + gabarito oficial no Storage)."""
    from ia_extrair import extrair_por_ia
    log = []
    try:
        m = json.loads(conc.get("cargos") or "{}")["envio"]
        banca = (conc.get("banca") or "CETAP").upper()
        pdf = supa.baixar_arquivo("provas", m["caderno"])
        gpdf = supa.baixar_arquivo("provas", m["gabarito"])
        # 1) gabarito: o do organizador local quando serve; senão (ou para conferir) o do PDF oficial
        gab, avisos_gab = gabarito_do_envio(m, gpdf, log)
        if not gab or len(gab) < 10:
            raise RuntimeError("não consegui ler o gabarito deste cargo")
        n = max(gab)
        # 2) caderno: texto se houver; senão IA transcreve
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                f.write(pdf); f.flush()
                qs = extrair_questoes(f.name, n)
        except Exception:  # noqa: BLE001
            qs = []
        if sum(1 for q in qs if "erro" not in q) < n * 0.8:
            _, qs, parada = extrair_por_ia(pdf, n, banca)
            for q in qs: q["figura_ok"] = True  # a IA descreve a figura no enunciado
            log.append("caderno transcrito por IA" + (" (resposta cortada)" if parada == "max_tokens" else ""))
        removidas = limpar_carimbos(qs)  # PDF baixado de terceiro (exceção TEC): o carimbo nunca entra na questão
        if removidas:
            log.append(f"carimbo de terceiro removido em {removidas} trecho(s)")
        ano = int(m.get("ano") or 0) or None
        orgao = m.get("orgao") or ""
        # id pela sigla (lote do organizador: "trt22"): o nome por extenso cortado em 32 letras era igual para todos os TRTs
        orgao_id = m.get("orgao_sigla") or orgao
        prova_id = supa.upsert("ingestao_provas", [{"concurso_ref": conc["id"], "cargo_cod": m.get("cargo_cod") or "01", "cargo_nome": m["cargo"],
                                                    "tipo_prova": str(m.get("tipo") or "1"), "pdf_path": m["caderno"], "gabarito": {str(k): v for k, v in gab.items()},
                                                    "n_questoes": len(gab), "status": "baixada"}], on_conflict="concurso_ref,cargo_cod,tipo_prova", retornar=True)[0]["id"]
        pref = f"{banca.lower()}-{ano or 'sd'}-{slug(orgao_id, 32)}-{slug(m['cargo'], 34)}"
        existentes = {q["id"] for q in supa.select("questoes", {"select": "id", "id": f"like.{pref}-q*"})}
        fonte_prova = (m.get("fonte_prova") or "").strip()  # ex.: "Prova FCC 2022 – TRT22 – Técnico Judiciário TI"
        linhas, repetidas = [], 0
        for q in qs:
            if "erro" in q or q["numero"] not in gab:
                continue
            qid = f"{pref}-q{q['numero']:02d}"
            if qid in existentes:
                repetidas += 1; continue
            g = gab.get(q["numero"])
            avisos = [x for x in [q.get("aviso"), "depende de figura: transcrever" if q.get("figura") and not q.get("figura_ok") else None, "anulada (sem gabarito)" if g is None else None] if x]
            avisos += avisos_gab.get(q["numero"], [])  # gabarito preliminar ou divergente do PDF: nunca publica sozinha
            linhas.append({"id": qid, "prova_id": prova_id, "numero": q["numero"], "banca": banca, "ano": ano, "orgao": orgao[:60], "cargo": m["cargo"][:120],
                           "disciplina_prova": q.get("disciplina"), "texto_base": q.get("texto_base"), "enunciado": q["enunciado"], "alternativas": q["alternativas"],
                           "gabarito": g, "anulada": g is None, "pode_depender_de_imagem": bool(q.get("figura")), "pagina": q.get("pagina"),
                           "avisos": avisos, "concurso_alvo": conc.get("concurso_alvo"),
                           "fonte": [f"{fonte_prova}, questão {q['numero']}"] if fonte_prova else None})
        faltando = sorted(set(gab) - {q["numero"] for q in qs if "erro" not in q})
        supa.upsert("ingestao_questoes", linhas, on_conflict="id")
        supa.update("ingestao_provas", {"id": f"eq.{prova_id}"}, {"status": "extraida", "erro": f"não extraídas: {faltando}" if faltando else None})
        log.append(f"{m['cargo']}: {len(linhas)}/{len(gab)} questões na fila" + (f", {repetidas} já estavam no banco" if repetidas else "")
                   + (f" (faltando {faltando})" if faltando else ""))
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"processado_em": agora(), "log": "\n".join(log)[:4000]})
        print(f"  {conc['id']}: " + " | ".join(log))
    except SemIA as e:  # arquivos já estão no Storage; volta sozinho quando houver IA
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: aguardando IA ({e})"[:4000]})
        print(f"  {conc['id']}: aguardando IA")
    except Exception as e:  # noqa: BLE001
        supa.update("ingestao_concursos", {"id": f"eq.{conc['id']}"}, {"log": f"{agora()}: ERRO {e}"[:4000]})
        traceback.print_exc()



def anexar_figuras(pdf_path, qs, pref):
    """Recorta as figuras do caderno (PDF com texto) e põe no enunciado como imagem pública (bucket 'figuras')."""
    for q in qs:
        if "erro" in q or not q.get("caixas"):
            continue
        try:
            pngs = recortar_figuras(pdf_path, q["caixas"])
        except Exception as e:  # noqa: BLE001
            print(f"    figura q{q['numero']}: {e}"); continue
        linhas = []
        for k, png in enumerate(pngs):
            caminho = f"{pref}-q{q['numero']:02d}-{k + 1}.png"
            supa.subir_arquivo("figuras", caminho, png, tipo="image/png")
            linhas.append(f"![Figura {k + 1} da questão {q['numero']}]({supa.URL}/storage/v1/object/public/figuras/{caminho})")
        if linhas:
            q["enunciado"] = q["enunciado"].rstrip() + "\n\n" + "\n".join(linhas)
            q["figura"] = q["figura_ok"] = True


# ---------------------------------------------------------------- enriquecer (IA, em lote)
def _prompt_enriquecer(grupo, lista_top):
    questoes = "\n\n".join(
        f"ID: {q['id']}\n" + (f"TEXTO DE APOIO: {q['texto_base'][:1500]}\n" if q.get("texto_base") else "") +
        f"ENUNCIADO: {q['enunciado']}\n" + "\n".join(f"({k}) {v}" for k, v in q["alternativas"].items()) +
        f"\nGABARITO OFICIAL: {q['gabarito'] or 'ANULADA'}" for q in grupo)
    return (
        "Você é professor de concursos. Para cada questão abaixo (prova oficial de banca de concurso), devolva APENAS um JSON "
        "(lista) com objetos {\"id\", \"topico\", \"explicacao\"}.\n"
        "- topico: o id do tópico do edital da lista que melhor corresponde. Se a questão cobra legislação de OUTRO ente "
        "(outro estado, município ou câmara) ou assunto fora do edital, use \"FORA\".\n"
        "- explicacao: comentário em pt-BR no formato \"**Gabarito: (X).** <por que está certa>\" seguido de uma linha por "
        "alternativa errada \"(Y) errada: <motivo curto>\" e uma linha final \"Dica de prova: ...\". Em questões de Certo/Errado, "
        "explique por que o item está certo ou errado. Siga o gabarito oficial; se discordar, diga \"Possível divergência\" e explique. "
        "Não invente número de artigo: sem certeza, cite só a lei.\n\n"
        f"TÓPICOS DO EDITAL:\n{lista_top}\n\nQUESTÕES:\n{questoes}")


def _itens_json(txt):
    m = re.search(r"\[.*\]", txt or "", re.S)
    try:
        return json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return []


def _topico_valido(top, alvo, cache_top):
    """Id do tópico do edital do alvo, o FORA do alvo, ou None (não existe na lista)."""
    if alvo not in cache_top:
        cache_top[alvo] = supa.select("topicos", {"select": "id,disciplina,titulo", "concurso_id": f"eq.{alvo}"})
    fora = "FORA" if alvo == "ifpa-2026" else f"FORA-{alvo}"
    top = str(top or "").strip()
    if top.upper() == "FORA" or top == fora:
        return fora
    return top if top in {t["id"] for t in cache_top[alvo]} else None


def _aplicar_enriquecer(txt, ids, cache_top, conferir_gratis=False, escritor=None):
    """Grava tópico + comentário. conferir_gratis=True (escrito pelo Gemini): só grava o que o conferente (outro modelo do Google; GitHub Models só com CONFERENTE_GITHUB=1)
    aprovou; divergência vai para a fila humana com aviso; sem conferente, guarda a proposta em proposta_ia e a próxima
    rodada só confere (não gasta o Gemini de novo)."""
    itens = _itens_json(txt)
    qs = {q["id"]: q for q in supa.select("ingestao_questoes", {"select": "id,concurso_alvo,texto_base,enunciado,alternativas,gabarito,avisos",
                                                                 "id": "in.(" + ",".join(ids) + ")"})} if ids else {}
    propostas = []
    for it in itens:
        qid = it.get("id")
        if qid not in qs:
            continue
        propostas.append((qid, _topico_valido(it.get("topico"), qs[qid]["concurso_alvo"], cache_top), it.get("explicacao") or None))
    return _gravar_propostas(propostas, qs, cache_top, conferir_gratis, escritor)


def _gravar_propostas(propostas, qs, cache_top, conferir_gratis, escritor=None):
    decisao = {}
    if conferir_gratis:
        try:
            decisao = _decidir(propostas, qs, cache_top, escritor=escritor)
        except Exception as e:  # noqa: BLE001  (conferente com problema não derruba a etapa nem perde o que o Gemini escreveu)
            print(f"  enriquecer: conferência falhou ({type(e).__name__}: {e}); propostas guardadas para conferir na próxima rodada")
    feitos = 0
    for qid, top, exp in propostas:
        mud = {"topico_sugerido": top, "explicacao": exp}
        if conferir_gratis:
            d = decisao.get(qid)
            if d is None:  # conferente (ou desempate) fora do ar, sem cota ou resposta ilegível: guarda a proposta
                supa.update("ingestao_questoes", {"id": f"eq.{qid}"}, {"proposta_ia": {"topico": top, "explicacao": exp, "modelo": escritor}})
                continue
            mud["topico_sugerido"] = d["topico"]
            mud["enriquecido_por"] = d["por"]  # ex.: gemini+gemma-4-31b-it (+ o terceiro modelo, se desempatou)
            mud["proposta_ia"] = None
            if d["aviso"]:
                mud["avisos"] = (qs[qid].get("avisos") or []) + [d["aviso"]]
        supa.update("ingestao_questoes", {"id": f"eq.{qid}"}, mud)
        feitos += 1
    return feitos


def _conferir_guardadas(cache_top, maximo=800):
    """Propostas do Gemini que ainda não foram conferidas (rodada anterior sem conferente): só confere."""
    guard = supa.select("ingestao_questoes", {"select": "id,concurso_alvo,texto_base,enunciado,alternativas,gabarito,avisos,proposta_ia",
                                              "status": "eq.pendente", "topico_sugerido": "is.null", "proposta_ia": "not.is.null",
                                              "limit": str(maximo)})
    if not guard:
        return 0
    for q in guard:
        if q["concurso_alvo"] not in cache_top:
            cache_top[q["concurso_alvo"]] = supa.select("topicos", {"select": "id,disciplina,titulo", "concurso_id": f"eq.{q['concurso_alvo']}"})
    print(f"  enriquecer: {len(guard)} propostas do Gemini guardadas de rodada anterior; conferindo")
    feitos, por_escritor = 0, {}
    for q in guard:  # agrupa por quem escreveu: o conferente nunca é o mesmo modelo
        por_escritor.setdefault((q["proposta_ia"] or {}).get("modelo"), []).append(q)
    for escritor, qs_ in por_escritor.items():
        propostas = [(q["id"], (q["proposta_ia"] or {}).get("topico"), (q["proposta_ia"] or {}).get("explicacao")) for q in qs_]
        feitos += _gravar_propostas(propostas, {q["id"]: q for q in qs_}, cache_top, True, escritor)
    return feitos


# Avisos de divergência. Antes de 02/11 era um só, sem tipo ("IAs divergiram: ..."); o _reconferir_divergentes refaz esses.
AVISO_DIVERGIU_ANTIGO = "IAs divergiram:"
AVISO_DIVERGIU_TOPICO = "IAs divergiram (tópico)"
AVISO_DIVERGIU_CONTEUDO = "IAs divergiram (conteúdo)"
ESCRITOR_PADRAO = "gemini-3.5-flash-lite"  # 1º da lista leve: quem escreve quando o nome não veio
TEXTO_APOIO_CONFERENCIA = 6000  # 02/11: com 700 caracteres o conferente acusava "a palavra X não está no texto" (estava adiante)
TEXTO_APOIO_DESEMPATE = 1500    # o desempate só decide o tópico
LIMITE_PEDIDO_CONFERENCIA = 18000  # pedido inteiro (instruções + lista de tópicos + questões): o maior que já ia ao Gemma (16K tokens/min)
MARCA_TEXTO_CORTADO = "[texto cortado: o resto não foi enviado]"

PROMPT_CONFERIR = (
    "Você confere o trabalho de outro professor de concursos. Para cada questão de prova oficial abaixo há o GABARITO OFICIAL, "
    "o tópico do edital escolhido e o comentário escrito. Aprove (ok=true) só se as duas coisas estiverem certas:\n"
    "(1) o comentário defende o gabarito oficial e não tem erro de conteúdo evidente (lei, conceito, cálculo);\n"
    "(2) o tópico está certo. ATENÇÃO: \"FORA\" é uma resposta VÁLIDA e esperada, não uma violação: significa que a questão "
    "cobra assunto que não está na lista de tópicos do edital (outro cargo, outra área, legislação de outro estado ou município). "
    "Quando o tópico for FORA, confira na LISTA DE TÓPICOS DO EDITAL (enviada abaixo): aprove se nenhum tópico da lista "
    "corresponde ao assunto da questão; reprove só se algum tópico da lista corresponder, e diga qual (id).\n"
    "Os TEXTOS DE APOIO vão inteiros (salvo se terminarem em \"" + MARCA_TEXTO_CORTADO + "\"): antes de dizer que uma palavra, "
    "frase ou trecho não está no texto, procure no texto INTEIRO; se o texto foi cortado, não reprove por algo que pode estar no resto.\n"
    "Na dúvida sobre o conteúdo do comentário, reprove e diga o motivo em uma frase. Devolva APENAS um JSON (lista) de "
    "objetos {\"id\", \"ok\", \"motivo\", \"tipo\", \"topico\"}:\n"
    "- tipo (quando ok=false): \"conteudo\" se há problema no comentário (mesmo que o tópico também esteja errado); "
    "\"topico\" se o comentário está certo e o problema é só o tópico.\n"
    "- topico (quando tipo=\"topico\"): o id da LISTA DE TÓPICOS DO EDITAL que você escolheria, ou \"FORA\".\n\n")

PROMPT_DESEMPATE = (
    "Dois professores de concursos discordam do tópico do edital de cada questão de prova oficial abaixo (o comentário já "
    "foi aprovado; a dúvida é só o tópico). Escolha o tópico certo na LISTA DE TÓPICOS DO EDITAL, considerando as duas "
    "propostas e o motivo da discordância. Use \"FORA\" se a questão cobra assunto que não está na lista (outro cargo, outra "
    "área, legislação de outro estado ou município). Devolva APENAS um JSON (lista) de objetos {\"id\", \"topico\", \"motivo\"} "
    "(topico = id da lista ou \"FORA\"; motivo em uma frase).\n\n")


def _sem_acento(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().strip().lower()


def _cortar_texto(texto, limite):
    texto = (texto or "").strip()
    return texto if len(texto) <= limite else texto[:limite].rstrip() + "\n" + MARCA_TEXTO_CORTADO


def _empacotar(itens, limite):
    """itens: [(qid, texto de apoio | None, corpo)] -> [conteúdo de cada pedido]. Cada texto de apoio vai UMA vez por pedido
    (as questões do mesmo texto ficam juntas) e o pedido não passa de `limite` caracteres; questão sozinha maior que o
    limite vai sozinha (menos questões por pedido, nunca texto mais curto)."""
    ordem = {}
    for _, texto, _ in itens:
        ordem.setdefault(texto, len(ordem))
    grupos, atual, tam = [], [], 0
    for item in sorted(itens, key=lambda i: ordem[i[1]]):  # sorted é estável: a ordem dentro de cada texto se mantém
        _, texto, corpo = item
        novo_texto = texto and all(texto != t for _, t, _ in atual)
        custo = len(corpo) + 60 + (len(texto) + 20 if novo_texto else 0)
        if atual and tam + custo > limite:
            grupos.append(atual); atual, tam = [], 0
            custo = len(corpo) + 60 + (len(texto) + 20 if texto else 0)
        atual.append(item); tam += custo
    if atual:
        grupos.append(atual)
    pedidos = []
    for g in grupos:
        rot = {}
        for _, texto, _ in g:
            if texto and texto not in rot:
                rot[texto] = f"T{len(rot) + 1}"
        partes = []
        if rot:
            partes.append("TEXTOS DE APOIO (cada um vai uma vez; a questão diz qual usa):\n\n"
                          + "\n\n".join(f"[{r}]\n{t}" for t, r in rot.items()) + "\n\n")
        partes.append("QUESTÕES:\n\n" + "\n\n".join(f"ID: {qid}\n" + (f"TEXTO DE APOIO: [{rot[t]}] (acima)\n" if t else "") + corpo
                                                  for qid, t, corpo in g))
        pedidos.append("".join(partes))
    return pedidos


def _lista_top(alvo, cache_top):
    return "\n".join(f"{t['id']} {t['titulo'][:90]}" for t in cache_top.get(alvo) or [])[:5000]


def _questao_txt(q):
    return (f"ENUNCIADO: {q['enunciado'][:1500]}\n" + "\n".join(f"({k}) {str(v)[:400]}" for k, v in (q["alternativas"] or {}).items())
            + f"\nGABARITO OFICIAL: {q['gabarito'] or 'ANULADA'}")


def _nome_topico(top, titulo):
    return "FORA (fora do edital)" if str(top or "").startswith("FORA") else titulo.get(top, top or "nenhum")


def _conferir_enriquecer(propostas, qs, cache_top, limite_chars=LIMITE_PEDIDO_CONFERENCIA, escritor=None):
    """{id: {"ok", "motivo", "tipo", "topico", "por"}} do conferente; ids sem resposta ficam de fora. tipo: "topico" (o
    comentário está certo, só o tópico não) ou "conteudo" (qualquer outra reprovação; na dúvida, conteúdo)."""
    out = {}
    if not propostas or not ia.conferente_disponivel():
        if propostas:
            print("  enriquecer: conferente indisponível; o que o Gemini escreveu fica para a próxima rodada")
        return out
    titulo = {t["id"]: f"{t['id']} [{t['disciplina']}] {t['titulo']}" for ts in cache_top.values() for t in ts}
    por_alvo = {}
    for qid, top, exp in propostas:
        q = qs[qid]
        corpo = _questao_txt(q) + f"\nTÓPICO ESCOLHIDO: {_nome_topico(top, titulo)}\nCOMENTÁRIO: {(exp or '')[:2500]}"
        por_alvo.setdefault(q.get("concurso_alvo"), []).append(
            (qid, _cortar_texto(q["texto_base"], TEXTO_APOIO_CONFERENCIA) if q.get("texto_base") else None, corpo))
    # a lista de tópicos do edital vai sempre: sem ela o conferente não sabe se FORA está certo (27/10: 64 divergências, a
    # maioria por isso) nem tem que tópico sugerir quando discorda (o desempate precisa das duas propostas)
    for alvo, itens in por_alvo.items():
        extra = f"LISTA DE TÓPICOS DO EDITAL ({alvo}):\n{_lista_top(alvo, cache_top)}\n\n"
        for pedido in _empacotar(itens, limite_chars - len(PROMPT_CONFERIR) - len(extra)):
            try:
                txt = ia.conferir("enriquecer-conferencia", PROMPT_CONFERIR + extra + pedido, escritor=escritor or ESCRITOR_PADRAO)
            except Exception as e:  # noqa: BLE001
                print(f"  enriquecer: {e}"); return out
            for it in _itens_json(txt):
                if it.get("id") in qs and f"ID: {it['id']}\n" in pedido:  # só vale para questão que estava neste pedido
                    ok = it.get("ok") is True
                    tipo = None if ok else "topico" if _sem_acento(it.get("tipo")) == "topico" else "conteudo"
                    out[it["id"]] = {"ok": ok, "motivo": it.get("motivo"), "tipo": tipo, "topico": it.get("topico"),
                                     "por": ia.ultimo_conferente or "conferencia"}
    return out


def _desempatar_topicos(empates, qs, cache_top, escritor):
    """empates: [(qid, tópico do escritor, tópico do conferente | None, veredito)]. Um TERCEIRO modelo da lista de conferência
    (nem o escritor nem o conferente) escolhe o tópico. Escolheu uma das duas propostas -> grava e segue (publica); outra
    coisa -> fica para o humano. -> {qid: decisão}; quem não teve resposta agora fica de fora (tenta na próxima rodada)."""
    out = {}
    titulo = {t["id"]: f"{t['id']} [{t['disciplina']}] {t['titulo']}" for ts in cache_top.values() for t in ts}
    grupos = {}
    for e in empates:
        grupos.setdefault((qs[e[0]].get("concurso_alvo"), e[3]["por"]), []).append(e)
    for (alvo, conferente), itens in grupos.items():
        def humano(qid, top, v, nota):
            motivo = str(v.get("motivo") or "sem motivo")
            return {"topico": top, "aviso": f"{AVISO_DIVERGIU_TOPICO}: {motivo[:150]}; {nota}"[:240],
                    "por": f"gemini+{conferente}", "como": "topico"}
        extra = f"LISTA DE TÓPICOS DO EDITAL ({alvo}):\n{_lista_top(alvo, cache_top)}\n\n"
        blocos = [(qid, _cortar_texto(qs[qid]["texto_base"], TEXTO_APOIO_DESEMPATE) if qs[qid].get("texto_base") else None,
                   _questao_txt(qs[qid]) + f"\nPROPOSTA A: {_nome_topico(top, titulo)}\nPROPOSTA B: "
                   + (_nome_topico(sug, titulo) if sug else "(não indicou outro tópico da lista)")
                   + f"\nMOTIVO DA DISCORDÂNCIA: {str(v.get('motivo') or '')[:300]}")
                  for qid, top, sug, v in itens]
        info = {qid: (top, sug, v) for qid, top, sug, v in itens}
        for pedido in _empacotar(blocos, LIMITE_PEDIDO_CONFERENCIA - len(PROMPT_DESEMPATE) - len(extra)):
            try:
                r = ia.desempatar("enriquecer", PROMPT_DESEMPATE + extra + pedido, excluir={escritor or ESCRITOR_PADRAO, conferente})
            except Exception as e:  # noqa: BLE001  (transitório: a decisão fica para a próxima rodada)
                print(f"  enriquecer: {e}"); return out
            ids = [qid for qid in info if f"ID: {qid}\n" in pedido]
            if r is None:  # a lista de conferência não tem um terceiro modelo: o humano decide
                out.update({qid: humano(qid, info[qid][0], info[qid][2], "sem terceiro modelo para desempatar") for qid in ids})
                continue
            txt, terceiro = r
            for it in _itens_json(txt):
                qid = it.get("id")
                if qid not in ids:
                    continue
                top, sug, v = info[qid]
                escolha = _topico_valido(it.get("topico"), alvo, cache_top)
                if escolha and escolha in (top, sug):
                    out[qid] = {"topico": escolha, "aviso": None, "por": f"gemini+{conferente}+{terceiro}", "como": "desempate"}
                else:
                    out[qid] = humano(qid, top, v, f"desempate ({terceiro}) escolheu {it.get('topico') or 'nenhum'}")
    return out


def _decidir(propostas, qs, cache_top, escritor=None):
    """Conferência + desempate do tópico. -> {qid: {"topico", "aviso" (None = segue para publicar), "por", "como"}};
    como: aprovada | desempate (resolvidas) | topico | conteudo (humano). Sem veredito agora -> qid fica de fora.
    Divergência sobre o CONTEÚDO do comentário nunca é desempatada."""
    ver = _conferir_enriquecer(propostas, qs, cache_top, escritor=escritor)
    prop = {qid: top for qid, top, _ in propostas}
    out, empates = {}, []
    for qid, v in ver.items():
        top = prop[qid]
        if v["ok"]:
            out[qid] = {"topico": top, "aviso": None, "por": f"gemini+{v['por']}", "como": "aprovada"}
        elif v["tipo"] == "topico":
            empates.append((qid, top, _topico_valido(v.get("topico"), qs[qid].get("concurso_alvo"), cache_top), v))
        else:
            out[qid] = {"topico": top, "aviso": f"{AVISO_DIVERGIU_CONTEUDO}: {str(v.get('motivo') or 'sem motivo')[:200]}",
                        "por": f"gemini+{v['por']}", "como": "conteudo"}
    if empates:
        out.update(_desempatar_topicos(empates, qs, cache_top, escritor))
    return out


def _reconferir_divergentes(cache_top, maximo=40):
    """Pendentes com o aviso antigo "IAs divergiram: ..." (sem tipo; até 02/11 o conferente só via 700 caracteres do texto de
    apoio): confere de novo com o texto inteiro e desempata o tópico. A proposta é o que já está gravado (topico_sugerido +
    explicacao; o Gemini escritor não é chamado). Sem veredito agora -> fica como está e volta na próxima rodada.
    Resultado em robo_diagnostico (etapa reconferir_divergencias)."""
    cand = supa.select("ingestao_questoes", {"select": "id,concurso_alvo,texto_base,enunciado,alternativas,gabarito,avisos,topico_sugerido,explicacao",
                                             "status": "eq.pendente", "enriquecido_por": "not.is.null", "concurso_alvo": "not.is.null"})
    cand = [q for q in cand if any(str(a).startswith(AVISO_DIVERGIU_ANTIGO) for a in q.get("avisos") or [])]
    total = len(cand)
    cand = cand[:maximo]
    if not cand:
        return 0
    print(f"  enriquecer: reconferindo {len(cand)} de {total} divergência(s) antiga(s) com o texto de apoio inteiro")
    for q in cand:
        q["avisos"] = [a for a in q["avisos"] if not str(a).startswith(AVISO_DIVERGIU_ANTIGO)]
    qs = {q["id"]: q for q in cand}
    try:
        decisao = _decidir([(q["id"], q["topico_sugerido"], q["explicacao"]) for q in cand], qs, cache_top)
    except Exception as e:  # noqa: BLE001
        print(f"  enriquecer: reconferência falhou ({type(e).__name__}: {e}); tenta na próxima rodada"); decisao = {}
    cont = {"aprovada": 0, "desempate": 0, "conteudo": 0, "topico": 0, "adiadas": 0}
    for q in cand:
        d = decisao.get(q["id"])
        if d is None:
            cont["adiadas"] += 1; continue
        supa.update("ingestao_questoes", {"id": f"eq.{q['id']}"},
                    {"topico_sugerido": d["topico"], "enriquecido_por": d["por"], "avisos": q["avisos"] + ([d["aviso"]] if d["aviso"] else [])})
        cont[d["como"]] += 1
    resolvidas, ficaram = cont["aprovada"] + cont["desempate"], cont["conteudo"] + cont["topico"]
    resumo = (f"{resolvidas} resolvida(s) (conferência {cont['aprovada']}, desempate {cont['desempate']}); {ficaram} ficaram "
              f"para o humano (conteúdo {cont['conteudo']}, tópico {cont['topico']}); {cont['adiadas']} sem veredito; "
              f"{total - len(cand)} para as próximas rodadas")
    print(f"  enriquecer: divergências antigas: {resumo}")
    try:
        supa.upsert("robo_diagnostico", [{"banca": "IA", "etapa": "reconferir_divergencias", "url": None, "status_http": None, "bytes": None,
                                          "titulo": f"{resolvidas} resolvidas, {ficaram} ficaram", "itens": len(cand),
                                          "detalhe": json.dumps({**cont, "resolvidas": resolvidas, "ficaram": ficaram,
                                                                 "faltam": total - len(cand)}, ensure_ascii=False)}])
    except Exception as e:  # noqa: BLE001
        print(f"  diagnóstico não gravado: {e}")
    return resolvidas + ficaram


def enriquecer(lote=8, maximo=800):
    if not ia.disponivel("enriquecer"):
        print(f"enriquecer: pulando — {ia.motivo_indisponivel('enriquecer')} (o revisor escolhe o tópico no Admin)"); return
    if not ia.anthropic_ok() and not ia.conferente_disponivel():
        print("enriquecer: sem Claude e sem conferente — o Gemini sozinho não publica comentário; pulando"); return
    cache_top, feitos = {}, 0
    for meta, res in ia.recolher("enriquecer"):
        for cid, txt in res.items():
            feitos += _aplicar_enriquecer(txt, meta.get(cid, []), cache_top)
    if ia.conferente_disponivel():
        feitos += _conferir_guardadas(cache_top)
        feitos += _reconferir_divergentes(cache_top)  # aviso antigo "IAs divergiram:" (antes do texto inteiro e do desempate)
    ja = ia.ids_em_lote("enriquecer")
    pend = [q for q in supa.select("ingestao_questoes", {"select": "id,concurso_alvo,disciplina_prova,texto_base,enunciado,alternativas,gabarito,anulada",
                                                         "status": "eq.pendente", "topico_sugerido": "is.null", "proposta_ia": "is.null",
                                                         "concurso_alvo": "not.is.null", "limit": str(maximo)})
            if q["id"] not in ja]
    por_alvo = {}
    for q in pend:
        por_alvo.setdefault(q["concurso_alvo"], []).append(q)
    pedidos, meta = [], {}
    for alvo, qs in por_alvo.items():
        if alvo not in cache_top:
            cache_top[alvo] = supa.select("topicos", {"select": "id,disciplina,titulo", "concurso_id": f"eq.{alvo}"})
        lista_top = "\n".join(f"{t['id']} [{t['disciplina']}] {t['titulo']}" for t in cache_top[alvo])
        for i in range(0, len(qs), lote):
            grupo = qs[i:i + lote]
            cid = f"enr-{len(pedidos)}"
            pedidos.append((cid, _prompt_enriquecer(grupo, lista_top)))
            meta[cid] = [q["id"] for q in grupo]
    if pedidos:
        res = ia.em_lote("enriquecer", pedidos, max_tokens=8000, meta=meta)
        gratis = ia.provedor_ultimo_lote == "gemini"
        for cid, txt in res.items():
            feitos += _aplicar_enriquecer(txt, meta[cid], cache_top, conferir_gratis=gratis, escritor=ia.modelo_por_pedido.get(cid))
    print(f"enriquecer: {feitos} questões com tópico/comentário sugeridos" + (f"; {len(pend)} enviadas" if pedidos else ""))


# ---------------------------------------------------------------- figuras (Gemini multimodal lê a página do caderno)
# Questão marcada "depende de figura": o Gemini (lista PESADA) vê a página e diz se a figura é mesmo necessária e, se for
# texto/tabela, transcreve. Só imagem de verdade (gráfico, foto, mapa) fica para o humano recortar.
AVISO_FIGURA_ESSENCIAL = "figura essencial: precisa de recorte"
MARCA_FIGURA = "[figura transcrita]"
PDF_INTEIRO_MAX = 15 * 1024 * 1024  # pedido inline do Gemini vai até 20 MB (base64 incha ~33%)
PROMPT_FIGURA = (
    "Esta é {onde} de um caderno de prova oficial de concurso. Olhe a questão {numero} abaixo (enunciado, alternativas e "
    "gabarito oficial já transcritos do texto do PDF) e a figura/quadro/tabela dela na página.\n"
    "Devolva APENAS um JSON {{\"precisa_da_figura\": bool, \"transcricao\": \"...\" ou null, \"ok\": bool, \"motivo\": \"...\"}}:\n"
    "- precisa_da_figura: false se dá para responder só com o texto já transcrito (a figura é decorativa, repete o texto ou "
    "não existe nesta questão).\n"
    "- transcricao: se a figura for texto, código, quadro ou tabela, transcreva fielmente (tabela em markdown simples). Se for "
    "imagem de verdade (gráfico, foto, mapa, diagrama, print de tela que não se reduz a texto), null.\n"
    "- ok: true só se você achou a questão {numero} e a leitura confere com o gabarito oficial.\n"
    "- motivo: uma frase.\n\nQUESTÃO {numero}\n{texto}ENUNCIADO: {enunciado}\n{alternativas}\nGABARITO OFICIAL: {gabarito}")


def _aviso_de_figura(a):
    return a.startswith(("depende de figura", "figura essencial", "figura:"))


def _figura_em_linha_propria(enunciado):
    """O app só desenha ![Figura](url) em linha própria; o recorte às vezes ficou colado no meio da frase."""
    sep = re.sub(r"[ \t]*(!\[Figura[^\]]*\]\(https://[^)\s]+\))[ \t]*", r"\n\n\1\n\n", enunciado or "")
    return re.sub(r"\n{3,}", "\n\n", sep).strip()


def decidir_figura(q, r):
    """Mudanças na questão a partir da resposta do Gemini (dict) ou None (resposta ilegível: tenta na próxima rodada)."""
    mud = _decidir_figura(q, r)
    if mud and mud.get("pode_depender_de_imagem") is False and "![Figura" in (q.get("enunciado") or ""):
        novo = _figura_em_linha_propria(mud.get("enunciado", q["enunciado"]))
        if novo != q["enunciado"]:
            mud["enunciado"] = novo
    return mud


def _decidir_figura(q, r):
    if not isinstance(r, dict) or "precisa_da_figura" not in r:
        return None
    outros = [a for a in (q.get("avisos") or []) if not _aviso_de_figura(a)]
    if not r.get("ok"):
        motivo = str(r.get("motivo") or "leitura não conferiu com o gabarito")[:160]
        return {"avisos": outros + [f"{AVISO_FIGURA_ESSENCIAL} (robô não confirmou a leitura: {motivo})"]}
    if not r.get("precisa_da_figura"):
        return {"pode_depender_de_imagem": False, "avisos": outros}
    trans = str(r.get("transcricao") or "").strip()
    if trans and trans.lower() != "null":
        return {"pode_depender_de_imagem": False, "avisos": outros,
                "enunciado": f"{q['enunciado'].rstrip()}\n\n{MARCA_FIGURA}\n{trans}\n{MARCA_FIGURA}"}
    if "![Figura" in (q.get("enunciado") or ""):  # imagem de verdade, mas o recorte do caderno já está no enunciado
        return {"pode_depender_de_imagem": False, "avisos": outros}
    return {"avisos": outros + [AVISO_FIGURA_ESSENCIAL]}


def _achar_pagina(pdf, enunciado):
    """Página (1..n) cujo texto contém o começo do enunciado; None se o PDF não tem texto ou não achou."""
    import io
    import pdfplumber
    alvo = re.sub(r"\W+", "", (enunciado or "")[:80]).lower()[:40]
    if len(alvo) < 15:
        return None
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        for i, pg in enumerate(p.pages):
            if alvo in re.sub(r"\W+", "", pg.extract_text() or "").lower():
                return i + 1
    return None


def _png_da_pagina(pdf, pagina, resolucao=150):
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        if not 1 <= pagina <= len(p.pages):
            return None
        buf = io.BytesIO()
        p.pages[pagina - 1].to_image(resolution=resolucao).original.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _json_obj(txt):
    m = re.search(r"\{.*\}", txt or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


PROMPT_FIGURAS_CADERNO = (
    "Este é o caderno inteiro de uma prova oficial de concurso. As questões abaixo (enunciado, alternativas e gabarito oficial "
    "já transcritos do texto do PDF) foram marcadas como dependentes de figura/quadro/tabela. Para CADA uma, ache-a no caderno "
    "pelo número e olhe a figura dela.\n"
    "Devolva APENAS um JSON (lista), um objeto por questão: {{\"numero\": int, \"precisa_da_figura\": bool, "
    "\"transcricao\": \"...\" ou null, \"ok\": bool, \"motivo\": \"...\"}}:\n"
    "- precisa_da_figura: false se dá para responder só com o texto já transcrito (a figura é decorativa, repete o texto ou "
    "não existe nesta questão).\n"
    "- transcricao: se a figura for texto, código, quadro ou tabela, transcreva fielmente (tabela em markdown simples). Se for "
    "imagem de verdade (gráfico, foto, mapa, diagrama, print de tela que não se reduz a texto), null.\n"
    "- ok: true só se você achou a questão e a leitura confere com o gabarito oficial.\n"
    "- motivo: uma frase.\n\n{questoes}")


def _texto_questao_figura(q):
    texto = f"TEXTO DE APOIO: {q['texto_base'][:1500]}\n" if q.get("texto_base") else ""
    return (f"QUESTÃO {q['numero']}\n{texto}ENUNCIADO: {q['enunciado']}\n"
            + "\n".join(f"({k}) {v}" for k, v in (q.get("alternativas") or {}).items()) + f"\nGABARITO OFICIAL: {q['gabarito']}")


def _lista_por_numero(txt):
    """Resposta em lista -> {numero: objeto}."""
    m = re.search(r"\[.*\]", txt or "", re.S)
    try:
        itens = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return {}
    out = {}
    for it in itens if isinstance(itens, list) else []:
        try:
            out[int(it.get("numero"))] = it
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def figuras(maximo=200):
    """Pendentes com figura e PDF guardado: o Gemini vê a página e resolve o que não precisa de humano. Questão com página
    conhecida = um pedido com a imagem da página; caderno escaneado sem página = UM pedido por caderno (PDF inteiro) com
    todas as questões com figura dele, resposta em lista por número. Teto GEMINI_MAX_FIGURAS (conta pedidos)."""
    import base64
    if not ia.gemini_disponivel("figuras"):
        print(f"figuras: pulando — {ia.motivo_indisponivel('figuras')}"); return
    pend = [q for q in supa.select("ingestao_questoes", {"select": "id,prova_id,numero,pagina,texto_base,enunciado,alternativas,gabarito,avisos",
                                                         "status": "eq.pendente", "pode_depender_de_imagem": "eq.true", "anulada": "eq.false",
                                                         "gabarito": "not.is.null", "order": "criado_em", "limit": str(maximo)})
            if not any(a.startswith(AVISO_FIGURA_ESSENCIAL) for a in q.get("avisos") or [])]  # já é do humano
    if not pend:
        print("figuras: nada a ler"); return
    ids = ",".join(sorted({q["prova_id"] for q in pend}))
    caminho = {p["id"]: p.get("pdf_path") for p in supa.select("ingestao_provas", {"select": "id,pdf_path", "id": f"in.({ids})"})}
    pdfs, inteiros = {}, {}
    cont = {"sem_figura": 0, "transcritas": 0, "essenciais": 0, "sem_pdf": 0, "sem_pagina": 0, "ilegiveis": 0, "pedidos": 0}

    def aplicar(q, r):
        mud = decidir_figura(q, r)
        if mud is None:
            cont["ilegiveis"] += 1; return
        supa.update("ingestao_questoes", {"id": f"eq.{q['id']}"}, mud)
        cont["transcritas" if "enunciado" in mud else "sem_figura" if mud.get("pode_depender_de_imagem") is False else "essenciais"] += 1

    def pedir(blocos):
        """-> texto. GeminiIndisponivel = acabou o Gemini da rodada; outra exceção = só este pedido falhou."""
        cont["pedidos"] += 1
        txt, _ = ia.gemini("figuras", blocos, max_tokens=8000)
        return txt

    parar = False
    for q in pend:  # 1º: questões com página conhecida (imagem da página, pedido pequeno)
        if not ia.gemini_disponivel("figuras") or ia.sem_tempo(90):
            parar = True; break
        path = caminho.get(q["prova_id"])
        if not path:  # só o link oficial (Storage cheio): fica para quando o PDF for guardado
            cont["sem_pdf"] += 1; continue
        try:
            if path not in pdfs:
                pdfs[path] = supa.baixar_arquivo("provas", path)
            pdf = pdfs[path]
            pagina = q.get("pagina") or _achar_pagina(pdf, q["enunciado"])
            png = _png_da_pagina(pdf, pagina) if pagina else None
        except Exception as e:  # noqa: BLE001
            print(f"  figuras {q['id']}: PDF ilegível ({e})"); cont["sem_pdf"] += 1; continue
        if not png:
            if len(pdf) <= PDF_INTEIRO_MAX:  # caderno escaneado (FGV, Cebraspe): junta para um pedido só do caderno
                inteiros.setdefault(path, []).append(q)
            else:  # grande demais para mandar inteiro: o robô não sabe onde está a figura
                supa.update("ingestao_questoes", {"id": f"eq.{q['id']}"},
                            {"avisos": [a for a in q.get("avisos") or [] if not _aviso_de_figura(a)] + [f"{AVISO_FIGURA_ESSENCIAL} (página não localizada no PDF)"]})
                cont["sem_pagina"] += 1
            continue
        prompt = PROMPT_FIGURA.format(onde="a página", numero=q["numero"], texto=f"TEXTO DE APOIO: {q['texto_base'][:1500]}\n" if q.get("texto_base") else "",
                                      enunciado=q["enunciado"], gabarito=q["gabarito"],
                                      alternativas="\n".join(f"({k}) {v}" for k, v in (q.get("alternativas") or {}).items()))
        try:
            txt = pedir([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
                         {"type": "text", "text": prompt}])
        except ia.GeminiIndisponivel as e:
            print(f"figuras: parando — {e}"); parar = True; break
        except Exception as e:  # noqa: BLE001
            print(f"  figuras {q['id']}: {e}"); cont["ilegiveis"] += 1; continue
        aplicar(q, _json_obj(txt))

    for path, qs in ([] if parar else inteiros.items()):  # 2º: um pedido por caderno inteiro
        if not ia.gemini_disponivel("figuras") or ia.sem_tempo(120):
            break
        prompt = PROMPT_FIGURAS_CADERNO.format(questoes="\n\n".join(_texto_questao_figura(q) for q in qs))
        try:
            txt = pedir([{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdfs[path]).decode()}},
                         {"type": "text", "text": prompt}])
        except ia.GeminiIndisponivel as e:
            print(f"figuras: parando — {e}"); break
        except Exception as e:  # noqa: BLE001
            print(f"  figuras caderno {path}: {e}"); cont["ilegiveis"] += len(qs); continue
        resp = _lista_por_numero(txt)
        for q in qs:
            aplicar(q, resp.get(int(q["numero"])))
    print(f"figuras: {cont['pedidos']} pedido(s) ({len(inteiros)} caderno(s) inteiro(s)); {cont['sem_figura']} não precisavam da figura, "
          f"{cont['transcritas']} transcritas, {cont['essenciais']} ficam para você (figura essencial); {cont['sem_pagina']} sem página no PDF, "
          f"{cont['sem_pdf']} sem PDF guardado, {cont['ilegiveis']} respostas ilegíveis")


# ---------------------------------------------------------------- publicar (aprovação automática)
RX_DIVERGENCIA = re.compile(r"diverg[êe]ncia", re.I)


def _hash_js(s):
    """Mesmo hash do Admin (JS: h = (h*31 + charCode) | 0, depois >>> 0 em base 36) para o id do texto de apoio."""
    h = 0
    for ch in s:  # "for (const ch of s)" anda por caractere; charCodeAt(0) pega a 1ª metade de emoji/caractere astral
        cp = ord(ch)
        u = 0xD800 + ((cp - 0x10000) >> 10) if cp > 0xFFFF else cp
        h = (h * 31 + u) & 0xFFFFFFFF
    dig, out = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while True:
        h, r = divmod(h, 36); out = dig[r] + out
        if not h:
            return out


def motivo_revisao(q):
    """None = pode publicar sozinha; senão, o motivo para ficar na fila de revisão humana."""
    alts, g = q.get("alternativas") or {}, q.get("gabarito")
    if q.get("avisos"):
        return "aviso da extração: " + "; ".join(q["avisos"])
    if q.get("pode_depender_de_imagem"):
        return "depende de figura"
    if not q.get("topico_sugerido"):
        return "sem tópico"
    exp = q.get("explicacao") or ""
    if len(exp) < 40:
        return "sem comentário"
    if RX_DIVERGENCIA.search(exp):
        return "comentário aponta possível divergência com o gabarito"
    if len((q.get("enunciado") or "").strip()) < 15:
        return "enunciado curto demais"
    chaves = "".join(sorted(alts))
    if chaves not in ("CE", "ABCD", "ABCDE"):
        return f"alternativas incompletas ({chaves or 'nenhuma'})"
    if any(not str(v).strip() for v in alts.values()) and chaves != "CE":
        return "alternativa vazia"
    if g not in alts:
        return "gabarito fora das alternativas"
    return None


def _verificar_geradas(qs):
    """Questão escrita por IA só sai sozinha se outra leitura, sem ver o gabarito, chegar à mesma resposta e não achar ambiguidade."""
    if not qs or not ia.disponivel():
        return {}
    res = {}
    for i in range(0, len(qs), 10):
        grupo = qs[i:i + 10]
        corpo = "\n\n".join(f"ID: {q['id']}\n" + (f"TEXTO: {q['texto_base'][:1200]}\n" if q.get("texto_base") else "") +
                             f"ENUNCIADO: {q['enunciado']}\n" + "\n".join(f"({k}) {v}" for k, v in q["alternativas"].items()) for q in grupo)
        try:
            txt, _ = ia.chamar("verificar_geradas", corpo, max_tokens=1500, system=(
                "Resolva cada questão de concurso como um especialista. Responda SÓ um JSON: lista de {\"id\", \"resposta\": letra, "
                "\"ambigua\": true se houver mais de uma alternativa defensável, enunciado incompleto ou erro de conteúdo}."))
            m = re.search(r"\[.*\]", txt or "", re.S)
            for it in json.loads(m.group(0)) if m else []:
                res[it.get("id")] = it
        except Exception as e:  # noqa: BLE001
            print(f"  verificar geradas: {e}")
    return res


def limpar_texto_pdf():
    """Desfaz palavra partida pelo PDF ("f ácil", "nó s", "usá- lo") e espaço sobrando em aspas e pontuação, nas questões,
    nos textos de apoio e na fila de revisão. O dicionário é o próprio banco: só junta quando a palavra inteira já existe nele."""
    try:
        qs = supa.select("questoes", {"select": "id,enunciado,alternativas"})
        txs = supa.select("textos", {"select": "id,texto"})
        fila = supa.select("ingestao_questoes", {"select": "id,enunciado,alternativas,texto_base", "status": "eq.pendente"})
    except Exception as e:  # noqa: BLE001
        print(f"limpar: ERRO ao ler {e}"); return
    vocab = textos.vocabulario([q["enunciado"] for q in qs + fila] + [" ".join(map(str, (q.get("alternativas") or {}).values())) for q in qs + fila]
                               + [t["texto"] for t in txs] + [q.get("texto_base") or "" for q in fila])
    n = 0

    def limpa_q(q, tabela, extra=()):
        nonlocal n
        novo = {"enunciado": textos.limpar_tudo(q["enunciado"], vocab),
                "alternativas": {k: textos.limpar_tudo(v, vocab) if isinstance(v, str) else v for k, v in (q.get("alternativas") or {}).items()}}
        for campo in extra:
            novo[campo] = textos.limpar_tudo(q.get(campo), vocab)
        mudou = {k: v for k, v in novo.items() if v != q.get(k)}
        if mudou:
            supa.update(tabela, {"id": f"eq.{q['id']}"}, mudou); n += 1

    for q in qs:
        limpa_q(q, "questoes")
    for q in fila:
        limpa_q(q, "ingestao_questoes", ("texto_base",))
    nt = 0
    for t in txs:
        # texto de apoio: só junta palavra partida (a pontuação dele já é tratada na estruturação, e os marcadores # > @ ficam)
        novo = textos.juntar_partidas(t["texto"], vocab)
        if novo != t["texto"]:
            supa.update("textos", {"id": f"eq.{t['id']}"}, {"texto": novo}); nt += 1
    print(f"limpar: {n} questão(ões) e {nt} texto(s) corrigidos")


def descartar_anuladas():
    """Anulada (ou sem gabarito) não vira questão: sai da fila com ou sem tópico, sem esperar comentário nem olho humano."""
    filtro = {"status": "eq.pendente", "or": "(anulada.eq.true,gabarito.is.null)"}
    n = len(supa.select("ingestao_questoes", {"select": "id", **filtro}))
    if n:
        supa.update("ingestao_questoes", filtro, {"status": "rejeitada", "revisado_em": datetime.now(timezone.utc).isoformat()})
    print(f"publicar: {n} anulada(s)/sem gabarito descartada(s)")
    return n


def publicar(maximo=4000):
    """Aprovação automática: questão de prova oficial com tópico, comentário, gabarito e alternativas completas, sem aviso nem
    divergência, vai direto para o banco de questões. Anulada é descartada. Só o que tem problema real fica para revisão no Admin."""
    descartar_anuladas()
    pend = supa.select("ingestao_questoes", {"select": "*", "status": "eq.pendente", "topico_sugerido": "not.is.null", "limit": str(maximo)})
    if not pend:
        print("publicar: nada pronto"); return
    disc = {t["id"]: t["disciplina"] for t in supa.select("topicos", {"select": "id,disciplina"})}
    agora = datetime.now(timezone.utc).isoformat()
    geradas = [q for q in pend if q["id"].startswith("gerada-") and not motivo_revisao(q)]
    verif = _verificar_geradas(geradas)
    publicadas = descartadas = 0
    fila = {}
    for q in pend:
        try:
            if q.get("anulada") or not q.get("gabarito"):
                supa.update("ingestao_questoes", {"id": f"eq.{q['id']}"}, {"status": "rejeitada", "revisado_em": agora})
                descartadas += 1; continue
            motivo = motivo_revisao(q)
            inedita = q["id"].startswith("gerada-")
            if not motivo and inedita:
                v = verif.get(q["id"])
                if not v:
                    motivo = "questão inédita ainda não verificada"
                elif v.get("ambigua") or str(v.get("resposta", "")).strip().upper()[:1] != q["gabarito"]:
                    motivo = "verificação independente discordou do gabarito ou achou ambiguidade"
                    supa.update("ingestao_questoes", {"id": f"eq.{q['id']}"}, {"avisos": (q.get("avisos") or []) + [motivo]})
            if motivo:
                fila[motivo.split(":")[0]] = fila.get(motivo.split(":")[0], 0) + 1
                # motivo que a próxima rodada não resolve sozinha vira aviso visível no Admin
                if not motivo.startswith(("aviso", "sem tópico", "sem comentário", "questão inédita ainda", "verificação independente")):
                    supa.update("ingestao_questoes", {"id": f"eq.{q['id']}"}, {"avisos": (q.get("avisos") or []) + [motivo]})
                continue
            texto_id = None
            if (q.get("texto_base") or "").strip():
                texto_id = f"{re.sub(r'-q[0-9]+$', '', q['id'])}-T{_hash_js(q['texto_base'])}"
                supa.upsert("textos", [{"id": texto_id, "texto": q["texto_base"]}], on_conflict="id")
            # inédita: a norma; prova com fonte gravada no envio (lote do organizador): "Prova FCC 2022 – TRT22 – ..., questão N"
            fonte = q.get("fonte") if inedita or q.get("fonte") else [re.sub(r"\s+", " ", f"Prova {q.get('banca') or ''} {q.get('ano') or ''} – {q.get('orgao') or ''} – {q.get('cargo') or ''}, questão {q.get('numero')}").strip()]
            supa.upsert("questoes", [{
                "id": q["id"], "origem": "gerada" if inedita else (q.get("banca") or "aocp").lower(), "banca": q.get("banca"), "ano": q.get("ano"),
                "orgao": q.get("orgao"), "cargo": q.get("cargo"), "nivel": None, "disciplina": disc.get(q["topico_sugerido"], q.get("disciplina_prova")),
                "topico_id": q["topico_sugerido"], "texto_id": texto_id, "enunciado": q["enunciado"], "alternativas": q["alternativas"],
                "gabarito": q["gabarito"], "anulada": False, "formato": None, "explicacao": q.get("explicacao"), "fonte": fonte,
                "arquivo": None, "numero": q.get("numero"), "pode_depender_de_imagem": False, "ativa": True,
                "revisao_status": "auto-ia-gratis" if q.get("enriquecido_por") else "auto-verificada", "via_ingestao": True}], on_conflict="id")
            supa.update("ingestao_questoes", {"id": f"eq.{q['id']}"}, {"status": "aprovada", "revisado_em": agora})
            publicadas += 1
        except Exception as e:  # noqa: BLE001  (uma questão com problema não para as outras)
            print(f"  publicar {q['id']}: {e}")
    resto = ", ".join(f"{n} {m}" for m, n in sorted(fila.items(), key=lambda x: -x[1]))
    print(f"publicar: {publicadas} publicadas sozinhas, {descartadas} anuladas descartadas; ficam para revisão: {resto or 'nenhuma'}")


# ---------------------------------------------------------------- classificar sem IA (modelo próprio, custo zero)
def _diag_classificar(msg):
    """Falha do classificador também vai para robo_diagnostico (dá para ver pelo banco, sem abrir o log do Actions)."""
    print(f"classificar_local: {msg}")
    try:
        supa.upsert("robo_diagnostico", [{"banca": "TODAS", "etapa": "classificar_local", "url": None, "status_http": None, "bytes": None,
                                          "titulo": "falhou", "itens": 0, "detalhe": msg[:2000]}])
    except Exception as e:  # noqa: BLE001
        print(f"  diagnóstico não gravado: {e}")


CLASSIFICAR_MIN_NOVOS = int(os.environ.get("CLASSIFICAR_LOCAL_MIN_NOVOS") or 50)


def deve_treinar(atuais, anteriores, minimo=CLASSIFICAR_MIN_NOVOS):
    """Treinar o classificador local só vale com alvos novos: -> (treinar?, quantos novos). Sem registro anterior: treina."""
    if anteriores is None:
        return True, len(set(atuais))
    novos = len(set(atuais) - set(anteriores))
    return novos >= minimo, novos


def _alvos_do_ultimo_treino():
    """Ids dos alvos do último treino (robo_diagnostico, etapa 'classificar_local_alvos'); None se nunca registrou."""
    r = supa.select("robo_diagnostico", {"select": "detalhe", "etapa": "eq.classificar_local_alvos", "order": "criado_em.desc", "limit": "1"})
    try:
        return json.loads(r[0]["detalhe"]) if r else None
    except (ValueError, TypeError, KeyError):
        return None


def classificar_local():
    """Assunto pelo classificador próprio (classificar_local.py). Questões publicadas sem assunto ganham assunto; questões
    de prova oficial na fila SEM concurso-alvo (ex.: FGV) são publicadas com o assunto, sem comentário (o aluno pode pedir).
    Treinar leva minutos: só treina com pelo menos CLASSIFICAR_LOCAL_MIN_NOVOS (50) alvos novos desde o último treino."""
    import time
    import classificar_local as CL
    ini = time.time()
    try:
        atuais = ([f"q:{q['id']}" for q in CL.todas("questoes", {"select": "id", "ativa": "eq.true", "assunto_id": "is.null"})]
                  + [f"i:{q['id']}" for q in CL.todas("ingestao_questoes", {"select": "id", "status": "eq.pendente", "concurso_alvo": "is.null",
                                                                            "anulada": "eq.false", "pode_depender_de_imagem": "eq.false", "gabarito": "not.is.null"})])
        treinar, novos = deve_treinar(atuais, _alvos_do_ultimo_treino())
    except Exception as e:  # noqa: BLE001  (sem conseguir comparar, treina como antes)
        print(f"classificar_local: não comparei com o último treino ({e}); treinando")
        atuais, treinar, novos = None, True, None
    if not treinar:
        seg = round(time.time() - ini, 1)
        print(f"classificar_local: pulado — {novos} alvo(s) novo(s) desde o último treino (mínimo {CLASSIFICAR_MIN_NOVOS}); {seg:.0f}s")
        try:
            supa.upsert("robo_diagnostico", [{"banca": "TODAS", "etapa": "classificar_local", "url": None, "status_http": None, "bytes": None,
                                              "titulo": f"pulado: {novos} alvo(s) novo(s) (mínimo {CLASSIFICAR_MIN_NOVOS})", "itens": 0,
                                              "detalhe": json.dumps({"pulado": True, "alvos": len(atuais), "novos": novos, "segundos": seg})}])
        except Exception as e:  # noqa: BLE001
            print(f"  diagnóstico não gravado: {e}")
        return
    try:
        assuntos = supa.select("assuntos", {"select": "id,pai_id,nivel"})
        pai_de = {a["id"]: a["pai_id"] for a in assuntos if a["nivel"] == 2 and a["pai_id"]}
        rot = CL.todas("questoes", {"select": "id,disciplina,enunciado,alternativas,assunto_id", "ativa": "eq.true", "assunto_id": "not.is.null",
                                    "or": "(assunto_origem.is.null,assunto_origem.neq.local)"})
        sem = CL.todas("questoes", {"select": "id,disciplina,enunciado,alternativas", "ativa": "eq.true", "assunto_id": "is.null"})
        fila = [q for q in CL.todas("ingestao_questoes", {"select": "*", "status": "eq.pendente", "concurso_alvo": "is.null", "anulada": "eq.false",
                                                          "pode_depender_de_imagem": "eq.false", "gabarito": "not.is.null"})
                if not motivo_revisao({**q, "topico_sugerido": "x", "explicacao": "x" * 40})]  # só o que não tem outro problema
        alvos = [(f"q:{q['id']}", CL.texto_de(q)) for q in sem] + [(f"i:{q['id']}", CL.texto_de(q)) for q in fila]
        prev, met = CL.treinar_e_prever([(CL.texto_de(q), q["assunto_id"]) for q in rot], alvos, pai_de)
    except ImportError as e:
        _diag_classificar(f"scikit-learn não instalado: {e}"); return
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(); _diag_classificar(f"ERRO {type(e).__name__}: {e}"); return
    mapa_q = {k[2:]: v for k, v in prev.items() if k.startswith("q:")}
    mapa_i = {k[2:]: v for k, v in prev.items() if k.startswith("i:")}
    feitas = 0
    ids = list(mapa_q)
    for i in range(0, len(ids), 300):
        lote = ids[i:i + 300]
        feitas += supa.rpc("definir_assuntos", {"mapa": {k: mapa_q[k] for k in lote}}) or 0
        for j in range(0, len(lote), 100):
            lista = ",".join('"' + x + '"' for x in lote[j:j + 100])
            supa.update("questoes", {"id": f"in.({lista})"}, {"assunto_origem": "local"})
    publicadas = _publicar_sem_comentario(fila, mapa_i, "local", "classificar_local")
    met.update(assunto_definido=feitas, publicadas_sem_comentario=publicadas, alvos_novos=novos, segundos=round(time.time() - ini, 1))
    print(f"classificar_local: {met}")
    if atuais is not None:
        try:  # o próximo treino compara com este conjunto
            supa.upsert("robo_diagnostico", [{"banca": "TODAS", "etapa": "classificar_local_alvos", "url": None, "status_http": None, "bytes": None,
                                              "titulo": f"{len(atuais)} alvos no treino", "itens": len(atuais), "detalhe": json.dumps(atuais)}])
        except Exception as e:  # noqa: BLE001
            print(f"  alvos do treino não gravados: {e}")
    try:
        supa.upsert("robo_diagnostico", [{"banca": "TODAS", "etapa": "classificar_local", "url": None, "status_http": None, "bytes": None,
                                          "titulo": f"{feitas} classificadas, {publicadas} publicadas", "itens": feitas + publicadas,
                                          "detalhe": json.dumps(met, ensure_ascii=False)[:2000]}])
    except Exception as e:  # noqa: BLE001
        print(f"  diagnóstico não gravado: {e}")


def _publicar_sem_comentario(fila, assunto_de, origem, etapa):
    """Questão oficial da fila SEM concurso-alvo com assunto definido -> banco de questões, sem comentário (o aluno pode pedir).
    origem do assunto: 'local' (classificador próprio) ou 'gemini'. -> quantas publicou."""
    publicadas, agora_ = 0, agora()
    for q in fila:
        a = assunto_de.get(q["id"])
        if not a:
            continue
        try:
            texto_id = None
            if (q.get("texto_base") or "").strip():
                texto_id = f"{re.sub(r'-q[0-9]+$', '', q['id'])}-T{_hash_js(q['texto_base'])}"
                supa.upsert("textos", [{"id": texto_id, "texto": q["texto_base"]}], on_conflict="id")
            fonte = [re.sub(r"\s+", " ", f"Prova {q.get('banca') or ''} {q.get('ano') or ''} – {q.get('orgao') or ''} – {q.get('cargo') or ''}, questão {q.get('numero')}").strip()]
            supa.upsert("questoes", [{
                "id": q["id"], "origem": (q.get("banca") or "").lower() or "oficial", "banca": q.get("banca"), "ano": q.get("ano"), "orgao": q.get("orgao"),
                "cargo": q.get("cargo"), "nivel": None, "disciplina": q.get("disciplina_prova"), "topico_id": None, "texto_id": texto_id,
                "enunciado": q["enunciado"], "alternativas": q["alternativas"], "gabarito": q["gabarito"], "anulada": False, "formato": None,
                "explicacao": None, "fonte": fonte, "arquivo": None, "numero": q.get("numero"), "pode_depender_de_imagem": False, "ativa": True,
                "revisao_status": "auto-sem-comentario", "via_ingestao": True, "assunto_id": a, "assunto_origem": origem}], on_conflict="id")
            supa.update("ingestao_questoes", {"id": f"eq.{q['id']}"}, {"status": "aprovada", "revisado_em": agora_})
            publicadas += 1
        except Exception as e:  # noqa: BLE001
            print(f"  {etapa} {q['id']}: {e}")
    return publicadas


def _fila_sem_alvo():
    """Questões oficiais pendentes sem concurso-alvo que só esperam o assunto (sem aviso, figura, anulação nem outro problema)."""
    fila = supa.select("ingestao_questoes", {"select": "*", "status": "eq.pendente", "concurso_alvo": "is.null", "anulada": "eq.false",
                                             "pode_depender_de_imagem": "eq.false", "gabarito": "not.is.null", "order": "id"})
    return [q for q in fila if not motivo_revisao({**q, "topico_sugerido": "x", "explicacao": "x" * 40})]


def _arvore_assuntos():
    """-> (assuntos, texto da árvore para o prompt, ids válidos)."""
    assuntos = supa.select("assuntos", {"select": "id,pai_id,nome,nivel", "order": "ordem"})
    pais = {a["pai_id"] for a in assuntos if a["pai_id"]}
    nome = {a["id"]: a["nome"] for a in assuntos}
    folhas = [a for a in assuntos if a["id"] not in pais]
    arvore = "\n".join(f"{a['id']} = {nome[a['id'].split('.')[0]]} › {a['nome']}" for a in folhas)
    return assuntos, arvore, {a["id"] for a in assuntos if a["nivel"] > 0}


def _sistema_classificar(arvore):
    return ("Você classifica questões de concurso público numa árvore fixa de assuntos. Para cada questão, escolha o ID do assunto "
            "(da lista abaixo) que corresponde ao CONTEÚDO cobrado, não ao cargo nem ao órgão. Use o tópico de origem só como pista. "
            "Questões sobre estatuto/regimento de um órgão específico que não seja IFPA ou ALEPA vão em leg.normas.outros. "
            "Responda APENAS com um JSON: lista de objetos {\"id\": <id da questão>, \"assunto\": <id do assunto>}.\n\nASSUNTOS:\n" + arvore)


def _corpo_classificar(grupo, origem_de):
    """origem_de(q) -> texto da pista de origem (tópico do edital ou disciplina da prova)."""
    return "\n\n".join(
        f"ID: {q['id']}\nTÓPICO DE ORIGEM: {origem_de(q)}\nENUNCIADO: {(q['enunciado'] or '')[:700]}\n"
        + "\n".join(f"({k}) {str(v)[:160]}" for k, v in (q["alternativas"] or {}).items()) for q in grupo)


def _assuntos_da_resposta(txt, ids, validos):
    m = re.search(r"\[.*\]", txt or "", re.S)
    try:
        itens = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        itens = []
    return {it["id"]: it["assunto"] for it in itens if isinstance(it, dict) and it.get("id") in set(ids) and it.get("assunto") in validos}


def classificar_fila(lote=25):
    """Fila sem concurso-alvo (FGV etc.) que ficou abaixo da trava do classificador local: o Gemini da lista LEVE dá só o
    assunto (mesmo prompt da etapa classificar, sem comentário) e a questão é publicada como no classificar_local, com
    assunto_origem 'gemini'. Teto GEMINI_MAX_CLASSIFICAR_FILA (padrão 40 pedidos de até `lote` questões)."""
    if not ia.gemini_disponivel("classificar_fila"):
        print(f"classificar_fila: pulando — {ia.motivo_indisponivel('classificar_fila')}"); return
    fila = _fila_sem_alvo()
    if not fila:
        print("classificar_fila: nada na fila sem concurso-alvo"); return
    _, arvore, validos = _arvore_assuntos()
    if not validos:
        print("classificar_fila: tabela assuntos vazia — rode Admin › Importar conteúdo"); return
    sistema = _sistema_classificar(arvore)
    assunto_de, pedidos = {}, 0
    for i in range(0, len(fila), lote):
        if not ia.gemini_disponivel("classificar_fila") or ia.sem_tempo(60):
            break
        grupo = fila[i:i + lote]
        try:
            txt, _ = ia.gemini("classificar_fila", _corpo_classificar(grupo, lambda q: q.get("disciplina_prova") or "—"),
                               max_tokens=3000, system=sistema)
        except ia.GeminiIndisponivel as e:
            print(f"classificar_fila: parando — {e}"); break
        except Exception as e:  # noqa: BLE001  (um lote ruim não para os outros)
            print(f"  classificar_fila: {e}"); continue
        finally:
            pedidos += 1
        assunto_de.update(_assuntos_da_resposta(txt, [q["id"] for q in grupo], validos))
    publicadas = _publicar_sem_comentario(fila, assunto_de, "gemini", "classificar_fila")
    print(f"classificar_fila: {pedidos} pedido(s) ao Gemini, {len(assunto_de)} com assunto, {publicadas} publicadas sem comentário; "
          f"{len(fila) - publicadas} continuam na fila")


# ---------------------------------------------------------------- estruturar (IA → parágrafos do texto de apoio)
def _chamar_ia(prompt, max_tokens=4000):
    return ia.chamar("estruturar", prompt, max_tokens=max_tokens, rapido=True)[0]


def precisa_estruturar(t):
    """Só vale chamar a IA para texto de leitura (≥ 500 caracteres, em várias linhas). Comando curto do Cebraspe
    ("Julgue os itens a seguir…") não é texto de apoio e era 90% das chamadas; texto numa linha só não tem o que dividir."""
    t = (t or "").strip()
    if len(t) < 500 or textos.esta_estruturado(t):
        return False
    return len([l for l in t.split("\n") if l.strip()]) >= 3


def _estruturar_um(texto, qs):
    """Texto já em parágrafos (veio da IA na transcrição): marca título/fonte sem IA. Texto linha a linha: IA aponta os blocos.
    -> (novo | None, relatório, usou_ia)"""
    if "\n\n" in texto.strip():
        novo = textos.estruturar_paragrafado(texto)
        if not novo:
            return None, "já em parágrafos, sem título/fonte para marcar (fica como está)", False
        n, falhas = textos.conferir_referencias(novo, qs)
        if falhas:
            return None, "referências das questões não bateram: " + "; ".join(falhas[:2]), False
        return novo, f"ok sem IA ({len(textos.paragrafos_do_corpo(novo))} parágrafos; {n} referência(s) conferida(s))", False
    if not ia.disponivel():
        return None, "precisa de IA (texto linha a linha) e a chave não está configurada", False
    novo, rel = textos.estruturar_por_ia(texto, qs, _chamar_ia)
    return novo, rel, True


def estruturar(maximo=60):
    """Textos de apoio sem marcação -> blocos (# título, > linha fina, parágrafos, @ fonte).
    Vale para a fila (ingestao_questoes.texto_base) e para o que já está publicado (textos).
    Antes, texto que já vinha em parágrafos ia para a IA toda rodada e nunca ficava "estruturado" (não tinha marcação):
    ~60 chamadas por dia sem efeito. Agora esses são marcados sem IA; a IA fica só para texto linha a linha do PDF."""
    feitos = falhas = chamadas_ia = 0
    pend = supa.select("ingestao_questoes", {"select": "id,texto_base,enunciado,alternativas", "status": "eq.pendente", "texto_base": "not.is.null"})
    grupos = {}
    for q in pend:
        if precisa_estruturar(q["texto_base"]):
            grupos.setdefault(q["texto_base"], []).append(q)
    pub = supa.select("textos", {"select": "id,texto"})
    alvos_pub = [t for t in pub if precisa_estruturar(t["texto"])]
    print(f"estruturar: {len(alvos_pub)} publicado(s) e {len(grupos)} texto(s) na fila sem estrutura")
    # publicados primeiro: são os que os alunos já estão lendo
    for t in alvos_pub:
        if chamadas_ia >= maximo or job_sem_tempo():
            break
        qs = supa.select("questoes", {"select": "enunciado,alternativas", "texto_id": f"eq.{t['id']}"})
        novo, rel, usou = _estruturar_um(t["texto"], qs)
        chamadas_ia += usou
        print(f"  {t['id']}: {rel}")
        if novo:
            supa.update("textos", {"id": f"eq.{t['id']}"}, {"texto": novo}); feitos += 1
        else:
            falhas += 1
    for original, qs in grupos.items():
        if chamadas_ia >= maximo or job_sem_tempo():
            break
        novo, rel, usou = _estruturar_um(original, qs)
        chamadas_ia += usou
        if novo:
            for q in qs:
                supa.update("ingestao_questoes", {"id": f"eq.{q['id']}"}, {"texto_base": novo})
            feitos += 1
        else:
            falhas += 1
            if usou:
                print(f"  fila ({len(qs)} questões): {rel}")
    print(f"estruturar: {feitos} estruturado(s), {falhas} sem mudança, {chamadas_ia} chamada(s) à IA")


# ---------------------------------------------------------------- gerar (IA → questões inéditas para lacunas do edital)
def _cobertura(alvo, topicos):
    """Questões disponíveis por tópico do edital: próprias + emprestadas pela taxonomia + pendentes na fila."""
    ids = {t["id"] for t in topicos}
    qs = supa.select("questoes", {"select": "topico_id,assunto_id", "ativa": "is.true"})
    pai = {a["id"]: a["pai_id"] for a in supa.select("assuntos", {"select": "id,pai_id"})}
    lig = {}
    for l in supa.select("topico_assuntos", {"select": "topico_id,assunto_id"}):
        if l["topico_id"] in ids:
            lig.setdefault(l["assunto_id"], l["topico_id"])
    cont = {t: 0 for t in ids}
    for q in qs:
        t = q["topico_id"] if q["topico_id"] in ids else None
        a = q.get("assunto_id")
        while not t and a:
            t, a = lig.get(a), pai.get(a)
        if t:
            cont[t] += 1
    for q in supa.select("ingestao_questoes", {"select": "topico_sugerido", "status": "eq.pendente", "concurso_alvo": f"eq.{alvo}"}):
        if q["topico_sugerido"] in cont:
            cont[q["topico_sugerido"]] += 1
    return cont


def gerar(alvos=None, minimo=25, por_topico=8, max_topicos=6):
    import hashlib
    if not ia.disponivel():
        print("gerar: ANTHROPIC_API_KEY/ANTHROPIC_MODEL não definidos — pulando"); return
    alvos = alvos or CFG.get("gerar_para", ["alepa-2026", "ifpa-2026"])
    hoje = datetime.now(timezone.utc).strftime("%Y%m%d")
    for alvo in alvos:
        if job_sem_tempo():
            print("gerar: prazo do job atingido; o resto fica para a próxima rodada"); break
        conc = (supa.select("concursos", {"select": "id,nome,banca", "id": f"eq.{alvo}"}) or [{}])[0]
        banca = conc.get("banca") or "AOCP"
        topicos = [t for t in supa.select("topicos", {"select": "id,disciplina,titulo,itens_edital", "concurso_id": f"eq.{alvo}"}) if t["disciplina"] != "FORA"]
        cont = _cobertura(alvo, topicos)
        lacunas = sorted((t for t in topicos if cont[t["id"]] < minimo), key=lambda t: cont[t["id"]])[:max_topicos]
        if not lacunas:
            print(f"gerar {alvo}: nenhum tópico abaixo de {minimo} questões"); continue
        cid = f"gerada:{alvo}"
        supa.upsert("ingestao_concursos", [{"id": cid, "banca": banca, "externo_id": alvo, "nome": f"Questões inéditas (IA) – {conc.get('nome', alvo)}",
                                            "status": "GERADA", "interesse": False, "concurso_alvo": alvo}], on_conflict="id")
        exemplos = supa.select("questoes", {"select": "enunciado,alternativas,gabarito", "banca": f"eq.{banca}", "origem": "neq.gerada", "limit": "3"})
        estilo = "\n\n".join(f"{e['enunciado'][:600]}\n" + "\n".join(f"({k}) {v}" for k, v in e["alternativas"].items()) + f"\nGabarito: {e['gabarito']}" for e in exemplos)
        for t in lacunas:
            if job_sem_tempo():
                break
            ficha = (supa.select("fichas", {"select": "conteudo", "topico_id": f"eq.{t['id']}"}) or [{}])[0].get("conteudo")
            base = json.dumps(ficha, ensure_ascii=False)[:9000] if ficha else ""
            prompt = (
                f"Você elabora questões INÉDITAS de concurso no estilo da banca {banca}, para o tópico do edital abaixo.\n"
                f"TÓPICO: {t['titulo']}\nITENS DO EDITAL: {json.dumps(t.get('itens_edital'), ensure_ascii=False)}\n\n"
                + (f"MATERIAL DE BASE (ficha de estudo revisada; use APENAS fatos que estejam aqui ou que sejam texto literal de lei que você conheça com certeza):\n{base}\n\n" if base else "")
                + f"EXEMPLOS DO ESTILO DA BANCA (não copie):\n{estilo}\n\n"
                f"Crie {por_topico} questões de múltipla escolha (A–E, uma única correta), variando dificuldade e subtemas. Regras:\n"
                "- Nada de pegadinha falsa ou ambígua; as erradas devem ser plausíveis e claramente erradas pela norma.\n"
                "- A correta NÃO pode ser a mais longa nem a mais detalhada: alternativas com tamanho parecido.\n"
                "- Distratores sem absurdos óbvios (\"nunca\", \"jamais\", \"100%\", \"garante a eliminação total\"): erros sutis, ditos com precisão.\n"
                "- Na explicação, refute CADA alternativa errada em uma linha própria \"(Y) errada: ...\".\n"
                "- Se não tiver CERTEZA de um artigo/dado, não use. Prefira menos questões a questões com fato inventado.\n"
                "- fonte: a norma e o artigo exatos que comprovam o gabarito (ex.: \"Lei 8.112/1990, art. 20\").\n"
                "- explicacao em pt-BR: \"**Gabarito: (X).** <por quê>\", uma linha por alternativa errada \"(Y) errada: <motivo>\" e \"Dica de prova: ...\".\n"
                "Responda APENAS com JSON: lista de {\"enunciado\", \"alternativas\": {\"A\":..,\"E\":..}, \"gabarito\", \"explicacao\", \"fonte\"}.")
            try:
                txt, _ = ia.chamar("gerar", prompt, max_tokens=12000)
            except RuntimeError as e:
                print(f"gerar {t['id']}: {e}"); break
            m = re.search(r"\[.*\]", txt, re.S)
            try:
                itens = json.loads(m.group(0)) if m else []
            except json.JSONDecodeError:
                itens = []
            import qualidade
            validos, descartes = [], []
            for i in itens:
                if not (i.get("enunciado") and len(i.get("alternativas") or {}) == 5 and str(i.get("gabarito", "")).upper() in list("ABCDE")):
                    descartes.append("formato"); continue
                i = {**i, "gabarito": str(i["gabarito"]).upper(), "alternativas": {k.upper(): str(v).strip() for k, v in i["alternativas"].items()}}
                motivos = qualidade.problemas(i)
                if motivos:
                    descartes.append("; ".join(motivos)); continue
                validos.append(qualidade.embaralhar(i))
            if descartes:
                print(f"gerar {t['id']}: {len(descartes)} descartadas na auditoria contra chute ({' | '.join(sorted(set(descartes))[:3])})")
            itens = validos
            if not itens:
                print(f"gerar {t['id']}: nada aproveitável"); continue
            prova = supa.upsert("ingestao_provas", [{"concurso_ref": cid, "cargo_cod": t["id"], "cargo_nome": f"Inéditas IA – {t['titulo'][:80]}", "tipo_prova": hoje,
                                                     "gabarito": {}, "n_questoes": len(itens), "status": "extraida"}],
                                on_conflict="concurso_ref,cargo_cod,tipo_prova", retornar=True)[0]["id"]
            linhas = []
            for n, i in enumerate(itens, 1):
                h = hashlib.sha1(i["enunciado"].encode()).hexdigest()[:6]
                linhas.append({"id": f"gerada-{alvo}-{t['id'].lower()}-{hoje}-{h}", "prova_id": prova, "numero": n, "banca": banca, "ano": None,
                               "orgao": None, "cargo": None, "disciplina_prova": t["disciplina"], "texto_base": None, "enunciado": i["enunciado"].strip(),
                               "alternativas": {k.upper(): str(v).strip() for k, v in i["alternativas"].items()}, "gabarito": str(i["gabarito"]).upper(),
                               "anulada": False, "pode_depender_de_imagem": False, "avisos": [], "concurso_alvo": alvo, "topico_sugerido": t["id"],
                               "explicacao": i.get("explicacao"), "fonte": [str(i["fonte"])[:300]]})
            supa.upsert("ingestao_questoes", linhas, on_conflict="id")
            print(f"gerar {alvo}/{t['id']} (tinha {cont[t['id']]}): {len(linhas)} questões na fila · {qualidade.relatorio(itens)}")


# ---------------------------------------------------------------- classificar (IA → árvore universal de assuntos)
def classificar(lote=25, maximo=3000):
    if not ia.disponivel("classificar"):
        print(f"classificar: pulando — {ia.motivo_indisponivel('classificar')}"); return
    assuntos, arvore, validos = _arvore_assuntos()
    if not assuntos:
        print("classificar: tabela assuntos vazia — rode Admin › Importar conteúdo"); return

    def aplicar(txt, ids):
        novos = _assuntos_da_resposta(txt, ids, validos)
        if novos:
            supa.rpc("definir_assuntos", {"mapa": novos})
        return len(novos)

    feitos = 0
    for meta, res in ia.recolher("classificar"):
        for cid, txt in res.items():
            feitos += aplicar(txt, meta.get(cid, []))
    topicos = {t["id"]: t["titulo"] for t in supa.select("topicos", {"select": "id,titulo"})}
    ja = ia.ids_em_lote("classificar")
    pend = [q for q in supa.select("questoes", {"select": "id,topico_id,disciplina,enunciado,alternativas", "assunto_id": "is.null",
                                                "ativa": "is.true", "order": "id", "limit": str(maximo)}) if q["id"] not in ja]
    sistema = [{"type": "text", "text": _sistema_classificar(arvore), "cache_control": {"type": "ephemeral"}}]
    pedidos, meta = [], {}
    for i in range(0, len(pend), lote):
        grupo = pend[i:i + lote]
        corpo = _corpo_classificar(grupo, lambda q: topicos.get(q["topico_id"], q["topico_id"]))
        cid = f"cls-{len(pedidos)}"
        pedidos.append((cid, corpo)); meta[cid] = [q["id"] for q in grupo]
    if pedidos:
        res = ia.em_lote("classificar", pedidos, rapido=True, max_tokens=3000, system=sistema, meta=meta)
        for cid, txt in res.items():
            feitos += aplicar(txt, meta[cid])
    print(f"classificar: {feitos} questões classificadas" + (f"; {len(pend)} enviadas em lote" if pedidos else ""))


# ---------------------------------------------------------------- auditar (IA → questões desatualizadas)
DISC_AUDITAR = ("dcon", "dadm", "leg", "afo", "adm", "info", "ti", "dh", "edu")


def auditar(lote=20, maximo=1500):
    if not ia.disponivel("auditar"):
        print(f"auditar: pulando — {ia.motivo_indisponivel('auditar')}"); return

    def aplicar(txt, ids):
        m = re.search(r"\[.*\]", txt or "", re.S)
        try:
            itens = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            itens = None
        if itens is None:
            return 0, 0  # resposta sem JSON: o lote volta para a fila (auditada_em continua vazio)
        mapa = {i: None for i in ids}
        for it in itens:
            if it.get("id") in mapa and it.get("motivo"):
                mapa[it["id"]] = str(it["motivo"])[:500]
        supa.rpc("marcar_auditoria", {"mapa": mapa})
        return len(mapa), sum(1 for v in mapa.values() if v)

    auditadas = marcadas = 0
    for meta, res in ia.recolher("auditar"):
        for cid, txt in res.items():
            a, b = aplicar(txt, meta.get(cid, [])); auditadas += a; marcadas += b
    ja = ia.ids_em_lote("auditar")
    pend = supa.select("questoes", {"select": "id,banca,ano,assunto_id,enunciado,alternativas,gabarito", "auditada_em": "is.null",
                                    "ativa": "is.true", "origem": "neq.gerada", "assunto_id": "not.is.null", "order": "id"})
    pend = [q for q in pend if q["assunto_id"].split(".")[0] in DISC_AUDITAR and q["id"] not in ja][:maximo]
    hoje = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    sistema = (
        f"Hoje é {hoje}. Você revisa questões ANTIGAS de concurso público para saber se o GABARITO OFICIAL ainda estaria correto "
        "pelas regras vigentes hoje: legislação (leis revogadas ou alteradas, ex.: Lei 8.666/93 → 14.133/21, improbidade alterada pela "
        "14.230/21, Decreto 1.171 e alterações), normas técnicas e versões (ex.: ITIL v3 → ITIL 4, ISO/IEC 27002:2013 → 2022, PMBOK, "
        "versões do Office/Windows/LibreOffice que mudaram menus ou recursos).\n"
        "Marque SOMENTE quando tiver certeza de uma mudança concreta que torna o gabarito errado ou a questão sem resposta hoje. "
        "Na dúvida, NÃO marque. Questões de conceito que não mudou estão válidas mesmo sendo antigas.\n"
        "Responda APENAS com um JSON: lista de objetos {\"id\", \"motivo\"} só das desatualizadas; motivo curto em pt-BR citando a "
        "norma nova (ex.: \"A Lei 8.666/93 foi revogada pela Lei 14.133/21, que não tem mais a modalidade tomada de preços.\"). "
        "Se nenhuma estiver desatualizada, responda []."
    )
    pedidos, meta = [], {}
    for i in range(0, len(pend), lote):
        grupo = pend[i:i + lote]
        corpo = "\n\n".join(
            f"ID: {q['id']} ({q['banca']} {q['ano']})\nENUNCIADO: {(q['enunciado'] or '')[:900]}\n"
            + "\n".join(f"({k}) {str(v)[:220]}" for k, v in (q["alternativas"] or {}).items())
            + f"\nGABARITO OFICIAL: {q['gabarito'] or 'ANULADA'}" for q in grupo)
        cid = f"aud-{len(pedidos)}"
        pedidos.append((cid, corpo)); meta[cid] = [q["id"] for q in grupo]
    if pedidos:
        res = ia.em_lote("auditar", pedidos, rapido=True, max_tokens=3000, system=sistema, meta=meta)
        for cid, txt in res.items():
            a, b = aplicar(txt, meta[cid]); auditadas += a; marcadas += b
    print(f"auditar: {auditadas} questões auditadas, {marcadas} marcadas como desatualizadas" + (f"; {len(pend)} enviadas em lote" if pedidos else ""))


# ---------------------------------------------------------------- exportar
def exportar():
    qs = supa.select("questoes", {"select": "*", "via_ingestao": "eq.true", "order": "id"})
    destino = RAIZ / "conteudo" / "questoes" / "ingest"
    destino.mkdir(parents=True, exist_ok=True)
    grupos = {}
    for q in qs:
        chave = re.sub(r"-\d{8}-[0-9a-f]{6}$", "", q["id"]) if q["id"].startswith("gerada-") else re.sub(r"-q\d+$", "", q["id"])
        grupos.setdefault(chave, []).append(q)
    for pref, lista in grupos.items():
        saida = [{"id": q["id"], "banca": q["banca"], "ano": q["ano"], "orgao": q["orgao"], "cargo": q["cargo"], "numero": q["numero"],
                  "disciplina_prova": q["disciplina"], "texto_id": q["texto_id"], "enunciado": q["enunciado"], "alternativas": q["alternativas"],
                  "gabarito": q["gabarito"], "anulada": q["anulada"], "topico": q["topico_id"], "pode_depender_de_imagem": q["pode_depender_de_imagem"],
                  "explicacao": q["explicacao"], "fonte": q["fonte"], "via_ingestao": True} for q in sorted(lista, key=lambda x: x["numero"] or 0)]
        (destino / f"{pref}.json").write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
    ids_txt = sorted({q["texto_id"] for q in qs if q.get("texto_id")})
    textos = []
    for i in range(0, len(ids_txt), 100):
        textos += supa.select("textos", {"select": "id,texto", "id": "in.(" + ",".join(ids_txt[i:i + 100]) + ")"})
    (destino / "textos.json").write_text(json.dumps(sorted(textos, key=lambda t: t["id"]), ensure_ascii=False, indent=1), encoding="utf-8")
    # textos já versionados em outros arquivos (questões importadas à mão): o banco manda, senão um "Importar conteúdo" desfaria a estrutura
    por_id = {t["id"]: t["texto"] for t in supa.select("textos", {"select": "id,texto"})}
    for arq in (RAIZ / "conteudo").rglob("textos*.json"):
        if arq.parent == destino:
            continue
        dados = json.loads(arq.read_text(encoding="utf-8"))
        mudou = 0
        for t in dados if isinstance(dados, list) else []:
            if t.get("id") in por_id and por_id[t["id"]] != t.get("texto"):
                t["texto"] = por_id[t["id"]]; mudou += 1
        if mudou:
            arq.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"exportar: {mudou} texto(s) atualizados em {arq.relative_to(RAIZ)}")
    todas_q = supa.select("questoes", {"select": "id,assunto_id", "assunto_id": "not.is.null", "order": "id"})
    (destino / "assuntos-por-questao.json").write_text(json.dumps({q["id"]: q["assunto_id"] for q in todas_q}, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"exportar: {len(todas_q)} questões com assunto em assuntos-por-questao.json")
    aud = supa.select("questoes", {"select": "id,desatualizada_motivo", "auditada_em": "not.is.null", "order": "id"})
    (destino / "auditoria.json").write_text(json.dumps({q["id"]: q["desatualizada_motivo"] for q in aud}, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"exportar: {len(aud)} questões auditadas em auditoria.json")
    print(f"exportar: {len(qs)} questões aprovadas em {len(grupos)} arquivos, {len(textos)} textos")


def _publicar_com_limpeza():
    limpar_texto_pdf()
    publicar()


def _diagnostico():
    import bancas_diag; bancas_diag.rodar()


def _sonda_fgv():
    import fgv_sonda; fgv_sonda.rodar()


ETAPAS = {
    "vigiar": vigiar, "noticias": noticiar, "editais": lambda: noticias.ler_editais(maximo=40),
    "planos": lambda: planos.processar(maximo=15), "catalogo": lambda: planos.catalogo(maximo=40),
    "diagnostico": _diagnostico, "sonda_fgv": _sonda_fgv, "classificar_local": classificar_local, "classificar_fila": classificar_fila,
    "processar": processar, "estruturar": estruturar, "enriquecer": enriquecer, "figuras": figuras, "limpar": limpar_texto_pdf, "publicar": _publicar_com_limpeza,
    "gerar": gerar, "classificar": classificar, "auditar": auditar, "exportar": exportar, "alunos": alunos,
}
# Dois repositórios (01/11/2026): o robô roda no PÚBLICO tarlisodoria/fixaconcursos-robo (Actions grátis, log público). Estas
# etapas só rodam no PRIVADO (ROBO_PRIVADO=1): 'alunos' e 'planos' leem/escrevem dado de aluno; 'exportar' grava conteudo/ do
# repositório privado. No público elas são recusadas (etapa avulsa) ou puladas (dentro de rapido/tudo).
SO_PRIVADO = {"alunos", "planos", "exportar"}
MSG_SO_PRIVADO = ("etapa '{etapa}' só roda no repositório privado (ROBO_PRIVADO=1): ela lê ou grava dado de aluno ou o "
                  "conteúdo versionado do site. No robô público ela fica de fora.")
# Ordem por valor: questão já extraída esperando tópico+comentário (enriquecer → publicar) vale mais que caderno novo, e por
# isso vem PRIMEIRO; publicar roda de novo depois do figuras (o que ele liberou). publicar, classificar_local e exportar (no
# fim, com reserva de tempo) nunca são pulados; cada etapa de IA tem orçamento próprio (ORCAMENTO_MIN). O workflow tem 2 jobs:
# RAPIDO (20 min) e depois PESADO (40 min), que começa pelo vigiar (listas das bancas, notícias, editais, planos) para o
# processar já ver os concursos novos. "tudo" (uso local) = os dois em sequência.
RAPIDO = ["enriquecer", "publicar", "figuras", "publicar", "classificar_local", "classificar_fila", "classificar", "auditar", "exportar"]
PESADO = ["vigiar", "processar", "estruturar", "gerar"]
SEQUENCIAS = {"rapido": RAPIDO, "pesado": PESADO, "tudo": RAPIDO + PESADO,
              "enriquecer": ["enriquecer", "publicar", "figuras", "publicar"]}  # enriquecer sozinho também publica o que ficou completo

ia.PRAZO_FINAL = INICIO_JOB + PRAZO_JOB  # o ia.py não começa espera longa (503) que passaria do prazo
_tempos = []  # [{etapa, inicio, fim, segundos, ok}] desta execução


def _gravar_tempos(rodada):
    """Durações por etapa em robo_diagnostico (etapa 'rodada'): a linha mais nova de cada rodada tem todas as etapas até ali."""
    total = round(sum(e["segundos"] for e in _tempos), 1)
    try:
        supa.upsert("robo_diagnostico", [{"banca": "ROBO", "etapa": "rodada", "url": None, "status_http": None, "bytes": None,
                                          "titulo": f"{rodada} · {len(_tempos)} etapa(s) · {total:.0f}s", "itens": len(_tempos),
                                          "detalhe": json.dumps({"rodada": rodada, "total_segundos": total, "etapas": _tempos}, ensure_ascii=False)}])
    except Exception as e:  # noqa: BLE001  (registro de tempo nunca derruba a rodada)
        print(f"  tempos da rodada não gravados: {e}")


def rodar_etapa(nome, rodada, reservar=False):
    """Roda uma etapa, mede a duração e grava logo em seguida o custo e as falhas de IA dela (mesmo se der erro): um timeout
    do job só perde o registro da etapa em andamento. Etapa obrigatória nunca é pulada; etapa de IA usa no máximo o seu
    orçamento e, com `reservar` (exportar vem depois), deixa RESERVA_FIM do prazo do job livre."""
    import time
    ini = time.time()
    obrigatoria = nome in OBRIGATORIAS
    fim_job = INICIO_JOB + PRAZO_JOB - (RESERVA_FIM if reservar and not obrigatoria else 0)
    orc = orcamento(nome)
    prazo = fim_job if orc is None else min(fim_job, ini + orc)
    if not obrigatoria and (job_sem_tempo() or ini >= prazo):
        print(f"===== etapa: {nome} PULADA (prazo do job de {PRAZO_JOB / 60:.0f} min atingido; fica para a próxima rodada)")
        _tempos.append({"etapa": nome, "inicio": None, "fim": None, "segundos": 0, "ok": None, "pulada": True})
        _gravar_tempos(rodada)
        return
    if not obrigatoria:
        ia.PRAZO_FINAL = prazo  # o ia.py para os laços, as esperas e os pedidos novos neste prazo
    print(f"===== etapa: {nome} · início {datetime.now(timezone.utc):%H:%M:%S} UTC"
          + (f" · orçamento {(prazo - ini) / 60:.1f} min" if orc is not None and not obrigatoria else ""))
    ok = False
    try:
        ETAPAS[nome]()
        ok = True
    finally:
        ia.PRAZO_FINAL = INICIO_JOB + PRAZO_JOB
        seg = round(time.time() - ini, 1)
        print(f"===== etapa: {nome} · fim {datetime.now(timezone.utc):%H:%M:%S} UTC · {seg:.0f}s" + ("" if ok else " · COM ERRO"))
        reg = {"etapa": nome, "inicio": datetime.fromtimestamp(ini, timezone.utc).isoformat(timespec="seconds"),
               "fim": datetime.now(timezone.utc).isoformat(timespec="seconds"), "segundos": seg, "ok": ok}
        if orc is not None:
            reg["orcamento_s"] = round(prazo - ini)
        _tempos.append(reg)
        ia.resumo(f"{rodada} › {nome}")
        _gravar_tempos(rodada)


def rodar_sequencia(seq, rodada):
    """Roda as etapas em ordem; uma etapa com erro não impede as seguintes (o erro sobe no fim, para o job ficar vermelho).
    Fora do privado, as etapas SO_PRIVADO ficam de fora (com aviso no log)."""
    if not supa.privado():
        fora = [e for e in seq if e in SO_PRIVADO]
        if fora:
            print(f"robô público: {', '.join(dict.fromkeys(fora))} fica(m) de fora (só no repositório privado)")
        seq = [e for e in seq if e not in SO_PRIVADO]
    erro = None
    for i, etapa in enumerate(seq):
        try:
            rodar_etapa(etapa, rodada, reservar="exportar" in seq[i + 1:])
        except Exception as e:  # noqa: BLE001
            print(f"===== etapa: {etapa} falhou ({type(e).__name__}: {e}); seguindo para a próxima")
            erro = erro or e
    if erro:
        raise erro


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "tudo").strip().lower() or "tudo"  # "rapido " digitado com espaço no Actions
    seq = SEQUENCIAS.get(cmd, [cmd])
    desconhecidas = [e for e in seq if e not in ETAPAS]
    if desconhecidas:
        sys.exit(f"etapa desconhecida: {', '.join(desconhecidas)} (use {', '.join(sorted(set(ETAPAS) | set(SEQUENCIAS)))})")
    if cmd in SO_PRIVADO and not supa.privado():
        sys.exit(MSG_SO_PRIVADO.format(etapa=cmd))
    # rodada do robô público leva a marca " [público]" (o Painel separa os minutos do privado das rodadas do público)
    rodada = f"{cmd} {datetime.now(timezone.utc):%Y-%m-%d %H:%M}" + (" [público]" if os.environ.get("ROBO_PUBLICO") == "1" else "")
    ia.descobrir_modelos()  # IDs do Gemini que existem na conta; descarta das listas os que não existem (com aviso no log)
    rodar_sequencia(seq, rodada)
