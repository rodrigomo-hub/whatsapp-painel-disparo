import unittest
from unittest.mock import Mock
import main
import test_mailing
from test_mailing import test_db


class TemplateTests(unittest.TestCase):
    resposta = staticmethod(test_mailing.MailingTests.resposta)

    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        self.whatsapp_patch.stop()
        self.api.return_value = Mock(status_code=200, json=Mock(return_value={'messages':[{'id':'wamid'}]}))

    def numero(self, estatico=False):
        with test_db() as c:
            c.execute('UPDATE numeros SET template_sem_variaveis=?', (int(estatico),))
            return dict(c.execute('SELECT * FROM numeros LIMIT 1').fetchone())

    def test_sem_variaveis_omite_componentes(self):
        self.assertTrue(main.enviar_whatsapp(self.numero(), '5511987654321', [])[0])
        self.assertNotIn('components', self.api.call_args.kwargs['json']['template'])

    def test_estatico_ignora_parametros_do_n8n(self):
        main.enviar_whatsapp(self.numero(True), '5511987654321', ['Nome','Texto',''])
        self.assertNotIn('components', self.api.call_args.kwargs['json']['template'])

    def test_dinamico_preserva_ordem_e_idioma(self):
        n = self.numero(); n['template_idioma'] = 'pt_PT'
        main.enviar_whatsapp(n, '5511987654321', ['Ana','Oferta','Detalhes'])
        t = self.api.call_args.kwargs['json']['template']
        self.assertEqual(t['language']['code'], 'pt_PT')
        self.assertEqual([p['text'] for p in t['components'][0]['parameters']], ['Ana','Oferta','Detalhes'])

    def test_dinamico_vazio_nao_chama_meta(self):
        self.assertFalse(main.enviar_whatsapp(self.numero(), '5511987654321', [''])[0])
        self.api.assert_not_called()

    def test_identifica_estatico_sem_confundir_midia_e_botoes(self):
        self.assertTrue(main.template_estatico({'components':[{'type':'BODY','text':'Olá!'}]}))
        for componentes in ([{'type':'BODY','text':'Olá {{1}}'}], [{'type':'HEADER','format':'IMAGE'}],
                            [{'type':'BUTTONS','buttons':[{'type':'URL','url':'https://example.com/{{1}}'}]}]):
            self.assertFalse(main.template_estatico({'components':componentes}))

    def test_salva_e_reseta_modo_quando_troca_template(self):
        r = self.client.patch('/api/numeros/1', json={'template_sem_variaveis':True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get('/api/numeros').json()[0]['template_sem_variaveis'], 1)
        self.client.patch('/api/numeros/1', json={'template_nome':'outro'})
        self.assertEqual(self.client.get('/api/numeros').json()[0]['template_sem_variaveis'], 0)
        self.assertEqual(self.client.patch('/api/numeros/1', json={'template_sem_variaveis':'false'}).status_code, 400)

    def test_endpoint_estatico_aceita_entrada_sem_lista_ou_com_vazios(self):
        self.numero(True)
        with test_db() as c:
            c.execute('UPDATE configuracao SET vendeai_habilitado=0')
        for variaveis in (None, [], ['','texto']):
            lead = {'telefone':'11987654321','nome':'Ana'}
            if variaveis is not None:
                lead['variaveis_template'] = variaveis
            r = self.client.post('/disparar', json=lead)
            self.assertEqual(r.json()['status'], 'sucesso')
            self.assertNotIn('components', self.api.call_args.kwargs['json']['template'])
