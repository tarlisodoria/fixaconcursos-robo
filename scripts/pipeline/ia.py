"""Acesso único à API da Anthropic para o robô: escolhe o modelo, usa a API em lote quando dá, e mede o custo.

Modelos (variáveis do repositório no GitHub):
  ANTHROPIC_MODEL         principal: transcrever cadernos, escrever comentários, gerar questões inéditas
  ANTHROPIC_MODEL_RAPIDO  barato (ex.: Haiku): classificar, auditar, estruturar textos. Sem ela, usa o principal.

API em lote (Message Batches): 50% mais barata, responde em minutos ou horas. O robô envia, espera alguns minutos
e, se não terminou, guarda o lote na tabela ia_lotes e recolhe o resultado na rodada seguinte.

Custo: cada chamada soma tokens por tarefa; ao fim de cada etapa `resumo()` grava em ia_uso e no resumo do GitHub Actions
(se o job for cortado no timeout, só a etapa em andamento fica sem registro).
Preços (US$ por milhão de tokens) são estimativas da tabela pública; ajuste PRECOS se a Anthropic mudar.
"""
import json, os, re, time
import requests

API = "https://api.anthropic.com/v1"
VERSAO = "2023-06-01"
ESPERA_LOTE = 4 * 60  # segundos esperando o lote nesta rodada (o resto é recolhido amanhã)

# entrada, saída por milhão de tokens; cache: leitura 0,1x e escrita 1,25x da entrada; lote: 50%
PRECOS = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}

_uso = {}  # (tarefa, modelo, em_lote) -> dict de contadores
_falhas = {}  # (provedor, código) -> vezes nesta etapa: "gemini 503", "conferente 403"... (gravado com o custo em resumo())

# Prazo do job (epoch), definido pelo rodar.py: perto dele o robô não começa espera longa nem chamada nova
PRAZO_FINAL = None


def sem_tempo(segundos=0):
    """Esperar `segundos` (ou começar algo agora) passaria do prazo do job?"""
    return PRAZO_FINAL is not None and time.time() + segundos > PRAZO_FINAL


def _timeout_pedido():
    """Um pedido não espera muito além do prazo (da etapa ou do job): até 10 min, no mínimo 60 s."""
    return 600 if PRAZO_FINAL is None else min(600, max(60, PRAZO_FINAL - time.time() + 30))


_amostras = {}  # (provedor, código) -> começo da 1ª resposta de erro (para diagnosticar sem abrir o log do Actions)


def falha(provedor, codigo, amostra=None):
    """Toda resposta de erro de IA vira registro (inclusive as que uma nova tentativa resolveu)."""
    k = (provedor, str(codigo))
    _falhas[k] = _falhas.get(k, 0) + 1
    if amostra and k not in _amostras:
        _amostras[k] = str(amostra)[:300]

# Sem dinheiro para IA: a variável do repositório IA_DESLIGADA=1 desliga tudo de propósito; e se a API responder
# "credit balance is too low", o robô desliga a IA sozinho até o fim da rodada. Em ambos os casos o que depende de IA
# (transcrever caderno escaneado/Cebraspe/FGV, comentar, classificar...) fica guardado para depois, sem erro.
_sem_credito = False


def desligada():
    """IA_DESLIGADA=1 desliga a IA paga (Anthropic). A reserva gratuita (Gemini) segue pela própria variável GEMINI_DESLIGADO."""
    return (os.environ.get("IA_DESLIGADA") or "").strip().lower() in ("1", "true", "sim", "yes", "on")


def checar_erro(e):
    """Se o erro é falta de crédito na Anthropic, desliga a IA nesta rodada. -> True quando for esse o caso."""
    global _sem_credito
    if "credit balance" in str(e).lower():
        if not _sem_credito:
            print("ia: sem crédito na Anthropic — IA desligada nesta rodada; o que depende dela fica guardado para depois")
        _sem_credito = True
        return True
    return False


def chave():
    return os.environ.get("ANTHROPIC_API_KEY")


def modelo(rapido=False):
    principal = os.environ.get("ANTHROPIC_MODEL")
    return (os.environ.get("ANTHROPIC_MODEL_RAPIDO") or principal) if rapido else principal


# Teto de gasto do mês (todas as tarefas de IA somadas). Variável do repositório IA_LIMITE_MENSAL_USD; padrão US$ 50.
_gasto_mes = None
_avisou = False


def limite_mensal():
    try:
        return float(os.environ.get("IA_LIMITE_MENSAL_USD") or 50)
    except ValueError:
        return 50.0


def gasto_mes():
    """Gasto do mês corrente: o que já está em ia_uso + o desta rodada (ainda não gravado)."""
    global _gasto_mes
    if _gasto_mes is None:
        try:
            import supa
            ini = time.strftime("%Y-%m-01T00:00:00Z", time.gmtime())
            _gasto_mes = sum(float(r.get("custo_usd") or 0) for r in supa.select("ia_uso", {"select": "custo_usd", "criado_em": f"gte.{ini}"}))
        except Exception as e:  # noqa: BLE001  (sem leitura do gasto, não bloqueia)
            print(f"ia: não consegui ler o gasto do mês ({e})"); _gasto_mes = 0.0
    return _gasto_mes + sum(c["custo_usd"] for c in _uso.values())


def orcamento_ok():
    global _avisou
    ok = gasto_mes() < limite_mensal()
    if not ok and not _avisou:
        print(f"ia: teto do mês atingido (US$ {gasto_mes():.2f} de US$ {limite_mensal():.2f}); tarefas de IA ficam para o mês que vem "
              "ou aumente IA_LIMITE_MENSAL_USD nas variáveis do repositório")
        _avisou = True
    return ok


def anthropic_ok():
    """IA principal (Claude) utilizável nesta rodada."""
    return not desligada() and not _sem_credito and bool(chave() and modelo()) and orcamento_ok()


