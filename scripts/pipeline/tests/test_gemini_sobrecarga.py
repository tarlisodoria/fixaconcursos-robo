"""Gemini com várias listas de modelos e cota por modelo: troca de modelo (cota do dia, 503, 404), espaçamento de RPM,
nova tentativa sem gastar teto, descoberta dos modelos da conta, mensagens com a causa real e registro de falhas/pedidos.
Também: classificar_local pula sem alvos novos e o tempo de cada etapa é registrado. Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import importlib, json, os, sys, types, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_ia_reserva import ENV, Base, Resp, gemini_ok  # noqa: E402

FLASH = ["gemini-x-flash", "gemini-y-flash"]
LITE = ["gemini-x-flash-lite", "gemini-y-flash-lite"]
SEM_CLAUDE = {**ENV, "IA_DESLIGADA": "1", "GEMINI_MAX_CHAMADAS": "150", "GEMINI_MODEL": "",
              "GEMINI_MODELOS_PESADO": ",".join(FLASH), "GEMINI_MODELOS_LEVE": ",".join(LITE)}
R503 = Resp(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "The model is overloaded"}})
R429_DIA = Resp(429, {"error": {"code": 429, "message": "Quota exceeded for metric GenerateRequestsPerDay"}})
R404 = Resp(404, {"error": {"code": 404, "message": "models/x is not found"}})
MSG_503 = "Gemini sobrecarregado (503): tenta na próxima rodada"


class Gemini(Base):
    def setUp(self):
        super().setUp()
        self.env2 = mock.patch.dict(os.environ, SEM_CLAUDE)
        self.env2.start()

    def tearDown(self):
        self.env2.stop()
        super().tearDown()

    def modelos(self, post):
        return [c.args[0].split("/models/")[1].split(":")[0] for c in post.call_args_list]


class TestTrocaDeModelo(Gemini):
    def test_cota_do_dia_esgotada_passa_ao_proximo_e_so_aquele_sai(self):
        with mock.patch("requests.post", side_effect=[R429_DIA, gemini_ok("do-y"), gemini_ok("de-novo-y")]) as post:
            self.assertEqual(self.ia.gemini("enriquecer", "x")[0], "do-y")
            self.ia.gemini("classificar", "x")  # mesma lista leve: não volta ao modelo sem cota
        self.assertEqual(self.modelos(post), [LITE[0], LITE[1], LITE[1]])
        self.assertEqual(self.ia._modelo_fora, {LITE[0]: "cota"})
        self.assertEqual(self.ia.modelos_gemini("transcrever"), FLASH)  # a lista pesada não é afetada

    def test_cota_diaria_de_todos_da_lista_tem_mensagem_propria(self):
        with mock.patch("requests.post", side_effect=[R429_DIA, R429_DIA]):
            with self.assertRaises(self.ia.GeminiSemCota) as ctx:
                self.ia.gemini("gabarito", "x")
        self.assertEqual(str(ctx.exception), "cota diária de todos os modelos leves esgotada; tenta amanhã")

    def test_503_em_3_tentativas_passa_ao_proximo_modelo(self):
        with mock.patch("requests.post", side_effect=[R503, R503, R503, gemini_ok("do-y")]) as post:
            self.assertEqual(self.ia.gemini("enriquecer", "x")[0], "do-y")
        self.assertEqual(self.modelos(post), [LITE[0]] * 3 + [LITE[1]])
        self.assertEqual(self.ia._modelo_fora[LITE[0]], "sobrecarga")
        self.assertIn(("enriquecer", f"gemini:{LITE[1]}", False), self.ia._uso)  # custo no modelo que respondeu

    def test_503_em_todos_os_modelos_grava_a_frase_certa_e_sai_da_rodada(self):
        with mock.patch("requests.post", side_effect=[R503] * 6) as post:
            with self.assertRaises(self.ia.GeminiSobrecarregado) as ctx:
                self.ia.gemini("auditar", "x")
        self.assertEqual(post.call_count, 6)
        self.assertEqual(str(ctx.exception), MSG_503)
        self.assertFalse(self.ia.gemini_disponivel("enriquecer"))
        self.assertEqual(self.ia.motivo_indisponivel("enriquecer"), MSG_503)

    def test_modelo_inexistente_404_passa_ao_proximo(self):
        with mock.patch("requests.post", side_effect=[R404, gemini_ok()]) as post:
            self.ia.gemini("transcrever caderno (AOCP)", "x")
        self.assertEqual(self.modelos(post), FLASH)
        self.assertEqual(self.ia._modelo_fora[FLASH[0]], "inexistente")

    def test_gemini_model_antigo_entra_primeiro_na_lista_pesada(self):
        os.environ["GEMINI_MODEL"] = "gemini-principal"
        self.assertEqual(self.ia.modelos_configurados("pesado"), ["gemini-principal"] + FLASH)
        self.assertEqual(self.ia.modelos_configurados("leve"), LITE)


class TestNovaTentativaNaoGastaTeto(Gemini):
    def test_503_503_depois_ok_conta_uma_chamada(self):
        with mock.patch("requests.post", side_effect=[R503, R503, gemini_ok()]):
            self.ia.gemini("enriquecer", "x")
        self.assertEqual(self.ia._gemini_chamadas, 1)
        self.assertEqual(self.ia._gemini_por_tarefa["enriquecer"], 1)
        self.assertEqual(self.ia._pedidos_modelo[LITE[0]], 3)          # pedidos enviados: os 3 (para o "uso por modelo")
        self.assertEqual(self.ia._falhas[(LITE[0], "503")], 2)         # falhas registradas mesmo resolvidas

    def test_teto_1_aguenta_varias_tentativas_e_troca_de_modelo(self):
        os.environ["GEMINI_MAX_TRANSCREVER"] = "1"
        with mock.patch("requests.post", side_effect=[R503, R503, R503, R503, gemini_ok()]):
            self.ia.gemini("transcrever caderno (AOCP)", "x")
        self.assertFalse(self.ia.gemini_disponivel("transcrever"))  # só agora: 1 chamada terminada

    def test_sobrecarga_total_nao_conta_no_teto(self):
        with mock.patch("requests.post", side_effect=[R503] * 6):
            with self.assertRaises(self.ia.GeminiSobrecarregado):
                self.ia.gemini("gabarito", "x")
        self.assertEqual(self.ia._gemini_chamadas, 0)


class TestEsperas(Gemini):
    def test_espera_30_e_60_entre_as_3_tentativas(self):
        with mock.patch("requests.post", side_effect=[R503, R503, R503, gemini_ok()]), mock.patch("time.sleep") as sono:
            self.ia.gemini("enriquecer", "x")
        longas = [c.args[0] for c in sono.call_args_list if c.args[0] > 20]  # as curtas são o espaçamento de RPM
        self.assertEqual(len(longas), 2)
        for base, s in zip((30, 60), longas):
            self.assertTrue(base * 0.75 <= s <= base * 1.25, (base, s))

    def test_rpm_espaca_pedidos_ao_mesmo_modelo(self):
        relogio = [1000.0]
        with mock.patch("time.time", side_effect=lambda: relogio[0]), mock.patch("time.sleep") as sono, \
             mock.patch("requests.post", side_effect=[gemini_ok(), gemini_ok(), gemini_ok(), gemini_ok()]):
            self.ia.gemini("enriquecer", "x")          # 1º pedido ao Lite: sem espera
            self.ia.gemini("enriquecer", "x")          # mesmo instante: espera 4,5 s (15 RPM)
            self.ia.gemini("transcrever caderno", "x")  # Flash: 1º pedido a ele, sem espera
            self.ia.gemini("transcrever caderno", "x")  # espera 13 s (5 RPM)
        self.assertEqual([c.args[0] for c in sono.call_args_list], [4.5, 13.0])

    def test_nao_comeca_espera_que_passaria_do_prazo_do_job(self):
        import time
        self.ia.PRAZO_FINAL = time.time() + 20  # menos que a 1ª espera de sobrecarga (≥ 22,5 s)
        with mock.patch("requests.post", side_effect=[R503] * 6) as post:
            with self.assertRaises(self.ia.GeminiSobrecarregado):
                self.ia.gemini("enriquecer", "x")
        self.assertEqual(post.call_count, 2)  # 1 pedido por modelo, sem as esperas longas


class TestDescobrirModelos(Gemini):
    def lista(self, *ids):
        return Resp(200, {"models": [{"name": f"models/{i}"} for i in ids]})

    def test_descarta_da_lista_os_que_nao_existem_na_conta(self):
        with mock.patch("requests.get", return_value=self.lista(LITE[1], FLASH[0], "gemma-teste", "text-embedding")) as get, \
             mock.patch("builtins.print") as log:
            self.ia.descobrir_modelos()
        self.assertEqual(get.call_args.kwargs["headers"]["x-goog-api-key"], "teste-g")  # chave no cabeçalho
        self.assertNotIn("teste-g", get.call_args.args[0])
        self.assertEqual(self.ia.modelos_gemini("enriquecer"), [LITE[1]])
        self.assertEqual(self.ia.modelos_gemini("transcrever"), [FLASH[0]])
        avisos = " ".join(str(c.args[0]) for c in log.call_args_list)
        self.assertIn(f"descartados: {LITE[0]}", avisos)
        self.assertIn(f"descartados: {FLASH[1]}", avisos)

    def test_nenhum_modelo_da_lista_existe_explica(self):
        with mock.patch("requests.get", return_value=self.lista("outro-modelo")), mock.patch("builtins.print"):
            self.ia.descobrir_modelos()
        self.assertFalse(self.ia.gemini_disponivel("enriquecer"))
        self.assertIn("nenhum modelo leve da lista existe", self.ia.motivo_indisponivel("enriquecer"))

    def test_falha_na_listagem_mantem_as_listas(self):
        with mock.patch("requests.get", return_value=Resp(500)), mock.patch("builtins.print"):
            self.ia.descobrir_modelos()
        self.assertEqual(self.ia.modelos_gemini("enriquecer"), LITE)


class TestMensagemGravada(Gemini):
    def extrair(self):
        import ia_extrair
        importlib.reload(ia_extrair)
        with self.assertRaises(ia_extrair.SemIA) as ctx:
            ia_extrair.extrair_por_ia(b"%PDF-1.4 teste", 10, banca="AOCP")
        return str(ctx.exception)

    def test_caderno_com_gemini_sobrecarregado(self):
        with mock.patch("requests.post", side_effect=[R503] * 6):
            self.assertEqual(self.extrair(), MSG_503)

    def test_caderno_sem_cota_em_todos_os_pesados(self):
        with mock.patch("requests.post", side_effect=[R429_DIA, R429_DIA]):
            self.assertEqual(self.extrair(), "cota diária de todos os modelos pesados esgotada; tenta amanhã")

    def test_teto_esgotado_explica_o_motivo_sem_ia_desligada_generico(self):
        os.environ["GEMINI_MAX_TRANSCREVER"] = "0"
        msg = self.extrair()
        self.assertIn("teto de 'transcrever'", msg)
        self.assertNotIn("IA desligada", msg)


class TestRegistros(Gemini):
    def test_resumo_grava_falhas_por_modelo_e_pedidos_por_modelo_e_zera(self):
        gravado = []
        sup = types.SimpleNamespace(upsert=lambda tabela, linhas: gravado.append((tabela, linhas)))
        with mock.patch("requests.post", side_effect=[R503, gemini_ok()]):
            self.ia.gemini("enriquecer", "x")
        self.ia.falha("conferente", 403)
        with mock.patch.dict(sys.modules, {"supa": sup}):
            self.ia.resumo("rapido › enriquecer")
        por_etapa = {linhas[0]["etapa"]: json.loads(linhas[0]["detalhe"]) for tabela, linhas in gravado if tabela == "robo_diagnostico"}
        self.assertEqual(por_etapa["ia_erros"], {f"{LITE[0]} 503": 1, "conferente 403": 1})
        self.assertEqual(por_etapa["ia_modelos"], {LITE[0]: 2})
        self.assertEqual(self.ia._falhas, {})
        self.assertEqual(self.ia._pedidos_modelo, {})


class TestRodar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.rodar = importlib.import_module("rodar")
        except ImportError as e:
            raise unittest.SkipTest(f"rodar.py não importa aqui ({e})")

    def test_deve_treinar(self):
        dt = self.rodar.deve_treinar
        self.assertEqual(dt(["q:1", "q:2"], None, 50), (True, 2))  # nunca treinou: treina
        self.assertEqual(dt(["q:1", "q:2", "i:3"], ["q:1", "q:2"], 50), (False, 1))
        self.assertEqual(dt([f"q:{i}" for i in range(60)], [], 50), (True, 60))
        self.assertEqual(dt(["q:1"], ["q:1", "q:9"], 50), (False, 0))  # sumiu alvo (já classificado): não é novo

    def test_classificar_local_pula_sem_alvos_novos(self):
        CL = importlib.import_module("classificar_local")
        anteriores = [f"q:{i}" for i in range(100)]
        sup = types.SimpleNamespace(select=lambda *a, **k: [{"detalhe": json.dumps(anteriores)}], upsert=mock.Mock(), update=mock.Mock())
        todas = lambda tabela, params: [{"id": str(i)} for i in range(100)] if tabela == "questoes" else []  # noqa: E731
        with mock.patch.object(self.rodar, "supa", sup), mock.patch.object(CL, "todas", side_effect=todas), \
             mock.patch.object(CL, "treinar_e_prever") as treino:
            self.rodar.classificar_local()
        treino.assert_not_called()
        linha = sup.upsert.call_args.args[1][0]
        self.assertEqual(linha["etapa"], "classificar_local")
        self.assertTrue(linha["titulo"].startswith("pulado: 0 alvo(s) novo(s)"))
        self.assertIn("segundos", json.loads(linha["detalhe"]))

    def test_tempo_de_cada_etapa_vai_para_robo_diagnostico(self):
        sup = types.SimpleNamespace(upsert=mock.Mock())
        self.rodar._tempos.clear()
        with mock.patch.object(self.rodar, "supa", sup), mock.patch.object(self.rodar.ia, "resumo"), \
             mock.patch.dict(self.rodar.ETAPAS, {"exportar": lambda: None}):
            self.rodar.rodar_etapa("exportar", "teste")
        linha = sup.upsert.call_args.args[1][0]
        self.assertEqual(linha["etapa"], "rodada")
        det = json.loads(linha["detalhe"])
        self.assertEqual(det["etapas"][0]["etapa"], "exportar")
        self.assertTrue(det["etapas"][0]["ok"])
        self.assertIn("segundos", det["etapas"][0])


if __name__ == "__main__":
    unittest.main()
