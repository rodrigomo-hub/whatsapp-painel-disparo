import unittest
from unittest.mock import patch,Mock
import main
import test_mailing
from test_mailing import test_db
class WabasPainelTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):test_mailing.MailingTests.setUp(self)
    def test_salva_sem_numero_e_nao_expoe_token(self):
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'id':'123','name':'Conta nova'}))):
            r=self.client.post('/api/wabas-painel',json={'waba_id':'123','access_token':'segredo'})
        self.assertEqual(r.status_code,200);self.assertNotIn('segredo',r.text)
        with test_db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM numeros').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT access_token FROM wabas_salvas WHERE waba_id='123'").fetchone()[0],'segredo')
        with patch.object(main,'consultar_meta_waba',side_effect=lambda w,t,e='',f='': {'dados': [] if e else {}}):
            d=self.client.get('/api/wabas-painel/123')
        self.assertEqual(d.status_code,200);self.assertNotIn('segredo',d.text)
    def test_token_invalido_nao_salva(self):
        with patch.object(main,'consultar_meta_waba',return_value={'erro':'erro'}):
            self.assertEqual(self.client.post('/api/wabas-painel',json={'waba_id':'123','access_token':'errado'}).status_code,400)
        with test_db() as c:self.assertIsNone(c.execute("SELECT * FROM wabas_salvas WHERE waba_id='123'").fetchone())
    def test_paginacao_e_erro(self):
        with patch.object(main.requests,'get',side_effect=[Mock(status_code=200,json=Mock(return_value={'data':[{'id':'1'}],'paging':{'next':'unused','cursors':{'after':'a'}}})),Mock(status_code=200,json=Mock(return_value={'data':[{'id':'2'}]}))]):
            self.assertEqual(len(main.consultar_meta_waba('123','secret','/message_templates')['dados']),2)
        with patch.object(main.requests,'get',return_value=Mock(status_code=403,json=Mock(return_value={'error':{}}))):
            self.assertIn('erro',main.consultar_meta_waba('123','secret','/message_templates'))
    def test_requer_sessao(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/wabas',follow_redirects=False).status_code,307)