def disponivel(tarefa=None):
    """Há IA para a tarefa? Sem `tarefa` = só a principal (Claude). Com `tarefa` de conteúdo público (TAREFAS_GEMINI),
    vale também a reserva gratuita (Gemini)."""
    return anthropic_ok() or (tarefa is not None and gemini_disponivel(tarefa))


# ------------------------------------------------------------------ reserva gratuita: Gemini (Google AI Studio)
# Usada só quando o Claude não está disponível (sem crédito, IA_DESLIGADA, teto mensal) ou esgotou as tentativas, e SÓ em
# tarefas que enviam conteúdo público de provas. REGRA: nunca mandar dado de aluno (tentativas, perfis, reportes, e-mails)
# ao Gemini: no plano gratuito o Google pode usar o conteúdo enviado para melhorar os produtos dele.
#
# A cota grátis é POR MODELO (AI Studio, projeto gratuito, 27/10/2026): Flash (3.8, 3.6, 3, 2.5) = 5 RPM e 20 pedidos/dia cada;
# Flash Lite (3.5, 3.1) = 15 RPM e 500/dia cada; Gemma 4 31B = 30 RPM, 16K TPM, 14.4K/dia. Por isso há duas listas, em ordem:
#   GEMINI_MODELOS_PESADO (transcrever caderno: PDF grande) e GEMINI_MODELOS_LEVE (enriquecer, gabarito, classificar, auditar).
# Cada modelo tem contador de pedidos e espaçamento mínimo entre pedidos (RPM); cota do dia esgotada ou 503 persistente tira SÓ
# aquele modelo da rodada e o pedido passa ao próximo da lista. Os IDs padrão foram deduzidos dos nomes de exibição: no início da
# rodada descobrir_modelos() lista os IDs que existem na conta e descarta da lista os que não existem (com aviso no log).
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
TAREFAS_GEMINI = ("transcrever", "gabarito", "classificar_fila", "classificar", "auditar", "enriquecer", "conferir", "figuras")  # enriquecer: só texto público da questão + tópicos
TAREFAS_PESADAS = ("transcrever", "figuras")  # figuras: página do caderno como imagem (multimodal)
# conferencia: segunda opinião sobre o que o Gemini escreveu quando o GitHub Models não responde. Gemma 4 31B primeiro (outra
# família de modelo que a do escritor); nunca o mesmo modelo que escreveu (ver conferir()).
MODELOS_PADRAO = {"pesado": "gemini-3.8-flash,gemini-3.6-flash,gemini-3-flash", "leve": "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
                  "conferencia": "gemma-4-31b-it,gemini-3.1-flash-lite"}
# Tetos da rodada (variáveis GEMINI_MAX_<TAREFA> e GEMINI_MAX_CHAMADAS), pensados para as cotas acima: só conta a chamada que
# terminou (sucesso ou erro definitivo); nova tentativa por 503/429/rede não gasta teto. Tarefa sem padrão só respeita o geral.
TETO_GEMINI_PADRAO = {"enriquecer": 60, "gabarito": 30, "transcrever": 15, "conferir": 120, "figuras": 10, "classificar_fila": 40}  # figuras: Flash = 20/dia
TETO_GERAL_PADRAO = 150
TENTATIVAS_POR_MODELO = 3
MSG_SOBRECARGA = "Gemini sobrecarregado ({codigo}): tenta na próxima rodada"
MSG_SEM_COTA = "cota diária de todos os modelos {classe}s esgotada; tenta amanhã"

_gemini_chamadas = 0
_gemini_por_tarefa = {}   # tarefa-base -> chamadas terminadas nesta rodada
_pedidos_modelo = {}      # modelo -> pedidos HTTP enviados nesta etapa (gravado em robo_diagnostico, etapa 'ia_modelos')
_ultimo_pedido = {}       # modelo -> time.time() do último pedido (espaçamento de RPM)
_modelo_fora = {}         # modelo -> "cota" | "sobrecarga" | "inexistente": fora no resto da rodada
_existentes = None        # IDs que existem na conta (descobrir_modelos); None = não consultado
_avisos_reserva = set()


class GeminiIndisponivel(RuntimeError):
    """Sem modelo Gemini para a tarefa. A mensagem diz a causa real e vai para ingestao_provas.erro."""


class GeminiSobrecarregado(GeminiIndisponivel):
    """503/500 (ou 429 por minuto) em todas as tentativas de todos os modelos da lista."""


class GeminiSemCota(GeminiIndisponivel):
    """Cota diária esgotada em todos os modelos da lista da tarefa."""


class _Proximo(Exception):
    """Este modelo não serve agora (cota do dia, sobrecarga, inexistente, rede): passa ao próximo da lista."""
    def __init__(self, codigo):
        super().__init__(codigo)
        self.codigo = codigo


def _env_int(nome, padrao):
    try:
        return int(os.environ.get(nome) or padrao)
    except ValueError:
        return padrao


def tarefa_base(tarefa):
    """'transcrever caderno (AOCP)' -> 'transcrever'; tarefa que não é de conteúdo público -> None."""
    return next((t for t in TAREFAS_GEMINI if str(tarefa).startswith(t)), None)


def classe_gemini(tarefa):
    base = tarefa_base(tarefa)
    return "pesado" if base in TAREFAS_PESADAS else "conferencia" if base == "conferir" else "leve"


def teto_gemini(tarefa):
    """Teto de chamadas desta tarefa por rodada (GEMINI_MAX_<TAREFA>); None = só o teto geral."""
    base = tarefa_base(tarefa)
    if base is None:
        return None
    padrao = TETO_GEMINI_PADRAO.get(base)
    if padrao is None and not os.environ.get(f"GEMINI_MAX_{base.upper()}"):
        return None
    return _env_int(f"GEMINI_MAX_{base.upper()}", padrao)


def intervalo_rpm(mod):
    """Espera mínima entre pedidos ao mesmo modelo: 60/RPM + folga (Flash 5 RPM -> 13 s; Lite 15 RPM -> 4,5 s; Gemma 30 RPM -> 2,5 s)."""
    m = mod.lower()
    # Gemma: 30 RPM, mas só 16K tokens/min; um pedido de conferência tem ~4K tokens -> no máximo ~4 por minuto
    return 4.5 if "lite" in m else 16.0 if "gemma" in m else 13.0


