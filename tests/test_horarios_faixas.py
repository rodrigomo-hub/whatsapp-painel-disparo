import unittest
from datetime import datetime
from unittest.mock import patch
from fastapi import HTTPException
import main

FAIXAS=[{'inicio':'00:00','fim':'03:00','dias':list(range(1,8))},{'inicio':'05:00','fim':'08:00','dias':list(range(1,8))},{'inicio':'18:00','fim':'19:00','dias':list(range(1,7))},{'inicio':'09:00','fim':'12:00','dias':[7]}]

class HorariosTests(unittest.TestCase):
    def test_limites_e_dias(self):
        cfg={'modo':'horario','faixas_horario':main.validar_faixas(FAIXAS)}
        for dia,hora,esperado in [(28,'00:00',True),(28,'02:59',True),(28,'03:00',False),(28,'05:00',True),(28,'08:00',False),(28,'18:00',True),(28,'19:00',False),(27,'09:00',True),(27,'12:00',False),(27,'18:00',False)]:
            with self.subTest(dia=dia,hora=hora), patch.object(main,'carregar_configuracao',return_value=cfg), patch.object(main,'datetime') as relogio:
                relogio.now.return_value=datetime.fromisoformat(f'2026-09-{dia}T{hora}:00').replace(tzinfo=main.TZ)
                self.assertEqual(main.dentro_do_horario_permitido()[0],esperado)
    def test_automatico(self):
        with patch.object(main,'carregar_configuracao',return_value={'modo':'automatico','faixas_horario':FAIXAS}):
            self.assertTrue(main.dentro_do_horario_permitido()[0])
    def test_validacao(self):
        for faixas in [[{'inicio':'25:00','fim':'26:00','dias':[1]}],[{'inicio':'22:00','fim':'03:00','dias':[1]}],[{'inicio':'00:00','fim':'03:00','dias':[]}],FAIXAS+[FAIXAS[0]]]:
            with self.assertRaises(HTTPException): main.validar_faixas(faixas)
        self.assertEqual(main.validar_faixas([]),[])
        self.assertEqual(main.validar_faixas([{'inicio':'23:00','fim':'24:00','dias':[1]}])[0]['fim'],'24:00')
