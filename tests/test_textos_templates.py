import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import main
import textos_templates as fixos
import test_mailing
from test_mailing import test_db


class TextosFixosTests(unittest.TestCase):
    setUp=test_mailing.MailingTests.setUp
    resposta=staticmethod(test_mailing.MailingTests.resposta)

    def preparar(self):
        with test_db() as c:
            c.execute("UPDATE numeros SET waba_id='888',template_nome='oferta',template_idioma='pt_BR'")
            c.execute("INSERT OR REPLACE INTO wabas_salvas(waba_id,access_token) VALUES('888','token')")
        self.template={'name':'oferta','language':'pt_BR','components':[{'type':'BODY','text':'Oi {{1}}, {{2}}'}]}

    def salvar(self,**kw):
        body={'nome':'oferta','idioma':'pt_BR','ativo':True,'valores':['Texto fixo','Outra frase'],**kw}
        with patch.object(main,'consultar_meta_waba',return_value={'dados':[self.template]}):
            return self.client.put('/api/wabas-painel/888/textos-fixos',json=body)

    def numero(self):
        with test_db() as c:return dict(c.execute('SELECT * FROM numeros WHERE id=1').fetchone())

    def test_priority_over_n8n_values_and_empty_values(self):
        self.preparar();self.assertEqual(self.salvar().status_code,200)
        for recebidos in (['API','API 2'],[],['','']):
            self.assertEqual(fixos.resolver(main.get_db,self.numero(),recebidos),['Texto fixo','Outra frase'])
            self.assertFalse(main.variaveis_vazias_impedem(recebidos))
            main.enviar_por_numero(self.numero(),'5511987654321',recebidos,'Maria')
            self.assertEqual(self.whatsapp.call_args.args[2],['Texto fixo','Outra frase'])

    def test_editor_loads_templates_and_saved_values_without_token(self):
        self.preparar();self.salvar()
        with patch.object(main,'consultar_meta_waba',return_value={'dados':[self.template]}) as consulta:
            r=self.client.get('/api/wabas-painel/888/textos-fixos')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['templates'][0]['name'],'oferta')
        self.assertEqual(r.json()['textos_fixos'][0]['valores'],['Texto fixo','Outra frase'])
        self.assertNotIn('access_token',r.text)
        self.assertEqual(consulta.call_count,1)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/wabas-painel/888/textos-fixos').status_code,401)

    def test_vendeai_official_receives_fixed_values(self):
        self.preparar();self.salvar()
        n={**self.numero(),'via_envio':'vendeai'}
        with patch.object(main.oficiais_vendeai,'enviar',return_value=(True,'','id')) as enviar:
            main.enviar_por_numero(n,'5511987654321',['n8n'],'Maria')
            self.assertEqual(enviar.call_args.args[3],['Texto fixo','Outra frase'])

    def test_meta_payload_contains_fixed_texts(self):
        self.preparar();self.salvar();self.whatsapp_patch.stop()
        self.api.return_value.status_code=200
        self.api.return_value.json.return_value={'messages':[{'id':'test'}]}
        self.assertTrue(main.enviar_por_numero(self.numero(),'5511987654321',['n8n'],'Maria')[0])
        payload=self.api.call_args.kwargs['json']
        self.assertEqual(payload['template']['components'][0]['parameters'],[
            {'type':'text','text':'Texto fixo'},{'type':'text','text':'Outra frase'}])

    def test_scope_shared_by_numbers_but_not_other_waba_language_or_template(self):
        self.preparar();self.salvar();n=self.numero()
        self.assertEqual(fixos.resolver(main.get_db,{**n,'id':900},['API']),['Texto fixo','Outra frase'])
        for alteracao in ({'waba_id':'999'},{'template_nome':'outro'},{'template_idioma':'pt_PT'},{'tipo_envio':'vendeai_normal'}):
            self.assertEqual(fixos.resolver(main.get_db,{**n,**alteracao},['API']),['API'])

    def test_disable_preserves_texts_and_restores_incoming_values(self):
        self.preparar();self.salvar();self.assertEqual(self.salvar(ativo=False).status_code,200)
        self.assertEqual(fixos.resolver(main.get_db,self.numero(),['API']),['API'])
        self.assertTrue(main.variaveis_vazias_impedem(['']))
        self.assertEqual(fixos.listar(main.get_db,'888')[0]['valores'],['Texto fixo','Outra frase'])

    def test_invalid_values_do_not_replace_saved_configuration(self):
        self.preparar();self.salvar()
        for valores in ([],['one'],['one','two','three'],['','x'],[' '*5,'x'],['x'*1025,'x'],[1,'x']):
            self.assertEqual(self.salvar(valores=valores).status_code,400)
        self.assertEqual(fixos.resolver(main.get_db,self.numero(),[]),['Texto fixo','Outra frase'])

    def test_meta_failure_missing_template_and_unsupported_format(self):
        self.preparar()
        body={'nome':'oferta','idioma':'pt_BR','ativo':True,'valores':['A','B']}
        with patch.object(main,'consultar_meta_waba',return_value={'erro':'Indisponivel'}):
            self.assertEqual(self.client.put('/api/wabas-painel/888/textos-fixos',json=body).status_code,502)
        self.assertEqual(self.salvar(nome='nao_existe').status_code,400)
        self.template['components'][0]['text']='Oi {{nome}}'
        self.assertEqual(self.salvar(valores=['Maria']).status_code,400)
        self.assertEqual(fixos.listar(main.get_db,'888'),[])

    def test_admin_session_required(self):
        self.preparar()
        client=TestClient(main.app)
        try:
            r=client.put('/api/wabas-painel/888/textos-fixos',json={})
            self.assertEqual(r.status_code,401)
        finally:client.close()

    def test_individual_and_spreadsheet_batch_use_fixed_values(self):
        self.preparar();self.salvar()
        with patch.object(main,'dentro_do_horario_permitido',return_value=(True,'')):
            r=self.client.post('/disparar',json={**self.lead,'variaveis_template':['','']})
            self.assertEqual(r.json()['status'],'sucesso',r.text)
            self.assertEqual(self.whatsapp.call_args.args[2],['Texto fixo','Outra frase'])
            self.whatsapp.reset_mock()
            r=self.client.post('/api/disparo-lote',json={'leads':[{**self.lead,'variaveis_template':[]}],'numeros_selecionados':[1]})
            self.assertEqual(r.status_code,200,r.text)
            self.whatsapp.assert_called_once()
            self.assertEqual(self.whatsapp.call_args.args[2],['Texto fixo','Outra frase'])

    def test_mixed_name_and_fixed_override_api(self):
        self.preparar()
        self.assertEqual(self.salvar(valores=[{'tipo':'primeiro_nome'},'Oferta CLT']).status_code,200)
        self.assertEqual(fixos.resolver(main.get_db,self.numero(),['Nome errado','API'],'  MARIA Silva '),['Maria','Oferta CLT'])
        main.enviar_por_numero(self.numero(),'5511987654321',['Outro','API'],'JOAO Silva')
        self.assertEqual(self.whatsapp.call_args.args[2],['Joao','Oferta CLT'])
        n={**self.numero(),'via_envio':'vendeai'}
        with patch.object(main.oficiais_vendeai,'enviar',return_value=(True,'','id')) as enviar:
            main.enviar_por_numero(n,'5511987654321',[],'Ana Silva')
            self.assertEqual(enviar.call_args.args[3],['Ana','Oferta CLT'])

    def test_missing_name_never_sends_marker_or_api_name(self):
        self.preparar();self.salvar(valores=[{'tipo':'primeiro_nome'},'Texto'])
        self.whatsapp.reset_mock()
        result=main.enviar_por_numero(self.numero(),'5511987654321',['API'], ' ')
        self.assertFalse(result[0]);self.assertIn('NOME_AUSENTE',result[1])
        self.whatsapp.assert_not_called()
        for valor in ({'tipo':'outro'},{'tipo':'primeiro_nome','extra':1},None):
            self.assertEqual(self.salvar(valores=[valor,'Texto']).status_code,400)

    def test_optout_buttons_without_variables_and_preserve_manual_rules(self):
        import optout
        self.preparar()
        self.template['components']=[{'type':'BODY','text':'Ola'},{'type':'BUTTONS','buttons':[
            {'type':'QUICK_REPLY','text':'Agora nao, obrigado!'},
            {'type':'QUICK_REPLY','text':'Consultar'},
            {'type':'URL','text':'Abrir site','url':'https://example.com'}]}]
        self.assertEqual(self.salvar(ativo=False,botoes_optout=['Agora nao, obrigado!']).status_code,200)
        self.assertEqual(fixos.listar(main.get_db,'888')[0]['botoes_optout'],['Agora nao, obrigado!'])
        event={'event':'conversation_created','first_message':' AGORA NAO, OBRIGADO! ',
            'chat_summary':{'account_id':'33393','chat_id':'321','details':{'contact':{'phone':'11987654321'}}}}
        with test_db() as c:
            c.execute('UPDATE optout_config SET habilitado=1,excluir_conversa_primeiro=1')
            self.assertFalse(optout.receber(c,{**event,'first_message':'Consultar'},1))
            self.assertTrue(optout.receber(c,event,2))
            self.assertEqual(c.execute('SELECT passo FROM optout_tarefas').fetchone()[0],'excluir_conversa')
        self.assertTrue(optout.bloqueado(main.get_db,'11987654321'))
        for bs in (['Abrir site'],['Inexistente'],[None],'texto'):
            self.assertEqual(self.salvar(ativo=False,botoes_optout=bs).status_code,400)
        # Alterar somente variaveis nao apaga marcacoes anteriores.
        self.salvar(ativo=False)
        self.assertEqual(fixos.listar(main.get_db,'888')[0]['botoes_optout'],['Agora nao, obrigado!'])
        self.assertEqual(self.salvar(ativo=False,botoes_optout=[]).status_code,200)
        with test_db() as c:
            self.assertFalse(optout.receber(c,{**event,'chat_summary':{**event['chat_summary'],'chat_id':'322'}},3))
            self.assertTrue(optout.receber(c,{**event,'first_message':'nao quero mensagens'},4))
        self.assertTrue(optout.bloqueado(main.get_db,'11987654321'))

    def test_button_validation_failure_is_atomic(self):
        self.preparar();self.salvar()
        self.assertEqual(self.salvar(valores=['novo','novo'],botoes_optout=['inventado']).status_code,400)
        self.assertEqual(fixos.resolver(main.get_db,self.numero(),[]),['Texto fixo','Outra frase'])
