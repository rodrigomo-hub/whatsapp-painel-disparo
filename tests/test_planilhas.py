import io
import unittest
from unittest.mock import patch

from openpyxl import Workbook

import main
import fila_disparos
import planilhas
import test_fila
from test_mailing import test_db


class PlanilhaTests(unittest.TestCase):
    resposta = staticmethod(test_fila.FilaTests.resposta)

    def setUp(self):
        test_fila.FilaTests.setUp(self)

    def importar(self, texto):
        return self.client.post('/api/lote/arquivo', files={'arquivo': ('leads.csv', texto.encode('utf-8'), 'text/csv')})

    def test_csv_preserva_dados_e_nao_envia_na_previa(self):
        r = self.importar('whatsapp;cpf;nome;nascimento;produto;campanha;variavel_1\n11987654321;11144477735;José;23/11/1992;clt;teste;José\n').json()
        self.assertTrue(r['pode_importar'])
        self.assertEqual(r['leads'][0]['cpf'], '11144477735')
        self.assertEqual(r['leads'][0]['variaveis_template'], ['José'])
        self.assertEqual(r['validos'], 1)
        self.api.assert_not_called()
        self.whatsapp.assert_not_called()
        self.assertFalse(self.fila.resumo()['contagens'])

    def test_duplicados_normalizados_e_erros_com_linha(self):
        r = self.importar('telefone;cpf\n11987654321;11144477735\n5511987654321;11144477735\n11987654322;123\n').json()
        self.assertEqual((r['total_linhas'], r['validos'], r['duplicados']), (3, 1, 1))
        self.assertEqual(r['erros'][0]['linha'], 4)
        self.assertFalse(r['pode_importar'])

    def test_xlsx_datas_numericas_e_cpf_com_zero(self):
        from datetime import datetime
        book = Workbook()
        book.active.append(['telefone', 'cpf', 'nome', 'nascimento'])
        book.active.append([11987654321, 1234567890, 'Teste', datetime(1991, 11, 10)])
        data = io.BytesIO(); book.save(data)
        rows, errors = planilhas.ler('leads.xlsx', data.getvalue())
        self.assertEqual(rows[0]['lead']['cpf'], '01234567890')
        self.assertEqual(rows[0]['lead']['nascimento'], '10/11/1991')
        self.assertFalse(errors)

    def test_arquivo_invalido_e_formula_nao_sao_importados(self):
        self.assertEqual(self.importar('nome;cpf\nTeste;123').status_code, 400)
        r = self.importar('telefone;nome\n11987654321;=FORMULA()').json()
        self.assertFalse(r['pode_importar'])
        self.assertEqual(r['erros'][0]['linha'], 2)
        r = self.client.post('/api/lote/arquivo', files={'arquivo': ('leads.xlsx', b'invalido')})
        self.assertEqual(r.status_code, 400)

    def test_capacidade_soma_minimo_por_numero_e_desconta_uso(self):
        with test_db() as c:
            c.execute('UPDATE numeros SET limite_diario=500, limite_hora=100')
            c.execute("INSERT INTO numeros(nome,phone_number_id,access_token,limite_diario,limite_hora) VALUES('Segundo','2','2',500,100)")
        with patch.object(main, 'contagem_hoje', side_effect=lambda n: 490 if n == 1 else 0), patch.object(main, 'contagem_ultima_hora', side_effect=lambda n: 0 if n == 1 else 90):
            r = self.client.post('/api/lote/capacidade', json={}).json()
            self.assertEqual(r['restante_dia'], 510)
            self.assertEqual(r['restante_hora'], 110)
            self.assertEqual(r['capacidade_agora'], 20)
            r = self.client.post('/api/lote/capacidade', json={'numeros_selecionados':[1]}).json()
            self.assertEqual(r['capacidade_agora'], 10)

    def test_lote_acima_da_capacidade_nao_grava_nem_cadastra(self):
        with test_db() as c:
            c.execute('UPDATE numeros SET limite_hora=1')
        r = self.client.post('/api/disparo-lote', json={'leads': [self.lead, {**self.lead, 'telefone':'11987654322'}]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['motivo_codigo'], 'capacidade_insuficiente')
        self.assertFalse(self.fila.resumo()['contagens'])
        self.api.assert_not_called()

    def test_lote_bloqueia_n8n_outro_lote_e_prechecagem_ate_finalizar(self):
        r = self.client.post('/api/disparo-lote', json={'leads':[self.lead]})
        self.assertEqual(r.status_code, 202)
        lead_id = r.json()['resultados'][0]['fila_id']
        self.assertEqual(self.client.post('/disparar', json={**self.lead, 'telefone':'11987654322'}).status_code, 409)
        self.assertEqual(self.client.post('/api/disparo-lote', json={'leads':[self.lead]}).status_code, 409)
        self.assertFalse(self.client.get('/api/pode-disparar').json()['pode_disparar'])
        self.fila.cancelar(lead_id)
        self.assertEqual(self.client.post('/disparar', json={**self.lead, 'telefone':'11987654322'}).status_code, 202)

    def test_lote_nao_mistura_com_fila_individual_anterior(self):
        self.client.post('/disparar', json=self.lead)
        r = self.client.post('/api/disparo-lote', json={'leads':[self.lead]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['motivo_codigo'], 'fila_ocupada')

    def test_admissao_concorrente_recusada_sem_gravar(self):
        with fila_disparos.trava_worker(str(main.DB_FILE) + '.entrada') as obtida:
            self.assertTrue(obtida)
            r = self.client.post('/disparar', json=self.lead)
            self.assertEqual(r.status_code, 409)
        self.assertFalse(self.fila.resumo()['contagens'])

    def test_arquivo_ate_cadastro_preserva_campos(self):
        d = self.importar('telefone;nome;cpf;nascimento;campanha;variavel_1\n11987654321;Ana;11144477735;10/11/1991;campanha_x;Ana').json()
        r = self.client.post('/api/disparo-lote', json={'leads':d['leads']})
        self.assertEqual(r.status_code, 202)
        self.clock.return_value += 300
        self.fila.processar(main.carregar_configuracao, main.cadastrar_base_vendeai, main.executar_disparo_preparado, main.dentro_do_horario_permitido, impedimento=main.impedimento_fila)
        m = self.api.call_args.kwargs['json']['leads'][0]
        self.assertEqual(m['birth_date'], '10/11/1991')
        self.assertEqual(m['campaign'], 'campanha_x')
        self.assertRegex(m['email'], r'^ana[0-9]{5}@gmail\.com$')
        self.whatsapp.assert_called_once()
