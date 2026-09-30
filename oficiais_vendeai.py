"""Importação de oficiais por identidade Meta e envio opcional pela VendeAI."""
import json
import re
import secrets
import time
from contextlib import closing
from urllib.parse import urlparse

import requests
from fastapi import HTTPException
import conexoes_normais as normal


def migrar(c):
    cols={r['name'] for r in c.execute('PRAGMA table_info(numeros)')}
    for nome,tipo in [('via_envio',"TEXT NOT NULL DEFAULT 'meta'"),('envio_base_url',"TEXT NOT NULL DEFAULT ''")]:
        if nome not in cols:c.execute(f'ALTER TABLE numeros ADD COLUMN {nome} {tipo}')
    c.execute('''CREATE TABLE IF NOT EXISTS oficiais_vendeai (
        account_id TEXT NOT NULL,inbox_id TEXT NOT NULL,phone_id TEXT NOT NULL,waba_id TEXT NOT NULL,
        nome TEXT NOT NULL,telefone TEXT NOT NULL,templates TEXT NOT NULL,consultado_em REAL NOT NULL,
        PRIMARY KEY(account_id,inbox_id))''')
    c.execute('CREATE TABLE IF NOT EXISTS midias_vendeai(token TEXT PRIMARY KEY,imagem_id TEXT NOT NULL,expira REAL NOT NULL)')


def via(n):return not normal.normal(n) and dict(n).get('via_envio')=='vendeai'


def obter_json(url,token,params=None):
    try:
        r=requests.get(url,headers={'api_access_token':token},params=params,timeout=(5,20),allow_redirects=False)
        if r.status_code!=200:raise ValueError()
        d=r.json()
        if not isinstance(d,dict):raise ValueError()
        return d
    except (requests.RequestException,ValueError,TypeError):
        raise HTTPException(502,'Não foi possível confirmar a conexão na VendeAI.')


def listar_remoto(cfg,token):
    account,token=normal.credenciais(cfg,token)
    crm=obter_json(normal.CRM+account+'/inboxes',token).get('payload')
    try:
        r=requests.post(normal.API+'inboxes/',json={'account_id':account,'crm_api_access_token':token},timeout=(5,25),allow_redirects=False)
        mailing=r.json().get('inboxes')
        if r.status_code!=200 or not isinstance(mailing,list) or not isinstance(crm,list):raise ValueError()
        lookup={str(i['id']):i for i in mailing}
        result=[]
        for i in crm:
            if i.get('provider')!='whatsapp_cloud':continue
            p=i.get('provider_config') or {};m=lookup.get(str(i['id']),{})
            result.append({'account_id':account,'inbox_id':str(i['id']),'phone_id':str(p.get('phone_number_id') or ''),
                'waba_id':str(p.get('business_account_id') or ''),'nome':str(i.get('name') or ''),'telefone':str(i.get('phone_number') or ''),
                'templates':m.get('templates') or [],'_token':p.get('api_key') or '', '_reauthorization':i.get('reauthorization_required') is True})
        return result
    except (requests.RequestException,ValueError,TypeError,AttributeError,KeyError):
        raise HTTPException(502,'Não foi possível obter os templates das conexões oficiais.')


def nomes_waba(get_db,items):
    nomes={}
    for i in items:
        waba=i['waba_id']
        if waba not in nomes:
            with closing(get_db()) as c:
                salvo=c.execute('SELECT nome FROM wabas_salvas WHERE waba_id=?',(waba,)).fetchone()
            nome=salvo['nome'] if salvo else ''
            if not nome and waba.isdigit() and i['_token']:
                try:
                    r=requests.get('https://graph.facebook.com/v21.0/'+waba,
                        headers={'Authorization':'Bearer '+i['_token']},params={'fields':'id,name'},timeout=(5,15),allow_redirects=False)
                    d=r.json() if r.status_code==200 else {}
                    if str(d.get('id'))==waba and isinstance(d.get('name'),str):nome=d['name'].strip()
                except (requests.RequestException,ValueError,TypeError,AttributeError):pass
            nomes[waba]=nome or ('WABA '+waba)
        i['nome_caixa']=i['nome']
        i['nome']=nomes[waba]
    return items


