"""Conexões de texto livre da VendeAI; nenhum número é ativado por descoberta."""
import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from fastapi import HTTPException

CRM='https://crm.vendeaitecnologia.com.br/api/v1/accounts/'
API='https://ia.vendeaitecnologia.com.br/api/message-handler/mailing/'

def agora():return datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat()
def normal(n):return dict(n).get('tipo_envio')=='vendeai_normal'
def migrar(c):
    cols={r['name'] for r in c.execute('PRAGMA table_info(numeros)')}
    for name,kind in [('tipo_envio',"TEXT NOT NULL DEFAULT 'meta'"),('vendeai_inbox_id',"TEXT NOT NULL DEFAULT ''"),('vendeai_conta',"TEXT NOT NULL DEFAULT ''"),('modelo_normal_id','INTEGER'),('conexao_status',"TEXT NOT NULL DEFAULT 'unknown'")]:
        if name not in cols:c.execute(f'ALTER TABLE numeros ADD COLUMN {name} {kind}')
    c.execute('CREATE TABLE IF NOT EXISTS modelos_normais(id INTEGER PRIMARY KEY,nome TEXT NOT NULL,texto TEXT NOT NULL,ativo INTEGER NOT NULL DEFAULT 1)')
    c.execute('CREATE TABLE IF NOT EXISTS conexoes_vendeai(conta TEXT,inbox_id TEXT,nome TEXT,telefone TEXT,provider TEXT,status TEXT,consultado_em TEXT,PRIMARY KEY(conta,inbox_id))')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_numero_inbox_normal ON numeros(vendeai_conta,vendeai_inbox_id) WHERE tipo_envio='vendeai_normal'")

def credenciais(cfg,token):
    account=str(cfg.get('vendeai_account_id') or '')
    token=cfg.get('vendeai_mailing_token','') or token
    if not account.isdigit() or not token:raise HTTPException(400,'Configure a conta e o token da VendeAI primeiro.')
    return account,token

def estado(d):
    if isinstance(d,dict) and d.get('reauthorization_required') is True: return 'disconnected'
    x=d.get('provider_connection',{}).get('connection') if isinstance(d,dict) and isinstance(d.get('provider_connection'),dict) else None
    return 'connected' if x=='connected' else 'disconnected' if x in ('disconnected','close','closed','connecting') else 'unknown'

def descobrir(get_db,cfg,token):
    account,token=credenciais(cfg,token)
    try:
        r=requests.get(CRM+account+'/inboxes',headers={'api_access_token':token},timeout=(5,20),allow_redirects=False)
        q=requests.post(API+'inboxes/',json={'account_id':account,'crm_api_access_token':token},timeout=(5,25),allow_redirects=False)
        if r.status_code!=200 or q.status_code!=200:raise ValueError()
        items=r.json()['payload'];free=q.json()['inboxes']
        if not isinstance(items,list) or not isinstance(free,list):raise ValueError()
        ids={str(i['id']) for i in free if i.get('accepts_free_text') is True}
        found=[i for i in items if str(i.get('id')) in ids and i.get('provider') and i['provider']!='whatsapp_cloud']
        with closing(get_db()) as c,c:
            c.execute('UPDATE conexoes_vendeai SET status=?,consultado_em=? WHERE conta=?',('unknown',agora(),account))
            for i in found:
                c.execute('INSERT INTO conexoes_vendeai VALUES(?,?,?,?,?,?,?) ON CONFLICT(conta,inbox_id) DO UPDATE SET nome=excluded.nome,telefone=excluded.telefone,provider=excluded.provider,status=excluded.status,consultado_em=excluded.consultado_em',
                    (account,str(i['id']),i.get('name',''),i.get('phone_number',''),i['provider'],estado(i),agora()))
        return len(found)
    except (requests.RequestException,ValueError,KeyError,TypeError,AttributeError):
        raise HTTPException(502,'Não foi possível consultar as conexões na VendeAI. Nenhum número foi adicionado.')