def modelos_configurados(classe):
    """Lista da classe (variável ou padrão), sem repetição; GEMINI_MODEL (compatibilidade) é o primeiro da lista pesada."""
    lista = [x.strip() for x in (os.environ.get(f"GEMINI_MODELOS_{classe.upper()}") or MODELOS_PADRAO[classe]).split(",") if x.strip()]
    principal = (os.environ.get("GEMINI_MODEL") or "").strip()
    if classe == "pesado" and principal:
        lista = [principal] + lista
    return list(dict.fromkeys(lista))


def modelos_gemini(tarefa):
    """Modelos ainda utilizáveis para a tarefa nesta rodada, em ordem: existem na conta (se consultado) e não estão fora."""
    return [m for m in modelos_configurados(classe_gemini(tarefa))
            if (_existentes is None or m in _existentes) and m not in _modelo_fora]


# Pedidos/dia do plano grátis (o mesmo quadro do CLAUDE.md e do Painel, LIMITE_DIA_GEMINI em page.tsx)
LIMITE_DIA = [("flash-lite", 500), ("gemma", 14400), ("flash", 20)]


def limite_dia(mod):
    return next((n for chave, n in LIMITE_DIA if chave in mod.lower()), None)


def inicio_dia_pacifico(agora=None):
    """O Google zera a cota à meia-noite de America/Los_Angeles (com horário de verão). Sem tzdata: 08:00 UTC (conta menos, nunca mais)."""
    from datetime import datetime, timedelta, timezone
    agora = agora or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        la = agora.astimezone(ZoneInfo("America/Los_Angeles"))
        return la.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        base = agora.replace(hour=8, minute=0, second=0, microsecond=0)
        return base if agora >= base else base - timedelta(days=1)


def cota_pesada_esgotada():
    """Todos os modelos da lista PESADA (transcrever) já gastaram os pedidos do dia? Consulta rápida em robo_diagnostico
    (etapa ia_modelos, gravada ao fim de cada etapa) somada ao que esta execução já sabe (_modelo_fora). Erro na consulta -> False."""
    import supa
    lista = modelos_configurados("pesado")
    if not lista:
        return True
    try:
        linhas = supa.select("robo_diagnostico", {"select": "detalhe", "etapa": "eq.ia_modelos",
                                                  "criado_em": f"gte.{inicio_dia_pacifico().isoformat()}"})
    except Exception as e:  # noqa: BLE001
        print(f"  cota do dia: não consegui consultar ({e}); considero disponível")
        return False
    usados = {}
    for ln in linhas:
        try:
            for m, n in json.loads(ln.get("detalhe") or "{}").items():
                usados[m] = usados.get(m, 0) + int(n)
        except (ValueError, TypeError, AttributeError):
            continue
    return all(_modelo_fora.get(m) in ("cota", "inexistente") or (limite_dia(m) or 10**9) <= usados.get(m, 0) for m in lista)


def gemini_desligado():
    return (os.environ.get("GEMINI_DESLIGADO") or "").strip().lower() in ("1", "true", "sim", "yes", "on")


def descobrir_modelos():
    """Chamado no início da rodada: lista os IDs da conta (GET /models, chave no cabeçalho), registra no log e descarta das
    listas os que não existem. Falhou a consulta: segue com as listas como estão (o 404 de um modelo também o tira da rodada)."""
    global _existentes
    if not os.environ.get("GEMINI_API_KEY") or gemini_desligado():
        return
    cab = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    ids, pagina = set(), None
    try:
        for _ in range(10):
            params = {"pageSize": 1000}
            if pagina:
                params["pageToken"] = pagina
            r = requests.get(f"{GEMINI_API}/models", timeout=60, headers=cab, params=params)
            if r.status_code >= 300:
                falha("gemini /models", r.status_code)
                print(f"ia: não consegui listar os modelos do Gemini (HTTP {r.status_code}); sigo com as listas configuradas")
                return
            j = r.json()
            ids |= {m["name"].split("/", 1)[-1] for m in j.get("models", []) if m.get("name")}
            pagina = j.get("nextPageToken")
            if not pagina:
                break
    except requests.RequestException as e:
        falha("gemini /models", "rede")
        print(f"ia: não consegui listar os modelos do Gemini ({type(e).__name__}); sigo com as listas configuradas")
        return
    _existentes = ids
    relevantes = sorted(i for i in ids if i.startswith(("gemini", "gemma")))
    print(f"ia: {len(ids)} modelos na conta do Gemini; gemini/gemma: {', '.join(relevantes)}")
    for classe in ("pesado", "leve"):
        lista = modelos_configurados(classe)
        fora = [m for m in lista if m not in ids]
        if fora:
            print(f"ia: AVISO — modelos {classe}s que não existem na conta, descartados: {', '.join(fora)} (ajuste GEMINI_MODELOS_{classe.upper()})")
        print(f"ia: modelos {classe}s em uso, em ordem: {', '.join(m for m in lista if m in ids) or 'nenhum'}")


def gemini_disponivel(tarefa=None):
    if gemini_desligado() or not os.environ.get("GEMINI_API_KEY"):
        return False
    if _gemini_chamadas >= _env_int("GEMINI_MAX_CHAMADAS", TETO_GERAL_PADRAO):
        return False
    if tarefa is None:
        return bool(modelos_gemini("transcrever") or modelos_gemini("enriquecer"))
    base = tarefa_base(tarefa)
    if base is None:
        return False
    teto = teto_gemini(base)
    if teto is not None and _gemini_por_tarefa.get(base, 0) >= teto:
        return False
    return bool(modelos_gemini(base))


