import base64
import unittest
from unittest.mock import Mock,patch
import main
import imagens_template
import test_mailing
from test_mailing import test_db

PNG='data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nmock').decode()
class ImagemTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        self.whatsapp_patch.stop()
        with test_db() as c: c.execute("UPDATE numeros SET waba_id='waba',template_nome='foto',template_idioma='pt_BR'")
        self.meta=patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'data':[{'name':'foto','language':'pt_BR','components':[{'type':'HEADER','format':'IMAGE'},{'type':'BODY','text':'Olá!'}]}]})))
        self.meta.start();self.addCleanup(self.meta.stop)
        self.api.return_value=Mock(status_code=200,json=Mock(return_value={'id':'media-id'}))
    def salvar(self,**extras):
        return self.client.patch('/api/numeros/1',json={'template_nome':'foto','template_idioma':'pt_BR',**extras})
    def numero(self):
        with test_db() as c:return dict(c.execute('SELECT * FROM numeros WHERE id=1').fetchone())
    def test_exige_imagem_servidor(self):
        self.assertEqual(self.salvar().status_code,400)
        self.api.assert_not_called()
    def test_imagem_persistida_preview_e_envio_estatico(self):
        self.assertEqual(self.salvar(imagem_base64=PNG).status_code,200)
        n=self.numero();self.assertEqual(n['template_header'],'IMAGE')
        self.assertTrue(n['template_sem_variaveis'])
        self.assertEqual(self.client.get('/api/numeros/1/imagem-template').content,b'\x89PNG\r\n\x1a\nmock')
        self.api.reset_mock();self.api.return_value=Mock(status_code=200,json=Mock(return_value={'messages':[{'id':'wamid'}]}))
        self.assertTrue(main.enviar_whatsapp(n,'5511987654321',[''])[0])
        comp=self.api.call_args.kwargs['json']['template']['components']
        self.assertEqual(comp,[{'type':'header','parameters':[{'type':'image','image':{'id':'media-id'}}]}])
        self.assertEqual(self.api.call_count,1)
    def test_preserva_ao_editar_mesmo_template(self):
        self.salvar(imagem_base64=PNG);chave=self.numero()['template_imagem']
        self.api.reset_mock();self.assertEqual(self.salvar().status_code,200)
        self.assertEqual(self.numero()['template_imagem'],chave);self.api.assert_not_called()
    def test_sem_imagem_sem_capacidade(self):
        with test_db() as c:c.execute("UPDATE numeros SET template_header='IMAGE'")
        self.assertFalse(main.disponivel_agora(self.numero()))
        self.assertEqual(main.capacidade_lote()['capacidade_agora'],0)
        self.assertFalse(self.client.get('/api/disponibilidade-consulta').json()['consultar_nova_vida'])
        self.assertFalse(main.enviar_whatsapp(self.numero(),'5511987654321',[])[0]);self.api.assert_not_called()
    def test_arquivo_invalido_e_upload_falha(self):
        self.assertEqual(self.salvar(imagem_base64='data:image/png;base64,aGVsbG8=').status_code,400)
        self.api.assert_not_called()
        self.api.return_value=Mock(status_code=400,json=Mock(return_value={'error':{}}))
        self.assertEqual(self.salvar(imagem_base64=PNG).status_code,400)
        self.assertFalse(self.numero()['template_imagem'])
    def test_renova_midia_antes_de_envio(self):
        self.salvar(imagem_base64=PNG)
        with test_db() as c:c.execute('UPDATE imagens_template SET enviado_em=0')
        self.api.reset_mock();self.api.return_value=Mock(status_code=200,json=Mock(return_value={'id':'media-novo'}))
        self.assertEqual(imagens_template.media_id(main.get_db,self.numero()),'media-novo')
        self.api.assert_called_once()
    def test_header_e_body_juntos(self):
        self.salvar(imagem_base64=PNG);n=self.numero();n['template_sem_variaveis']=0
        self.api.return_value=Mock(status_code=200,json=Mock(return_value={'messages':[{'id':'wamid'}]}))
        main.enviar_whatsapp(n,'5511987654321',['Ana'])
        comp=self.api.call_args.kwargs['json']['template']['components']
        self.assertEqual([c['type'] for c in comp],['header','body'])
        self.assertEqual(comp[1]['parameters'][0]['text'],'Ana')
    def test_preview_privado(self):
        self.salvar(imagem_base64=PNG);self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/numeros/1/imagem-template').status_code,401)
