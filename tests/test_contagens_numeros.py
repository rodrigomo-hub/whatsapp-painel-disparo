import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
import main
from contextlib import closing

class ContagensTests(unittest.TestCase):
    def test_lote_equivale_contagens_individuais_inclusive_meia_noite(self):
        with tempfile.TemporaryDirectory() as pasta, patch.object(main,'DB_FILE',Path(pasta)/'db'):
            main.init_db()
            agora=datetime(2026,10,7,0,30,tzinfo=main.TZ)
            with closing(main.get_db()) as c,c:
                c.execute("INSERT INTO numeros(nome,phone_number_id,access_token) VALUES('Teste','1','fake')")
                c.execute("INSERT INTO numeros(nome,phone_number_id,access_token) VALUES('Vazio','2','fake')")
                for minutos,status in [(10,'sucesso'),(20,'incerto'),(40,'sucesso'),(70,'sucesso'),(5,'erro')]:
                    c.execute("INSERT INTO disparos(numero_id,telefone_destino,status,criado_em) VALUES(1,'5511999999999',?,?)",(status,(agora-timedelta(minutes=minutos)).isoformat()))
            with patch.object(main,'datetime') as relogio:
                relogio.now.return_value=agora
                rows=main.listar_numeros()
                self.assertEqual(rows[0]['enviados_hoje'],main.contagem_hoje(1))
                self.assertEqual(rows[0]['enviados_hora'],main.contagem_ultima_hora(1))
                self.assertEqual((rows[0]['enviados_hoje'],rows[0]['enviados_hora']),(2,3))
                self.assertEqual((rows[1]['enviados_hoje'],rows[1]['enviados_hora']),(0,0))
