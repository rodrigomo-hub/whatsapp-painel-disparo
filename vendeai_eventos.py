"""Recepção durável dos eventos VendeAI, independente do processamento no CRM."""
import hashlib
import json
import secrets
import re
import math
import requests
import logging
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import HTTPException


def migrar(c):
    c.execute('''CREATE TABLE IF NOT EXISTS vendeai_webhook_config (
        id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL,
        new_habilitado INTEGER NOT NULL DEFAULT 0)''')
    cols={r['name'] for r in c.execute('PRAGMA table_info(vendeai_webhook_config)')}
    if 'new_config' not in cols:c.execute("ALTER TABLE vendeai_webhook_config ADD COLUMN new_config TEXT NOT NULL DEFAULT '{}'")
    c.execute('INSERT OR IGNORE INTO vendeai_webhook_config(id,token) VALUES(1,?)',(secrets.token_urlsafe(32),))
    c.execute('''CREATE TABLE IF NOT EXISTS vendeai_eventos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recebido_em TEXT NOT NULL,
        evento TEXT NOT NULL, account_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        produto TEXT NOT NULL, banco TEXT NOT NULL, proposta_id TEXT NOT NULL,
        payload_raw BLOB NOT NULL, fingerprint TEXT NOT NULL, repetido_de INTEGER)''')
    c.execute('CREATE INDEX IF NOT EXISTS vendeai_eventos_hash ON vendeai_eventos(fingerprint)')
    c.execute('''CREATE TABLE IF NOT EXISTS vendeai_new_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, evento_id INTEGER NOT NULL,
        account_id TEXT NOT NULL, proposta_id TEXT NOT NULL,
        estado TEXT NOT NULL DEFAULT 'AGUARDANDO_CONFIGURACAO',
        detalhe TEXT NOT NULL DEFAULT '', criado_em TEXT NOT NULL,
        UNIQUE(account_id,proposta_id))''')
    cols={r['name'] for r in c.execute('PRAGMA table_info(vendeai_new_outbox)')}
    for campo,tipo in [('http_status','INTEGER'),('resposta','TEXT'),('enviado_em','TEXT')]:
        if campo not in cols:c.execute(f'ALTER TABLE vendeai_new_outbox ADD COLUMN {campo} {tipo}')


def objeto(v):
    return v if isinstance(v,dict) else {}


