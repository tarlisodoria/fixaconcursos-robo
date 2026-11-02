"""Testes da IA do robô: novas tentativas, troca Claude -> Gemini e limites. Sem rede: requests.post é simulado.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import base64, importlib, json, os, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENV = {"ANTHROPIC_API_KEY": "teste", "ANTHROPIC_MODEL": "claude-teste", "GEMINI_API_KEY": "teste-g", "GEMINI_MODEL": "gemini-teste", "GEMINI_MODELOS_PESADO": "gemini-teste", "GEMINI_MODELOS_LEVE": "gemini-teste",
       "GEMINI_MAX_CHAMADAS": "10", "IA_DESLIGADA": "", "GEMINI_DESLIGADO": ""}


class Resp:
    def __init__(self, status, corpo=None, headers=None, linhas=None):
        self.status_code, self._corpo, self.headers, self._linhas = status, corpo or {}, headers or {}, linhas or []
        self.text = json.dumps(self._corpo)

    def json(self):
        return self._corpo

    def iter_lines(self, decode_unicode=True):
        return iter(self._linhas)


def claude_ok(txt="ok-claude"):
    return Resp(200, {"model": "claude-teste", "content": [{"type": "text", "text": txt}], "stop_reason": "end_turn",
                      "usage": {"input_tokens": 10, "output_tokens": 5}})


def gemini_ok(txt="ok-gemini"):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": txt}]}, "finishReason": "STOP"}],
                      "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})


SEM_CREDITO = Resp(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}})


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        import ia
        self.ia = importlib.reload(ia)
        self.ia._gasto_mes = 0.0  # não consulta o banco
        self.sono = mock.patch("time.sleep")
        self.sono.start()

    def tearDown(self):
        self.sono.stop()
        self.env.stop()

    def urls(self, post):
        return ["googleapis" in c.args[0] for c in post.call_args_list]


class TestChamar(Base):
    def test_429_depois_sucesso_fica_no_claude(self):
        with mock.patch("requests.post", side_effect=[Resp(429, headers={"retry-after": "1"}), Resp(503), claude_ok()]) as post:
            txt, _ = self.ia.chamar("classificar", "x")
        self.assertEqual(txt, "ok-claude")
        self.assertEqual(self.urls(post), [False, False, False])

    def test_falha_de_rede_tenta_de_novo(self):
        import requests
        with mock.patch("requests.post", side_effect=[requests.Timeout("lento"), claude_ok()]):
            txt, _ = self.ia.chamar("classificar", "x")
        self.assertEqual(txt, "ok-claude")

    def test_sem_credito_troca_para_gemini(self):
        with mock.patch("requests.post", side_effect=[SEM_CREDITO, gemini_ok()]) as post:
            txt, _ = self.ia.chamar("classificar", "x")
        self.assertEqual(txt, "ok-gemini")
        self.assertEqual(self.urls(post), [False, True])
        self.assertFalse(self.ia.anthropic_ok())  # desligado pelo resto da rodada
        with mock.patch("requests.post", side_effect=[gemini_ok("2")]) as post:  # próxima chamada já vai direto
            self.assertEqual(self.ia.chamar("auditar", "x")[0], "2")
        self.assertEqual(self.urls(post), [True])

    def test_claude_esgota_tentativas_e_troca(self):
        with mock.patch("requests.post", side_effect=[Resp(529)] * 3 + [gemini_ok()]) as post:
            txt, _ = self.ia.chamar("classificar", "x", tentativas=3)
        self.assertEqual(txt, "ok-gemini")
        self.assertEqual(self.urls(post), [False, False, False, True])

    def test_tarefa_com_conteudo_nao_publico_nunca_vai_ao_gemini(self):
        with mock.patch("requests.post", side_effect=[SEM_CREDITO]) as post:
            with self.assertRaises(RuntimeError):
                self.ia.chamar("responder_aluno", "x")
        self.assertEqual(self.urls(post), [False])

    def test_erro_400_comum_nao_repete_nem_troca(self):
        with mock.patch("requests.post", side_effect=[Resp(400, {"error": {"message": "prompt inválido"}})]) as post:
            with self.assertRaises(RuntimeError):
                self.ia.chamar("classificar", "x")
        self.assertEqual(post.call_count, 1)


class TestGemini(Base):
    def setUp(self):
        super().setUp()
        os.environ["IA_DESLIGADA"] = "1"  # Claude fora: tudo cai no Gemini

    def test_429_e_500_repetem(self):
        with mock.patch("requests.post", side_effect=[Resp(429), Resp(500), gemini_ok()]) as post:
            self.assertEqual(self.ia.chamar("classificar", "x")[0], "ok-gemini")
        self.assertEqual(post.call_count, 3)

    def test_cota_do_dia_esgotada_para_de_tentar(self):
        dia = Resp(429, {"error": {"message": "Quota exceeded for GenerateRequestsPerDay"}})
        with mock.patch("requests.post", side_effect=[dia]) as post:
            with self.assertRaises(RuntimeError):
                self.ia.chamar("classificar", "x")
        self.assertEqual(post.call_count, 1)
        self.assertFalse(self.ia.gemini_disponivel("classificar"))

    def test_chave_vai_no_cabecalho_e_nao_na_url(self):
        with mock.patch("requests.post", side_effect=[gemini_ok()]) as post:
            self.ia.chamar("classificar", "x")
        url, kw = post.call_args.args[0], post.call_args.kwargs
        self.assertNotIn("teste-g", url)
        self.assertEqual(kw["headers"]["x-goog-api-key"], "teste-g")

    def test_custo_zero_e_modelo_registrado(self):
        with mock.patch("requests.post", side_effect=[gemini_ok()]):
            self.ia.chamar("classificar", "x")
        (tarefa, mod, _), c = next(iter(self.ia._uso.items()))
        self.assertEqual(mod, "gemini:gemini-teste")
        self.assertEqual(c["custo_usd"], 0.0)

    def test_lote_respeita_teto_de_chamadas(self):
        os.environ["GEMINI_MAX_CHAMADAS"] = "2"
        pedidos = [(f"p{i}", "x") for i in range(5)]
        with mock.patch("requests.post", side_effect=[gemini_ok(str(i)) for i in range(5)]) as post:
            res = self.ia.em_lote("classificar", pedidos)
        self.assertEqual(len(res), 2)
        self.assertEqual(post.call_count, 2)

    def test_sem_reserva_disponivel_para_tarefa_privada(self):
        self.assertFalse(self.ia.disponivel("responder_aluno"))
        self.assertTrue(self.ia.disponivel("enriquecer"))  # só texto público da questão + tópicos
        self.assertTrue(self.ia.disponivel("classificar"))


class TestTranscricao(Base):
    def setUp(self):
        super().setUp()
        os.environ["IA_DESLIGADA"] = "1"
        import ia_extrair
        self.ie = importlib.reload(ia_extrair)
        self.pdf = b"%PDF-1.4 teste"

    def resposta(self, n):
        qs = [{"numero": i, "disciplina": "TI", "enunciado": f"Q{i}", "alternativas": {k: k for k in "ABCDE"}} for i in range(1, n + 1)]
        return gemini_ok(json.dumps({"cargo": "Técnico", "textos": [], "questoes": qs}))

    def test_gemini_transcreve_completo(self):
        with mock.patch("requests.post", side_effect=[self.resposta(10)]) as post:
            cargo, qs, parada = self.ie.extrair_por_ia(self.pdf, 10, "FGV")
        self.assertEqual((cargo, len(qs), parada), ("Técnico", 10, "end_turn"))
        partes = post.call_args.kwargs["json"]["contents"][0]["parts"]
        self.assertEqual(partes[0]["inline_data"]["mime_type"], "application/pdf")
        self.assertEqual(base64.b64decode(partes[0]["inline_data"]["data"]), self.pdf)

    def test_gemini_incompleto_volta_para_aguardando(self):
        with mock.patch("requests.post", side_effect=[self.resposta(7)]):
            with self.assertRaises(self.ie.SemIA):
                self.ie.extrair_por_ia(self.pdf, 10, "FGV")

    def test_sem_nenhum_provedor_guarda_para_depois(self):
        os.environ["GEMINI_DESLIGADO"] = "1"
        with mock.patch("requests.post") as post:
            with self.assertRaises(self.ie.SemIA):
                self.ie.extrair_por_ia(self.pdf, 10, "FGV")
        post.assert_not_called()

    def test_claude_sem_credito_no_caderno_troca_para_gemini(self):
        os.environ["IA_DESLIGADA"] = ""
        with mock.patch("requests.post", side_effect=[SEM_CREDITO, self.resposta(10)]) as post:
            _, qs, _ = self.ie.extrair_por_ia(self.pdf, 10, "FGV")
        self.assertEqual(len(qs), 10)
        self.assertEqual(self.urls(post), [False, True])


if __name__ == "__main__":
    unittest.main()
