"""Supabase instável (18/11/2026: um 522 do Cloudflare derrubou a rodada): nova tentativa com espera 2 s, 5 s e 15 s.
Idempotente (leitura, update, upsert com chave, delete, rpc) repete em conexão, timeout e 502/503/504/520-524; insert simples
só em conexão e 520-524 (o pedido não chegou ao banco). O log mostra uma linha curta, nunca a página HTML. Sem rede nem banco.

Rodar:  python -m unittest discover -s scripts/pipeline/tests
"""
import contextlib, importlib, io, os, sys, unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supa  # noqa: E402

PAGINA_CF = ("<!DOCTYPE html>\n<html><head><title>supabase.co | 522: Connection timed out</title><style>body{}</style></head>"
             "<body><div id='cf-wrapper'>" + "x" * 3000 + "</div></body></html>")


class R:
    def __init__(self, status=200, text="[]"):
        self.status_code, self.text = status, text

    def json(self):
        return []


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SUPABASE_URL": "https://exemplo.invalid", "SUPABASE_SERVICE_ROLE_KEY": "x"})
        self.env.start()
        os.environ.pop("ROBO_PRIVADO", None)
        importlib.reload(supa)
        self.sleep = mock.patch.object(supa.time, "sleep").start()
        self.out = io.StringIO()
        self._redir = contextlib.redirect_stdout(self.out)
        self._redir.__enter__()

    def tearDown(self):
        self._redir.__exit__(None, None, None)
        mock.patch.stopall()
        self.env.stop()
        importlib.reload(supa)

    def respostas(self, metodo, *seq):
        """requests.<metodo> devolve/levanta cada item de seq, em ordem."""
        return mock.patch(f"requests.{metodo}", side_effect=list(seq))


class TestIdempotentes(Base):
    def test_leitura_repete_no_522_e_segue(self):
        with self.respostas("get", R(522, PAGINA_CF), R(200)) as get:
            self.assertEqual(supa.select("questoes"), [])
        self.assertEqual(get.call_count, 2)
        self.sleep.assert_called_once_with(2)
        log = self.out.getvalue()
        self.assertIn("supa: GET questoes HTTP 522; nova tentativa 1/3 em 2s", log)
        self.assertNotIn("<html", log.lower())

    def test_espera_crescente_e_desiste_com_mensagem_curta(self):
        with self.respostas("patch", *[R(503, PAGINA_CF)] * 4) as patch:
            with self.assertRaises(RuntimeError) as cm:
                supa.update("ingestao_provas", {"id": "eq.1"}, {"erro": None})
        self.assertEqual(patch.call_count, 4)  # 1 + 3 novas tentativas
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2, 5, 15])
        self.assertEqual(str(cm.exception), "ingestao_provas: 503 supabase.co | 522: Connection timed out")
        self.assertNotIn("cf-wrapper", self.out.getvalue() + str(cm.exception))

    def test_todos_os_codigos_repetidos_na_leitura(self):
        for cod in (502, 503, 504, 520, 521, 522, 523, 524):
            with self.subTest(cod=cod), self.respostas("get", R(cod), R(200)) as get:
                supa.select("questoes")
                self.assertEqual(get.call_count, 2)

    def test_timeout_e_conexao_repetem_no_upsert_com_chave(self):
        with self.respostas("post", requests.ReadTimeout("lento"), requests.ConnectionError("caiu"), R(201)) as post:
            supa.upsert("ingestao_questoes", [{"id": "q1"}], on_conflict="id")
        self.assertEqual(post.call_count, 3)

    def test_upsert_sem_on_conflict_mas_com_id_e_idempotente(self):
        with self.respostas("post", R(503), R(201)) as post:
            supa.upsert("ingestao_concursos", [{"id": "fgv:x", "nome": "X"}])
        self.assertEqual(post.call_count, 2)

    def test_delete_e_rpc_repetem(self):
        with self.respostas("delete", R(504), R(204)) as d:
            supa.delete("textos", {"id": "eq.1"})
        with self.respostas("post", R(520), R(200, "1")) as p:
            self.assertEqual(supa.rpc("uso_storage_bytes", {}), [])
        self.assertEqual((d.call_count, p.call_count), (2, 2))

    def test_erro_do_pedido_nao_repete(self):
        with self.respostas("patch", R(400, '{"message":"coluna x não existe"}')) as patch:
            with self.assertRaises(RuntimeError) as cm:
                supa.update("ingestao_provas", {}, {"x": 1})
        self.assertEqual(patch.call_count, 1)
        self.assertIn("coluna x não existe", str(cm.exception))
        self.sleep.assert_not_called()

    def test_conexao_persistente_sobe_a_excecao(self):
        with self.respostas("get", *[requests.ConnectionError("caiu")] * 4) as get:
            with self.assertRaises(requests.ConnectionError):
                supa.select("questoes")
        self.assertEqual(get.call_count, 4)


class TestInsertSimples(Base):
    LINHA = [{"banca": "ROBO", "etapa": "rodada", "titulo": "x"}]  # robo_diagnostico: id serial, sem chave de conflito

    def test_repete_em_520_a_524(self):
        with self.respostas("post", R(522, PAGINA_CF), R(201)) as post:
            supa.upsert("robo_diagnostico", self.LINHA)
        self.assertEqual(post.call_count, 2)

    def test_repete_em_falha_de_conexao(self):
        with self.respostas("post", requests.ConnectionError("recusada"), R(201)) as post:
            supa.upsert("robo_diagnostico", self.LINHA)
        self.assertEqual(post.call_count, 2)

    def test_nao_repete_em_503_nem_timeout(self):
        # pode ter chegado ao banco: repetir duplicaria a linha
        with self.respostas("post", R(503)) as post:
            with self.assertRaises(RuntimeError):
                supa.upsert("robo_diagnostico", self.LINHA)
        self.assertEqual(post.call_count, 1)
        with self.respostas("post", requests.ReadTimeout("lento")) as post:
            with self.assertRaises(requests.ReadTimeout):
                supa.upsert("robo_diagnostico", self.LINHA)
        self.assertEqual(post.call_count, 1)
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
