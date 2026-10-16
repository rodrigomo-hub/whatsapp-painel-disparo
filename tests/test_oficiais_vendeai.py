import copy,json,time,unittest
from unittest.mock import Mock,patch
import requests,main,oficiais_vendeai as oficial
import test_mailing
from test_mailing import test_db

class OficiaisTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        self.t={'name':'oferta','language':'pt_BR','status':'APPROVED','category':'UTILITY','components':[{'type':'BODY','text':'Oi {{1}}!'}]}
        self.inbox={'id':77,'name':'Oficial','phone_number':'+5511999999999','provider':'whatsapp_cloud','provider_config':{'phone_number_id':'777','business_account_id':'888','api_key':'secret-from-crm'}}
        with test_db() as c:c.execute("UPDATE numeros SET phone_number_id='777',waba_id='888',template_nome='oferta',template_idioma='pt_BR',template_status='APPROVED'")
        self.get_patch=patch.object(requests,'get',side_effect=self.get_response);self.get=self.get_patch.start();self.addCleanup(self.get_patch.stop)
        self.api.side_effect=self.post_response
        self.cfg=main.carregar_configuracao()
    def response(self,d,status=200):return Mock(status_code=status,json=Mock(return_value=d))
    def get_response(self,url,**kw):
        if url.endswith('/inboxes/77'):return self.response(dict(copy.deepcopy(self.inbox),meta_alert=None))
        if url.endswith('/inboxes/77/health'):return self.response({'expected_webhook_url':'https://crm.example/webhook','webhook_configuration':{'phone_number':'https://crm.example/webhook'}})
        if url.endswith('/inboxes'):return self.response({'payload':[copy.deepcopy(self.inbox)]})
        if url.endswith('/message_templates'):return self.response({'data':[copy.deepcopy(self.t)]})
        if url.endswith('/777'):return self.response({'status':'CONNECTED','quality_rating':'GREEN'})
        if url.endswith('/888'):return self.response({'id':'888','name':'Empresa WABA','health_status':{'can_send_message':'AVAILABLE'}})
        if url.endswith('/999'):return self.response({},403)
        raise AssertionError(url)
    def post_response(self,url,**kw):
        if url.endswith('/inboxes/'):return self.response({'inboxes':[{'id':'77','templates':[copy.deepcopy(self.t)]}]})
        if url.endswith('/send/'):return self.response({'status':'success','message_id':'901'})
        raise AssertionError(url)
    def numero(self):
        with test_db() as c:return dict(c.execute('SELECT * FROM numeros WHERE id=1').fetchone())
    def vincular(self):return oficial.vincular(main.get_db,self.cfg,'',{'inbox_id':'77','numero_id':1})
    def habilitar(self):
        self.vincular()
        with test_db() as c:c.execute("UPDATE numeros SET via_envio='vendeai',envio_base_url='https://painel.example'")
        main.webhook_saude.consultar(main.get_db,self.numero(),self.cfg,'')
    def enviar(self,vars=None):
        return oficial.enviar(main.get_db,self.numero(),'5511987654321',vars if vars is not None else ['Maria'],self.cfg,'',main.atualizar_status_numero,lambda _:False)
    def sends(self):return [c for c in self.api.call_args_list if c.args[0].endswith('/send/')]
    def test_discovery_auto_matches_existing_and_redacts_credentials(self):
        oficial.descobrir(main.get_db,self.cfg,'');d=oficial.listar(main.get_db,self.cfg)
        self.assertEqual(d[0]['numero']['id'],1);self.assertNotIn('secret',json.dumps(d));self.assertEqual(self.numero()['vendeai_inbox_id'],'77')
        self.assertEqual(d[0]['nome'],'Empresa WABA')
    def test_link_preserves_configuration_and_history(self):
        with test_db() as c:c.execute("UPDATE numeros SET ativo=0,limite_diario=81,limite_hora=9,observacao='anotação'")
        antes=self.numero();self.vincular();depois=self.numero()
        for k in antes:
            if k not in ('vendeai_conta','vendeai_inbox_id'):self.assertEqual(antes[k],depois[k],k)
        self.vincular()
        with test_db() as c:self.assertEqual(c.execute('SELECT count(*) FROM numeros').fetchone()[0],1)
    def test_waba_conflict_blocks_link(self):
        self.inbox['provider_config']['business_account_id']='999'
        with self.assertRaises(main.HTTPException):self.vincular()
        self.assertEqual(self.numero()['vendeai_inbox_id'],'')
    def test_discovery_repairs_missing_waba_for_existing_number(self):
        with test_db() as c:c.execute("DELETE FROM wabas_salvas")
        antes=self.numero()
        oficial.descobrir(main.get_db,self.cfg,'')
        with test_db() as c:
            w=c.execute("SELECT * FROM wabas_salvas WHERE waba_id='888'").fetchone()
            self.assertEqual(w['access_token'],'secret-from-crm')
            self.assertEqual(w['nome'],'Empresa WABA')
        for k in ('ativo','limite_diario','limite_hora','access_token','template_nome'):
            self.assertEqual(self.numero()[k],antes[k])
    def test_existing_link_refreshes_waba_without_duplicates(self):
        with test_db() as c:
            c.execute("INSERT OR REPLACE INTO wabas_salvas(waba_id,access_token,nome) VALUES('888','old','Empresa WABA')")
        self.vincular();self.vincular()
        with test_db() as c:
            rows=c.execute("SELECT * FROM wabas_salvas WHERE waba_id='888'").fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['access_token'],'secret-from-crm')
    def test_duplicate_requires_explicit_existing_selection(self):
        with self.assertRaises(main.HTTPException):oficial.vincular(main.get_db,self.cfg,'',{'inbox_id':'77'})
    def test_new_number_imports_paused_with_token_server_side(self):
        self.inbox['provider_config']['phone_number_id']='778'
        r=oficial.vincular(main.get_db,self.cfg,'',{'inbox_id':'77','template_nome':'oferta','template_idioma':'pt_BR','limite_diario':40,'limite_hora':5})
        with test_db() as c:
            n=c.execute('SELECT * FROM numeros WHERE id=?',(r['id'],)).fetchone()
            self.assertEqual(n['ativo'],0);self.assertEqual(n['access_token'],'secret-from-crm');self.assertEqual(n['via_envio'],'meta')
            self.assertEqual(n['nome'],'Empresa WABA +5511999999999')
            w=c.execute("SELECT * FROM wabas_salvas WHERE waba_id='888'").fetchone()
            self.assertEqual(w['nome'],'Empresa WABA')
            self.assertEqual(w['access_token'],'secret-from-crm')
    def test_discovery_replaces_inbox_name_but_preserves_custom_name(self):
        with test_db() as c:c.execute("UPDATE numeros SET nome='Oficial'")
        oficial.descobrir(main.get_db,self.cfg,'')
        self.assertEqual(self.numero()['nome'],'Empresa WABA +5511999999999')
        with test_db() as c:c.execute("UPDATE numeros SET nome='Nome personalizado'")
        oficial.descobrir(main.get_db,self.cfg,'')
        self.assertEqual(self.numero()['nome'],'Nome personalizado')
    def test_official_payload_and_label_task(self):
        self.habilitar();self.assertTrue(self.enviar()[0]);j=self.sends()[0].kwargs['json']
        self.assertEqual(j['payload'],{'type':'whatsapp_oficial','message':'Oi {{1}}!','template_name':'oferta','template_category':'UTILITY','language':'pt_BR','params':{'1':'Maria'}})
        self.assertEqual(j['inbox_id'],'77')
        with test_db() as c:self.assertEqual(c.execute('SELECT message_id FROM etiquetas_disparo').fetchone()[0],'901')
        with test_db() as c:self.assertEqual(c.execute('SELECT etiqueta FROM etiquetas_disparo').fetchone()[0],'disparo')
    def test_bad_webhook_blocks_capacity_and_send(self):
        self.habilitar();original=self.get.side_effect
        self.get.side_effect=lambda url,**kw:self.response({'expected_webhook_url':'https://crm.example/webhook','webhook_configuration':{'application':'https://wrong.example'}}) if url.endswith('/health') else original(url,**kw)
        self.assertFalse(self.enviar()[0]);self.assertFalse(self.sends())
        self.assertFalse(main.disponivel_agora(self.numero()))
        self.assertEqual(self.numero()['webhook_estado'],'MISMATCH')
    def test_static_ignores_incoming_variables(self):
        self.habilitar();self.t['components'][0]['text']='Olá!'
        self.assertTrue(self.enviar(['ignored'])[0]);self.assertEqual(self.sends()[0].kwargs['json']['payload']['params'],{})
    def test_missing_extra_or_empty_variables_block(self):
        self.habilitar()
        for v in ([],[''],['Maria','extra']):self.assertFalse(self.enviar(v)[0])
        self.assertFalse(self.sends())
    def test_unknown_meta_status_or_quality_blocks_before_send(self):
        self.habilitar();original=self.get.side_effect
        self.get.side_effect=lambda url,**kw:self.response({'status':'CONNECTED','quality_rating':'UNKNOWN'}) if url.endswith('/777') else original(url,**kw)
        self.assertFalse(self.enviar()[0]);self.assertFalse(self.sends())
    def test_template_paused_or_changed_blocks(self):
        self.habilitar();self.t['status']='PAUSED';self.assertFalse(self.enviar()[0]);self.assertFalse(self.sends())
    def test_wrong_template_language_or_body_blocks(self):
        self.habilitar();original=self.api.side_effect
        def post(url,**kw):
            if url.endswith('/inboxes/'):
                t=copy.deepcopy(self.t);t['components'][0]['text']='Outra mensagem {{1}}'
                return self.response({'inboxes':[{'id':'77','templates':[t]}]})
            return original(url,**kw)
        self.api.side_effect=post;self.assertFalse(self.enviar()[0]);self.assertFalse(self.sends())
    def test_changed_inbox_identity_blocks(self):
        self.habilitar();self.inbox['provider_config']['phone_number_id']='999'
        self.assertFalse(self.enviar()[0]);self.assertFalse(self.sends())
    def test_timeout_stays_uncertain_no_sms_or_meta_retry(self):
        self.habilitar();original=self.api.side_effect
        def post(url,**kw):
            if url.endswith('/send/'):raise requests.Timeout()
            return original(url,**kw)
        self.api.side_effect=post
        with patch.object(main,'tentar_fallback_sms') as sms,patch.object(main,'dentro_do_horario_permitido',return_value=(True,'')):
            r=main.executar_disparo_preparado({'mailing':{'phone':'5511987654321','name':'Maria'},'variaveis_template':['Maria']},{},numeros=[1])
        self.assertEqual(r['status'],'incerto');sms.assert_not_called();self.whatsapp.assert_not_called();self.assertEqual(len(self.sends()),1)
    def test_configuration_changes_path_only_after_compatibility(self):
        r=self.client.patch('/api/numeros/1',json={'via_envio':'vendeai'});self.assertEqual(r.status_code,400)
        self.vincular();r=self.client.patch('/api/numeros/1',json={'via_envio':'vendeai'});self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.numero()['via_envio'],'vendeai')
        r=self.client.patch('/api/numeros/1',json={'via_envio':'meta'});self.assertEqual(r.status_code,200)
    def test_image_uses_expiring_url_not_meta_media_id(self):
        self.habilitar();self.t['components'].insert(0,{'type':'HEADER','format':'IMAGE'})
        with test_db() as c:
            c.execute("INSERT INTO imagens_template VALUES('image','777',?,'image/png','media',?)",(b'png-test',time.time()))
            c.execute("UPDATE numeros SET template_imagem='image',template_header='IMAGE'")
        self.assertTrue(self.enviar()[0]);p=self.sends()[0].kwargs['json']['payload'];self.assertEqual(p['header_media_type'],'image')
        path='/midia-vendeai/'+p['header_url'].rsplit('/',1)[1];self.client.cookies.clear()
        r=self.client.get(path);self.assertEqual(r.content,b'png-test');self.assertEqual(r.headers['content-type'],'image/png')
        with test_db() as c:c.execute('UPDATE midias_vendeai SET expira=0')
        self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.client.get('/midia-vendeai/'+'0'*64).status_code,404)
    def test_model_rejects_dynamic_buttons_and_named_parameters(self):
        for t in [{'components':[{'type':'BODY','text':'Olá {{nome}}'}]}, {'components':[{'type':'BODY','text':'Olá'},{'type':'BUTTONS','buttons':[{'type':'URL','url':'https://example.com/{{1}}'}]}]}]:
            with self.assertRaises(main.HTTPException):oficial.estrutura(t)

    def test_reauthorization_blocks(self):
        self.habilitar();self.inbox['reauthorization_required']=True
        self.assertFalse(self.enviar()[0]);self.assertFalse(self.sends())
    def test_local_image_does_not_upload_to_meta_until_direct_path(self):
        import imagens_template,base64
        dados=b'\x89PNG\r\n\x1a\nmock'
        image='data:image/png;base64,'+base64.b64encode(dados).decode()
        self.api.reset_mock()
        chave=imagens_template.salvar(main.get_db,self.numero(),image,somente_local=True)
        self.api.assert_not_called()
        n=self.numero();n['template_imagem']=chave
        with patch.object(imagens_template,'upload',return_value='media-id') as upload:
            self.assertEqual(imagens_template.media_id(main.get_db,n),'media-id');upload.assert_called_once()
    def test_optout_after_metadata_check_prevents_send(self):
        self.habilitar()
        r=oficial.enviar(main.get_db,self.numero(),'5511987654321',['Maria'],self.cfg,'',main.atualizar_status_numero,lambda _:True)
        self.assertFalse(r[0]);self.assertFalse(self.sends())

    def test_auto_match_only_changes_identity_link(self):
        antes=self.numero()
        oficial.descobrir(main.get_db,self.cfg,'')
        depois=self.numero()
        for k in antes:
            if k not in ('vendeai_conta','vendeai_inbox_id'):self.assertEqual(antes[k],depois[k],k)
        oficial.descobrir(main.get_db,self.cfg,'')
        with test_db() as c:self.assertEqual(c.execute('SELECT count(*) FROM numeros').fetchone()[0],1)
    def test_discovery_does_not_add_new_number(self):
        self.inbox['provider_config']['phone_number_id']='778'
        oficial.descobrir(main.get_db,self.cfg,'')
        self.assertEqual(self.numero()['vendeai_inbox_id'],'')
        with test_db() as c:self.assertEqual(c.execute('SELECT count(*) FROM numeros').fetchone()[0],1)
    def test_ambiguous_remote_connections_not_auto_linked(self):
        original=oficial.listar_remoto(self.cfg,'');outro=copy.deepcopy(original[0]);outro['inbox_id']='78'
        with patch.object(oficial,'listar_remoto',return_value=original+[outro]):oficial.descobrir(main.get_db,self.cfg,'')
        self.assertEqual(self.numero()['vendeai_inbox_id'],'')
        self.assertTrue(all(i['divergencia'] for i in oficial.listar(main.get_db,self.cfg)))