def _motivo_gemini(tarefa):
    """Causa real de não haver Gemini para a tarefa (ou None se há)."""
    base = tarefa_base(tarefa)
    if gemini_desligado():
        return "Gemini desligado (GEMINI_DESLIGADO)"
    if not os.environ.get("GEMINI_API_KEY"):
        return "Gemini não configurado (sem GEMINI_API_KEY)"
    if base is None:
        return "tarefa não vai ao Gemini (não é conteúdo público de prova)"
    if _gemini_chamadas >= _env_int("GEMINI_MAX_CHAMADAS", TETO_GERAL_PADRAO):
        return f"Gemini: teto geral da rodada atingido ({_env_int('GEMINI_MAX_CHAMADAS', TETO_GERAL_PADRAO)}); tenta na próxima rodada"
    teto = teto_gemini(base)
    if teto is not None and _gemini_por_tarefa.get(base, 0) >= teto:
        return f"Gemini: teto de '{base}' da rodada atingido ({teto}); tenta na próxima rodada"
    if modelos_gemini(base):
        return None
    classe = classe_gemini(base)
    config = [m for m in modelos_configurados(classe) if _existentes is None or m in _existentes]
    if not config:
        return f"nenhum modelo {classe} da lista existe na conta do Gemini (ajuste GEMINI_MODELOS_{classe.upper()})"
    motivos = {_modelo_fora.get(m) for m in config}
    if motivos == {"cota"}:
        return MSG_SEM_COTA.format(classe=classe)
    if "sobrecarga" in motivos:
        return MSG_SOBRECARGA.format(codigo="503")
    return f"modelos {classe}s do Gemini indisponíveis nesta rodada; tenta na próxima"


def motivo_indisponivel(tarefa):
    """Por que não há IA para a tarefa agora: a frase que vai para ingestao_provas.erro (nunca um "IA desligada" genérico
    quando a IA está configurada)."""
    gem = _motivo_gemini(tarefa)
    if gem in (MSG_SOBRECARGA.format(codigo="503"), MSG_SEM_COTA.format(classe=classe_gemini(tarefa))):
        return gem  # a causa que decide é a do Gemini; a frase fica exata para o painel
    claude = ("IA_DESLIGADA=1" if desligada() else "sem crédito no Claude" if _sem_credito else
              "sem chave/modelo do Claude" if not (chave() and modelo()) else "teto mensal de IA atingido" if not orcamento_ok() else None)
    partes = [m for m in (claude, gem) if m]
    return "; ".join(partes) if partes else "IA indisponível nesta rodada"


def _erro_gemini(motivo):
    """Exceção com a causa real (o tipo diz se é sobrecarga, cota ou outra coisa)."""
    if motivo.startswith("cota diária"):
        return GeminiSemCota(motivo)
    if motivo.startswith("Gemini sobrecarregado"):
        return GeminiSobrecarregado(motivo)
    return GeminiIndisponivel(motivo)


def _avisar_reserva(tarefa, motivo):
    if (tarefa, motivo) not in _avisos_reserva:
        _avisos_reserva.add((tarefa, motivo))
        print(f"  ia[{tarefa}]: usando a reserva Gemini ({', '.join(modelos_gemini(tarefa))}) — motivo: {motivo}")


def _partes_gemini(conteudo):
    """Blocos no formato da Anthropic -> partes do Gemini (texto, PDF e imagem em base64)."""
    if isinstance(conteudo, str):
        return [{"text": conteudo}]
    out = []
    for b in conteudo:
        tipo, fonte = b.get("type"), b.get("source") or {}
        if tipo == "text":
            out.append({"text": b["text"]})
        elif tipo in ("document", "image") and fonte.get("type") == "base64":
            out.append({"inline_data": {"mime_type": fonte["media_type"], "data": fonte["data"]}})
        else:
            raise ValueError(f"bloco não suportado pelo Gemini: {tipo}")
    return out


def _contar_gemini(tarefa):
    """Conta UMA chamada no teto geral e no da tarefa: só a que terminou (sucesso ou erro definitivo); nova tentativa não conta."""
    global _gemini_chamadas
    _gemini_chamadas += 1
    base = tarefa_base(tarefa)
    _gemini_por_tarefa[base] = _gemini_por_tarefa.get(base, 0) + 1


def _espera_sobrecarga(t):
    """503/500: 30 s, 60 s, 120 s (com variação aleatória de ±25%)."""
    import random
    return 30 * (2 ** t) * random.uniform(0.75, 1.25)


def _esperar_rpm(mod):
    """Respeita o RPM do modelo: espera o que falta do intervalo mínimo desde o último pedido a ele."""
    falta = _ultimo_pedido.get(mod, 0) + intervalo_rpm(mod) - time.time()
    if falta > 0:
        time.sleep(falta)


def _tentar_modelo(tarefa, mod, corpo, tentativas=TENTATIVAS_POR_MODELO):
    """-> resposta JSON. Erro definitivo do pedido (400...): RuntimeError (conta no teto). Modelo não serve agora: _Proximo."""
    cab = {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "content-type": "application/json"}  # chave no cabeçalho, nunca na URL/log
    for t in range(tentativas):
        ultima = t == tentativas - 1
        _esperar_rpm(mod)
        _pedidos_modelo[mod] = _pedidos_modelo.get(mod, 0) + 1
        _ultimo_pedido[mod] = time.time()
        try:
            r = requests.post(f"{GEMINI_API}/models/{mod}:generateContent", timeout=_timeout_pedido(), headers=cab, json=corpo)
        except (requests.ConnectionError, requests.Timeout) as e:
            falha(mod, "rede")
            espera = _espera(t, base=15)
            if ultima or sem_tempo(espera):
                raise _Proximo("rede")
            print(f"  gemini[{tarefa}]: {mod} rede ({type(e).__name__}), aguardando {espera:.0f}s")
            time.sleep(espera)
            continue
        if r.status_code == 429 and re.search(r"per ?day|PerDay|daily", r.text, re.I):
            falha(mod, "429 cota do dia")
            _modelo_fora[mod] = "cota"  # só este modelo: os outros têm cota própria
            raise _Proximo("429 cota do dia")
        if r.status_code == 404:
            falha(mod, "404")
            _modelo_fora[mod] = "inexistente"
            raise _Proximo("404")
        if r.status_code in (429, 500, 502, 503, 504):
            falha(mod, r.status_code)
            espera = (int(r.headers.get("retry-after", "0") or 0) if r.status_code == 429 else 0) or _espera_sobrecarga(t)
            if ultima or sem_tempo(espera):
                _modelo_fora[mod] = "sobrecarga"
                raise _Proximo(str(r.status_code))
            print(f"  gemini[{tarefa}]: {mod} API {r.status_code}, tentativa {t + 1}/{tentativas}, aguardando {espera:.0f}s")
            time.sleep(espera)
            continue
        if r.status_code >= 300:
            falha(mod, r.status_code)
            _contar_gemini(tarefa)
            raise RuntimeError(f"Gemini {mod} {r.status_code}: {r.text[:300]}")
        _contar_gemini(tarefa)
        return r.json()
    raise _Proximo("?")  # não chega aqui: a última tentativa sempre retorna ou levanta