def descobrir(get_db,cfg,token):
    items=nomes_waba(get_db,listar_remoto(cfg,token))
    account,_=normal.credenciais(cfg,token)
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM oficiais_vendeai WHERE account_id=?',(account,))
        for i in items:
            c.execute('INSERT INTO oficiais_vendeai VALUES(?,?,?,?,?,?,?,?)',
                (account,i['inbox_id'],i['phone_id'],i['waba_id'],i['nome'],i['telefone'],json.dumps(i['templates']),time.time()))
        for i in items:
            n,erro=correspondencia(c,i)
            if n and not erro and i['phone_id'].isdigit() and i['waba_id'].isdigit():
                c.execute('UPDATE numeros SET vendeai_conta=?,vendeai_inbox_id=? WHERE id=?',
                          (account,i['inbox_id'],n['id']))
                if n['nome']==i['nome_caixa']:
                    c.execute('UPDATE numeros SET nome=? WHERE id=?',((i['nome']+' '+i['telefone']).strip(),n['id']))
    return len(items)


def correspondencia(c,i):
    quantidade=c.execute('SELECT count(*) FROM oficiais_vendeai WHERE account_id=? AND phone_id=? AND waba_id=?',
                         (i['account_id'],i['phone_id'],i['waba_id'])).fetchone()[0]
    if quantidade>1:return None,'Mais de uma conexão VendeAI usa este ID Meta. Revise as conexões.'
    vinculo=c.execute("SELECT phone_number_id,waba_id FROM numeros WHERE tipo_envio='meta' AND vendeai_conta=? AND vendeai_inbox_id=?",(i['account_id'],i['inbox_id'])).fetchone()
    if vinculo and (vinculo['phone_number_id']!=i['phone_id'] or vinculo['waba_id']!=i['waba_id']):
        return None,'Esta inbox está vinculada a outro ID Meta. Revise a troca de número.'
    rows=c.execute("SELECT * FROM numeros WHERE phone_number_id=? AND tipo_envio='meta'",(i['phone_id'],)).fetchall() if i['phone_id'] else []
    if len(rows)>1:return None,'Mais de um cadastro usa este ID Meta. Revise antes de vincular.'
    if rows:
        n=rows[0]
        if n['waba_id']!=i['waba_id']:return None,'O ID do número coincide, mas a WABA é diferente.'
        if n['vendeai_inbox_id'] and (n['vendeai_inbox_id']!=i['inbox_id'] or n['vendeai_conta']!=i['account_id']):
            return None,'Número já vinculado a outra conexão VendeAI.'
        return n,''
    return None,''


def listar(get_db,cfg):
    with closing(get_db()) as c:
        result=[]
        for row in c.execute('SELECT * FROM oficiais_vendeai WHERE account_id=? ORDER BY nome',(str(cfg['vendeai_account_id']),)):
            i=dict(row);i['templates']=json.loads(i['templates']);n,erro=correspondencia(c,i)
            i['numero']=({'id':n['id'],'nome':n['nome'],'template_nome':n['template_nome'],'template_idioma':n['template_idioma'],
                          'vinculado':bool(n['vendeai_inbox_id']),'via_envio':n['via_envio']} if n else None)
            i['divergencia']=erro or ('' if i['phone_id'].isdigit() and i['waba_id'].isdigit() else 'IDs Meta não confirmados.')
            result.append(i)
        return result


def meta_template(n):
    try:
        r=requests.get('https://graph.facebook.com/v21.0/'+n['waba_id']+'/message_templates',
            headers={'Authorization':'Bearer '+n['access_token']},params={'name':n['template_nome'],'limit':100},timeout=(5,15),allow_redirects=False)
        if r.status_code!=200:raise ValueError()
        items=r.json().get('data',[])
        t=next((t for t in items if t.get('name')==n['template_nome'] and t.get('language')==n['template_idioma']),None)
        if not t or t.get('status')!='APPROVED':raise ValueError()
        return t
    except (requests.RequestException,ValueError,TypeError,AttributeError):
        raise HTTPException(400,'Template não confirmado como aprovado na Meta. Atualize ou selecione outro.')


def estrutura(t):
    componentes=t.get('components') or []
    corpo=next((c.get('text','') for c in componentes if c.get('type')=='BODY'),t.get('body',''))
    header=next((c for c in componentes if c.get('type')=='HEADER'),{})
    tipo=header.get('format',t.get('header_type')) or ''
    variaveis=re.findall(r'\{\{\s*([^{}]+?)\s*\}\}',corpo)
    if t.get('parameter_format')=='NAMED' or any(not v.isdigit() for v in variaveis):
        raise HTTPException(400,'Este caminho aceita variáveis posicionais: {{1}}, {{2}}…')
    ids=sorted(set(variaveis),key=int)
    if ids!=[str(i) for i in range(1,len(ids)+1)]:raise HTTPException(400,'Variáveis do template fora da sequência esperada.')
    if tipo not in ('','TEXT','IMAGE'):raise HTTPException(400,'O painel suporta texto e imagem neste caminho. Vídeo e documento ainda não estão disponíveis.')
    if tipo=='TEXT' and ('{{' in header.get('text','') or t.get('header_variables')):
        raise HTTPException(400,'Variável no cabeçalho ainda não suportada neste caminho.')
    for comp in componentes:
        if comp.get('type')=='BUTTONS':
            for b in comp.get('buttons',[]):
                if '{{' in json.dumps(b) or b.get('type') in ('COPY_CODE','OTP','FLOW'):
                    raise HTTPException(400,'Este template exige parâmetros de botão ainda não suportados.')
    return corpo.replace('\r\n','\n').strip(),tipo,ids


