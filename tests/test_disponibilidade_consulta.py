import unittest
from unittest.mock import patch,Mock
import requests
import main
import test_fila
from test_mailing import test_db

class DisponibilidadeConsultaTests(unittest.TestCase):
    resposta=staticmethod(test_fila.FilaTests.resposta)
    def setUp(self):
        test_fila.FilaTests.setUp(self)
        with test_db() as c:
            c.execute("UPDATE configuracao SET transbordo_habilitado=1,transbordo_url='https://destino.example',transbordo_token='segredo',transbordo_nome='Simplifica'")
    def consultar(self):
        r=self.client.get('/api/disponibilidade-consulta')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.headers['cache-control'],'no-store')
        self.api.assert_not_called()
        self.whatsapp.assert_not_called()
        self.assertFalse(self.fila.resumo()['contagens'])
        return r.json()
    def sem_capacidade(self):
        with test_db() as c: c.execute('UPDATE numeros SET ativo=0')
    def test_local_prioritario(self):
        with patch.object(main.requests,'get') as remoto:
            d=self.consultar()
            self.assertTrue(d['consultar_nova_vida']);self.assertEqual(d['destino_provavel'],'local')
            remoto.assert_not_called()
    def test_destino_disponivel(self):
        self.sem_capacidade()
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'disponivel':True}))):
            d=self.consultar()
            self.assertTrue(d['consultar_nova_vida']);self.assertEqual(d['nome_destino'],'Simplifica')
    def test_destino_recusa(self):
        self.sem_capacidade()
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'disponivel':False,'motivo_codigo':'destino_nao_configurado'}))):
            d=self.consultar();self.assertFalse(d['consultar_nova_vida']);self.assertEqual(d['motivo_destino'],'destino_nao_configurado')
    def test_falha_destino_nao_autoriza_consulta(self):
        self.sem_capacidade()
        with patch.object(main.requests,'get',side_effect=requests.Timeout()):
            self.assertFalse(self.consultar()['consultar_nova_vida'])
    def test_fora_horario_e_lote(self):
        with patch.object(main,'dentro_do_horario_permitido',return_value=(False,'Fora do horario')), patch.object(main.requests,'get',side_effect=requests.Timeout()):
            self.assertFalse(self.consultar()['consultar_nova_vida'])
        with patch.object(main,'estado_lote',return_value={'lote':True}):
            self.assertFalse(self.consultar()['consultar_nova_vida'])
    def test_fora_horario_consulta_destino(self):
        with patch.object(main,'dentro_do_horario_permitido',return_value=(False,'Fechado')), patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'disponivel':True}))):
            d=self.consultar()
            self.assertTrue(d['consultar_nova_vida'])
            self.assertEqual(d['destino_provavel'],'transbordo')

    def test_autenticacao_n8n(self):
        self.client.cookies.clear()
        with patch.object(main,'API_KEY','chave-n8n'):
            self.assertEqual(self.client.get('/api/disponibilidade-consulta').status_code,401)
            self.assertEqual(self.client.get('/api/disponibilidade-consulta',headers={'X-API-Key':'chave-n8n'}).status_code,200)