def receber(get_db,token,raw,conta):
    with closing(get_db()) as c:
        cfg=c.execute('SELECT token FROM vendeai_webhook_config WHERE id=1').fetchone()
    if not cfg or not secrets.compare_digest(str(token),cfg['token']):
        raise HTTPException(401,'Credencial inválida.')
    if len(raw)>2_000_000:raise HTTPException(413,'Evento acima do limite de 2 MB.')
    try:d=json.loads(raw)
    except (ValueError,UnicodeDecodeError):raise HTTPException(400,'JSON inválido.')
    if not isinstance(d,dict):raise HTTPException(400,'O evento deve ser um objeto JSON.')
    chat=objeto(d.get('chat_summary'));p=objeto(d.get('proposal'))
    account=str(chat.get('account_id') or '')
    if conta and account!=str(conta):raise HTTPException(403,'Evento de outra conta.')
    evento=str(d.get('event') or '')
    produto=str(chat.get('product') or '')
    banco=str(p.get('bank') or '')
    proposta=str(p.get('proposal_id') or objeto(d.get('proposal_status')).get('proposal_id') or '')
    fingerprint=hashlib.sha256(json.dumps(d,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    agora=datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat()
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        anterior=c.execute('SELECT id FROM vendeai_eventos WHERE fingerprint=? ORDER BY id LIMIT 1',(fingerprint,)).fetchone()
        cursor=c.execute('INSERT INTO vendeai_eventos(recebido_em,evento,account_id,chat_id,produto,banco,proposta_id,payload_raw,fingerprint,repetido_de) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (agora,evento,account,str(chat.get('chat_id') or ''),produto,banco,proposta,raw,fingerprint,anterior['id'] if anterior else None))
        if evento=='proposal_created' and produto=='clt' and banco=='facta' and proposta:
            c.execute("INSERT INTO vendeai_new_outbox(evento_id,account_id,proposta_id,criado_em,estado) VALUES(?,?,?,?,'PENDENTE') ON CONFLICT(account_id,proposta_id) DO UPDATE SET evento_id=excluded.evento_id,estado='PENDENTE' WHERE vendeai_new_outbox.estado='DADOS_INCOMPLETOS'",(cursor.lastrowid,account,proposta,agora))
    return {'ok':True}


def listar(get_db):
    with closing(get_db()) as c:
        rows=c.execute('''SELECT e.id,e.recebido_em,e.evento,e.account_id,e.chat_id,e.produto,e.banco,e.proposta_id,e.repetido_de,
            o.estado as new_estado FROM vendeai_eventos e LEFT JOIN vendeai_new_outbox o ON o.account_id=e.account_id AND o.proposta_id=e.proposta_id
            ORDER BY e.id DESC LIMIT 100''').fetchall()
        total=c.execute('SELECT count(*) FROM vendeai_eventos').fetchone()[0]
    return {'total':total,'eventos':[dict(r) for r in rows]}


class OcultarChaveWebhook(logging.Filter):
    def filter(self, record):
        if isinstance(record.args,tuple):
            record.args=tuple(re.sub(r'(/webhook/vendeai-eventos)\?[^ ]*',r'\1?credencial=oculta',v) if isinstance(v,str) else v for v in record.args)
        return True


logging.getLogger('uvicorn.access').addFilter(OcultarChaveWebhook())


def montar_new(config,event):
    def resolver(v):
        if isinstance(v,dict):return {k:resolver(x) for k,x in v.items()}
        if isinstance(v,list):return [resolver(x) for x in v]
        if not isinstance(v,str) or v not in config['campos']:return v
        regra=config['campos'][v];d=event
        for k in regra['path']:d=d.get(k) if isinstance(d,dict) else None
        if d is None or d=='':raise ValueError('Campo ausente: '+'.'.join(regra['path']))
        return str(d).split(' - ')[0].strip() if regra['tabela'] else d
    payload=resolver(config['modelo'])
    contato=objeto(objeto(objeto(event.get('chat_summary')).get('details')).get('contact'))
    phone=re.sub(r'\D','',str(contato.get('phone') or ''))
    if len(phone) in (12,13) and phone.startswith('55'):phone=phone[2:]
    if len(phone) not in (10,11) or phone[:2].startswith('0'):
        raise ValueError('Telefone real do webhook ausente ou inválido.')
    ddd,numero=phone[:2],phone[2:]
    payload['content']['cliente']['telefones']={numero:{'ddd':ddd,'numero':numero}}
    proposta=payload['content']['proposta'];proposta['telefone_id']=numero
    for k in ('valor_parcela','valor_financiado','valor_liberado','prazo'):
        v=proposta.get(k)
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0:
            raise ValueError('Valor inválido: '+k)
    return payload


def processar_um(get_db):
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        cfg=c.execute('SELECT * FROM vendeai_webhook_config WHERE id=1').fetchone()
        if not cfg or not cfg['new_habilitado']:return
        config=json.loads(cfg['new_config'])
        if config.get('url')!='https://api.newcorban.com.br/api/propostas/':return
        item=c.execute("SELECT o.*,e.payload_raw FROM vendeai_new_outbox o JOIN vendeai_eventos e ON e.id=o.evento_id WHERE o.estado='PENDENTE' ORDER BY o.id LIMIT 1").fetchone()
        if not item:return
        c.execute("UPDATE vendeai_new_outbox SET estado='ENVIANDO' WHERE id=?",(item['id'],))
    status=None;resposta='';enviado=None
    try:
        payload=montar_new(config,json.loads(item['payload_raw']))
    except (ValueError,KeyError,TypeError):
        estado,detalhe='DADOS_INCOMPLETOS','Faltam dados válidos para o New. Confira o JSON recebido, especialmente telefone, identificação e valores da proposta.'
    else:
        try:
            r=requests.post(config['url'],json=payload,headers={'Accept':'application/json'},timeout=(5,30),allow_redirects=False)
            status=r.status_code;resposta=r.text[:30000]
            for valor in config.get('modelo',{}).get('auth',{}).values():
                if isinstance(valor,str) and valor:resposta=resposta.replace(valor,'[oculto]')
            enviado=datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat()
            if 200<=status<300:
                estado,detalhe='ENVIADO_HTTP','New respondeu HTTP '+str(status)+'. Confira a resposta para a confirmação do cadastro.'
            elif 400<=status<500:
                estado,detalhe='RECUSADO','New recusou a solicitação: HTTP '+str(status)+'.'
            else:estado,detalhe='INCERTO','Sem confirmação do cadastro. Não haverá reenvio automático.'
        except requests.RequestException:
            estado,detalhe='INCERTO','Falha de comunicação: o New pode ter recebido. Confira antes de reenviar.'
    with closing(get_db()) as c,c:
        c.execute('UPDATE vendeai_new_outbox SET estado=?,detalhe=?,http_status=?,resposta=?,enviado_em=? WHERE id=?',(estado,detalhe,status,resposta,enviado,item['id']))


def recuperar_interrompidos(get_db):
    with closing(get_db()) as c,c:
        c.execute("UPDATE vendeai_new_outbox SET estado='INCERTO',detalhe='Envio interrompido por reinício. Confira no New; não reenviado automaticamente.' WHERE estado='ENVIANDO'")
