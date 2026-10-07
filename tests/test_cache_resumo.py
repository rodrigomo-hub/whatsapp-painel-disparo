import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import main

class CacheResumoTests(unittest.TestCase):
    def setUp(self):
        main.invalidar_resumo();self.addCleanup(main.invalidar_resumo)
        p=patch.object(main.resultados,'calcular',return_value={'atualizado_em':'2026-10-07T10:00:00-03:00',
            'atividade':{'simulacoes':1,'valor_propostas':100,'propostas_sem_valor':0},'pagamentos':{},'comissoes':{}})
        self.calcular=p.start();self.addCleanup(p.stop)
    def test_cache_expira_e_mantem_horario_original(self):
        with patch.object(main.time,'monotonic',return_value=100):
            a=main.resumo_resultados();b=main.resumo_resultados()
        self.assertEqual(self.calcular.call_count,1);self.assertEqual(a.body,b.body)
        self.assertEqual(b.headers['X-Resumo-Cache'],'hit')
        with patch.object(main.time,'monotonic',return_value=131):main.resumo_resultados()
        self.assertEqual(self.calcular.call_count,2)
    def test_concorrencia_calcula_uma_vez(self):
        with ThreadPoolExecutor(max_workers=4) as pool:res=list(pool.map(lambda _:main.resumo_resultados(),range(4)))
        self.assertEqual(self.calcular.call_count,1)
        self.assertEqual(sum(r.headers['X-Resumo-Cache']=='miss' for r in res),1)
    def test_edicao_financeira_invalida_e_outro_banco_nao_reutiliza(self):
        main.resumo_resultados()
        with patch.object(main.comissoes,'operar',return_value={'ok':True}):main.proposta_equipe({})
        main.resumo_resultados();self.assertEqual(self.calcular.call_count,2)
        with patch.object(main.resultados,'salvar_manual',return_value={'ok':True}):main.salvar_pagamento_manual('2026-10-07',{})
        main.resumo_resultados();self.assertEqual(self.calcular.call_count,3)
        with patch.object(main,'DB_FILE','outra-conta.db'):main.resumo_resultados()
        self.assertEqual(self.calcular.call_count,4)
