"""Estrutura o texto de apoio (Língua Portuguesa) em parágrafos, como na prova.

O caderno vem com uma quebra de linha por linha impressa, então "qual é o 5º parágrafo?" fica impossível de responder.
Formato gravado em textos.texto (o app numera os parágrafos do corpo):

    # Título ou intertítulo          (não numera)
    > Linha fina, subtítulo, autoria (não numera)
    Parágrafo do corpo                (numera: 1, 2, 3…)
    @ Fonte / referência              (não numera)

Blocos separados por linha em branco. Duas formas de chegar nisso:
  1. estruturar_por_geometria(linhas): a partir das linhas do PDF (recuo da 1ª linha, fim de linha curto, fonte).
  2. estruturar_por_linhas(texto): só com o texto (comprimento das linhas) — reserva para textos já gravados.
Nenhuma das duas altera palavras: `mesmas_palavras` confere isso antes de gravar.
"""
import re
import unicodedata

FIM_FRASE = re.compile(r"""[.!?…:;]["'”’)\]»]*\s*$|\[\s*…\s*\]\s*$|\[\s*\.\.\.\s*\]\s*$""")
FONTE = re.compile(r"^(Adaptado de|Texto adaptado|Dispon[ií]vel em|Fonte:|Acesso em|Extra[ií]do de|In:|https?://)", re.I)


MARCADO = re.compile(r"^(#|>|@|\|)\s")


def esta_estruturado(t):
    """Mesmo critério do app (components/TextoApoio.tsx)."""
    return "\n\n" in t and any(MARCADO.match(b) for b in re.split(r"\n{2,}", t))


def limpar_espacos(t):
    """Tira espaços que o PDF põe antes de pontuação e por dentro de aspas ("vem ” ," -> "vem”,"). Não mexe em letras."""
    t = re.sub(r"([“‘(\[])\s+", r"\1", t)
    t = re.sub(r"\s+([”’)\],.;:!?])", r"\1", t)
    # Dall ’ Igna -> Dall’Igna; mas ’ que fecha uma aspa aberta (‘ü’ do) é aspa, não apóstrofo: o espaço depois fica
    def apostrofo(m):
        antes = t[:m.start()]
        return m.group(0) if antes.count("‘") > antes.count("’") else f"{m.group(1)}’{m.group(2)}"
    t = re.sub(r"(\w)\s*’\s*(\w)", apostrofo, t)
    return t


ACENTOS = "áéíóúãõâêôàçÁÉÍÓÚÃÕÂÊÔÀÇ"
RX_PALAVRA = re.compile(r"[A-Za-zÀ-ÿ]+")
RX_PARTIDA3 = re.compile(r"(?<![A-Za-zÀ-ÿ])([A-Za-zÀ-ÿ]{1,14}) ([áéíóúãõâêôàç]) ([A-Za-zÀ-ÿ]{1,14})(?![A-Za-zÀ-ÿ])")
L = "A-Za-zÀ-ÿ"
AC = "áéíóúãõâêôàç"
# par em que o espaço encosta numa letra acentuada (só esses podem ser palavra partida pelo PDF); duas alternativas
# para o par não "comer" a palavra seguinte: "Era f ácil" testa "f ácil", não "Era f"
RX_PARTIDA = re.compile(rf"(?<![{L}])([{L}]{{1,14}}) ([{AC}][{L}]{{0,13}})(?![{L}])|(?<![{L}])([{L}]{{0,13}}[{AC}]) ([{L}]{{1,14}})(?![{L}])")


def vocabulario(textos_iter):
    """Frequência de cada palavra (minúscula) no banco inteiro: é o "dicionário" para desfazer palavra partida."""
    from collections import Counter
    c = Counter()
    for t in textos_iter:
        c.update(w.lower() for w in RX_PALAVRA.findall(t or ""))
    return c


def juntar_partidas(t, vocab):
    """O PDF às vezes parte a palavra em volta da letra acentuada: "f ácil", "nó s", "usá- lo".
    Junta só quando a palavra inteira é conhecida no banco (≥ 3 vezes) e os pedaços não são palavras comuns sozinhos,
    para nunca colar "e à", "é um", "relação à"."""
    if not t:
        return t
    t = re.sub(r"([A-Za-zÀ-ÿ]-) (lo|la|los|las|lhe|lhes|se|me|te|nos|vos|o|a|os|as)\b", r"\1\2", t)  # usá- lo -> usá-lo

    def troca(m):
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        junto = (a + b).lower()
        comum = lambda w: vocab.get(w.lower(), 0) >= 20 and len(w) > 1
        if vocab.get(junto, 0) >= 3 and not (comum(a) and comum(b)):
            return a + b
        return m.group(0)
    def troca3(m):  # "p ú blico": letra acentuada sozinha no meio
        junto = (m.group(1) + m.group(2) + m.group(3)).lower()
        return m.group(1) + m.group(2) + m.group(3) if vocab.get(junto, 0) >= 3 else m.group(0)
    t = RX_PARTIDA3.sub(troca3, t)
    anterior = None
    while anterior != t:
        anterior, t = t, RX_PARTIDA.sub(troca, t)
    return t


