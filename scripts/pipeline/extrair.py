"""Extração de questões de cadernos AOCP (2 colunas) com detecção de figuras.

Reaproveita o parser de scripts/extrair_aocp.py e acrescenta eventos de imagem por página/coluna,
para marcar as questões que dependem de figura (que o revisor precisa transcrever).
"""
import re, sys
from pathlib import Path
import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import extrair_aocp as base  # noqa: E402

PALAVRAS_FIGURA = re.compile(r"\b(imagem|figura|infogr[aá]fico|gr[aá]fico|charge|tirinha|ilustra[çc][ãa]o|planilha a seguir|tabela a seguir|observe a)\b", re.I)


def extrair_seq(pdf_path):
    """Como base.extrair, mas com eventos ('img', '', meta) na posição de cada imagem relevante."""
    p = pdfplumber.open(pdf_path)
    seq = []
    for i, pg in enumerate(p.pages):
        if i == 0:
            continue  # capa
        W, H = pg.width, pg.height
        top, bottom = 50, H - 60
        txt_pg = pg.extract_text() or ""
        if "RASCUNHO" in txt_pg.upper() and len(txt_pg) < 300:
            continue
        ws_all = [w for w in pg.extract_words() if top < w["top"] < bottom]
        best, split = None, W / 2
        for x in range(int(W * 0.40), int(W * 0.60), 2):
            c = sum(1 for w in ws_all if w["x0"] < x < w["x1"])
            if best is None or c < best:
                best, split = c, x
        cols = [(0, W)] if best > 3 else [(0, split), (split, W)]
        imgs = [im for im in pg.images if (im["x1"] - im["x0"]) > 40 and (im["bottom"] - im["top"]) > 25 and top < im["top"] < bottom]
        for (a, b) in cols:
            eventos = []
            for ln in base.linhas_coluna(pg, a, b, top, bottom):
                t, allb = base.render(ln)
                w0 = ln["words"][0]
                isnum = (len(ln["words"]) == 1 and re.fullmatch(r"\d{1,3}", t) and base.bold(w0["fontname"]) and w0["size"] > 10.5)
                eventos.append((ln["top"], ("num" if isnum else "txt", t, {"bold": allb, "size": w0["size"], "pagina": i + 1})))
            for im in imgs:
                cx = (im["x0"] + im["x1"]) / 2
                if a <= cx < b:
                    eventos.append((im["top"], ("img", "", {"bold": False, "size": 0, "pagina": i + 1,
                                                            "bbox": (im["x0"], im["top"], im["x1"], im["bottom"])})))
            eventos.sort(key=lambda e: e[0])
            seq.extend(e for _, e in eventos)
    return seq


def extrair_questoes(pdf_path, total):
    """Lista de questões {numero, disciplina, texto_base, enunciado, alternativas, pagina, figura, aviso?}."""
    seq = extrair_seq(pdf_path)
    # 1) marca, por posição, a questão aberta quando aparece uma imagem
    numero_atual, esperado, figura_em, pendente = None, 1, set(), []
    caixas = {}
    pagina_de = {}
    for tipo, t, meta in seq:
        if tipo == "num" and int(t) == esperado:
            numero_atual = esperado; esperado += 1
            pagina_de[numero_atual] = meta["pagina"]
            if pendente:
                figura_em.add(numero_atual); caixas.setdefault(numero_atual, []).extend(pendente); pendente = []
        elif tipo == "img":
            caixa = {"pagina": meta["pagina"], "bbox": meta.get("bbox")}
            if numero_atual is None:
                pendente.append(caixa)
            else:
                figura_em.add(numero_atual); caixas.setdefault(numero_atual, []).append(caixa)
    # 2) parse normal (ignorando os eventos de imagem)
    qs = base.parse([e for e in seq if e[0] != "img"], total)
    for q in qs:
        if "erro" in q:
            continue
        n = q["numero"]
        q["pagina"] = pagina_de.get(n)
        q["figura"] = n in figura_em or bool(PALAVRAS_FIGURA.search(q["enunciado"]))
        q["caixas"] = caixas.get(n, [])
    return qs


def recortar_figuras(pdf_path, caixas, resolucao=150, margem=4):
    """PNG (bytes) de cada figura detectada; ignora as minúsculas (logotipos, ícones)."""
    import io
    out = []
    with pdfplumber.open(pdf_path) as p:
        for c in caixas:
            if not c.get("bbox"):
                continue
            pg = p.pages[c["pagina"] - 1]
            x0, t, x1, b = c["bbox"]
            if (x1 - x0) * (b - t) < 4000:
                continue
            area = (max(0, x0 - margem), max(0, t - margem), min(pg.width, x1 + margem), min(pg.height, b + margem))
            img = pg.crop(area).to_image(resolution=resolucao).original
            buf = io.BytesIO(); img.save(buf, format="PNG", optimize=True)
            out.append(buf.getvalue())
    return out


if __name__ == "__main__":
    import json
    qs = extrair_questoes(sys.argv[1], int(sys.argv[2]))
    print(json.dumps([{k: q.get(k) for k in ("numero", "disciplina", "pagina", "figura", "aviso", "erro")} for q in qs], ensure_ascii=False))
