import sqlite3
import unittest
import ranking_wabas

class RankingTests(unittest.TestCase):
    def test_sum_sort_and_unknown(self):
        ns=[{'numero_id':i,'numero':'Número','pagos':set(range(q)),'valor_pago':v} for i,q,v in [(1,2,100),(2,3,200),(3,4,900),(4,1,10)]]
        refs={1:{'waba_id':'A','nome':'Primeiro'},2:{'waba_id':'A','nome':'Removido'},3:{'waba_id':'B'}}
        d=ranking_wabas.calcular(ns,refs,{'A':'WABA A','B':'WABA B'})
        self.assertEqual([x['pagos'] for x in d['ranking']],[5,4])
        self.assertEqual(d['ranking'][0]['valor_pago'],300)
        self.assertEqual(d['pagos_atribuidos'],9)
        self.assertEqual(d['pagos_envio_sem_waba'],1)

    def test_delete_retains_identification(self):
        c=sqlite3.connect(':memory:')
        c.execute('CREATE TABLE numeros(id INTEGER PRIMARY KEY,nome TEXT,waba_id TEXT,phone_number_id TEXT,tipo_envio TEXT)')
        ranking_wabas.migrar(c)
        c.execute("INSERT INTO numeros VALUES(1,'A','123','456','meta')")
        c.execute("UPDATE numeros SET nome='Nome atualizado' WHERE id=1")
        c.execute('DELETE FROM numeros WHERE id=1')
        ranking_wabas.migrar(c)
        self.assertEqual(c.execute('SELECT nome,waba_id FROM remetentes_historicos').fetchone(),('Nome atualizado','123'))
        c.close()
