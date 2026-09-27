import unittest
from unittest.mock import Mock,patch
import main
from test_mailing import MailingTests

class SituacaoTests(unittest.TestCase):
    resposta=staticmethod(MailingTests.resposta)
    def setUp(self): MailingTests.setUp(self)
    def consultar(self,d,code=200):
        with patch.object(main.requests,'get',return_value=Mock(status_code=code,json=Mock(return_value=d))) as req:
            r=self.client.get('/api/numeros/1/situacao-meta')
            self.assertEqual(r.status_code,200)
            self.assertEqual(req.call_args.kwargs['params'],{'fields':'health_status'})
            self.whatsapp.assert_not_called()
            return r.json()
    def test_estados(self):
        for estado,rotulo in [('AVAILABLE','Disponível para envio'),('LIMITED','Envio limitado'),('BLOCKED','Envio bloqueado')]:
            self.assertEqual(self.consultar({'health_status':{'can_send_message':estado}})['situacao'],rotulo)
    def test_token_nao_e_banimento(self):
        r=self.consultar({'error':{'code':190,'message':'fake-token'}},400)
        self.assertEqual(r['situacao'],'Não foi possível consultar')
        self.assertNotIn('fake-token',str(r))
    def test_motivo_e_segredo(self):
        r=self.consultar({'health_status':{'can_send_message':'BLOCKED','entities':[{'entity_type':'WABA','can_send_message':'BLOCKED','errors':[{'error_code':123,'error_description':'Problema fake-token'}]}]}})
        self.assertEqual(r['entidades'][0]['erros'][0]['codigo'],123)
        self.assertNotIn('fake-token',str(r))
    def test_ausente(self):
        self.assertEqual(self.client.get('/api/numeros/999/situacao-meta').status_code,404)
    def test_sem_sessao(self):
        self.client.cookies.clear()
        r=self.client.get('/api/numeros/1/situacao-meta',follow_redirects=False)
        self.assertIn(r.status_code,(401,303,307,302))

    def test_avisos_chamadas_em_portugues(self):
        for codigo in (138024,138025):
            r=self.consultar({'health_status':{'can_send_message':'AVAILABLE','entities':[{'entity_type':'PHONE_NUMBER','can_send_message':'AVAILABLE','errors':[{'error_code':codigo,'error_description':'SIP is not enabled'}]}]}})
            e=r['entidades'][0]
            self.assertEqual(e['tipo'],'Número de telefone')
            self.assertIn('não ao envio de mensagens',e['erros'][0]['descricao'])
            self.assertNotIn('is not enabled',e['erros'][0]['descricao'])

    def test_motivos_especificos(self):
        for codigo,trecho in [(141006,'Problema no método de pagamento'),(141008,'inativa'),(141010,'Verificação da empresa'),(141014,'banida')]:
            for valor in (codigo,str(codigo)):
                r=self.consultar({'health_status':{'can_send_message':'BLOCKED','entities':[{'entity_type':'WABA','can_send_message':'BLOCKED','errors':[{'error_code':valor}]}]}})
                self.assertIn(trecho,r['entidades'][0]['erros'][0]['descricao'])
    def test_codigo_desconhecido_nao_inventa_diagnostico(self):
        r=self.consultar({'health_status':{'can_send_message':'BLOCKED','entities':[{'entity_type':'WABA','errors':[{'error_code':999999,'error_description':'Unknown reason'}]}]}})
        e=r['entidades'][0]['erros'][0]
        self.assertIn('sem tradução',e['descricao'])
        self.assertEqual(e['descricao_original'],'Unknown reason')
