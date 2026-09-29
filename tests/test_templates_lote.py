import base64
import unittest
from unittest.mock import Mock,patch
import requests
import main
import templates_lote as tl
import test_mailing
from test_mailing import test_db

class TemplatesLoteTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        with test_db() as c:
            for w in ['123','456']:c.execute('INSERT INTO wabas_salvas(waba_id,access_token,nome) VALUES(?,?,?)',(w,'secret','Conta '+w))
        self.body={'name':'teste','body':'Olá {{1}}','examples':['Maria'],'wabas':['123','456']}
    def lote(self):return tl.criar_lote(main.get_db,self.body)['id']
    def item(self,id,w='123'):return next(i for i in tl.resumo(main.get_db,id)['itens'] if i['waba_id']==w)
    def executar(self,id,lookup=None,consultar=False):return tl.executar(main.get_db,lookup or Mock(return_value={'dados':[]}),id,'123',consultar)
    def test_cria_e_nao_repete(self):
        id=self.lote();self.api.return_value=Mock(status_code=200,json=Mock(return_value={'id':'t1','status':'PENDING'}))
        self.executar(id);self.executar(id)
        self.api.assert_called_once();self.assertEqual(self.item(id)['meta_status'],'PENDING')
        self.assertEqual(self.api.call_args.kwargs['json']['components'][0]['example'],{'body_text':[['Maria']]})
        self.assertEqual(self.item(id,'456')['estado'],'AGUARDANDO')
    def test_ja_existe_preserva(self):
        id=self.lote();self.executar(id,Mock(return_value={'dados':[{'id':'t','name':'teste','language':'pt_BR','status':'APPROVED'}]}))
        self.api.assert_not_called();self.assertEqual(self.item(id)['estado'],'EXISTENTE')
    def test_timeout_nao_reenvia(self):
        id=self.lote();self.api.side_effect=requests.Timeout('secret')
        self.executar(id);self.executar(id)
        self.assertEqual(self.item(id)['estado'],'INCERTO');self.api.assert_called_once()
        self.assertNotIn('secret',self.item(id)['detalhe'])
    def test_consulta_recupera_incerto_sem_post(self):
        id=self.lote();tl.gravar(main.get_db,id,'123','INCERTO')
        self.executar(id,Mock(return_value={'dados':[{'id':'t','name':'teste','language':'pt_BR','status':'PENDING'}]}),True)
        self.assertEqual(self.item(id)['estado'],'EXISTENTE');self.api.assert_not_called()
    def test_imagem_upload_por_token(self):
        self.body['image']='data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nexample').decode()
        id=self.lote();self.api.side_effect=[Mock(status_code=200,json=Mock(return_value={'id':'upload:abc?sig=abc'})),Mock(status_code=200,json=Mock(return_value={'h':'handle-example'})),Mock(status_code=200,json=Mock(return_value={'id':'t','status':'PENDING'}))]
        self.executar(id);self.assertEqual(self.api.call_count,3)
        self.assertEqual(self.api.call_args.kwargs['json']['components'][0]['example'],{'header_handle':['handle-example']})
        self.assertEqual(self.api.call_args_list[1].kwargs['headers']['file_offset'],'0')
    def test_rejeicao_permite_tentar_falha(self):
        id=self.lote();self.api.return_value=Mock(status_code=400,json=Mock(return_value={'error':{'code':100,'message':'Invalid secret'}}))
        self.executar(id);self.assertEqual(self.item(id)['estado'],'ERRO');self.assertNotIn('secret',self.item(id)['detalhe'])
    def test_nao_cria_sem_conferir_existente(self):
        id=self.lote();self.executar(id,Mock(return_value={'erro':'offline'}))
        self.api.assert_not_called();self.assertEqual(self.item(id)['estado'],'ERRO')
    def test_outro_lote_nao_duplica_incerto(self):
        a=self.lote();tl.gravar(main.get_db,a,'123','INCERTO');b=self.lote()
        with self.assertRaises(main.HTTPException):self.executar(b)
        self.api.assert_not_called()
    def test_validacao_variaveis(self):
        with self.assertRaises(main.HTTPException):tl.validar({**self.body,'body':'Olá {{2}}'})
        with self.assertRaises(main.HTTPException):tl.validar({**self.body,'image':'https://file'})
        with self.assertRaises(main.HTTPException):tl.validar({**self.body,'examples':[]})
    def test_pagina_e_autenticacao(self):
        self.assertEqual(self.client.get('/wabas/criar-templates').status_code,200)
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/templates-lotes',json=self.body,follow_redirects=False).status_code,401)
