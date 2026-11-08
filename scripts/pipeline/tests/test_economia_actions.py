"""Economia de minutos do Actions (01/11/2026: a cota de 2.000 min/mês acabou em 30/10): o processar não reabre os cadernos
guardados quando não há caderno esperando IA ou quando a cota diária dos modelos Flash já acabou; a cota do dia é lida de
robo_diagnostico (ia_modelos) desde a meia-noite do Pacífico. Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import json, os, sys, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_figuras_anuladas import Banco, BaseRodar  # noqa: E402

FLASH = "gemini-x-flash,gemini-y-flash"


class BancoProcessar(Banco):
    def __init__(self, provas, concursos):
        super().__init__({}); self.provas, self.concursos = provas, concursos

    def select(self, tabela, params=None, **k):
        self.selects.append((tabela, params))
        if tabela == "ingestao_provas":
            return [p for p in self.provas if (params or {}).get("status") != "eq.aguardando_ia" or p["status"] == "aguardando_ia"]
        if tabela == "ingestao_concursos":
            return list(self.concursos)
        return []


class TestProcessarEconomiza(BaseRodar):
    def rodar_processar(self, provas, concursos, com_ia=(True, "")):
        banco = BancoProcessar(provas, concursos)
        with mock.patch.object(self.rodar, "supa", banco), mock.patch.object(self.rodar, "ia_para_cadernos", return_value=com_ia), \
             mock.patch.object(self.rodar, "reabrir_aguardando") as reabrir, \
             mock.patch.object(self.rodar.aocp, "sessao", side_effect=AssertionError("não devia abrir sessão")) as sessao:
            try:
                self.rodar.processar()
            except AssertionError:
                pass
        return reabrir, sessao

    def test_cota_flash_esgotada_nao_reabre_nem_reprocessa_os_guardados(self):
        provas = [{"concurso_ref": "fgv:1", "status": "aguardando_ia", "pdf_path": "fgv/1.pdf"}]
        reabrir, sessao = self.rodar_processar(provas, [{"id": "fgv:1"}], com_ia=(False, "cota diária dos modelos Flash (transcrever) já esgotada"))
        reabrir.assert_not_called()
        sessao.assert_not_called()  # o único pendente era um guardado: nada a fazer, sai em segundos

    def test_sem_caderno_guardado_nao_reabre(self):
        reabrir, _ = self.rodar_processar([], [])
        reabrir.assert_not_called()

    def test_com_cota_e_caderno_guardado_reabre(self):
        provas = [{"concurso_ref": "fgv:1", "status": "aguardando_ia", "pdf_path": "fgv/1.pdf"}]
        reabrir, _ = self.rodar_processar(provas, [])
        reabrir.assert_called_once()

    def test_concurso_novo_continua_sem_cota(self):
        provas = [{"concurso_ref": "fgv:1", "status": "aguardando_ia", "pdf_path": "fgv/1.pdf"}]
        _, sessao = self.rodar_processar(provas, [{"id": "fgv:1"}, {"id": "aocp:9", "externo_id": "9"}], com_ia=(False, "sem IA"))
        sessao.assert_called_once()  # o AOCP novo é processado (sem IA ele vira questão pelo texto do PDF)


class TestCotaPesada(BaseRodar):
    def cota(self, linhas, fora=None):
        ia = self.rodar.ia
        sup = mock.Mock(select=mock.Mock(return_value=[{"detalhe": json.dumps(d)} for d in linhas]))
        with mock.patch.dict(os.environ, {"GEMINI_MODELOS_PESADO": FLASH, "GEMINI_MODEL": ""}), mock.patch.dict(sys.modules, {"supa": sup}), \
             mock.patch.dict(ia._modelo_fora, fora or {}, clear=True):
            return ia.cota_pesada_esgotada(), sup.select.call_args

    def test_todos_os_flash_com_20_pedidos_hoje(self):
        esgotada, chamada = self.cota([{"gemini-x-flash": 12, "gemini-x-flash-lite": 300}, {"gemini-x-flash": 8, "gemini-y-flash": 20}])
        self.assertTrue(esgotada)
        self.assertEqual(chamada.args[1]["etapa"], "eq.ia_modelos")
        self.assertTrue(chamada.args[1]["criado_em"].startswith("gte."))

    def test_um_flash_com_folga(self):
        self.assertFalse(self.cota([{"gemini-x-flash": 20, "gemini-y-flash": 5}])[0])

    def test_429_do_dia_nesta_execucao_conta(self):
        self.assertTrue(self.cota([{"gemini-x-flash": 20}], fora={"gemini-y-flash": "cota"})[0])

    def test_meia_noite_do_pacifico_com_horario_de_verao(self):
        ia = self.rodar.ia
        verao = ia.inicio_dia_pacifico(datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))
        inverno = ia.inicio_dia_pacifico(datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))
        self.assertIn(verao.hour, (7, 8))  # 07:00 UTC com tzdata; 08:00 no fallback (conta menos, nunca mais)
        self.assertEqual(inverno.hour, 8)
        self.assertEqual(ia.limite_dia("gemini-3.8-flash"), 20)
        self.assertEqual(ia.limite_dia("gemini-3.5-flash-lite"), 500)


class TestWorkflow(unittest.TestCase):
    """Workflows do robô PÚBLICO (tarlisodoria/fixaconcursos-robo). No repositório privado eles não existem: pula."""
    PASTA = Path(__file__).resolve().parents[3] / ".github" / "workflows"

    def setUp(self):
        if not (self.PASTA / "rapido.yml").exists():
            self.skipTest("workflows do robô público não estão nesta pasta")
        self.wfs = {p.name: p.read_text(encoding="utf-8") for p in self.PASTA.glob("*.yml")}

    def test_repeticoes_no_maximo_3_e_runner_fixo(self):
        wf = self.wfs["rapido.yml"]
        self.assertIn('{"1":[1],"2":[1,2],"3":[1,2,3]}', wf)
        self.assertNotIn('"4":', wf)
        for nome, texto in self.wfs.items():
            self.assertNotIn("ubuntu-latest", texto, nome)
            self.assertIn("ubuntu-24.04", texto, nome)

    def test_repositorio_publico_sem_gatilho_de_terceiros_e_sem_privado(self):
        for nome, texto in self.wfs.items():
            self.assertNotIn("pull_request", texto, nome)  # PR de fork nunca roda nada aqui
            self.assertNotIn("ROBO_PRIVADO", texto, nome)  # dado de aluno nunca no público
            if nome != "heartbeat.yml":
                self.assertNotIn("contents: write", texto, nome)
        self.assertNotIn("unittest discover", self.wfs["rapido.yml"])  # testes só no testes.yml
        self.assertIn('ROBO_PUBLICO: "1"', self.wfs["rapido.yml"])
        self.assertIn('ROBO_PUBLICO: "1"', self.wfs["pesado.yml"])

    def test_horario_vem_do_supabase_e_so_o_heartbeat_agenda(self):
        # pg_cron do Supabase dispara rapido.yml {"etapa":"rapido"} e pesado.yml {"etapa":"pesado"|"vigiar"} (migração
        # 20261108_robo_agendado_supabase.sql no privado): schedule aqui faria rodar em dobro
        for nome in ("rapido.yml", "pesado.yml"):
            wf = self.wfs[nome]
            self.assertNotRegex(wf, r"(?m)^\s*schedule:", nome)
            self.assertIn("workflow_dispatch:", wf, nome)
            self.assertRegex(wf, r"(?m)^\s{6}etapa:", nome)  # input que o cron manda
        self.assertIn("'rapido'", self.wfs["rapido.yml"])
        self.assertIn("vigiar", self.wfs["pesado.yml"].split("etapa:")[1])
        self.assertRegex(self.wfs["heartbeat.yml"], r"(?m)^\s*schedule:")


if __name__ == "__main__":
    unittest.main()
