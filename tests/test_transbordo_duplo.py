import unittest
from unittest.mock import Mock,patch
import requests
import main,transbordo
import test_transbordo
from test_mailing import test_db
class DuploTests(unittest.TestCase):
 resposta=staticmethod(test_transbordo.TransbordoTests.resposta)
 def setUp(self):
  test_transbordo.TransbordoTests.setUp(self)
  with test_db() as c:c.execute("UPDATE configuracao SET transbordo2_habilitado=1,transbordo2_nome='Milone',transbordo2_url='https://milone.example',transbordo2_token='secret2'")
 def aceita(self,url,**kw):
  if url.endswith('/disparar'):return Mock(status_code=200,json=Mock(return_value={'status':'sucesso'}))
  return test_transbordo.TransbordoTests.aceita(self,url,**kw)
 def enviar(self):
  cfg=main.carregar_configuracao();lead=main.vendeai_mailing.preparar_lead(self.lead,cfg)['mailing']
  return transbordo.encaminhar_todos(main.get_db,self.lead,'teste-duplo',cfg,lead)
 def test_dois_destinos_sem_repetir(self):
  with patch('transbordo.requests.post',side_effect=self.aceita) as post:
   r=self.enviar();self.assertTrue(r['aceito']);self.assertEqual(len(r['destinos']),2);self.assertEqual(post.call_count,2)
   self.enviar();self.assertEqual(post.call_count,2)
   call=next(x for x in post.call_args_list if x.args[0].endswith('/disparar'))
   self.assertEqual(call.kwargs['headers']['X-Transbordo-Hop'],'1');self.assertIn('whatsapp',call.kwargs['json'])
 def test_timeout_nao_impede_outro(self):
  def enviar(url,**kw):
   if url.endswith('/disparar'):raise requests.Timeout()
   return self.aceita(url,**kw)
  with patch('transbordo.requests.post',side_effect=enviar) as post:
   r=self.enviar();self.assertEqual(r['status'],'parcial');self.assertIsNone(r['destinos'][1]['aceito']);self.enviar();self.assertEqual(post.call_count,2)
 def test_optout(self):
  with patch('transbordo.optout.bloqueado',return_value=True),patch('transbordo.requests.post') as post:
   self.assertFalse(self.enviar()['aceito']);post.assert_not_called()
 def test_segredo_nao_retornado(self):
  d=self.client.get('/api/configuracao').json();self.assertNotIn('transbordo2_token',d);self.assertTrue(d['transbordo2_token_configurado'])

 def test_entrada_sem_capacidade_envia_a_ambos(self):
  with test_db() as c:c.execute('UPDATE numeros SET ativo=0')
  with patch('transbordo.requests.post',side_effect=self.aceita) as post:
   r=self.client.post('/disparar',json=self.lead);self.assertEqual(r.status_code,200);self.assertEqual(len(r.json()['destinos']),2)
   self.client.post('/disparar',json=self.lead);self.assertEqual(post.call_count,2)
 def test_disponibilidade_independente(self):
  with patch.object(main,'testar_destino_unico',return_value={'conectado':True,'disponivel':False}),patch('main.requests.get',return_value=Mock(status_code=200,json=Mock(return_value={'pode_disparar':True}))):
   self.assertTrue(main.testar_destino_transbordo(main.carregar_configuracao())['disponivel'])
 def test_todos_explicito_nao_envia_local_e_nao_duplica(self):
  body={**self.lead,'parceiro_destino':' TODOS '}
  with patch('transbordo.requests.post',side_effect=self.aceita) as post:
   a=self.client.post('/disparar',json=body);self.assertEqual(a.status_code,200)
   self.assertEqual(len(a.json()['destinos']),2)
   self.client.post('/disparar',json=body);self.assertEqual(post.call_count,2)
   self.assertFalse(self.fila.resumo()['contagens']);self.whatsapp.assert_not_called()
 def test_milone_explicito_so_segundo(self):
  with patch('transbordo.requests.post',side_effect=self.aceita) as post:
   a=self.client.post('/disparar',json={**self.lead,'parceiro_destino':'Milone'})
   self.assertEqual(a.status_code,200);self.assertEqual(len(a.json()['destinos']),1)
   post.assert_called_once();self.assertEqual(post.call_args.args[0],'https://milone.example/disparar')
   self.assertNotIn('parceiro_destino',post.call_args.kwargs['json']);self.whatsapp.assert_not_called()
 def test_todos_loop_bloqueado(self):
  with patch('transbordo.requests.post') as post:
   r=self.client.post('/disparar',json={**self.lead,'parceiro_destino':'todos'},headers={'X-Transbordo-Hop':'1'})
   self.assertEqual(r.status_code,400);post.assert_not_called()
 def test_todos_recusa_um_nao_envia_local(self):
  def envio(url,**kw):
   return Mock(status_code=409) if url.endswith('/disparar') else self.aceita(url,**kw)
  with patch('transbordo.requests.post',side_effect=envio) as post:
   r=self.client.post('/disparar',json={**self.lead,'parceiro_destino':'todos'})
   self.assertEqual(r.json()['status'],'parcial');self.assertEqual(post.call_count,2);self.whatsapp.assert_not_called()
