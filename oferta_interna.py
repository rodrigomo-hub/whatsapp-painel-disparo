"""Consulta a Esteira e registra uma oferta como nota privada, fora do webhook."""
import json
import re
import time
from contextlib import closing
import requests
import optout

BASE = 'https://app.esteiradeleads.shop'
CONTA = '33393'


def migrar(c):
    c.execute("""CREATE TABLE IF NOT EXISTS oferta_interna_config(
        id INTEGER PRIMARY KEY CHECK(id=1), ativo INTEGER NOT NULL DEFAULT 0,
        usuario TEXT NOT NULL DEFAULT '', senha TEXT NOT NULL DEFAULT '',
        token TEXT NOT NULL DEFAULT '')""")
    c.execute('INSERT OR IGNORE INTO oferta_interna_config(id) VALUES(1)')
    c.execute("""CREATE TABLE IF NOT EXISTS oferta_interna_notas(
        conta TEXT NOT NULL,chat TEXT NOT NULL,cpf TEXT NOT NULL,evento_id INTEGER NOT NULL,
        estado TEXT NOT NULL DEFAULT 'PENDENTE',tentativas INTEGER NOT NULL DEFAULT 0,
        tentar_em REAL NOT NULL DEFAULT 0,criado_em REAL NOT NULL,atualizado_em REAL NOT NULL,
        detalhe TEXT NOT NULL DEFAULT '',message_id TEXT,oferta TEXT,
        PRIMARY KEY(conta,chat,cpf))""")


def cpf(v):
    s=re.sub(r'\D','',str(v or ''))
    return s.zfill(11) if 0<len(s)<=11 else ''


def agendar(c,d,evento):
    cfg=c.execute('SELECT ativo FROM oferta_interna_config WHERE id=1').fetchone()
    chat=d.get('chat_summary') or {}; conta=str(chat.get('account_id') or '')
    ident=str(chat.get('chat_id') or '')
    contato=(chat.get('details') or {}).get('contact') or {}
    doc=cpf(contato.get('cpf'))
    if not cfg or not cfg['ativo'] or conta!=CONTA or not ident.isdigit() or not doc:return
    if impedido(c,conta,ident):return
    now=time.time()
    c.execute('INSERT OR IGNORE INTO oferta_interna_notas(conta,chat,cpf,evento_id,criado_em,atualizado_em) VALUES(?,?,?,?,?,?)',
              (conta,ident,doc,evento,now,now))


def impedido(c,conta,chat):
    return bool(c.execute('SELECT 1 FROM optout_tarefas WHERE account_id=? AND chat_id=?',(conta,chat)).fetchone())


def recuperar(get_db):
    with closing(get_db()) as c,c:
        c.execute("UPDATE oferta_interna_notas SET estado='PENDENTE' WHERE estado='CONSULTANDO'")
        c.execute("UPDATE oferta_interna_notas SET estado='INCERTO',detalhe='Envio interrompido; conferir nota antes de repetir.' WHERE estado='ENVIANDO'")


def autenticar(get_db,cfg):
    r=requests.post(BASE+'/auth/login',json={'username':cfg['usuario'],'password':cfg['senha']},timeout=(4,15),allow_redirects=False)
    if r.status_code!=200:raise ValueError('Falha de login na Esteira: HTTP '+str(r.status_code))
    token=r.json().get('access_token')
    if not isinstance(token,str) or not token:raise ValueError('Login sem token')
    with closing(get_db()) as c,c:c.execute('UPDATE oferta_interna_config SET token=? WHERE id=1',(token,))
    return token


def buscar(get_db,cfg,doc):
    token=cfg['token'] or autenticar(get_db,cfg)
    def consulta(t):
        return requests.get(BASE+'/leads',params={'busca':doc},headers={'Authorization':'Bearer '+t},timeout=(4,15),allow_redirects=False)
    r=consulta(token)
    if r.status_code==401:r=consulta(autenticar(get_db,cfg))
    if r.status_code!=200:raise ValueError('Consulta da Esteira: HTTP '+str(r.status_code))
    d=r.json()
    if not isinstance(d,dict) or not isinstance(d.get('items'),list):raise ValueError('Resposta de consulta invalida')
    # A busca e textual: exigir CPF exato, nunca selecionar por nome ou proximidade.
    if int(d.get('total_paginas') or 1)>1:raise ValueError('Busca retornou varias paginas; revisar correspondencia')
    encontrados=[x for x in d['items'] if isinstance(x,dict) and cpf(x.get('cpf'))==doc and not x.get('duplicado_de_id')]
    ofertas=[]
    for lead in encontrados:
        # Um registro em processamento nao invalida outra oferta pronta do mesmo CPF.
        # Se a API informa status, somente com_oferta pode fornecer a nota.
        if lead.get('status') and lead['status']!='com_oferta':continue
        resultado=lead.get('resultado') or {}
        candidatos=[lead.get('texto_oferta'),resultado.get('oferta') if isinstance(resultado,dict) else None]
        texto=next((v.strip() for v in candidatos if isinstance(v,str) and v.strip()),None)
        if texto:ofertas.append(texto)
    if not ofertas:raise ValueError('Oferta ainda indisponivel')
    if len(ofertas)!=1:raise ValueError('Mais de uma oferta valida; revisar correspondencia')
    return ofertas[0]