def compatibilidade(n,templates,meta):
    t=next((t for t in templates if t.get('name')==n['template_nome'] and t.get('language')==n['template_idioma']),None)
    if not t or t.get('status')!='APPROVED':raise HTTPException(400,'O template escolhido não está aprovado/disponível na conexão VendeAI. Atualize a lista ou escolha outro.')
    if meta.get('category')!=t.get('category') or estrutura(t)!=estrutura(meta):
        raise HTTPException(400,'O template da VendeAI diverge da Meta (texto, categoria, variáveis ou cabeçalho). Atualize a conexão antes de enviar.')
    return t


def validar_via(get_db,n,cfg,token,base_url):
    account,_=normal.credenciais(cfg,token)
    if n.get('vendeai_conta')!=account or not n.get('vendeai_inbox_id'):raise HTTPException(400,'Vincule este número à conexão oficial da VendeAI primeiro.')
    i=next((i for i in listar_remoto(cfg,token) if i['inbox_id']==n['vendeai_inbox_id']),None)
    if not i or i['phone_id']!=n['phone_number_id'] or i['waba_id']!=n['waba_id']:
        raise HTTPException(400,'O vínculo com a VendeAI não corresponde ao número e à WABA cadastrados.')
    if i.get('_reauthorization'):raise HTTPException(400,'A conexão oficial exige reautorização na VendeAI.')
    meta=meta_template(n);t=compatibilidade(n,i['templates'],meta)
    corpo,tipo,ids=estrutura(t)
    if tipo=='IMAGE':
        if not n.get('template_imagem'):raise HTTPException(400,'Anexe a imagem antes de usar o envio pela VendeAI.')
        u=urlparse(base_url)
        if u.scheme!='https' or not u.hostname or u.username or u.password:raise HTTPException(400,'A imagem exige acesso ao painel por HTTPS.')
        with closing(get_db()) as c:
            if not c.execute('SELECT 1 FROM imagens_template WHERE id=? AND phone_id=?',(n['template_imagem'],n['phone_number_id'])).fetchone():raise HTTPException(400,'Imagem configurada não encontrada para este número.')
    return t


def vincular(get_db,cfg,token,body):
    inbox=str(body.get('inbox_id') or '')
    items=listar_remoto(cfg,token);i=next((i for i in items if i['inbox_id']==inbox),None)
    if not i or not i['phone_id'].isdigit() or not i['waba_id'].isdigit() or not i['_token']:raise HTTPException(400,'Credencial ou IDs da conexão não confirmados.')
    nomes_waba(get_db,[i])
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        n,erro=correspondencia(c,i)
        if erro:raise HTTPException(409,erro)
        if n:
            if body.get('numero_id')!=n['id']:raise HTTPException(409,'Já existe um cadastro. Atualize a lista e vincule o número existente.')
            c.execute('UPDATE numeros SET vendeai_conta=?,vendeai_inbox_id=? WHERE id=?',(i['account_id'],inbox,n['id']))
            return {'id':n['id'],'novo':False}
        if body.get('numero_id'):raise HTTPException(409,'O cadastro mudou. Atualize a lista.')
        for k in ('limite_diario','limite_hora'):
            if type(body.get(k)) is not int or not 1<=body[k]<=100000:raise HTTPException(400,'Informe limites inteiros positivos.')
        nome=body.get('template_nome');idioma=body.get('template_idioma')
        t=next((t for t in i['templates'] if t.get('name')==nome and t.get('language')==idioma),None)
        if not t or t.get('status')!='APPROVED':raise HTTPException(400,'Selecione um template aprovado desta conexão.')
        # Importar fica pausado; o operador revisa imagem e caminho no formulário existente.
        componentes=t.get('components') or []
        tipo=next((c.get('format','') for c in componentes if c.get('type')=='HEADER'),t.get('header_type') or '')
        corpo=next((c.get('text','') for c in componentes if c.get('type')=='BODY'),t.get('body',''))
        ident=c.execute('''INSERT INTO numeros(nome,phone_number_id,access_token,waba_id,template_nome,template_idioma,
            template_sem_variaveis,template_header,limite_diario,limite_hora,ativo,vendeai_conta,vendeai_inbox_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)''',((i['nome']+' '+i['telefone']).strip(),i['phone_id'],i['_token'],i['waba_id'],nome,idioma,
            int('{{' not in corpo),tipo,body['limite_diario'],body['limite_hora'],i['account_id'],inbox)).lastrowid
        c.execute('INSERT OR IGNORE INTO wabas_salvas(waba_id,access_token) VALUES(?,?)',(i['waba_id'],i['_token']))
        return {'id':ident,'novo':True}


