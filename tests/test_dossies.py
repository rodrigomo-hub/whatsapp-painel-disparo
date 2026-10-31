import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import dossies
from fastapi import HTTPException

class Connection(sqlite3.Connection):
 def __exit__(self,*args):
  try:return super().__exit__(*args)
  finally:self.close()

class DossiesTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'db'
  with self.db() as c:
   dossies.migrar(c)
   c.execute('CREATE TABLE vendeai_eventos(account_id TEXT,chat_id TEXT,conta_confirmada INTEGER,payload_raw BLOB,id INTEGER)')
 def db(self):
  c=sqlite3.connect(self.path,factory=Connection);c.row_factory=sqlite3.Row;return c
 def test_reference_account_and_host(self):
  self.assertEqual(dossies.referencia('https://app.vendeaitecnologia.com.br/chats/33393/conversations/123','33393'),'123')
  for s in ('https://evil.test/chats/33393/conversations/123','https://app.vendeaitecnologia.com.br/chats/5/conversations/123','../1'):
   with self.assertRaises(HTTPException):dossies.referencia(s,'33393')
 def test_authorization(self):
  with self.db() as c:
   c.execute('INSERT INTO dossies_permissoes VALUES(?,?,?)',('seller','33393','123'))
   self.assertTrue(dossies.permitido(c,'seller','33393','123'))
   self.assertFalse(dossies.permitido(c,'seller','33393','124'))
   self.assertFalse(dossies.permitido(c,'seller','1','123'))
 def test_payment_any_bank_optin_idempotent(self):
  event={'chat_summary':{'account_id':'33393','chat_id':'123'},'proposal':{'proposal_id':'a','bank':'sempre_facil','status':'LIQUIDATED_TO_CUSTOMER'}}
  with self.db() as c:
   dossies.pagamento(c,event);self.assertEqual(c.execute('SELECT count(*) FROM dossies_jobs').fetchone()[0],0)
   c.execute('UPDATE dossies_config SET automatico=1');dossies.pagamento(c,event);dossies.pagamento(c,event)
   self.assertEqual(c.execute('SELECT count(*) FROM dossies_jobs').fetchone()[0],1)
 def test_all_pages_and_no_progress(self):
  def get(base,token,path,raw):
   if path=='/conversations/123':return {'id':123}
   if 'before=1' in path:return {'payload':[]}
   if 'before=20' in path:return {'payload':[{'id':1,'content':'first'}]}
   return {'payload':[{'id':20,'content':'last'}]}
  with patch.object(dossies,'ler_api',side_effect=get),patch.object(dossies,'pdf',return_value=b'pdf'):
   self.assertEqual(dossies.coletar({'vendeai_account_id':'33393'},'secret','123')[2],2)
  with patch.object(dossies,'ler_api',return_value={'payload':[{'id':20}]}):
   with self.assertRaisesRegex(RuntimeError,'avanço'):dossies.coletar({'vendeai_account_id':'33393'},'secret','123')
 def test_media_untrusted_host(self):
  with patch.object(dossies.requests,'get') as get:
   with self.assertRaises(RuntimeError):dossies.baixar('http://127.0.0.1/file')
   get.assert_not_called()
 def test_version_and_local_copy_only(self):
  storage=dossies.LocalStorage(Path(self.tmp.name)/'files')
  with patch.object(dossies,'coletar',return_value=({'raw/test.json':b'{}'},[],2,0,'same',{})):
   for _ in range(2):
    with self.db() as c:dossies.agendar(c,'33393','123','admin')
    self.assertTrue(dossies.processar(self.db,{'vendeai_account_id':'33393'},'secret',storage))
  with self.db() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM dossies_versoes').fetchone()[0],1)
   self.assertEqual(c.execute('SELECT provedor FROM dossies_copias').fetchone()[0],'local')
  with patch.object(dossies,'coletar',return_value=({'raw/test.json':b'{"new":1}'},[],3,0,'changed',{})):
   with self.db() as c:dossies.agendar(c,'33393','123','admin')
   dossies.processar(self.db,{'vendeai_account_id':'33393'},'secret',storage)
  with self.db() as c:self.assertEqual(c.execute('SELECT max(versao) FROM dossies_versoes').fetchone()[0],2)
 def test_media_failure_not_complete(self):
  with self.db() as c:dossies.agendar(c,'33393','123','admin')
  with patch.object(dossies,'coletar',return_value=({'raw/test.json':b'{}'},[{'erro':'indisponivel'}],1,0,'partial',{})):
   dossies.processar(self.db,{'vendeai_account_id':'33393'},'secret',dossies.LocalStorage(Path(self.tmp.name)/'files'))
  with self.db() as c:self.assertEqual(c.execute('SELECT estado FROM dossies_jobs').fetchone()[0],'com_pendencias')
 def test_storage_cannot_overwrite(self):
  s=dossies.LocalStorage(Path(self.tmp.name)/'files');key='a'*32+'.zip';s.put(key,b'a')
  with self.assertRaises(FileExistsError):s.put(key,b'b')
  with self.assertRaises(ValueError):s.path('../secret')
