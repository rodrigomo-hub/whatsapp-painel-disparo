import base64,unittest
from unittest.mock import patch
import requests,main,conexoes_normais as normal
import test_conexoes_normais as fixtures
from test_mailing import test_db


class ImagensNormaisTests(unittest.TestCase):
    setUp=fixtures.ConexoesTests.setUp
    response=fixtures.ConexoesTests.response
    resposta=staticmethod(fixtures.ConexoesTests.resposta)
    numero=fixtures.ConexoesTests.numero
    importar=fixtures.ConexoesTests.importar
    enviar=fixtures.ConexoesTests.enviar

    def anexar(self):
        normal.salvar_modelo(main.get_db,{'nome':'Modelo','texto':'Oi, {primeiro_nome}!','ativo':True,
            'imagem_base64':'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nimage-test').decode()},self.modelo)

    def preparar(self,existente=True):
        self.importar();self.anexar()
        def get(url,**kwargs):
            if url.endswith('/inboxes/77'):return self.response(self.item)
            if url.endswith('/contacts/search'):return self.response({'payload':[{'id':9,'phone_number':'+5511987654321'}] if existente else []})
            if url.endswith('/contacts/9/conversations'):return self.response({'payload':[{'id':10,'inbox_id':77}] if existente else []})
            raise AssertionError(url)
        def post(url,**kwargs):
            if url.endswith('/contacts'):return self.response({'payload':{'contact':{'id':9,'contact_inboxes':[{'inbox':{'id':77},'source_id':'5511987654321'}]}}})
            if url.endswith('/conversations'):return self.response({'id':10})
            if url.endswith('/messages'):return self.response({'id':123,'status':'sent','attachments':[{'id':5}]})
            raise AssertionError(url)
        self.get.side_effect=get;self.api.side_effect=post;self.api.reset_mock()

    def test_image_persists_preview_is_private_and_can_be_removed(self):
        self.anexar()
        self.assertEqual(self.client.get('/api/modelos-normais/'+str(self.modelo)+'/imagem').status_code,200)
        normal.salvar_modelo(main.get_db,{'nome':'Renomeado','texto':'Texto','ativo':True},self.modelo)
        self.assertEqual(self.client.get('/api/modelos-normais/'+str(self.modelo)+'/imagem').status_code,200)
        self.client.cookies.clear()
        self.assertNotEqual(self.client.get('/api/modelos-normais/'+str(self.modelo)+'/imagem',follow_redirects=False).status_code,200)
        normal.salvar_modelo(main.get_db,{'nome':'Renomeado','texto':'Texto','ativo':True,'remover_imagem':True},self.modelo)
        with test_db() as c:self.assertEqual(c.execute('SELECT imagem FROM modelos_normais WHERE id=?',(self.modelo,)).fetchone()[0],'')

    def test_image_and_caption_one_post_existing_contact(self):
        self.preparar()
        self.assertEqual(self.enviar(),(True,'','123'))
        self.assertEqual(self.api.call_count,1)
        k=self.api.call_args.kwargs
        self.assertEqual(k['data']['content'],'Oi, Maria!')
        self.assertEqual(k['data']['message_type'],'outgoing')
        self.assertEqual(k['files']['attachments[]'][2],'image/png')
        with test_db() as c:self.assertEqual(c.execute('SELECT message_id FROM etiquetas_disparo').fetchone()[0],'123')
        with test_db() as c:self.assertEqual(c.execute('SELECT etiqueta FROM etiquetas_disparo').fetchone()[0],'disparo_normal')

    def test_new_contact_and_conversation_created_without_text_only_message(self):
        self.preparar(False);self.assertTrue(self.enviar()[0])
        self.assertEqual(sum(x.args[0].endswith('/messages') for x in self.api.call_args_list),1)
        self.assertFalse(any(x.args[0].endswith('/send/') for x in self.api.call_args_list))

    def test_timeout_never_retries_or_sends_plain_text(self):
        self.preparar();self.api.side_effect=requests.Timeout()
        ok,error,_=self.enviar();self.assertFalse(ok);self.assertTrue(error.startswith('ENVIO_INCERTO:'));self.assertEqual(self.api.call_count,1)

    def test_existing_contact_gets_missing_inbox_link(self):
        self.preparar();original_get=self.get.side_effect;original_post=self.api.side_effect
        def get(url,**kw):
            if url.endswith('/contacts/9/conversations'):return self.response({'payload':[]})
            return original_get(url,**kw)
        def post(url,**kw):
            if url.endswith('/contact_inboxes'):
                self.assertEqual(kw['json'],{'inbox_id':77,'source_id':'5511987654321'})
                return self.response({'source_id':'5511987654321','inbox':{'id':77}})
            return original_post(url,**kw)
        self.get.side_effect=get;self.api.side_effect=post
        self.assertTrue(self.enviar()[0])

    def test_long_caption_prevents_all_crm_writes(self):
        self.preparar()
        with test_db() as c:c.execute('UPDATE modelos_normais SET texto=?',('x'*1025,))
        self.assertFalse(self.enviar()[0]);self.api.assert_not_called()

    def test_optout_after_crm_lookup_prevents_message(self):
        self.preparar();checks=iter([False,True])
        self.assertFalse(self.enviar(suprimido=lambda _:next(checks))[0]);self.api.assert_not_called()

    def test_invalid_image_does_not_replace_saved_model(self):
        self.anexar()
        with self.assertRaises(main.HTTPException):normal.salvar_modelo(main.get_db,{'nome':'Oops','texto':'Outro','imagem_base64':'invalid'},self.modelo)
        with test_db() as c:self.assertEqual(c.execute('SELECT nome FROM modelos_normais WHERE id=?',(self.modelo,)).fetchone()[0],'Modelo')

    def test_paused_during_crm_preparation_prevents_message(self):
        self.preparar();original=self.get.side_effect
        def get(url,**kwargs):
            r=original(url,**kwargs)
            if url.endswith('/contacts/9/conversations'):
                with test_db() as c:c.execute("UPDATE numeros SET ativo=0 WHERE tipo_envio='vendeai_normal'")
            return r
        self.get.side_effect=get
        self.assertFalse(self.enviar()[0]);self.api.assert_not_called()
