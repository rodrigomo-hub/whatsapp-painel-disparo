"""Gupshup já conectada à VendeAI; sem credenciais Meta nem qualidade presumida."""
import json
import time
import re
from datetime import datetime,timezone
from contextlib import closing
from urllib.parse import urlparse
import requests
from fastapi import HTTPException
import oficiais_vendeai as oficial
import conexoes_normais as normal
import imagens_template

TIPO='gupshup_vendeai'


def via(n):return dict(n).get('tipo_envio')==TIPO


def chave(n):return 'gupshup-vendeai:'+str(n['vendeai_conta'])+':'+str(n['vendeai_inbox_id'])


def migrar(c):
    c.execute('''CREATE TABLE IF NOT EXISTS gup_vendeai_saude(
        numero_id INTEGER PRIMARY KEY, consultado REAL NOT NULL DEFAULT 0,
        disponivel INTEGER NOT NULL DEFAULT 0, detalhe TEXT NOT NULL DEFAULT '')''')


def importar(db,i,body):
    if not i.get('app_id') or not re.fullmatch(r'\+?\d{10,15}',i.get('telefone','')):
        raise HTTPException(400,'App ID ou telefone da conexão não informado.')
    for k in ('limite_diario','limite_hora'):
        if type(body.get(k)) is not int or not 0<=body[k]<=100000:raise HTTPException(400,'Informe limites inteiros de 0 a 100000.')
    t=next((t for t in i['templates'] if t.get('name')==body.get('template_nome') and t.get('language')==body.get('template_idioma')),None)
    if not t or t.get('status')!='APPROVED':raise HTTPException(400,'Selecione um template aprovado.')
    _,header,ids=oficial.estrutura(t)
    with closing(db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        n,erro=oficial.correspondencia(c,i)
        if erro:raise HTTPException(409,erro)
        if n:return {'id':n['id'],'novo':False}
        # Também evita importar o mesmo app pela segunda inbox ou pelo caminho direto.
        if c.execute("SELECT 1 FROM numeros WHERE phone_number_id=?",('gupshup:'+i['app_id'],)).fetchone() or c.execute('SELECT 1 FROM gup_apps WHERE app_id=? AND numero_id IS NOT NULL',(i['app_id'],)).fetchone():
            raise HTTPException(409,'Este aplicativo já está cadastrado no painel. Revise o cadastro existente.')
        w='gupshup-vendeai:'+i['account_id']+':'+i['inbox_id']
        ident=c.execute('''INSERT INTO numeros(nome,phone_number_id,access_token,waba_id,tipo_envio,via_envio,
            vendeai_conta,vendeai_inbox_id,template_nome,template_idioma,template_header,template_sem_variaveis,
            template_status,template_categoria,limite_diario,limite_hora,ativo,qualidade,status_envio_meta)
            VALUES(?,?,'',?,?,'vendeai',?,?,?,?,?,?,?,?,?,?,0,'UNKNOWN','UNKNOWN')''',
            ((i['nome']+' '+i['telefone']).strip(),'gupshup:'+i['app_id'],w,TIPO,i['account_id'],i['inbox_id'],
             t['name'],t['language'],header,int(not ids),t['status'],t.get('category','UNKNOWN'),body['limite_diario'],body['limite_hora'])).lastrowid
    return {'id':ident,'novo':True}


def remoto(n,cfg,token):
    account,_=normal.credenciais(cfg,token)
    if str(n['vendeai_conta'])!=account:raise HTTPException(400,'Conexão de outra conta.')
    i=next((i for i in oficial.listar_remoto(cfg,token) if i['inbox_id']==str(n['vendeai_inbox_id'])),None)
    if not i or i.get('provider')!='gupshup' or 'gupshup:'+i.get('app_id','')!=n['phone_number_id']:
        raise HTTPException(400,'Conexão Gupshup ausente ou aplicativo alterado na VendeAI.')
    if i.get('_reauthorization'):raise HTTPException(400,'Conexão exige reautorização na VendeAI.')
    return i


def template(n,i):
    t=next((t for t in i['templates'] if t.get('name')==n['template_nome'] and t.get('language')==n['template_idioma']),None)
    if not t:raise HTTPException(400,'Template não encontrado na conexão VendeAI.')
    return t


def atualizar(db,n,cfg,token):
    n=dict(n);t=None;ok=False;detalhe=''
    try:
        i=remoto(n,cfg,token);t=template(n,i);oficial.estrutura(t)
        ok=t.get('status')=='APPROVED'
        if not ok:detalhe='Template não aprovado: '+str(t.get('status','UNKNOWN'))
    except HTTPException as e:detalhe=str(e.detail)
    with closing(db()) as c,c:
        # Não aplicar o resultado a uma edição concorrente.
        atual=c.execute('SELECT * FROM numeros WHERE id=?',(n['id'],)).fetchone()
        if not atual or any(atual[k]!=n[k] for k in ('phone_number_id','template_nome','template_idioma','vendeai_inbox_id','vendeai_conta')):return None
        c.execute('INSERT INTO gup_vendeai_saude VALUES(?,?,?,?) ON CONFLICT(numero_id) DO UPDATE SET consultado=excluded.consultado,disponivel=excluded.disponivel,detalhe=excluded.detalhe',
                  (n['id'],time.time(),int(ok),detalhe))
        c.execute("UPDATE numeros SET qualidade='UNKNOWN',status_envio_meta='UNKNOWN',template_status=?,template_categoria=?,meta_consultado_em=? WHERE id=?",
                  ((t or {}).get('status','UNKNOWN'),(t or {}).get('category','UNKNOWN'),datetime.now(timezone.utc).isoformat(),n['id']))
        if t:
            header=next((x.get('format','') for x in t.get('components',[]) if x.get('type')=='HEADER'),t.get('header_type') or '')
            c.execute('UPDATE numeros SET template_header=? WHERE id=?',(header,n['id']))
        if not ok:c.execute('UPDATE numeros SET ativo=0 WHERE id=?',(n['id'],))
    return t


def elegivel(db,n):
    n=dict(n)
    if not n.get('ativo') or n.get('template_status')!='APPROVED' or (n.get('template_header')=='IMAGE' and not n.get('template_imagem')):return False
    with closing(db()) as c:
        r=c.execute('SELECT * FROM gup_vendeai_saude WHERE numero_id=?',(n['id'],)).fetchone()
        cfg=c.execute('SELECT vendeai_account_id FROM configuracao WHERE id=1').fetchone()
    return bool(cfg and str(cfg[0])==n['vendeai_conta'] and r and r['disponivel'] and time.time()-r['consultado']<1200)


def editar(db,n,body,cfg,token,base_url):
    n=dict(n)
    if body=={'ativo':False}:
        with closing(db()) as c,c:c.execute('UPDATE numeros SET ativo=0 WHERE id=?',(n['id'],))
        return {'status':'ok'}
    allowed={'ativo','via_envio','template_nome','template_idioma','template_sem_variaveis','imagem_base64','limite_diario','limite_hora'}
    if set(body)-allowed:raise HTTPException(400,'Configuração não suportada.')
    if 'ativo' in body and type(body['ativo']) is not bool:raise HTTPException(400,'Informe ativo como booleano.')
    if body.get('via_envio','vendeai')!='vendeai':raise HTTPException(400,'Esta conexão envia pela VendeAI.')
    for k in ('limite_diario','limite_hora'):
        if k in body and (type(body[k]) is not int or not 0<=body[k]<=100000):raise HTTPException(400,'Limite inválido.')
    previsto={**n,**body};i=remoto(previsto,cfg,token);t=template(previsto,i)
    if t.get('status')!='APPROVED':raise HTTPException(400,'Escolha um template aprovado.')
    _,header,ids=oficial.estrutura(t)
    imagem=n['template_imagem'] if all(previsto[k]==n[k] for k in ('template_nome','template_idioma')) else ''
    if body.get('imagem_base64'):
        if header!='IMAGE':raise HTTPException(400,'Este template não tem imagem.')
        imagem=imagens_template.salvar(db,n,body['imagem_base64'],somente_local=True)
    if header=='IMAGE' and not imagem:raise HTTPException(400,'Anexe a imagem do template.')
    if header=='IMAGE' and urlparse(base_url).scheme!='https':raise HTTPException(400,'A imagem exige HTTPS.')
    with closing(db()) as c,c:
        c.execute('''UPDATE numeros SET template_nome=?,template_idioma=?,template_header=?,template_imagem=?,
            template_sem_variaveis=?,template_status=?,template_categoria=?,envio_base_url=?,limite_diario=?,limite_hora=?,ativo=? WHERE id=?''',
            (t['name'],t['language'],header,imagem if header=='IMAGE' else '',int(not ids),t['status'],t.get('category','UNKNOWN'),base_url,
             previsto['limite_diario'],previsto['limite_hora'],int(previsto['ativo']),n['id']))
        c.execute('INSERT INTO gup_vendeai_saude VALUES(?,?,1,\'\') ON CONFLICT(numero_id) DO UPDATE SET consultado=excluded.consultado,disponivel=1,detalhe=\'\'',(n['id'],time.time()))
    return {'status':'ok'}


def enviar(db,n,telefone,variaveis,cfg,token,suprimido):
    n=dict(n)
    try:
        t=atualizar(db,n,cfg,token)
        with closing(db()) as c:atual=c.execute('SELECT * FROM numeros WHERE id=?',(n['id'],)).fetchone()
        keys=('ativo','template_nome','template_idioma','template_imagem','phone_number_id','vendeai_conta','vendeai_inbox_id')
        if not atual or any(atual[k]!=n[k] for k in keys) or not elegivel(db,atual) or not t or suprimido(telefone):raise HTTPException(400,'Conexão ou template indisponível, configuração alterada ou cliente na lista negra.')
        n=dict(atual);corpo,header,ids=oficial.estrutura(t)
        if ids and (len(variaveis)!=len(ids) or any(not isinstance(v,str) or not v.strip() for v in variaveis)):raise HTTPException(400,'Preencha as variáveis do template.')
        payload={'type':'whatsapp_oficial','message':corpo,'template_name':t['name'],'template_category':t['category'],'language':t['language'],'params':dict(zip(ids,variaveis)) if ids else {}}
        if header=='IMAGE':
            if urlparse(n['envio_base_url']).scheme!='https':raise HTTPException(400,'Imagem requer HTTPS.')
            payload.update(header_url=oficial.imagem_url(db,n),header_media_type='image')
    except HTTPException as e:return False,str(e.detail),''
    # Última conferência após preparar a mídia; nunca repete um envio incerto.
    with closing(db()) as c:atual=c.execute('SELECT * FROM numeros WHERE id=?',(n['id'],)).fetchone()
    if not atual or any(atual[k]!=n[k] for k in keys) or suprimido(telefone):return False,'Envio cancelado: configuração alterada ou lista negra.',''
    account,token=normal.credenciais(cfg,token)
    try:
        r=requests.post(normal.API+'send/',json={'account_id':account,'crm_api_access_token':token,'inbox_id':n['vendeai_inbox_id'],'customer_phone':'+'+telefone,'payload':payload},timeout=(5,30),allow_redirects=False)
        if r.status_code==200:
            d=r.json()
            if d.get('status')=='success' and d.get('message_id'):
                normal.agendar_etiqueta(db,account,str(d['message_id']),n['vendeai_inbox_id'],telefone,n['id'])
                return True,'',str(d['message_id'])
            return False,'ENVIO_INCERTO: VendeAI não confirmou o envio. Confira no CRM.',''
        if r.status_code>=500 or 300<=r.status_code<400:return False,'ENVIO_INCERTO: resposta VendeAI HTTP '+str(r.status_code),''
        return False,'VendeAI recusou o envio: HTTP '+str(r.status_code),''
    except (requests.RequestException,ValueError,TypeError,AttributeError):return False,'ENVIO_INCERTO: conferir na VendeAI antes de repetir',''
