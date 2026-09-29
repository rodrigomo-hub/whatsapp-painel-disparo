import json
import unittest
from unittest.mock import Mock,patch
import main,optout,vendeai_eventos
import test_mailing
from test_mailing import test_db

class OptoutTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        with test_db() as c:
            c.execute('UPDATE optout_config SET habilitado=1')
            self.token=c.execute('SELECT token FROM vendeai_webhook_config').fetchone()[0]
        self.event={'event':'conversation_created','chat_summary':{'account_id':'33393','chat_id':'123','details':{'contact':{'name':'Teste','phone':'+55 11 98765-4321','cpf':'00123456789'},'session':{'campaign':'teste'}}},'first_message':' Não QUERO mensagens '}
    def receber(self):
        return vendeai_eventos.receber(main.get_db,self.token,json.dumps(self.event).encode(),'33393')
    def task(self):
        with test_db() as c:return dict(c.execute('SELECT * FROM optout_tarefas').fetchone())
    def response(self,status=200,data=None,headers=None):
        return Mock(status_code=status,json=Mock(return_value=data or {}),headers=headers or {})
    def test_suppression_exact_normalization_dedup_and_account(self):
        self.receber();self.receber()
        self.assertTrue(optout.bloqueado(main.get_db,'11987654321'))
        self.assertTrue(optout.bloqueado(main.get_db,'551187654321'))
        with test_db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM optout').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM optout_tarefas').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT cpf FROM optout').fetchone()[0],'00123456789')
    def test_regular_message_and_wrong_account_no_action(self):
        for text in ['Consultar detalhes','Eu nao quero mensagens agora']:
            self.event['first_message']=text;self.receber()
        self.event['first_message']='Não quero mensagens';self.event['chat_summary']['account_id']='999';self.receber()
        with test_db() as c:self.assertEqual(c.execute('SELECT count(*) FROM optout').fetchone()[0],0)
    def test_resolve_is_first_preserves_labels_deletes_after_resolve(self):
        self.receber()
        responses=[self.response(),self.response(data={'meta':{'sender':{'id':321}}}),self.response(),self.response(data={'payload':['cliente','clt']}),self.response(),self.response(),self.response(404)]
        with patch.object(optout.requests,'request',side_effect=responses) as req:
            for _ in range(7):self.assertTrue(optout.processar_um(main.get_db,'secret'))
        self.assertTrue(req.call_args_list[0].args[1].endswith('/toggle_status'))
        self.assertEqual(req.call_args_list[0].kwargs['json'],{'status':'resolved'})
        self.assertEqual(set(req.call_args_list[4].kwargs['json']['labels']),{'cliente','clt','nao_quer_mensagens'})
        self.assertEqual(req.call_args_list[5].args[0],'DELETE')
        self.assertEqual(self.task()['estado'],'concluido')
        self.assertTrue(optout.bloqueado(main.get_db,'11987654321'))
    def test_retry_resolve_backoff_three_attempts_no_delete_on_failure(self):
        self.receber()
        with patch.object(optout.requests,'request',return_value=self.response(429,headers={'Retry-After':'10'})) as req:
            optout.processar_um(main.get_db,'secret');self.assertEqual(self.task()['tentativas'],1)
            self.assertFalse(optout.processar_um(main.get_db,'secret'))
            for _ in range(2):
                with test_db() as c:c.execute('UPDATE optout_tarefas SET tentar_em=0')
                optout.processar_um(main.get_db,'secret')
            self.assertEqual(req.call_count,3)
        self.assertEqual(self.task()['passo'],'contato');self.assertFalse(self.task()['resolvida'])
    def test_missing_contact_still_unassigns_and_resolves(self):
        self.receber()
        with patch.object(optout.requests,'request',side_effect=[self.response(),self.response(404),self.response(404),self.response(404),self.response(404)]) as req:
            for _ in range(5):optout.processar_um(main.get_db,'secret')
        self.assertTrue(any('/assignments' in x.args[1] for x in req.call_args_list))
        self.assertFalse(any(x.args[0]=='DELETE' for x in req.call_args_list))
        self.assertEqual(self.task()['estado'],'revisar')
    def test_no_http_inside_webhook_transaction(self):
        with patch.object(optout.requests,'request') as req:
            self.receber();req.assert_not_called()
        self.assertEqual(self.task()['estado'],'pendente')
    def test_api_mailing_sms_and_overflow_blocked(self):
        self.receber()
        with test_db() as c:c.execute("UPDATE configuracao SET transbordo_habilitado=1,transbordo_url='https://example.invalid'")
        self.assertEqual(self.client.post('/disparar',json=self.lead).json()['motivo_codigo'],'optout')
        self.assertEqual(main.cadastrar_base_vendeai([{'phone':'5511987654321'}],main.carregar_configuracao())[0]['motivo'],'OPTOUT')
        self.assertFalse(main.enviar_sms('5511987654321','teste')[0])
        self.assertEqual(main.tentar_fallback_sms('','','5511987654321',[])['motivo_codigo'],'optout')
        self.api.assert_not_called();self.whatsapp.assert_not_called()
    def test_final_whatsapp_guard(self):
        self.receber();self.whatsapp_patch.stop()
        with test_db() as c:n=c.execute('SELECT * FROM numeros LIMIT 1').fetchone()
        self.assertFalse(main.enviar_whatsapp(n,'5511987654321',[])[0]);self.api.assert_not_called()
    def test_batch_reports_blocked(self):
        self.receber()
        r=self.client.post('/api/disparo-lote',json={'leads':[self.lead]}).json()
        self.assertEqual(r['resultados'][0]['motivo_codigo'],'optout');self.api.assert_not_called()
    def test_queued_items_cancelled(self):
        with test_db() as c:c.execute('UPDATE configuracao SET fila_habilitada=1')
        r=self.client.post('/disparar',json=self.lead).json()
        self.receber()
        self.assertEqual(main.obter_fila().consultar(r['fila_id'])['status'],'cancelado')
    def test_disabled_blocks_existing_but_does_not_create_new(self):
        self.receber()
        with test_db() as c:c.execute('UPDATE optout_config SET habilitado=0')
        self.event['chat_summary']['details']['contact']['phone']='5511987654322';self.receber()
        self.assertFalse(optout.bloqueado(main.get_db,'5511987654322'))
        self.assertTrue(optout.bloqueado(main.get_db,'5511987654321'))

    def test_direct_delete_one_shot_is_first_request_and_no_contact_delete(self):
        with test_db() as c:c.execute("UPDATE optout_config SET teste_excluir_restantes=1,teste_telefone='5511987654321'")
        self.receber()
        self.assertEqual(self.task()['passo'],'excluir_conversa')
        with patch.object(optout.requests,'request',side_effect=[self.response(),self.response(404)]) as req:
            optout.processar_um(main.get_db,'secret',True)
            optout.processar_um(main.get_db,'secret')
        self.assertEqual(req.call_args_list[0].args,('DELETE',optout.BASE+'33393/conversations/123'))
        self.assertEqual(req.call_args_list[1].args,('GET',optout.BASE+'33393/conversations/123'))
        self.assertEqual(self.task()['estado'],'concluido')
        self.assertTrue(optout.bloqueado(main.get_db,'11987654321'))
        self.event['chat_summary']['chat_id']='124';self.receber()
        with test_db() as c:
            self.assertEqual(c.execute("SELECT passo FROM optout_tarefas WHERE chat_id='124'").fetchone()[0],'resolver')
    def test_duplicate_does_not_consume_armed_test(self):
        self.receber()
        with test_db() as c:c.execute("UPDATE optout_config SET teste_excluir_restantes=1,teste_telefone='5511987654321'")
        self.receber()
        with test_db() as c:self.assertEqual(c.execute('SELECT teste_excluir_restantes FROM optout_config').fetchone()[0],1)
    def test_direct_delete_refused_falls_back_to_resolve(self):
        with test_db() as c:c.execute("UPDATE optout_config SET teste_excluir_restantes=1,teste_telefone='5511987654321'")
        self.receber()
        with patch.object(optout.requests,'request',return_value=self.response(403)):
            optout.processar_um(main.get_db,'secret')
        self.assertEqual(self.task()['passo'],'resolver')
    def test_delete_accepted_but_not_gone_is_not_success(self):
        with test_db() as c:c.execute("UPDATE optout_config SET teste_excluir_restantes=1,teste_telefone='5511987654321'")
        self.receber()
        with patch.object(optout.requests,'request',return_value=self.response()):
            optout.processar_um(main.get_db,'secret')
            for _ in range(3):
                with test_db() as c:c.execute('UPDATE optout_tarefas SET tentar_em=0')
                optout.processar_um(main.get_db,'secret')
        self.assertEqual(self.task()['estado'],'revisar')

    def test_other_phone_does_not_consume_or_run_targeted_test(self):
        with test_db() as c:c.execute("UPDATE optout_config SET teste_excluir_restantes=1,teste_telefone='5511987654322'")
        self.receber()
        self.assertEqual(self.task()['passo'],'resolver')
        with test_db() as c:self.assertEqual(c.execute('SELECT teste_excluir_restantes FROM optout_config').fetchone()[0],1)
    def test_test_endpoint_requires_specific_phone(self):
        with patch.object(main,'VENDEAI_API_TOKEN','secret'):
            self.assertEqual(self.client.post('/api/optout/testar-exclusao',json={}).status_code,400)

    def test_general_direct_delete_matches_optout_only_for_all_phones(self):
        with test_db() as c:c.execute('UPDATE optout_config SET excluir_conversa_primeiro=1')
        self.receber()
        self.event['chat_summary']['chat_id']='124';self.event['chat_summary']['details']['contact']['phone']='5511987654322';self.receber()
        self.event['chat_summary']['chat_id']='125';self.event['first_message']='Consultar detalhes';self.receber()
        with test_db() as c:
            rows=c.execute('SELECT passo FROM optout_tarefas').fetchall()
            self.assertEqual([r['passo'] for r in rows],['excluir_conversa','excluir_conversa'])
            self.assertEqual(c.execute('SELECT count(*) FROM optout').fetchone()[0],2)
