"""Gabarito FGV (22/11/2026): 22 provas de 11 concursos foram para 'erro' com "0 respostas lidas". Causa: a FGV publica vários
gabaritos definitivos por concurso (um por cargo, por dia de prova, por nível) e o robô lia só um. Os textos abaixo são
trechos reais reduzidos dos gabaritos oficiais (tcerr24, tjmtservidor, mprj2025). Também: download com nova tentativa,
erro passageiro volta para a fila e JSON da IA com caractere de controle. Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import contextlib, importlib, io, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fgv  # noqa: E402
import requests  # noqa: E402

LINHAS = ("1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20\nC A C E D E D C B D E D B A A E A B E D\n"
          "21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40\nC D D E A D A B D C D E D E D B C D D C\n")
OUTRAS = ("1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20\nD A A B B D * B E A D C D C E A B A C B\n"
          "21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40\nD C A B * A E E D D D E C E B B D C A D\n")

# gabarito_definitivo_tcerr_10.03.2025.pdf (reduzido)
TCERR = ("GABARITO DEFINITIVO\nAuditor de Controle Externo - Controle Externo - TIPO 1\n" + OUTRAS +
         "(*) Questão Anulada\nAuditor de Controle Externo - Tecnologia da Informação - Análise de Dados - TIPO 1\n" + LINHAS)
# tjmt_servidor_gabarito_definitivo.pdf (reduzido) e a versão retificada, que só traz Direito e Oficial
TJMT = ("GABARITO DEFINITIVO DA PROVA APLICADA NO DIA 15/12/2024\nAnalista Judiciário - Tecnologia da Informação - TIPO 1\n" + LINHAS +
        "Analista Judiciário - Tecnologia da Informação - TIPO 2\n" + OUTRAS)
TJMT_V2 = ("TRIBUNAL DE JUSTIÇA DE MATO GROSSO\nGABARITO DEFINITIVO DA PROVA APLICADA NO DIA 15/12/2024\n"
           "Analista Judiciário - Direito - TIPO 1\n" + OUTRAS + "Oficial de Justiça - Direito - TIPO 1\n" + LINHAS)
# mprj2025: layout que o leitor determinístico não conhece (sem "TIPO"); um PDF para técnico, outro para analista
MPRJ_TEC = ("GABARITO DEFINITIVO DA PROVA APLICADA NO DIA 11/05/2025\nTécnico do Ministério Público - Área Administrativa - 1\n" + LINHAS) * 3
MPRJ_ANA = ("GABARITO DEFINITIVO\nAnalista do Ministério Público - Área Administrativa - Tecnologia da Informação - 1\n" + LINHAS) * 3

PAGINA_TJMT = """
<a href="/sites/default/files/concursos/tjmt_servidor_gabarito_definitivo_v2_apenas-analista-e-oficial.pdf">Gabarito Oficial Definitivo da Prova Objetiva Retificado (Analista Judiciário e Oficial)</a>
<a href="/sites/default/files/concursos/comunicado-retificacao-gabarito-definitivo-e-reprocessamento-r-1.pdf">COMUNICADO (atualizado em 04/02/2025)</a>
<a href="/sites/default/files/concursos/tjmt_servidor_gabarito_definitivo.pdf">Gabarito Oficial Definitivo da Prova Objetiva</a>
<a href="/sites/default/files/concursos/tjmt_servidor_gabarito_preliminar.pdf">Gabarito Preliminar</a>
"""


def doc(texto):
    return b"%PDF", [texto], fgv.parse_gabarito(texto)


class TestListaDeGabaritos(unittest.TestCase):
    def test_todos_os_definitivos_retificado_na_frente_sem_comunicado(self):
        urls = fgv.gabaritos_definitivos([("https://conhecimento.fgv.br/concursos/tjmtservidor", PAGINA_TJMT)])
        self.assertEqual([u.rsplit("/", 1)[-1] for u in urls],
                         ["tjmt_servidor_gabarito_definitivo_v2_apenas-analista-e-oficial.pdf", "tjmt_servidor_gabarito_definitivo.pdf"])
        self.assertEqual(fgv.gabarito_definitivo([("https://conhecimento.fgv.br/concursos/tjmtservidor", PAGINA_TJMT)]), urls[0])

    def test_subpagina_do_caderno_primeiro(self):
        partes = [("https://x/concursos/c", '<a href="/g-geral-definitivo.pdf">Gabarito Oficial Definitivo</a>'),
                  ("https://x/concursos/c/02", '<a href="/caderno-tipo1.pdf">Tipo 1</a><a href="/g-02-definitivo.pdf">Gabarito Oficial Definitivo</a>')]
        parte = fgv.parte_do_caderno(partes, "https://x/caderno-tipo1.pdf")
        self.assertEqual(parte, "https://x/concursos/c/02")
        self.assertEqual(fgv.gabaritos_definitivos(partes, parte), ["https://x/g-02-definitivo.pdf", "https://x/g-geral-definitivo.pdf"])


class TestCasamentoDoCargo(unittest.TestCase):
    def test_tcerr_nao_casa_cargo_mais_curto_que_cabe_no_arquivo(self):
        par = fgv.achar_no_gabarito(fgv.parse_gabarito(TCERR), "Auditor de Controle Externo - Tecnologia da Informação, com especialidade em Análise de Dados",
                                    "auditor-de-controle-externo-tecnologia-com-especialidade-em-analise-de-dadoscns306", "1")
        self.assertEqual(par[0], "Auditor de Controle Externo - Tecnologia da Informação - Análise de Dados")
        self.assertEqual(par[1][1], "C")

    def test_tjmt_nome_vindo_do_arquivo_com_codigo_e_ti(self):
        par = fgv.achar_no_gabarito(fgv.parse_gabarito(TJMT), "cns102 analista judiciario analista de ti t02",
                                    "cns102-analista-judiciario-analista-de-ti-t02", "2")
        self.assertEqual(par[0], "Analista Judiciário - Tecnologia da Informação")
        self.assertEqual(par[1][1], "D")  # o bloco do TIPO 2, não o do TIPO 1

    def test_documento_sem_o_cargo_nao_casa(self):
        self.assertIsNone(fgv.achar_no_gabarito(fgv.parse_gabarito(TJMT_V2), "Analista Judiciário - Tecnologia da Informação",
                                                "cns102-analista-judiciario-analista-de-ti-t01", "1"))
        self.assertLess(fgv.cobertura([TJMT_V2], "Analista Judiciário - Tecnologia da Informação", "cns102-analista-judiciario-analista-de-ti-t01"), 0.75)
        self.assertEqual(fgv.cobertura([TJMT], "Analista Judiciário - Tecnologia da Informação", "x"), 1.0)

    def test_dois_cargos_parecidos_nao_escolhe(self):
        texto = "Analista de Sistemas - Manhã - TIPO 1\n" + LINHAS + "Analista de Sistemas - Tarde - TIPO 1\n" + OUTRAS
        gabs = fgv.parse_gabarito(texto)
        self.assertIsNone(fgv.achar_no_gabarito(gabs, "Analista de Sistemas", "analista-de-sistemas", "1"))
        self.assertIn("dois cargos parecidos", fgv.empate_no_gabarito(gabs, "Analista de Sistemas", "analista-de-sistemas", "1"))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.rodar = importlib.import_module("rodar")
        except ImportError as e:  # máquina sem as dependências do robô
            raise unittest.SkipTest(f"rodar.py não importa aqui ({e})")


class TestVariosGabaritos(Base):
    def test_tjmt_pula_o_retificado_que_nao_tem_o_cargo(self):
        docs = {"g/v2.pdf": doc(TJMT_V2), "g/todos.pdf": doc(TJMT)}
        u, achado, gab, motivo = self.rodar._gabarito_fgv_docs(list(docs), docs.get, "Analista Judiciário - Tecnologia da Informação",
                                                                "cns102-analista-judiciario-analista-de-ti-t01", "1")
        self.assertEqual((u, len(gab), motivo), ("g/todos.pdf", 40, None))

    def test_mprj_layout_desconhecido_vai_para_a_ia_com_o_documento_do_cargo(self):
        docs = {"g/tecnico.pdf": doc(MPRJ_TEC), "g/analista.pdf": doc(MPRJ_ANA)}
        lido = {n: "A" for n in range(1, 81)}
        with mock.patch.object(self.rodar, "_gabarito_fgv", return_value=("IA: Analista", lido, None)) as gab_fgv:
            u, achado, gab, _ = self.rodar._gabarito_fgv_docs(list(docs), docs.get, "Analista do Ministério Público – Área: Administrativa - "
                                                              "Tecnologia da Informação", "analista-do-ministerio-publico-area-administrativa-tecnologia-da-informacaocns102", "1")
        self.assertEqual((u, gab), ("g/analista.pdf", lido))
        self.assertEqual(gab_fgv.call_args.args[1], [MPRJ_ANA])  # a IA recebe o gabarito do analista, não o do técnico

    def test_nenhum_documento_cita_o_cargo_e_o_unico_erro(self):
        docs = {"g/tecnico.pdf": doc(MPRJ_TEC)}
        with mock.patch.object(self.rodar, "_gabarito_fgv", side_effect=AssertionError("não devia chamar a IA")):
            _, _, gab, motivo = self.rodar._gabarito_fgv_docs(list(docs), docs.get, "Analista - Tecnologia da Informação", "analista-ti", "1")
        self.assertEqual(gab, {})
        self.assertIn("nenhum dos 1 gabaritos definitivos cita o cargo", motivo)

    def test_ia_com_zero_respostas_num_documento_que_cita_o_cargo_fica_na_fila(self):
        docs = {"g/analista.pdf": doc(MPRJ_ANA)}
        with mock.patch.object(self.rodar, "_gabarito_fgv", return_value=("?", {}, None)):
            _, _, gab, motivo = self.rodar._gabarito_fgv_docs(list(docs), docs.get, "Analista do Ministério Público - Tecnologia da Informação",
                                                              "analista-mp-ti", "1")
        self.assertIsNone(gab)
        self.assertIn("IA leu 0 respostas", motivo)

    def test_empate_nao_vai_para_a_ia(self):
        texto = "Analista de Sistemas - Manhã - TIPO 1\n" + LINHAS + "Analista de Sistemas - Tarde - TIPO 1\n" + OUTRAS
        docs = {"g/a.pdf": doc(texto)}
        with mock.patch.object(self.rodar, "_gabarito_fgv", side_effect=AssertionError("não devia chamar a IA")):
            _, _, gab, motivo = self.rodar._gabarito_fgv_docs(list(docs), docs.get, "Analista de Sistemas", "analista-de-sistemas", "1")
        self.assertIsNone(gab)
        self.assertIn("dois cargos parecidos", motivo)


class TestFalhasAvulsas(Base):
    def test_download_tenta_de_novo_com_espera(self):
        ok = mock.Mock(content=b"%PDF-1.4 ...")
        quebrou = requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead(1231 bytes read, 8085 more expected)")
        with mock.patch.object(fgv, "_get", side_effect=[quebrou, quebrou, ok]), mock.patch.object(fgv.time, "sleep") as dorme:
            self.assertEqual(fgv.baixar(None, "https://conhecimento.fgv.br/g.pdf"), b"%PDF-1.4 ...")
        self.assertEqual([c.args[0] for c in dorme.call_args_list], [2, 5])

    def test_download_desiste_depois_de_tres_esperas(self):
        quebrou = requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
        with mock.patch.object(fgv, "_get", side_effect=quebrou), mock.patch.object(fgv.time, "sleep") as dorme, \
             self.assertRaises(requests.exceptions.ChunkedEncodingError):
            fgv.baixar(None, "https://conhecimento.fgv.br/g.pdf")
        self.assertEqual([c.args[0] for c in dorme.call_args_list], [2, 5, 15])

    def test_conexao_cortada_e_erro_passageiro(self):
        self.assertTrue(self.rodar.erro_passageiro("('Connection broken: IncompleteRead(1231 bytes read, 8085 more expected)', "
                                                   "IncompleteRead(1231 bytes read, 8085 more expected))"))
        self.assertFalse(self.rodar.erro_passageiro("gabarito do cargo não encontrado no definitivo"))

    def test_erro_passageiro_no_caderno_volta_para_a_fila(self):
        r = self.rodar
        gab = {str(n): "ABCDE"[n % 5] for n in range(1, 41)}
        antiga = {"cargo_cod": "analista-ti", "status": "aguardando_ia", "erro": "x", "pdf_path": "fgv/x.pdf", "gabarito": gab}
        cad = {"cod": "analista-ti", "cargo": "Analista TI", "arquivo": "analista ti", "url": "https://conhecimento.fgv.br/c.pdf",
               "tipo": "1", "parte": "https://conhecimento.fgv.br/concursos/x"}
        updates = []
        sup = mock.Mock(upsert=mock.Mock(return_value=[{"id": "p1"}]), select=mock.Mock(return_value=[]),
                        update=mock.Mock(side_effect=lambda t, f, v: updates.append((t, f, v))), baixar_arquivo=mock.Mock(return_value=b"%PDF"))
        quebrou = requests.exceptions.ChunkedEncodingError("('Connection broken: IncompleteRead(1231 bytes read, 8085 more expected)')")
        with mock.patch.object(r, "supa", sup), mock.patch.object(r.fgv, "sessao"), \
             mock.patch.object(r.fgv, "pagina", return_value=("Concurso X", [(cad["parte"], "")])), \
             mock.patch.object(r.fgv, "cadernos", return_value=[cad]), \
             mock.patch.object(r.fgv, "gabaritos_definitivos", return_value=["https://conhecimento.fgv.br/g.pdf"]), \
             mock.patch.object(r, "provas_do", return_value=[antiga]), mock.patch.object(r, "tempo_esgotado", return_value=False), \
             mock.patch.object(r, "rodada_cheia", return_value=False), mock.patch.object(r.ia, "disponivel", return_value=True), \
             mock.patch("ia_extrair.extrair_por_ia", side_effect=quebrou), contextlib.redirect_stdout(io.StringIO()):
            r.processar_fgv({"id": "fgv:x", "externo_id": "x", "nome": "Concurso X", "filtro_cargos": "ti"})
        prova = [v for t, f, v in updates if t == "ingestao_provas"]
        self.assertEqual(prova[-1]["status"], "aguardando_ia")
        self.assertIn("IncompleteRead", prova[-1]["erro"])
        conc = [v for t, f, v in updates if t == "ingestao_concursos"][-1]
        self.assertNotIn("processado_em", conc)  # o concurso volta na próxima rodada

    def test_json_da_ia_com_caractere_de_controle(self):
        from ia_extrair import _ler_transcricao
        txt = '{"cargo": "Analista", "questoes": [{"numero": 1, "enunciado": "Linha 1\nLinha 2\tfim", "alternativas": {"A": "x", "B": "y"}}]}'
        cargo, qs = _ler_transcricao(txt, "end_turn", "abcde")
        self.assertEqual((cargo, qs[0]["enunciado"]), ("Analista", "Linha 1\nLinha 2\tfim"))


if __name__ == "__main__":
    unittest.main()
