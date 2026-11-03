"""Provas FCC enviadas em lote pelo organizador (03/11/2026): carimbo de terceiro nunca entra na questão, transcrição na lista
PESADA, envio com concurso-alvo na frente da fila do processar, id pela sigla do órgão e fonte "Prova FCC <ano> – TRTn –
Técnico Judiciário TI, questão N". Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import json, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ia_extrair  # noqa: E402
from test_gabarito_organizador import LETRAS, BaseEnvio, FakeSupa, payload  # noqa: E402


class TestCarimbo(unittest.TestCase):
    def test_prompt_manda_ignorar_carimbo(self):
        self.assertIn("www.tecconcursos.com.br", ia_extrair.PROMPT)
        self.assertIn("nunca os transcreva", ia_extrair.PROMPT)
        ia_extrair.PROMPT.format(banca="FCC", faixa="de 1 a 60", regra_formato="")  # o texto novo não quebra o format

    def test_filtro_tira_a_linha_do_carimbo_e_mantem_o_resto(self):
        qs = [{"enunciado": "Considere o código abaixo.\nwww.tecconcursos.com.br/questoes/123\nO resultado é:",
               "texto_base": "Texto I\nLicenciado - TEC Concursos - 12345\nfim",
               "alternativas": {"A": "10", "B": "20\nwww.TecConcursos.com.br", "C": "30", "D": "40", "E": "50"}},
              {"enunciado": "Questão limpa", "texto_base": None, "alternativas": {"A": "x", "B": "y"}}]
        n = ia_extrair.limpar_carimbos(qs)
        self.assertEqual(n, 3)
        self.assertEqual(qs[0]["enunciado"], "Considere o código abaixo.\nO resultado é:")
        self.assertEqual(qs[0]["texto_base"], "Texto I\nfim")
        self.assertEqual(qs[0]["alternativas"]["B"], "20")
        self.assertEqual(qs[1]["enunciado"], "Questão limpa")
        self.assertNotRegex(json.dumps(qs, ensure_ascii=False).lower(), r"tec\s*concursos")


class TestListaPesada(unittest.TestCase):
    def test_transcricao_de_caderno_fcc_usa_a_lista_pesada(self):
        import ia
        self.assertEqual(ia.classe_gemini("transcrever caderno (FCC)"), "pesado")


class Fila:
    def __init__(self, envios, resto):
        self.envios, self.resto, self.params = envios, resto, []

    def select(self, tabela, params=None):
        self.params.append(params)
        return list(self.envios if (params or {}).get("id") == "like.envio:*" else self.resto)


class TestPrioridadeDaFila(BaseEnvio):
    def test_envio_com_alvo_vem_antes_de_tudo(self):
        envios = [{"id": f"envio:{i}"} for i in range(18)]
        resto = [{"id": "fgv:1"}, {"id": "envio:3"}, {"id": "aocp:9"}]
        f = Fila(envios, resto)
        with mock.patch.object(self.rodar, "supa", f):
            fila = self.rodar.fila_processar(20)
        self.assertEqual([c["id"] for c in fila[:18]], [f"envio:{i}" for i in range(18)])
        self.assertEqual([c["id"] for c in fila[18:]], ["fgv:1", "aocp:9"])  # sem repetir o envio:3
        p_envio = next(p for p in f.params if p.get("id") == "like.envio:*")
        self.assertEqual(p_envio["concurso_alvo"], "not.is.null")
        self.assertTrue(p_envio["order"].startswith("tentado_em.asc.nullsfirst"))  # envio com erro não trava os outros

    def test_mais_envios_que_o_limite_vao_todos(self):
        with mock.patch.object(self.rodar, "supa", Fila([{"id": f"envio:{i}"} for i in range(25)], [{"id": "fgv:1"}])):
            self.assertEqual(len(self.rodar.fila_processar(20)), 25)


class TestEnvioDoLote(BaseEnvio):
    def test_id_pela_sigla_fonte_e_carimbo(self):
        manifesto = {"envio": {"caderno": "envios/x/caderno.pdf", "gabarito": "envios/x/gabarito.pdf",
                               "cargo": "Técnico Judiciário – Apoio Especializado – Tecnologia da Informação", "cargo_cod": "K11",
                               "tipo": "1", "orgao": "Tribunal Regional do Trabalho da 22ª Região", "orgao_sigla": "trt22", "ano": 2022,
                               "fonte_prova": "Prova FCC 2022 – TRT22 – Técnico Judiciário TI", "gabarito_respostas": payload()}}
        qs = [{"numero": n, "enunciado": f"Q{n}\nwww.tecconcursos.com.br", "alternativas": dict(zip("ABCDE", "12345"))} for n in LETRAS]
        sup = FakeSupa()
        with mock.patch.object(self.rodar, "supa", sup), mock.patch.object(self.rodar, "extrair_questoes", return_value=qs), \
             mock.patch.object(self.rodar, "_texto_pdf", side_effect=AssertionError("não devia ler o PDF")):
            self.rodar.processar_envio({"id": "envio:x", "banca": "FCC", "cargos": json.dumps(manifesto), "concurso_alvo": "trt8-2026"})
        linhas = next(ls for t, ls in sup.upserts if t == "ingestao_questoes")
        q1 = next(q for q in linhas if q["numero"] == 1)
        self.assertEqual(q1["id"], "fcc-2022-trt22-tecnico-judiciario-apoio-especiali-q01")
        self.assertEqual(q1["fonte"], ["Prova FCC 2022 – TRT22 – Técnico Judiciário TI, questão 1"])
        self.assertEqual(q1["orgao"], "Tribunal Regional do Trabalho da 22ª Região")
        self.assertEqual(q1["enunciado"], "Q1")  # carimbo do PDF de texto também sai
        self.assertEqual(q1["concurso_alvo"], "trt8-2026")


if __name__ == "__main__":
    unittest.main()
