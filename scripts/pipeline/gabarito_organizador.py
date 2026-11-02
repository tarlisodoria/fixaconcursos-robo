"""Valida e normaliza o JSON de gabarito produzido por organizar_provas.py e o confere com o gabarito do PDF oficial.

Decisões do dono (01/11/2026):
- situacao "preliminar": aceito, mas toda questão da prova vai para a fila com AVISO_PRELIMINAR (nunca publica sozinha);
- lido_por_ocr ou incompleto: conferido questão a questão com o PDF oficial enviado junto; diferença vira aviso;
- JSON inválido (cargo/tipo divergente, letra estranha...): o processar_envio registra no log e lê o PDF, como antes.
"""

AVISO_PRELIMINAR = "gabarito preliminar: confira se saiu o definitivo"


def normalizar_gabarito_organizador(payload, cargo_cod=None, tipo=None):
    """-> {questão: letra ou None (anulada)}. ValueError se o JSON não serve (aí vale a leitura do PDF)."""
    if not isinstance(payload, dict):
        raise ValueError("JSON do organizador inválido: esperado um objeto")

    codigo_json = str(payload.get("cargo_codigo") or "").strip().upper()
    codigo_esperado = str(cargo_cod or "").strip().upper()
    if codigo_esperado and not codigo_json:
        raise ValueError("JSON do organizador sem código de cargo")
    if codigo_esperado and codigo_json and codigo_esperado != codigo_json:
        raise ValueError(f"código do cargo diverge: formulário {codigo_esperado}, JSON {codigo_json}")

    def tipo_numero(valor):
        try:
            return str(int(str(valor or "1").strip()))
        except (TypeError, ValueError):
            return str(valor or "1").strip().upper()

    if tipo is not None and tipo_numero(tipo) != tipo_numero(payload.get("tipo")):
        raise ValueError(f"tipo de prova diverge: formulário {tipo}, JSON {payload.get('tipo')}")

    respostas = payload.get("respostas")
    if not isinstance(respostas, dict) or len(respostas) < 10:
        raise ValueError("JSON do organizador não contém ao menos 10 respostas")
    total = payload.get("total")
    if not isinstance(total, int) or total != len(respostas):
        raise ValueError("total declarado no JSON não corresponde às respostas")

    resultado = {}
    for numero, resposta in respostas.items():
        try:
            n = int(numero)
        except (TypeError, ValueError):
            raise ValueError(f"número de questão inválido no JSON: {numero!r}") from None
        if n < 1 or n in resultado:
            raise ValueError(f"número de questão inválido ou repetido: {n}")
        letra = str(resposta).strip().upper()
        if letra in {"*", "X", "#"}:
            resultado[n] = None
        elif letra in {"A", "B", "C", "D", "E"}:
            resultado[n] = letra
        else:
            raise ValueError(f"resposta inválida na questão {n}: esperado A–E ou anulada")
    return dict(sorted(resultado.items()))


def precisa_conferir(payload):
    """Leitura por OCR ou incompleta: o robô confere com o PDF oficial antes de usar."""
    return payload.get("lido_por_ocr") is True or payload.get("completo") is not True or bool(payload.get("faltando"))


def preliminar(payload):
    return str(payload.get("situacao") or "").strip().lower() == "preliminar"


def _fmt(letra):
    return letra or "anulada"


def conferir_com_pdf(organizador, pdf):
    """Confere questão a questão. -> (gabarito, {questão: [avisos]}). Vale a resposta do PDF oficial; a divergente e a que
    o PDF não trouxe (fica a do organizador) recebem aviso e vão para a fila humana."""
    gab, avisos = dict(pdf), {}
    for n, letra in organizador.items():
        if n not in pdf:
            gab[n] = letra
            avisos[n] = [f"gabarito do organizador não conferido no PDF oficial (Q{n}: {_fmt(letra)})"]
        elif pdf[n] != letra:
            avisos[n] = [f"gabarito do organizador diverge do PDF oficial (Q{n}: {_fmt(letra)} vs {_fmt(pdf[n])})"]
    return dict(sorted(gab.items())), avisos
