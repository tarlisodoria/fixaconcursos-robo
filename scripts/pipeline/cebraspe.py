"""Cliente do Cebraspe: lista concursos pela API pública do site e baixa cadernos + gabaritos definitivos do CDN.

Fluxo público (o mesmo do site www.cebraspe.org.br, que é uma aplicação JavaScript):
  1. GET https://apis.cebraspe.org.br/cebraspe/eventos/tipo/concursos/fase/{fase}
       -> [{"faseEvento": "...", "eventos": [{"eventoURL", "eventoNomeAbreviado", "eventoAno", "eventoStatus", ...}]}]
  2. GET https://apis.cebraspe.org.br/cebraspe/eventos/{eventoURL}
       -> "eventoCargos" [{"idArea": "04", "area": "Tecnologia da Informação"}],
          "arquivosGabarito" [{"nomeArquivo", "descricaoArquivo", "dataArquivo"}]  (cadernos + gabaritos ficam aqui)
  3. GET https://cdn.cebraspe.org.br/concursos/{eventoURL}/arquivos/{nomeArquivo}
Caderno "PROVA OBJETIVA – ... – CARGO 4" = 051_AEB_24_004_01.pdf; gabarito definitivo = Gab_Definitivo_051_AEB_24_004_01.pdf.
Duas famílias de prova: múltipla escolha (Questão 21..60, A–E) e Certo/Errado (itens 1..120). Anulada = "X".
robots.txt do site: liberado. Boa educação: User-Agent identificado, 1 requisição por vez, pausa entre requisições.
"""
import io, re, time, unicodedata
from urllib.parse import quote
import requests

API = "https://apis.cebraspe.org.br/cebraspe"
CDN = "https://cdn.cebraspe.org.br/concursos"
UA = "FixaConcursosBot/1.0 (+https://www.fixaconcursos.com.br; coleta de provas publicas para estudo)"
PAUSA = 1.5


def sessao():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9"})
    return s


def _get(s, url, **kw):
    time.sleep(PAUSA)
    r = s.get(url, timeout=90, **kw)
    r.raise_for_status()
    return r


def _sem_acento(t):
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().upper()


def listar_eventos(s):
    """Todos os concursos do site, de todas as fases (novos, inscrições abertas, em andamento, encerrados).
    O endpoint sem fase devolve os grupos juntos; os nomes de fase são novos | inscricoes-abertas | em-andamento | encerrado."""
    grupos = _get(s, f"{API}/eventos/tipo/concursos").json()
    vistos, out = set(), []
    for g in grupos if isinstance(grupos, list) else [grupos]:
        for e in (g or {}).get("eventos") or []:
            eid = e.get("eventoURL")
            if eid and eid not in vistos:
                vistos.add(eid); out.append(e)
    return out


def listar(s):
    """Concursos no formato que o vigia usa (id, nome, edital, status, cargos, dataProva)."""
    out = []
    for e in listar_eventos(s):
        cargos = [c.get("area") for c in e.get("eventoCargos") or [] if c.get("area")]
        out.append({"id": e["eventoURL"], "nome": e.get("eventoNomeAbreviado") or e["eventoURL"], "edital": e.get("eventoNomeCompleto"),
                    "status": e.get("eventoStatus"), "cargos": cargos,
                    "dataProva": str(e.get("eventoAno") or "") or None, "tipoProcesso": e.get("eventoTipo")})
    return out


def detalhe(s, eid):
    j = _get(s, f"{API}/eventos/{quote(eid)}").json()
    return j[0] if isinstance(j, list) and j else j


def cargos_de(s, eid):
    """Nomes dos cargos (a lista de concursos não traz; o detalhe traz em eventoCargos)."""
    return [c.get("area") for c in detalhe(s, eid).get("eventoCargos") or [] if c.get("area")]


def url_arquivo(eid, nome):
    return f"{CDN}/{quote(eid)}/arquivos/{quote(nome)}"


RX_CARGOS = re.compile(r"\b(?:CARGOS?|AREAS?|ENFASES?|PERFIS?|PERFIL)\s+((?:\d+\s*(?:,|E|A|-|–)?\s*)+)", re.I)
RX_COD_CARGO = re.compile(r"^\d{3}_.+?_(\d{3})_\d{2}$")  # 931_FINEP_005_01 -> cargo/área 5 (CG1/CB1 = conhecimentos gerais)
RX_ESPEC = re.compile(r"(?:PERFIL|ESPECIALIDADE|[ÁA]REA DE ATUA[ÇC][ÃA]O|[ÁA]REA|[ÊE]NFASE\s*\d*)\s*[:–-]\s*", re.I)


def especialidade(nome):
    """Parte que diferencia o cargo: 'ANALISTA DE TI – PERFIL: ENGENHARIA CIVIL' -> 'ENGENHARIA CIVIL'.
    Sem isso o filtro casa 'Tecnologia da Informação' no nome genérico do cargo e traz prova de engenharia civil."""
    partes = RX_ESPEC.split(nome or "")
    return partes[-1].strip() if len(partes) > 1 and partes[-1].strip() else (nome or "")


