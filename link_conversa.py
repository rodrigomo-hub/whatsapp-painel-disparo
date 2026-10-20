"""Teste opt-in do painel de origem: uma mensagem por nova conversa."""
import json
import time
import re
import requests
import optout
from contextlib import closing
from fastapi import HTTPException

CONTA='33393'
TEXTO='*Você também pode consultar sua oferta e seguir a contratação pelo link* 👇\nhttps://simular.vendeaitecnologia.com.br/s/4mOoz7'


def migrar(c):
    c.execute('CREATE TABLE IF NOT EXISTS link_conversa_config(id INTEGER PRIMARY KEY CHECK(id=1),ativo INTEGER NOT NULL DEFAULT 0)')
    if 'texto' not in {r[1] for r in c.execute('PRAGMA table_info(link_conversa_config)')}:
        c.execute("ALTER TABLE link_conversa_config ADD COLUMN texto TEXT")
    c.execute('INSERT OR IGNORE INTO link_conversa_config(id,ativo) VALUES(1,0)')
    c.execute('''CREATE TABLE IF NOT EXISTS link_conversa_envios(
        account_id TEXT NOT NULL,chat_id TEXT NOT NULL,telefone TEXT NOT NULL,
        evento_id INTEGER NOT NULL,criado_em REAL NOT NULL,estado TEXT NOT NULL DEFAULT 'PENDENTE',
        http_status INTEGER,message_id TEXT,finalizado_em REAL,
        PRIMARY KEY(account_id,chat_id))''')


def impedido(c,chat,telefone):
    return bool(c.execute('SELECT 1 FROM optout WHERE telefone=?',(telefone,)).fetchone() or
                c.execute('SELECT 1 FROM optout_tarefas WHERE account_id=? AND chat_id=?',(CONTA,chat)).fetchone())


def agendar(c,d,evento_id):
    chat=d.get('chat_summary') or {}
    if d.get('event')!='conversation_created' or str(chat.get('account_id'))!=CONTA:return
    if not c.execute('SELECT ativo FROM link_conversa_config WHERE id=1').fetchone()[0]:return
    ident=str(chat.get('chat_id') or '')
    detalhes=chat.get('details') or {}
    if not isinstance(detalhes,dict):return
    contato=detalhes.get('contact') or {}
    if not isinstance(contato,dict):return
    telefone=optout.telefone(contato.get('phone'))
    if not ident.isdigit() or not re.fullmatch(r'55[1-9][0-9][0-9]{8,9}',telefone) or impedido(c,ident,telefone):return
    cfg=c.execute('SELECT textos FROM optout_config WHERE id=1').fetchone()
    textos=json.loads(cfg['textos']) if cfg else []
    if optout.normalizar(d.get('first_message')) in {optout.normalizar(t) for t in textos+['Não quero mensagens']}:return
    # Não transformar reentrega de evento antigo em mensagem nova ao ativar o teste.
    if c.execute("SELECT 1 FROM vendeai_eventos WHERE account_id=? AND chat_id=? AND evento='conversation_created' AND id<?",(CONTA,ident,evento_id)).fetchone():return
    c.execute('INSERT OR IGNORE INTO link_conversa_envios(account_id,chat_id,telefone,evento_id,criado_em) VALUES(?,?,?,?,?)',
              (CONTA,ident,telefone,evento_id,time.time()))


def configurar(get_db,conta,ativo=None,texto=None):
    if str(conta)!=CONTA:raise HTTPException(403,'Teste disponível somente no painel de origem.')
    if ativo is not None and type(ativo) is not bool:raise HTTPException(400,'Informe ativo como verdadeiro ou falso.')
    if texto is not None and (not isinstance(texto,str) or not 1<=len(texto.strip())<=4096):
        raise HTTPException(400,'Informe uma mensagem entre 1 e 4096 caracteres.')
    with closing(get_db()) as c,c:
        if texto is not None:c.execute('UPDATE link_conversa_config SET texto=? WHERE id=1',(texto.strip(),))
        if ativo is not None:c.execute('UPDATE link_conversa_config SET ativo=? WHERE id=1',(int(ativo),))
        if ativo is False:c.execute("UPDATE link_conversa_envios SET estado='CANCELADO',finalizado_em=? WHERE estado='PENDENTE'",(time.time(),))
    return {'ativo':ativo}


def resumo(get_db,conta):
    if str(conta)!=CONTA:return {'disponivel':False}
    with closing(get_db()) as c:
        return {'disponivel':True,'ativo':bool(c.execute('SELECT ativo FROM link_conversa_config WHERE id=1').fetchone()[0]),
                'texto':c.execute('SELECT texto FROM link_conversa_config WHERE id=1').fetchone()[0] or TEXTO,'envios':[dict(r) for r in c.execute('SELECT chat_id,estado,http_status,message_id,criado_em FROM link_conversa_envios ORDER BY criado_em DESC LIMIT 30')]}


def recuperar(get_db):
    with closing(get_db()) as c,c:
        c.execute("UPDATE link_conversa_envios SET estado='INCERTO',finalizado_em=? WHERE estado='ENVIANDO'",(time.time(),))


def processar_um(get_db,conta,token):
    if str(conta)!=CONTA or not token:return False
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT ativo FROM link_conversa_config WHERE id=1').fetchone()[0]:return False
        r=c.execute("SELECT * FROM link_conversa_envios WHERE estado='PENDENTE' ORDER BY criado_em LIMIT 1").fetchone()
        if not r:return False
        r=dict(r)
        estado='CANCELADO' if impedido(c,r['chat_id'],r['telefone']) else 'EXPIRADO' if time.time()-r['criado_em']>120 else 'ENVIANDO'
        c.execute('UPDATE link_conversa_envios SET estado=? WHERE account_id=? AND chat_id=?',(estado,CONTA,r['chat_id']))
    if estado!='ENVIANDO':return True
    # Reconfere a supressão imediatamente antes do único POST; não altera a IA.
    with closing(get_db()) as c:
        cancelar=impedido(c,r['chat_id'],r['telefone']) or not c.execute('SELECT ativo FROM link_conversa_config WHERE id=1').fetchone()[0]
        texto=c.execute('SELECT texto FROM link_conversa_config WHERE id=1').fetchone()[0] or TEXTO
    status=None;message_id=None
    if cancelar:estado='CANCELADO'
    else:
        try:
            resp=requests.post('https://crm.vendeaitecnologia.com.br/api/v1/accounts/'+CONTA+'/conversations/'+r['chat_id']+'/messages',
                headers={'api_access_token':token},json={'content':texto,'message_type':'outgoing','private':False,'content_type':'text'},
                timeout=(2,5),allow_redirects=False)
            status=resp.status_code
            if 200<=status<300:
                data=resp.json();message_id=str(data.get('id') or '')
                estado='ACEITO_CRM' if message_id else 'INCERTO'
            else:estado='RECUSADO' if 400<=status<500 else 'INCERTO'
        except (requests.RequestException,ValueError,AttributeError):estado='INCERTO'
    with closing(get_db()) as c,c:
        c.execute('UPDATE link_conversa_envios SET estado=?,http_status=?,message_id=?,finalizado_em=? WHERE account_id=? AND chat_id=?',
                  (estado,status,message_id,time.time(),CONTA,r['chat_id']))
    return True
