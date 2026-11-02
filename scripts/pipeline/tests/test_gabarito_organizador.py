"""Gabarito estruturado do organizador local (decisões do dono, 01/11/2026): preliminar vai para a fila, OCR/incompleto é
conferido com o PDF oficial e JSON inválido cai para a leitura do PDF. Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import importlib, json, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gabarito_organizador import (AVISO_PRELIMINAR, conferir_com_pdf, normalizar_gabarito_organizador,  # noqa: E402
                                  precisa_conferir, preliminar)

LETRAS = {n: "ABCDE"[(n - 1) % 5] for n in range(1, 11)}
# texto de um gabarito oficial da FCC (camada de texto do PDF), cargo K11 tipo 1
PDF_FCC = "Cargo ou opção K11 - Técnico Judiciário - TI\nTipo gabarito 1\n" + "\n".join(f"{n:03d} - {l}" for n, l in LETRAS.items())


def payload(**alteracoes):
    dados = {
        "cargo_codigo": "K11",
        "tipo": 1,
        "situacao": "definitivo",
        "lido_por_ocr": False,
        "completo": True,
        "total": 10,
        "faltando": [],
        "respostas": {str(n): l for n, l in LETRAS.items()},
    }
    dados.update(alteracoes)
    return dados


class TestNormalizar(unittest.TestCase):
    def test_normaliza_respostas_e_anuladas(self):
        dados = payload(respostas={**payload()["respostas"], "11": "*"}, total=11)
        resultado = normalizar_gabarito_organizador(dados, "K11", "001")
        self.assertEqual(resultado[1], "A")
        self.assertIsNone(resultado[11])

    def test_incompleto_ou_ocr_e_aceito_mas_precisa_conferir(self):
        self.assertEqual(len(normalizar_gabarito_organizador(payload(completo=False), "K11", "1")), 10)
        self.assertTrue(precisa_conferir(payload(completo=False)))
        self.assertTrue(precisa_conferir(payload(lido_por_ocr=True)))
        self.assertTrue(precisa_conferir(payload(faltando=[4])))
        self.assertFalse(precisa_conferir(payload()))

    def test_preliminar(self):
        self.assertTrue(preliminar(payload(situacao="preliminar")))
        self.assertFalse(preliminar(payload()))

    def test_rejeita_codigo_de_cargo_divergente(self):
        with self.assertRaisesRegex(ValueError, "código do cargo diverge"):
            normalizar_gabarito_organizador(payload(), "T18", "1")

    def test_rejeita_json_sem_codigo_de_cargo(self):
        with self.assertRaisesRegex(ValueError, "sem código de cargo"):
            normalizar_gabarito_organizador(payload(cargo_codigo=None), "K11", "1")

    def test_rejeita_tipo_divergente(self):
        with self.assertRaisesRegex(ValueError, "tipo de prova diverge"):
            normalizar_gabarito_organizador(payload(), "K11", "2")

    def test_rejeita_total_inconsistente(self):
        with self.assertRaisesRegex(ValueError, "total declarado"):
            normalizar_gabarito_organizador(payload(total=11), "K11", "1")

    def test_rejeita_letra_invalida(self):
        respostas = payload()["respostas"]
        respostas["10"] = "F"
        with self.assertRaisesRegex(ValueError, "resposta inválida"):
            normalizar_gabarito_organizador(payload(respostas=respostas), "K11", "1")

    def test_conferir_vale_o_pdf_e_avisa_o_que_diverge(self):
        org = {1: "A", 2: "C", 3: None, 4: "D"}
        gab, avisos = conferir_com_pdf(org, {1: "A", 2: "B", 3: "C", 5: "E"})
        self.assertEqual(gab, {1: "A", 2: "B", 3: "C", 4: "D", 5: "E"})
        self.assertEqual(avisos[2], ["gabarito do organizador diverge do PDF oficial (Q2: C vs B)"])
        self.assertEqual(avisos[3], ["gabarito do organizador diverge do PDF oficial (Q3: anulada vs C)"])
        self.assertIn("não conferido", avisos[4][0])
        self.assertNotIn(1, avisos)


class BaseEnvio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.rodar = importlib.import_module("rodar")
        except ImportError as e:  # máquina sem as dependências do robô
            raise unittest.SkipTest(f"rodar.py não importa aqui ({e})")

    def gabarito(self, dados, texto_pdf=PDF_FCC, ia=None):
        m = {"cargo_cod": "K11", "tipo": "1", "cargo": "Técnico TI", "gabarito_respostas": dados}
        log = []
        ia_mock = mock.Mock(return_value=("K11", ia or {}))
        with mock.patch.object(self.rodar, "_texto_pdf", return_value=texto_pdf), \
             mock.patch("ia_extrair.gabarito_por_ia", ia_mock):
            gab, avisos = self.rodar.gabarito_do_envio(m, b"%PDF", log)
        return gab, avisos, log, ia_mock


class TestGabaritoDoEnvio(BaseEnvio):
    def test_completo_e_por_texto_usa_direto_sem_ler_o_pdf(self):
        with mock.patch.object(self.rodar, "_texto_pdf", side_effect=AssertionError("não devia ler o PDF")):
            gab, avisos = self.rodar.gabarito_do_envio({"cargo_cod": "K11", "tipo": "1", "gabarito_respostas": payload()}, b"%PDF", [])
        self.assertEqual(gab, LETRAS)
        self.assertEqual(avisos, {})

    # 4) preliminar: aceito, mas toda questão vai para a fila com o aviso
    def test_preliminar_poe_aviso_em_todas(self):
        gab, avisos, log, _ = self.gabarito(payload(situacao="preliminar"))
        self.assertEqual(gab, LETRAS)
        self.assertEqual(set(avisos), set(LETRAS))
        self.assertTrue(all(AVISO_PRELIMINAR in a for a in avisos.values()))
        self.assertIn("PRELIMINAR", log[-1])

    # 5) OCR ou incompleto: confere com a leitura determinística do PDF; IA só se ela falhar
    def test_ocr_confere_com_o_pdf_e_avisa_a_divergente(self):
        respostas = {**payload()["respostas"], "7": "E"}  # o PDF diz B
        gab, avisos, log, ia = self.gabarito(payload(lido_por_ocr=True, respostas=respostas))
        ia.assert_not_called()
        self.assertEqual(gab[7], "B")  # vale o PDF oficial
        self.assertEqual(avisos, {7: ["gabarito do organizador diverge do PDF oficial (Q7: E vs B)"]})
        self.assertIn("conferido com o PDF oficial: 1 questão(ões)", log[-1])

    def test_incompleto_completa_pelo_pdf_sem_aviso(self):
        respostas = {k: v for k, v in payload()["respostas"].items() if k != "9"}
        respostas.update({str(n): "A" for n in range(11, 13)})  # 11 respostas, sem a 9
        gab, avisos, _, _ = self.gabarito(payload(completo=False, faltando=[9], respostas=respostas, total=11))
        self.assertEqual(gab[9], LETRAS[9])  # a que faltava vem do PDF
        self.assertIn("não conferido", avisos[11][0])  # o PDF não tem a 11: fica a do organizador, com aviso
        self.assertNotIn(9, avisos)

    def test_pdf_sem_texto_vai_para_a_ia(self):
        gab, avisos, log, ia = self.gabarito(payload(lido_por_ocr=True), texto_pdf="", ia=dict(LETRAS))
        ia.assert_called_once()
        self.assertEqual(avisos, {})
        self.assertTrue(any("gabarito lido pela IA" in x for x in log))

    # 6) JSON inválido: registra no log e lê o PDF, como antes
    def test_json_invalido_cai_para_a_leitura_do_pdf(self):
        gab, avisos, log, ia = self.gabarito(payload(cargo_codigo="T18"), ia=dict(LETRAS))
        self.assertTrue(log[0].startswith("JSON do organizador inválido (código do cargo diverge"))
        ia.assert_called_once()  # sem o JSON, o caminho antigo: CETAP e depois IA (sem leitor genérico)
        self.assertEqual(gab, LETRAS)
        self.assertEqual(avisos, {})


class FakeSupa:
    def __init__(self):
        self.upserts, self.updates = [], []

    def baixar_arquivo(self, bucket, caminho):
        return b"%PDF"

    def upsert(self, tabela, linhas, **k):
        self.upserts.append((tabela, linhas))
        return [{"id": 1}]

    def update(self, tabela, filtro, valores):
        self.updates.append((tabela, filtro, valores))

    def select(self, *a, **k):
        return []


class TestProcessarEnvio(BaseEnvio):
    def test_aviso_do_gabarito_chega_na_questao_da_fila(self):
        manifesto = {"envio": {"caderno": "envios/x/caderno.pdf", "gabarito": "envios/x/gabarito.pdf", "cargo": "Técnico TI", "cargo_cod": "K11",
                               "tipo": "1", "orgao": "TRT22", "ano": 2022, "gabarito_respostas": payload(situacao="preliminar")}}
        qs = [{"numero": n, "enunciado": f"Q{n}", "alternativas": dict(zip("ABCDE", "12345"))} for n in LETRAS]
        sup = FakeSupa()
        with mock.patch.object(self.rodar, "supa", sup), mock.patch.object(self.rodar, "extrair_questoes", return_value=qs), \
             mock.patch.object(self.rodar, "_texto_pdf", side_effect=AssertionError("não devia ler o PDF")):
            self.rodar.processar_envio({"id": "envio:x", "banca": "FCC", "cargos": json.dumps(manifesto), "concurso_alvo": "trt8-2026"})
        linhas = next(ls for t, ls in sup.upserts if t == "ingestao_questoes")
        self.assertEqual(len(linhas), 10)
        self.assertTrue(all(AVISO_PRELIMINAR in q["avisos"] for q in linhas))
        motivo = self.rodar.motivo_revisao({**linhas[0], "topico_sugerido": "x", "explicacao": "x" * 40, "status": "pendente"})
        self.assertTrue(motivo)  # o publicar não aprova sozinho


if __name__ == "__main__":
    unittest.main()
