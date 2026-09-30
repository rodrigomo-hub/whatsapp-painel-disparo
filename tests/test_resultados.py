import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import resultados as r
from fastapi import HTTPException


class ResultadosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'db'
        with self.db() as c:
            r.migrar(c)
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

    def test_manual_daily_upsert_edit_no_double_count_and_no_attribution(self):
        body = {'valor': '3534.27', 'revisao': 0}
        first = r.salvar_manual(self.db, '2026-09-29', body)
        self.assertEqual(first['valor_centavos'], 353427)
        self.assertEqual(r.salvar_manual(self.db, '2026-09-29', body)['revisao'], 1)
        r.salvar_manual(self.db, '2026-09-29', dict(body, quantidade=11, valor='4000.10', revisao=1))
        d = r.calcular(self.db, r.momento('2026-09-29'), r.momento('2026-10-01'))
        self.assertEqual(d['pagamentos']['manual'], {'valor': 4000.10})
        self.assertNotIn('quantidade', d['pagamentos']['total'])
        self.assertEqual(d['atividade']['pagos'], 0)
        self.assertEqual(d['coorte']['pagos'], 0)
        self.assertEqual(d['numeros'], [])
        self.assertEqual(self.calc()['pagamentos']['manual']['valor'], 0)
        r.salvar_manual(self.db, '2026-09-29', {'quantidade': 0, 'valor': '0', 'revisao': 2})
        self.assertEqual(r.manual_dia(self.db, '2026-09-29')['valor_centavos'], 0)

    def test_manual_validation_and_concurrent_update(self):
        for val in [-10, 'NaN', 'Infinity', '0.001']:
            with self.subTest(val=val), self.assertRaises(HTTPException) as cm:
                r.salvar_manual(self.db, '2026-09-29', {'valor': val, 'revisao': 0})
            self.assertEqual(cm.exception.status_code, 400)
        for dia in ['invalid', '2099-01-01', '20260929']:
            with self.assertRaises(HTTPException): r.manual_dia(self.db, dia)
        r.salvar_manual(self.db, '2026-09-29', {'quantidade': 1, 'valor': '0.10', 'revisao': 0})
        with self.assertRaises(HTTPException) as cm:
            r.salvar_manual(self.db, '2026-09-29', {'quantidade': 2, 'valor': '0.20', 'revisao': 0})
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(r.manual_dia(self.db, '2026-09-29')['valor_centavos'], 10)

    def test_manual_total_plus_ai_preserves_missing_value_warning(self):
        self.event('proposal_status_updated', proposal={'proposal_id': 'p', 'proposal_status': r.PAGO})
        r.salvar_manual(self.db, '2026-09-30', {'quantidade': 10, 'valor': '3534', 'revisao': 0})
        d = self.calc()
        self.assertEqual(d['pagamentos']['total'], {'valor': 3534.0, 'sem_valor': 1})
        self.assertEqual(d['pagamentos']['ia']['quantidade'], 1)
        self.assertNotIn('quantidade', d['pagamentos']['manual'])

    def historico(self, codigo='123', dia='2026-09-30', centavos=50000):
        with self.db() as c:
            c.execute('INSERT INTO pagamentos_ia_historicos VALUES(?,?,?,?,?,?,?,?,?)',
                      ('a', codigo, '', dia, centavos, dia, 'csv', 'hash', dia))

    def test_history_fills_missing_then_late_webhook_does_not_double(self):
        self.historico()
        d = self.calc()
        self.assertEqual(d['pagamentos']['ia']['valor'], 500)
        self.assertEqual(d['pagamentos']['ia']['quantidade'], 0)
        self.assertEqual(d['coorte']['valor_pago'], 0)
        self.event('proposal_status_updated', proposal={'proposal_id': 'uuid', 'proposal_number': '123', 'proposal_status': r.PAGO})
        d = self.calc()
        self.assertEqual(d['pagamentos']['ia']['valor'], 500)
        self.assertEqual(d['pagamentos']['ia']['sem_valor'], 0)
        self.event('proposal_created', proposal={'proposal_id': 'uuid', 'proposal_number': '123', 'liquid_value': 500})
        d = self.calc()
        self.assertEqual(d['pagamentos']['ia']['valor'], 500)
        self.assertEqual(d['pagamentos']['ia']['ajuste_historico'], 0)

    def test_history_reference_date_moves_value_without_changing_events(self):
        self.historico(dia='2026-09-29')
        self.event('proposal_created', proposal={'proposal_id': '123', 'liquid_value': 500, 'proposal_status': r.PAGO})
        today = self.calc()
        self.assertEqual(today['pagamentos']['ia']['valor'], 0)
        self.assertEqual(today['atividade']['valor_pago'], 500)
        previous = r.calcular(self.db, r.momento('2026-09-29'), r.momento('2026-09-30'))
        self.assertEqual(previous['pagamentos']['ia']['valor'], 500)
        month = r.calcular(self.db, r.momento('2026-09-01'), r.momento('2026-10-01'))
        self.assertEqual(month['pagamentos']['ia']['valor'], 500)
        with self.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM vendeai_eventos').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