def limpar_tudo(t, vocab):
    """Para enunciado e alternativas. Protege os marcadores de item "( )" e "[ ]" (V/F), que a limpeza de espaço apagaria."""
    if not t:
        return t
    t = re.sub(r"\(\s+\)", "\x00P\x00", re.sub(r"\[\s+\]", "\x00C\x00", t))
    t = limpar_espacos(juntar_partidas(t, vocab))
    return t.replace("\x00P\x00", "( )").replace("\x00C\x00", "[ ]")


def _juntar(linhas):
    out = ""
    for ln in linhas:
        ln = ln.strip()
        if not out:
            out = ln
        elif out.endswith("-") and len(out) > 1 and (out[-2].isalpha() and ln[:1].islower() or "://" in out.rsplit(" ", 1)[-1]):
            out += ln  # palavra composta ou link quebrados no hífen (ex.: "não-" + "governamental")
        else:
            out += " " + ln
    return limpar_espacos(re.sub(r"\s+", " ", out).strip())


def estruturar_por_linhas(texto):
    """Heurística só com o texto: fim de parágrafo = linha que termina frase e é bem mais curta que a coluna."""
    if "\n\n" in texto.strip():
        return texto  # já estruturado
    linhas = [l.rstrip() for l in texto.split("\n") if l.strip()]
    if not linhas:
        return texto
    tams = sorted(len(l) for l in linhas)
    largura = tams[int(len(tams) * 0.85)] if len(tams) > 4 else max(tams)
    blocos, atual, tipo = [], [], None

    def fecha():
        nonlocal atual, tipo
        if atual:
            blocos.append((tipo or "", _juntar(atual)))
        atual, tipo = [], None

    for i, l in enumerate(linhas):
        s = l.strip()
        if FONTE.match(s):
            fecha(); tipo = "@"; atual = [s]
            continue
        if tipo == "@":
            atual.append(s); continue
        curta = len(s) < 0.78 * largura
        prox = linhas[i + 1].strip() if i + 1 < len(linhas) else ""
        if not atual and curta and not FIM_FRASE.search(s) and (not prox or prox[:1].isupper() or prox[:1] in "“\""):
            # linha curta e isolada sem pontuação final: título, intertítulo ou autoria
            blocos.append(("#" if not blocos or blocos[-1][0] in ("#", "") else "#", s))
            continue
        atual.append(s)
        if curta and FIM_FRASE.search(s):
            fecha()
    fecha()
    return montar(blocos)


FONTE_EM_QQ_LUGAR = re.compile(r"(Adaptado de|Texto adaptado|Dispon[ií]vel em|Acesso em|Extra[ií]do de|Fonte:|https?://|www\.)", re.I)


