"""Compara a transcrição de cadernos guardados por dois modelos do Gemini (ex.: Lite x Flash). SÓ LEITURA: não grava nada.

Uso local (as chaves ficam só no seu terminal; nunca no código nem em arquivo):
    set SUPABASE_URL=...  &  set SUPABASE_SERVICE_ROLE_KEY=...  &  set GEMINI_API_KEY=...      (cmd)
    python scripts/pipeline/comparar_transcricao.py --n 5 --modelos gemini-3.5-flash-lite,gemini-3.6-flash

Pega N provas com status aguardando_ia e PDF guardado (uma por concurso, bancas variadas), transcreve cada caderno com cada
modelo (mesmo prompt do robô, ia_extrair.PROMPT) e mostra: questões esperadas (gabarito oficial), lidas, completas (4+
alternativas; Certo/Errado conta inteira), se passa na trava de 90% (ia_extrair.COBERTURA_MIN_GEMINI) e as divergências entre
os modelos (questão que um leu e o outro não; enunciado ou alternativas diferentes). Gasta 1 pedido por caderno e modelo
(Flash: 20/dia). Custo zero no plano grátis.
"""
import argparse, base64, difflib, json, os, re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("IA_DESLIGADA", "1")  # só Gemini
os.environ.setdefault("GEMINI_MAX_CHAMADAS", "100")
os.environ.setdefault("GEMINI_MAX_TRANSCREVER", "100")

import ia, supa  # noqa: E402
from ia_extrair import COBERTURA_MIN_GEMINI, PROMPT, REGRA_CE, _ler_transcricao  # noqa: E402


def _so_leitura():
    """Trava de segurança: qualquer escrita no banco vira erro."""
    def recusa(*a, **k):
        raise RuntimeError("comparar_transcricao é só leitura")
    for f in ("upsert", "update", "delete", "rpc", "subir_arquivo"):
        setattr(supa, f, recusa)


def escolher(n):
    provas = supa.select("ingestao_provas", {"select": "id,concurso_ref,cargo_nome,pdf_path,gabarito,n_questoes", "status": "eq.aguardando_ia",
                                             "pdf_path": "not.is.null", "order": "id"})
    refs = ",".join(sorted({f'"{p["concurso_ref"]}"' for p in provas}))
    banca = {c["id"]: c["banca"] for c in supa.select("ingestao_concursos", {"select": "id,banca", "id": f"in.({refs})"})} if refs else {}
    escolhidas, vistos, bancas = [], set(), {}
    for p in sorted(provas, key=lambda p: (bancas.get(banca.get(p["concurso_ref"]), 0), p["id"])):
        b = banca.get(p["concurso_ref"], "?")
        if p["concurso_ref"] in vistos or not p.get("gabarito"):
            continue
        if sum(1 for e in escolhidas if e["banca"] == b) >= max(1, n // 2):  # varia as bancas
            continue
        vistos.add(p["concurso_ref"]); escolhidas.append({**p, "banca": b})
        if len(escolhidas) == n:
            break
    return escolhidas


def transcrever(p, pdf, modelo):
    numeros = sorted(int(k) for k in p["gabarito"])
    formato = "ce" if set(v for v in p["gabarito"].values() if v) <= {"C", "E"} else "me"
    blocos = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf).decode()}},
              {"type": "text", "text": PROMPT.format(banca=p["banca"], faixa=f"numeradas de {min(numeros)} a {max(numeros)}",
                                                     regra_formato=REGRA_CE if formato == "ce" else "")}]
    ia._modelo_fora.clear()
    ini = time.time()
    try:
        txt, parada = ia.gemini(f"transcrever caderno ({p['banca']})", blocos, 32000, modelos=[modelo])
        _, qs = _ler_transcricao(txt, parada, formato)
        erro = None
    except Exception as e:  # noqa: BLE001
        qs, parada, erro = [], None, f"{type(e).__name__}: {str(e)[:120]}"
    esperadas = set(numeros)
    lidas = {q["numero"]: q for q in qs if q["numero"] in esperadas}
    completas = sum(1 for q in lidas.values() if formato == "ce" or len(q["alternativas"]) >= 4)
    return {"modelo": modelo, "esperadas": len(esperadas), "lidas": len(lidas), "completas": completas, "parada": parada, "erro": erro,
            "passa": completas >= COBERTURA_MIN_GEMINI * len(esperadas), "segundos": round(time.time() - ini), "questoes": lidas}


