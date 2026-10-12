import optout
"""Encaminhamento com recibo persistente, sem repetir POST de resultado incerto."""
import hashlib
import json
import re
import time
import uuid
from contextlib import closing
from datetime import datetime, date
from urllib.parse import urlparse

import requests
from fastapi import HTTPException

CONFIG_COLUMNS = {
    'disparo_compartilhado': 'INTEGER NOT NULL DEFAULT 0',
    'transbordo_habilitado': 'INTEGER NOT NULL DEFAULT 0',
    'transbordo_url': "TEXT NOT NULL DEFAULT ''",
    'transbordo_nome': "TEXT NOT NULL DEFAULT ''",
    'transbordo_token': "TEXT NOT NULL DEFAULT ''",
}


def migrar(c):
    colunas = {r['name'] for r in c.execute('PRAGMA table_info(configuracao)')}
    for k, v in CONFIG_COLUMNS.items():
        if k not in colunas:
            c.execute(f'ALTER TABLE configuracao ADD COLUMN {k} {v}')
    c.execute('''CREATE TABLE IF NOT EXISTS transbordos (
        id TEXT PRIMARY KEY, chave TEXT NOT NULL, fingerprint TEXT NOT NULL,
        destino TEXT NOT NULL, destino_nome TEXT NOT NULL, nome TEXT NOT NULL,
        telefone TEXT NOT NULL, status TEXT NOT NULL, status_destino TEXT NOT NULL DEFAULT '',
        motivo TEXT NOT NULL DEFAULT '', criado_em REAL NOT NULL, atualizado_em REAL NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS disparos_compartilhados(id TEXT PRIMARY KEY, disparo_id INTEGER UNIQUE, destino TEXT NOT NULL, payload TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'pendente', criado_em REAL NOT NULL, resultado TEXT NOT NULL DEFAULT '{}')''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_transbordo_chave ON transbordos(chave,criado_em)')
    c.execute('''CREATE TABLE IF NOT EXISTS transbordos_recebidos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recibo TEXT NOT NULL,
        nome TEXT NOT NULL, telefone TEXT NOT NULL, criado_em REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'recebido', motivo TEXT NOT NULL DEFAULT '',
        fila_id TEXT NOT NULL DEFAULT '')''')


def registrar_recebido(get_db, recibo, lead):
    def texto(*campos):
        return next((str(lead[k])[:200] for k in campos if isinstance(lead.get(k), (str, int))), '')
    with closing(get_db()) as c, c:
        return c.execute('INSERT INTO transbordos_recebidos(recibo,nome,telefone,criado_em) VALUES(?,?,?,?)',
                         (recibo,texto('nome','name'),texto('whatsapp','telefone','phone'),time.time())).lastrowid


def finalizar_recebido(get_db, id, status, motivo='', fila_id=''):
    with closing(get_db()) as c, c:
        c.execute('UPDATE transbordos_recebidos SET status=?,motivo=?,fila_id=? WHERE id=?', (status,motivo,fila_id,id))


def listar_recebidos(get_db):
    with closing(get_db()) as c:
        rows=c.execute('''SELECT r.*, f.status AS estado_fila, f.resultado, f.cadastro FROM transbordos_recebidos r
            LEFT JOIN fila_disparos f ON f.id=r.fila_id ORDER BY r.id DESC LIMIT 50''').fetchall()
    itens=[]
    for r in rows:
        item={k:r[k] for k in ('id','recibo','nome','telefone','criado_em','motivo','fila_id')}
        item['status']=r['estado_fila'] or r['status']
        if item['status']=='erro':
            resultado=json.loads(r['resultado'] or '{}')
            cadastro=json.loads(r['cadastro'] or '{}')
            item['motivo']=resultado.get('motivo_codigo') or cadastro.get('motivo') or 'falha_no_envio'
        itens.append(item)
    return itens


def validar_url(valor):
    try:
        u = urlparse(valor)
        porta = u.port
    except ValueError:
        raise HTTPException(400, 'URL do destino invalida')
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('', '/') or porta not in (None, 443):
        raise HTTPException(400, 'Destino deve ser uma URL base HTTPS, sem caminho, porta ou credenciais')
    return valor.rstrip('/')


def validar_lead(m):
    if not re.fullmatch(r'55[1-9][0-9][0-9]{8,9}', m['phone']):
        raise HTTPException(400, 'Telefone invalido para transbordo')
    if m.get('cpf'):
        cpf = re.sub(r'\D', '', m['cpf'])
        if len(cpf) != 11 or len(set(cpf)) == 1 or any(int(cpf[k]) != (sum(int(cpf[i]) * (k+1-i) for i in range(k)) * 10 % 11) % 10 for k in (9,10)):
            raise HTTPException(400, 'CPF invalido para transbordo')
    if m.get('birth_date'):
        try:
            d = datetime.strptime(m['birth_date'], '%d/%m/%Y').date()
            if d > date.today() or (date.today()-d).days > 120*366:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, 'Nascimento invalido para transbordo')
    if m.get('email') and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', m['email']):
        raise HTTPException(400, 'Email invalido para transbordo')


def dados_lead(body):
    campos = ('whatsapp','telefone','phone','cpf','nome','name','nascimento','birth_date','email','produto','product','campanha','campaign','variaveis_template')
    lead = {k: body[k] for k in campos if k in body}
    if body.get('info_extra') and not isinstance(body['info_extra'],dict):
        raise HTTPException(400,'info_extra deve ser objeto')
    if body.get('info_extra'):
        lead['info_extra'] = {k: v for k,v in body['info_extra'].items() if k in ('email','nascimento','birth_date')}
    return lead


def identidade(body, chave):
    if chave is not None and (not isinstance(chave,str) or not 1 <= len(chave.strip()) <= 200):
        raise HTTPException(400, 'Idempotency-Key deve ter de 1 a 200 caracteres')
    fp = hashlib.sha256(json.dumps(dados_lead(body),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return ('exp:' + hashlib.sha256(chave.strip().encode()).hexdigest() if chave else 'auto:'+fp), fp


class Transbordo:
    def __init__(self, get_db):
        self.get_db = get_db

    def buscar(self, body, chave):
        key, fp = identidade(body, chave)
        with closing(self.get_db()) as c:
            r = c.execute("SELECT * FROM transbordos WHERE chave=? AND (criado_em>=? OR chave LIKE 'exp:%' OR status IN ('enviando','incerto','encaminhado')) ORDER BY criado_em DESC LIMIT 1", (key,time.time()-86400)).fetchone()
        if r and r['fingerprint'] != fp:
            raise HTTPException(409, 'Idempotency-Key ja usada com dados diferentes')
        return dict(r) if r else None

    def obter(self, id):
        with closing(self.get_db()) as c:
            r = c.execute('SELECT * FROM transbordos WHERE id=?',(id,)).fetchone()
        if not r:
            raise HTTPException(404, 'Transbordo nao encontrado')
        return dict(r)

    def listar(self):
        with closing(self.get_db()) as c:
            return [self.publico(dict(r)) for r in c.execute('SELECT * FROM transbordos ORDER BY criado_em DESC LIMIT 50')]

    @staticmethod
    def publico(r):
        incerto = r['status'] in ('enviando','incerto')
        return {'status': 'pendente' if incerto else r['status'],
                'aceito': None if incerto else r['status'] not in ('recusado',),
                'transbordo_id':r['id'], 'destino':r['destino_nome'] or r['destino'],
                'nome':r['nome'], 'telefone':r['telefone'], 'status_destino':r['status_destino'],
                'motivo_codigo':r['motivo'], 'criado_em':r['criado_em'],
                'consultar_em':'/api/transbordo/'+r['id']}

    def alterar(self,id,status,motivo='',status_destino=''):
        with closing(self.get_db()) as c, c:
            c.execute('UPDATE transbordos SET status=?,motivo=?,status_destino=?,atualizado_em=? WHERE id=?',(status,motivo,status_destino,time.time(),id))
        return self.obter(id)

    def encaminhar(self, body, chave, cfg, mailing):
        if optout.bloqueado(self.get_db,mailing.get('phone')):
            return {'status':'bloqueado','aceito':False,'motivo_codigo':'optout'}
        anterior = self.buscar(body,chave)
        if anterior:
            return self.publico(anterior)
        key, fp = identidade(body,chave)
        id = uuid.uuid4().hex
        with closing(self.get_db()) as c, c:
            c.execute('INSERT INTO transbordos(id,chave,fingerprint,destino,destino_nome,nome,telefone,status,criado_em,atualizado_em) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (id,key,fp,cfg['transbordo_url'],cfg['transbordo_nome'],mailing.get('name',''),mailing['phone'],'enviando',time.time(),time.time()))
        try:
            r = requests.post(cfg['transbordo_url']+'/api/transbordo/receber',
                              json={'id':id,'lead':dados_lead(body)},
                              headers={'X-API-Key':cfg['transbordo_token']},timeout=(5,20),allow_redirects=False)
            if r.status_code in (200,202):
                d = r.json()
                if d.get('recibo') != id or d.get('status') not in ('enfileirado','sucesso','erro','cancelado','incerto'):
                    raise ValueError()
                status = 'encaminhado' if d['status']=='enfileirado' else d['status']
                resultado = self.alterar(id,status,status_destino=d['status'])
            elif r.status_code in (400,401,403,409,422,429):
                resultado = self.alterar(id,'recusado','destino_recusou_'+str(r.status_code))
            else:
                resultado = self.alterar(id,'incerto','transbordo_sem_confirmacao')
        except (requests.RequestException,ValueError,TypeError,AttributeError):
            resultado = self.alterar(id,'incerto','transbordo_sem_confirmacao')
        return self.publico(resultado)

    def consultar_destino(self,id,cfg):
        row = self.obter(id)
        if row['status'] not in ('enviando','incerto','encaminhado') or row['destino'] != cfg['transbordo_url'] or not cfg['transbordo_token']:
            return self.publico(row)
        try:
            r = requests.get(row['destino']+'/api/transbordo/recebidos/'+id,headers={'X-API-Key':cfg['transbordo_token']},timeout=(5,10),allow_redirects=False)
            if r.status_code == 200:
                d = r.json()
                if d.get('recibo') == id and d.get('status') in ('aguardando_cadastro','cadastrando','pronto','enviando','sucesso','erro','cancelado','incerto'):
                    status = 'encaminhado' if d['status'] in ('aguardando_cadastro','cadastrando','pronto','enviando') else d['status']
                    row = self.alterar(id,status,status_destino=d['status'])
        except (requests.RequestException,ValueError,TypeError,AttributeError):
            pass
        return self.publico(row)


def agendar_compartilhado(c, disparo_id, preparado):
    if not preparado or preparado.get('_nao_compartilhar'):return
    cfg=dict(c.execute('SELECT * FROM configuracao WHERE id=1').fetchone())
    if str(cfg.get('vendeai_account_id'))!='33393' or not cfg.get('disparo_compartilhado'):return
    if not cfg.get('transbordo_url') or not cfg.get('transbordo_token'):return
    lead=preparado['mailing']
    body={k:v for k,v in lead.items() if k in ('phone','cpf','name','birth_date','email','product','campaign')}
    body['variaveis_template']=preparado.get('variaveis_template',[])
    c.execute('INSERT OR IGNORE INTO disparos_compartilhados(id,disparo_id,destino,payload,criado_em) VALUES(?,?,?,?,?)',
              (uuid.uuid4().hex,disparo_id,cfg['transbordo_url'],json.dumps(body,ensure_ascii=False),time.time()))


def recuperar_compartilhados(get_db):
    with closing(get_db()) as c,c:
        c.execute("UPDATE disparos_compartilhados SET estado='incerto' WHERE estado='enviando'")


def processar_compartilhado(get_db,cfg):
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute("SELECT * FROM disparos_compartilhados WHERE estado='pendente' ORDER BY criado_em LIMIT 1").fetchone()
        if not r:return False
        item=dict(r)
        permitido=(str(cfg.get('vendeai_account_id'))=='33393' and cfg.get('disparo_compartilhado') and cfg.get('transbordo_token') and cfg.get('transbordo_url')==item['destino'] and time.time()-item['criado_em']<=300)
        c.execute('UPDATE disparos_compartilhados SET estado=? WHERE id=?',('enviando' if permitido else 'cancelado',item['id']))
    if not permitido:return True
    body=json.loads(item['payload'])
    try:
        validar_lead(body)
        result=Transbordo(get_db).encaminhar(body,'compartilhado:'+item['id'],cfg,body)
    except Exception:
        # Sem repetir automaticamente um envio cujo resultado nao foi confirmado.
        result={'status':'incerto','motivo_codigo':'compartilhamento_sem_confirmacao'}
    with closing(get_db()) as c,c:
        c.execute('UPDATE disparos_compartilhados SET estado=?,resultado=? WHERE id=?',
                  ('incerto' if result['status']=='pendente' else result['status'],json.dumps(result,ensure_ascii=False),item['id']))
    return True
