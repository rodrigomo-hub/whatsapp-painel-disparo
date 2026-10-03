import sqlite3,tempfile,unittest,uuid
from pathlib import Path
from datetime import datetime
from fastapi import HTTPException
import comissoes,resultados,custos_meta
class ComissoesTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'db'
  with self.db() as c:resultados.migrar(c);comissoes.semear(c)
 def db(self):
  c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row;self.addCleanup(c.close);return c
 def body(self,**kw):return dict(acao='salvar',operacao_id=str(uuid.uuid4()),revisao=0,dia='2026-10-03',identificacao='Proposta teste',tabela='69639',prazo=48,valor='5.067,26',**kw)
 def test_regras_calculo_e_vigencia(self):
  with self.db() as c:r=comissoes.carregar_regras(c)
  self.assertEqual(len(r),17)
  d={'bank':'facta','product':'clt','table_name':'69639 - NOVO SMART','number_of_payments':48}
  self.assertEqual(comissoes.estimar(r,d,'2026-10-03',506726)['centavos'],26350)
  self.assertIsNone(comissoes.estimar(r,d,'2026-09-13',506726)['centavos'])
  self.assertIsNone(comissoes.estimar(r,dict(d,number_of_payments=60),'2026-10-03',506726)['centavos'])
 def test_criar_retry_editar_excluir(self):
  b=self.body();resp=comissoes.operar(self.db,b);self.assertEqual(resp,comissoes.operar(self.db,b));ident=resp['ids'][0]
  b.update(id=ident,revisao=1,valor='100',operacao_id=str(uuid.uuid4()));comissoes.operar(self.db,b)
  with self.db() as c:self.assertEqual(c.execute('SELECT comissao_centavos FROM propostas_equipe').fetchone()[0],520)
  with self.assertRaises(HTTPException):comissoes.operar(self.db,dict(b,operacao_id=str(uuid.uuid4())))
  comissoes.operar(self.db,{'acao':'excluir','id':ident,'revisao':2,'operacao_id':str(uuid.uuid4())})
  with self.db() as c:self.assertEqual(c.execute('SELECT excluido FROM propostas_equipe').fetchone()[0],1)
 def test_converter_atomico_sem_duplicar(self):
  resultados.salvar_manual(self.db,'2026-10-03',{'valor':'1000','revisao':0})
  body={'acao':'converter','dia':'2026-10-03','revisao':1,'operacao_id':str(uuid.uuid4()),'propostas':[dict(self.body(),valor='600'),dict(self.body(),valor='400')]}
  bad=dict(body,propostas=[dict(self.body(),valor='999')])
  with self.assertRaises(HTTPException):comissoes.operar(self.db,bad)
  self.assertEqual(resultados.manual_dia(self.db,'2026-10-03')['valor_centavos'],100000)
  comissoes.operar(self.db,body);comissoes.operar(self.db,body)
  self.assertEqual(resultados.manual_dia(self.db,'2026-10-03')['valor_centavos'],0)
  with self.db() as c:self.assertEqual(c.execute('SELECT sum(valor_centavos),count(*) FROM propostas_equipe').fetchone()[:],(100000,2))
 def test_custo_apenas_oficiais_snapshot_e_pendente(self):
  with self.db() as c:
   c.executescript("CREATE TABLE numeros(id INTEGER PRIMARY KEY,tipo_envio TEXT,template_categoria TEXT); CREATE TABLE disparos(id INTEGER PRIMARY KEY,numero_id INTEGER,telefone_destino TEXT,criado_em TEXT,status TEXT,canal TEXT,tipo_envio TEXT); INSERT INTO numeros VALUES(1,'meta','UTILITY'),(2,'vendeai_normal','UNKNOWN');")
   custos_meta.migrar(c)
   c.execute("INSERT INTO disparos(id,numero_id,telefone_destino,criado_em,status,canal,tipo_envio) VALUES(1,1,'5511999991234','2026-10-03T12:00:00-03:00','sucesso','whatsapp','meta'),(2,2,'5511999991234','2026-10-03T12:00:00-03:00','sucesso','whatsapp','vendeai_normal')")
   start=datetime.fromisoformat('2026-10-03T00:00:00-03:00');end=datetime.fromisoformat('2026-10-04T00:00:00-03:00')
   self.assertIsNone(custos_meta.resumo(c,start,end)['valor'])
   custos_meta.salvar(c,{'moeda':'BRL','UTILITY':'0,01','MARKETING':'0,20','AUTHENTICATION':'0,02'},'2026-10-03')
   c.execute("UPDATE numeros SET template_categoria='MARKETING' WHERE id=1")
   calc=custos_meta.resumo(c,start,end);self.assertEqual(calc['valor'],.01);self.assertEqual(calc['calculados'],1)

