"""Extrai as questões de um caderno de prova (PDF, inclusive escaneado) com a IA lendo o documento.

Usado quando o caderno não tem texto (ex.: CETAP publica cadernos digitalizados) e nas bancas que só a IA lê (Cebraspe, FGV).
Provedor: Claude (ANTHROPIC_API_KEY/ANTHROPIC_MODEL); sem ele, a reserva gratuita Gemini (GEMINI_API_KEY/GEMINI_MODEL) — ver ia.py.
A IA só TRANSCREVE: o gabarito vem sempre do PDF oficial de gabarito, nunca da IA.
Transcrição do Gemini só vale se trouxer >= COBERTURA_MIN_GEMINI das questões com alternativas completas; senão o caderno
volta para aguardando_ia e espera o Claude.
"""
import base64, json, os, re, time
import requests
import ia


class SemIA(RuntimeError):
    """IA desligada ou sem crédito: o caderno fica guardado (status aguardando_ia) para transcrever depois."""


def _motivo_reserva(e):
    """Sem modelo Gemini (sobrecarga, cota diária de todos os modelos, teto...): a frase exata da causa, ex.:
    "Gemini sobrecarregado (503): tenta na próxima rodada". Outro erro do pedido: "reserva Gemini falhou: <erro>"."""
    return str(e) if isinstance(e, ia.GeminiIndisponivel) else f"reserva Gemini falhou: {e}"


def _falha(msg):
    erro = RuntimeError(msg)
    if ia.checar_erro(erro):
        raise SemIA("sem crédito de IA")
    raise erro

PROMPT = """Este PDF é o caderno oficial de uma prova de concurso público (banca {banca}). Transcreva TODAS as questões {faixa}.
Regras:
- Transcrição FIEL, palavra por palavra (corrija só hifenização de fim de linha). Não resuma, não corrija a banca, não responda.
- Textos de apoio usados por várias questões vão uma vez em "textos" (id "T1", "T2"...); a questão aponta com "texto": "T1".
- "disciplina": o título da seção em que a questão está (ex.: "Língua Portuguesa", "Legislação e Ética no Serviço Público").
- Se a questão depende de imagem, gráfico, tabela ou figura, descreva-a entre colchetes no enunciado, ex.: "[Tabela: ...]", e marque "figura": true.
- Itens romanos (I, II, III) ficam no enunciado, um por linha.
- IGNORE carimbos e marcas d'água de terceiros que não são da banca (ex.: "www.tecconcursos.com.br", "TEC Concursos", número
  ou nome de usuário, "Licenciado para ...", data de download): nunca os transcreva em lugar nenhum.
{regra_formato}Responda APENAS com JSON neste formato, sem comentários:
{{"cargo": "<nome do cargo na capa>", "textos": [{{"id": "T1", "texto": "..."}}],
 "questoes": [{{"numero": 1, "disciplina": "...", "texto": null, "enunciado": "...", "alternativas": {{"A": "...", "B": "...", "C": "...", "D": "...", "E": "..."}}, "figura": false}}]}}"""


COBERTURA_MIN_GEMINI = 0.9

REGRA_CE = """- PROVA DE CERTO/ERRADO: cada item numerado é uma questão. O texto de apoio E o comando que vem antes dos itens (ex.: "Considerando o texto, julgue os itens a seguir.", ou uma situação hipotética) vão juntos em "textos", e cada item aponta para ele com "texto". "enunciado" = só a afirmação do item. Use "alternativas": {"C": "Certo", "E": "Errado"}.
"""


