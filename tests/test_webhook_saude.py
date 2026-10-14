import unittest,sqlite3,tempfile,time
from unittest.mock import patch,Mock
from contextlib import closing
import webhook_saude as w
class WebhookTests(unittest.TestCase):
 def test_precedencia(self):
  d={'expected_webhook_url':'https://crm/numero','webhook_configuration':{'phone_number':'https://crm/numero','application':'https://outro'}}
  self.assertEqual(w.diagnosticar(d)[0],'OK')
  del d['webhook_configuration']['phone_number']
  self.assertEqual(w.diagnosticar(d)[0],'MISMATCH')
  d['webhook_configuration']['whatsapp_business_account']='https://crm/numero'
  self.assertEqual(w.diagnosticar(d)[0],'OK')
 def test_indisponivel_ausente(self):
  self.assertEqual(w.diagnosticar({})[0],'UNKNOWN')
  self.assertEqual(w.diagnosticar({'expected_webhook_url':'x','webhook_configuration':{}})[0],'MISSING')
 def test_escopo_e_validade(self):
  n={'tipo_envio':'meta','vendeai_conta':'33393','vendeai_inbox_id':'1','webhook_estado':'OK','webhook_verificado_em':time.time()}
  self.assertTrue(w.permitido(n));n['webhook_verificado_em']=0;self.assertFalse(w.permitido(n))
  n['vendeai_conta']='32239';self.assertTrue(w.permitido(n))
 def test_registro_e_recuperacao(self):
  with tempfile.TemporaryDirectory() as d:
   def db():
    c=sqlite3.connect(d+'/test.db');c.row_factory=sqlite3.Row;return c
   with closing(db()) as c,c:
    c.execute('CREATE TABLE numeros(id INTEGER PRIMARY KEY,vendeai_conta TEXT,vendeai_inbox_id TEXT,tipo_envio TEXT)');w.migrar(c)
    c.execute("INSERT INTO numeros(id,vendeai_conta,vendeai_inbox_id,tipo_envio) VALUES(1,'33393','1','meta')")
   def numero():
    with closing(db()) as c:return dict(c.execute('SELECT * FROM numeros').fetchone())
   cfg={'vendeai_account_id':'33393'}
   with patch.object(w.requests,'get',return_value=Mock(status_code=200,json=lambda:{'expected_webhook_url':'x','webhook_configuration':{'application':'y'}})):
    self.assertFalse(w.consultar(db,numero(),cfg,'secret'))
   self.assertFalse(w.permitido(numero()))
   with patch.object(w.requests,'get',return_value=Mock(status_code=200,json=lambda:{'expected_webhook_url':'x','webhook_configuration':{'phone_number':'x'}})):
    self.assertTrue(w.consultar(db,numero(),cfg,'secret'))
   self.assertTrue(w.permitido(numero()))
   with closing(db()) as c:self.assertEqual(c.execute('SELECT count(*) FROM webhook_verificacoes').fetchone()[0],2)
