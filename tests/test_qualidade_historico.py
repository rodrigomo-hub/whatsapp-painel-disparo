import sqlite3
import unittest
import qualidade_historico as h

class HistoricoTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.addCleanup(self.c.close)
        self.c.execute("CREATE TABLE numeros(id INTEGER PRIMARY KEY, qualidade TEXT,tipo_envio TEXT)")
        self.c.execute("INSERT INTO numeros VALUES(1,'GREEN','meta'),(2,'UNKNOWN','vendeai_normal')")
        h.migrar(self.c)
    def test_sem_historico_inventado_e_sem_duplicar(self):
        self.assertEqual(h.recentes(self.c,1),[])
        self.c.execute("UPDATE numeros SET qualidade='GREEN' WHERE id=1")
        self.assertEqual(h.recentes(self.c,1),[])
        self.c.execute("UPDATE numeros SET qualidade='RED' WHERE id=1")
        h.migrar(self.c)
        self.c.execute("UPDATE numeros SET qualidade='GREEN' WHERE id=1")
        rows=h.recentes(self.c,1)
        self.assertEqual(len(rows),2)
        self.assertEqual((rows[0]['anterior'],rows[0]['atual']),('RED','GREEN'))
        self.assertTrue(rows[0]['detectado_em'].endswith('Z'))
    def test_normal_ignorado_e_limite(self):
        self.c.execute("UPDATE numeros SET qualidade='GREEN' WHERE id=2")
        self.assertEqual(h.recentes(self.c,2),[])
        for v in ['RED','YELLOW','GREEN','UNKNOWN','RED','GREEN']:
            self.c.execute('UPDATE numeros SET qualidade=? WHERE id=1',(v,))
        self.assertEqual(len(h.recentes(self.c,1)),5)
        self.assertEqual(self.c.execute('SELECT count(*) FROM qualidade_historico').fetchone()[0],6)
