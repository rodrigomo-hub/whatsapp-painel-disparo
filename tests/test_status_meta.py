import unittest
from unittest.mock import Mock,patch
import main
import test_fila
from test_mailing import test_db
class StatusMetaTests(unittest.TestCase):
    resposta=staticmethod(test_fila.FilaTests.resposta)
    def setUp(self):
        test_fila.FilaTests.setUp(self)
        with test_db() as c:c.execute("UPDATE numeros SET waba_id='waba'")
    def numero(self):
        with test_db() as c:return c.execute('SELECT * FROM numeros LIMIT 1').fetchone()
    def test_banido_com_qualidade_alta(self):
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'quality_rating':'GREEN','status':'BANNED'}))):
            self.assertEqual(main.verificar_qualidade(self.numero()),'GREEN')
        self.assertEqual(self.numero()['status_envio_meta'],'BANNED')
        self.assertEqual(self.numero()['ativo'],0)
        self.assertEqual(self.numero()['pausado_por_qualidade'],0)
        self.assertFalse(main.disponivel_agora(self.numero()))
        self.assertEqual(main.capacidade_lote()['capacidade_agora'],0)
    def test_waba_bloqueada_com_numero_conectado(self):
        for health,esperado in [({'can_send_message':'BLOCKED'},'BLOCKED'),({'can_send_message':'AVAILABLE'},'AVAILABLE'),({},'UNKNOWN')]:
            with self.subTest(health=health),patch.object(main.requests,'get',side_effect=[Mock(status_code=200,json=Mock(return_value={'quality_rating':'GREEN','status':'CONNECTED'})),Mock(status_code=200,json=Mock(return_value={'health_status':health}))]):
                main.verificar_qualidade(self.numero())
                self.assertEqual(self.numero()['status_envio_meta'],esperado)
    def test_bloqueia_no_envio_com_selecao_antiga(self):
        self.whatsapp_patch.stop();antigo=self.numero()
        with test_db() as c:c.execute("UPDATE numeros SET status_envio_meta='BANNED'")
        self.assertFalse(main.enviar_whatsapp(antigo,'5511999999999',[])[0])
        self.api.assert_not_called()
