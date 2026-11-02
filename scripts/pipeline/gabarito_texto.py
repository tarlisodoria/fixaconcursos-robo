"""Leitura determinística de gabarito oficial em texto (sem IA): a mesma do organizador local (scripts/organizar_provas.py,
que importa daqui) e a que o robô usa para conferir o _gabarito.json do organizador contra o PDF enviado."""
import re

# "001 - B" (folha de gabarito) ou "31 A Gabarito sem alteração" (página do Portal do Candidato impressa)
# 2º formato (tabela do portal): "3 C Gabarito sem alteração". Por OCR a letra vem como "Cc"/"cC" -> aceita 1-2 letras iguais.
RX_RESPOSTA = re.compile(r"\b(\d{1,3})\s*[-–.:]\s*([A-E*X#])\b|(?:^|\s)(\d{1,3}) ([A-Ea-e*X#]{1,2}) ?(?=Gabarito|Anulad|Alterad|Quest)", re.M)


def respostas(t):
    """{número da questão: letra} ('*'/'X'/'#' = anulada). Descarta leituras incoerentes do OCR (ex.: 'AB')."""
    out = {}
    for m in RX_RESPOSTA.finditer(t or ""):
        num, tok = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        tok = tok.upper()
        if len(set(tok)) == 1:
            out.setdefault(int(num), tok[0])
    return out


def faltando(resp):
    """Números ausentes entre 1 e a maior questão lida."""
    return [i for i in range(1, max(resp, default=0) + 1) if i not in resp]


# código no gabarito: "Cargo ou opção M13 - ...", "Cargo: U21 - ...", "Cargo: Q - ...", "U14 - tipo 1"
RX_COD_GABARITO = re.compile(r"Cargo(?: ou op[çc][ãa]o)?\s*:?\s*([A-Z0-9][0-9]{0,2})\s*[-–]\s*\S|\b([A-Z]\d{2})\s*-\s*tipo\s*\d", re.I)
RX_TIPO = re.compile(r"\b(?:Tipo(?: de)?(?: gabarito)?|TIPO-)\W{0,3}0*(\d{1,3})\b", re.I)


def blocos_gabarito(t):
    """{(codigo, tipo): {questão: letra}} lendo cada bloco 'Cargo ou opção M13 ... Tipo gabarito 1 ... 001 - B'."""
    out = {}
    blocos = list(RX_COD_GABARITO.finditer(t))
    for i, m in enumerate(blocos):
        fim = blocos[i + 1].start() if i + 1 < len(blocos) else len(t)
        trecho = t[m.start():fim]
        r = respostas(trecho)
        if len(r) >= 10:
            tp = RX_TIPO.search(trecho)
            out.setdefault(((m.group(1) or m.group(2)).upper(), int(tp.group(1)) if tp else 1), {}).update(r)
    if len({c for c, _ in out}) == 1 and len(out) == 1:  # um cargo só (ex.: página do portal): vale o documento inteiro
        out[next(iter(out))] = respostas(t)
    return out


def respostas_do_cargo(texto, cod=None, tipo=None):
    """{questão: letra} do cargo/tipo no texto do gabarito ({} se não achar). Mesma escolha do organizador: código e tipo,
    depois só o código, depois o único bloco do documento."""
    blocos = blocos_gabarito(texto or "")
    cod = (cod or "").strip().upper()
    tipo = int(tipo or 1) if str(tipo or "1").strip().isdigit() else 1
    return (blocos.get((cod, tipo)) or next((r for (cd, _), r in blocos.items() if cod and cd == cod), None)
            or (next(iter(blocos.values())) if len(blocos) == 1 else {}))
