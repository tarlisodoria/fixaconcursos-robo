"""Estado (UF) de um concurso a partir do que a banca publica (sigla, nome do órgão, cidade).

As bancas não informam a UF. Ordem das pistas, da mais segura para a menos:
  1. sufixo explícito no nome: "SAD/PE", "CASAN - SC", "IGP-RS"
  2. UF solta na sigla: "PM AL", "TJ CE", "SEFAZ AL"
  3. nome do estado no órgão: "Secretaria da Fazenda de Alagoas", "Instituto Federal do Pará"
  4. cidade de prefeitura/câmara/município, pela lista do IBGE (nome ambíguo entre estados = sem UF)
  5. órgão federal com vagas no país todo -> "BR" (Nacional)
O resumo do edital por IA confirma depois (ele lê o edital e manda a UF certa).
"""
import json, os, re, unicodedata

UFS = {"AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará", "DF": "Distrito Federal",
       "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais",
       "PA": "Pará", "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
       "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins"}

FEDERAIS_SIGLA = {"CGU", "PF", "PRF", "BCB", "BACEN", "CD", "STF", "STJ", "TST", "TSE", "TCU", "AGU", "INSS", "IBGE", "ANATEL", "ANEEL", "ANP",
                  "ANS", "ANVISA", "ANTT", "ANAC", "CNPQ", "CAPES", "FINEP", "DATAPREV", "SERPRO", "EBSERH", "MPU", "DPU", "ABIN", "CVM",
                  "SUSEP", "PREVIC", "INCRA", "IBAMA", "ICMBIO", "FUNAI", "AEB", "MRE", "MPF", "AGSUS", "CNU", "ENEM", "DREI", "EMBRAPA", "CEF"}
FEDERAIS_NOME = re.compile(r"\b(da uni[aã]o|c[aâ]mara dos deputados|senado federal|banco central|minist[eé]rio|pol[ií]cia federal|"
                           r"pol[ií]cia rodovi[aá]ria federal|receita federal|ag[eê]ncia nacional|instituto nacional|conselho nacional|"
                           r"tribunal superior|supremo tribunal|advocacia-geral|defensoria p[uú]blica da uni[aã]o|empresa brasileira)\b", re.I)
# adjetivo do estado no nome: "Universidade Federal do Triângulo Mineiro", "IF Baiano", "Instituto Rio Grandense do Arroz"
GENTILICOS = [(re.compile(r), uf) for r, uf in [(r"\bmineir[oa]\b", "MG"), (r"\bfluminense\b", "RJ"), (r"\bpaulista\b", "SP"), (r"\bbaian[oa]\b", "BA"),
              (r"\brio[- ]?grandense\b|\bgauch[oa]\b", "RS"), (r"\bcatarinense\b", "SC"), (r"\bparanaense\b", "PR"), (r"\bcapixaba\b", "ES"),
              (r"\bpotiguar\b", "RN"), (r"\bcearense\b", "CE"), (r"\bparaense\b", "PA"), (r"\bamazonense\b", "AM"), (r"\bgoiano\b", "GO"),
              (r"\bpernambucano\b", "PE"), (r"\bsergipano\b", "SE"), (r"\balagoano\b", "AL"), (r"\bmaranhense\b", "MA"), (r"\bpiauiense\b", "PI"),
              (r"\bparaibano\b", "PB"), (r"\btocantinense\b", "TO"), (r"\bacreano\b", "AC"), (r"\brondoniense\b", "RO")]]
MUNICIPAL = re.compile(r"prefeitura|c[aâ]mara municipal|municipal|munic[ií]pio|santa casa|guarda", re.I)
RX_DE = re.compile(r"\b(?:de|do|da)\s+([A-ZÀ-Ú][A-Za-zÀ-ú' ]+?)(?=\s*(?:[-/,(]|\bde\b|\bdo\b|\bda\b|$))")


def _sa(t):
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()


# nome do estado (sem acento) -> UF; os mais longos primeiro ("mato grosso do sul" antes de "mato grosso")
ESTADOS = sorted(((_sa(n), uf) for uf, n in UFS.items()), key=lambda x: -len(x[0]))
RX_ESTADO = [(re.compile(r"\b(?:de|do|da|dos|das)\s+" + re.escape(n) + r"\b"), uf) for n, uf in ESTADOS]
RX_SUFIXO = re.compile(r"(?:[/-]\s*|\s-\s)(" + "|".join(UFS) + r")\s*$|\b(?:[A-Z]{2,})[/-](" + "|".join(UFS) + r")\b")
RX_CIDADE = re.compile(r"(?:prefeitura(?: municipal)?|c[aâ]mara municipal|munic[ií]pio|secretaria municipal[^,]*?)\s+(?:de|do|da)\s+([A-Za-zÀ-ú' .-]+?)(?:\s*[-/,(]|$)", re.I)

_MUNICIPIOS = None


def municipios():
    """{nome sem acento: {UFs}} a partir da lista do IBGE (baixada uma vez por rodada, guardada em arquivo)."""
    global _MUNICIPIOS
    if _MUNICIPIOS is not None:
        return _MUNICIPIOS
    cache = os.path.join(os.path.dirname(__file__), ".cache_municipios.json")
    dados = None
    try:
        if os.path.exists(cache):
            dados = json.load(open(cache, encoding="utf-8"))
        else:
            import requests
            r = requests.get("https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=60)
            r.raise_for_status()
            dados = [[m["nome"], m["microrregiao"]["mesorregiao"]["UF"]["sigla"]] for m in r.json() if m.get("microrregiao")]
            json.dump(dados, open(cache, "w", encoding="utf-8"))
    except Exception as e:  # noqa: BLE001  (sem IBGE: só não deduz pela cidade)
        print(f"uf: lista de municípios indisponível ({e})")
        dados = []
    _MUNICIPIOS = {}
    for nome, uf in dados:
        _MUNICIPIOS.setdefault(_sa(nome), set()).add(uf)
    return _MUNICIPIOS


def inferir_uf(sigla, orgao=None, extra=None):
    """-> 'PA', 'BR' (nacional) ou None."""
    textos = [t for t in (orgao, sigla, extra) if t]
    for t in textos:  # 1. sufixo explícito
        m = RX_SUFIXO.search(t.strip())
        if m:
            return m.group(1) or m.group(2)
    for tok in re.split(r"[\s/–-]+", sigla or ""):  # 2. UF solta na sigla
        if tok in UFS and tok not in ("CE",) or tok == "CE" and len((sigla or "").split()) > 1:
            return tok
    for t in textos:  # 3. nome do estado
        a = _sa(t)
        for rx, uf in RX_ESTADO:
            if rx.search(a):
                return uf
    for t in textos:  # 3b. adjetivo do estado
        a = _sa(t)
        for rx, uf in GENTILICOS:
            if rx.search(a):
                return uf
    for t in textos:  # 4. cidade (só órgão municipal: "Saúde" e "Educação" também são nomes de município)
        m = RX_CIDADE.search(t)
        cands = [m.group(1)] if m else []
        if MUNICIPAL.search(t):
            cands += [c for c in reversed(RX_DE.findall(t))]  # "Secretaria Municipal de Educação de Salvador" -> Salvador
        for c in cands:
            ufs = municipios().get(_sa(c).strip())
            if ufs and len(ufs) == 1:
                return next(iter(ufs))
    primeira = re.split(r"[\s/–-]+", (sigla or "").upper())[0]
    if primeira in FEDERAIS_SIGLA or any(FEDERAIS_NOME.search(t) for t in textos):  # 5. federal
        return "BR"
    return None