def consultar(get_db,n,cfg,token):
    status='unknown'
    try:
        account,token=credenciais(cfg,token)
        if account!=n['vendeai_conta']:raise ValueError()
        r=requests.get(CRM+account+'/inboxes/'+n['vendeai_inbox_id'],headers={'api_access_token':token},timeout=(3,10),allow_redirects=False)
        if r.status_code==200:
            data=r.json()
            if str(data.get('id'))==n['vendeai_inbox_id'] and data.get('provider') and data['provider']!='whatsapp_cloud':status=estado(data)
        elif r.status_code==404:status='disconnected'
    except (requests.RequestException,ValueError,TypeError,AttributeError,HTTPException):pass
    with closing(get_db()) as c,c:
        c.execute('UPDATE numeros SET conexao_status=?,meta_consultado_em=? WHERE id=?',(status,agora(),n['id']))
        c.execute('UPDATE conexoes_vendeai SET status=?,consultado_em=? WHERE conta=? AND inbox_id=?',(status,agora(),n['vendeai_conta'],n['vendeai_inbox_id']))
    return status

def elegivel(get_db,n):
    # Relê para respeitar pausas/desconexões ocorridas durante um lote.
    with closing(get_db()) as c:
        atual=c.execute('SELECT * FROM numeros WHERE id=?',(n['id'],)).fetchone()
        conta=c.execute('SELECT vendeai_account_id FROM configuracao WHERE id=1').fetchone()[0]
        if not atual or not atual['ativo'] or atual['conexao_status']!='connected' or atual['vendeai_conta']!=str(conta): return False
        try:
            idade=(datetime.now(ZoneInfo('America/Sao_Paulo'))-datetime.fromisoformat(atual['meta_consultado_em'])).total_seconds()
            if idade<0 or idade>1800: return False
        except (TypeError,ValueError): return False
        return bool(c.execute('SELECT 1 FROM modelos_normais WHERE id=? AND ativo=1',(atual['modelo_normal_id'],)).fetchone())


def validar_modelo(body):
    nome=body.get('nome');texto=body.get('texto');ativo=body.get('ativo',True)
    if not isinstance(nome,str) or not 1<=len(nome.strip())<=100 or not isinstance(texto,str) or not 1<=len(texto.strip())<=4000 or type(ativo) is not bool:
        raise HTTPException(400,'Informe nome (até 100 caracteres), mensagem (até 4.000) e ativação válida.')
    rest=texto.replace('{primeiro_nome}','')
    if '{' in rest or '}' in rest:raise HTTPException(400,'A variável disponível é {primeiro_nome}.')
    return nome.strip(),texto.strip(),int(ativo)

def salvar_modelo(get_db,body,ident=None):
    values=validar_modelo(body)
    with closing(get_db()) as c,c:
        if ident is None:ident=c.execute('INSERT INTO modelos_normais(nome,texto,ativo) VALUES(?,?,?)',values).lastrowid
        else:
            if not c.execute('UPDATE modelos_normais SET nome=?,texto=?,ativo=? WHERE id=?',(*values,ident)).rowcount:raise HTTPException(404,'Modelo não encontrado.')
        c.execute("UPDATE numeros SET template_nome=? WHERE tipo_envio='vendeai_normal' AND modelo_normal_id=?",(values[0],ident))
    return {'id':ident}

def mensagem(texto,nome):
    if '{primeiro_nome}' in texto:
        if not isinstance(nome,str) or not nome.strip():raise HTTPException(400,'O modelo exige o nome do lead para preencher {primeiro_nome}.')
        texto=texto.replace('{primeiro_nome}',nome.strip().split()[0].capitalize())
    return texto

def validar_numero(c,body):
    for k in ('limite_diario','limite_hora'):
        if type(body.get(k)) is not int or not 1<=body[k]<=100000:raise HTTPException(400,'Limites diário e por hora devem ser inteiros positivos.')
    if type(body.get('modelo_id')) is not int:raise HTTPException(400,'Escolha um modelo de mensagem.')
    m=c.execute('SELECT * FROM modelos_normais WHERE id=? AND ativo=1',(body['modelo_id'],)).fetchone()
    if not m:raise HTTPException(400,'Escolha um modelo ativo.')
    if 'ativo' in body and type(body['ativo']) is not bool:raise HTTPException(400,'Ativação inválida.')
    return m

