import unittest
from unittest.mock import patch,Mock
import requests,main,oferta_interna as oi,test_mailing
from test_mailing import test_db

class OfertaTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        with test_db() as c:c.execute("UPDATE oferta_interna_config SET ativo=1,usuario='u',senha='s',token='cached'")
        self.event={'event':'stage_updated','chat_summary':{'account_id':'33393','chat_id':'123','details':{'contact':{'cpf':'062.355.585-97'}}}}
    def schedule(self):
        with test_db() as c:oi.agendar(c,self.event,1)
    def state(self):
        with test_db() as c:return dict(c.execute('select * from oferta_interna_notas').fetchone())
    def response(self,status,data):return Mock(status_code=status,json=Mock(return_value=data))
    def test_private_note_cached_token_exact_match_and_dedup(self):
        self.schedule();self.schedule()
        lookup={'items':[{'cpf':'99999999999','texto_oferta':'wrong'},{'cpf':'6235558597','resultado':{'oferta':'Oferta correta'}}]}
        self.api.return_value=self.response(200,{'id':5,'private':True})
        with patch.object(oi.requests,'get',return_value=self.response(200,lookup)) as get:
            self.assertTrue(oi.processar_um(main.get_db,'33393','crm'))
            self.assertFalse(oi.processar_um(main.get_db,'33393','crm'))
            self.assertEqual(get.call_args.kwargs['headers']['Authorization'],'Bearer cached')
        self.api.assert_called_once()
        body=self.api.call_args.kwargs['json'];self.assertIs(body['private'],True);self.assertIn('Oferta correta',body['content'])
        self.assertEqual(self.state()['estado'],'CONCLUIDO')
    def test_refresh_401_and_cache_new_token(self):
        self.schedule()
        self.api.side_effect=[self.response(200,{'access_token':'fresh'}),self.response(200,{'id':1,'private':True})]
        with patch.object(oi.requests,'get',side_effect=[self.response(401,{}),self.response(200,{'items':[{'cpf':'06235558597','texto_oferta':'Oferta'}]})]):oi.processar_um(main.get_db,'33393','crm')
        self.assertEqual(self.state()['estado'],'CONCLUIDO')
        with test_db() as c:self.assertEqual(c.execute('select token from oferta_interna_config').fetchone()[0],'fresh')
    def test_no_match_or_ambiguous_never_posts_note(self):
        self.schedule()
        for items in ([],[{'cpf':'11111111111','texto_oferta':'wrong'}],[{'cpf':'06235558597','texto_oferta':'a'}]*2):
            with test_db() as c:c.execute("update oferta_interna_notas set estado='PENDENTE',tentar_em=0")
            with patch.object(oi.requests,'get',return_value=self.response(200,{'items':items})):oi.processar_um(main.get_db,'33393','crm')
        self.api.assert_not_called();self.assertEqual(self.state()['estado'],'REVISAR')
    def test_timeout_note_is_not_repeated(self):
        self.schedule();self.api.side_effect=requests.Timeout()
        with patch.object(oi.requests,'get',return_value=self.response(200,{'items':[{'cpf':'06235558597','texto_oferta':'Oferta'}]})):
            oi.processar_um(main.get_db,'33393','crm');oi.recuperar(main.get_db);oi.processar_um(main.get_db,'33393','crm')
        self.assertEqual(self.state()['estado'],'INCERTO');self.api.assert_called_once()
    def test_disabled_wrong_account_and_missing_cpf(self):
        with test_db() as c:
            oi.agendar(c,{'chat_summary':{'account_id':'32239','chat_id':'123','details':{'contact':{'cpf':'06235558597'}}}},1)
            oi.agendar(c,{'chat_summary':{'account_id':'33393','chat_id':'123'}},1)
            c.execute('update oferta_interna_config set ativo=0');oi.agendar(c,self.event,1)
            self.assertEqual(c.execute('select count(*) from oferta_interna_notas').fetchone()[0],0)
    def test_optout_prevents_note(self):
        self.schedule()
        with test_db() as c:c.execute("insert into optout_tarefas(account_id,chat_id,criado_em) values('33393','123',0)")
        with patch.object(oi.requests,'get',return_value=self.response(200,{'items':[{'cpf':'06235558597','texto_oferta':'Oferta'}]})):oi.processar_um(main.get_db,'33393','crm')
        self.assertEqual(self.state()['estado'],'CANCELADO');self.api.assert_not_called()

    def test_ready_offer_wins_over_processing_record_in_any_order(self):
        ready={'cpf':'06235558597','status':'com_oferta','texto_oferta':'  ','resultado':{'banco':'V8','oferta':'Oferta V8'}}
        pending={'cpf':'06235558597','status':'processando','resultado':None}
        for items in ([pending,ready],[ready,pending]):
            with patch.object(oi.requests,'get',return_value=self.response(200,{'items':items})):
                self.assertEqual(oi.buscar(main.get_db,{'token':'cached'},'06235558597'),'Oferta V8')
        self.api.assert_not_called()

    def test_cancelled_or_wrong_cpf_offers_do_not_count(self):
        items=[{'cpf':'06235558597','status':'cancelado','texto_oferta':'Oferta antiga'},
               {'cpf':'11111111111','status':'com_oferta','texto_oferta':'Outro cliente'},
               {'cpf':'06235558597','status':'processando','texto_oferta':'Texto antigo'}]
        with patch.object(oi.requests,'get',return_value=self.response(200,{'items':items})):
            with self.assertRaises(ValueError):oi.buscar(main.get_db,{'token':'cached'},'06235558597')
        self.api.assert_not_called()

    def test_v8_note_then_tag_preserves_existing_labels(self):
        self.schedule();self.api.return_value=self.response(200,{'id':7,'private':True})
        with patch.object(oi.requests,'get',return_value=self.response(200,{'items':[{'cpf':'06235558597','status':'com_oferta','resultado':{'banco':'V8','oferta':'Oferta'}}]})):
            oi.processar_um(main.get_db,'33393','crm')
        self.assertEqual(self.state()['tag_estado'],'PENDENTE')
        self.api.reset_mock();self.api.return_value=self.response(200,{})
        with patch.object(oi.requests,'get',side_effect=[self.response(200,{'payload':['clt','cliente']}),self.response(200,{'payload':['clt','cliente','oferta_v8']})]):
            oi.processar_tag(main.get_db,'33393','crm')
        self.api.assert_called_once();self.assertEqual(self.api.call_args.kwargs['json']['labels'],['clt','cliente','oferta_v8'])
        self.assertEqual(self.state()['estado'],'CONCLUIDO');self.assertEqual(self.state()['tag_estado'],'CONCLUIDO')
        self.assertFalse(oi.processar_tag(main.get_db,'33393','crm'))

    def test_tag_failure_never_reposts_note_or_overwrites_unknown_labels(self):
        self.schedule()
        with test_db() as c:c.execute("update oferta_interna_notas set estado='CONCLUIDO',banco='V8',tag_estado='PENDENTE'")
        with patch.object(oi.requests,'get',return_value=self.response(500,{})):
            oi.processar_tag(main.get_db,'33393','crm')
        self.api.assert_not_called();self.assertEqual(self.state()['estado'],'CONCLUIDO');self.assertEqual(self.state()['tag_estado'],'PENDENTE')
        with test_db() as c:c.execute('update oferta_interna_notas set tag_tentar_em=0')
        with patch.object(oi.requests,'get',return_value=self.response(200,{'payload':['oferta_v8']})):
            oi.processar_tag(main.get_db,'33393','crm')
        self.api.assert_not_called();self.assertEqual(self.state()['tag_estado'],'CONCLUIDO')

    def test_simplifica_uses_only_its_own_account(self):
        with test_db() as c:c.execute("update configuracao set vendeai_account_id='32239'")
        self.schedule()
        with test_db() as c:self.assertEqual(c.execute('select count(*) from oferta_interna_notas').fetchone()[0],0)
        self.event['chat_summary']['account_id']='32239';self.schedule()
        self.api.return_value=self.response(200,{'id':8,'private':True})
        with patch.object(oi.requests,'get',return_value=self.response(200,{'items':[{'cpf':'06235558597','resultado':{'banco':'V8','oferta':'Oferta'}}]})):
            self.assertFalse(oi.processar_um(main.get_db,'33393','wrong-token'))
            self.assertTrue(oi.processar_um(main.get_db,'32239','simplifica-token'))
        self.assertIn('/accounts/32239/conversations/123/messages',self.api.call_args.args[0])
        self.assertEqual(self.api.call_args.kwargs['headers']['api_access_token'],'simplifica-token')
        self.api.reset_mock();self.api.return_value=self.response(200,{})
        with patch.object(oi.requests,'get',side_effect=[self.response(200,{'payload':[]}),self.response(200,{'payload':['oferta_v8']})]):
            oi.processar_tag(main.get_db,'32239','simplifica-token')
        self.assertIn('/accounts/32239/conversations/123/labels',self.api.call_args.args[0])