def gemini(tarefa, conteudo, max_tokens=4000, system=None, tentativas=TENTATIVAS_POR_MODELO, modelos=None):
    """Chamada ao Gemini percorrendo a lista de modelos da tarefa. -> (texto, parada) com parada no vocabulário da Anthropic.
    Sem modelo que sirva: GeminiSemCota / GeminiSobrecarregado / GeminiIndisponivel com a causa real."""
    if not gemini_disponivel(tarefa):
        raise _erro_gemini(_motivo_gemini(tarefa) or motivo_indisponivel(tarefa))
    corpo = {"contents": [{"role": "user", "parts": _partes_gemini(conteudo)}],
             "generationConfig": {"maxOutputTokens": min(max_tokens, _env_int("GEMINI_MAX_SAIDA", 65536))}}
    if system:
        # o system pode vir no formato da Anthropic (lista de blocos com cache_control): o Gemini quer só o texto
        txt_sys = system if isinstance(system, str) else "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))
        corpo["systemInstruction"] = {"parts": [{"text": txt_sys}]}
    j = mod = None
    for m in [x for x in modelos if x in modelos_gemini(tarefa)] if modelos else modelos_gemini(tarefa):
        if sem_tempo():
            raise GeminiIndisponivel("prazo da etapa (ou do job) no fim: o Gemini fica para a próxima rodada")
        try:
            j, mod = _tentar_modelo(tarefa, m, corpo, tentativas), m
            break
        except _Proximo as e:
            print(f"  gemini[{tarefa}]: {m} fora desta rodada ({e.codigo}); passando ao próximo modelo da lista")
    if j is None:
        raise _erro_gemini(_motivo_gemini(tarefa) or MSG_SOBRECARGA.format(codigo="503"))
    cand = (j.get("candidates") or [{}])[0]
    txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
    fim = cand.get("finishReason") or ""
    parada = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}.get(fim, fim.lower() or None)
    um = j.get("usageMetadata") or {}
    registrar(tarefa, f"gemini:{mod}", {"input_tokens": um.get("promptTokenCount", 0), "output_tokens": um.get("candidatesTokenCount", 0)})
    global ultimo_modelo_gemini
    ultimo_modelo_gemini = mod
    return txt, parada


# ------------------------------------------------------------------ conferente gratuito: GitHub Models
# Segunda opinião quando quem escreveu foi o Gemini: outra empresa, outro modelo. Roda no GitHub Actions com o próprio
# GITHUB_TOKEN (permissão `models: read` no workflow), sem chave nova. Cota grátis pequena (cerca de 150 pedidos/dia nos
# modelos "low", 8 mil tokens de entrada por pedido): por isso pedidos curtos e teto por rodada. Só conteúdo público.
# 28/10/2026: no Actions ele responde "HTTP 200 text/plain OK" em TODA rodada desde 27/10. Fica FORA da cadeia por padrão;
# CONFERENTE_GITHUB=1 religa. Sem ele, a conferência vai direto à lista GEMINI_MODELOS_CONFERENCIA (nunca o modelo que escreveu).
GITHUB_MODELS_API = "https://models.github.ai/inference/chat/completions"
_conf_chamadas = 0
_conf_esgotado = False
ultimo_conferente = None  # quem conferiu a última vez: "github" ou o modelo do Google (vai para enriquecido_por)


def github_ligado():
    return (os.environ.get("CONFERENTE_GITHUB") or "").strip().lower() in ("1", "true", "sim", "yes", "on")


def _github_disponivel():
    return github_ligado() and bool(os.environ.get("GITHUB_TOKEN")) and not _conf_esgotado and _conf_chamadas < _env_int("CONFERENTE_MAX_CHAMADAS", 60)


def conferente_disponivel():
    """Há quem confira: um modelo do Google diferente do que escreveu (Gemma 4 31B...) ou, se religado, o GitHub Models."""
    if (os.environ.get("CONFERENTE_DESLIGADO") or "").strip().lower() in ("1", "true", "sim", "yes", "on"):
        return False
    return _github_disponivel() or gemini_disponivel("conferir")


def conferir(tarefa, texto, max_tokens=3000, tentativas=4, escritor=None):
    """Segunda opinião. GitHub Models só com CONFERENTE_GITHUB=1 (e, se falhar, segue); senão direto um modelo da lista
    'conferencia' que NÃO seja o que escreveu (escritor = nome do modelo Gemini que escreveu). -> texto. RuntimeError se ninguém puder conferir."""
    if (os.environ.get("CONFERENTE_DESLIGADO") or "").strip().lower() in ("1", "true", "sim", "yes", "on"):
        raise RuntimeError("conferente desligado")
    global ultimo_conferente
    if _github_disponivel():
        try:
            txt = _conferir_github(tarefa, texto, max_tokens, tentativas)
            ultimo_conferente = "github"
            return txt
        except RuntimeError as e:
            print(f"  conferente[{tarefa}]: GitHub Models falhou ({e}); tentando o conferente do Google")
    if not gemini_disponivel("conferir"):
        raise RuntimeError("conferente indisponível (sem modelo do Google para conferir)")
    lista = [m for m in modelos_gemini("conferir") if m != escritor]
    if not lista:
        raise RuntimeError("conferente indisponível: o único modelo de conferência é o mesmo que escreveu")
    try:
        # 28/10: o Gemma vive sobrecarregado; 2 respostas 500/503 seguidas e a conferência já passa ao próximo da lista
        txt, _ = gemini(f"conferir {tarefa}", texto, max_tokens, modelos=lista, tentativas=_env_int("CONFERENCIA_TENTATIVAS", 2))
    except RuntimeError as e:
        raise RuntimeError(f"conferente do Google falhou: {e}") from e
    ultimo_conferente = globals().get("ultimo_modelo_gemini") or "google"
    return txt


