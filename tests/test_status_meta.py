import unittest
from unittest.mock import Mock,patch
import main
import test_fila
from test_mailing import test_db
class StatusMetaTests(unittest.TestCase):
    resposta=staticmethod(test_fila.FilaTests.resposta)
    def setUp(self):
        test_fila.FilaTests.setUp(self)
        with test_db() as c:c.execute("UPDATE numeros SET waba_id='waba'")
    def numero(self):
        with test_db() as c:return c.execute('SELECT * FROM numeros LIMIT 1').fetchone()
    def test_banido_com_qualidade_alta(self):
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'quality_rating':'GREEN','status':'BANNED'}))):
            self.assertEqual(main.verificar_qualidade(self.numero()),'GREEN')
        self.assertEqual(self.numero()['status_envio_meta'],'BANNED')
        self.assertEqual(self.numero()['ativo'],0)
        self.assertEqual(self.numero()['pausado_por_qualidade'],0)
        self.assertFalse(main.disponivel_agora(self.numero()))
        self.assertEqual(main.capacidade_lote()['capacidade_agora'],0)
        self.assertEqual(main.total_hoje()['total_banidos_hoje'],1)

    def test_banimento_daily_dedup_unknown_and_brasilia(self):
        with test_db() as c:
            with patch.object(main,'agora_iso',return_value='2026-10-02T01:00:00+00:00'):
                main.registrar_banimento_meta(c,1,'BANNED')
                main.registrar_banimento_meta(c,1,'UNKNOWN')
                main.registrar_banimento_meta(c,1,'BANNED')
                main.registrar_banimento_meta(c,1,'AVAILABLE')
                main.registrar_banimento_meta(c,1,'BANNED')
            rows=c.execute('SELECT * FROM banimentos_meta').fetchall()
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['dia'],'2026-10-01')
            with patch.object(main,'agora_iso',return_value='2026-10-03T10:00:00-03:00'):
                main.registrar_banimento_meta(c,1,'BANNED')
                self.assertEqual(c.execute('SELECT count(*) FROM banimentos_meta').fetchone()[0],1)
                main.registrar_banimento_meta(c,1,'AVAILABLE')
                main.registrar_banimento_meta(c,1,'BANNED')
            self.assertEqual(c.execute('SELECT count(*) FROM banimentos_meta').fetchone()[0],2)

    def test_waba_ban_counts_each_official_once_not_payment_block(self):
        with test_db() as c:
            c.execute("INSERT INTO numeros(nome,phone_number_id,access_token,waba_id) VALUES('Outro','2','token','waba')")
        main.bloquear_numeros_waba('waba',{'can_send_message':'BLOCKED'})
        self.assertEqual(main.total_hoje()['total_banidos_hoje'],0)
        health={'entities':[{'entity_type':'WABA','errors':[{'error_code':141014}]}]}
        main.bloquear_numeros_waba('waba',health);main.bloquear_numeros_waba('waba',health)
        self.assertEqual(main.total_hoje()['total_banidos_hoje'],2)

    def test_migration_does_not_date_existing_bans_as_today(self):
        with test_db() as c:
            c.execute("UPDATE numeros SET status_envio_meta='BANNED'")
            c.execute('ALTER TABLE numeros DROP COLUMN banimento_meta_ativo')
        main.init_db()
        with test_db() as c:main.registrar_banimento_meta(c,1,'BANNED')
        self.assertEqual(main.total_hoje()['total_banidos_hoje'],0)
    def test_waba_bloqueada_com_numero_conectado(self):
        for health,esperado in [({'can_send_message':'BLOCKED'},'BLOCKED'),({'can_send_message':'AVAILABLE'},'AVAILABLE'),({},'UNKNOWN')]:
            with self.subTest(health=health),patch.object(main.requests,'get',side_effect=[Mock(status_code=200,json=Mock(return_value={'quality_rating':'GREEN','status':'CONNECTED'})),Mock(status_code=200,json=Mock(return_value={'health_status':health}))]):
                main.verificar_qualidade(self.numero())
                self.assertEqual(self.numero()['status_envio_meta'],esperado)
    def test_bloqueia_no_envio_com_selecao_antiga(self):
        self.whatsapp_patch.stop();antigo=self.numero()
        with test_db() as c:c.execute("UPDATE numeros SET status_envio_meta='BANNED'")
        self.assertFalse(main.enviar_whatsapp(antigo,'5511999999999',[])[0])
        self.api.assert_not_called()

    def test_bloqueio_pagamento_pausa_toda_waba_sem_afetar_outra(self):
        with test_db() as c:
            c.execute("INSERT INTO numeros(nome,phone_number_id,access_token,waba_id,ativo) VALUES('Mesmo','2','token','waba',1)")
            c.execute("INSERT INTO numeros(nome,phone_number_id,access_token,waba_id,ativo) VALUES('Outra','3','token','outra',1)")
        main.bloquear_numeros_waba('waba',{'can_send_message':'BLOCKED','entities':[{'entity_type':'WABA','errors':[{'error_code':141006}]}]})
        with test_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM numeros WHERE waba_id='waba' AND ativo=1").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT ativo FROM numeros WHERE waba_id='outra'").fetchone()[0],1)
        main.bloquear_numeros_waba('waba',{'can_send_message':'AVAILABLE'})
        with test_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM numeros WHERE waba_id='waba' AND ativo=1").fetchone()[0],0)
