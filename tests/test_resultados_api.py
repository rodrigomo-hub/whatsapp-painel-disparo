import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import main


class ResultadosApiTests(unittest.TestCase):
    def test_manual_requires_login_and_persists_through_migration(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DB_FILE', Path(tmp)/'db'):
            main.init_db()
            client = TestClient(main.app)
            body = {'quantidade': 10, 'valor': '3534.00', 'revisao': 0}
            url = '/api/resultados/manuais/2026-09-29'
            try:
                self.assertEqual(client.get(url).status_code, 401)
                self.assertEqual(client.put(url, json=body).status_code, 401)
                main.sessoes_ativas.add('test-resultados')
                client.cookies.set('session_token', 'test-resultados')
                self.assertEqual(client.get(url).json()['revisao'], 0)
                self.assertEqual(client.put(url, json=body).status_code, 200)
                main.init_db()
                self.assertEqual(client.get(url).json()['valor_centavos'], 353400)
                d = client.get('/api/resultados?inicio=2026-09-29&fim=2026-09-29').json()
                self.assertEqual(d['pagamentos']['total']['quantidade'], 10)
                self.assertEqual(d['coorte']['pagos'], 0)
                self.assertEqual(client.put(url, json=dict(body, quantidade=11)).status_code, 409)
                self.assertEqual(client.put(url, json=dict(body, valor='1.001')).status_code, 400)
                self.assertIn('Pagamentos manuais', client.get('/resultados').text)
            finally:
                main.sessoes_ativas.discard('test-resultados')
                client.close()
