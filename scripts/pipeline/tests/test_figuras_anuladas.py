"""Robô resolvendo sozinho o que caía para o humano: anulada sem tópico é descartada e a etapa figuras (Gemini simulado)
limpa o aviso, transcreve a figura ou deixa para o humano só a figura essencial. Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import importlib, json, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Banco:
    """supa de mentira: guarda os selects pedidos e os updates feitos."""
    def __init__(self, linhas=None):
        self.linhas, self.selects, self.updates = linhas or {}, [], []

    def select(self, tabela, params=None):
        self.selects.append((tabela, dict(params or {})))
        return self.linhas.get(tabela, [])

    def update(self, tabela, filtro, valores):
        self.updates.append((tabela, dict(filtro), valores))

    def upsert(self, *a, **k):
        pass

    def baixar_arquivo(self, bucket, caminho):
        return b"%PDF-teste"


class BaseRodar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.rodar = importlib.import_module("rodar")
        except ImportError as e:  # máquina sem as dependências do robô
            raise unittest.SkipTest(f"rodar.py não importa aqui ({e})")


class TestAnuladas(BaseRodar):
    def test_descarta_anulada_e_sem_gabarito_com_ou_sem_topico(self):
        banco = Banco({"ingestao_questoes": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
        with mock.patch.object(self.rodar, "supa", banco):
            n = self.rodar.descartar_anuladas()
        self.assertEqual(n, 3)
        tabela, filtro, valores = banco.updates[0]
        self.assertEqual(filtro, {"status": "eq.pendente", "or": "(anulada.eq.true,gabarito.is.null)"})
        self.assertNotIn("topico_sugerido", filtro)  # antes só descartava as que já tinham tópico
        self.assertEqual(valores["status"], "rejeitada")
        self.assertIn("revisado_em", valores)

    def test_sem_anuladas_nao_atualiza(self):
        banco = Banco()
        with mock.patch.object(self.rodar, "supa", banco):
            self.assertEqual(self.rodar.descartar_anuladas(), 0)
        self.assertEqual(banco.updates, [])

    def test_publicar_descarta_antes_de_olhar_o_topico(self):
        banco = Banco()
        with mock.patch.object(self.rodar, "supa", banco):
            self.rodar.publicar()
        self.assertEqual(banco.selects[0][1].get("or"), "(anulada.eq.true,gabarito.is.null)")


Q = {"id": "aocp-x-q07", "prova_id": "p1", "numero": 7, "pagina": 3, "texto_base": None, "enunciado": "Observe o quadro a seguir e responda.",
     "alternativas": {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5"}, "gabarito": "C", "avisos": ["depende de figura: transcrever"]}


class TestDecidirFigura(BaseRodar):
    def test_nao_precisa_da_figura_limpa_o_aviso(self):
        mud = self.rodar.decidir_figura({**Q, "avisos": ["depende de figura: transcrever", "outro aviso"]},
                                        {"precisa_da_figura": False, "transcricao": None, "ok": True, "motivo": "decorativa"})
        self.assertEqual(mud, {"pode_depender_de_imagem": False, "avisos": ["outro aviso"]})

    def test_tabela_e_transcrita_no_enunciado(self):
        mud = self.rodar.decidir_figura(Q, {"precisa_da_figura": True, "transcricao": "| a | b |\n|---|---|\n| 1 | 2 |", "ok": True})
        self.assertFalse(mud["pode_depender_de_imagem"])
        self.assertEqual(mud["avisos"], [])
        self.assertTrue(mud["enunciado"].startswith(Q["enunciado"]))
        self.assertEqual(mud["enunciado"].count("[figura transcrita]"), 2)
        self.assertIn("| 1 | 2 |", mud["enunciado"])

    def test_imagem_de_verdade_fica_para_o_humano(self):
        mud = self.rodar.decidir_figura(Q, {"precisa_da_figura": True, "transcricao": None, "ok": True, "motivo": "gráfico"})
        self.assertEqual(mud, {"avisos": ["figura essencial: precisa de recorte"]})
        self.assertNotIn("pode_depender_de_imagem", mud)  # continua marcada: publicar não solta

    def test_imagem_com_recorte_ja_no_enunciado_segue(self):
        q = {**Q, "enunciado": Q["enunciado"] + "\n\n![Figura 1 da questão 7](https://x/figuras/a.png)"}
        mud = self.rodar.decidir_figura(q, {"precisa_da_figura": True, "transcricao": None, "ok": True})
        self.assertEqual(mud, {"pode_depender_de_imagem": False, "avisos": []})

    def test_recorte_colado_na_frase_vai_para_linha_propria(self):
        url = "![Figura 1 da questão 6](https://x.supabase.co/storage/v1/object/public/figuras/a-q06-1.png)"
        q = {**Q, "enunciado": f"trecho “Torna-se necessário”.{url}"}
        mud = self.rodar.decidir_figura(q, {"precisa_da_figura": True, "transcricao": None, "ok": True})
        self.assertEqual(mud["enunciado"], f"trecho “Torna-se necessário”.\n\n{url}")
        self.assertFalse(mud["pode_depender_de_imagem"])

    def test_leitura_nao_confirmada_vai_para_o_humano(self):
        mud = self.rodar.decidir_figura(Q, {"precisa_da_figura": False, "ok": False, "motivo": "questão não está na página"})
        self.assertTrue(mud["avisos"][0].startswith("figura essencial: precisa de recorte"))
        self.assertIn("não está na página", mud["avisos"][0])

    def test_resposta_ilegivel_nao_muda_nada(self):
        self.assertIsNone(self.rodar.decidir_figura(Q, None))
        self.assertIsNone(self.rodar.decidir_figura(Q, {"motivo": "sem campo"}))


def gemini_json(obj):
    return lambda tarefa, conteudo, **k: (json.dumps(obj), "end_turn")


class TestEtapaFiguras(BaseRodar):
    def rodar_figuras(self, qs, resposta, png=b"png", disponivel=True, provas=None):
        banco = Banco({"ingestao_questoes": qs, "ingestao_provas": provas if provas is not None else [{"id": "p1", "pdf_path": "aocp/x.pdf"}]})
        ia = self.rodar.ia
        with mock.patch.object(self.rodar, "supa", banco), \
             mock.patch.object(ia, "gemini_disponivel", return_value=disponivel), mock.patch.object(ia, "sem_tempo", return_value=False), \
             mock.patch.object(ia, "gemini", side_effect=resposta) as gem, \
             mock.patch.object(self.rodar, "_png_da_pagina", return_value=png), mock.patch.object(self.rodar, "_achar_pagina", return_value=None):
            self.rodar.figuras()
        return banco, gem

    def test_manda_a_pagina_ao_gemini_da_lista_pesada_e_limpa_o_aviso(self):
        banco, gem = self.rodar_figuras([Q], gemini_json({"precisa_da_figura": False, "transcricao": None, "ok": True, "motivo": "x"}))
        tarefa, conteudo = gem.call_args.args
        self.assertEqual(tarefa, "figuras")
        self.assertEqual(self.rodar.ia.classe_gemini(tarefa), "pesado")
        self.assertEqual(conteudo[0]["type"], "image")
        self.assertIn("GABARITO OFICIAL: C", conteudo[1]["text"])
        self.assertEqual(banco.updates, [("ingestao_questoes", {"id": "eq.aocp-x-q07"}, {"pode_depender_de_imagem": False, "avisos": []})])

    def test_ja_marcada_como_essencial_nao_gasta_cota(self):
        q = {**Q, "avisos": ["figura essencial: precisa de recorte"]}
        banco, gem = self.rodar_figuras([q], gemini_json({}))
        gem.assert_not_called()
        self.assertEqual(banco.updates, [])

    def test_sem_pdf_guardado_espera(self):
        banco, gem = self.rodar_figuras([Q], gemini_json({}), provas=[{"id": "p1", "pdf_path": None}])
        gem.assert_not_called()
        self.assertEqual(banco.updates, [])

    def test_caderno_escaneado_sem_pagina_vai_inteiro(self):
        banco, gem = self.rodar_figuras([{**Q, "pagina": None}], gemini_json([{"numero": 7, "precisa_da_figura": False, "ok": True}]), png=None)
        bloco, texto = gem.call_args.args[1]
        self.assertEqual(bloco["source"]["media_type"], "application/pdf")
        self.assertIn("caderno inteiro", texto["text"])
        self.assertFalse(banco.updates[0][2]["pode_depender_de_imagem"])

    def test_um_pedido_por_caderno_com_todas_as_questoes_dele(self):
        # 28/10: antes era um pedido por questão, e o caderno inteiro ia de novo a cada uma (cota Flash = 20/dia)
        qs = [{**Q, "id": "a-q07", "numero": 7, "pagina": None}, {**Q, "id": "a-q12", "numero": 12, "pagina": None},
              {**Q, "id": "b-q03", "prova_id": "p2", "numero": 3, "pagina": None}]
        provas = [{"id": "p1", "pdf_path": "fgv/a.pdf"}, {"id": "p2", "pdf_path": "fgv/b.pdf"}]
        resposta = {"fgv/a.pdf": [{"numero": 7, "precisa_da_figura": False, "ok": True},
                                  {"numero": "12", "precisa_da_figura": True, "transcricao": None, "ok": True}],
                    "fgv/b.pdf": [{"numero": 3, "precisa_da_figura": True, "transcricao": "| x |", "ok": True}]}
        chamadas = []

        def gem(tarefa, conteudo, **k):
            texto = conteudo[1]["text"]
            chamadas.append(texto)
            return json.dumps(resposta["fgv/a.pdf" if "QUESTÃO 7" in texto else "fgv/b.pdf"]), "end_turn"
        banco, _ = self.rodar_figuras(qs, gem, png=None, provas=provas)
        self.assertEqual(len(chamadas), 2)  # um por caderno, não um por questão
        self.assertIn("QUESTÃO 7", chamadas[0]); self.assertIn("QUESTÃO 12", chamadas[0]); self.assertNotIn("QUESTÃO 3\n", chamadas[0])
        mud = {f["id"]: v for _, f, v in banco.updates}
        self.assertEqual(mud["eq.a-q07"], {"pode_depender_de_imagem": False, "avisos": []})
        self.assertEqual(mud["eq.a-q12"], {"avisos": ["figura essencial: precisa de recorte"]})
        self.assertIn("| x |", mud["eq.b-q03"]["enunciado"])

    def test_questao_que_falta_na_resposta_do_caderno_fica_para_a_proxima(self):
        qs = [{**Q, "id": "a-q07", "numero": 7, "pagina": None}, {**Q, "id": "a-q12", "numero": 12, "pagina": None}]
        banco, gem = self.rodar_figuras(qs, gemini_json([{"numero": 7, "precisa_da_figura": False, "ok": True}]), png=None)
        self.assertEqual(gem.call_count, 1)
        self.assertEqual([f["id"] for _, f, _ in banco.updates], ["eq.a-q07"])

    def test_caderno_grande_sem_pagina_vai_para_o_humano_sem_chamar_o_gemini(self):
        with mock.patch.object(self.rodar, "PDF_INTEIRO_MAX", 3):
            banco, gem = self.rodar_figuras([{**Q, "pagina": None}], gemini_json({}), png=None)
        gem.assert_not_called()
        self.assertIn("página não localizada", banco.updates[0][2]["avisos"][0])

    def test_gemini_sem_cota_para_a_etapa(self):
        def sem_cota(*a, **k):
            raise self.rodar.ia.GeminiSemCota("cota diária de todos os modelos pesados esgotada; tenta amanhã")
        banco, gem = self.rodar_figuras([Q, {**Q, "id": "outra"}], sem_cota)
        self.assertEqual(gem.call_count, 1)  # não insiste com a segunda
        self.assertEqual(banco.updates, [])

    def test_sem_gemini_pula(self):
        banco, gem = self.rodar_figuras([Q], gemini_json({}), disponivel=False)
        gem.assert_not_called()
        self.assertEqual(banco.selects, [])

    def test_teto_padrao_10_por_rodada(self):
        import importlib as il
        import ia
        ia = il.reload(ia)
        with mock.patch.dict("os.environ", {"GEMINI_MAX_FIGURAS": ""}):
            self.assertEqual(ia.teto_gemini("figuras"), 10)


if __name__ == "__main__":
    unittest.main()