def estruturar_paragrafado(texto):
    """Texto que já veio em parágrafos (transcrito pela IA: blocos separados por linha em branco), só sem marcação.
    Sem IA e sem custo: 1ª linha curta = título (#), linha logo abaixo dela = linha fina (>), último bloco com
    "Disponível em/Adaptado de/http" = fonte (@). Poema, letra de música e tabela ficam como estão (None).
    -> texto marcado, ou None se não há o que marcar com segurança."""
    t = texto.strip()
    if "\n\n" not in t or esta_estruturado(t):
        return None
    blocos = [b.strip() for b in re.split(r"\n{2,}", t) if b.strip()]
    if len(blocos) < 2:
        return None
    for b in blocos:  # verso ou tabela: muitas linhas curtas no mesmo bloco
        ls = [l for l in b.split("\n") if l.strip()]
        if len(ls) >= 3 and sum(len(l) for l in ls) / len(ls) < 70 or "|" in b:
            return None
    saida = []
    primeiro = [l.strip() for l in blocos[0].split("\n") if l.strip()]
    resto = blocos[1:]
    tit = primeiro[0]
    eh_titulo = len(tit) <= 140 and len(blocos) >= 3 and not re.match(r"^[\"“'(]", tit) and not FONTE_EM_QQ_LUGAR.search(tit)
    if eh_titulo:
        saida.append(("#", tit))
        if len(primeiro) > 1:
            fina = " ".join(primeiro[1:])
            saida.append((">" if len(fina) <= 320 else "", fina))
    else:
        saida.append(("", _juntar(primeiro)))
    fonte = []
    def eh_fonte(b):
        b1 = " ".join(b.split())
        if FONTE_EM_QQ_LUGAR.match(b1) or re.match(r"^\(?\s*fonte\b", b1, re.I):
            return True
        if len(b1) > 260:
            return False
        if FONTE_EM_QQ_LUGAR.search(b1):
            return True
        # "(MORENO, Cláudio. O Prazer das Palavras. p. 74)", "(Super Interessante, Março de 2024)", "(Autor desconhecido)"
        if b1.startswith("(") and b1.endswith(")"):
            return True
        # assinatura com data: "Joselene L. Alvim-psicóloga. 10/04/2023"
        return len(b1) <= 120 and bool(re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b(19|20)\d{2}\b", b1))
    while resto and (len(resto) > 1 or not eh_titulo) and eh_fonte(resto[-1]):  # sobra ao menos 1 parágrafo de corpo
        fonte.insert(0, resto.pop())
    for b in resto:
        saida.append(("", _juntar(b.split("\n"))))
    for f in fonte:
        saida.append(("@", _juntar(f.split("\n"))))
    if not any(tp for tp, _ in saida):
        return None
    novo = montar(saida)
    return novo if mesmas_palavras(texto, novo) else None


def montar(blocos):
    partes = []
    for tipo, t in blocos:
        partes.append(f"{tipo} {t}" if tipo else t)
    return "\n\n".join(partes)


def _palavras(t):
    t = re.sub(r"(?m)^(#|>|@)\s+", "", t)
    t = unicodedata.normalize("NFC", t).replace("-\n", "-")
    return re.findall(r"\w+", t.lower())


def mesmas_palavras(original, estruturado):
    """True se o estruturado tem exatamente as mesmas letras, na mesma ordem, que o original
    (permite só mexer em espaços e quebras: juntar "hipó tese" -> "hipótese", separar parágrafos)."""
    return "".join(_palavras(original)) == "".join(_palavras(estruturado))


OMISSAO = re.compile(r"^\[\s*(…|\.\.\.)\s*\]$")
TEXTO_N = re.compile(r"^#\s+Texto\s+\d+\s*$", re.I)


def paragrafos_do_corpo(estruturado, texto_n=1):
    """Parágrafos numeráveis (o corpo), do Texto N quando há 'Texto 1', 'Texto 2'..."""
    grupos, atual = [], []
    for b in estruturado.split("\n\n"):
        if TEXTO_N.match(b):
            if atual or grupos:
                grupos.append(atual)
            atual = []
            continue
        if b and not re.match(r"^(#|>|@|\|)\s", b) and not OMISSAO.match(b.strip()):
            atual.append(b)
    grupos.append(atual)
    grupos = [g for g in grupos if g] or [[]]
    return grupos[min(texto_n, len(grupos)) - 1]


def aplicar_spec(texto, spec):
    """spec = {índice da linha onde começa o bloco: papel ('', '#', '>', '@', '|')}. Só reagrupa linhas, nunca muda palavras."""
    linhas = texto.split("\n")
    inicios = sorted(int(k) for k in spec)
    if not inicios or inicios[0] != 0:
        inicios = [0] + inicios
        spec = {0: "", **{int(k): v for k, v in spec.items()}}
    spec = {int(k): v for k, v in spec.items()}
    blocos = []
    for k, ini in enumerate(inicios):
        fim = inicios[k + 1] if k + 1 < len(inicios) else len(linhas)
        trecho = [l for l in linhas[ini:fim] if l.strip()]
        if not trecho:
            continue
        papel = spec.get(ini, "")
        papel = papel if papel in ("", "#", ">", "@", "|") else ""
        blocos.append((papel, "\n".join(l.strip() for l in trecho) if papel == "|" else _juntar(trecho)))
    return montar(blocos)


ORDINAIS = {"primeiro": 1, "segundo": 2, "terceiro": 3, "quarto": 4, "quinto": 5, "sexto": 6, "sétimo": 7, "setimo": 7, "oitavo": 8,
            "nono": 9, "décimo": 10, "decimo": 10, "décimo primeiro": 11, "décimo segundo": 12, "décimo terceiro": 13,
            "décimo quarto": 14, "décimo quinto": 15}
RX_REF = re.compile(r"(d[ée]cimo\s+\w+|primeiro|segundo|terceiro|quarto|quinto|sexto|s[ée]timo|oitavo|nono|d[ée]cimo|[úu]ltimo|pen[úu]ltimo)\s+par[áa]grafo", re.I)
RX_ASPAS = re.compile(r"[“\"]([^”\"]{3,})[”\"]")


def conferir_referencias(estruturado, questoes):
    """Usa as próprias questões como gabarito da estrutura: “X”, no quinto parágrafo -> X tem de estar no 5º parágrafo.
    questoes: [{'enunciado', 'alternativas'}]. Devolve (conferidas, falhas[lista de strings])."""
    pars = paragrafos_do_corpo(estruturado)
    norm = lambda t: "".join(_palavras(t))  # só letras: tolera "hipó tese" (PDF) x "hipótese" (questão)
    pars_n = [norm(p) for p in pars]
    conferidas, falhas = 0, []
    for q in questoes:
        trechos = [q.get("enunciado") or ""] + list((q.get("alternativas") or {}).values())
        trechos = [t for tr in trechos for t in re.split(r"\(\s*\)", tr)]
        for t in trechos:
            for m in RX_REF.finditer(t):
                o = m.group(1).lower()
                n = len(pars) if "último" in o or "ultimo" in o else len(pars) - 1 if "penúltimo" in o or "penultimo" in o else ORDINAIS.get(re.sub(r"\s+", " ", o).replace("decimo", "décimo"))
                if not n:
                    continue
                citacoes = [c for c in (norm(x) for x in RX_ASPAS.findall(t[:m.start()] + t[m.end():])) if len(c) >= 4]
                if not citacoes:
                    continue
                conferidas += 1
                if n > len(pars) or not any(c and c in pars_n[n - 1] for c in citacoes):
                    falhas.append(f"{m.group(0)}: {citacoes[0][:50]!r} não está no parágrafo {n} (o texto tem {len(pars)})")
    return conferidas, falhas


PROMPT_IA = """Abaixo está um texto de apoio de prova de concurso, extraído do PDF com UMA LINHA POR LINHA IMPRESSA (numeradas).
Diga onde começa cada bloco, como na prova original:
- "#": título ou intertítulo (ou o rótulo "Texto 1", "Texto 2")
- ">": linha fina/subtítulo logo abaixo do título, ou autoria ("Por Fulano", "Fulano – jornalista")
- "": parágrafo do corpo do texto (são estes que a banca conta como 1º, 2º, 3º parágrafo...)
- "@": referência/fonte ("Adaptado de:", "Disponível em:", "Acesso em:")
- "|": tabela, gráfico ou infográfico transcrito (mantém as linhas)
Pistas: no corpo, um parágrafo termina numa linha que fecha a frase e costuma ser mais curta que as outras; "[...]" sozinho é um bloco "".
{questoes}
Responda APENAS com JSON: {{"blocos": [{{"linha": 0, "papel": "#"}}, {{"linha": 3, "papel": ""}}, ...]}}

TEXTO:
{linhas}"""


def estruturar_por_ia(texto, questoes, chamar):
    """chamar(prompt) -> resposta em texto. A IA só aponta índices de linha; as palavras nunca passam por ela.
    Devolve (texto_estruturado | None, relatório)."""
    import json as _json
    linhas = texto.split("\n")
    refs = [q for q in questoes if RX_REF.search((q.get("enunciado") or "") + " " + " ".join((q.get("alternativas") or {}).values()))]
    bloco_q = ""
    if refs:
        bloco_q = "As questões desta prova citam parágrafos assim (use para conferir a contagem):\n" + "\n".join(
            "- " + " | ".join([q.get("enunciado") or ""] + [v for v in (q.get("alternativas") or {}).values() if RX_REF.search(v)])[:600] for q in refs[:8])
    numeradas = "\n".join(f"{i}| {l}" for i, l in enumerate(linhas))
    ultimo = None
    for tentativa in range(2):
        prompt = PROMPT_IA.format(questoes=bloco_q, linhas=numeradas)
        if ultimo:
            prompt += f"\n\nNa tentativa anterior a contagem não bateu com as questões: {ultimo}. Corrija."
        resp = chamar(prompt)
        m = re.search(r"\{.*\}", resp or "", re.S)
        try:
            blocos = _json.loads(m.group(0))["blocos"] if m else []
        except (ValueError, KeyError):
            blocos = []
        spec = {}
        for b in blocos:
            try:
                i = int(b.get("linha"))
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(linhas):
                spec[i] = b.get("papel") or ""
        if not spec:
            ultimo = "resposta sem blocos"; continue
        novo = aplicar_spec(texto, spec)
        if not mesmas_palavras(texto, novo):
            ultimo = "palavras diferentes"; continue
        n, falhas = conferir_referencias(novo, questoes)
        if not falhas:
            return novo, f"ok ({len(paragrafos_do_corpo(novo))} parágrafos; {n} referência(s) das questões conferida(s))"
        ultimo = "; ".join(falhas[:3])
    return None, f"não estruturado: {ultimo}"
