import unittest
from unittest.mock import Mock, patch
import main
import test_fila
from test_mailing import test_db

class PresencaTests(unittest.TestCase):
    resposta=staticmethod(test_fila.FilaTests.resposta)
    def setUp(self):
        test_fila.FilaTests.setUp(self)
        with test_db() as c:
            c.execute("UPDATE numeros SET waba_id='waba',access_token='token',phone_number_id='123'")
    def estados(self):
        with test_db() as c:
            return [r['presenca_meta'] for r in c.execute('SELECT presenca_meta FROM numeros')]
    def test_ausencia(self):
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'data':[]}))):
            main.verificar_presenca_meta()
        self.assertTrue(all(x=='ausente' for x in self.estados()))
    def test_erro_nao_e_exclusao(self):
        with patch.object(main.requests,'get',return_value=Mock(status_code=403,json=Mock(return_value={'error':{}}))):
            main.verificar_presenca_meta()
        self.assertTrue(all(x=='indisponivel' for x in self.estados()))
    def test_paginacao(self):
        respostas=[Mock(status_code=200,json=Mock(return_value={'data':[],'paging':{'next':'ignored','cursors':{'after':'abc'}}})),Mock(status_code=200,json=Mock(return_value={'data':[{'id':'123'}]}))]
        with patch.object(main.requests,'get',side_effect=respostas) as api:
            main.verificar_presenca_meta()
            self.assertEqual(api.call_args.kwargs['params']['after'],'abc')
        self.assertTrue(all(x=='presente' for x in self.estados()))
