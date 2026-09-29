import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException
import vendeai_eventos as eventos


class EventosTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.db'
        with self.db() as c:
            eventos.migrar(c)
            self.token = c.execute('SELECT token FROM vendeai_webhook_config').fetchone()[0]
        self.event = {'event': 'proposal_created', 'chat_summary': {
            'account_id': 'test', 'chat_id': '1', 'product': 'clt',
            'details': {'contact': {'phone': '+55 11 99999-1234'}}},
            'proposal': {'proposal_id': 'p1', 'bank': 'facta'}}
        self.config = {'url': 'https://api.newcorban.com.br/api/propostas/',
                       'campos': {}, 'modelo': {'auth': {'password': 'secret-test'},
                       'content': {'cliente': {'telefones': {}}, 'proposta': {
                           'valor_parcela': 100, 'valor_financiado': 1000,
                           'valor_liberado': 1000, 'prazo': 12}}}}

    def db(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        self.addCleanup(c.close)
        return c

    def receive(self):
        return eventos.receber(self.db, self.token, json.dumps(self.event).encode(), 'test')

    def enable(self):
        with self.db() as c:
            c.execute('UPDATE vendeai_webhook_config SET new_habilitado=1,new_config=?',
                      (json.dumps(self.config),))

    def state(self):
        with self.db() as c:
            return dict(c.execute('SELECT * FROM vendeai_new_outbox').fetchone())

    def test_raw_receipts_preserved_and_proposal_deduplicated(self):
        self.receive(); self.receive()
        with self.db() as c:
            rows = c.execute('SELECT * FROM vendeai_eventos ORDER BY id').fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]['repetido_de'], rows[0]['id'])
            self.assertEqual(rows[0]['payload_raw'], json.dumps(self.event).encode())
            self.assertEqual(c.execute('SELECT count(*) FROM vendeai_new_outbox').fetchone()[0], 1)

    def test_wrong_token_and_account_rejected(self):
        for token, account in [('wrong', 'test'), (self.token, 'other')]:
            with self.assertRaises(HTTPException):
                eventos.receber(self.db, token, json.dumps(self.event).encode(), account)
        self.assertEqual(eventos.listar(self.db)['total'], 0)

    def test_other_events_saved_without_forwarding(self):
        self.event['event'] = 'stage_updated'
        self.receive()
        self.assertEqual(eventos.listar(self.db)['total'], 1)
        with self.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM vendeai_new_outbox').fetchone()[0], 0)

    def test_actual_phone_and_http_result_no_duplicate(self):
        self.enable(); self.receive()
        with patch.object(eventos.requests, 'post', return_value=Mock(status_code=200, text='secret-test ok')) as post:
            eventos.processar_um(self.db)
            self.receive(); eventos.processar_um(self.db)
            post.assert_called_once()
            payload = post.call_args.kwargs['json']
            self.assertEqual(payload['content']['cliente']['telefones'], {'999991234': {'ddd': '11', 'numero': '999991234'}})
            self.assertEqual(payload['content']['proposta']['telefone_id'], '999991234')
        self.assertEqual(self.state()['estado'], 'ENVIADO_HTTP')
        self.assertNotIn('secret-test', self.state()['resposta'])

    def test_missing_phone_does_not_send_and_corrected_event_can_retry(self):
        self.enable()
        self.event['chat_summary']['details']['contact']['phone'] = ''
        self.receive()
        with patch.object(eventos.requests, 'post') as post:
            eventos.processar_um(self.db)
            post.assert_not_called()
        self.assertEqual(self.state()['estado'], 'DADOS_INCOMPLETOS')
        self.event['chat_summary']['details']['contact']['phone'] = '5511999991234'
        self.receive()
        self.assertEqual(self.state()['estado'], 'PENDENTE')

    def test_timeout_never_retried_automatically(self):
        self.enable(); self.receive()
        with patch.object(eventos.requests, 'post', side_effect=requests.Timeout) as post:
            eventos.processar_um(self.db)
            self.receive(); eventos.processar_um(self.db)
            post.assert_called_once()
        self.assertEqual(self.state()['estado'], 'INCERTO')

    def test_disabled_stores_without_sending(self):
        self.receive()
        with patch.object(eventos.requests, 'post') as post:
            eventos.processar_um(self.db)
            post.assert_not_called()
        self.assertEqual(self.state()['estado'], 'PENDENTE')

    def test_restart_marks_uncertain(self):
        self.receive()
        with self.db() as c:
            c.execute("UPDATE vendeai_new_outbox SET estado='ENVIANDO'")
        eventos.recuperar_interrompidos(self.db)
        self.assertEqual(self.state()['estado'], 'INCERTO')