def _conferir_github(tarefa, texto, max_tokens=3000, tentativas=4):
    """Pergunta ao GitHub Models. -> texto. RuntimeError se indisponível."""
    global _conf_chamadas, _conf_esgotado
    mod = os.environ.get("CONFERENTE_MODEL") or "openai/gpt-4.1-mini"
    cab = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/json",
           "X-GitHub-Api-Version": "2022-11-28", "content-type": "application/json"}
    corpo = {"model": mod, "max_tokens": min(max_tokens, 4000), "temperature": 0, "messages": [{"role": "user", "content": texto}]}
    for t in range(tentativas):
        _conf_chamadas += 1
        try:
            # sem seguir redirecionamento: o requests troca POST por GET num 301/302 e a resposta vira um "OK" de texto
            # (27/10: HTTP 200 text/plain "OK"). 307/308 repetem o POST no novo endereço; 301/302 viram erro com o endereço.
            r = requests.post(GITHUB_MODELS_API, timeout=180, headers=cab, json=corpo, allow_redirects=False)
            if r.status_code in (307, 308) and r.headers.get("location"):
                r = requests.post(r.headers["location"], timeout=180, headers=cab, json=corpo, allow_redirects=False)
        except (requests.ConnectionError, requests.Timeout) as e:
            falha("conferente", "rede")
            espera = _espera(t, base=10)
            print(f"  conferente[{tarefa}]: rede ({type(e).__name__}), aguardando {espera:.0f}s"); time.sleep(espera); continue
        if r.status_code >= 300:
            falha("conferente", r.status_code, f"location={r.headers.get('location')!r} corpo={r.text[:200]!r}")
        if r.status_code == 429:
            espera = int(r.headers.get("retry-after", "0") or 0)
            if espera > 120 or re.search(r"per ?day|daily|UserByDay", r.text, re.I):
                _conf_esgotado = True
                raise RuntimeError("conferente: cota gratuita do dia esgotada")
            espera = espera or _espera(t, base=15)
            print(f"  conferente[{tarefa}]: limite por minuto, aguardando {espera:.0f}s"); time.sleep(espera); continue
        if r.status_code in (500, 502, 503, 504):
            time.sleep(_espera(t, base=10)); continue
        if r.status_code >= 300:
            if r.status_code in (401, 403):
                _conf_esgotado = True  # sem permissão models: read: não adianta insistir nesta rodada
            raise RuntimeError(f"conferente {r.status_code}: {r.text[:200]}")
        try:
            j = r.json()
        except ValueError:  # 2xx sem JSON (corpo vazio, página HTML): registra o que veio; na 2ª vez desiste da rodada
            amostra = f"HTTP {r.status_code} content-type={r.headers.get('content-type')!r} corpo={r.text[:200]!r}"
            falha("conferente", "resposta-nao-json", amostra)
            print(f"  conferente[{tarefa}]: resposta sem JSON: {amostra}")
            if t >= 1:
                _conf_esgotado = True  # insistir só queima o tempo do job (27/10: job cancelado aos 20 min)
                raise RuntimeError("conferente: respostas sem JSON; desligado nesta rodada")
            time.sleep(5); continue
        u = j.get("usage") or {}
        registrar(tarefa, f"github:{mod}", {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)})
        return ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    raise RuntimeError("conferente indisponível após várias tentativas")


def _h():
    return {"x-api-key": chave(), "anthropic-version": VERSAO, "content-type": "application/json"}


def preco(mod, u, em_lote=False):
    m = (mod or "").lower()
    if m.startswith(("gemini", "github")):
        return 0.0  # reserva / conferente no plano gratuito
    ent, sai = next((v for k, v in PRECOS.items() if k in m), PRECOS["sonnet"])
    usd = (u.get("input_tokens", 0) * ent + u.get("output_tokens", 0) * sai
           + u.get("cache_read_input_tokens", 0) * ent * 0.1 + u.get("cache_creation_input_tokens", 0) * ent * 1.25) / 1e6
    return usd * (0.5 if em_lote else 1)


def registrar(tarefa, mod, usage, em_lote=False):
    if not usage:
        return
    k = (tarefa, mod, em_lote)
    c = _uso.setdefault(k, {"chamadas": 0, "entrada": 0, "saida": 0, "cache_leitura": 0, "cache_escrita": 0, "custo_usd": 0.0})
    c["chamadas"] += 1
    c["entrada"] += usage.get("input_tokens", 0) or 0
    c["saida"] += usage.get("output_tokens", 0) or 0
    c["cache_leitura"] += usage.get("cache_read_input_tokens", 0) or 0
    c["cache_escrita"] += usage.get("cache_creation_input_tokens", 0) or 0
    c["custo_usd"] += preco(mod, usage, em_lote)


def _texto(msg):
    return "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")


def _espera(t, base=20, teto=300):
    """Espera exponencial com variação aleatória (vários pedidos não voltam todos no mesmo segundo)."""
    import random
    return min(teto, base * (2 ** t)) * random.uniform(0.5, 1.0)