def imagem_url(get_db,n):
    token=secrets.token_hex(32)
    with closing(get_db()) as c,c:
        c.execute('DELETE FROM midias_vendeai WHERE expira<?',(time.time(),))
        c.execute('INSERT INTO midias_vendeai VALUES(?,?,?)',(token,n['template_imagem'],time.time()+3600))
    return n['envio_base_url'].rstrip('/')+'/midia-vendeai/'+token


def midia(get_db,token):
    if not re.fullmatch('[0-9a-f]{64}',token):raise HTTPException(404,'Imagem indisponível.')
    with closing(get_db()) as c:
        r=c.execute('SELECT i.dados,i.mime FROM midias_vendeai m JOIN imagens_template i ON i.id=m.imagem_id WHERE m.token=? AND m.expira>?',(token,time.time())).fetchone()
    if not r:raise HTTPException(404,'Imagem indisponível.')
    return r


def enviar(get_db,numero,telefone,variaveis,cfg,token,verificar,suprimido):
    try:
        verificar(numero['id'])  # Consulta a Meta, inclusive saúde da WABA e template.
        with closing(get_db()) as c:n=dict(c.execute('SELECT * FROM numeros WHERE id=?',(numero['id'],)).fetchone())
        if not via(n) or not n['ativo'] or n['qualidade'] not in ('GREEN','YELLOW') or n['status_envio_meta']!='AVAILABLE' or n['template_status']!='APPROVED' or n['pausado_por_template']:
            raise HTTPException(400,'Número, WABA ou template não estão liberados pela Meta.')
        t=validar_via(get_db,n,cfg,token,n['envio_base_url'])
        corpo,tipo,ids=estrutura(t)
        if ids and (len(variaveis)!=len(ids) or any(not isinstance(v,str) or not v.strip() for v in variaveis)):
            raise HTTPException(400,'Quantidade ou conteúdo das variáveis incompatível com o template escolhido.')
        payload={'type':'whatsapp_oficial','message':corpo,'template_name':n['template_nome'],
            'template_category':t['category'],'language':n['template_idioma'],
            'params':dict(zip(ids,variaveis)) if ids else {}}
        if tipo=='IMAGE':payload.update(header_url=imagem_url(get_db,n),header_media_type='image')
        with closing(get_db()) as c:atual=c.execute('SELECT * FROM numeros WHERE id=?',(n['id'],)).fetchone()
        keys=('ativo','via_envio','template_nome','template_idioma','template_imagem','vendeai_conta','vendeai_inbox_id','phone_number_id','waba_id','qualidade','status_envio_meta','template_status','pausado_por_template')
        if not atual or any(atual[k]!=n[k] for k in keys) or suprimido(telefone):raise HTTPException(400,'Envio bloqueado: configuração alterada ou telefone na lista negra.')
    except HTTPException as e:return False,str(e.detail),''
    account,token=normal.credenciais(cfg,token)
    try:
        r=requests.post(normal.API+'send/',json={'account_id':account,'crm_api_access_token':token,'inbox_id':n['vendeai_inbox_id'],
            'customer_phone':'+'+telefone,'payload':payload},timeout=(5,30),allow_redirects=False)
        if r.status_code==200:
            d=r.json()
            if d.get('status')=='success' and d.get('message_id'):
                normal.agendar_etiqueta(get_db,account,str(d['message_id']),n['vendeai_inbox_id'],telefone,n['id'])
                return True,'',str(d['message_id'])
            return False,'ENVIO_INCERTO: VendeAI não confirmou a mensagem. Confira no CRM antes de repetir.',''
        if r.status_code>=500 or 300<=r.status_code<400:return False,'ENVIO_INCERTO: resposta VendeAI HTTP '+str(r.status_code),''
        return False,'VendeAI recusou o envio: HTTP '+str(r.status_code),''
    except (requests.RequestException,ValueError,TypeError,AttributeError):return False,'ENVIO_INCERTO: conferir na VendeAI antes de repetir',''


def elegivel(get_db,n):
    with closing(get_db()) as c:
        account=c.execute('SELECT vendeai_account_id FROM configuracao WHERE id=1').fetchone()[0]
    return (n['vendeai_conta']==str(account) and bool(n['vendeai_inbox_id']) and n['template_status']=='APPROVED')
