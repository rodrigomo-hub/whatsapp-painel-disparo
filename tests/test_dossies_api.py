import unittest
from unittest.mock import patch
import main,dossies
from test_mailing import MailingTests,test_db

class AccessTests(unittest.TestCase):
 resposta=staticmethod(MailingTests.resposta)
 def setUp(self):MailingTests.setUp(self)
 def test_anonymous_not_allowed(self):
  self.client.cookies.clear()
  self.assertEqual(self.client.get('/dossies').status_code,200)
  self.assertEqual(self.client.get('/dossies/api/lista').status_code,401)
  self.assertEqual(self.client.post('/dossies/api/gerar',json={'conversa':'123'},headers={'X-Dossie-Action':'1'}).status_code,401)
 def test_restricted_user_cannot_admin_or_other_chat(self):
  headers={'X-Dossie-Action':'1'}
  r=self.client.post('/dossies/api/usuarios',json={'nome':'vendedora','senha':'senha-longa-teste','conversas':['123']},headers=headers)
  self.assertEqual(r.status_code,200,r.text)
  self.client.cookies.clear()
  r=self.client.post('/dossies/api/login',json={'nome':'vendedora','senha':'senha-longa-teste'},headers=headers)
  self.assertEqual(r.status_code,200,r.text)
  self.client.cookies.set('dossie_session',r.cookies.get('dossie_session'))
  self.assertEqual(self.client.post('/dossies/api/config',json={'automatico':True},headers=headers).status_code,403)
  self.assertEqual(self.client.post('/dossies/api/gerar',json={'conversa':'124'},headers=headers).status_code,403)
  self.assertEqual(self.client.post('/dossies/api/gerar',json={'conversa':'123'},headers=headers).status_code,200)
  self.assertEqual(self.client.get('/api/configuracao').status_code,401)
  data=self.client.get('/dossies/api/lista').json();self.assertFalse(data['admin']);self.assertEqual(len(data['itens']),1)
 def test_restricted_user_can_be_created_before_any_conversation_is_authorized(self):
  headers={'X-Dossie-Action':'1'}
  r=self.client.post('/dossies/api/usuarios',json={'nome':'nova-vendedora','senha':'senha-longa-teste','conversas':[]},headers=headers)
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['autorizadas'],0)
  self.client.cookies.clear()
  r=self.client.post('/dossies/api/login',json={'nome':'nova-vendedora','senha':'senha-longa-teste'},headers=headers)
  self.assertEqual(r.status_code,200,r.text)
  self.client.cookies.set('dossie_session',r.cookies.get('dossie_session'))
  self.assertEqual(self.client.post('/dossies/api/gerar',json={'conversa':'123'},headers=headers).status_code,403)
 def test_csrf_and_foreign_account(self):
  self.assertEqual(self.client.post('/dossies/api/gerar',json={'conversa':'123'}).status_code,403)
  self.assertEqual(self.client.post('/dossies/api/gerar',json={'conversa':'https://app.vendeaitecnologia.com.br/chats/32239/conversations/123'},headers={'X-Dossie-Action':'1'}).status_code,400)