def finalizar(get_db,item,estado,detalhe='',message_id=None):
    with closing(get_db()) as c,c:
        c.execute('UPDATE oferta_interna_notas SET estado=?,detalhe=?,message_id=?,atualizado_em=?,tentar_em=? WHERE conta=? AND chat=? AND cpf=?',
                  (estado,detalhe,message_id,time.time(),time.time()+60*item['tentativas'],item['conta'],item['chat'],item['cpf']))


def processar_um(get_db,conta,token_crm):
    if str(conta)!=CONTA or not token_crm:return False
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        cfg=dict(c.execute('SELECT * FROM oferta_interna_config WHERE id=1').fetchone())
        if not cfg['ativo'] or not cfg['usuario'] or not cfg['senha']:return False
        r=c.execute("SELECT * FROM oferta_interna_notas WHERE conta=? AND estado='PENDENTE' AND tentar_em<=? ORDER BY criado_em LIMIT 1",(CONTA,time.time())).fetchone()
        if not r:return False
        item=dict(r);item['tentativas']+=1
        c.execute("UPDATE oferta_interna_notas SET estado='CONSULTANDO',tentativas=?,atualizado_em=? WHERE conta=? AND chat=? AND cpf=?",(item['tentativas'],time.time(),CONTA,item['chat'],item['cpf']))
    try:
        texto=buscar(get_db,cfg,item['cpf'])
    except (requests.RequestException,ValueError,TypeError,AttributeError):
        finalizar(get_db,item,'PENDENTE' if item['tentativas']<3 else 'REVISAR','Consulta sem oferta confirmada; nenhuma nota enviada.')
        return True
    with closing(get_db()) as c,c:
        if not c.execute('SELECT ativo FROM oferta_interna_config WHERE id=1').fetchone()[0] or impedido(c,CONTA,item['chat']):
            c.execute("UPDATE oferta_interna_notas SET estado='CANCELADO' WHERE conta=? AND chat=? AND cpf=?",(CONTA,item['chat'],item['cpf']))
            return True
        c.execute("UPDATE oferta_interna_notas SET estado='ENVIANDO',oferta=?,atualizado_em=? WHERE conta=? AND chat=? AND cpf=?",(texto,time.time(),CONTA,item['chat'],item['cpf']))
    try:
        r=requests.post('https://crm.vendeaitecnologia.com.br/api/v1/accounts/'+CONTA+'/conversations/'+item['chat']+'/messages',
            headers={'api_access_token':token_crm},json={'content':'Oferta consultada na Esteira de Leads\n\n'+texto,'message_type':'outgoing','private':True,'content_type':'text'},timeout=(4,15),allow_redirects=False)
        if 200<=r.status_code<300:
            d=r.json()
            if d.get('id') and d.get('private') is True:
                finalizar(get_db,item,'CONCLUIDO',message_id=str(d['id']))
            else:finalizar(get_db,item,'INCERTO','Nota sem confirmacao de privacidade; revisar no CRM.')
        elif r.status_code==404:finalizar(get_db,item,'CANCELADO','Conversa nao encontrada.')
        elif r.status_code==429:
            finalizar(get_db,item,'PENDENTE' if item['tentativas']<3 else 'REVISAR','CRM limitou a requisicao.')
        else:finalizar(get_db,item,'INCERTO' if r.status_code>=500 else 'REVISAR','CRM HTTP '+str(r.status_code))
    except (requests.RequestException,ValueError,TypeError,AttributeError):
        finalizar(get_db,item,'INCERTO','Resposta incerta; conferir nota antes de repetir.')
    return True
