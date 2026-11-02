"""Dois repositórios (01/11/2026): o robô roda num repositório PÚBLICO (log do Actions é público). Sem ROBO_PRIVADO=1 ele só
toca tabelas de conteúdo público; as etapas com dado de aluno (alunos, planos) e o exportar são recusadas ou ficam de fora; e
nenhum log imprime id de usuário. Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import contextlib, importlib, io, os, subprocess, sys, unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AQUI))

import supa  # noqa: E402

USUARIO = "6f1c2a9e-0000-4000-8000-1234567890ab"


class Resp:
    status_code, text = 200, "[]"

    def json(self):
        return []

    def raise_for_status(self):
        pass


class TestSupaSoConteudoPublico(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SUPABASE_URL": "https://exemplo.invalid", "SUPABASE_SERVICE_ROLE_KEY": "x"})
        self.env.start()
        os.environ.pop("ROBO_PRIVADO", None)
        importlib.reload(supa)

    def tearDown(self):
        self.env.stop()
        importlib.reload(supa)

    def test_tabela_de_aluno_bloqueada_antes_de_ir_a_rede(self):
        with mock.patch("requests.get") as get, mock.patch("requests.post") as post, mock.patch("requests.patch") as patch:
            for chamada in (lambda: supa.select("perfis"), lambda: supa.select("tentativas"), lambda: supa.upsert("avisos", [{"a": 1}]),
                            lambda: supa.update("pedidos_plano", {}, {}), lambda: supa.select("assinaturas"),
                            lambda: supa.select("concurso_seguidores"), lambda: supa.rpc("acesso_de", {})):
                with self.assertRaises(supa.DadoDeAluno):
                    chamada()
        get.assert_not_called(); post.assert_not_called(); patch.assert_not_called()

    def test_tabela_nova_fica_bloqueada_ate_entrar_na_lista(self):
        with self.assertRaises(supa.DadoDeAluno):
            supa.select("tabela_que_alguem_criou_amanha")

    def test_conteudo_publico_passa(self):
        with mock.patch("requests.get", return_value=Resp()) as get:
            self.assertEqual(supa.select("questoes"), [])
        get.assert_called_once()

    def test_privado_libera(self):
        os.environ["ROBO_PRIVADO"] = "1"
        with mock.patch("requests.get", return_value=Resp()):
            self.assertEqual(supa.select("assinaturas"), [])


class BaseRodar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.rodar = importlib.import_module("rodar")
        except ImportError as e:  # máquina sem as dependências do robô
            raise unittest.SkipTest(f"rodar.py não importa aqui ({e})")


class TestEtapasSoNoPrivado(BaseRodar):
    def test_etapa_avulsa_recusada_com_mensagem_clara(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ROBO_PRIVADO", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
        for etapa in ("alunos", "planos", "exportar"):
            r = subprocess.run([sys.executable, str(AQUI / "rodar.py"), etapa], capture_output=True, text=True, encoding="utf-8",
                               env={**env, "PYTHONUTF8": "1"}, timeout=120)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(f"etapa '{etapa}' só roda no repositório privado", r.stderr)

    def test_sequencia_publica_pula_exportar(self):
        feitas = []
        falsas = {e: (lambda e=e: feitas.append(e)) for e in ("publicar", "exportar", "alunos")}
        with mock.patch.object(self.rodar.supa, "privado", return_value=False), mock.patch.object(self.rodar.supa, "upsert"), \
             mock.patch.object(self.rodar.ia, "resumo"), mock.patch.dict(self.rodar.ETAPAS, falsas), \
             contextlib.redirect_stdout(io.StringIO()) as saida:
            self.rodar.rodar_sequencia(["publicar", "alunos", "exportar"], "teste")
        self.assertEqual(feitas, ["publicar"])
        self.assertIn("alunos, exportar fica(m) de fora", saida.getvalue())

    def test_alunos_fora_do_privado_sai(self):
        with mock.patch.object(self.rodar.supa, "privado", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                self.rodar.alunos()
        self.assertIn("só roda no repositório privado", str(ctx.exception))

    def test_vigiar_nao_toca_dado_de_aluno(self):
        with mock.patch.object(self.rodar, "_vigiar"), mock.patch.object(self.rodar, "noticiar"), \
             mock.patch.object(self.rodar, "avisar_mensalidades") as mens:
            self.rodar.vigiar()
        mens.assert_not_called()


class FakeSupa:
    def __init__(self, tabelas):
        self.tabelas, self.upserts = tabelas, []

    def select(self, tabela, params=None):
        return self.tabelas.get(tabela, [])

    def upsert(self, tabela, linhas, **k):
        self.upserts.append((tabela, linhas))


class TestAlunosNoPrivado(BaseRodar):
    def test_avisa_seguidores_das_noticias_novas_e_marca_a_rodada(self):
        novas = [{"id": "n1", "tipo": "gabarito", "titulo": "Gabarito publicado", "concurso_ref": "aocp:1"}]
        sup = FakeSupa({"robo_diagnostico": [{"criado_em": "2026-11-01T10:00:00+00:00"}], "noticias": novas})
        with mock.patch.object(self.rodar, "supa", sup), mock.patch.object(self.rodar.noticias, "avisar_seguidores") as avisar:
            self.rodar.avisar_seguidores_desde_ultima()
        avisar.assert_called_once_with(novas)
        self.assertEqual(sup.upserts[-1][1][0]["etapa"], "alunos")

    def test_log_dos_avisos_nao_imprime_id_de_usuario(self):
        N = self.rodar.noticias
        sup = FakeSupa({"vitrine_concursos": [{"id": "aocp:1", "slug": "ifpa"}],
                        "concurso_seguidores": [{"user_id": USUARIO, "concurso_id": "aocp:1"}]})
        with mock.patch.object(N, "supa", sup), contextlib.redirect_stdout(io.StringIO()) as saida:
            N.avisar_seguidores([{"id": "n1", "tipo": "gabarito", "titulo": "Gabarito", "concurso_ref": "aocp:1"}])
        self.assertIn("1 aviso(s) para 1 pessoa(s)", saida.getvalue())
        self.assertNotIn(USUARIO, saida.getvalue())


if __name__ == "__main__":
    unittest.main()
