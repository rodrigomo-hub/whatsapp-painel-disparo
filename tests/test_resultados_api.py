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
            body = {'valor': '3534.00', 'revisao': 0}
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
                self.assertEqual(d['pagamentos']['total']['valor'], 3534)
                self.assertEqual(d['coorte']['pagos'], 0)
                self.assertEqual(client.put(url, json=dict(body, valor='4000')).status_code, 409)
                self.assertEqual(client.put(url, json=dict(body, valor='1.001')).status_code, 400)
                self.assertIn('Pagamentos da equipe', client.get('/resultados').text)
            finally:
                main.sessoes_ativas.discard('test-resultados')
                client.close()

    def test_proposta_equipe_e_custos_exigem_login(self):
        import uuid,comissoes
        with tempfile.TemporaryDirectory() as tmp, patch.object(main,'DB_FILE',Path(tmp)/'db'):
            main.init_db()
            c=main.get_db()
            with c:comissoes.semear(c)
            c.close()
            client=TestClient(main.app)
            try:
                body={'acao':'salvar','operacao_id':str(uuid.uuid4()),'revisao':0,'dia':'2026-10-03','identificacao':'Teste','tabela':'69639','prazo':48,'valor':'5.067,26'}
                self.assertEqual(client.post('/api/resultados/equipe',json=body).status_code,401)
                self.assertEqual(client.put('/api/resultados/custos-meta',json={}).status_code,401)
                main.sessoes_ativas.add('test-equipe');client.cookies.set('session_token','test-equipe')
                self.assertEqual(client.post('/api/resultados/equipe',json=body).status_code,200)
                self.assertEqual(client.post('/api/resultados/equipe',json=body).status_code,200)
                data=client.get('/api/resultados?inicio=2026-10-03&fim=2026-10-03').json()
                self.assertEqual(data['pagamentos']['manual']['valor'],5067.26)
                self.assertEqual(data['comissoes']['pagos_equipe'],263.5)
                self.assertEqual(len(data['equipe']),1)
            finally:
                main.sessoes_ativas.discard('test-equipe');client.close()
