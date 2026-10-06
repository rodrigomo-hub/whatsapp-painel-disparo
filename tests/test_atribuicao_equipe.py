import unittest
import sqlite3
import atribuicao_equipe as a
import comissoes
import ranking_wabas

class AtribuicaoTests(unittest.TestCase):
    def test_normalization_and_last_prior_send(self):
        rows=[{'id':1,'numero_id':1,'cpf':'1234567890','telefone_destino':'11999991234','criado_em':'2026-10-02T08:00:00-03:00'},
              {'id':2,'numero_id':2,'cpf':'01234567890','telefone_destino':'5511999991234','criado_em':'2026-10-02T10:00:00-03:00'}]
        ref=a.instante('2026-10-02T09:00:00-03:00')
        self.assertEqual(a.encontrar(rows,'012.345.678-90','',ref)['id'],1)
        self.assertIsNone(a.encontrar(rows,'11111111111','5511999991234',ref))
        self.assertIsNone(a.encontrar(rows,'01234567890','',a.instante('2026-10-12T09:00:00-03:00')))

    def test_manual_unknown_and_persisted_match(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        comissoes.migrar(c)
        c.executescript("CREATE TABLE wabas_salvas(waba_id TEXT);INSERT INTO wabas_salvas VALUES('A');CREATE TABLE numeros(id INTEGER,nome TEXT,waba_id TEXT,phone_number_id TEXT,tipo_envio TEXT);INSERT INTO numeros VALUES(1,'Numero','A','P','meta');CREATE TABLE disparos(id INTEGER,numero_id INTEGER,cpf TEXT,telefone_destino TEXT,criado_em TEXT,status TEXT,canal TEXT);INSERT INTO disparos VALUES(1,1,'01234567890','5511999991234','2026-10-02T08:00:00-03:00','sucesso','whatsapp');")
        ranking_wabas.migrar(c)
        c.execute("INSERT INTO propostas_equipe(id,dia,identificacao,tabela,prazo,valor_centavos,revisao,atualizado_em) VALUES('e','2026-10-02','Teste','69639',24,10000,1,'agora')")
        a.salvar(c,'e',{'dia':'2026-10-02','cpf':'01234567890','proposta_em':'2026-10-02T09:00'})
        row=dict(c.execute('SELECT * FROM propostas_equipe').fetchone())
        self.assertEqual((row['waba_id'],row['disparo_origem_id'],row['atribuicao_fonte']),('A',1,'disparo'))
        a.salvar(c,'e',{'dia':'2026-10-02','waba_manual':'nenhuma'})
        self.assertEqual(c.execute('SELECT waba_id FROM propostas_equipe').fetchone()[0],'')
        a.salvar(c,'e',{'dia':'2026-10-02','waba_manual':'A'})
        self.assertEqual(c.execute('SELECT atribuicao_fonte FROM propostas_equipe').fetchone()[0],'manual')
        c.close()

    def test_ranking_separates_ia_and_team(self):
        ns=[{'numero_id':1,'numero':'N','pagos':{1,2},'valor_pago':100}]
        r=ranking_wabas.calcular(ns,{1:{'waba_id':'A'}},{'A':'Conta'},[{'waba_id':'A','valor_centavos':20000}])['ranking'][0]
        self.assertEqual((r['ia_pagos'],r['equipe_pagos'],r['pagos'],r['valor_pago']),(2,1,3,300))
