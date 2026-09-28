import unittest
from unittest.mock import Mock, patch
import requests
import main
import transbordo
import test_fila
from test_mailing import test_db


class TransbordoTests(unittest.TestCase):
    resposta = staticmethod(test_fila.FilaTests.resposta)

    def setUp(self):
        test_fila.FilaTests.setUp(self)
        with test_db() as c:
            c.execute("UPDATE configuracao SET transbordo_habilitado=1,transbordo_url='https://destino.example',transbordo_nome='Amigo',transbordo_token='dest-secret'")

    def sem_capacidade(self):
        with test_db() as c:
            c.execute('UPDATE numeros SET ativo=0')

    def aceita(self, *args, **kwargs):
        return Mock(status_code=202,json=Mock(return_value={'recibo':kwargs['json']['id'],'status':'enfileirado'}))

    def entrada(self, **extra):
        return self.client.post('/disparar',json={**self.lead,**extra})

    def test_capacidade_local_nao_transborda(self):
        r=self.entrada()
        self.assertEqual(r.status_code,202)
        self.assertEqual(r.json()['status'],'enfileirado')
        self.api.assert_not_called()

    def test_encaminha_antes_de_cadastro_local_e_preserva_dados(self):
        self.sem_capacidade(); self.api.side_effect=self.aceita
        r=self.entrada().json()
        self.assertEqual(r['status'],'encaminhado')
        self.assertTrue(r['aceito'])
        self.api.assert_called_once()
        self.assertEqual(self.api.call_args.args[0],'https://destino.example/api/transbordo/receber')
        d=self.api.call_args.kwargs['json']['lead']
        self.assertEqual(d['cpf'],self.lead['cpf'])
        self.assertNotIn('nomeMae',d)
        self.assertFalse(self.fila.resumo()['contagens'])
        self.whatsapp.assert_not_called()

    def test_destino_recusa_sem_fila_e_sem_fallback(self):
        self.sem_capacidade(); self.api.return_value=Mock(status_code=409)
        r=self.entrada().json()
        self.assertEqual(r['status'],'recusado'); self.assertFalse(r['aceito'])
        self.assertFalse(self.fila.resumo()['contagens'])
        self.whatsapp.assert_not_called()

    def test_timeout_nao_reenvia_mesmo_quando_capacidade_local_volta(self):
        self.sem_capacidade(); self.api.side_effect=requests.Timeout()
        a=self.entrada().json()
        self.assertEqual(a['status'],'pendente'); self.assertIsNone(a['aceito'])
        with test_db() as c: c.execute('UPDATE numeros SET ativo=1')
        b=self.entrada().json()
        self.assertEqual(a['transbordo_id'],b['transbordo_id'])
        self.api.assert_called_once(); self.whatsapp.assert_not_called()
        self.assertFalse(self.fila.resumo()['contagens'])

    def test_idempotencia_e_conflito(self):
        self.sem_capacidade(); self.api.side_effect=self.aceita
        a=self.entrada(idempotency_key='exec-1').json()
        b=self.entrada(idempotency_key='exec-1').json()
        self.assertEqual(a['transbordo_id'],b['transbordo_id'])
        self.assertEqual(self.entrada(idempotency_key='exec-1',nome='Outro').status_code,409)
        self.api.assert_called_once()

    def test_retry_de_item_ja_local_nao_muda_para_amigo(self):
        a=self.entrada().json()
        self.sem_capacidade()
        b=self.entrada().json()
        self.assertEqual(a['fila_id'],b['fila_id'])
        self.api.assert_not_called()

    def test_nao_transborda_cpf_invalido_ou_lote(self):
        self.sem_capacidade()
        self.assertEqual(self.entrada(cpf='123').status_code,400)
        self.client.post('/api/disparo-lote',json={'leads':[self.lead]})
        self.api.assert_not_called()

    def test_fora_horario_encaminha_sem_fila_local(self):
        self.api.side_effect=self.aceita
        with patch.object(main,'dentro_do_horario_permitido',return_value=(False,'Fechado')):
            self.assertEqual(self.entrada().json()['status'],'encaminhado')
        self.api.assert_called_once()
        self.assertFalse(self.fila.resumo()['contagens'])
        self.whatsapp.assert_not_called()

    def test_falha_vendeai_local_nao_transborda(self):
        with test_db() as c:
            c.execute("UPDATE configuracao SET fila_habilitada=0,vendeai_falha_modo='bloquear'")
        self.api.return_value=Mock(status_code=429,headers={'Retry-After':'100'})
        self.entrada()
        self.api.assert_called_once()
        self.assertIn('mailing/leads',self.api.call_args.args[0])
        self.assertFalse(transbordo.Transbordo(main.get_db).listar())

    def test_recebedor_exige_chave_e_nao_reencaminha(self):
        self.sem_capacidade()
        body={'id':'a'*32,'lead':self.lead}
        self.assertEqual(self.client.post('/api/transbordo/receber',json=body).status_code,401)
        with patch.object(main,'API_KEY','dest-key'):
            r=self.client.post('/api/transbordo/receber',json=body,headers={'X-API-Key':'dest-key'})
        self.assertEqual(r.status_code,409)
        self.api.assert_not_called()

    def test_recebedor_idempotente_e_consulta_recibo(self):
        body={'id':'b'*32,'lead':self.lead}
        with patch.object(main,'API_KEY','dest-key'):
            h={'X-API-Key':'dest-key'}
            a=self.client.post('/api/transbordo/receber',json=body,headers=h)
            b=self.client.post('/api/transbordo/receber',json=body,headers=h)
            self.assertEqual(a.status_code,202)
            self.assertEqual(a.json()['fila_id'],b.json()['fila_id'])
            r=self.client.get('/api/transbordo/recebidos/'+'b'*32,headers=h)
            self.assertEqual(r.json()['status'],'aguardando_cadastro')
        self.assertEqual(self.fila.resumo()['contagens']['aguardando_cadastro'],1)
        self.api.assert_not_called()

    def test_recebedor_sem_vendeai_configurada_recusa(self):
        with test_db() as c: c.execute('UPDATE configuracao SET vendeai_habilitado=0')
        with patch.object(main,'API_KEY','dest-key'):
            r=self.client.post('/api/transbordo/receber',json={'id':'b'*32,'lead':self.lead},headers={'X-API-Key':'dest-key'})
        self.assertEqual(r.status_code,409)
        self.assertFalse(self.fila.resumo()['contagens'])

    def test_configuracao_mascara_token_e_valida_destino(self):
        r=self.client.get('/api/configuracao')
        self.assertNotIn('dest-secret',r.text)
        self.assertTrue(r.json()['transbordo_token_configurado'])
        for url in ('http://destino.example','https://testserver','https://destino.example/path','https://user:senha@destino.example'):
            self.assertEqual(self.client.patch('/api/configuracao',json={'transbordo_url':url}).status_code,400)
        self.assertEqual(self.client.patch('/api/configuracao',json={'transbordo_token':'','transbordo_nome':'Amigo'}).status_code,200)
        self.assertEqual(main.carregar_configuracao()['transbordo_token'],'dest-secret')

    def test_consulta_resolve_timeout_sem_novo_post(self):
        self.sem_capacidade(); self.api.side_effect=requests.Timeout()
        a=self.entrada().json(); id=a['transbordo_id']
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'recibo':id,'status':'sucesso'}))):
            r=self.client.get('/api/transbordo/'+id)
        self.assertEqual(r.json()['status'],'sucesso')
        self.api.assert_called_once()

    def test_prechecagem_consulta_destino_sem_encaminhar(self):
        self.sem_capacidade()
        with patch.object(main.requests,'get',return_value=Mock(status_code=200,json=Mock(return_value={'disponivel':True}))):
            r=self.client.get('/api/pode-disparar').json()
        self.assertTrue(r['pode_disparar']); self.assertEqual(r['canal_provavel'],'transbordo')
        self.api.assert_not_called()

    def test_redirecionamento_nao_envia_token_nem_lead_a_outro_host(self):
        self.sem_capacidade(); self.api.return_value=Mock(status_code=302)
        r=self.entrada().json()
        self.assertEqual(r['status'],'pendente')
        self.assertFalse(self.api.call_args.kwargs['allow_redirects'])

    def test_token_n8n_consulta_apenas_recibo_e_nao_configuracoes(self):
        self.sem_capacidade(); self.api.side_effect=self.aceita
        id=self.entrada().json()['transbordo_id']
        self.client.cookies.clear()
        with patch.object(main,'API_KEY','source-key'), patch.object(main.requests,'get',side_effect=requests.Timeout()):
            self.assertEqual(self.client.get('/api/transbordo/'+id,headers={'X-API-Key':'source-key'}).status_code,200)
            self.assertEqual(self.client.get('/api/transbordos',headers={'X-API-Key':'source-key'}).status_code,401)

    def test_dois_bancos_destino_usa_sua_conta_e_seus_padroes(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from fastapi.testclient import TestClient
        self.sem_capacidade()
        with test_db() as c:
            c.execute("UPDATE configuracao SET vendeai_campanha_padrao='ORIGEM'")
        with TemporaryDirectory() as folder:
            destino=Path(folder)/'destino.db'
            with patch.object(main,'DB_FILE',destino):
                main.init_db()
                with test_db() as c:
                    c.execute("UPDATE configuracao SET vendeai_habilitado=1,fila_habilitada=1,vendeai_account_id='CONTA-AMIGO',vendeai_mailing_token='crm-amigo',vendeai_campanha_padrao='DESTINO'")
                    c.execute("INSERT INTO numeros(nome,phone_number_id,access_token) VALUES('Amigo','fake','fake')")
                    c.execute("UPDATE numeros SET qualidade='GREEN',status_envio_meta='AVAILABLE' WHERE id=last_insert_rowid()")
            def destino_http(url,**kw):
                with patch.object(main,'DB_FILE',destino), patch.object(main,'API_KEY','dest-secret'):
                    cliente=TestClient(main.app)
                    try:
                        return cliente.post('/api/transbordo/receber',json=kw['json'],headers=kw['headers'])
                    finally:
                        cliente.close()
            self.api.side_effect=destino_http
            lead={k:v for k,v in self.lead.items() if k!='campanha'}
            r=self.client.post('/disparar',json=lead).json()
            self.assertEqual(r['status'],'encaminhado')
            self.assertFalse(self.fila.resumo()['contagens'])
            with patch.object(main,'DB_FILE',destino), test_db() as c:
                import json
                row=c.execute('SELECT * FROM fila_disparos').fetchone()
                self.assertTrue(row['destino'].endswith('|CONTA-AMIGO'))
                self.assertEqual(json.loads(row['payload'])['mailing']['campaign'],'DESTINO')


class RecebidosTests(TransbordoTests):
    def receber(self):
        with patch.object(main,'API_KEY','dest-key'):
            return self.client.post('/api/transbordo/receber',json={'id':'d'*32,'lead':self.lead},headers={'X-API-Key':'dest-key'})

    def test_recusa_visivel_sem_enfileirar(self):
        self.sem_capacidade()
        self.assertEqual(self.receber().status_code,409)
        itens=self.client.get('/api/transbordos/recebidos').json()['itens']
        self.assertEqual(itens[0]['status'],'recusado')
        self.assertEqual(itens[0]['motivo'],'nenhum_numero_disponivel')
        self.assertFalse(self.fila.resumo()['contagens'])

    def test_aceito_acompanha_fila_e_duplicado_nao_reenvia(self):
        a=self.receber()
        self.assertEqual(a.status_code,202)
        self.assertEqual(self.receber().status_code,200)
        with test_db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM fila_disparos').fetchone()[0],1)
            c.execute("UPDATE fila_disparos SET status='sucesso'")
        itens=self.client.get('/api/transbordos/recebidos').json()['itens']
        self.assertEqual(len(itens),2)
        self.assertTrue(all(i['status']=='sucesso' for i in itens))

    def test_sem_configuracao_visivel(self):
        with test_db() as c: c.execute('UPDATE configuracao SET vendeai_habilitado=0')
        self.receber()
        self.assertEqual(transbordo.listar_recebidos(main.get_db)[0]['motivo'],'destino_nao_configurado')

    def test_sem_autenticacao_nao_registra(self):
        self.client.post('/api/transbordo/receber',json={'id':'d'*32,'lead':self.lead})
        self.assertEqual(transbordo.listar_recebidos(main.get_db),[])

    def test_entrada_ocupada_visivel(self):
        from contextlib import contextmanager
        @contextmanager
        def ocupada(*args): yield False
        with patch.object(main.fila_disparos,'trava_worker',ocupada):
            self.assertEqual(self.receber().status_code,409)
        self.assertEqual(transbordo.listar_recebidos(main.get_db)[0]['motivo'],'entrada_em_andamento')

    def test_lote_em_andamento_registra_recusa(self):
        with patch.object(main,'estado_lote',return_value={'lote':True}):
            self.assertEqual(self.receber().status_code,409)
        self.assertEqual(transbordo.listar_recebidos(main.get_db)[0]['motivo'],'lote_em_andamento')

    def test_erro_posterior_exibe_motivo(self):
        self.receber()
        with test_db() as c:
            c.execute("UPDATE fila_disparos SET status='erro',resultado=?", ('{"motivo_codigo":"nenhum_numero_disponivel"}',))
        item=transbordo.listar_recebidos(main.get_db)[0]
        self.assertEqual(item['status'],'erro')
        self.assertEqual(item['motivo'],'nenhum_numero_disponivel')