def chamar(tarefa, conteudo, max_tokens=4000, system=None, rapido=False, tentativas=6):
    """Chamada direta. conteudo: texto ou lista de blocos. -> (texto, stop_reason)
    Ordem: Claude (com novas tentativas em erro passageiro) -> Gemini grátis, só em tarefa de conteúdo público."""
    if not anthropic_ok():
        if gemini_disponivel(tarefa):
            _avisar_reserva(tarefa, "Claude indisponível (sem crédito, IA_DESLIGADA ou teto mensal)")
            return gemini(tarefa, conteudo, max_tokens, system)
        raise RuntimeError(motivo_indisponivel(tarefa))
    try:
        return _chamar_anthropic(tarefa, conteudo, max_tokens, system, rapido, tentativas)
    except RuntimeError as e:
        # sem crédito (checar_erro já desligou o Claude) ou tentativas esgotadas: troca de provedor se a tarefa permitir
        if gemini_disponivel(tarefa) and (_sem_credito or "várias tentativas" in str(e)):
            _avisar_reserva(tarefa, "sem crédito na Anthropic" if _sem_credito else "Claude esgotou as tentativas")
            return gemini(tarefa, conteudo, max_tokens, system)
        raise


def _chamar_anthropic(tarefa, conteudo, max_tokens, system, rapido, tentativas):
    mod = modelo(rapido)
    corpo = {"model": mod, "max_tokens": max_tokens, "messages": [{"role": "user", "content": conteudo}]}
    if system:
        corpo["system"] = system
    for t in range(tentativas):
        try:
            r = requests.post(f"{API}/messages", timeout=600, headers=_h(), json=corpo)
        except (requests.ConnectionError, requests.Timeout) as e:  # rede caiu ou demorou: tenta de novo
            falha("claude", "rede")
            espera = _espera(t)
            print(f"  ia[{tarefa}]: rede ({type(e).__name__}), aguardando {espera:.0f}s"); time.sleep(espera); continue
        if r.status_code >= 300:
            falha("claude", r.status_code)
        if r.status_code in (429, 529) or r.status_code >= 500:
            espera = int(r.headers.get("retry-after", "0") or 0) or _espera(t)
            print(f"  ia[{tarefa}]: API {r.status_code}, aguardando {espera:.0f}s"); time.sleep(espera); continue
        if r.status_code >= 300:
            erro = RuntimeError(f"API {r.status_code}: {r.text[:300]}")
            checar_erro(erro)
            raise erro
        j = r.json()
        registrar(tarefa, j.get("model") or mod, j.get("usage"))
        return _texto(j), j.get("stop_reason")
    raise RuntimeError("API indisponível após várias tentativas")


