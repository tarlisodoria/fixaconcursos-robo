"""27/10: GitHub Models devolveu HTTP 200 text/plain "OK" (POST virou GET num redirecionamento); system em lista quebrou o Gemini
(400 "Proto field is not repeating"); ia_erros não gravava com amostras. Sem rede e sem banco."""
import importlib, json, os, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ENV = {"GITHUB_TOKEN": "tok", "CONFERENTE_GITHUB": "1", "CONFERENTE_DESLIGADO": "", "CONFERENTE_MODEL": "", "IA_DESLIGADA": "1", "GEMINI_API_KEY": "g",
       "GEMINI_MODEL": "gemini-teste", "GEMINI_MODELOS_LEVE": "gemini-teste-lite", "GEMINI_MODELOS_PESADO": "gemini-teste",
       "GEMINI_DESLIGADO": "", "SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"}


class Resp:
    def __init__(self, status, corpo=None, headers=None, texto=None):
        self.status_code, self._c, self.headers = status, corpo, headers or {}
        self.text = texto if texto is not None else json.dumps(corpo or {})

    def json(self):
        if self._c is None:
            raise ValueError("sem json")
        return self._c


OK = Resp(200, {"choices": [{"message": {"content": "[]"}}], "usage": {}})


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start()
        import ia
        self.ia = importlib.reload(ia)
        self.ia._gasto_mes = 0.0
        self.sono = mock.patch("time.sleep"); self.sono.start()

    def tearDown(self):
        self.sono.stop(); self.env.stop()


class Teste(Base):
    def test_nao_segue_redirect_e_registra_endereco(self):
        os.environ["GEMINI_DESLIGADO"] = "1"  # só o GitHub Models aqui
        with mock.patch("requests.post", side_effect=[Resp(302, None, {"location": "https://novo/x"}, "")]) as post:
            with self.assertRaises(RuntimeError):
                self.ia.conferir("t", "oi")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertIn("https://novo/x", next(iter(self.ia._amostras.values())))

    def test_307_repete_o_post_no_novo_endereco(self):
        with mock.patch("requests.post", side_effect=[Resp(307, None, {"location": "https://novo/x"}, ""), OK]) as post:
            self.assertEqual(self.ia.conferir("t", "oi"), "[]")
        self.assertEqual(post.call_args_list[1].args[0], "https://novo/x")

    def test_system_em_lista_vira_texto_no_gemini(self):
        ok = Resp(200, {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "STOP"}], "usageMetadata": {}})
        with mock.patch("requests.post", side_effect=[ok]) as post:
            self.ia.chamar("classificar", "oi", system=[{"type": "text", "text": "regras", "cache_control": {"type": "ephemeral"}}])
        si = post.call_args.kwargs["json"]["systemInstruction"]
        self.assertEqual(si, {"parts": [{"text": "regras"}]})

    def test_ia_erros_grava_com_amostras(self):
        self.ia.falha("conferente", "resposta-nao-json", "HTTP 200 OK")
        gravado = []
        import supa
        with mock.patch.object(supa, "upsert", side_effect=lambda t, linhas, **k: gravado.append((t, linhas))):
            self.ia.resumo("teste")
        erros = [l for t, ls in gravado for l in ls if l.get("etapa") == "ia_erros"]
        self.assertEqual(erros[0]["itens"], 1)
        self.assertIn("amostras", json.loads(erros[0]["detalhe"]))


if __name__ == "__main__":
    unittest.main()


class TesteConferenteGoogle(Base):
    """27/10: o GitHub Models devolve HTTP 200 'OK' sem JSON no Actions. A conferência cai para um modelo do Google
    diferente do que escreveu (Gemma 4 31B primeiro)."""
    GEM = Resp(200, {"candidates": [{"content": {"parts": [{"text": '[{"id": "q1", "ok": true}]'}]}, "finishReason": "STOP"}], "usageMetadata": {}})

    def test_github_falha_e_gemma_confere(self):
        texto_ok = Resp(200, None, {"content-type": "text/plain"}, "OK\r\n")
        with mock.patch("requests.post", side_effect=[texto_ok, texto_ok, self.GEM]) as post:
            txt = self.ia.conferir("t", "confira", escritor="gemini-3.5-flash-lite")
        self.assertIn('"ok": true', txt)
        self.assertIn("gemma-4-31b-it", post.call_args.args[0])

    def test_padrao_sem_github_vai_direto_ao_google(self):
        # 28/10: GitHub Models fora da cadeia por padrão (respondia "OK" em toda rodada); CONFERENTE_GITHUB=1 religa
        os.environ.pop("CONFERENTE_GITHUB")
        with mock.patch("requests.post", side_effect=[self.GEM]) as post:
            self.ia.conferir("t", "confira", escritor="gemini-3.5-flash-lite")
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("models.github.ai", post.call_args.args[0])
        self.assertIn("gemma-4-31b-it", post.call_args.args[0])

    def test_conferente_nunca_e_o_escritor(self):
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["GEMINI_MODELOS_CONFERENCIA"] = "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
        with mock.patch("requests.post", side_effect=[self.GEM]) as post:
            self.ia.conferir("t", "confira", escritor="gemini-3.5-flash-lite")
        self.assertIn("gemini-3.1-flash-lite", post.call_args.args[0])

    def test_so_o_escritor_na_lista_nao_confere(self):
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["GEMINI_MODELOS_CONFERENCIA"] = "gemini-3.5-flash-lite"
        with mock.patch("requests.post") as post:
            with self.assertRaises(RuntimeError):
                self.ia.conferir("t", "confira", escritor="gemini-3.5-flash-lite")
        post.assert_not_called()

    def test_gemma_respeita_limite_de_tokens_por_minuto(self):
        self.assertEqual(self.ia.intervalo_rpm("gemma-4-31b-it"), 16.0)
