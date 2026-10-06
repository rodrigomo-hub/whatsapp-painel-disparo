import json
import unittest
from datetime import timedelta
from unittest.mock import patch
import main
import lembretes_sms as l
import test_mailing
from test_mailing import test_db

class LembretesTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        self.now=l.agora()
        self.clock=patch.object(l,'agora',side_effect=lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
        self.sender=patch.object(l.campanhas_sms,'enviar',return_value=('sucesso','','uid')).start()
        self.addCleanup(patch.stopall)
        l.salvar(main.get_db,{'habilitado':True,'intervalos':[30,240,1440],'mensagens':l.TEXTOS,'idade_maxima_dias':7})
    def event(self,status='FORMALIZATION_REQUESTED',pid='123',age=60,phone='5511999999999',link='facta.ly/teste',conta='33393'):
        t=(self.now-timedelta(minutes=age)).isoformat()
        d={'event':'proposal_created','chat_summary':{'account_id':conta,'chat_id':'1','details':{'contact':{'name':'Teste','phone':phone}}},'proposal':{'proposal_id':pid,'proposal_status':status,'bank':'facta','formalization_link':link}}
        with test_db() as c:
            c.execute("INSERT INTO vendeai_eventos(recebido_em,evento,account_id,chat_id,produto,banco,proposta_id,payload_raw,fingerprint,conta_confirmada) VALUES(?,'proposal_created',?,'1','clt','facta',?,?,'x',1)",(t,conta,pid,json.dumps(d)))
    def runone(self,horario=True,saldo=10):
        return l.processar_um(main.get_db,'33393','secret',lambda:(horario,'fora'),lambda:{'valor':saldo})
    def test_sequencia_antiga_sem_rajada_e_parada_por_assinatura(self):
        self.event(age=1800)
        self.assertTrue(self.runone());self.assertFalse(self.runone());self.assertEqual(self.sender.call_count,1)
        self.assertIn('https://facta.ly/teste',self.sender.call_args.args[2])
        self.now+=timedelta(hours=4);self.assertTrue(self.runone())
        self.event('FORMALIZATION_COMPLETED',age=0)
        self.now+=timedelta(days=1);self.assertFalse(self.runone())
        self.assertEqual(self.sender.call_count,2)
        d=l.relatorio(main.get_db,'33393',self.now-timedelta(days=3),self.now+timedelta(days=1))['resumo']
        self.assertEqual((d['sms_enviados'],d['propostas_lembradas'],d['formalizadas_apos']),(2,1,1))
    def test_status_terminal_nao_reabre_com_webhook_atrasado(self):
        for i,status in enumerate(['CANCELED','LIQUIDATED_TO_CUSTOMER','FORMALIZATION_COMPLETED']):
            self.event(status,pid=str(i));self.event(pid=str(i),age=0)
        self.assertFalse(self.runone());self.sender.assert_not_called()
    def test_optout_link_conta_idade_e_horario(self):
        self.event(pid='1',link='https://evil.example/assinatura')
        self.event(pid='2',conta='32239')
        self.event(pid='3',age=8*1440)
        self.event(pid='4',phone='123')
        self.assertFalse(self.runone())
        self.event(pid='5')
        self.assertFalse(self.runone(horario=False));self.assertFalse(self.runone(saldo=0))
        with test_db() as c:c.execute("INSERT INTO optout(telefone) VALUES('5511999999999')")
        self.assertFalse(self.runone());self.sender.assert_not_called()
    def test_incerto_nao_repete_e_limita_por_telefone(self):
        self.event(pid='1');self.event(pid='2')
        self.sender.return_value=('incerto','Timeout','')
        self.assertTrue(self.runone());self.assertFalse(self.runone())
        self.now+=timedelta(hours=5)
        self.assertTrue(self.runone());self.assertEqual(self.sender.call_count,2)
        self.now+=timedelta(days=1);self.assertFalse(self.runone())
    def test_cancelamento_entre_reserva_e_post(self):
        self.event()
        real=l.sincronizar;calls=[]
        def sync(db):
            calls.append(1)
            if len(calls)==2:self.event('CANCELED',age=0)
            return real(db)
        with patch.object(l,'sincronizar',side_effect=sync):self.runone()
        self.sender.assert_not_called()
    def test_config_autenticacao_e_relatorio_vazio(self):
        self.assertEqual(self.client.get('/lembretes-sms').status_code,200)
        self.assertEqual(self.client.get('/api/lembretes-sms').json()['resumo']['sms_enviados'],0)
        self.assertEqual(self.client.get('/api/lembretes-sms?inicio=xxx').status_code,400)
        self.assertEqual(self.client.put('/api/lembretes-sms',json={'habilitado':True}).status_code,400)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/lembretes-sms').status_code,401)

    def test_reinicio_nao_repete_e_limite_tres_lembretes(self):
        self.event()
        for hours in [0,4,24]:
            self.now+=timedelta(hours=hours);self.assertTrue(self.runone())
        self.now+=timedelta(days=1);self.assertFalse(self.runone());self.assertEqual(self.sender.call_count,3)
        with test_db() as c:c.execute("UPDATE lembretes_sms_envios SET estado='enviando' WHERE etapa=3")
        l.recuperar(main.get_db)
        with test_db() as c:self.assertEqual(c.execute('SELECT estado FROM lembretes_sms_envios WHERE etapa=3').fetchone()[0],'incerto')
        self.assertFalse(self.runone())

    def test_relatorio_nao_conta_pagamento_antes_do_sms_nem_fora_da_janela(self):
        self.event();self.runone();self.now+=timedelta(days=8)
        self.event('LIQUIDATED_TO_CUSTOMER',age=0);l.sincronizar(main.get_db)
        r=l.relatorio(main.get_db,'33393',self.now-timedelta(days=10),self.now+timedelta(days=1))['resumo']
        self.assertEqual(r['pagas_apos'],0)
        r=l.relatorio(main.get_db,'33393',self.now-timedelta(days=1),self.now+timedelta(days=1))['resumo']
        self.assertEqual(r['propostas_lembradas'],0)