# ------------------------------------------------------------------ API em lote
def em_lote(tarefa, pedidos, rapido=False, max_tokens=4000, system=None, meta=None):
    """pedidos: [(custom_id, conteudo)]. Envia um lote e espera até ESPERA_LOTE.
    -> {custom_id: texto} se terminou a tempo; {} se ficou para a próxima rodada (guardado em ia_lotes)."""
    import supa
    if not pedidos:
        return {}
    global provedor_ultimo_lote
    provedor_ultimo_lote = "claude"
    if not anthropic_ok():
        provedor_ultimo_lote = "gemini"
        return _lote_gemini(tarefa, pedidos, max_tokens, system, "Claude indisponível")
    mod = modelo(rapido)
    reqs = []
    for cid, conteudo in pedidos:
        p = {"model": mod, "max_tokens": max_tokens, "messages": [{"role": "user", "content": conteudo}]}
        if system:
            p["system"] = system
        reqs.append({"custom_id": cid, "params": p})
    r = requests.post(f"{API}/messages/batches", timeout=120, headers=_h(), json={"requests": reqs})
    if r.status_code >= 300:
        erro = RuntimeError(f"lote: API {r.status_code}: {r.text[:300]}")
        if checar_erro(erro):
            provedor_ultimo_lote = "gemini"
            return _lote_gemini(tarefa, pedidos, max_tokens, system, "sem crédito na Anthropic")
        raise erro
    lote = r.json()["id"]
    supa.upsert("ia_lotes", [{"id": lote, "tarefa": tarefa, "modelo": mod, "status": "pendente", "meta": meta or {}}], on_conflict="id")
    print(f"  ia[{tarefa}]: lote {lote} enviado ({len(reqs)} pedidos, 50% mais barato)")
    fim = min(time.time() + ESPERA_LOTE, PRAZO_FINAL or float("inf"))  # não espera além do prazo da etapa: o lote fica para recolher
    while time.time() < fim:
        time.sleep(20)
        st = requests.get(f"{API}/messages/batches/{lote}", timeout=60, headers=_h()).json()
        if st.get("processing_status") == "ended":
            res = _resultados(tarefa, st, mod)
            supa.update("ia_lotes", {"id": f"eq.{lote}"}, {"status": "aplicado", "aplicado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            return res
    print(f"  ia[{tarefa}]: lote {lote} ainda processando; o resultado entra na próxima rodada")
    return {}


provedor_ultimo_lote = None  # "claude" ou "gemini": quem respondeu o último em_lote (o Gemini precisa de conferência)
ultimo_modelo_gemini = None  # modelo que respondeu a última chamada ao Gemini
modelo_por_pedido = {}       # custom_id -> modelo Gemini que respondeu (no em_lote sem Claude): o conferente não pode ser ele


def _lote_gemini(tarefa, pedidos, max_tokens, system, motivo):
    """Sem Claude: faz os pedidos um a um no Gemini (não há lote grátis), até o teto de chamadas da rodada."""
    if not gemini_disponivel(tarefa):
        return {}
    _avisar_reserva(tarefa, motivo)
    out = {}
    for cid, conteudo in pedidos:
        if sem_tempo(60):
            print(f"  gemini[{tarefa}]: prazo da etapa perto do fim; {len(pedidos) - len(out)} pedido(s) ficam para a próxima rodada"); break
        if not gemini_disponivel(tarefa):
            print(f"  gemini[{tarefa}]: teto de chamadas desta rodada atingido (tarefa {teto_gemini(tarefa)}, geral "
                  f"{_env_int('GEMINI_MAX_CHAMADAS', 40)}); {len(pedidos) - len(out)} pedido(s) ficam para amanhã"); break
        try:
            out[cid], _ = gemini(tarefa, conteudo, max_tokens, system)
            modelo_por_pedido[cid] = ultimo_modelo_gemini
        except RuntimeError as e:
            print(f"  gemini[{tarefa}]: {e}")
            if isinstance(e, GeminiIndisponivel):
                break
    return out


def _resultados(tarefa, st, mod):
    url = st.get("results_url")
    if not url:
        return {}
    r = requests.get(url, timeout=300, headers=_h())
    out = {}
    for linha in r.text.splitlines():
        if not linha.strip():
            continue
        j = json.loads(linha)
        res = j.get("result") or {}
        if res.get("type") == "succeeded":
            msg = res["message"]
            registrar(tarefa, msg.get("model") or mod, msg.get("usage"), em_lote=True)
            out[j["custom_id"]] = _texto(msg)
        else:
            print(f"  ia[{tarefa}]: pedido {j.get('custom_id')} sem resposta ({res.get('type')})")
    return out


def recolher(tarefa):
    """Lotes pendentes de rodadas anteriores que já terminaram. -> [(meta, {custom_id: texto})]"""
    import supa
    out = []
    for l in supa.select("ia_lotes", {"select": "id,modelo,meta,criado_em", "tarefa": f"eq.{tarefa}", "status": "eq.pendente"}):
        try:
            st = requests.get(f"{API}/messages/batches/{l['id']}", timeout=60, headers=_h()).json()
        except Exception as e:  # noqa: BLE001
            print(f"  ia[{tarefa}]: não consegui consultar o lote {l['id']}: {e}"); continue
        if st.get("processing_status") != "ended":
            continue
        res = _resultados(tarefa, st, l.get("modelo"))
        out.append((l.get("meta") or {}, res))
        supa.update("ia_lotes", {"id": f"eq.{l['id']}"}, {"status": "aplicado" if res else "expirado",
                                                       "aplicado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if out:
        print(f"  ia[{tarefa}]: {len(out)} lote(s) de rodadas anteriores recolhido(s)")
    return out


def ids_em_lote(tarefa):
    """Ids já enviados em lotes ainda pendentes (para não pagar duas vezes pela mesma questão)."""
    import supa
    ids = set()
    for l in supa.select("ia_lotes", {"select": "meta", "tarefa": f"eq.{tarefa}", "status": "eq.pendente"}):
        for v in (l.get("meta") or {}).values():
            ids.update(v if isinstance(v, list) else [])
    return ids


# ------------------------------------------------------------------ resumo de custo
def resumo(rodada=""):
    """Grava em ia_uso o uso acumulado desde a última gravação e, em robo_diagnostico, as falhas de IA por provedor/modelo e
    código (etapa 'ia_erros') e os pedidos por modelo do Gemini (etapa 'ia_modelos', para o "uso por modelo hoje"); escreve uma tabela no resumo do GitHub Actions. Chamado ao fim de CADA etapa (rodar.py): se o job for
    cortado no timeout, o das etapas anteriores já está salvo. Cada acumulado só é zerado depois de gravar com sucesso."""
    global _gasto_mes
    import supa
    if _falhas:
        det = {f"{p} {c}": n for (p, c), n in sorted(_falhas.items())}
        if _amostras:
            det["amostras"] = {f"{p} {c}": a for (p, c), a in _amostras.items()}
        print(f"falhas de IA [{rodada}]: " + ", ".join(f"{k}: {n}" for k, n in det.items()))
        try:
            supa.upsert("robo_diagnostico", [{"banca": "IA", "etapa": "ia_erros", "url": None, "status_http": None, "bytes": None,
                                              "titulo": rodada[:200], "itens": sum(n for (_, _), n in _falhas.items()), "detalhe": json.dumps(det, ensure_ascii=False)}])
        except Exception as e:  # noqa: BLE001
            print(f"ia_erros: não gravou ({e}); fica acumulado para a próxima gravação")
        else:
            _falhas.clear(); _amostras.clear()
    if _pedidos_modelo:
        ped = dict(sorted(_pedidos_modelo.items()))
        try:
            supa.upsert("robo_diagnostico", [{"banca": "IA", "etapa": "ia_modelos", "url": None, "status_http": None, "bytes": None,
                                              "titulo": rodada[:200], "itens": sum(ped.values()), "detalhe": json.dumps(ped, ensure_ascii=False)}])
        except Exception as e:  # noqa: BLE001
            print(f"ia_modelos: não gravou ({e}); fica acumulado para a próxima gravação")
        else:
            _pedidos_modelo.clear()
    if not _uso:
        print(f"custo de IA [{rodada}]: nenhuma chamada"); return
    linhas, total = [], 0.0
    for (tarefa, mod, lote), c in sorted(_uso.items()):
        total += c["custo_usd"]
        linhas.append({"rodada": rodada, "tarefa": tarefa, "modelo": mod, "em_lote": lote, **{k: (round(v, 4) if k == "custo_usd" else v) for k, v in c.items()}})
    try:
        supa.upsert("ia_uso", linhas)
    except Exception as e:  # noqa: BLE001
        print(f"ia_uso: não gravou ({e}); o custo fica acumulado para a próxima gravação")
    else:
        _uso.clear()
        if _gasto_mes is not None:
            _gasto_mes += total  # o teto mensal continua vendo o que já foi gravado nesta rodada
    md = [f"### Custo de IA – {rodada} (estimado)", "", "| Tarefa | Modelo | Lote | Chamadas | Entrada | Saída | Cache lido | US$ |", "|---|---|---|---|---|---|---|---|"]
    for l in linhas:
        md.append(f"| {l['tarefa']} | {l['modelo']} | {'sim' if l['em_lote'] else 'não'} | {l['chamadas']} | {l['entrada']:,} | {l['saida']:,} | {l['cache_leitura']:,} | {l['custo_usd']:.2f} |")
    md.append(f"| **Total** | | | | | | | **{total:.2f}** |")
    texto = "\n".join(md)
    print(texto)
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as f:
            f.write(texto + "\n")