def _parecido(a, b):
    norm = lambda s: re.sub(r"\W+", " ", (s or "").lower()).strip()  # noqa: E731
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def divergencias(r1, r2, limite=0.9):
    so1 = sorted(set(r1["questoes"]) - set(r2["questoes"]))
    so2 = sorted(set(r2["questoes"]) - set(r1["questoes"]))
    enun, alts = [], []
    for n in sorted(set(r1["questoes"]) & set(r2["questoes"])):
        q1, q2 = r1["questoes"][n], r2["questoes"][n]
        if _parecido(q1["enunciado"], q2["enunciado"]) < limite:
            enun.append(n)
        if set(q1["alternativas"]) != set(q2["alternativas"]) or any(
                _parecido(q1["alternativas"][k], q2["alternativas"].get(k)) < limite for k in q1["alternativas"]):
            alts.append(n)
    return {"so_no_1": so1, "so_no_2": so2, "enunciado_diferente": enun, "alternativas_diferentes": alts}


def main():
    ap = argparse.ArgumentParser(description="Compara a transcrição de dois modelos Gemini (só leitura)")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--modelos", default="gemini-3.5-flash-lite,gemini-3.6-flash")
    ap.add_argument("--saida", default=None, help="JSON com o resumo (sem o texto das questões)")
    a = ap.parse_args()
    modelos = [m.strip() for m in a.modelos.split(",") if m.strip()]
    os.environ["GEMINI_MODELOS_PESADO"] = ",".join(modelos)  # a lista pesada passa a ter exatamente os modelos comparados
    os.environ["GEMINI_MODEL"] = ""
    _so_leitura()
    ia.descobrir_modelos()
    linhas = []
    for p in escolher(a.n):
        pdf = supa.baixar_arquivo("provas", p["pdf_path"])
        rs = [transcrever(p, pdf, m) for m in modelos]
        div = divergencias(rs[0], rs[1]) if len(rs) == 2 else {}
        linhas.append({"prova": p["id"], "banca": p["banca"], "cargo": (p.get("cargo_nome") or "")[:40],
                       "resultados": [{k: v for k, v in r.items() if k != "questoes"} for r in rs], "divergencias": div})
    print("\n| Prova | Banca | Modelo | Esperadas | Lidas | Completas | Passa 90% | Tempo |")
    print("|---|---|---|---|---|---|---|---|")
    for l in linhas:
        for r in l["resultados"]:
            print(f"| {l['prova']} {l['cargo']} | {l['banca']} | {r['modelo']} | {r['esperadas']} | {r['lidas']} | {r['completas']} | "
                  f"{'sim' if r['passa'] else 'NÃO'} | {r['segundos']}s{(' · ' + r['erro']) if r['erro'] else ''} |")
    print("\nDivergências (modelo 1 x modelo 2):")
    for l in linhas:
        d = l["divergencias"]
        print(f"- {l['prova']}: só no 1º {len(d.get('so_no_1', []))}, só no 2º {len(d.get('so_no_2', []))}, "
              f"enunciado diferente {len(d.get('enunciado_diferente', []))}, alternativas diferentes {len(d.get('alternativas_diferentes', []))}")
    passa = {m: sum(1 for l in linhas for r in l["resultados"] if r["modelo"] == m and r["passa"]) for m in modelos}
    print("\nPassaram na trava de 90%: " + ", ".join(f"{m} {n}/{len(linhas)}" for m, n in passa.items()))
    if a.saida:
        Path(a.saida).write_text(json.dumps(linhas, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
