"""Acesso mínimo ao Supabase (REST + Storage) com a service role key (só no GitHub Actions, via secrets)."""
import json, os
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
        r = requests.get(f"{URL}/rest/v1/{tabela}", params=params or {}, headers=_h({"Range": f"{de}-{de + 999}"}), timeout=60)
        if r.status_code == 416 and de:  # total era múltiplo de 1000: a página seguinte não existe
            return out
        r.raise_for_status()
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
    out = []
    for i in range(0, len(linhas), 200):
        r = requests.post(f"{URL}/rest/v1/{tabela}", params=params, headers=_h({"Prefer": pref}), data=json.dumps(linhas[i:i + 200]), timeout=120)
        if r.status_code >= 300:
            raise RuntimeError(f"{tabela}: {r.status_code} {r.text[:500]}")
        if retornar:
            out += r.json()
    return out


def update(tabela, filtro, valores):
    _checar(tabela, TABELAS_PUBLICAS)
    r = requests.patch(f"{URL}/rest/v1/{tabela}", params=filtro, headers=_h({"Prefer": "return=minimal"}), data=json.dumps(valores), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"{tabela}: {r.status_code} {r.text[:500]}")


def delete(tabela, filtro):
    _checar(tabela, TABELAS_PUBLICAS)
    r = requests.delete(f"{URL}/rest/v1/{tabela}", params=filtro, headers=_h({"Prefer": "return=minimal"}), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"{tabela}: {r.status_code} {r.text[:500]}")


def subir_arquivo(bucket, caminho, conteudo, tipo="application/pdf"):
    r = requests.post(f"{URL}/storage/v1/object/{bucket}/{caminho}", headers={"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": tipo, "x-upsert": "true"}, data=conteudo, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"storage: {r.status_code} {r.text[:300]}")
    return caminho


def rpc(funcao, args):
    _checar(funcao, RPCS_PUBLICAS)
    r = requests.post(f"{URL}/rest/v1/rpc/{funcao}", headers=_h(), data=json.dumps(args), timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"rpc {funcao}: {r.status_code} {r.text[:500]}")
    return r.json() if r.text else None


def baixar_arquivo(bucket, caminho):
    r = requests.get(f"{URL}/storage/v1/object/{bucket}/{caminho}", headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"}, timeout=180)
    if r.status_code >= 300:
        raise RuntimeError(f"storage: {r.status_code} {r.text[:300]}")
    return r.content
