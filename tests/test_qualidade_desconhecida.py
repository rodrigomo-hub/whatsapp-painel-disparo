import unittest
from unittest.mock import patch
import main
import test_fila
from test_mailing import test_db

class QualidadeDesconhecidaTests(unittest.TestCase):
    resposta=staticmethod(test_fila.FilaTests.resposta)
    def setUp(self):
        test_fila.FilaTests.setUp(self)
    def test_sem_capacidade_nem_entrada(self):
        with test_db() as c:
            c.execute("UPDATE numeros SET qualidade='UNKNOWN'")
            n=c.execute('SELECT * FROM numeros LIMIT 1').fetchone()
        self.assertFalse(main.disponivel_agora(n))
        self.assertEqual(main.capacidade_lote()['capacidade_agora'],0)
        self.assertFalse(self.client.get('/api/disponibilidade-consulta').json()['consultar_nova_vida'])
        self.client.post('/disparar',json=self.lead)
        self.whatsapp.assert_not_called()
        self.assertFalse(self.fila.resumo()['contagens'])
    def test_envio_reconsulta_qualidade_atual(self):
        self.whatsapp_patch.stop()
        with test_db() as c:
            n=c.execute('SELECT * FROM numeros LIMIT 1').fetchone()
            c.execute("UPDATE numeros SET qualidade='UNKNOWN'")
        self.assertFalse(main.enviar_whatsapp(n,'5511987654321',[])[0])
        self.api.assert_not_called()
    def test_unknown_nao_reativa_pausado(self):
        with test_db() as c:
            c.execute('UPDATE numeros SET ativo=0,pausado_por_qualidade=1')
        main._aplicar_pausa_ou_reativacao_qualidade(1,'Teste',0,1,'UNKNOWN')
        with test_db() as c:
            self.assertEqual(c.execute('SELECT ativo FROM numeros WHERE id=1').fetchone()['ativo'],0)
