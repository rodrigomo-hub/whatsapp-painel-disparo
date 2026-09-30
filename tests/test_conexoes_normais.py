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
        for body in [{**self.body,'limite_hora':0},{**self.body,'modelo_id':999}]:
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
