import unittest
from unittest.mock import Mock
import main
import test_mailing
from test_mailing import test_db

class PausaTemplateTests(unittest.TestCase):
    resposta=staticmethod(test_mailing.MailingTests.resposta)
    def setUp(self):
        test_mailing.MailingTests.setUp(self);self.whatsapp_patch.stop()
        with test_db() as c:
            c.execute("UPDATE numeros SET waba_id='w1',template_nome='oferta',template_idioma='pt_BR'")
            for phone,waba,template,idioma in [('p2','w1','oferta','pt_BR'),('p3','w2','oferta','pt_BR'),('p4','w1','outra','pt_BR'),('p5','w1','oferta','en_US')]:
                c.execute('INSERT INTO numeros(nome,phone_number_id,access_token,waba_id,template_nome,template_idioma) VALUES(?,?,?,?,?,?)',(phone,phone,'token',waba,template,idioma))
                c.execute("UPDATE numeros SET qualidade='GREEN' WHERE id=last_insert_rowid()")
            self.numero=dict(c.execute('SELECT * FROM numeros WHERE id=1').fetchone())
    def test_132015_pausa_mesma_waba_e_variante(self):
        self.api.return_value=Mock(status_code=400,json=Mock(return_value={'error':{'code':132015}}))
        self.assertFalse(main.enviar_whatsapp(self.numero,'5511987654321',[])[0])
        with test_db() as c:
            pausados=[r[0] for r in c.execute('SELECT id FROM numeros WHERE pausado_por_template=1 ORDER BY id')]
            self.assertEqual(pausados,[1,2])
            self.assertEqual(c.execute('SELECT ativo FROM numeros WHERE id=1').fetchone()[0],0)
        self.assertEqual(self.client.patch('/api/numeros/1',json={'ativo':1}).status_code,400)
    def test_qualidade_nao_reativa_template_pausado(self):
        main.pausar_template(self.numero)
        main._aplicar_pausa_ou_reativacao_qualidade(1,'Teste',0,1,'GREEN')
        with test_db() as c:self.assertEqual(c.execute('SELECT ativo FROM numeros WHERE id=1').fetchone()[0],0)
    def test_outro_erro_nao_pausa(self):
        self.api.return_value=Mock(status_code=400,text='erro',json=Mock(return_value={'error':{'code':132000}}))
        main.enviar_whatsapp(self.numero,'5511987654321',[])
        with test_db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM numeros WHERE pausado_por_template=1').fetchone()[0],0)
    def test_repeticao_nao_duplica_evento(self):
        main.pausar_template(self.numero);main.pausar_template(self.numero)
        with test_db() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM eventos WHERE tipo='pausado_template'").fetchone()[0],2)