def cargos_da_descricao(desc):
    """'CONHECIMENTOS BÁSICOS PARA OS CARGOS 2, 3 E 4' -> [2,3,4]; 'CARGO 4' -> [4]; 'CARGOS 1 A 5' -> [1..5]; nada -> []."""
    d = _sem_acento(desc)
    m = RX_CARGOS.search(d)
    if not m:
        return []
    trecho = m.group(1)
    faixa = re.search(r"(\d+)\s*(?:A|-|–)\s*(\d+)", trecho)
    if faixa:
        return list(range(int(faixa.group(1)), int(faixa.group(2)) + 1))
    return [int(x) for x in re.findall(r"\d+", trecho)]


def cadernos(det):
    """Pares (caderno, gabarito definitivo) da prova OBJETIVA, com os cargos a que cada caderno se aplica.
    -> [{"codigo", "descricao", "caderno", "gabarito", "cargos": [nomes], "cargos_num": [int], "geral": bool}] (gabarito None se ainda não saiu)."""
    arqs = det.get("arquivosGabarito") or []
    areas = {int(c["idArea"]): c.get("area") or "" for c in det.get("eventoCargos") or [] if str(c.get("idArea") or "").isdigit()}
    por_nome = {_sem_acento(a.get("nomeArquivo")): a for a in arqs}
    out = []
    for a in arqs:
        desc, nome = _sem_acento(a.get("descricaoArquivo")), a.get("nomeArquivo") or ""
        if not nome.lower().endswith(".pdf") or nome.upper().startswith("GAB") or "DISCURSIV" in desc or "PADRAO" in desc:
            continue
        if not ("PROVA OBJETIVA" in desc or "CADERNO" in desc or re.match(r"^\d{3}_", nome)):
            continue
        codigo = re.sub(r"\.pdf$", "", nome, flags=re.I)
        gab = por_nome.get(_sem_acento(f"Gab_Definitivo_{codigo}.pdf"))
        nums = cargos_da_descricao(a.get("descricaoArquivo"))
        if not nums and (m := RX_COD_CARGO.match(codigo)) and int(m.group(1)) in areas:
            nums = [int(m.group(1))]
        nomes = [areas[n] for n in nums if n in areas] or ([] if nums else list(areas.values()))
        out.append({"codigo": codigo, "descricao": a.get("descricaoArquivo") or "", "caderno": nome,
                    "gabarito": gab.get("nomeArquivo") if gab else None, "cargos": nomes, "cargos_num": nums,
                    "geral": not nums or len(nums) > 1})
    return out


def baixar(s, eid, nome):
    r = _get(s, url_arquivo(eid, nome), headers={"Accept": "application/pdf,*/*"})
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"{nome}: o CDN não devolveu um PDF")
    return r.content


RESP = re.compile(r"^(?:[A-E]|X|\*|C|E)$")


def parse_gabarito(pdf_bytes):
    """Gabarito oficial do Cebraspe (PDF com texto): linhas de números com a linha de respostas logo abaixo,
    casadas pela posição horizontal. -> {numero: "A".."E" | "C" | "E" | None (anulada)}"""
    import pdfplumber
    out = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pg in pdf.pages:
            ws = pg.extract_words(keep_blank_chars=False, x_tolerance=1.5, y_tolerance=2)
            linhas, ult = {}, None
            for w in sorted(ws, key=lambda w: w["top"]):
                if ult is None or w["top"] - ult > 3:
                    ult = w["top"]
                linhas.setdefault(ult, []).append(w)
            ordem = sorted(linhas)
            for i, k in enumerate(ordem):
                nums = [w for w in linhas[k] if re.fullmatch(r"\d{1,3}", w["text"])]
                if len(nums) < 5 or len(nums) < 0.8 * len(linhas[k]):
                    continue
                # a linha de respostas é a próxima (até ~25 pt abaixo) que só tem letras de resposta
                for k2 in ordem[i + 1:i + 4]:
                    resp = [w for w in linhas[k2] if RESP.match(w["text"].upper())]
                    if len(resp) >= 0.8 * len(nums) and (min(w["top"] for w in linhas[k2]) - max(w["top"] for w in nums)) < 30:
                        for n in nums:
                            cx = (n["x0"] + n["x1"]) / 2
                            r = min(resp, key=lambda w: abs((w["x0"] + w["x1"]) / 2 - cx))
                            if abs((r["x0"] + r["x1"]) / 2 - cx) < 15:
                                v = r["text"].upper()
                                out[int(n["text"])] = None if v in ("X", "*") else v
                        break
    return out


def formato(gab):
    """'ce' quando todas as respostas válidas são C/E (prova de julgar itens), senão 'me' (múltipla escolha)."""
    vals = {v for v in gab.values() if v}
    return "ce" if vals and vals <= {"C", "E"} else "me"
