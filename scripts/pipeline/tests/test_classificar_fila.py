"""Fila sem concurso-alvo (FGV etc.) abaixo da trava do classificador local: o Gemini da lista LEVE dá o assunto e a questão é
publicada sem comentário; e a ordem do job rápido. Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import importlib, json, os, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_figuras_anuladas import Banco, BaseRodar  # noqa: E402


def q(i, **extra):
    return {"id": f"fgv-x-q{i:02d}", "banca": "FGV", "ano": 2024, "orgao": "TJ", "cargo": "Analista", "numero": i, "disciplina_prova": "Português",
            "texto_base": None, "enunciado": "Assinale a alternativa correta quanto à concordância.", "alternativas": dict(zip("ABCDE", "12345")),
            "gabarito": "B", "avisos": [], "anulada": False, "pode_depender_de_imagem": False, "concurso_alvo": None, **extra}


ASSUNTOS = [{"id": "por", "pai_id": None, "nome": "Português", "nivel": 0},
            {"id": "por.conc", "pai_id": "por", "nome": "Concordância", "nivel": 1},
            {"id": "por.crase", "pai_id": "por", "nome": "Crase", "nivel": 1}]


class Upserts(Banco):
    def __init__(self, linhas=None):
        super().__init__(linhas); self.upserts = []

    def upsert(self, tabela, linhas, **k):
        self.upserts.append((tabela, linhas))


class TestClassificarFila(BaseRodar):
    def rodar_fila(self, fila, resposta, disponivel=True):
        banco = Upserts({"ingestao_questoes": fila, "assuntos": ASSUNTOS})
        ia = self.rodar.ia
        with mock.patch.object(self.rodar, "supa", banco), \
             mock.patch.object(ia, "gemini_disponivel", return_value=disponivel), mock.patch.object(ia, "sem_tempo", return_value=False), \
             mock.patch.object(ia, "gemini", side_effect=resposta) as gem:
            self.rodar.classificar_fila()
        return banco, gem

    def test_assunto_do_gemini_e_publicada_sem_comentario(self):
        resp = lambda tarefa, corpo, **k: (json.dumps([{"id": "fgv-x-q01", "assunto": "por.conc"}, {"id": "fgv-x-q02", "assunto": "inventado"}]), "end_turn")
        banco, gem = self.rodar_fila([q(1), q(2)], resp)
        tarefa, corpo = gem.call_args.args
        self.assertEqual(tarefa, "classificar_fila")
        self.assertEqual(self.rodar.ia.classe_gemini(tarefa), "leve")
        self.assertIn("ASSUNTOS:", gem.call_args.kwargs["system"])  # mesmo prompt da etapa classificar
        self.assertIn("ID: fgv-x-q01", corpo)
        questoes = [l for t, ls in banco.upserts if t == "questoes" for l in ls]
        self.assertEqual(len(questoes), 1)  # q02 veio com assunto que não existe na árvore: fica na fila
        pub = questoes[0]
        self.assertEqual((pub["assunto_id"], pub["assunto_origem"], pub["revisao_status"]), ("por.conc", "gemini", "auto-sem-comentario"))
        self.assertIsNone(pub["explicacao"])
        self.assertEqual(banco.updates, [("ingestao_questoes", {"id": "eq.fgv-x-q01"}, banco.updates[0][2])])
        self.assertEqual(banco.updates[0][2]["status"], "aprovada")

    def test_lotes_de_25_e_teto_de_pedidos(self):
        fila = [q(i) for i in range(1, 61)]
        resp = lambda tarefa, corpo, **k: ("[]", "end_turn")
        _, gem = self.rodar_fila(fila, resp)
        self.assertEqual(gem.call_count, 3)  # 60 questões / 25 por pedido
        os.environ.pop("GEMINI_MAX_CLASSIFICAR_FILA", None)
        ia = importlib.reload(importlib.import_module("ia"))
        self.assertEqual(ia.teto_gemini("classificar_fila"), 40)
        self.assertIsNone(ia.teto_gemini("classificar"))  # a etapa classificar não herda o teto da fila

    def test_questao_com_problema_nao_vai_ao_gemini(self):
        fila = [q(1, alternativas={"A": "x", "B": "y"}), q(2, avisos=["IAs divergiram: x"])]
        _, gem = self.rodar_fila(fila, lambda *a, **k: ("[]", "end_turn"))
        gem.assert_not_called()

    def test_sem_gemini_pula_sem_ler_a_fila(self):
        banco, gem = self.rodar_fila([q(1)], lambda *a, **k: ("[]", "end_turn"), disponivel=False)
        gem.assert_not_called()
        self.assertEqual(banco.selects, [])

    def test_sem_cota_para_e_publica_o_que_ja_tem(self):
        chamadas = []

        def resp(tarefa, corpo, **k):
            chamadas.append(1)
            if len(chamadas) > 1:
                raise self.rodar.ia.GeminiSemCota("cota diária de todos os modelos leves esgotada; tenta amanhã")
            return json.dumps([{"id": "fgv-x-q01", "assunto": "por.crase"}]), "end_turn"
        banco, _ = self.rodar_fila([q(i) for i in range(1, 61)], resp)
        self.assertEqual(len(chamadas), 2)
        self.assertEqual([l["id"] for t, ls in banco.upserts if t == "questoes" for l in ls], ["fgv-x-q01"])


class TestOrdemRapido(BaseRodar):
    def test_classificar_fila_depois_do_local_e_exportar_no_fim(self):
        # 28/10: exportar voltou para o fim (nunca é pulado e tem reserva de tempo); antes ia antes de classificar/auditar
        r = self.rodar.SEQUENCIAS["rapido"]
        self.assertLess(r.index("classificar_local"), r.index("classificar_fila"))
        self.assertEqual(r[-1], "exportar")
        self.assertIn("classificar_fila", self.rodar.ETAPAS)


if __name__ == "__main__":
    unittest.main()
