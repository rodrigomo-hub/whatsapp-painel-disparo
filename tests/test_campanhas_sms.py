import unittest,time
from unittest.mock import patch,Mock
import main,campanhas_sms as sms,test_mailing
from test_mailing import test_db

class CampanhasSmsTests(unittest.TestCase):
    setUp=test_mailing.MailingTests.setUp
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def criar(self,phones=None):
        return sms.criar(main.get_db,'Campanha','Mensagem com link https://example.com',[
            {'linha':i,'lead':{'telefone':p}} for i,p in enumerate(phones or ['11999991234','11999991235'],1)],[])
    def saldo(self,*a):return {'valor':100,'erro':None}
    def horario(self):return True,''
    def iniciar(self,d):return sms.iniciar(main.get_db,d['id'],'token',self.horario,self.saldo)
    def processar(self):return sms.processar_um(main.get_db,'token',self.horario,self.saldo)

    def test_preview_dedup_invalid_and_optout_no_send(self):
        with test_db() as c:c.execute("INSERT INTO optout(telefone) VALUES('5511999991235')")
        d=self.criar(['11999991234','+55 11 99999-1234','11999991235','invalid'])
        self.assertEqual(d['importacao']['validos'],1);self.assertEqual(d['importacao']['repetidos'],1)
        self.assertEqual(d['importacao']['lista_negra'],1);self.assertEqual(d['importacao']['invalidos'],1)
        self.assertEqual(d['importacao']['linhas'],4);self.assertFalse(self.processar());self.api.assert_not_called()

    def test_explicit_start_sends_sms_only_and_history(self):
        d=self.criar();self.iniciar(d);self.iniciar(d)
        with patch.object(sms,'enviar',return_value=('sucesso','','uid')) as enviar:
            self.processar();self.processar();self.processar();self.processar();self.assertEqual(enviar.call_count,2)
        self.assertEqual(sms.detalhe(main.get_db,d['id'])['estado'],'concluida')
        with self.assertRaises(main.HTTPException):self.iniciar(d)
        rows=main.historico(canal='sms');self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['campanha_sms_nome'],'Campanha');self.assertEqual(rows[0]['canal'],'sms')
        self.assertEqual(main.total_hoje()['total_sms_hoje'],2);self.assertEqual(main.total_hoje()['total_outros_hoje'],0)
        self.whatsapp.assert_not_called();self.api.assert_not_called()

    def test_expiry_balance_error_and_window_close_stop_without_later_send(self):
        d=self.criar();self.iniciar(d)
        sms.processar_um(main.get_db,'token',lambda:(False,'Fora do horário'),self.saldo)
        self.assertEqual(sms.detalhe(main.get_db,d['id'])['contagens']['cancelado'],2)
        self.assertFalse(self.processar());self.api.assert_not_called()

    def test_timeout_stops_remaining_no_retry_and_restart(self):
        d=self.criar();self.iniciar(d)
        with patch.object(sms,'enviar',return_value=('incerto','Timeout','')) as enviar:
            self.processar();self.processar();sms.recuperar(main.get_db);self.processar();enviar.assert_called_once()
        self.assertEqual(sms.detalhe(main.get_db,d['id'])['contagens'],{'cancelado':1,'incerto':1})

    def test_optout_after_preview_skip_and_cancel(self):
        d=self.criar();self.iniciar(d)
        with test_db() as c:c.execute("INSERT INTO optout(telefone) VALUES('5511999991234')")
        self.processar();sms.cancelar(main.get_db,d['id']);self.assertFalse(self.processar())
        self.assertEqual(sms.detalhe(main.get_db,d['id'])['contagens'],{'bloqueado':1,'cancelado':1});self.api.assert_not_called()

    def test_no_token_no_balance_outside_window_and_duplicate_active(self):
        d=self.criar()
        for token,horario,saldo in [('',self.horario,self.saldo),('t',lambda:(False,'Fora'),self.saldo),('t',self.horario,lambda *a:{'valor':0}),('t',self.horario,lambda *a:{'valor':None})]:
            with self.assertRaises(main.HTTPException):sms.iniciar(main.get_db,d['id'],token,horario,saldo)
        self.iniciar(d)
        with self.assertRaises(main.HTTPException):self.iniciar(self.criar())

    def test_csv_upload_only_phone_and_no_automatic_sending(self):
        with test_db() as c:c.execute("UPDATE configuracao SET sms_template='Mensagem fixa' WHERE id=1")
        r=self.client.post('/api/campanhas-sms',data={'nome':'Teste CSV'},files={'arquivo':('lista.csv',b'telefone\n11999991234\n11999991235\n','text/csv')})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['contagens']['pendente'],2)
        self.assertFalse(self.processar());self.api.assert_not_called()

    def test_balance_query_no_undefined_phone(self):
        with patch.object(main,'SMS_GTI_TOKEN','token'),patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'data':{'remaining_balance':'10'}}))):
            self.assertEqual(main.consultar_saldo_sms(True)['valor'],10)

    def test_long_or_dynamic_message_rejected_not_truncated(self):
        for mensagem in ('x'*161,'Oi {1}',''):
            with self.assertRaises(main.HTTPException):sms.criar(main.get_db,'Teste',mensagem,[{'linha':1,'lead':{'telefone':'11999991234'}}],[])
