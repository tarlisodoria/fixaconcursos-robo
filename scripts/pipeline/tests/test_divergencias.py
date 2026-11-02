"""02/11/2026: 71 questões com "IAs divergiram". (1) o conferente via só 700 caracteres do texto de apoio e acusava "a palavra X
não está no texto"; (2) divergência só de tópico vai a um TERCEIRO modelo (nem o escritor nem o conferente); (3) as pendentes com
o aviso antigo são reconferidas sem chamar o escritor. Sem rede e sem banco (tudo simulado).

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import importlib, json, os, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENV = {"CONFERENTE_GITHUB": "", "ANTHROPIC_API_KEY": "", "ANTHROPIC_MODEL": "", "GEMINI_API_KEY": "g", "GEMINI_MODEL": "",
       "GEMINI_MODELOS_PESADO": "gemini-3.8-flash", "GEMINI_MODELOS_LEVE": "gemini-3.5-flash-lite",
       "GEMINI_MODELOS_CONFERENCIA": "gemma-4-31b-it,gemini-3.1-flash-lite", "GEMINI_MAX_CHAMADAS": "50",
       "IA_DESLIGADA": "1", "GEMINI_DESLIGADO": "", "GITHUB_TOKEN": "", "CONFERENTE_DESLIGADO": "",
       "SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"}
ESCRITOR = "gemini-3.5-flash-lite"
TOPICOS = [{"id": "T1", "disciplina": "Direito", "titulo": "Constituição"}, {"id": "T2", "disciplina": "Direito", "titulo": "Licitações"},
           {"id": "T3", "disciplina": "Português", "titulo": "Interpretação de texto"}]


class Resp:
    def __init__(self, status, corpo=None):
        self.status_code, self._c, self.headers = status, corpo or {}, {}
        self.text = json.dumps(self._c)

    def json(self):
        return self._c


def gem(itens):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(itens)}]}, "finishReason": "STOP"}], "usageMetadata": {}})


def questao(qid="q1", texto=None, avisos=None, **extra):
    return {"id": qid, "concurso_alvo": "trt8-2026", "texto_base": texto, "enunciado": f"Enunciado da questão {qid}",
            "alternativas": {"C": "Certo", "E": "Errado"}, "gabarito": "C", "avisos": avisos or [], **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start()
        import ia, rodar
        self.ia = importlib.reload(ia)
        self.ia._gasto_mes = 0.0
        self.rodar = importlib.reload(rodar)
        self.sono = mock.patch("time.sleep"); self.sono.start()
        self.updates, self.diag, self.qs = [], [], [questao()]
        self.sel = mock.patch.object(self.rodar.supa, "select", side_effect=lambda t, p=None: self.qs if t == "ingestao_questoes" else TOPICOS)
        self.upd = mock.patch.object(self.rodar.supa, "update", side_effect=lambda t, f, m: self.updates.append((f["id"][3:], m)))
        self.ups = mock.patch.object(self.rodar.supa, "upsert", side_effect=lambda t, linhas, **k: self.diag.extend(linhas))
        for m in (self.sel, self.upd, self.ups):
            m.start()
        self.comentario = "**Gabarito: (C).** Porque sim, conforme a Constituição."

    def tearDown(self):
        for m in (self.sel, self.upd, self.ups, self.sono, self.env):
            m.stop()

    def aplicar(self, respostas, topico="T1"):
        txt = json.dumps([{"id": q["id"], "topico": topico, "explicacao": self.comentario} for q in self.qs])
        with mock.patch("requests.post", side_effect=respostas) as post:
            self.rodar._aplicar_enriquecer(txt, [q["id"] for q in self.qs], {}, conferir_gratis=True, escritor=ESCRITOR)
        return post

    def ultimo(self, qid="q1"):
        return [m for i, m in self.updates if i == qid][-1]


class TestTextoInteiro(Base):
    def test_texto_de_apoio_vai_inteiro(self):
        texto = "Primeiro parágrafo. " * 150 + "A palavra-chave está no fim."  # ~3.000 caracteres (antes: só 700)
        self.qs = [questao(texto=texto)]
        post = self.aplicar([gem([{"id": "q1", "ok": True}])])
        enviado = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("A palavra-chave está no fim.", enviado)
        self.assertNotIn(self.rodar.MARCA_TEXTO_CORTADO + "\n", enviado.split("QUESTÕES:")[0])

    def test_texto_acima_de_6000_e_cortado_com_marca(self):
        self.qs = [questao(texto="x" * 9000)]
        post = self.aplicar([gem([{"id": "q1", "ok": True}])])
        enviado = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("x" * 6000 + "\n" + self.rodar.MARCA_TEXTO_CORTADO, enviado)
        self.assertNotIn("x" * 6001, enviado)

    def test_mesmo_texto_vai_uma_vez_por_pedido(self):
        texto = "Texto de apoio compartilhado. " * 100
        self.qs = [questao("q1", texto), questao("q2", texto), questao("q3", texto)]
        post = self.aplicar([gem([{"id": q, "ok": True} for q in ("q1", "q2", "q3")])])
        self.assertEqual(post.call_count, 1)
        enviado = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertEqual(enviado.count(texto.strip()), 1)
        self.assertEqual(enviado.count("TEXTO DE APOIO: [T1] (acima)"), 3)

    def test_textos_grandes_dividem_em_mais_pedidos(self):
        self.qs = [questao(f"q{i}", c * 6000) for i, c in enumerate("abc", 1)]
        todos = gem([{"id": f"q{i}", "ok": True} for i in (1, 2, 3)])
        post = self.aplicar([todos] * 3)
        self.assertGreaterEqual(post.call_count, 2)  # juntos passariam do limite do pedido: menos questões por pedido, texto inteiro
        for c in post.call_args_list:
            enviado = c.kwargs["json"]["contents"][0]["parts"][0]["text"]
            self.assertLessEqual(len(enviado), self.rodar.LIMITE_PEDIDO_CONFERENCIA)
        for c in "abc":
            self.assertTrue(any(c * 6000 in x.kwargs["json"]["contents"][0]["parts"][0]["text"] for x in post.call_args_list))
        self.assertEqual(len([m for _, m in self.updates if m.get("topico_sugerido")]), 3)


class TestDesempate(Base):
    def test_terceiro_modelo_confirma_o_escritor_e_publica(self):
        post = self.aplicar([gem([{"id": "q1", "ok": False, "motivo": "seria T2", "tipo": "topico", "topico": "T2"}]),
                             gem([{"id": "q1", "topico": "T1", "motivo": "cobra a Constituição"}])])
        self.assertEqual(post.call_count, 2)
        self.assertIn("gemma-4-31b-it", post.call_args_list[0].args[0])        # conferente
        self.assertIn("gemini-3.1-flash-lite", post.call_args_list[1].args[0])  # terceiro: nem escritor nem conferente
        u = self.ultimo()
        self.assertEqual(u["topico_sugerido"], "T1")
        self.assertNotIn("avisos", u)
        self.assertEqual(u["enriquecido_por"], "gemini+gemma-4-31b-it+gemini-3.1-flash-lite")
        self.assertIsNone(self.rodar.motivo_revisao({**self.qs[0], **u}))  # o publicar aprova sozinho

    def test_terceiro_escolhe_a_proposta_do_conferente(self):
        self.aplicar([gem([{"id": "q1", "ok": False, "motivo": "é FORA", "tipo": "tópico", "topico": "FORA"}]),
                      gem([{"id": "q1", "topico": "FORA", "motivo": "legislação municipal"}])])
        self.assertEqual(self.ultimo()["topico_sugerido"], "FORA-trt8-2026")
        self.assertNotIn("avisos", self.ultimo())

    def test_desempate_recebe_lista_e_as_duas_propostas(self):
        post = self.aplicar([gem([{"id": "q1", "ok": False, "motivo": "seria T2", "tipo": "topico", "topico": "T2"}]),
                             gem([{"id": "q1", "topico": "T1"}])])
        enviado = post.call_args_list[1].kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("LISTA DE TÓPICOS DO EDITAL (trt8-2026)", enviado)
        self.assertIn("PROPOSTA A: T1 [Direito] Constituição", enviado)
        self.assertIn("PROPOSTA B: T2 [Direito] Licitações", enviado)

    def test_terceira_opcao_fica_para_o_humano(self):
        self.aplicar([gem([{"id": "q1", "ok": False, "motivo": "seria T2", "tipo": "topico", "topico": "T2"}]),
                      gem([{"id": "q1", "topico": "T3", "motivo": "é interpretação"}])])
        u = self.ultimo()
        self.assertEqual(u["topico_sugerido"], "T1")
        self.assertTrue(u["avisos"][-1].startswith("IAs divergiram (tópico): seria T2"))
        self.assertIn("escolheu T3", u["avisos"][-1])
        self.assertTrue(self.rodar.motivo_revisao({**self.qs[0], **u}))

    def test_conteudo_nunca_e_desempatado(self):
        post = self.aplicar([gem([{"id": "q1", "ok": False, "motivo": "o art. 5º não diz isso", "tipo": "conteudo"}])])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.ultimo()["avisos"], ["IAs divergiram (conteúdo): o art. 5º não diz isso"])

    def test_sem_tipo_conta_como_conteudo(self):
        post = self.aplicar([gem([{"id": "q1", "ok": False, "motivo": "erro no comentário"}])])
        self.assertEqual(post.call_count, 1)
        self.assertTrue(self.ultimo()["avisos"][0].startswith("IAs divergiram (conteúdo)"))

    def test_sem_terceiro_modelo_fica_para_o_humano(self):
        os.environ["GEMINI_MODELOS_CONFERENCIA"] = "gemma-4-31b-it,gemini-3.5-flash-lite"  # o 2º é o próprio escritor
        post = self.aplicar([gem([{"id": "q1", "ok": False, "motivo": "seria T2", "tipo": "topico", "topico": "T2"}])])
        self.assertEqual(post.call_count, 1)
        self.assertIn("sem terceiro modelo", self.ultimo()["avisos"][-1])

    def test_desempate_fora_do_ar_guarda_a_proposta(self):
        erro = Resp(503, {"error": {"message": "overloaded"}})
        self.aplicar([gem([{"id": "q1", "ok": False, "motivo": "seria T2", "tipo": "topico", "topico": "T2"}])] + [erro] * 6)
        u = self.ultimo()
        self.assertIn("proposta_ia", u)  # a próxima rodada confere e desempata de novo (sem chamar o escritor)
        self.assertNotIn("topico_sugerido", u)


class TestReconferir(Base):
    def test_reprocessa_o_aviso_antigo_sem_chamar_o_escritor(self):
        antigo = "IAs divergiram: a palavra 'cidadania' não existe no texto"
        self.qs = [questao("q1", "Texto longo... " * 80 + "cidadania", [antigo, "outro aviso"], topico_sugerido="T1", explicacao=self.comentario,
                           enriquecido_por="gemini+gemma-4-31b-it"),
                   questao("q2", None, [antigo], topico_sugerido="T1", explicacao=self.comentario, enriquecido_por="gemini+gemma-4-31b-it"),
                   questao("q3", None, [antigo], topico_sugerido="T1", explicacao=self.comentario, enriquecido_por="gemini+gemma-4-31b-it"),
                   questao("q4", None, ["IAs divergiram (conteúdo): já no formato novo"], topico_sugerido="T1", explicacao=self.comentario,
                           enriquecido_por="gemini+gemma-4-31b-it")]
        conf = gem([{"id": "q1", "ok": True}, {"id": "q2", "ok": False, "motivo": "seria T2", "tipo": "topico", "topico": "T2"},
                    {"id": "q3", "ok": False, "motivo": "comentário errado", "tipo": "conteudo"}])
        with mock.patch("requests.post", side_effect=[conf, gem([{"id": "q2", "topico": "T2"}])]) as post:
            self.rodar._reconferir_divergentes({})
        self.assertEqual(post.call_count, 2)  # conferência + desempate; nenhum pedido ao escritor
        self.assertTrue(all(ESCRITOR not in c.args[0] for c in post.call_args_list))
        self.assertNotIn("q4", post.call_args_list[0].kwargs["json"]["contents"][0]["parts"][0]["text"])
        self.assertEqual(self.ultimo("q1")["avisos"], ["outro aviso"])  # o aviso antigo sai, os outros ficam
        self.assertEqual(self.ultimo("q2")["topico_sugerido"], "T2")
        self.assertEqual(self.ultimo("q2")["avisos"], [])
        self.assertEqual(self.ultimo("q3")["avisos"], ["IAs divergiram (conteúdo): comentário errado"])
        d = [l for l in self.diag if l["etapa"] == "reconferir_divergencias"][0]
        det = json.loads(d["detalhe"])
        self.assertEqual((det["resolvidas"], det["ficaram"], det["aprovada"], det["desempate"], det["conteudo"]), (2, 1, 1, 1, 1))
        self.assertEqual(d["itens"], 3)

    def test_sem_conferente_fica_como_esta(self):
        self.qs = [questao("q1", None, ["IAs divergiram: x"], topico_sugerido="T1", explicacao=self.comentario, enriquecido_por="gemini+g")]
        with mock.patch("requests.post", side_effect=[Resp(503, {})] * 6):
            self.rodar._reconferir_divergentes({})
        self.assertEqual(self.updates, [])
        self.assertEqual(json.loads(self.diag[0]["detalhe"])["adiadas"], 1)


if __name__ == "__main__":
    unittest.main()
