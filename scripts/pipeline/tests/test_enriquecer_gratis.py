"""Enriquecer sem Claude: o Gemini escreve tópico + comentário e o GitHub Models confere. Sem rede e sem banco (tudo simulado).

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import importlib, json, os, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENV = {"CONFERENTE_GITHUB": "1", "ANTHROPIC_API_KEY": "", "ANTHROPIC_MODEL": "", "GEMINI_API_KEY": "g", "GEMINI_MODEL": "gemini-teste", "GEMINI_MODELOS_PESADO": "gemini-teste", "GEMINI_MODELOS_LEVE": "gemini-teste", "GEMINI_MAX_CHAMADAS": "10",
       "IA_DESLIGADA": "1", "GEMINI_DESLIGADO": "", "GITHUB_TOKEN": "tok-gh", "CONFERENTE_MODEL": "", "CONFERENTE_DESLIGADO": "",
       "SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"}


class Resp:
    def __init__(self, status, corpo=None, headers=None):
        self.status_code, self._c, self.headers = status, corpo or {}, headers or {}
        self.text = json.dumps(self._c)

    def json(self):
        return self._c


def conf_ok(itens):
    return Resp(200, {"choices": [{"message": {"content": json.dumps(itens)}}], "usage": {"prompt_tokens": 9, "completion_tokens": 3}})


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start()
        import ia, rodar
        self.ia = importlib.reload(ia)
        self.ia._gasto_mes = 0.0
        self.rodar = importlib.reload(rodar)
        self.sono = mock.patch("time.sleep"); self.sono.start()
        self.updates = []
        q = {"id": "q1", "concurso_alvo": "trt8-2026", "texto_base": None, "enunciado": "Enunciado da questão um", "alternativas": {"C": "Certo", "E": "Errado"},
             "gabarito": "C", "avisos": []}
        self.sel = mock.patch.object(self.rodar.supa, "select", side_effect=lambda t, p=None: [q] if t == "ingestao_questoes"
                                     else [{"id": "T1", "disciplina": "Direito", "titulo": "Constituição"}])
        self.upd = mock.patch.object(self.rodar.supa, "update", side_effect=lambda t, f, m: self.updates.append(m))
        self.sel.start(); self.upd.start()
        self.gemini_txt = json.dumps([{"id": "q1", "topico": "T1", "explicacao": "**Gabarito: (C).** Porque sim, conforme a Constituição."}])

    def tearDown(self):
        for m in (self.sel, self.upd, self.sono, self.env):
            m.stop()


class TestConferente(Base):
    def test_token_vai_no_cabecalho(self):
        with mock.patch("requests.post", side_effect=[conf_ok([])]) as post:
            self.ia.conferir("x", "oi")
        self.assertEqual(post.call_args.args[0], self.ia.GITHUB_MODELS_API)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer tok-gh")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "openai/gpt-4.1-mini")

    def test_cota_do_dia_desliga(self):
        os.environ["GEMINI_DESLIGADO"] = "1"  # só o GitHub Models aqui
        with mock.patch("requests.post", side_effect=[Resp(429, {"error": {"message": "Rate limit UserByDay"}}, {"retry-after": "40000"})]):
            with self.assertRaises(RuntimeError):
                self.ia.conferir("x", "oi")
        self.assertFalse(self.ia.conferente_disponivel())

    def test_custo_zero(self):
        with mock.patch("requests.post", side_effect=[conf_ok([])]):
            self.ia.conferir("x", "oi")
        self.assertTrue(all(c["custo_usd"] == 0 for c in self.ia._uso.values()))


class TestAplicar(Base):
    def test_conferente_aprova_grava_com_marca(self):
        with mock.patch("requests.post", side_effect=[conf_ok([{"id": "q1", "ok": True, "motivo": ""}])]):
            n = self.rodar._aplicar_enriquecer(self.gemini_txt, ["q1"], {}, conferir_gratis=True)
        self.assertEqual(n, 1)
        self.assertEqual(self.updates[-1]["topico_sugerido"], "T1")
        self.assertEqual(self.updates[-1]["enriquecido_por"], "gemini+github")
        self.assertNotIn("avisos", self.updates[-1])

    def test_divergencia_vai_para_humano(self):
        with mock.patch("requests.post", side_effect=[conf_ok([{"id": "q1", "ok": False, "motivo": "comentário contradiz o gabarito", "tipo": "conteudo"}])]):
            self.rodar._aplicar_enriquecer(self.gemini_txt, ["q1"], {}, conferir_gratis=True)
        self.assertIn("IAs divergiram (conteúdo): comentário contradiz o gabarito", self.updates[-1]["avisos"])
        self.assertTrue(self.rodar.motivo_revisao({**self.updates[-1], "avisos": self.updates[-1]["avisos"]}))

    def test_sem_conferente_guarda_proposta_sem_publicar(self):
        os.environ["CONFERENTE_DESLIGADO"] = "1"
        with mock.patch("requests.post") as post:
            n = self.rodar._aplicar_enriquecer(self.gemini_txt, ["q1"], {}, conferir_gratis=True)
        self.assertEqual(n, 0)
        self.assertEqual(self.updates, [{"proposta_ia": {"topico": "T1", "explicacao": json.loads(self.gemini_txt)[0]["explicacao"], "modelo": None}}])
        post.assert_not_called()

    def test_conferente_sem_json_nao_derruba_e_guarda(self):
        # 27/10: GitHub Models devolveu 2xx com corpo que não era JSON; a etapa caiu e o trabalho do Gemini se perdeu
        vazio = Resp(200); vazio.text = ""
        vazio.json = lambda: json.loads("")
        with mock.patch("requests.post", side_effect=[vazio] * 4):
            n = self.rodar._aplicar_enriquecer(self.gemini_txt, ["q1"], {}, conferir_gratis=True)
        self.assertEqual(n, 0)
        self.assertIn("proposta_ia", self.updates[-1])
        self.assertNotIn("topico_sugerido", self.updates[-1])

    def test_proposta_guardada_so_confere_na_rodada_seguinte(self):
        guardada = {"id": "q1", "concurso_alvo": "trt8-2026", "texto_base": None, "enunciado": "Enunciado da questão um",
                    "alternativas": {"C": "Certo", "E": "Errado"}, "gabarito": "C", "avisos": [],
                    "proposta_ia": {"topico": "T1", "explicacao": "**Gabarito: (C).** Porque sim."}}
        self.sel.stop()
        sel = mock.patch.object(self.rodar.supa, "select", side_effect=lambda t, p=None: [guardada] if t == "ingestao_questoes"
                                else [{"id": "T1", "disciplina": "Direito", "titulo": "Constituição"}])
        sel.start(); self.sel = sel
        with mock.patch("requests.post", side_effect=[conf_ok([{"id": "q1", "ok": True, "motivo": ""}])]) as post:
            n = self.rodar._conferir_guardadas({})
        self.assertEqual(n, 1)
        self.assertEqual(post.call_count, 1)  # só o conferente; o Gemini não é chamado de novo
        self.assertEqual(self.updates[-1]["topico_sugerido"], "T1")
        self.assertIsNone(self.updates[-1]["proposta_ia"])

    def test_fora_leva_lista_de_topicos_ao_conferente(self):
        # 27/10: sem a lista, o conferente reprovava quase todo "FORA" (61 de 64 divergências)
        txt = json.dumps([{"id": "q1", "topico": "FORA", "explicacao": "**Gabarito: (C).** Assunto de outro cargo."}])
        with mock.patch("requests.post", side_effect=[conf_ok([{"id": "q1", "ok": True, "motivo": ""}])]) as post:
            self.rodar._aplicar_enriquecer(txt, ["q1"], {}, conferir_gratis=True)
        enviado = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("LISTA DE TÓPICOS DO EDITAL (trt8-2026)", enviado)
        self.assertIn("T1 Constituição", enviado)
        self.assertIn("FORA (fora do edital)", enviado)
        self.assertTrue(self.updates[-1]["topico_sugerido"].startswith("FORA"))

    def test_claude_grava_sem_conferir(self):
        with mock.patch("requests.post") as post:
            n = self.rodar._aplicar_enriquecer(self.gemini_txt, ["q1"], {})
        self.assertEqual(n, 1)
        self.assertNotIn("enriquecido_por", self.updates[-1])
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
