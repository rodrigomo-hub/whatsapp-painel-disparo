import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import resultados as r


class ResultadosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'db'
        with self.db() as c:
            c.executescript('''
                CREATE TABLE numeros(id INTEGER PRIMARY KEY,nome TEXT,tipo_envio TEXT);
                CREATE TABLE disparos(id INTEGER PRIMARY KEY,numero_id INTEGER,telefone_destino TEXT,
                    criado_em TEXT,canal TEXT,tipo_envio TEXT,status TEXT);
                CREATE TABLE vendeai_eventos(id INTEGER PRIMARY KEY,recebido_em TEXT,payload_raw BLOB,
                    conta_confirmada INTEGER,repetido_de INTEGER);
                INSERT INTO numeros VALUES(1,'Normal teste','vendeai_normal');
                INSERT INTO numeros VALUES(2,'Oficial teste','meta');
            ''')

    def db(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        self.addCleanup(c.close)
        return c

    def send(self, ident=1, data='2026-09-30T06:00:00-03:00', tipo='vendeai_normal', numero=1):
        with self.db() as c:
            c.execute('INSERT INTO disparos VALUES(?,?,?,?,?,?,?)',
                      (ident, numero, '5511999991234', data, 'whatsapp', tipo, 'sucesso'))

    def event(self, event, time='2026-09-30T06:10:00-03:00', chat='1', proposal=None, duplicate=None,
              confirmed=1, ad=False, phone='5511999991234', cpf='01234567890'):
        d = {'event': event, 'chat_summary': {'account_id': 'a', 'chat_id': chat,
             'details': {'contact': {'name': 'Cliente', 'phone': phone, 'cpf': cpf},
                         'referral': {'ctwa_clid': 'ad123'} if ad else {}}}}
        if proposal:
            d['proposal' if event == 'proposal_created' else 'proposal_status'] = proposal
        with self.db() as c:
            c.execute('INSERT INTO vendeai_eventos(recebido_em,payload_raw,conta_confirmada,repetido_de) VALUES(?,?,?,?)',
                      (time, json.dumps(d), confirmed, duplicate))

    def calc(self, **kwargs):
        return r.calcular(self.db, r.momento('2026-09-30T00:00:00-03:00'),
                          r.momento('2026-10-01T00:00:00-03:00'), **kwargs)

    def test_funnel_unique_simulations_payment_values_and_link(self):
        self.send()
        self.event('conversation_created')
        self.event('simulation_offered')
        self.event('simulation_offered', chat='2')
        self.event('proposal_created', proposal={'proposal_id': 'p', 'liquid_value': 1200,
                   'formalization_link': 'https://example.test/p', 'proposal_status': 'FORMALIZATION_REQUESTED'})
        for minute in [20, 30]:
            self.event('proposal_status_updated', time=f'2026-09-30T06:{minute}:00-03:00',
                       proposal={'proposal_id': 'p', 'proposal_status': r.PAGO})
        self.event('proposal_created', proposal={'proposal_id': 'duplicado'}, duplicate=1)
        self.event('proposal_created', proposal={'proposal_id': 'outra_conta'}, confirmed=0)
        d = self.calc()
        self.assertEqual(d['atividade']['simulacoes'], 1)
        self.assertEqual(d['atividade']['pagos'], 1)
        self.assertEqual(d['atividade']['valor_pago'], 1200)
        self.assertEqual(d['origens']['normais']['propostas'], 1)
        self.assertEqual(d['coorte']['pagos'], 1)
        self.assertEqual(d['propostas'][0]['link'], 'https://example.test/p')
        self.assertEqual(d['propostas'][0]['status_nome'], 'Pago ao cliente')

    def test_paid_today_from_old_send_not_today_cohort(self):
        self.send(data='2026-09-29T06:00:00-03:00')
        self.event('conversation_created', time='2026-09-29T06:05:00-03:00')
        self.event('proposal_created', time='2026-09-29T06:10:00-03:00',
                   proposal={'proposal_id': 'p', 'liquid_value': 100, 'proposal_status': 'PROCESSING'})
        self.event('proposal_status_updated', proposal={'proposal_id': 'p', 'proposal_status': r.PAGO})
        d = self.calc()
        self.assertEqual(d['atividade']['pagos'], 1)
        self.assertEqual(d['atividade']['propostas'], 0)
        self.assertEqual(d['coorte']['pagos'], 0)
        self.assertEqual(d['numeros'][0]['pagos'], 1)

    def test_explicit_ad_wins_and_campaign_alone_does_not_imply_traffic(self):
        self.send()
        self.event('conversation_created', ad=True)
        self.event('proposal_created', proposal={'proposal_id': 'p', 'proposal_status': r.PAGO})
        d = self.calc()
        self.assertEqual(d['origens']['trafego']['pagos'], 1)
        self.assertEqual(d['coorte']['pagos'], 0)
        self.assertEqual(d['atividade']['pagos_sem_valor'], 1)
        self.assertIsNone(d['propostas'][0]['valor'])

    def test_missing_creation_and_old_sends_remain_unattributed(self):
        self.send(data='2026-09-01T06:00:00-03:00')
        self.event('conversation_created')
        self.event('proposal_created', chat='2', proposal={'proposal_id': 'p'})
        d = self.calc()
        self.assertEqual(d['origens']['nao_identificada']['conversas'], 1)
        self.assertEqual(d['propostas'][0]['origem'], 'nao_identificada')

    def test_latest_previous_send_snapshot_survives_changed_number_type(self):
        self.send(1, tipo='vendeai_normal', numero=2)
        self.send(2, data='2026-09-30T06:09:00-03:00', tipo='meta', numero=1)
        self.send(3, data='2026-09-30T06:11:00-03:00')
        self.event('conversation_created')
        self.event('proposal_created', proposal={'proposal_id': 'p'})
        d = self.calc()
        self.assertEqual(d['propostas'][0]['origem'], 'oficiais')
        self.assertEqual(d['origens']['oficiais']['propostas'], 1)
        self.assertEqual(d['origens']['normais']['propostas'], 0)

    def test_brasilia_day_boundary_and_first_paid_only(self):
        self.event('proposal_status_updated', time='2026-09-30T02:59:00+00:00',
                   proposal={'proposal_id': 'p', 'proposal_status': r.PAGO})
        self.event('proposal_status_updated', time='2026-09-30T03:01:00+00:00',
                   proposal={'proposal_id': 'p', 'proposal_status': r.PAGO})
        d = self.calc()
        self.assertEqual(d['atividade']['pagos'], 0)
        self.assertEqual(d['total_propostas'], 1)

    def test_search_filters_and_readonly_database(self):
        self.event('proposal_created', proposal={'proposal_id': 'abc', 'liquid_value': '123.45'})
        self.assertEqual(self.calc(busca='ABC')['total_propostas'], 1)
        self.assertEqual(self.calc(origem='normais')['total_propostas'], 0)
        self.assertEqual(self.calc(status=r.PAGO)['total_propostas'], 0)
        def readonly():
            c = sqlite3.connect('file:'+str(self.path)+'?mode=ro', uri=True)
            c.row_factory = sqlite3.Row
            return c
        self.assertEqual(r.calcular(readonly, r.momento('2026-09-30'), r.momento('2026-10-01'))['atividade']['valor_propostas'], 123.45)


if __name__ == '__main__':
    unittest.main()
