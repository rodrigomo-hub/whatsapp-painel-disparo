import json
import time
import unittest
from unittest.mock import Mock, patch

import main
import gupshup
import test_mailing
from test_mailing import test_db


class GupTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        self.app_id='01ebe36e-74cf-4ba3-81cc-86b6d118a2cd'
        self.t={'id':'template1','appId':self.app_id,'elementName':'teste','languageCode':'pt_BR',
                'status':'APPROVED','templateType':'TEXT','quality':'UNKNOWN','category':'UTILITY',
                'data':'Oi {{1}}','wabaId':'123'}
        self.get=patch.object(gupshup.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'status':'success','templates':[self.t]})))
        self.get.start();self.addCleanup(self.get.stop)
        self.assertEqual(self.client.post('/api/gupshup',json={'app_id':self.app_id,'token':'segredo-gup'}).status_code,200)
        self.config={'nome':'Jrq1956','telefone':'5511999999999','template_id':'template1','limite_diario':100,'limite_hora':10,'ativo':True}
        r=self.client.put('/api/gupshup/'+self.app_id,json=self.config)
        self.assertEqual(r.status_code,200,r.text)
        self.ident=r.json()['numero_id']

    def n(self):
        with test_db() as c:return c.execute('SELECT * FROM numeros WHERE id=?',(self.ident,)).fetchone()

    def test_unknown_allowed_with_limits_and_no_meta(self):
        self.assertEqual(self.n()['qualidade'],'UNKNOWN')
        self.assertTrue(main.disponivel_agora(self.n()))
        with test_db() as c:c.execute('UPDATE numeros SET limite_diario=0 WHERE id=?',(self.ident,))
        self.assertFalse(main.disponivel_agora(self.n()))
        self.assertEqual(main.verificar_qualidade(self.n()),'UNKNOWN')

    def test_stale_templates_and_rotation_modes(self):
        self.assertTrue(main.permitido_no_rodizio(self.n(),{'rodizio_tipo':'oficiais'}))
        self.assertFalse(main.permitido_no_rodizio(self.n(),{'rodizio_tipo':'normais'}))
        with test_db() as c:c.execute('UPDATE gup_apps SET consultado=?',(time.time()-1300,))
        self.assertFalse(main.disponivel_agora(self.n()))
        gupshup.atualizar(main.get_db,self.app_id)
        self.assertTrue(main.disponivel_agora(self.n()))
        self.client.patch('/api/numeros/'+str(self.ident),json={'ativo':False})
        gupshup.atualizar(main.get_db,self.app_id)
        self.assertFalse(main.disponivel_agora(self.n()))

    def test_existing_meta_unknown_stays_blocked(self):
        with test_db() as c:
            c.execute("UPDATE numeros SET qualidade='UNKNOWN' WHERE tipo_envio='meta'")
            n=c.execute("SELECT * FROM numeros WHERE tipo_envio='meta'").fetchone()
        self.assertFalse(main.disponivel_agora(n))

    def test_pending_and_invalid_token(self):
        self.t['status']='PENDING'
        self.assertEqual(self.client.put('/api/gupshup/'+self.app_id,json=self.config).status_code,400)
        self.assertFalse(main.disponivel_agora(self.n()))
        with patch.object(gupshup.requests,'get',return_value=Mock(status_code=401)):
            self.assertEqual(self.client.post('/api/gupshup',json={'app_id':self.app_id,'token':'bad'}).status_code,400)
        with test_db() as c:self.assertEqual(c.execute('SELECT token FROM gup_apps').fetchone()[0],'segredo-gup')

    def test_payload_no_duplicate_retry_and_optout(self):
        self.api.return_value=Mock(status_code=202,json=Mock(return_value={'status':'success','messageId':'gup-msg'}))
        r=gupshup.enviar(main.get_db,self.n(),'5547999999999',['Rodrigo'],lambda p:False)
        self.assertEqual(r,(True,'','gup-msg'))
        data=self.api.call_args.kwargs['data']
        self.assertEqual(json.loads(data['template']),{'id':'template1','params':['Rodrigo']})
        self.assertEqual(data['source'],'5511999999999')
        self.api.reset_mock()
        self.assertFalse(gupshup.enviar(main.get_db,self.n(),'5547999999999',['Rodrigo'],lambda p:True)[0])
        self.api.assert_not_called()
        self.api.side_effect=gupshup.requests.Timeout()
        self.assertTrue(gupshup.enviar(main.get_db,self.n(),'5547999999999',['Rodrigo'],lambda p:False)[1].startswith('ENVIO_INCERTO:'))
        self.assertEqual(self.api.call_count,1)

    def test_webhook_block_order_idempotency_and_auth(self):
        path=self.client.get('/api/gupshup/'+self.app_id+'/webhook').json()['path']
        event={'app':'Jrq1956','timestamp':int(time.time()*1000),'type':'account-event',
               'payload':{'type':'status-event','payload':{'status':'ACCOUNT_DISABLE'}}}
        self.assertEqual(self.client.post(path,json=event).status_code,200)
        self.assertFalse(main.disponivel_agora(self.n()))
        self.assertEqual(self.client.post(path,json=event).status_code,200)
        with test_db() as c:self.assertEqual(c.execute('SELECT count(*) FROM gup_eventos').fetchone()[0],1)
        event['timestamp']-=10000;event['payload']['payload']['status']='REINSTATE'
        self.client.post(path,json=event)
        with test_db() as c:self.assertEqual(c.execute('SELECT bloqueado FROM gup_apps').fetchone()[0],1)
        self.assertEqual(self.client.post('/webhook/gupshup/'+self.app_id,json=event).status_code,403)

    def test_template_event_not_overwritten_and_stale_block(self):
        path=self.client.get('/api/gupshup/'+self.app_id+'/webhook').json()['path']
        event={'app':'Jrq1956','timestamp':int(time.time()*1000),'type':'template-event',
               'payload':{'id':'template1','type':'status-update','status':'paused'}}
        self.client.post(path,json=event)
        gupshup.atualizar(main.get_db,self.app_id)
        self.assertEqual(self.n()['template_status'],'PAUSED')
        self.assertFalse(main.disponivel_agora(self.n()))

    def test_no_secret_in_api_and_session_required(self):
        self.assertNotIn('segredo-gup',self.client.get('/api/gupshup').text)
        self.assertNotIn('segredo-gup',self.client.get('/api/numeros').text)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/gupshup').status_code,401)
        self.assertEqual(self.client.post('/api/gupshup',json={}).status_code,401)


if __name__=='__main__':unittest.main()
