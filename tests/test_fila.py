import json
import time
import unittest
from unittest.mock import Mock, patch

import fila_disparos
import main
import test_mailing
from test_mailing import test_db


class FilaTests(unittest.TestCase):
    resposta = staticmethod(test_mailing.MailingTests.resposta)

    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        with test_db() as conn:
            conn.execute("UPDATE configuracao SET fila_habilitada=1, fila_intervalo_segundos=300, vendeai_falha_modo='continuar'")
        self.agora = time.time()
        self.relogio = patch.object(fila_disparos.time, "time", return_value=self.agora)
        self.clock = self.relogio.start()
        self.addCleanup(self.relogio.stop)
        self.fila = main.obter_fila()

    def avancar(self, segundos):
        self.agora += segundos
        self.clock.return_value = self.agora

    def rodada(self):
        self.fila.processar(main.carregar_configuracao, main.cadastrar_base_vendeai,
                            main.executar_disparo_preparado, main.dentro_do_horario_permitido, impedimento=main.impedimento_fila)

    def entrada(self, **campos):
        return self.client.post("/disparar", json={**self.lead, **campos})

    def test_acumula_cinco_minutos_uma_chamada_libera_envios(self):
        ids = []
        for i in range(3):
            r = self.entrada(telefone=f"1198765432{i}")
            self.assertEqual(r.status_code, 202)
            self.assertEqual(r.json()["status"], "enfileirado")
            ids.append(r.json()["fila_id"])
        self.rodada()
        self.avancar(299)
        self.rodada()
        self.api.assert_not_called()
        self.whatsapp.assert_not_called()
        self.api.return_value = self.resposta(3)
        self.avancar(1)
        self.rodada()
        self.api.assert_called_once()
        self.assertEqual(len(self.api.call_args.kwargs["json"]["leads"]), 3)
        self.assertEqual(self.whatsapp.call_count, 3)
        self.assertTrue(all(self.fila.consultar(i)["status"] == "sucesso" for i in ids))
        self.rodada()
        self.assertEqual(self.whatsapp.call_count, 3)

    def test_429_persistente_junta_novos_sem_disparar_antes(self):
        primeiro = self.entrada(email="").json()["fila_id"]
        self.api.return_value = Mock(status_code=429, headers={"Retry-After": "900"})
        self.avancar(300)
        self.rodada()
        email = self.api.call_args.kwargs["json"]["leads"][0]["email"]
        self.whatsapp.assert_not_called()
        self.assertEqual(self.fila.consultar(primeiro)["status"], "aguardando_cadastro")
        self.fila = fila_disparos.Fila(main.get_db, main.DB_FILE)  # Novo worker le o mesmo banco.
        self.avancar(100)
        segundo = self.entrada(telefone="11987654322").json()["fila_id"]
        self.avancar(799)
        self.rodada()
        self.assertEqual(self.api.call_count, 1)
        self.api.return_value = self.resposta(2)
        self.avancar(1)
        self.rodada()
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(self.api.call_args.kwargs["json"]["leads"][0]["email"], email)
        self.assertEqual(self.fila.consultar(primeiro)["status"], "sucesso")
        self.assertEqual(self.fila.consultar(segundo)["status"], "sucesso")

    def test_parcial_so_dispara_aceitos(self):
        a = self.entrada().json()["fila_id"]
        b = self.entrada(telefone="11987654322", cpf="invalido").json()["fila_id"]
        self.api.return_value = self.resposta(2, [{"index": 1, "reason": "INVALID_CPF"}])
        self.avancar(300)
        self.rodada()
        self.whatsapp.assert_called_once()
        self.assertEqual(self.fila.consultar(a)["status"], "sucesso")
        self.assertEqual(self.fila.consultar(b)["status"], "erro")
        self.assertEqual(self.fila.consultar(b)["vendeai"]["motivo"], "INVALID_CPF")

    def test_retry_do_n8n_deduplica_sem_trocar_email(self):
        a = self.entrada(email="").json()
        b = self.entrada(email="").json()
        self.assertEqual(a["fila_id"], b["fila_id"])
        self.assertTrue(b["duplicado"])
        self.assertEqual(self.fila.resumo()["contagens"]["aguardando_cadastro"], 1)
        self.avancar(300)
        self.rodada()
        r = self.entrada(email="").json()
        self.assertEqual(r["status"], "sucesso")
        self.assertEqual(r["fila_id"], a["fila_id"])
        self.whatsapp.assert_called_once()

    def test_idempotency_key_conflito(self):
        a = self.entrada(idempotency_key="execucao-1").json()
        b = self.entrada(idempotency_key="execucao-1").json()
        self.assertEqual(a["fila_id"], b["fila_id"])
        self.assertEqual(self.entrada(idempotency_key="execucao-1", nome="Outro").status_code, 409)

    def test_sem_numero_no_envio_encerra_sem_repetir(self):
        a = self.entrada().json()["fila_id"]
        self.avancar(300)
        with patch.object(main, "proximo_numero_disponivel", return_value=None):
            self.rodada()
        self.assertEqual(self.fila.consultar(a)["status"], "erro")
        self.whatsapp.assert_not_called()
        self.avancar(30)
        self.rodada()
        self.api.assert_called_once()
        self.whatsapp.assert_not_called()

    def test_recusa_entrada_sem_numero_sem_gravar(self):
        with test_db() as conn:
            conn.execute("UPDATE numeros SET ativo=0")
        r = self.entrada().json()
        self.assertFalse(r["aceito"])
        self.assertEqual(r["motivo_codigo"], "nenhum_numero_disponivel")
        self.assertEqual(self.fila.resumo()["contagens"], {})
        self.api.assert_not_called()

    def test_pendentes_encerram_quando_numero_pausa(self):
        a = self.entrada().json()["fila_id"]
        with test_db() as conn:
            conn.execute("UPDATE numeros SET ativo=0")
        self.rodada()
        self.assertEqual(self.fila.consultar(a)["status"], "erro")
        with test_db() as conn:
            conn.execute("UPDATE numeros SET ativo=1")
        self.avancar(300)
        self.rodada()
        self.api.assert_not_called()
        self.whatsapp.assert_not_called()

    def test_horario_recusa_entrada_e_encerra_pendente(self):
        a = self.entrada().json()["fila_id"]
        with patch.object(main, "dentro_do_horario_permitido", return_value=(False, "Fechado")):
            r = self.entrada(telefone="11987654322").json()
            self.assertFalse(r["aceito"])
            self.rodada()
        self.assertEqual(self.fila.consultar(a)["status"], "erro")
        self.avancar(300)
        self.rodada()
        self.whatsapp.assert_not_called()

    def test_lote_recusa_se_selecionado_indisponivel(self):
        r = self.client.post("/api/disparo-lote", json={"leads": [self.lead], "numeros_selecionados": [999]}).json()
        self.assertFalse(r["aceito"])
        self.assertEqual(self.fila.resumo()["contagens"], {})

    def test_excecao_envio_nao_duplica(self):
        a = self.entrada().json()["fila_id"]
        self.avancar(300)
        self.whatsapp.side_effect = RuntimeError("Falhou depois do envio")
        self.rodada()
        self.assertEqual(self.fila.consultar(a)["status"], "incerto")
        self.rodada()
        self.whatsapp.assert_called_once()

    def test_recupera_cadastro_interrompido_mas_nao_envio_incerto(self):
        a = self.entrada().json()["fila_id"]
        b = self.entrada(telefone="11987654322").json()["fila_id"]
        self.fila.alterar(a, status="cadastrando")
        self.fila.alterar(b, status="enviando")
        self.avancar(300)
        self.rodada()
        self.assertEqual(self.fila.consultar(a)["status"], "sucesso")
        self.assertEqual(self.fila.consultar(b)["status"], "incerto")
        self.whatsapp.assert_called_once()

    def test_lock_impede_dois_workers(self):
        self.entrada()
        self.avancar(300)
        with fila_disparos.trava_worker(main.DB_FILE) as obtida:
            self.assertTrue(obtida)
            self.rodada()
        self.api.assert_not_called()
        self.rodada()
        self.api.assert_called_once()

    def test_lote_fila_preserva_numero_selecionado_e_variaveis(self):
        with test_db() as conn:
            conn.execute("INSERT INTO numeros(nome, phone_number_id, access_token) VALUES('Segundo','fake2','token2')")
            conn.execute("UPDATE numeros SET qualidade='GREEN',status_envio_meta='AVAILABLE' WHERE id=last_insert_rowid()")
        r = self.client.post("/api/disparo-lote", json={"leads": [self.lead], "numeros_selecionados": [2]})
        self.assertEqual(r.status_code, 202)
        self.api.assert_not_called()
        self.avancar(300)
        self.rodada()
        self.assertEqual(self.whatsapp.call_args.args[0]["id"], 2)
        self.assertEqual(self.whatsapp.call_args.args[2], ["Teste"])

    def test_cancela_somente_antes_de_processar(self):
        a = self.entrada().json()["fila_id"]
        r = self.client.post(f"/api/fila/{a}/cancelar")
        self.assertEqual(r.json()["status"], "cancelado")
        self.avancar(300)
        self.rodada()
        self.api.assert_not_called()
        self.whatsapp.assert_not_called()
        self.assertEqual(self.client.post(f"/api/fila/{a}/cancelar").status_code, 409)

    def test_consulta_n8n_sem_expor_payload_token_ou_lista(self):
        a = self.entrada().json()["fila_id"]
        self.client.cookies.clear()
        with patch.object(main, "API_KEY", "test-key"):
            r = self.client.get(f"/api/fila/{a}", headers={"X-API-Key": "test-key"})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("test-secret", r.text)
            self.assertNotIn(self.lead["cpf"], r.text)
            self.assertEqual(self.client.get("/api/fila", headers={"X-API-Key": "test-key"}).status_code, 401)
            self.assertEqual(self.client.post(f"/api/fila/{a}/cancelar", headers={"X-API-Key": "test-key"}).status_code, 401)

    def test_configuracao_nao_troca_destino_com_pendentes(self):
        self.entrada()
        self.assertEqual(self.client.patch("/api/configuracao", json={"vendeai_account_id": "outra"}).status_code, 409)
        for valor in (0, 5, 3601, "300", True):
            self.assertEqual(self.client.patch("/api/configuracao", json={"fila_intervalo_segundos": valor}).status_code, 400)

    def test_erro_503_aguarda_proxima_rodada(self):
        a = self.entrada().json()["fila_id"]
        self.api.return_value = Mock(status_code=503)
        self.avancar(300)
        self.rodada()
        self.whatsapp.assert_not_called()
        self.assertEqual(self.fila.consultar(a)["status"], "aguardando_cadastro")
        self.avancar(299)
        self.rodada()
        self.api.assert_called_once()
        self.api.return_value = self.resposta(1)
        self.avancar(1)
        self.rodada()
        self.assertEqual(self.fila.consultar(a)["status"], "sucesso")

    def test_validade_expirada_recadastra_antes_de_enviar(self):
        a = self.entrada().json()["fila_id"]
        self.fila.alterar(a, status="pronto", cadastro_em=self.agora - 8 * 86400)
        self.avancar(300)
        self.rodada()
        self.whatsapp.assert_not_called()
        self.rodada()
        self.assertEqual(self.fila.consultar(a)["status"], "sucesso")
        self.api.assert_called_once()

    def test_mesmo_telefone_nao_sobrescreve_dados_antes_do_envio(self):
        a = self.entrada().json()["fila_id"]
        b = self.entrada(campanha="outra").json()["fila_id"]
        self.avancar(300)
        self.rodada()
        self.assertEqual(len(self.api.call_args.kwargs["json"]["leads"]), 1)
        self.assertEqual(self.fila.consultar(a)["status"], "sucesso")
        self.assertEqual(self.fila.consultar(b)["status"], "aguardando_cadastro")
        self.avancar(300)
        self.rodada()
        self.assertEqual(self.fila.consultar(b)["status"], "sucesso")
