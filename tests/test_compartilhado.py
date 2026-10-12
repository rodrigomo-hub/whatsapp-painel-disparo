import unittest
from unittest.mock import patch, Mock
import requests, main, transbordo, vendeai_mailing
import test_transbordo
from test_mailing import test_db

class CompartilhadoTests(unittest.TestCase):
    resposta=staticmethod(test_transbordo.TransbordoTests.resposta)
    def setUp(self):
        test_transbordo.TransbordoTests.setUp(self)
        with test_db() as c:c.execute("UPDATE configuracao SET disparo_compartilhado=1,vendeai_account_id='33393'")
    def prep(self):return vendeai_mailing.preparar_lead(self.lead,main.carregar_configuracao())
    def count(self):
        with test_db() as c:return c.execute('SELECT COUNT(*) FROM disparos_compartilhados').fetchone()[0]
    def test_only_success_schedules_and_payload_contains_no_local_status(self):
        p=self.prep()
        main.registrar_disparo(1,'','Ana',p['mailing']['phone'],'erro',compartilhado=p)
        self.assertEqual(self.count(),0)
        main.registrar_disparo(1,'','Ana',p['mailing']['phone'],'sucesso',compartilhado=p)
        self.assertEqual(self.count(),1)
        self.api.side_effect=lambda *a,**kw:Mock(status_code=202,json=lambda:{'recibo':kw['json']['id'],'status':'enfileirado'})
        self.assertTrue(transbordo.processar_compartilhado(main.get_db,main.carregar_configuracao()))
        body=self.api.call_args.kwargs['json'];self.assertEqual(set(body),{'id','lead'})
        self.assertNotIn('status',body['lead']);self.assertNotIn('disparo_id',body['lead'])
        self.assertFalse(transbordo.processar_compartilhado(main.get_db,main.carregar_configuracao()))
        self.api.assert_called_once()
    def test_disabled_other_account_and_received_do_not_schedule(self):
        p=self.prep();p['_nao_compartilhar']=True
        main.registrar_disparo(1,'','Ana',p['mailing']['phone'],'sucesso',compartilhado=p)
        p.pop('_nao_compartilhar')
        with test_db() as c:c.execute("UPDATE configuracao SET vendeai_account_id='32239'")
        main.registrar_disparo(1,'','Ana',p['mailing']['phone'],'sucesso',compartilhado=p)
        with test_db() as c:c.execute("UPDATE configuracao SET vendeai_account_id='33393',disparo_compartilhado=0")
        main.registrar_disparo(1,'','Ana',p['mailing']['phone'],'sucesso',compartilhado=p)
        self.assertEqual(self.count(),0)
    def test_timeout_never_repeats_and_recovery_is_uncertain(self):
        p=self.prep();main.registrar_disparo(1,'','Ana',p['mailing']['phone'],'sucesso',compartilhado=p)
        self.api.side_effect=requests.Timeout()
        transbordo.processar_compartilhado(main.get_db,main.carregar_configuracao())
        transbordo.recuperar_compartilhados(main.get_db)
        self.assertFalse(transbordo.processar_compartilhado(main.get_db,main.carregar_configuracao()))
        self.api.assert_called_once()
    def test_optout_checked_again_before_forward(self):
        p=self.prep();main.registrar_disparo(1,'','Ana',p['mailing']['phone'],'sucesso',compartilhado=p)
        with test_db() as c:c.execute('INSERT INTO optout(telefone) VALUES(?)',(p['mailing']['phone'],))
        transbordo.processar_compartilhado(main.get_db,main.carregar_configuracao());self.api.assert_not_called()
    def test_expired_cancelled_without_call(self):
        p=self.prep();main.registrar_disparo(1,'','Ana',p['mailing']['phone'],'sucesso',compartilhado=p)
        with test_db() as c:c.execute('UPDATE disparos_compartilhados SET criado_em=0')
        transbordo.processar_compartilhado(main.get_db,main.carregar_configuracao());self.api.assert_not_called()

    def test_repeated_input_keeps_existing_receipt(self):
        a=self.client.post('/disparar',json={**self.lead,'idempotency_key':'same'}).json()
        b=self.client.post('/disparar',json={**self.lead,'idempotency_key':'same'},headers={'X-Transbordo-Hop':'1'}).json()
        self.assertEqual(a['fila_id'],b['fila_id'])
