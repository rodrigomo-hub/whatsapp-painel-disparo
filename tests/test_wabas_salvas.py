import unittest
from unittest.mock import Mock,patch
import main
import test_mailing
from test_mailing import test_db

class WabaSalvaTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self)
        with test_db() as c:c.execute("INSERT INTO wabas_salvas(waba_id,access_token,nome,nome_consultado_em) VALUES('waba-1','segredo-salvo','Minha conta',9999999999)")
    def test_lista_nao_expoe_token(self):
        r=self.client.get('/api/wabas-salvas')
        self.assertEqual(r.json()['wabas'],['waba-1'])
        self.assertEqual(r.json()['detalhes'],[{'id':'waba-1','nome':'Minha conta'}])
        self.assertNotIn('segredo',r.text)
    def test_descoberta_usa_token_salvo(self):
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'data':[]}))) as get:
            for rota in ('/api/buscar-numeros','/api/buscar-templates'):
                self.assertEqual(self.client.post(rota,json={'waba_id':'waba-1'}).status_code,200)
                self.assertEqual(get.call_args.kwargs['headers']['Authorization'],'Bearer segredo-salvo')
    def test_cadastro_reusa_e_impede_duplicacao(self):
        template={'name':'oferta','language':'pt_BR','components':[{'type':'BODY','text':'Oi'}]}
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'data':[template]}))):
            body={'nome':'Segundo','phone_number_id':'novo-id','waba_id':'waba-1','template_nome':'oferta'}
            self.assertEqual(self.client.post('/api/numeros',json=body).status_code,200)
            self.assertEqual(self.client.post('/api/numeros',json=body).status_code,409)
        with test_db() as c:
            self.assertEqual(c.execute("SELECT access_token FROM numeros WHERE phone_number_id='novo-id'").fetchone()[0],'segredo-salvo')
    def test_waba_nova_exige_token(self):
        self.assertEqual(self.client.post('/api/numeros',json={'nome':'Novo','phone_number_id':'novo','waba_id':'outra'}).status_code,400)
    def test_lista_privada(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/wabas-salvas').status_code,401)
    def test_migracao_preserva_token_mais_recente(self):
        with test_db() as c:
            c.execute("UPDATE numeros SET waba_id='antiga',access_token='antigo'")
            c.execute("INSERT INTO numeros(nome,phone_number_id,waba_id,access_token) VALUES('Outro','outro','antiga','recente')")
        main.init_db()
        self.assertEqual(main.token_waba({'waba_id':'antiga'}),'recente')

    def test_nome_meta_e_cache(self):
        with test_db() as c:c.execute('UPDATE wabas_salvas SET nome_consultado_em=0')
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'id':'waba-1','name':'Nome Meta'}))) as get:
            self.assertEqual(self.client.get('/api/wabas-salvas').json()['detalhes'][0]['nome'],'Nome Meta')
            self.client.get('/api/wabas-salvas');get.assert_called_once()
    def test_falha_preserva_nome(self):
        with test_db() as c:c.execute('UPDATE wabas_salvas SET nome_consultado_em=0')
        with patch.object(main.requests,'get',return_value=Mock(status_code=403,json=Mock(return_value={'error':{}}))):
            self.assertEqual(self.client.get('/api/wabas-salvas').json()['detalhes'][0]['nome'],'Minha conta')
