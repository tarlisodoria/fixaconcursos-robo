"""Acesso mínimo ao Supabase (REST + Storage) com a service role key (só no GitHub Actions, via secrets)."""
import json, os, re, time
import requests

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Fronteira de dados (01/11/2026): o robô roda num repositório PÚBLICO (log do Actions é público). Lá ele só toca tabelas de
# conteúdo público; tabela com dado de aluno ou de assinatura (perfis, tentativas, assinaturas, avisos, seguidores, pedidos de
# plano...) só com ROBO_PRIVADO=1, que existe apenas nos workflows do repositório privado. Lista de PERMISSÃO: tabela nova
# fica bloqueada no robô público até alguém decidir que ela é pública.
TABELAS_PUBLICAS = {"assuntos", "concursos", "config", "fichas", "ia_lotes", "ia_uso", "ingestao_concursos", "ingestao_provas",
                    "ingestao_questoes", "leis_staging", "noticias", "planos_catalogo", "questoes", "robo_diagnostico", "textos",
                    "topico_assuntos", "topicos", "vitrine_concursos"}
RPCS_PUBLICAS = {"definir_assuntos", "marcar_auditoria", "uso_storage_bytes"}


def privado():
    return (os.environ.get("ROBO_PRIVADO") or "").strip() == "1"


class DadoDeAluno(PermissionError):
    """Acesso a tabela/RPC fora da lista pública sem ROBO_PRIVADO=1."""


def _checar(nome, publicas):
    if nome not in publicas and not privado():
        raise DadoDeAluno(f"'{nome}' não é conteúdo público: só roda no repositório privado (ROBO_PRIVADO=1)")


# Supabase instável (18/11/2026: um 522 do Cloudflare derrubou a rodada): nova tentativa com espera crescente. Pedido
# idempotente (leitura, update, upsert com chave, delete, rpc, Storage) repete em falha de conexão, timeout e 502/503/504/520-524.
# Insert simples (upsert sem chave de conflito nem id) só repete quando o pedido não chegou ao banco: falha de conexão e 520-524.
ESPERAS = (2, 5, 15)
SEMPRE = {502, 503, 504, 520, 521, 522, 523, 524}
NAO_CHEGOU = {520, 521, 522, 523, 524}


def _corpo(r):
    """Começo da resposta para a mensagem de erro; página HTML (Cloudflare) vira só o <title>."""
    t = r.text or ""
    if "<html" in t[:1000].lower() or t.lstrip().lower().startswith("<!doctype"):
        m = re.search(r"<title[^>]*>(.*?)</title>", t, re.I | re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else "página HTML"
    return t[:500]


def _pedir(metodo, url, nome, idempotente=True, **kw):
    """requests.get/post/... com novas tentativas (ESPERAS). Devolve a última resposta; a última falha de rede sobe."""
    for i in range(len(ESPERAS) + 1):
        try:
            r = getattr(requests, metodo.lower())(url, **kw)
            if r.status_code not in (SEMPRE if idempotente else NAO_CHEGOU) or i == len(ESPERAS):
                return r
            motivo = f"HTTP {r.status_code}"
        except requests.ConnectionError as e:  # inclui ConnectTimeout: o pedido não saiu
            if i == len(ESPERAS):
                raise
            motivo = type(e).__name__
        except requests.Timeout as e:  # ReadTimeout: o banco pode ter gravado
            if not idempotente or i == len(ESPERAS):
                raise
            motivo = type(e).__name__
        print(f"supa: {metodo} {nome} {motivo}; nova tentativa {i + 1}/{len(ESPERAS)} em {ESPERAS[i]}s")
        time.sleep(ESPERAS[i])


def _h(extra=None):
    if not URL or not KEY:
        raise SystemExit("Defina SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY (secrets do GitHub).")
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    h.update(extra or {})
    return h


def select(tabela, params=None):
    _checar(tabela, TABELAS_PUBLICAS)
    out, de = [], 0
    while True:
        r = _pedir("GET", f"{URL}/rest/v1/{tabela}", tabela, params=params or {}, headers=_h({"Range": f"{de}-{de + 999}"}), timeout=60)
        if r.status_code == 416 and de:  # total era múltiplo de 1000: a página seguinte não existe
            return out
        if r.status_code >= 300:
            raise RuntimeError(f"{tabela}: {r.status_code} {_corpo(r)}")
        lote = r.json(); out += lote
        if len(lote) < 1000:
            return out
        de += 1000


def upsert(tabela, linhas, on_conflict=None, retornar=False, ignorar=False):
    """ignorar=True: linha cujo id já existe fica como está (não sobrescreve)."""
    _checar(tabela, TABELAS_PUBLICAS)
    if not linhas:
        return []
    pref = ("resolution=ignore-duplicates," if ignorar else "resolution=merge-duplicates,") + ("return=representation" if retornar else "return=minimal")
    params = {"on_conflict": on_conflict} if on_conflict else {}
    # sem chave de conflito nem id (ex.: robo_diagnostico, id serial) é insert simples: repetir à toa duplicaria a linha
    idem = bool(on_conflict) or all("id" in l for l in linhas)
    out = []
    for i in range(0, len(linhas), 200):
        r = _pedir("POST", f"{URL}/rest/v1/{tabela}", tabela, idempotente=idem, params=params, headers=_h({"Prefer": pref}),
                   data=json.dumps(linhas[i:i + 200]), timeout=120)
        if r.status_code >= 300:
            raise RuntimeError(f"{tabela}: {r.status_code} {_corpo(r)}")
        if retornar:
            out += r.json()
    return out


def update(tabela, filtro, valores):
    _checar(tabela, TABELAS_PUBLICAS)
    r = _pedir("PATCH", f"{URL}/rest/v1/{tabela}", tabela, params=filtro, headers=_h({"Prefer": "return=minimal"}), data=json.dumps(valores), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"{tabela}: {r.status_code} {_corpo(r)}")


def delete(tabela, filtro):
    _checar(tabela, TABELAS_PUBLICAS)
    r = _pedir("DELETE", f"{URL}/rest/v1/{tabela}", tabela, params=filtro, headers=_h({"Prefer": "return=minimal"}), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"{tabela}: {r.status_code} {_corpo(r)}")


def subir_arquivo(bucket, caminho, conteudo, tipo="application/pdf"):
    # x-upsert: subir de novo o mesmo arquivo só o substitui (idempotente)
    r = _pedir("POST", f"{URL}/storage/v1/object/{bucket}/{caminho}", f"storage {bucket}",
               headers={"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": tipo, "x-upsert": "true"}, data=conteudo, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"storage: {r.status_code} {_corpo(r)[:300]}")
    return caminho


def rpc(funcao, args):
    _checar(funcao, RPCS_PUBLICAS)
    r = _pedir("POST", f"{URL}/rest/v1/rpc/{funcao}", f"rpc {funcao}", headers=_h(), data=json.dumps(args), timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"rpc {funcao}: {r.status_code} {_corpo(r)}")
    return r.json() if r.text else None


def baixar_arquivo(bucket, caminho):
    r = _pedir("GET", f"{URL}/storage/v1/object/{bucket}/{caminho}", f"storage {bucket}", headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"}, timeout=180)
    if r.status_code >= 300:
        raise RuntimeError(f"storage: {r.status_code} {_corpo(r)[:300]}")
    return r.content
