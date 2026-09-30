import unittest,json
from unittest.mock import Mock,patch
import requests,main,conexoes_normais as normal
import test_mailing
from test_mailing import test_db

class ConexoesTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        self.cfg=main.carregar_configuracao()
        self.item={'id':77,'name':'Normal','phone_number':'+5567981292150','provider':'uazapi','provider_connection':{'connection':'connected'},'provider_config':{'secret':'never-public'}}
        self.get_patch=patch.object(normal.requests,'get',return_value=self.response({'payload':[self.item]}))
        self.get=self.get_patch.start();self.addCleanup(self.get_patch.stop)
        self.api.return_value=self.response({'inboxes':[{'id':'77','accepts_free_text':True}]})
        self.modelo=normal.salvar_modelo(main.get_db,{'nome':'Saudação','texto':'Oi, *{primeiro_nome}*!','ativo':True})['id']
        self.body={'inbox_id':'77','modelo_id':self.modelo,'limite_diario':5,'limite_hora':2}
    def response(self,d,status=200):return Mock(status_code=status,json=Mock(return_value=d))
    def numero(self):
        with test_db() as c:return c.execute("SELECT * FROM numeros WHERE tipo_envio='vendeai_normal'").fetchone()
    def importar(self):
        result=normal.importar(main.get_db,self.cfg,'',self.body)
        self.get.return_value=self.response(self.item)
        self.api.reset_mock();self.get.reset_mock()
        return result['id']
    def enviar(self,nome='MARIA SILVA',suprimido=lambda p:False):
        return normal.enviar(main.get_db,self.numero(),'5511987654321',nome,self.cfg,'',suprimido)
    def test_discovery_no_activation_or_secret_exposure(self):
        self.item['provider_config']['access_token']='super-secret'
        normal.descobrir(main.get_db,self.cfg,'')
        self.assertIsNone(self.numero())
        d=self.client.get('/api/conexoes-normais').json()
        self.assertEqual(len(d['conexoes']),1)
        self.assertNotIn('secret',json.dumps(d))
        self.assertNotIn('test-secret',json.dumps(d))
    def test_cloud_and_without_explicit_free_text_not_discovered(self):
        self.item['provider']='whatsapp_cloud'
        normal.descobrir(main.get_db,self.cfg,'')
        self.assertEqual(self.client.get('/api/conexoes-normais').json()['conexoes'],[])
    def test_import_requires_model_limits_connection_and_deduplicates(self):
        for body in [{**self.body,'limite_hora':-1},{**self.body,'modelo_id':999}]:
            r=self.client.post('/api/conexoes-normais/importar',json=body);self.assertEqual(r.status_code,400)
        r=self.client.post('/api/conexoes-normais/importar',json=self.body);self.assertEqual(r.status_code,200,r.text)
        r=self.client.post('/api/conexoes-normais/importar',json=self.body);self.assertEqual(r.status_code,409)
        self.assertTrue(main.disponivel_agora(self.numero()))
        self.assertEqual(self.numero()['access_token'],'')
    def test_send_uses_local_message_no_meta_template(self):
        self.importar();self.api.return_value=self.response({'status':'success','message_id':'123'})
        self.assertEqual(self.enviar(),(True,'','123'))
        j=self.api.call_args.kwargs['json']
        self.assertEqual(j['payload'],{'type':'whatsapp_nao_oficial','message':'Oi, *Maria*!'})
        self.assertEqual(j['customer_phone'],'+5511987654321')
        self.assertEqual(j['inbox_id'],'77');self.whatsapp.assert_not_called()
    def test_missing_name_blacklist_and_model_disabled_prevent_send(self):
        self.importar()
        self.assertFalse(self.enviar('')[0]);self.api.assert_not_called()
        self.assertFalse(self.enviar(suprimido=lambda p:True)[0]);self.api.assert_not_called()
        with test_db() as c:c.execute('UPDATE modelos_normais SET ativo=0')
        self.assertFalse(self.enviar()[0]);self.api.assert_not_called()
        self.assertFalse(main.disponivel_agora(self.numero()))
        r=self.client.patch('/api/numeros/'+str(self.numero()['id']),json={'ativo':False})
        self.assertEqual(r.status_code,200);self.assertEqual(self.numero()['ativo'],0)
    def test_disconnect_unknown_and_reconnect_respect_manual_pause(self):
        self.importar()
        for state in ('disconnected','unexpected'):
            self.item['provider_connection']['connection']=state
            normal.consultar(main.get_db,self.numero(),self.cfg,'')
            self.assertFalse(main.disponivel_agora(self.numero()))
            self.assertEqual(self.numero()['ativo'],1)
        self.item['provider_connection']['connection']='connected'
        normal.consultar(main.get_db,self.numero(),self.cfg,'')
        self.assertTrue(main.disponivel_agora(self.numero()))
        normal.editar(main.get_db,self.numero(),{'ativo':False})
        normal.consultar(main.get_db,self.numero(),self.cfg,'')
        self.assertFalse(main.disponivel_agora(self.numero()))
    def test_stale_or_other_account_excluded(self):
        self.importar()
        with test_db() as c:c.execute("UPDATE numeros SET meta_consultado_em='2020-01-01T00:00:00-03:00' WHERE tipo_envio='vendeai_normal'")
        self.assertFalse(main.disponivel_agora(self.numero()))
        normal.consultar(main.get_db,self.numero(),self.cfg,'')
        with test_db() as c:c.execute("UPDATE configuracao SET vendeai_account_id='999'")
        self.assertFalse(main.disponivel_agora(self.numero()))

    def test_discovery_updates_registered_connection_and_ui_eligibility(self):
        ident=self.importar()
        self.item['provider_connection']['connection']='disconnected'
        self.get.return_value=self.response({'payload':[self.item]})
        self.api.return_value=self.response({'inboxes':[{'id':'77','accepts_free_text':True}]})
        normal.descobrir(main.get_db,self.cfg,'')
        self.assertEqual(self.numero()['conexao_status'],'disconnected')
        self.assertFalse(main.disponivel_agora(self.numero()))
        self.assertEqual(self.numero()['ativo'],1)
        self.assertFalse(next(n for n in self.client.get('/api/numeros').json() if n['id']==ident)['conexao_elegivel'])
        self.assertFalse(self.client.get('/api/conexoes-normais').json()['numeros'][0]['conexao_elegivel'])
        normal.editar(main.get_db,self.numero(),{'ativo':False})
        self.item['provider_connection']['connection']='connected'
        normal.descobrir(main.get_db,self.cfg,'')
        self.assertEqual(self.numero()['conexao_status'],'connected')
        self.assertEqual(self.numero()['ativo'],0)
        self.assertFalse(main.disponivel_agora(self.numero()))

    def test_missing_connection_in_discovery_blocks_registered_number(self):
        self.importar()
        self.get.return_value=self.response({'payload':[]})
        self.api.return_value=self.response({'inboxes':[]})
        normal.descobrir(main.get_db,self.cfg,'')
        self.assertEqual(self.numero()['conexao_status'],'unknown')
        self.assertFalse(main.disponivel_agora(self.numero()))
    def test_mixed_rotation_and_capacity_limits(self):
        ident=self.importar()
        self.assertEqual(main.proximo_numero_disponivel(1)['id'],ident)
        self.assertEqual(main.proximo_numero_disponivel(ident)['id'],1)
        self.assertEqual(next(n for n in main.capacidade_lote()['numeros'] if n['id']==ident)['disponivel_agora'],2)
        for i in range(2):main.registrar_disparo(ident,'','Test','5511987654321','incerto')
        self.assertFalse(main.disponivel_agora(self.numero()))
    def test_uncertain_send_no_fallback_and_recorded_once(self):
        ident=self.importar();self.api.side_effect=requests.Timeout()
        prepared={'mailing':{'phone':'5511987654321','name':'Maria','cpf':''},'variaveis_template':[]}
        with patch.object(main,'tentar_fallback_sms') as sms,patch.object(main,'dentro_do_horario_permitido',return_value=(True,'')):
            r=main.executar_disparo_preparado(prepared,{},numeros=[ident])
        self.assertEqual(r['status'],'incerto');sms.assert_not_called();self.whatsapp.assert_not_called()
        self.assertEqual(self.api.call_count,1)
        with test_db() as c:self.assertEqual(c.execute('SELECT status FROM disparos').fetchone()[0],'incerto')
    def test_model_deactivated_while_checking_connection(self):
        self.importar()
        def checking(*args,**kwargs):
            with test_db() as c:c.execute('UPDATE modelos_normais SET ativo=0')
            return self.response(self.item)
        self.get.side_effect=checking
        self.assertFalse(self.enviar()[0]);self.api.assert_not_called()
    def test_official_status_not_changed_by_migration(self):
        main.init_db()
        with test_db() as c:
            n=c.execute('SELECT * FROM numeros WHERE id=1').fetchone()
            self.assertEqual(n['tipo_envio'],'meta');self.assertTrue(main.disponivel_agora(n))
    def test_normal_page_and_unauthenticated_protection(self):
        self.assertEqual(self.client.get('/conexoes').status_code,200)
        self.client.cookies.clear()
        self.assertIn(self.client.post('/api/conexoes-normais/importar',json=self.body,follow_redirects=False).status_code,(401,303,307))
    def test_invalid_variables_rejected(self):
        with self.assertRaises(main.HTTPException):normal.salvar_modelo(main.get_db,{'nome':'x','texto':'Oi {cpf}'})

    def test_default_zero_limits_import_without_allowing_send(self):
        self.assertEqual(normal.padrao(main.get_db,self.cfg),{'modelo_id':self.modelo,'limite_diario':0,'limite_hora':0})
        normal.importar(main.get_db,self.cfg,'',{'inbox_id':'77'})
        self.api.reset_mock()
        self.assertEqual(self.numero()['limite_diario'],0)
        self.assertFalse(main.disponivel_agora(self.numero()))
        self.assertFalse(self.enviar()[0]);self.api.assert_not_called()

    def test_rotation_type_filters_capacity_selection_and_direct_dispatch(self):
        ident=self.importar()
        for tipo,esperado,bloqueado in [('normais',ident,1),('oficiais',1,ident)]:
            r=self.client.patch('/api/configuracao',json={'rodizio_tipo':tipo})
            self.assertEqual(r.status_code,200)
            self.assertEqual(main.proximo_numero_disponivel(None)['id'],esperado)
            detalhes=main.capacidade_lote([bloqueado])['numeros']
            self.assertFalse(detalhes[0]['elegivel']);self.assertEqual(detalhes[0]['disponivel_agora'],0)
            with test_db() as c:n=c.execute('SELECT * FROM numeros WHERE id=?',(bloqueado,)).fetchone()
            with patch.object(main,'enviar_whatsapp') as meta,patch.object(normal,'enviar') as enviar:
                self.assertFalse(main.enviar_por_numero(n,'5511987654321',[],'Maria')[0]);meta.assert_not_called();enviar.assert_not_called()
        self.client.patch('/api/configuracao',json={'rodizio_tipo':'todos'})
        self.assertEqual(main.proximo_numero_disponivel(1)['id'],ident)
        self.assertEqual(self.client.patch('/api/configuracao',json={'rodizio_tipo':'invalido'}).status_code,400)
        self.assertEqual(main.carregar_configuracao()['rodizio_tipo'],'todos')

    def test_bulk_import_only_connected_deduplicates_and_preserves_existing(self):
        import copy
        self.importar();before=dict(self.numero())
        items=[copy.deepcopy(self.item) for _ in range(3)]
        items[1]['id']=78;items[2]['id']=79;items[2]['provider_connection']['connection']='disconnected'
        self.get.return_value=self.response({'payload':items})
        self.api.return_value=self.response({'inboxes':[{'id':str(i),'accepts_free_text':True} for i in (77,78,79)]})
        r=self.client.post('/api/conexoes-normais/importar-todas')
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['adicionados'],1)
        after=dict(self.numero())
        for k in ('nome','ativo','modelo_normal_id','limite_diario','limite_hora'):self.assertEqual(before[k],after[k])
        with test_db() as c:
            n=c.execute("SELECT * FROM numeros WHERE vendeai_inbox_id='78'").fetchone()
            self.assertEqual(n['limite_diario'],0);self.assertEqual(n['modelo_normal_id'],self.modelo)
            self.assertFalse(normal.elegivel(main.get_db,n))
        self.assertEqual(self.client.post('/api/conexoes-normais/importar-todas').json()['adicionados'],0)

    def test_saved_defaults_scoped_and_disabled_model_rejected(self):
        r=self.client.patch('/api/conexoes-normais/padrao',json={'modelo_id':self.modelo,'limite_diario':30,'limite_hora':5})
        self.assertEqual(r.status_code,200)
        self.assertEqual(normal.padrao(main.get_db,self.cfg)['limite_diario'],30)
        self.assertEqual(normal.padrao(main.get_db,{**self.cfg,'vendeai_account_id':'999'})['limite_diario'],0)
        with test_db() as c:c.execute('UPDATE modelos_normais SET ativo=0')
        r=self.client.post('/api/conexoes-normais/importar-todas')
        self.assertEqual(r.status_code,400);self.assertIsNone(self.numero())

    def test_account_change_blocks_send_without_http_post(self):
        self.importar();self.cfg['vendeai_account_id']='999'
        self.assertFalse(self.enviar()[0]);self.api.assert_not_called()
    def test_paused_during_connection_check_blocks_send(self):
        self.importar()
        def checking(*args,**kwargs):
            normal.editar(main.get_db,self.numero(),{'ativo':False})
            return self.response(self.item)
        self.get.side_effect=checking
        self.assertFalse(self.enviar()[0]);self.api.assert_not_called()
    def test_reauthorization_required_blocks_even_if_connected(self):
        self.importar();self.item['reauthorization_required']=True
        self.assertFalse(self.enviar()[0]);self.api.assert_not_called()
    def test_static_message_allows_missing_name(self):
        self.importar();normal.salvar_modelo(main.get_db,{'nome':'Fixo','texto':'Olá!'},self.modelo)
        self.api.return_value=self.response({'status':'success','message_id':'123'})
        self.assertTrue(self.enviar('')[0])
    def test_queue_uncertainty_is_terminal_and_no_retry(self):
        import time
        ident=self.importar()
        with test_db() as c:c.execute('UPDATE configuracao SET fila_habilitada=1,fila_intervalo_segundos=300')
        cfg=main.carregar_configuracao()
        preparado={'mailing':{'phone':'5511987654321','name':'Maria'},'variaveis_template':[]}
        fila=main.obter_fila()
        fila.enfileirar([(preparado,{},'individual',[ident],None)],cfg)
        with test_db() as c:c.execute("UPDATE fila_disparos SET status='pronto',cadastro_em=?,cadastro='{}'",(time.time(),))
        self.api.side_effect=requests.Timeout()
        for _ in range(2):fila.processar(main.carregar_configuracao,Mock(),main.executar_disparo_preparado,lambda:(True,''))
        with test_db() as c:self.assertEqual(c.execute('SELECT status FROM fila_disparos').fetchone()[0],'incerto')
        self.assertEqual(self.api.call_count,1)
    def preparar_etiqueta(self):
        normal.agendar_etiqueta(main.get_db,'33393','901','77','5511987654321',1)
        self.labels=['clt','em_atendimento']
        def get(url,**kw):
            if url.endswith('/contacts/search'):return self.response({'payload':[{'id':2,'phone_number':'+5511987654321'}]})
            if url.endswith('/contacts/2/conversations'):return self.response({'payload':[{'id':8,'inbox_id':88},{'id':9,'inbox_id':77}]})
            if url.endswith('/conversations/9/messages'):return self.response({'payload':[{'id':901,'conversation_id':9}]})
            if url.endswith('/conversations/9/labels'):return self.response({'payload':list(self.labels)})
            raise AssertionError('Endpoint inesperado '+url)
        def post(url,**kw):
            self.assertTrue(url.endswith('/conversations/9/labels'))
            self.labels=kw['json']['labels']
            return self.response({'payload':self.labels})
        self.get.side_effect=get;self.api.side_effect=post
    def test_tag_preserves_labels_exact_chat_and_idempotence(self):
        self.preparar_etiqueta()
        normal.processar_etiqueta(main.get_db,self.cfg,'')
        self.assertEqual(self.labels,['clt','em_atendimento','disparo'])
        self.api.assert_called_once()
        normal.agendar_etiqueta(main.get_db,'33393','901','77','5511987654321',1)
        self.assertFalse(normal.processar_etiqueta(main.get_db,self.cfg,''))
        self.api.assert_called_once()
        with test_db() as c:self.assertEqual(c.execute('SELECT estado FROM etiquetas_disparo').fetchone()[0],'aplicada')
    def test_tag_never_guesses_conversation(self):
        self.preparar_etiqueta();original=self.get.side_effect
        def get(url,**kw):
            if url.endswith('/messages'):return self.response({'payload':[]})
            return original(url,**kw)
        self.get.side_effect=get
        normal.processar_etiqueta(main.get_db,self.cfg,'')
        self.api.assert_not_called()
        with test_db() as c:self.assertEqual(c.execute('SELECT estado FROM etiquetas_disparo').fetchone()[0],'pendente')
    def test_tag_retry_reads_latest_labels_and_never_resends(self):
        self.preparar_etiqueta();self.api.side_effect=requests.Timeout()
        normal.processar_etiqueta(main.get_db,self.cfg,'')
        self.labels.append('disparo')
        with test_db() as c:c.execute('UPDATE etiquetas_disparo SET tentar_em=0')
        normal.processar_etiqueta(main.get_db,self.cfg,'')
        self.api.assert_called_once();self.whatsapp.assert_not_called()
        with test_db() as c:self.assertEqual(c.execute('SELECT estado FROM etiquetas_disparo').fetchone()[0],'aplicada')
    def test_invalid_labels_do_not_replace_existing_list(self):
        self.preparar_etiqueta();original=self.get.side_effect
        self.get.side_effect=lambda url,**kw:self.response({'payload':None}) if url.endswith('/labels') else original(url,**kw)
        normal.processar_etiqueta(main.get_db,self.cfg,'');self.api.assert_not_called()
    def test_tag_stops_after_three_failures_and_records_event(self):
        self.preparar_etiqueta();self.get.side_effect=requests.Timeout()
        for _ in range(3):
            with test_db() as c:c.execute('UPDATE etiquetas_disparo SET tentar_em=0')
            normal.processar_etiqueta(main.get_db,self.cfg,'')
        with test_db() as c:
            self.assertEqual(c.execute('SELECT estado FROM etiquetas_disparo').fetchone()[0],'erro')
            self.assertEqual(c.execute("SELECT count(*) FROM eventos WHERE tipo='erro_etiqueta_disparo'").fetchone()[0],1)
        self.api.assert_not_called()
    def test_success_queues_tag_without_delaying_send(self):
        self.importar();self.api.return_value=self.response({'status':'success','message_id':'901'})
        self.assertTrue(self.enviar()[0]);self.api.assert_called_once()
        with test_db() as c:
            t=c.execute('SELECT * FROM etiquetas_disparo').fetchone()
            self.assertEqual(t['estado'],'pendente');self.assertEqual(t['message_id'],'901')