def importar(get_db,cfg,token,body):
    account,_=credenciais(cfg,token);inbox=str(body.get('inbox_id') or '')
    if not inbox.isdigit():raise HTTPException(400,'Selecione uma conexão.')
    descobrir(get_db,cfg,token)
    with closing(get_db()) as c,c:
        m=validar_numero(c,body)
        i=c.execute('SELECT * FROM conexoes_vendeai WHERE conta=? AND inbox_id=?',(account,inbox)).fetchone()
        if not i or i['status']!='connected':raise HTTPException(400,'Conexão não confirmada. Atualize a lista antes de adicionar.')
        if c.execute("SELECT 1 FROM numeros WHERE tipo_envio='vendeai_normal' AND vendeai_conta=? AND vendeai_inbox_id=?",(account,inbox)).fetchone():raise HTTPException(409,'Este número já foi adicionado.')
        name=(i['nome']+' '+i['telefone']).strip()
        try:
            ident=c.execute("INSERT INTO numeros(nome,phone_number_id,access_token,tipo_envio,vendeai_conta,vendeai_inbox_id,modelo_normal_id,template_nome,template_sem_variaveis,ativo,limite_diario,limite_hora,conexao_status,meta_consultado_em) VALUES(?,?,'','vendeai_normal',?,?,?,?,1,1,?,?,?,?)",(name,'vendeai:'+account+':'+inbox,account,inbox,m['id'],m['nome'],body['limite_diario'],body['limite_hora'],i['status'],i['consultado_em'])).lastrowid
        except sqlite3.IntegrityError:raise HTTPException(409,'Este número já foi adicionado.')
    return {'id':ident}

def editar(get_db,n,body):
    if body == {'ativo':False}:
        with closing(get_db()) as c,c: c.execute('UPDATE numeros SET ativo=0 WHERE id=?',(n['id'],))
        return {'status':'ok'}
    with closing(get_db()) as c,c:
        full={'modelo_id':n['modelo_normal_id'],'limite_diario':n['limite_diario'],'limite_hora':n['limite_hora'],**body}
        m=validar_numero(c,full)
        if any(k in body for k in ('access_token','phone_number_id','waba_id','template_nome','template_idioma','tipo_envio','imagem_base64')):raise HTTPException(400,'Use os campos de conexão normal: modelo e limites.')
        c.execute('UPDATE numeros SET modelo_normal_id=?,template_nome=?,limite_diario=?,limite_hora=?,ativo=? WHERE id=?',(m['id'],m['nome'],full['limite_diario'],full['limite_hora'],int(body.get('ativo',n['ativo'])),n['id']))
    return {'status':'ok'}

def enviar(get_db,n,phone,nome,cfg,token,suprimido):
    with closing(get_db()) as c:
        atual=c.execute('SELECT * FROM numeros WHERE id=?',(n['id'],)).fetchone()
        m=c.execute('SELECT * FROM modelos_normais WHERE id=? AND ativo=1',(atual['modelo_normal_id'],)).fetchone() if atual else None
    if not atual or not atual['ativo'] or not m:return False,'BLOQUEADO_MODELO: número pausado ou modelo indisponível',''
    try:msg=mensagem(m['texto'],nome)
    except HTTPException as e:return False,'BLOQUEADO_MODELO: '+e.detail,''
    if consultar(get_db,atual,cfg,token)!='connected':return False,'CONEXAO_INDISPONIVEL: sem conexão confirmada na VendeAI',''
    # Consulta externa pode demorar: revalida pausa e supressão antes do POST.
    with closing(get_db()) as c:
        a=c.execute('SELECT n.ativo,m.texto FROM numeros n JOIN modelos_normais m ON m.id=n.modelo_normal_id AND m.ativo=1 WHERE n.id=?',(n['id'],)).fetchone()
    if not a or not a['ativo'] or suprimido(phone):return False,'BLOQUEADO_MODELO: envio bloqueado por pausa ou lista negra',''
    try: msg=mensagem(a['texto'],nome)
    except HTTPException as e:return False,'BLOQUEADO_MODELO: '+e.detail,''
    account,token=credenciais(cfg,token)
    try:
        r=requests.post(API+'send/',json={'account_id':account,'crm_api_access_token':token,'inbox_id':atual['vendeai_inbox_id'],'customer_phone':'+'+phone,'payload':{'type':'whatsapp_nao_oficial','message':msg}},timeout=(5,30),allow_redirects=False)
        if r.status_code==200:
            d=r.json()
            if d.get('status')=='success' and d.get('message_id'):return True,'',str(d['message_id'])
            return False,'ENVIO_INCERTO: VendeAI não confirmou o identificador da mensagem',''
        if r.status_code>=500 or 300<=r.status_code<400:return False,'ENVIO_INCERTO: resposta VendeAI HTTP '+str(r.status_code),''
        return False,'VendeAI recusou o envio: HTTP '+str(r.status_code),''
    except (requests.RequestException,ValueError,TypeError,AttributeError):return False,'ENVIO_INCERTO: conferir na VendeAI antes de repetir',''
