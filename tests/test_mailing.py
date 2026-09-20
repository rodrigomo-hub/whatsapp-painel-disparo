import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

import main
import vendeai_mailing as mailing


@contextmanager
def test_db():
    with closing(main.get_db()) as conn:
        with conn:
            yield conn


class MailingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = patch.object(main, "DB_FILE", Path(self.temp.name) / "test.db")
        self.db.start()
        self.addCleanup(self.db.stop)
        main.init_db()
        self.token_env = patch.object(main, "VENDEAI_API_TOKEN", "")
        self.token_env.start()
        self.addCleanup(self.token_env.stop)
        main.sessoes_ativas.add("test-session")
        self.addCleanup(main.sessoes_ativas.discard, "test-session")
        self.client = TestClient(main.app)  # Sem startup/background ou chamadas reais.
        self.addCleanup(self.client.close)
        self.client.cookies.set("session_token", "test-session")
        with test_db() as conn:
            conn.execute("UPDATE configuracao SET vendeai_habilitado=1, vendeai_account_id='33393', vendeai_mailing_token='test-secret'")
            conn.execute("INSERT INTO numeros(nome, phone_number_id, access_token) VALUES ('Teste', 'fake-id', 'fake-token')")
        self.post = patch.object(mailing.requests, "post")
        self.api = self.post.start()
        self.addCleanup(self.post.stop)
        self.api.return_value = self.resposta(1)
        self.whatsapp_patch = patch.object(main, "enviar_whatsapp", return_value=(True, "", "fake-wamid"))
        self.whatsapp = self.whatsapp_patch.start()
        self.addCleanup(self.whatsapp_patch.stop)
        self.qualidade = patch.object(main, "verificar_qualidade_pos_envio", return_value="GREEN")
        self.qualidade.start()
        self.addCleanup(self.qualidade.stop)
        self.lead = {"telefone": "11987654321", "cpf": "11144477735", "nome": "Lead de teste",
                     "nascimento": "1992-11-23", "email": "teste@example.com", "produto": "clt",
                     "campanha": "teste", "nomeMae": "Nao deve ser enviado", "variaveis_template": ["Teste"]}

    @staticmethod
    def resposta(total, falhas=None):
        falhas = falhas or []
        return Mock(status_code=200, headers={}, json=Mock(return_value={
            "success": True, "success_count_mailings": total - len(falhas),
            "fail_count_mailings": len(falhas), "failing_reasons": falhas,
        }))

    def test_individual_cadastra_antes_de_disparar(self):
        ordem = []
        self.api.side_effect = lambda *a, **kw: (ordem.append("cadastro"), self.resposta(1))[1]
        self.whatsapp.side_effect = lambda *a: (ordem.append("disparo"), (True, "", "wamid"))[1]
        res = self.client.post("/disparar", json=self.lead)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "sucesso")
        self.assertEqual(ordem, ["cadastro", "disparo"])
        args, kw = self.api.call_args
        self.assertEqual(args[0], mailing.MAILING_URL + mailing.MAILING_PATH)
        self.assertEqual(kw["json"], {"account_id": "33393", "crm_api_access_token": "test-secret", "leads": [{
            "phone": "5511987654321", "cpf": "11144477735", "name": "Lead de teste",
            "birth_date": "23/11/1992", "email": "teste@example.com", "product": "clt", "campaign": "teste"}]})
        self.assertFalse(kw["allow_redirects"])
        self.assertEqual(res.json()["vendeai"]["validade_dias"], 7)

    def test_compatibilidade_entrada_antiga_e_defaults(self):
        res = self.client.post("/disparar", json={"whatsapp": "5511987654321", "nome": "Teste"})
        self.assertEqual(res.json()["status"], "sucesso")
        enviado = self.api.call_args.kwargs["json"]["leads"][0]
        self.assertRegex(enviado["email"], r"^lead[0-9a-f]{24}@gmail\.com$")
        self.assertEqual({k: v for k, v in enviado.items() if k != "email"}, {"phone": "5511987654321", "name": "Teste", "product": "clt"})

    def test_email_ausente_vazio_ou_nulo_gera_aleatorio(self):
        cfg = main.carregar_configuracao()
        emails = []
        for valor in (None, "", "   "):
            lead = mailing.preparar_lead({"telefone": "11987654321", "email": valor}, cfg)["mailing"]
            self.assertRegex(lead["email"], r"^lead[0-9a-f]{24}@gmail\.com$")
            emails.append(lead["email"])
        self.assertEqual(len(set(emails)), 3)

    def test_email_info_extra_preservado(self):
        lead = mailing.preparar_lead({"telefone": "11987654321", "email": " ", "info_extra": {"email": "real@example.com"}}, main.carregar_configuracao())["mailing"]
        self.assertEqual(lead["email"], "real@example.com")

    def test_lote_gera_email_apenas_para_quem_nao_informou(self):
        self.api.return_value = self.resposta(2)
        self.client.post("/api/disparo-lote", json={"leads": [self.lead, {"telefone": "11987654322"}]})
        enviados = self.api.call_args.kwargs["json"]["leads"]
        self.assertEqual(enviados[0]["email"], "teste@example.com")
        self.assertRegex(enviados[1]["email"], r"^lead[0-9a-f]{24}@gmail\.com$")

    def test_aliases_data_e_ddd_55(self):
        cfg = main.carregar_configuracao()
        preparado = mailing.preparar_lead({"phone": "55987654321", "name": "Teste", "birth_date": "23/11/1992", "product": "fgts", "campaign": "x"}, cfg)
        self.assertEqual(preparado["mailing"]["phone"], "5555987654321")
        self.assertEqual(preparado["mailing"]["birth_date"], "23/11/1992")
        self.assertEqual(preparado["mailing"]["product"], "fgts")

    def test_dados_info_extra_aceitos_com_lista_permitida(self):
        cfg = main.carregar_configuracao()
        lead = mailing.preparar_lead({"telefone": "11987654321", "info_extra": {"nascimento": "1992-11-23", "nomeMae": "Nao enviar"}}, cfg)["mailing"]
        self.assertEqual(lead["birth_date"], "23/11/1992")
        self.assertNotIn("nomeMae", lead)

    def test_lote_parcial_envia_somente_aceitos(self):
        self.api.return_value = self.resposta(2, [{"index": 1, "reason": "INVALID_CPF"}])
        res = self.client.post("/api/disparo-lote", json={"leads": [self.lead, {**self.lead, "telefone": "11987654322", "cpf": "invalido"}]}).json()
        self.assertEqual([r["status"] for r in res["resultados"]], ["sucesso", "pendente"])
        self.whatsapp.assert_called_once()
        self.assertEqual(self.whatsapp.call_args.args[2], ["Teste"])
        self.api.assert_called_once()
        self.assertEqual(res["resultados"][1]["vendeai"]["motivo"], "INVALID_CPF")
        with test_db() as conn:
            row = conn.execute("SELECT cpf, nome_lead FROM disparos").fetchone()
            self.assertEqual(tuple(row), ("11144477735", "Lead de teste"))

    def test_lote_antigo_deduplica_e_agrupa(self):
        self.api.return_value = self.resposta(2)
        res = self.client.post("/api/disparo-lote", json={"numeros_texto": "11987654321\n5511987654321;11987654322"}).json()
        self.assertEqual(res["total"], 2)
        self.api.assert_called_once()
        self.assertEqual(self.whatsapp.call_count, 2)

    def test_lote_invalido_local_nao_desloca_indices_api(self):
        self.api.return_value = self.resposta(2, [{"index": 1, "reason": "INVALID_EMAIL"}])
        res = self.client.post("/api/disparo-lote", json={"leads": [{"telefone": "12"}, self.lead, {"telefone": "11987654322", "email": "ruim"}]}).json()
        self.assertEqual([r["status"] for r in res["resultados"]], ["invalido", "sucesso", "pendente"])
        self.assertEqual(len(self.api.call_args.kwargs["json"]["leads"]), 2)

    def test_falhas_bloqueiam_whatsapp_e_sms(self):
        for tipo in (403, 429, 500):
            with self.subTest(status=tipo), patch.object(main, "tentar_fallback_sms") as sms:
                self.api.return_value = Mock(status_code=tipo, headers={"Retry-After": "120"})
                res = self.client.post("/disparar", json=self.lead).json()
                self.assertEqual(res["status"], "pendente")
                self.whatsapp.assert_not_called()
                sms.assert_not_called()
                if tipo == 429:
                    self.assertEqual(res["vendeai"]["tentar_apos_segundos"], 120)

    def test_timeout_bloqueia_sem_expor_segredo(self):
        self.api.side_effect = requests.Timeout("test-secret")
        res = self.client.post("/disparar", json=self.lead)
        self.assertEqual(res.json()["vendeai"]["motivo"], "FALHA_COMUNICACAO")
        self.assertNotIn("test-secret", res.text)
        self.whatsapp.assert_not_called()

    def test_resposta_malformada_nao_libera_envio(self):
        for payload in ({}, [], {"success_count_mailings": 1, "fail_count_mailings": 1, "failing_reasons": []}):
            with self.subTest(payload=payload):
                self.api.return_value = Mock(status_code=200, json=Mock(return_value=payload))
                res = self.client.post("/disparar", json=self.lead).json()
                self.assertEqual(res["vendeai"]["motivo"], "RESPOSTA_INVALIDA")
        self.whatsapp.assert_not_called()

    def test_modo_continuar_explicito(self):
        self.client.patch("/api/configuracao", json={"vendeai_falha_modo": "continuar"})
        self.api.return_value = Mock(status_code=403)
        res = self.client.post("/disparar", json=self.lead).json()
        self.assertEqual(res["status"], "sucesso")
        self.assertEqual(res["vendeai"]["motivo"], "HTTP_403")

    def test_desabilitado_nao_chama_api(self):
        self.client.patch("/api/configuracao", json={"vendeai_habilitado": False})
        res = self.client.post("/disparar", json=self.lead).json()
        self.assertEqual(res["status"], "sucesso")
        self.assertEqual(res["vendeai"]["status"], "desabilitado")
        self.api.assert_not_called()

    def test_sms_preserva_resultado_cadastro(self):
        with patch.object(main, "proximo_numero_disponivel", return_value=None), patch.object(main, "tentar_fallback_sms", return_value={"status": "sucesso", "canal": "sms"}):
            res = self.client.post("/disparar", json=self.lead).json()
            self.assertEqual(res["canal"], "sms")
            self.assertEqual(res["vendeai"]["status"], "cadastrado")

    def test_horario_e_variavel_invalida_nao_cadastram(self):
        with patch.object(main, "dentro_do_horario_permitido", return_value=(False, "Fechado")):
            self.client.post("/disparar", json=self.lead)
            self.client.post("/api/disparo-lote", json={"leads": [self.lead]})
        self.client.post("/disparar", json={**self.lead, "variaveis_template": [""]})
        self.api.assert_not_called()
        self.whatsapp.assert_not_called()

    def test_limites_concorrentes_e_persistentes(self):
        with ThreadPoolExecutor(max_workers=12) as executor:
            resultados = list(executor.map(lambda _: mailing.reservar_chamada(main.get_db, "conta"), range(12)))
        self.assertEqual(resultados.count(0), 10)
        self.assertGreater(mailing.reservar_chamada(main.get_db, "conta"), 0)
        self.assertEqual(mailing.reservar_chamada(main.get_db, "outra"), 0)
        with test_db() as conn:
            conn.executemany("INSERT INTO vendeai_chamadas VALUES (?, ?)", [("dia", mailing.time.time() - 120)] * 1440)
        self.assertGreater(mailing.reservar_chamada(main.get_db, "dia"), 86000)

    def test_limite_local_nao_chama_servico(self):
        with patch.object(mailing, "reservar_chamada", return_value=42):
            res = self.client.post("/disparar", json=self.lead).json()
        self.assertEqual(res["vendeai"]["tentar_apos_segundos"], 42)
        self.api.assert_not_called()
        self.whatsapp.assert_not_called()

    def test_configuracao_token_protegido_e_preservado(self):
        res = self.client.patch("/api/configuracao", json={"vendeai_mailing_token": "novo-segredo", "vendeai_campanha_padrao": "campanha"})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("novo-segredo", res.text)
        self.assertNotIn("vendeai_mailing_token", res.json())
        self.assertTrue(res.json()["vendeai_token_configurado"])
        self.client.patch("/api/configuracao", json={"vendeai_mailing_token": "", "vendeai_produto_padrao": "fgts"})
        self.assertEqual(main.carregar_configuracao()["vendeai_mailing_token"], "novo-segredo")
        self.assertNotIn("novo-segredo", self.client.get("/api/configuracao").text)
        with test_db() as conn:
            self.assertNotIn("novo-segredo", str([tuple(r) for r in conn.execute("SELECT * FROM eventos")]))
        with patch.object(main, "VENDEAI_API_TOKEN", "env-secret"):
            res = self.client.patch("/api/configuracao", json={"vendeai_remover_token": True})
            self.assertEqual(res.json()["vendeai_token_origem"], "ambiente")

    def test_configuracao_rejeita_url_insegura_e_modo_invalido(self):
        for campo, valor in (("vendeai_mailing_url", "http://example.com"), ("vendeai_mailing_url", "https://user:pass@example.com"), ("vendeai_mailing_url", "https://example.com/caminho"), ("vendeai_falha_modo", "x"), ("vendeai_habilitado", "false")):
            self.assertEqual(self.client.patch("/api/configuracao", json={campo: valor}).status_code, 400)

    def test_tipos_invalidos_retornam_400(self):
        for body in ([], {"telefone": 11987654321}, {"telefone": "11987654321", "variaveis_template": "x"}, {"telefone": "11987654321", "nascimento": 1992}):
            self.assertEqual(self.client.post("/disparar", json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/disparo-lote", json={"leads": [], "numeros_texto": "1"}).status_code, 400)
        self.assertEqual(self.client.post("/api/disparo-lote", json={"leads": [self.lead] * 20001}).status_code, 400)
        self.api.assert_not_called()

    def test_migracao_idempotente_preserva_configuracao(self):
        main.init_db()
        main.init_db()
        self.assertEqual(main.carregar_configuracao()["vendeai_mailing_token"], "test-secret")
        # Simula tabela de configuracao anterior a esta integracao.
        with test_db() as conn:
            conn.execute("DROP TABLE configuracao")
            conn.execute("CREATE TABLE configuracao (id INTEGER PRIMARY KEY, modo TEXT, hora_inicio TEXT, hora_fim TEXT, dias_semana TEXT)")
            conn.execute("INSERT INTO configuracao VALUES (1, 'horario', '09:00', '17:00', '1,2,3')")
        main.init_db()
        cfg = main.carregar_configuracao()
        self.assertEqual(cfg["modo"], "horario")
        self.assertEqual(cfg["vendeai_falha_modo"], "bloquear")
        self.assertEqual(cfg["vendeai_mailing_url"], mailing.MAILING_URL)


if __name__ == "__main__":
    unittest.main()
