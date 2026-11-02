"""Classificador de assunto SEM IA (custo zero): aprende com as questões que já têm assunto e sugere para as que não têm.

Como funciona:
  1. Treina um modelo de texto simples (TF-IDF de palavras + regressão logística, scikit-learn) com as questões cujo assunto
     foi dado pela IA ou por revisão humana (nunca com o que ele mesmo classificou: não aprende com o próprio chute).
  2. Mede o acerto em 20% das questões rotuladas deixadas de fora do treino e escolhe a confiança mínima em que o acerto
     fica >= PRECISAO_ALVO. Se nenhuma confiança atinge isso, NÃO classifica nada (a trava protege o banco).
  3. Assunto específico só com confiança alta; senão tenta o assunto-pai (mais fácil de acertar); senão deixa sem.
  4. Grava assunto_origem = 'local' para a IA poder revisar depois.
As métricas de cada rodada vão para robo_diagnostico (etapa 'classificar_local').
"""
import re, unicodedata

import supa

PRECISAO_ALVO = 0.90
MIN_EXEMPLOS = 4       # assunto com menos exemplos que isso não entra no modelo
MIN_COBERTURA = 0.15   # a trava só libera se, com a confiança escolhida, ao menos 15% do teste for classificado


def _norm(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", t)


def texto_de(q):
    alts = q.get("alternativas") or {}
    partes = [q.get("disciplina") or q.get("disciplina_prova") or "", q.get("enunciado") or "",
              " ".join(str(v) for v in alts.values()) if isinstance(alts, dict) else ""]
    return _norm(" ".join(partes))


def todas(tabela, params):
    """Todas as linhas (supa.select já pagina de 1000 em 1000 pelo cabeçalho Range; ordem fixa por id para não repetir/pular)."""
    return supa.select(tabela, {**params, "order": "id"})


def _limiar(confs, certos, alvo=PRECISAO_ALVO):
    """Menor confiança t tal que, entre os itens com confiança >= t, o acerto é >= alvo. -> (t, precisão, cobertura) ou None."""
    pares = sorted(zip(confs, certos), reverse=True)
    melhor, acertos = None, 0
    for i, (c, ok) in enumerate(pares, 1):
        acertos += ok
        if acertos / i >= alvo and i >= 10:
            melhor = (c, acertos / i, i / len(pares))
    return melhor


def treinar_e_prever(rotuladas, alvos, pai_de):
    """rotuladas: [(texto, assunto)]; alvos: [(id, texto)]. -> ({id: assunto}, métricas)."""
    import numpy as np
    from collections import Counter
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split

    cont = Counter(a for _, a in rotuladas)
    dados = [(t, a) for t, a in rotuladas if cont[a] >= MIN_EXEMPLOS]
    met = {"rotuladas": len(rotuladas), "usadas": len(dados), "assuntos": len({a for _, a in dados}), "alvos": len(alvos)}
    if len(dados) < 200 or not alvos:
        met["motivo"] = "poucos dados para treinar ou nada a classificar"; return {}, met
    X, y = [t for t, _ in dados], [a for _, a in dados]

    def modelo():
        vet = TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=60000, sublinear_tf=True)
        clf = LogisticRegression(C=8, max_iter=3000, class_weight="balanced")
        return vet, clf

    def prever(vet, clf, textos):
        P = clf.predict_proba(vet.transform(textos))
        classes = list(clf.classes_)
        pais = {}
        for j, c in enumerate(classes):  # probabilidade do pai = soma dos filhos + o próprio
            p = pai_de.get(c) or c
            pais.setdefault(p, []).append(j)
        res = []
        for row in P:
            j = int(np.argmax(row))
            folha, pf = classes[j], float(row[j])
            pai, pp = max(((p, float(row[js].sum())) for p, js in pais.items()), key=lambda x: x[1])
            res.append((folha, pf, pai, pp))
        return res

    # 1) avaliação honesta: 20% fora do treino
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    vet, clf = modelo()
    clf.fit(vet.fit_transform(Xtr), ytr)
    av = prever(vet, clf, Xte)
    lim_f = _limiar([pf for _, pf, _, _ in av], [int(f == r) for (f, _, _, _), r in zip(av, yte)])
    # o pai está certo se o assunto real é ele ou filho dele
    lim_p = _limiar([pp for _, _, _, pp in av], [int(p == (pai_de.get(r) or r)) for (_, _, p, _), r in zip(av, yte)])
    met["acerto_geral_folha"] = round(float(sum(f == r for (f, _, _, _), r in zip(av, yte)) / len(yte)), 3)
    met["limiar_folha"] = lim_f and {"conf": round(float(lim_f[0]), 3), "precisao": round(float(lim_f[1]), 3), "cobertura": round(float(lim_f[2]), 3)}
    met["limiar_pai"] = lim_p and {"conf": round(float(lim_p[0]), 3), "precisao": round(float(lim_p[1]), 3), "cobertura": round(float(lim_p[2]), 3)}
    ok_f = lim_f and lim_f[2] >= MIN_COBERTURA
    ok_p = lim_p and lim_p[2] >= MIN_COBERTURA
    if not ok_f and not ok_p:
        met["motivo"] = f"trava: nenhuma confiança atinge {int(PRECISAO_ALVO * 100)}% de acerto com cobertura útil"; return {}, met

    # 2) modelo final com todos os rotulados
    vet, clf = modelo()
    clf.fit(vet.fit_transform(X), y)
    out = {}
    for (qid, _), (folha, pf, pai, pp) in zip(alvos, prever(vet, clf, [t for _, t in alvos])):
        if ok_f and pf >= lim_f[0]:
            out[qid] = str(folha)
        elif ok_p and pp >= lim_p[0]:
            out[qid] = str(pai)
    met["classificadas"] = len(out)
    return out, met
