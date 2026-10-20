import copy,json,time,unittest
from unittest.mock import Mock,patch
import main,requests,oficiais_vendeai as oficial,gupshup_vendeai as g
from test_mailing import MailingTests,test_db

class GupVendeaiTests(unittest.TestCase):
    resposta=staticmethod(MailingTests.resposta)
    def setUp(self):
        MailingTests.setUp(self)
        self.t={'id':'t1','name':'oferta','language':'pt_BR','status':'APPROVED','category':'UTILITY','components':[{'type':'BODY','text':'Olá {{1}}'}]}
        self.i={'id':77,'name':'Gup','phone_number':'+5511999999999','provider':'gupshup','provider_config':{'app_id':'app-123','app_name':'Teste'},'reauthorization_required':False}
        self.get_patch=patch.object(requests,'get',side_effect=lambda *a,**k:self.response({'payload':[copy.deepcopy(self.i)]}));self.get=self.get_patch.start();self.addCleanup(self.get_patch.stop)
        self.api.side_effect=self.post_response;self.cfg=main.carregar_configuracao()
    def response(self,d,status=200):return Mock(status_code=status,json=Mock(return_value=d))
    def post_response(self,url,**kw):
        if url.endswith('/inboxes/'):return self.response({'inboxes':[{'id':'77','templates':[copy.deepcopy(self.t)]}]})
        if url.endswith('/send/'):return self.response({'status':'success','message_id':'456'})
        raise AssertionError(url)
    def importar(self):
        return oficial.vincular(main.get_db,self.cfg,'',{'inbox_id':'77','template_nome':'oferta','template_idioma':'pt_BR','limite_diario':300,'limite_hora':100})['id']
    def numero(self,id):
        with test_db() as c:return dict(c.execute('SELECT * FROM numeros WHERE id=?',(id,)).fetchone())
    def ativar(self,id):return g.editar(main.get_db,self.numero(id),{'ativo':True},self.cfg,'','https://painel.example')
    def test_discovery_and_import_paused_no_fake_quality(self):
        oficial.descobrir(main.get_db,self.cfg,'');items=oficial.listar(main.get_db,self.cfg)
        self.assertEqual(items[0]['provider'],'gupshup');self.assertFalse(items[0]['divergencia']);self.assertNotIn('_token',items[0])
        id=self.importar();self.assertEqual(self.importar(),id);n=self.numero(id)
        self.assertFalse(n['ativo']);self.assertEqual(n['qualidade'],'UNKNOWN');self.assertEqual(n['access_token'],'')
        self.ativar(id);n=self.numero(id);self.assertTrue(main.disponivel_agora(n))
        self.assertFalse(any('graph.facebook' in str(c) for c in self.get.call_args_list))
    def test_variables_and_routing(self):
        id=self.importar();self.ativar(id);n=self.numero(id)
        ok,_,mid=main.enviar_por_numero(n,'5511987654321',['Ana'],'Ana')
        self.assertTrue(ok);self.assertEqual(mid,'456')
        sent=[c for c in self.api.call_args_list if c.args[0].endswith('/send/')]
        self.assertEqual(sent[0].kwargs['json']['payload']['params'],{'1':'Ana'})
        self.assertEqual(sent[0].kwargs['json']['inbox_id'],'77')
        self.assertEqual(main.capacidade_lote([id])['numeros'][0]['disponivel_agora'],100)
    def test_paused_or_reauth_or_stale_blocks(self):
        id=self.importar();self.ativar(id);self.t['status']='PAUSED'
        ok,_,_=g.enviar(main.get_db,self.numero(id),'5511987654321',['Ana'],self.cfg,'',lambda _:False)
        self.assertFalse(ok);self.assertFalse(self.numero(id)['ativo'])
        self.assertFalse(any(c.args[0].endswith('/send/') for c in self.api.call_args_list))
        self.t['status']='APPROVED';self.i['reauthorization_required']=True
        with self.assertRaises(main.HTTPException):self.ativar(id)
        self.i['reauthorization_required']=False;self.ativar(id)
        with test_db() as c:c.execute('UPDATE gup_vendeai_saude SET consultado=0')
        self.assertFalse(main.disponivel_agora(self.numero(id)))
    def test_image_required_and_optout(self):
        self.t['components'].append({'type':'HEADER','format':'IMAGE'});id=self.importar()
        with self.assertRaises(main.HTTPException):self.ativar(id)
        self.t['components'].pop();self.ativar(id)
        self.assertFalse(g.enviar(main.get_db,self.numero(id),'5511987654321',['Ana'],self.cfg,'',lambda _:True)[0])
    def test_image_uses_panel_url_and_no_meta_upload(self):
        self.t['components'].append({'type':'HEADER','format':'IMAGE'});id=self.importar()
        import base64
        data='data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nimage-test').decode()
        g.editar(main.get_db,self.numero(id),{'imagem_base64':data,'ativo':True},self.cfg,'','https://painel.example')
        ok,_,_=g.enviar(main.get_db,self.numero(id),'5511987654321',['Ana'],self.cfg,'',lambda _:False)
        self.assertTrue(ok)
        sent=[c for c in self.api.call_args_list if c.args[0].endswith('/send/')][0]
        self.assertTrue(sent.kwargs['json']['payload']['header_url'].startswith('https://painel.example/midia-vendeai/'))
        self.assertFalse(any('graph.facebook' in str(c) for c in self.api.call_args_list))
    def test_changed_app_and_other_account_block(self):
        id=self.importar();self.ativar(id);n=self.numero(id)
        self.i['provider_config']['app_id']='replacement'
        self.assertFalse(g.enviar(main.get_db,n,'5511987654321',['Ana'],self.cfg,'',lambda _:False)[0])
        self.assertFalse(any(c.args[0].endswith('/send/') for c in self.api.call_args_list))
        with self.assertRaises(main.HTTPException):g.remoto(n,{**self.cfg,'vendeai_account_id':'other'},'')

    def test_fixed_values_endpoint_and_edit(self):
        id=self.importar();n=self.numero(id);url='/api/wabas-painel/'+n['waba_id']+'/textos-fixos'
        self.assertEqual(self.client.get(url).status_code,200)
        r=self.client.put(url,json={'nome':'oferta','idioma':'pt_BR','ativo':True,'valores':['Texto fixo']})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(main.textos_templates.resolver(main.get_db,n,['API'],'Ana'),['Texto fixo'])
        r=self.client.patch('/api/numeros/'+str(id),json={'via_envio':'vendeai','template_nome':'oferta','template_idioma':'pt_BR','limite_diario':300,'limite_hora':100})
        self.assertEqual(r.status_code,200,r.text)
    def test_uncertain_not_repeated(self):
        id=self.importar();self.ativar(id);original=self.post_response
        self.api.side_effect=lambda url,**kw:self.response({},503) if url.endswith('/send/') else original(url,**kw)
        ok,msg,_=g.enviar(main.get_db,self.numero(id),'5511987654321',['Ana'],self.cfg,'',lambda _:False)
        self.assertFalse(ok);self.assertIn('ENVIO_INCERTO',msg)
        self.assertEqual(len([c for c in self.api.call_args_list if c.args[0].endswith('/send/')]),1)