def extrair_por_ia(pdf_bytes, n_esperado, banca="CETAP", max_tokens=32000, numeros=None, formato="me"):
    """numeros: lista dos números das questões do caderno (ex.: 21..60 no Cebraspe); formato 'ce' = itens Certo/Errado."""
    if not ia.disponivel("transcrever"):
        raise SemIA(ia.motivo_indisponivel("transcrever"))
    blocos = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}},
              {"type": "text", "text": PROMPT.format(
                  banca=banca, faixa=(f"numeradas de {min(numeros)} a {max(numeros)}" if numeros else f"de 1 a {n_esperado}"),
                  regra_formato=REGRA_CE if formato == "ce" else "")}]
    txt = parada = None
    via_gemini = False
    if ia.anthropic_ok():
        try:
            txt, parada = _transcrever_claude(blocos, max_tokens, banca)
        except (SemIA, RuntimeError, requests.ConnectionError, requests.Timeout) as e:
            # sem crédito, rede ou API fora do ar depois das tentativas: a reserva tenta; erro de conteúdo (400) não troca de provedor
            passageiro = isinstance(e, (SemIA, requests.ConnectionError, requests.Timeout)) or "várias tentativas" in str(e)
            if not ia.gemini_disponivel("transcrever") or not passageiro:
                raise
    if txt is None:
        if not ia.gemini_disponivel("transcrever"):
            raise SemIA(ia.motivo_indisponivel("transcrever"))
        ia._avisar_reserva("transcrever", "Claude indisponível")
        try:
            txt, parada = ia.gemini(f"transcrever caderno ({banca})", blocos, max_tokens)
        except RuntimeError as e:
            raise SemIA(_motivo_reserva(e)) from e
        via_gemini = True
    cargo, out = _ler_transcricao(txt, parada, formato)
    if via_gemini:
        completas = sum(1 for q in out if formato == "ce" or len(q["alternativas"]) >= 4)
        if completas < COBERTURA_MIN_GEMINI * n_esperado:
            raise SemIA(f"transcrição do Gemini incompleta ({completas} de {n_esperado} questões): fica para o Claude")
    return cargo, out, parada


