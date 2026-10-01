import json,unittest,time
from unittest.mock import Mock,patch
import requests
import link_conversa as link
import vendeai_eventos as eventos
import test_vendeai_eventos


class LinkTests(unittest.TestCase):
    db=test_vendeai_eventos.EventosTests.db
    def setUp(self):
        test_vendeai_eventos.EventosTests.setUp(self)
        self.event={'event':'conversation_created','chat_summary':{'account_id':'33393','chat_id':'123','details':{'contact':{'phone':'5511999991234'}}},'first_message':'Consultar detalhes'}
        self.post=patch.object(requests,'post',return_value=Mock(status_code=200,json=Mock(return_value={'id':456})))
        self.send=self.post.start();self.addCleanup(self.post.stop)

    def receive(self,conta='33393'):
        return eventos.receber(self.db,self.token,json.dumps(self.event).encode(),conta)
    def enable(self):link.configurar(self.db,'33393',True)
    def run_one(self):return link.processar_um(self.db,'33393','secret')
    def rows(self):return link.resumo(self.db,'33393')['envios']

    def test_disabled_and_other_account_never_send(self):
        self.receive();self.assertFalse(self.run_one());self.enable()
        self.receive();self.assertFalse(self.run_one()) # evento antigo reentregue
        self.event['chat_summary'].update(account_id='32239',chat_id='124')
        self.receive('32239');self.assertFalse(self.run_one());self.send.assert_not_called()

    def test_one_send_per_conversation_no_inline_network_and_exact_link(self):
        self.enable();self.assertEqual(self.receive(),{'ok':True});self.send.assert_not_called()
        self.receive();self.event['first_message']='Outro texto';self.receive()
        self.assertTrue(self.run_one());self.assertFalse(self.run_one());self.send.assert_called_once()
        self.assertTrue(self.send.call_args.args[0].endswith('/33393/conversations/123/messages'))
        self.assertEqual(self.send.call_args.kwargs['json']['message_type'],'outgoing')
        self.assertIn('/s/4mOoz7',self.send.call_args.kwargs['json']['content'])
        self.assertEqual(self.rows()[0]['estado'],'ACEITO_CRM')

    def test_optout_never_queues_even_when_optout_feature_disabled(self):
        self.enable();self.event['first_message']='  NÃO QUERO MENSAGENS  '
        self.receive();self.assertFalse(self.run_one());self.send.assert_not_called()

    def test_optout_received_before_worker_blocks_send(self):
        self.enable();self.receive()
        with self.db() as c:c.execute("INSERT INTO optout(telefone) VALUES('5511999991234')")
        self.run_one();self.send.assert_not_called();self.assertEqual(self.rows()[0]['estado'],'CANCELADO')

    def test_timeout_no_retry_and_restart_no_retry(self):
        self.enable();self.receive();self.send.side_effect=requests.Timeout()
        self.run_one();self.assertFalse(self.run_one());self.send.assert_called_once()
        self.assertEqual(self.rows()[0]['estado'],'INCERTO')
        with self.db() as c:c.execute("UPDATE link_conversa_envios SET estado='ENVIANDO'")
        link.recuperar(self.db);self.assertFalse(self.run_one())

    def test_old_queue_expires_and_disabling_cancels(self):
        self.enable();self.receive()
        with self.db() as c:c.execute('UPDATE link_conversa_envios SET criado_em=?',(time.time()-121,))
        self.run_one();self.assertEqual(self.rows()[0]['estado'],'EXPIRADO');self.send.assert_not_called()
        self.event['chat_summary']['chat_id']='125';self.receive()
        link.configurar(self.db,'33393',False);self.assertFalse(self.run_one())
        self.assertEqual(self.rows()[0]['estado'],'CANCELADO')

    def test_other_event_and_unconfirmed_account_not_queued(self):
        self.enable();self.receive('32239');self.assertFalse(self.run_one())
        self.event['event']='message_created';self.receive();self.assertFalse(self.run_one())
