"""Consulta a Esteira e registra uma oferta como nota privada, fora do webhook."""
import json
import re
import time
from contextlib import closing
import requests
import optout

BASE = 'https://app.esteiradeleads.shop'



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
    cols={r[1] for r in c.execute('PRAGMA table_info(oferta_interna_notas)')}
    for nome,tipo in [('banco',"TEXT NOT NULL DEFAULT ''"),('tag_estado',"TEXT NOT NULL DEFAULT ''"),('tag_tentativas','INTEGER NOT NULL DEFAULT 0'),('tag_tentar_em','REAL NOT NULL DEFAULT 0')]:
        if nome not in cols:c.execute(f'ALTER TABLE oferta_interna_notas ADD COLUMN {nome} {tipo}')



def cpf(v):
    s=re.sub(r'\D','',str(v or ''))
    return s.zfill(11) if 0<len(s)<=11 else ''


def agendar(c,d,evento):
    cfg=c.execute('SELECT ativo FROM oferta_interna_config WHERE id=1').fetchone()
    chat=d.get('chat_summary') or {}; conta=str(chat.get('account_id') or '')
    ident=str(chat.get('chat_id') or '')
    contato=(chat.get('details') or {}).get('contact') or {}
    doc=cpf(contato.get('cpf'))
    if not cfg or not cfg['ativo'] or conta!=str(c.execute('SELECT vendeai_account_id FROM configuracao WHERE id=1').fetchone()[0]) or not ident.isdigit() or not doc:return
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


def buscar(get_db,cfg,doc,detalhes=False):
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
        if texto:ofertas.append({'texto':texto,'banco':str(resultado.get('banco') or '').strip().upper() if isinstance(resultado,dict) else ''})
    if not ofertas:raise ValueError('Oferta ainda indisponivel')
    if len(ofertas)!=1:raise ValueError('Mais de uma oferta valida; revisar correspondencia')
    return ofertas[0] if detalhes else ofertas[0]['texto']



def finalizar(get_db,item,estado,detalhe='',message_id=None):
    with closing(get_db()) as c,c:
        c.execute('UPDATE oferta_interna_notas SET estado=?,detalhe=?,message_id=?,atualizado_em=?,tentar_em=? WHERE conta=? AND chat=? AND cpf=?',
                  (estado,detalhe,message_id,time.time(),time.time()+60*item['tentativas'],item['conta'],item['chat'],item['cpf']))


def processar_um(get_db,conta,token_crm):
    conta=str(conta)
    if not conta.isdigit() or not token_crm:return False
    with closing(get_db()) as c:
        if conta!=str(c.execute('SELECT vendeai_account_id FROM configuracao WHERE id=1').fetchone()[0]):return False
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        cfg=dict(c.execute('SELECT * FROM oferta_interna_config WHERE id=1').fetchone())
        if not cfg['ativo'] or not cfg['usuario'] or not cfg['senha']:return False
        r=c.execute("SELECT * FROM oferta_interna_notas WHERE conta=? AND estado='PENDENTE' AND tentar_em<=? ORDER BY criado_em LIMIT 1",(conta,time.time())).fetchone()
        if not r:return False
        item=dict(r);item['tentativas']+=1
        c.execute("UPDATE oferta_interna_notas SET estado='CONSULTANDO',tentativas=?,atualizado_em=? WHERE conta=? AND chat=? AND cpf=?",(item['tentativas'],time.time(),conta,item['chat'],item['cpf']))
    try:
        oferta=buscar(get_db,cfg,item['cpf'],detalhes=True)
        texto=oferta['texto']
    except (requests.RequestException,ValueError,TypeError,AttributeError):
        finalizar(get_db,item,'PENDENTE' if item['tentativas']<3 else 'REVISAR','Consulta sem oferta confirmada; nenhuma nota enviada.')
        return True
    with closing(get_db()) as c,c:
        if not c.execute('SELECT ativo FROM oferta_interna_config WHERE id=1').fetchone()[0] or impedido(c,conta,item['chat']):
            c.execute("UPDATE oferta_interna_notas SET estado='CANCELADO' WHERE conta=? AND chat=? AND cpf=?",(conta,item['chat'],item['cpf']))
            return True
        c.execute("UPDATE oferta_interna_notas SET estado='ENVIANDO',oferta=?,banco=?,tag_estado=?,atualizado_em=? WHERE conta=? AND chat=? AND cpf=?",(texto,oferta['banco'],'PENDENTE' if oferta['banco'] in ('V8','V8 BANK') else '',time.time(),conta,item['chat'],item['cpf']))
    try:
        r=requests.post('https://crm.vendeaitecnologia.com.br/api/v1/accounts/'+conta+'/conversations/'+item['chat']+'/messages',
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


def processar_tag(get_db,conta,token_crm):
    conta=str(conta)
    if not conta.isdigit() or not token_crm:return False
    with closing(get_db()) as c:
        if conta!=str(c.execute('SELECT vendeai_account_id FROM configuracao WHERE id=1').fetchone()[0]):return False
    with closing(get_db()) as c:
        if not c.execute('SELECT ativo FROM oferta_interna_config WHERE id=1').fetchone()[0]:return False
        r=c.execute("SELECT * FROM oferta_interna_notas WHERE conta=? AND estado='CONCLUIDO' AND tag_estado='PENDENTE' AND tag_tentar_em<=? ORDER BY criado_em LIMIT 1",(conta,time.time())).fetchone()
        if not r:return False
        item=dict(r)
        blocked=impedido(c,conta,item['chat'])
    estado='CANCELADO' if blocked else 'CONCLUIDO'
    tentativa=item['tag_tentativas']+1
    if not blocked:
        try:
            url=optout.BASE+conta+'/conversations/'+item['chat']+'/labels'
            headers={'api_access_token':token_crm}
            def ler():
                r=requests.get(url,headers=headers,timeout=(3,10),allow_redirects=False)
                if r.status_code!=200:raise ValueError('Etiquetas indisponiveis')
                labels=r.json().get('payload')
                if not isinstance(labels,list) or not all(isinstance(x,str) for x in labels):raise ValueError('Etiquetas invalidas')
                return labels
            labels=ler()
            if 'oferta_v8' not in labels:
                r=requests.post(url,headers=headers,json={'labels':list(dict.fromkeys(labels+['oferta_v8']))},timeout=(3,10),allow_redirects=False)
                if not 200<=r.status_code<300:raise ValueError('Etiqueta recusada')
                if 'oferta_v8' not in ler():raise ValueError('Etiqueta nao confirmada')
        except (requests.RequestException,ValueError,TypeError,AttributeError):
            estado='PENDENTE' if tentativa<3 else 'REVISAR'
    with closing(get_db()) as c,c:
        c.execute('UPDATE oferta_interna_notas SET tag_estado=?,tag_tentativas=?,tag_tentar_em=? WHERE conta=? AND chat=? AND cpf=?',
                  (estado,tentativa,time.time()+60*tentativa,conta,item['chat'],item['cpf']))
    return True
