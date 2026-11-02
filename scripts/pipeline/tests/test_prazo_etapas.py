"""Orçamento de tempo por etapa no job rápido (28/10: o enriquecer levou 12 min e o job pulou publicar e exportar):
publicar, classificar_local e exportar nunca são pulados; cada etapa de IA para no seu orçamento; o conferente troca de
modelo depois de 2 respostas 503. Relógio simulado, sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import os, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_figuras_anuladas import BaseRodar  # noqa: E402
from test_gemini_sobrecarga import R503, Gemini  # noqa: E402
from test_ia_reserva import gemini_ok  # noqa: E402

T0 = 1_000_000.0


class TestPrazoDasEtapas(BaseRodar):
    def rodar_rapido(self, etapas):
        """Roda a sequência do job rápido com as etapas trocadas por `etapas` (nome -> função) e um relógio simulado."""
        self.relogio = [T0]
        chamadas = []

        def registrar(nome, f):
            def etapa():
                chamadas.append(nome)
                f()
            return etapa
        falsas = {n: registrar(n, etapas.get(n, lambda: None)) for n in set(self.rodar.RAPIDO)}
        with mock.patch("time.time", side_effect=lambda: self.relogio[0]), \
             mock.patch.object(self.rodar, "INICIO_JOB", T0), mock.patch.object(self.rodar, "PRAZO_JOB", 16 * 60), \
             mock.patch.object(self.rodar, "supa", mock.Mock()), mock.patch.object(self.rodar.ia, "resumo"), \
             mock.patch.dict(self.rodar.ETAPAS, falsas), mock.patch.dict(os.environ, {}, clear=False):
            for n in self.rodar.ORCAMENTO_MIN:
                os.environ.pop(f"PRAZO_{n.upper()}_MIN", None)
            self.rodar._tempos.clear()
            try:
                self.rodar.rodar_sequencia(self.rodar.RAPIDO, "teste")
            finally:
                self.prazo_depois = self.rodar.ia.PRAZO_FINAL
        return chamadas

    def lenta(self, passo=30):
        """Etapa de IA que só para quando o ia.py diz que o prazo acabou (como os laços do Gemini)."""
        def f():
            while not self.rodar.ia.sem_tempo(passo):  # cada "pedido" leva `passo` s: não começa um que passe do prazo
                self.relogio[0] += passo
        return f

    def test_enriquecer_lento_para_no_orcamento_e_publicar_e_exportar_rodam(self):
        ias = ("enriquecer", "figuras", "classificar_fila", "classificar", "auditar")
        chamadas = self.rodar_rapido({n: self.lenta() for n in ias})
        # 6 + 3 + 2 + 3 = 14 min: o auditar não cabe antes da reserva de 2 min do exportar
        self.assertEqual(chamadas, ["enriquecer", "publicar", "figuras", "publicar", "classificar_local", "classificar_fila",
                                    "classificar", "exportar"])
        tempos = {t["etapa"]: t for t in self.rodar._tempos if not t.get("pulada")}
        self.assertEqual(tempos["enriquecer"]["segundos"], 360)
        self.assertEqual(tempos["enriquecer"]["orcamento_s"], 360)
        self.assertEqual(tempos["figuras"]["segundos"], 180)
        self.assertEqual([t["etapa"] for t in self.rodar._tempos if t.get("pulada")], ["auditar"])
        self.assertEqual(self.prazo_depois, T0 + 16 * 60)  # fora das etapas, o prazo do ia.py volta a ser o do job

    def test_enriquecer_que_passa_do_prazo_do_job_nao_impede_as_obrigatorias(self):
        def travou():  # uma chamada só que levou 20 min (não volta ao laço para ver o prazo)
            self.relogio[0] += 20 * 60
        chamadas = self.rodar_rapido({"enriquecer": travou})
        self.assertEqual(chamadas, ["enriquecer", "publicar", "publicar", "classificar_local", "exportar"])

    def test_erro_numa_etapa_nao_impede_as_seguintes(self):
        def quebra():
            raise RuntimeError("caiu")
        with self.assertRaises(RuntimeError):  # o job fica vermelho no fim...
            self.rodar_rapido({"enriquecer": quebra})
        chamadas = [t["etapa"] for t in self.rodar._tempos]  # ...mas as outras etapas rodaram
        self.assertEqual(chamadas[-1], "exportar")
        self.assertIn("publicar", chamadas)

    def test_orcamento_pela_variavel(self):
        vistos = []
        with mock.patch.dict(os.environ, {"PRAZO_ENRIQUECER_MIN": "1"}):
            self.assertEqual(self.rodar.orcamento("enriquecer"), 60)
        self.assertEqual(self.rodar.orcamento("auditar"), 120)
        self.assertIsNone(self.rodar.orcamento("publicar"))
        self.rodar_rapido({"figuras": lambda: vistos.append(self.rodar.ia.PRAZO_FINAL - self.relogio[0])})
        self.assertEqual(vistos, [180])

    def test_obrigatorias_e_ordem(self):
        r = self.rodar.RAPIDO
        self.assertEqual(r[:4], ["enriquecer", "publicar", "figuras", "publicar"])
        self.assertEqual(r[-1], "exportar")
        self.assertEqual(self.rodar.OBRIGATORIAS, {"publicar", "classificar_local", "exportar"})


class TestConferenteTrocaDepoisDe2x503(Gemini):
    def test_dois_503_do_gemma_e_vai_ao_proximo(self):
        os.environ["GEMINI_MODELOS_CONFERENCIA"] = "gemma-x,gemini-x-flash-lite"
        with mock.patch("requests.post", side_effect=[R503, R503, gemini_ok("conferido")]) as post:
            txt = self.ia.conferir("enriquecer", "x", escritor="gemini-y-flash-lite")
        self.assertEqual(txt, "conferido")
        self.assertEqual(self.modelos(post), ["gemma-x", "gemma-x", "gemini-x-flash-lite"])
        self.assertEqual(self.ia.ultimo_conferente, "gemini-x-flash-lite")


if __name__ == "__main__":
    unittest.main()
