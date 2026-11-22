"""Fila 'aguardando_ia' e vigia (18/11/2026). Nenhuma prova nova desde 05/11 e 235 provas paradas, 67 só esperando o gabarito:
  - resumo de toda rodada por banca ("vigiar <banca>: lidos N, novos N, erros N") e aviso no Painel após 7 dias sem prova nova;
  - reabertos da prova mais antiga para a mais nova, e nunca marcados processados sem tentativa quando a lista pesada sai
    do ar no meio da rodada (antes ja_feitas os dava como resolvidos e eles iam para o fim do rodízio);
  - gabarito FGV em layout diferente lido à parte, pela lista LEVE, e reaproveitado na transcrição.
Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import contextlib, importlib, io, sys, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GAB80 = {n: "ABCDE"[n % 5] for n in range(1, 81)}


class Supa:
    """supa simulado: select devolve o que o teste mandar; update/upsert ficam registrados."""
    def __init__(self, select=None):
        self._select, self.updates, self.upserts = select or (lambda t, p: []), [], []

    def select(self, tabela, params=None):
        return self._select(tabela, params or {})

    def update(self, tabela, filtro, valores):
        self.updates.append((tabela, filtro, valores))

    def upsert(self, tabela, linhas, **k):
        self.upserts.append((tabela, linhas))
        return [{"id": "p1"}]

    def baixar_arquivo(self, bucket, caminho):
        return b"%PDF caderno"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.rodar = importlib.import_module("rodar")
        except ImportError as e:  # máquina sem as dependências do robô
            raise unittest.SkipTest(f"rodar.py não importa aqui ({e})")

    def rodar_calado(self, fn, *a, **k):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = fn(*a, **k)
        return r, out.getvalue()


class TestVigiar(Base):
    def test_resumo_por_banca_mesmo_com_banca_fora_do_ar(self):
        def banca(nome, mod, interesse_todos=False):
            if nome == "AOCP":
                raise RuntimeError("403 Client Error: Forbidden")
            return {"CEBRASPE": (500, 0), "FGV": (339, 1)}[nome]
        with mock.patch.object(self.rodar, "vigiar_banca", side_effect=banca), mock.patch("traceback.print_exc"):
            _, log = self.rodar_calado(self.rodar._vigiar)
        self.assertIn("vigiar AOCP: lidos 0, novos 0, erros 1", log)
        self.assertIn("vigiar CEBRASPE: lidos 500, novos 0, erros 0", log)
        self.assertIn("vigiar FGV: lidos 339, novos 1, erros 0", log)

    def test_vigiar_banca_devolve_lidos_e_novos(self):
        mod = mock.Mock()
        mod.listar.return_value = [{"id": "A_26", "nome": "A"}, {"id": "B_26", "nome": "B"}]
        sup = Supa(lambda t, p: [{"id": "cebraspe:A_26"}])
        with mock.patch.object(self.rodar, "supa", sup):
            r, _ = self.rodar_calado(self.rodar.vigiar_banca, "CEBRASPE", mod)
        self.assertEqual(r, (2, 1))


class TestAvisoSemProvaNova(Base):
    def chamar(self, ultima, hoje):
        sup = Supa(lambda t, p: [{"criado_em": ultima}])
        with mock.patch.object(self.rodar, "supa", sup), mock.patch.object(self.rodar.ia, "falha") as falha:
            dias, log = self.rodar_calado(self.rodar.avisar_sem_prova_nova, hoje)
        return dias, falha, log

    def test_sete_dias_sem_prova_vira_aviso_no_painel(self):
        dias, falha, log = self.chamar("2026-11-05T18:30:53.560027+00:00", datetime(2026, 11, 18, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(dias, 13)
        falha.assert_called_once()
        prov, cod, msg = falha.call_args.args
        self.assertEqual((prov, cod), ("robô", "sem prova nova"))  # card "Falhas de IA": "robô" em negrito + "sem prova nova"
        self.assertIn("desde 05/11 (13 dias)", msg)
        self.assertIn("AVISO", log)

    def test_prova_recente_sem_aviso(self):
        dias, falha, _ = self.chamar("2026-11-16T10:00:00+00:00", datetime(2026, 11, 18, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(dias, 2)
        falha.assert_not_called()

    def test_banco_fora_nao_derruba_o_auditar(self):
        sup = Supa(lambda t, p: (_ for _ in ()).throw(RuntimeError("ingestao_provas: 522 supabase.co | 522")))
        with mock.patch.object(self.rodar, "supa", sup):
            dias, log = self.rodar_calado(self.rodar.avisar_sem_prova_nova)
        self.assertIsNone(dias)
        self.assertIn("não conferido", log)


class TestOrdemDaFila(Base):
    def test_reabertos_no_fim_da_prova_mais_antiga_para_a_mais_nova(self):
        resto = [{"id": "fgv:nova26"}, {"id": "fgv:recente"}, {"id": "aocp:9"}, {"id": "fgv:velha"}]
        sup = Supa(lambda t, p: [{"id": "envio:1"}] if p.get("id") == "like.envio:*" else resto)
        guardados = {"fgv:recente": "2026-11-03T18:22:03+00:00", "fgv:velha": "2026-10-15T10:00:00+00:00"}
        with mock.patch.object(self.rodar, "supa", sup):
            fila = self.rodar.fila_processar(20, guardados)
        self.assertEqual([c["id"] for c in fila], ["envio:1", "fgv:nova26", "aocp:9", "fgv:velha", "fgv:recente"])

    def test_aguardando_com_pdf_guarda_a_prova_mais_antiga(self):
        linhas = [{"concurso_ref": "fgv:x", "criado_em": "2026-10-26"}, {"concurso_ref": "fgv:x", "criado_em": "2026-10-16"},
                  {"concurso_ref": "cebraspe:y", "criado_em": "2026-10-29"}]
        with mock.patch.object(self.rodar, "supa", Supa(lambda t, p: linhas)):
            self.assertEqual(self.rodar.aguardando_com_pdf(), {"fgv:x": "2026-10-16", "cebraspe:y": "2026-10-29"})

    def test_reaberto_nao_e_gasto_quando_a_lista_pesada_sai_do_ar(self):
        r = self.rodar
        sup = Supa()
        fila = [{"id": "fgv:novo", "banca": "FGV"}, {"id": "fgv:velha", "banca": "FGV"}]
        with mock.patch.object(r, "supa", sup), mock.patch.object(r, "ler_gabaritos_guardados"), mock.patch.object(r, "resumo_fila"), \
             mock.patch.object(r, "aguardando_com_pdf", return_value={"fgv:velha": "2026-10-15"}), \
             mock.patch.object(r, "ia_para_cadernos", return_value=(True, "")), mock.patch.object(r, "reabrir_aguardando"), \
             mock.patch.object(r, "fila_processar", return_value=fila), mock.patch.object(r.aocp, "sessao"), \
             mock.patch.object(r, "tempo_esgotado", return_value=False), mock.patch.object(r, "rodada_cheia", return_value=False), \
             mock.patch.object(r.ia, "disponivel", side_effect=lambda t=None: t != "transcrever"), \
             mock.patch.object(r.ia, "motivo_indisponivel", return_value="cota diária de todos os modelos pesados esgotada; tenta amanhã"), \
             mock.patch.object(r, "processar_fgv") as pfgv:
            _, log = self.rodar_calado(r.processar)
        self.assertEqual([c.args[0]["id"] for c in pfgv.call_args_list], ["fgv:novo"])  # concurso novo segue (pode só guardar)
        self.assertNotIn({"id": "eq.fgv:velha"}, [f for _, f, _ in sup.updates])  # nem tentado_em nem processado_em
        self.assertIn("ficam para a próxima", log)


class TestGabaritoPelaListaLeve(Base):
    def test_gabarito_e_tarefa_leve(self):
        self.assertEqual(self.rodar.ia.classe_gemini("gabarito"), "leve")
        self.assertEqual(self.rodar.ia.classe_gemini("transcrever caderno (FGV)"), "pesado")

    def test_le_so_os_gabaritos_mais_antigos_primeiro_e_grava_o_motivo_real(self):
        from ia_extrair import SemIA
        r = self.rodar
        provas = [{"id": f"p{i}", "concurso_ref": "fgv:x", "cargo_cod": f"cargo-{i}", "cargo_nome": f"Cargo {i}", "tipo_prova": "1",
                   "origem": {"url": "c.pdf", "gabarito_url": "https://conhecimento.fgv.br/g.pdf"}} for i in (1, 2, 3)]
        pedidos = []
        sup = Supa(lambda t, p: pedidos.append(p) or provas)
        ia_leve = mock.Mock(side_effect=[("Cargo 1", GAB80), ("?", {}), SemIA("Gemini sobrecarregado (503): tenta na próxima rodada")])
        with mock.patch.object(r, "supa", sup), mock.patch.object(r.fgv, "sessao"), mock.patch.object(r.fgv, "baixar", return_value=b"%PDF") as baixar, \
             mock.patch.object(r.fgv, "pagina", return_value=("X", [("https://conhecimento.fgv.br/concursos/x", "")])) as pagina, \
             mock.patch.object(r, "_doc_gabarito_fgv", return_value=(b"%PDF", ["texto"], {})), \
             mock.patch.object(r, "tempo_esgotado", return_value=False), mock.patch.object(r.ia, "disponivel", return_value=True), \
             mock.patch("ia_extrair.gabarito_por_ia", ia_leve):
            lidos, log = self.rodar_calado(r.ler_gabaritos_guardados)
        self.assertEqual(lidos, 1)
        self.assertEqual(pedidos[0]["order"], "criado_em.asc")
        self.assertEqual(pedidos[0]["gabarito"], "is.null")
        baixar.assert_called_once()  # o mesmo gabarito definitivo serve aos três cargos
        pagina.assert_called_once()  # a página do concurso também
        upd = {f["id"]: v for _, f, v in sup.updates}
        self.assertEqual(upd["eq.p1"]["n_questoes"], 80)
        self.assertEqual(upd["eq.p1"]["gabarito"]["1"], "B")
        self.assertIn("falta transcrever", upd["eq.p1"]["erro"])
        # PDF sem texto e IA com 0 respostas: não prova que o cargo falta, fica na fila
        self.assertNotIn("status", upd["eq.p2"])
        self.assertIn("IA leu 0 respostas", upd["eq.p2"]["erro"])
        self.assertNotIn("status", upd["eq.p3"])
        self.assertIn("Gemini sobrecarregado (503)", upd["eq.p3"]["erro"])  # motivo atual no lugar do antigo
        self.assertIn("gabaritos: 1 lido(s) de 3", log)

    def test_sem_ia_leve_nao_faz_nada(self):
        with mock.patch.object(self.rodar.ia, "disponivel", return_value=False), \
             mock.patch.object(self.rodar, "supa", Supa(lambda t, p: self.fail("não devia consultar"))):
            self.assertEqual(self.rodar.ler_gabaritos_guardados(), 0)

    def test_transcricao_reaproveita_o_gabarito_ja_lido(self):
        r = self.rodar
        guardado = {str(n): v for n, v in GAB80.items()}
        antiga = {"cargo_cod": "analista-de-sistemas", "status": "aguardando_ia", "erro": "x", "pdf_path": "fgv/x.pdf", "gabarito": guardado}
        cad = {"cod": "analista-de-sistemas", "cargo": "Analista de Sistemas", "arquivo": "analista de sistemas", "url": "https://conhecimento.fgv.br/c.pdf",
               "tipo": "1", "parte": "https://conhecimento.fgv.br/concursos/x"}
        qs = [{"numero": n, "enunciado": f"Q{n}", "alternativas": dict(zip("ABCDE", "12345"))} for n in GAB80]
        sup = Supa()
        with mock.patch.object(r, "supa", sup), mock.patch.object(r.fgv, "sessao"), \
             mock.patch.object(r.fgv, "pagina", return_value=("Concurso X", [(cad["parte"], "")])), \
             mock.patch.object(r.fgv, "cadernos", return_value=[cad]), mock.patch.object(r.fgv, "gabaritos_definitivos", return_value=["https://conhecimento.fgv.br/g.pdf"]), \
             mock.patch.object(r.fgv, "baixar", side_effect=AssertionError("não devia baixar nada")), \
             mock.patch.object(r, "provas_do", return_value=[antiga]), mock.patch.object(r, "tempo_esgotado", return_value=False), \
             mock.patch.object(r, "rodada_cheia", return_value=False), mock.patch.object(r.ia, "disponivel", return_value=True), \
             mock.patch.object(r, "_gabarito_fgv", side_effect=AssertionError("não devia ler o gabarito de novo")), \
             mock.patch("ia_extrair.extrair_por_ia", return_value=(None, qs, "end_turn")):
            self.rodar_calado(r.processar_fgv, {"id": "fgv:x", "externo_id": "x", "nome": "Concurso X", "filtro_cargos": "sistemas"})
        prova = next(ls for t, ls in sup.upserts if t == "ingestao_provas")[0]
        self.assertEqual(prova["gabarito"], guardado)
        questoes = next(ls for t, ls in sup.upserts if t == "ingestao_questoes")
        self.assertEqual(len(questoes), 80)


class TestResumoFila(Base):
    def test_estimativa_de_dias_com_a_cota_pesada(self):
        r = self.rodar
        with mock.patch.object(r, "supa", Supa(lambda t, p: [{"id": i} for i in range(235)])), \
             mock.patch.object(r.ia, "modelos_gemini", return_value=["gemini-3.8-flash", "gemini-3.6-flash"]):
            _, log = self.rodar_calado(r.resumo_fila)
        self.assertIn("fila IA: 235 prova(s) esperando; cota pesada 40 pedidos/dia (2 modelo(s)", log)
        self.assertIn("~6 dia(s) para zerar", log)


if __name__ == "__main__":
    unittest.main()