def _transcrever_claude(blocos, max_tokens, banca):
    """Claude com resposta em fluxo (caderno longo). -> (texto, stop_reason)"""
    chave, modelo = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("ANTHROPIC_MODEL")
    corpo = {"model": modelo, "max_tokens": max_tokens, "stream": True, "messages": [{"role": "user", "content": blocos}]}
    for tentativa in range(5):
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=900, stream=True, json=corpo,
                          headers={"x-api-key": chave, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        if r.status_code in (429, 529) or r.status_code >= 500:
            espera = int(r.headers.get("retry-after", "0") or 0) or 30 * (tentativa + 1)
            print(f"    ia_extrair: API {r.status_code}, aguardando {espera}s"); time.sleep(espera); continue
        if r.status_code >= 300:
            _falha(f"API {r.status_code}: {r.text[:300]}")
        partes, parada, uso, mod_resp, erro = [], None, {}, modelo, None
        for linha in r.iter_lines(decode_unicode=True):
            if not linha or not linha.startswith("data:"):
                continue
            ev = json.loads(linha[5:])
            if ev.get("type") == "message_start":
                uso.update(ev["message"].get("usage") or {}); mod_resp = ev["message"].get("model") or modelo
            elif ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                partes.append(ev["delta"]["text"])
            elif ev.get("type") == "message_delta":
                parada = ev["delta"].get("stop_reason")
                uso["output_tokens"] = (ev.get("usage") or {}).get("output_tokens", uso.get("output_tokens", 0))
            elif ev.get("type") == "error":
                erro = ev.get("error") or {}; break
        ia.registrar(f"transcrever caderno ({banca})", mod_resp, uso)
        if erro:
            # sobrecarga no meio da resposta: espera e tenta de novo (antes isso derrubava o caderno inteiro)
            if erro.get("type") in ("overloaded_error", "rate_limit_error", "api_error") and tentativa < 4:
                espera = 60 * (tentativa + 1)
                print(f"    ia_extrair: {erro.get('type')} no meio da resposta, aguardando {espera}s"); time.sleep(espera); continue
            _falha(f"API: {erro}")
        return "".join(partes), parada
    raise RuntimeError("API indisponível após várias tentativas")


# Carimbo de site de terceiro em PDF original da banca baixado à mão pelo dono (exceção TEC, CLAUDE.md item 10): a linha
# que o contém sai do texto, venha a questão da IA ou do texto do PDF.
RX_CARIMBO = re.compile(r"tec\s*concursos", re.I)


def _sem_carimbo(texto):
    if not texto or not RX_CARIMBO.search(texto):
        return texto, 0
    linhas = texto.split("\n")
    fica = [ln for ln in linhas if not RX_CARIMBO.search(ln)]
    return "\n".join(fica).strip(), len(linhas) - len(fica)


def limpar_carimbos(qs):
    """Tira do enunciado, das alternativas e do texto de apoio toda linha com "tecconcursos". -> nº de linhas removidas."""
    total = 0
    for q in qs:
        for campo in ("enunciado", "texto_base"):
            q[campo], n = _sem_carimbo(q.get(campo))
            total += n
        alts = q.get("alternativas") or {}
        for k in list(alts):
            alts[k], n = _sem_carimbo(alts[k])
            total += n
    return total


def _ler_transcricao(txt, parada, formato):
    """JSON da IA -> (cargo, questões). Igual para Claude e Gemini."""
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        raise RuntimeError(f"resposta sem JSON (stop={parada})")
    dados = json.loads(m.group(0))
    textos = {t["id"]: t["texto"] for t in dados.get("textos") or []}
    out = []
    for q in dados.get("questoes") or []:
        alts = ({"C": "Certo", "E": "Errado"} if formato == "ce" else
                {k.upper(): " ".join(str(v).split()) for k, v in (q.get("alternativas") or {}).items() if str(v).strip()})
        if not q.get("enunciado") or len(alts) < 2:
            continue
        out.append({"numero": int(q["numero"]), "disciplina": q.get("disciplina"), "texto_base": textos.get(q.get("texto") or ""),
                    "enunciado": q["enunciado"].strip(), "alternativas": alts, "figura": bool(q.get("figura")),
                    "aviso": None if formato == "ce" or len(alts) >= 4 else "alternativas incompletas: conferir com o PDF"})
    return dados.get("cargo"), out


def gabarito_por_ia(pdf_bytes, texto, cargo_cod, cargo_nome, tipo):
    """Lê o gabarito oficial (texto extraído ou, se vazio, o próprio PDF) e devolve {numero: letra | None} do cargo/tipo pedido."""
    chave, modelo = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("ANTHROPIC_MODEL")
    if not ia.disponivel("gabarito"):
        raise SemIA(ia.motivo_indisponivel("gabarito"))
    pedido = (f"Este é o GABARITO OFICIAL de um concurso. Extraia as respostas do cargo {cargo_cod or ''} \"{cargo_nome}\""
              f", caderno tipo/prova {tipo or '1'} (se só houver um tipo, use-o). Questões anuladas (X, *, NULA) valem null. "
              "Copie exatamente o que está no documento, sem deduzir nada. Responda APENAS com JSON: "
              "{\"cargo_encontrado\": \"<nome como aparece>\", \"respostas\": {\"1\": \"A\", \"2\": null}}")
    conteudo = ([{"type": "text", "text": f"TEXTO DO GABARITO:\n{texto[:60000]}"}] if texto and len(texto.strip()) > 200 else
                [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}}])
    blocos = conteudo + [{"type": "text", "text": pedido}]
    txt = None
    if ia.anthropic_ok():
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=300,
                              headers={"x-api-key": chave, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                              json={"model": modelo, "max_tokens": 4000, "messages": [{"role": "user", "content": blocos}]})
            if r.status_code >= 300:
                _falha(f"API {r.status_code}: {r.text[:300]}")
            ia.registrar("ler gabarito", r.json().get("model") or modelo, r.json().get("usage"))
            txt = "".join(b.get("text", "") for b in r.json().get("content", []))
        except (SemIA, requests.ConnectionError, requests.Timeout):
            if not ia.gemini_disponivel("gabarito"):
                raise
    if txt is None:
        if not ia.gemini_disponivel("gabarito"):
            raise SemIA(ia.motivo_indisponivel("gabarito"))
        ia._avisar_reserva("gabarito", "Claude indisponível")
        try:
            txt, _ = ia.gemini("gabarito", blocos, 4000)
        except RuntimeError as e:
            raise SemIA(_motivo_reserva(e)) from e
    m = re.search(r"\{.*\}", txt, re.S)
    dados = json.loads(m.group(0)) if m else {}
    resp = {int(k): (v.upper() if isinstance(v, str) and v.upper() in ("A", "B", "C", "D", "E") else None) for k, v in (dados.get("respostas") or {}).items()}
    return dados.get("cargo_encontrado"), resp
