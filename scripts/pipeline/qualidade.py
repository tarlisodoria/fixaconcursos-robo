"""Auditoria contra o chute para questões geradas por IA (ideia do projeto ENADE-ADS-2026, MIT).

Uma questão que se resolve sem saber a matéria não treina ninguém. Medimos três vícios e equilibramos a posição da correta.
"""
import random, re

MARCADORES_ABSURDO = re.compile(
    r"\b(nunca|jamais|em nenhuma hip[óo]tese|garante (?:a |o )?(?:total|ausência|eliminação)|elimina (?:totalmente|completamente)|"
    r"100\s?%|absolutamente|de forma alguma|sem qualquer exce[çc][ãa]o|imposs[íi]vel)\b", re.I)
RX_LETRA = re.compile(r"\(([A-E])\)")


def problemas(q):
    """Lista de motivos para descartar a questão (vazia = aprovada na régua)."""
    alts, g = q["alternativas"], q["gabarito"]
    out = []
    # 1) chute pelo tamanho: a correta é a visivelmente mais longa (margem de 15 caracteres sobre a 2ª)
    tam = sorted(((len(v), k) for k, v in alts.items()), reverse=True)
    if tam[0][1] == g and tam[0][0] - tam[1][0] >= 15:
        out.append("correta é a visivelmente mais longa")
    # 2) eliminação por absurdo: 3+ distratores com marcador de absurdo
    absurdos = sum(1 for k, v in alts.items() if k != g and MARCADORES_ABSURDO.search(v))
    if absurdos >= 3:
        out.append(f"{absurdos} distratores eliminam-se por absurdo")
    # 3) explicação que justifica sem refutar: precisa discutir ao menos 2 distratores
    exp = q.get("explicacao") or ""
    citados = {l for l in RX_LETRA.findall(exp) if l != g}
    if len(citados) < 2:
        out.append("explicação não refuta ao menos 2 distratores")
    if not q.get("fonte"):
        out.append("sem fonte normativa")
    return out


def embaralhar(q, rnd=random):
    """Sorteia a posição da correta (A–E uniforme) e renomeia as letras citadas na explicação."""
    letras = list("ABCDE")
    origem = list(q["alternativas"].keys())
    nova_pos = rnd.choice(letras)
    distratores = [l for l in origem if l != q["gabarito"]]
    rnd.shuffle(distratores)
    destino = [l for l in letras if l != nova_pos]
    mapa = {q["gabarito"]: nova_pos, **dict(zip(distratores, destino))}
    alts = {mapa[k]: v for k, v in q["alternativas"].items()}
    exp = RX_LETRA.sub(lambda m: f"({mapa.get(m.group(1), m.group(1))})", q.get("explicacao") or "")
    exp = re.sub(r"(Gabarito:\s*)([A-E])\b", lambda m: m.group(1) + mapa.get(m.group(2), m.group(2)), exp)
    return {**q, "alternativas": dict(sorted(alts.items())), "gabarito": nova_pos, "explicacao": exp}


def relatorio(qs):
    """Métricas do lote (para o log): acerto chutando a mais longa e distribuição da correta."""
    if not qs:
        return "lote vazio"
    longa = sum(1 for q in qs if max(q["alternativas"], key=lambda k: len(q["alternativas"][k])) == q["gabarito"])
    dist = {l: sum(1 for q in qs if q["gabarito"] == l) for l in "ABCDE"}
    return f"chute na mais longa acertaria {100 * longa // len(qs)}% · posições {dist}"
