"""Orçamento da cota grátis do Gemini por tarefa, custo gravado a cada etapa e ordem das etapas. Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import importlib, os, sys, types, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_ia_reserva import ENV, Base, gemini_ok  # noqa: E402

SEM_CLAUDE = {**ENV, "IA_DESLIGADA": "1", "GEMINI_MAX_CHAMADAS": "500"}  # teto geral folgado: quem limita é o da tarefa


class TestTetoPorTarefa(Base):
    def setUp(self):
        super().setUp()
        self.env2 = mock.patch.dict(os.environ, SEM_CLAUDE)
        self.env2.start()
        for v in ("GEMINI_MAX_ENRIQUECER", "GEMINI_MAX_GABARITO", "GEMINI_MAX_TRANSCREVER", "GEMINI_MAX_CLASSIFICAR"):
            os.environ.pop(v, None)

    def tearDown(self):
        self.env2.stop()
        super().tearDown()

    def gastar(self, tarefa, n):
        """Faz chamadas até o Gemini recusar (ou n). -> quantas passaram."""
        feitas = 0
        with mock.patch("requests.post", return_value=gemini_ok()):
            for _ in range(n):
                if not self.ia.gemini_disponivel(tarefa):
                    break
                self.ia.gemini(tarefa, "x")
                feitas += 1
        return feitas

    def test_enriquecer_padrao_60(self):
        self.assertEqual(self.gastar("enriquecer", 100), 60)
        self.assertFalse(self.ia.gemini_disponivel("enriquecer"))

    def test_gabarito_padrao_30(self):
        self.assertEqual(self.gastar("gabarito", 100), 30)

    def test_transcrever_padrao_15_conta_pelo_prefixo(self):
        # o robô chama "transcrever caderno (AOCP)", "transcrever caderno (FGV)"...: tudo conta como "transcrever"
        self.assertEqual(self.gastar("transcrever caderno (AOCP)", 6) + self.gastar("transcrever caderno (FGV)", 100), 15)
        self.assertFalse(self.ia.gemini_disponivel("transcrever"))

    def test_teto_geral_padrao_150(self):
        os.environ.pop("GEMINI_MAX_CHAMADAS")
        self.assertEqual(self.ia._env_int("GEMINI_MAX_CHAMADAS", self.ia.TETO_GERAL_PADRAO), 150)

    def test_teto_da_tarefa_pela_variavel(self):
        os.environ["GEMINI_MAX_ENRIQUECER"] = "3"
        os.environ["GEMINI_MAX_TRANSCREVER"] = "0"
        self.assertEqual(self.gastar("enriquecer", 50), 3)
        self.assertFalse(self.ia.gemini_disponivel("transcrever caderno (AOCP)"))

    def test_transcrever_esgotado_nao_tira_a_cota_do_enriquecer(self):
        self.assertEqual(self.gastar("transcrever caderno (AOCP)", 100), 15)
        self.assertTrue(self.ia.gemini_disponivel("enriquecer"))
        self.assertEqual(self.gastar("enriquecer", 100), 60)

    def test_teto_geral_vale_acima_dos_tetos_por_tarefa(self):
        os.environ["GEMINI_MAX_CHAMADAS"] = "20"
        self.assertEqual(self.gastar("transcrever caderno (AOCP)", 100), 15)
        self.assertEqual(self.gastar("enriquecer", 100), 5)  # sobrou só 5 do geral
        self.assertFalse(self.ia.gemini_disponivel())

    def test_tarefa_sem_teto_proprio_so_respeita_o_geral(self):
        os.environ["GEMINI_MAX_CHAMADAS"] = "7"
        self.assertIsNone(self.ia.teto_gemini("classificar"))
        self.assertEqual(self.gastar("classificar", 50), 7)

    def test_tarefa_com_dado_que_nao_e_publico_nunca_vai_ao_gemini(self):
        self.assertFalse(self.ia.gemini_disponivel("plano"))
        self.assertIsNone(self.ia.tarefa_base("gerar"))

    def test_lote_gemini_para_no_teto_da_tarefa(self):
        os.environ["GEMINI_MAX_ENRIQUECER"] = "4"
        with mock.patch("requests.post", return_value=gemini_ok("r")):
            res = self.ia.em_lote("enriquecer", [(f"q{i}", "texto") for i in range(9)])
        self.assertEqual(len(res), 4)


class TestCustoPorEtapa(Base):
    def test_grava_zera_e_continua_contando_no_teto_mensal(self):
        gravado = []
        self.ia.registrar("classificar", "claude-haiku", {"input_tokens": 1_000_000, "output_tokens": 0})
        custo = self.ia.gasto_mes()
        with mock.patch.dict(sys.modules, {"supa": types.SimpleNamespace(upsert=lambda t, linhas: gravado.extend(linhas))}):
            self.ia.resumo("tudo › classificar")
        self.assertEqual(len(gravado), 1)
        self.assertEqual(gravado[0]["rodada"], "tudo › classificar")
        self.assertEqual(self.ia._uso, {})  # acumulado zerado depois de gravar
        self.assertAlmostEqual(self.ia.gasto_mes(), custo)  # o teto mensal não "esquece" o que foi gravado

    def test_falha_ao_gravar_mantem_o_custo_para_a_proxima_etapa(self):
        self.ia.registrar("auditar", "claude-haiku", {"input_tokens": 1000, "output_tokens": 10})

        def falha(*a):
            raise RuntimeError("supabase fora")

        with mock.patch.dict(sys.modules, {"supa": types.SimpleNamespace(upsert=falha)}):
            self.ia.resumo("tudo › auditar")
        self.assertEqual(len(self.ia._uso), 1)
        gravado = []
        with mock.patch.dict(sys.modules, {"supa": types.SimpleNamespace(upsert=lambda t, linhas: gravado.extend(linhas))}):
            self.ia.resumo("tudo › exportar")
        self.assertEqual([l["tarefa"] for l in gravado], ["auditar"])


class TestOrdemDasEtapas(unittest.TestCase):
    """rodar.py importa os módulos do robô (bs4, pdfplumber...): o teste roda onde o requirements.txt está instalado."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.rodar = importlib.import_module("rodar")
        except ImportError as e:  # máquina sem as dependências do robô
            raise unittest.SkipTest(f"rodar.py não importa aqui ({e})")

    def setUp(self):
        # no GitHub Actions os testes rodam com os segredos reais: nada daqui pode gravar no banco de verdade
        self.sem_banco = mock.patch.object(self.rodar, "supa", mock.Mock())
        self.sem_banco.start()

    def tearDown(self):
        self.sem_banco.stop()

    def test_tudo_enriquece_e_publica_antes_de_tudo(self):
        seq = self.rodar.SEQUENCIAS["tudo"]
        self.assertEqual(seq[:4], ["enriquecer", "publicar", "figuras", "publicar"])
        for depois in ("vigiar", "classificar_local", "processar"):
            self.assertLess(seq.index("publicar"), seq.index(depois))

    def test_jobs_cobrem_o_tudo_sem_repetir(self):
        r, p = self.rodar.SEQUENCIAS["rapido"], self.rodar.SEQUENCIAS["pesado"]
        self.assertEqual(sorted(r + p), sorted(self.rodar.SEQUENCIAS["tudo"]))
        self.assertFalse(set(r) & set(p))
        self.assertEqual(r[:4], ["enriquecer", "publicar", "figuras", "publicar"])  # job rápido: o que vale mais primeiro
        self.assertEqual(p[0], "vigiar")  # vigiar foi para o job pesado, antes do processar
        self.assertNotIn("vigiar", r)
        self.assertNotIn("processar", r)

    def test_custo_gravado_ao_fim_de_cada_etapa_mesmo_com_erro(self):
        rotulos = []

        def quebra():
            raise RuntimeError("caiu")

        with mock.patch.object(self.rodar.ia, "resumo", side_effect=rotulos.append), \
             mock.patch.dict(self.rodar.ETAPAS, {"vigiar": lambda: None, "exportar": quebra}):
            self.rodar.rodar_etapa("vigiar", "teste")
            with self.assertRaises(RuntimeError):
                self.rodar.rodar_etapa("exportar", "teste")
        self.assertEqual(rotulos, ["teste › vigiar", "teste › exportar"])

    def test_etapa_pulada_quando_o_prazo_do_job_acabou(self):
        chamou = []
        with mock.patch.object(self.rodar, "job_sem_tempo", return_value=True), \
             mock.patch.dict(self.rodar.ETAPAS, {"auditar": lambda: chamou.append(1)}):
            self.rodar.rodar_etapa("auditar", "teste")
        self.assertEqual(chamou, [])


if __name__ == "__main__":
    unittest.main()
