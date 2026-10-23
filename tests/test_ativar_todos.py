import unittest
import main
import test_mailing
from test_mailing import test_db
class AtivarTodosTests(unittest.TestCase):
 resposta=staticmethod(test_mailing.MailingTests.resposta)
 def setUp(self):
  test_mailing.MailingTests.setUp(self)
  with test_db() as c:c.execute("UPDATE numeros SET ativo=0,template_status='APPROVED',limite_diario=100,limite_hora=10")
 def test_ativa_preserva_limites(self):
  self.assertEqual(main.ativar_todos_numeros()['ativados'],1)
  self.assertEqual(main.ativar_todos_numeros()['ja_ativos'],1)
  with test_db() as c:self.assertEqual(tuple(c.execute('SELECT ativo,limite_diario,limite_hora FROM numeros').fetchone()),(1,100,10))
  self.assertEqual(main.pausar_todos_numeros()['pausados'],1)
 def test_bloqueados(self):
  for coluna,valor in [('status_envio_meta','BLOCKED'),('qualidade','RED'),('qualidade','UNKNOWN'),('template_status','PAUSED'),('template_status','UNKNOWN'),('template_header','IMAGE'),('limite_hora',0)]:
   with test_db() as c:
    c.execute("UPDATE numeros SET status_envio_meta='AVAILABLE',qualidade='GREEN',template_status='APPROVED',template_header='',limite_hora=10")
    c.execute('UPDATE numeros SET '+coluna+'=?',(valor,))
   r=main.ativar_todos_numeros();self.assertEqual(r['ativados'],0);self.assertEqual(len(r['bloqueados']),1)
 def test_normal_desconectado(self):
  with test_db() as c:c.execute("UPDATE numeros SET tipo_envio='vendeai_normal',conexao_status='disconnected'")
  self.assertEqual(main.ativar_todos_numeros()['ativados'],0)
 def test_sem_sessao(self):
  self.client.cookies.clear();r=self.client.post('/api/numeros/ativar-todos',follow_redirects=False);self.assertNotEqual(r.status_code,200)
