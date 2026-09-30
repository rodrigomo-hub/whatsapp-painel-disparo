"""Conexões de texto livre da VendeAI; nenhum número é ativado por descoberta."""
import sqlite3
import time
import re
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import imagens_template
from fastapi import HTTPException

CRM='https://crm.vendeaitecnologia.com.br/api/v1/accounts/'
API='https://ia.vendeaitecnologia.com.br/api/message-handler/mailing/'

def agora():return datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat()
def normal(n):return dict(n).get('tipo_envio')=='vendeai_normal'
def migrar(c):
    c.execute("""CREATE TABLE IF NOT EXISTS etiquetas_disparo(
        account_id TEXT NOT NULL, message_id TEXT NOT NULL, inbox_id TEXT NOT NULL,
        telefone TEXT NOT NULL, numero_id INTEGER, chat_id TEXT,
        estado TEXT NOT NULL DEFAULT 'pendente', tentativas INTEGER NOT NULL DEFAULT 0,
        tentar_em REAL NOT NULL DEFAULT 0, detalhe TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(account_id,message_id))""")
    cols={r['name'] for r in c.execute('PRAGMA table_info(numeros)')}
    for name,kind in [('tipo_envio',"TEXT NOT NULL DEFAULT 'meta'"),('vendeai_inbox_id',"TEXT NOT NULL DEFAULT ''"),('vendeai_conta',"TEXT NOT NULL DEFAULT ''"),('modelo_normal_id','INTEGER'),('conexao_status',"TEXT NOT NULL DEFAULT 'unknown'")]:
        if name not in cols:c.execute(f'ALTER TABLE numeros ADD COLUMN {name} {kind}')
    c.execute('CREATE TABLE IF NOT EXISTS modelos_normais(id INTEGER PRIMARY KEY,nome TEXT NOT NULL,texto TEXT NOT NULL,ativo INTEGER NOT NULL DEFAULT 1)')
    if 'imagem' not in {r['name'] for r in c.execute('PRAGMA table_info(modelos_normais)')}:
        c.execute("ALTER TABLE modelos_normais ADD COLUMN imagem TEXT NOT NULL DEFAULT ''")
    c.execute('CREATE TABLE IF NOT EXISTS padrao_conexoes_normais(conta TEXT PRIMARY KEY,modelo_id INTEGER,limite_diario INTEGER NOT NULL DEFAULT 0,limite_hora INTEGER NOT NULL DEFAULT 0)')
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
            # A descoberta também precisa retirar conexões indisponíveis do rodízio.
            # A preferência de ativação manual permanece intacta.
            c.execute("""UPDATE numeros SET
                conexao_status=COALESCE((SELECT status FROM conexoes_vendeai v
                    WHERE v.conta=numeros.vendeai_conta AND v.inbox_id=numeros.vendeai_inbox_id),'unknown'),
                meta_consultado_em=? WHERE tipo_envio='vendeai_normal' AND vendeai_conta=?""",(agora(),account))
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
        if not atual or not atual['ativo'] or atual['limite_diario']<=0 or atual['limite_hora']<=0 or atual['conexao_status']!='connected' or atual['vendeai_conta']!=str(conta): return False
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
    if 'remover_imagem' in body and type(body['remover_imagem']) is not bool:raise HTTPException(400,'Remoção de imagem inválida.')
    if body.get('remover_imagem') and body.get('imagem_base64'):raise HTTPException(400,'Escolha anexar ou remover a imagem.')
    with closing(get_db()) as c:
        if ident is not None and not c.execute('SELECT 1 FROM modelos_normais WHERE id=?',(ident,)).fetchone():raise HTTPException(404,'Modelo não encontrado.')
    imagem=None
    if body.get('imagem_base64'):
        imagem=imagens_template.salvar(get_db,{'phone_number_id':'modelo-normal'},body['imagem_base64'],somente_local=True)
    elif body.get('remover_imagem'):imagem=''
    with closing(get_db()) as c,c:
        if ident is None:ident=c.execute('INSERT INTO modelos_normais(nome,texto,ativo) VALUES(?,?,?)',values).lastrowid
        else:
            if not c.execute('UPDATE modelos_normais SET nome=?,texto=?,ativo=? WHERE id=?',(*values,ident)).rowcount:raise HTTPException(404,'Modelo não encontrado.')
        if imagem is not None:c.execute('UPDATE modelos_normais SET imagem=? WHERE id=?',(imagem,ident))
        c.execute("UPDATE numeros SET template_nome=? WHERE tipo_envio='vendeai_normal' AND modelo_normal_id=?",(values[0],ident))
    return {'id':ident}

def mensagem(texto,nome):
    if '{primeiro_nome}' in texto:
        if not isinstance(nome,str) or not nome.strip():raise HTTPException(400,'O modelo exige o nome do lead para preencher {primeiro_nome}.')
        texto=texto.replace('{primeiro_nome}',nome.strip().split()[0].capitalize())
    return texto

def validar_numero(c,body):
    for k in ('limite_diario','limite_hora'):
        if type(body.get(k)) is not int or not 0<=body[k]<=100000:raise HTTPException(400,'Limites devem ser inteiros de 0 a 100000. Zero impede disparos.')
    if type(body.get('modelo_id')) is not int:raise HTTPException(400,'Escolha um modelo de mensagem.')
    m=c.execute('SELECT * FROM modelos_normais WHERE id=? AND ativo=1',(body['modelo_id'],)).fetchone()
    if not m:raise HTTPException(400,'Escolha um modelo ativo.')
    if 'ativo' in body and type(body['ativo']) is not bool:raise HTTPException(400,'Ativação inválida.')
    return m

def padrao(get_db,cfg):
    with closing(get_db()) as c:
        r=c.execute('SELECT modelo_id,limite_diario,limite_hora FROM padrao_conexoes_normais WHERE conta=?',(str(cfg['vendeai_account_id']),)).fetchone()
        if r:return dict(r)
        modelos=c.execute('SELECT id FROM modelos_normais WHERE ativo=1').fetchall()
        return {'modelo_id':modelos[0]['id'] if len(modelos)==1 else None,'limite_diario':0,'limite_hora':0}


def salvar_padrao(get_db,cfg,body):
    with closing(get_db()) as c,c:
        validar_numero(c,body)
        c.execute('INSERT INTO padrao_conexoes_normais VALUES(?,?,?,?) ON CONFLICT(conta) DO UPDATE SET modelo_id=excluded.modelo_id,limite_diario=excluded.limite_diario,limite_hora=excluded.limite_hora',
            (str(cfg['vendeai_account_id']),body['modelo_id'],body['limite_diario'],body['limite_hora']))
    return padrao(get_db,cfg)


def inserir_numero(c,account,i,m,body):
    inbox=i['inbox_id'];name=(i['nome']+' '+i['telefone']).strip()
    return c.execute("INSERT INTO numeros(nome,phone_number_id,access_token,tipo_envio,vendeai_conta,vendeai_inbox_id,modelo_normal_id,template_nome,template_sem_variaveis,ativo,limite_diario,limite_hora,conexao_status,meta_consultado_em) VALUES(?,?,'','vendeai_normal',?,?,?,?,1,1,?,?,?,?)",(name,'vendeai:'+account+':'+inbox,account,inbox,m['id'],m['nome'],body['limite_diario'],body['limite_hora'],i['status'],i['consultado_em'])).lastrowid


def importar_todas(get_db,cfg,token):
    account,_=credenciais(cfg,token)
    descobrir(get_db,cfg,token)
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        body=padrao(get_db,cfg);m=validar_numero(c,body)
        items=c.execute("SELECT * FROM conexoes_vendeai v WHERE conta=? AND status='connected' AND NOT EXISTS(SELECT 1 FROM numeros n WHERE n.tipo_envio='vendeai_normal' AND n.vendeai_conta=v.conta AND n.vendeai_inbox_id=v.inbox_id)",(account,)).fetchall()
        ids=[inserir_numero(c,account,i,m,body) for i in items]
    return {'adicionados':len(ids),'ids':ids}


def importar(get_db,cfg,token,body):
    body={**padrao(get_db,cfg),**body}
    account,_=credenciais(cfg,token);inbox=str(body.get('inbox_id') or '')
    if not inbox.isdigit():raise HTTPException(400,'Selecione uma conexão.')
    descobrir(get_db,cfg,token)
    with closing(get_db()) as c,c:
        m=validar_numero(c,body)
        i=c.execute('SELECT * FROM conexoes_vendeai WHERE conta=? AND inbox_id=?',(account,inbox)).fetchone()
        if not i or i['status']!='connected':raise HTTPException(400,'Conexão não confirmada. Atualize a lista antes de adicionar.')
        if c.execute("SELECT 1 FROM numeros WHERE tipo_envio='vendeai_normal' AND vendeai_conta=? AND vendeai_inbox_id=?",(account,inbox)).fetchone():raise HTTPException(409,'Este número já foi adicionado.')
        try:
            ident=inserir_numero(c,account,i,m,body)
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
    if atual['limite_diario']<=0 or atual['limite_hora']<=0:return False,'SEM_CAPACIDADE: configure limites maiores que zero',''
    try:msg=mensagem(m['texto'],nome)
    except HTTPException as e:return False,'BLOQUEADO_MODELO: '+e.detail,''
    if consultar(get_db,atual,cfg,token)!='connected':return False,'CONEXAO_INDISPONIVEL: sem conexão confirmada na VendeAI',''
    # Consulta externa pode demorar: revalida pausa e supressão antes do POST.
    with closing(get_db()) as c:
        a=c.execute('SELECT n.ativo,n.limite_diario,n.limite_hora,m.texto,m.imagem FROM numeros n JOIN modelos_normais m ON m.id=n.modelo_normal_id AND m.ativo=1 WHERE n.id=?',(n['id'],)).fetchone()
    if not a or not a['ativo'] or a['limite_diario']<=0 or a['limite_hora']<=0 or suprimido(phone):return False,'BLOQUEADO_MODELO: envio bloqueado por pausa, limites ou lista negra',''
    try: msg=mensagem(a['texto'],nome)
    except HTTPException as e:return False,'BLOQUEADO_MODELO: '+e.detail,''
    account,token=credenciais(cfg,token)
    if a['imagem']:
        return enviar_imagem(get_db,atual,phone,nome,msg,a['imagem'],account,token,suprimido)
    try:
        r=requests.post(API+'send/',json={'account_id':account,'crm_api_access_token':token,'inbox_id':atual['vendeai_inbox_id'],'customer_phone':'+'+phone,'payload':{'type':'whatsapp_nao_oficial','message':msg}},timeout=(5,30),allow_redirects=False)
        if r.status_code==200:
            d=r.json()
            if d.get('status')=='success' and d.get('message_id'):
                agendar_etiqueta(get_db,account,str(d['message_id']),atual['vendeai_inbox_id'],phone,atual['id'])
                return True,'',str(d['message_id'])
            return False,'ENVIO_INCERTO: VendeAI não confirmou o identificador da mensagem',''
        if r.status_code>=500 or 300<=r.status_code<400:return False,'ENVIO_INCERTO: resposta VendeAI HTTP '+str(r.status_code),''
        return False,'VendeAI recusou o envio: HTTP '+str(r.status_code),''
    except (requests.RequestException,ValueError,TypeError,AttributeError):return False,'ENVIO_INCERTO: conferir na VendeAI antes de repetir',''


def enviar_imagem(get_db,n,phone,nome,msg,imagem,account,token,suprimido):
    if len(msg)>1024:return False,'BLOQUEADO_MODELO: legenda da imagem excede 1024 caracteres',''
    base=CRM+account;headers={'api_access_token':token};post_iniciado=False
    def get(path):
        r=requests.get(base+path,headers=headers,timeout=(3,15),allow_redirects=False)
        r.raise_for_status();return r.json()
    def post(path,body):
        r=requests.post(base+path,headers=headers,json=body,timeout=(5,20),allow_redirects=False)
        r.raise_for_status();return r.json()
    try:
        with closing(get_db()) as c:
            media=c.execute("SELECT dados,mime FROM imagens_template WHERE id=? AND phone_id='modelo-normal'",(imagem,)).fetchone()
        if not media:raise ValueError('Imagem indisponível')
        r=requests.get(base+'/contacts/search',headers=headers,params={'q':phone[-11:]},timeout=(3,15),allow_redirects=False)
        r.raise_for_status()
        contatos=[x for x in r.json().get('payload',[]) if re.sub(r'\D','',x.get('phone_number') or '')==phone]
        if len(contatos)>1:raise ValueError('Contato ambíguo')
        if contatos:
            contato=contatos[0]
        else:
            d=post('/contacts',{'inbox_id':int(n['vendeai_inbox_id']),'name':nome,'phone_number':'+'+phone})
            p=d.get('payload',d);contato=p.get('contact',p)
        ident=str(contato['id'])
        if not ident.isdigit():raise ValueError('Contato inválido')
        conversas=get('/contacts/'+ident+'/conversations').get('payload',[])
        candidatas=[x for x in conversas if str(x.get('inbox_id'))==n['vendeai_inbox_id'] and str(x.get('id','')).isdigit()]
        if candidatas:
            chat=str(max(candidatas,key=lambda x:int(x['id']))['id'])
        else:
            links=contato.get('contact_inboxes',[])
            link=next((x for x in links if str(x.get('inbox',{}).get('id'))==n['vendeai_inbox_id']),None)
            if not link:link=post('/contacts/'+ident+'/contact_inboxes',{'inbox_id':int(n['vendeai_inbox_id']),'source_id':phone})
            if not link.get('source_id'):raise ValueError('Vínculo indisponível')
            d=post('/conversations',{'contact_id':int(ident),'inbox_id':int(n['vendeai_inbox_id']),'source_id':link['source_id'],'status':'open'})
            chat=str(d['id'])
        if not chat.isdigit():raise ValueError('Conversa inválida')
        with closing(get_db()) as c:
            atual=c.execute('SELECT n.*,m.texto,m.imagem,m.ativo AS modelo_ativo FROM numeros n JOIN modelos_normais m ON m.id=n.modelo_normal_id WHERE n.id=?',(n['id'],)).fetchone()
            cfg=c.execute('SELECT vendeai_account_id,rodizio_tipo FROM configuracao WHERE id=1').fetchone()
        if not atual or not atual['ativo'] or not atual['modelo_ativo'] or atual['limite_diario']<=0 or atual['limite_hora']<=0 or atual['conexao_status']!='connected' or atual['imagem']!=imagem or atual['vendeai_conta']!=account or atual['vendeai_inbox_id']!=n['vendeai_inbox_id'] or atual['modelo_normal_id']!=n['modelo_normal_id'] or str(cfg['vendeai_account_id'])!=account or cfg['rodizio_tipo']=='oficiais' or suprimido(phone):
            return False,'BLOQUEADO_MODELO: configuração alterada ou contato bloqueado',''
        if mensagem(atual['texto'],nome)!=msg:return False,'BLOQUEADO_MODELO: modelo alterado durante envio',''
        post_iniciado=True
        r=requests.post(base+'/conversations/'+chat+'/messages',headers=headers,
            data={'content':msg,'message_type':'outgoing','private':'false'},
            files={'attachments[]':('imagem.png' if media['mime']=='image/png' else 'imagem.jpg',media['dados'],media['mime'])},
            timeout=(5,45),allow_redirects=False)
        if r.status_code>=500 or 300<=r.status_code<400:return False,'ENVIO_INCERTO: conferir imagem na VendeAI antes de repetir',''
        if r.status_code not in (200,201):return False,'CRM recusou a imagem: HTTP '+str(r.status_code),''
        d=r.json()
        if not d.get('id') or not d.get('attachments'):return False,'ENVIO_INCERTO: CRM não confirmou a imagem',''
        if d.get('status')=='failed':return False,'CRM informou falha no envio da imagem',''
        agendar_etiqueta(get_db,account,str(d['id']),n['vendeai_inbox_id'],phone,n['id'])
        return True,'',str(d['id'])
    except (requests.RequestException,ValueError,KeyError,TypeError,AttributeError,HTTPException):
        return False,('ENVIO_INCERTO: conferir imagem na VendeAI antes de repetir' if post_iniciado else 'CONEXAO_INDISPONIVEL: não foi possível preparar o envio da imagem'),''


def agendar_etiqueta(get_db,account,message_id,inbox,telefone,numero_id):
    with closing(get_db()) as c,c:
        c.execute('INSERT OR IGNORE INTO etiquetas_disparo(account_id,message_id,inbox_id,telefone,numero_id) VALUES(?,?,?,?,?)',
                  (account,message_id,inbox,telefone,numero_id))


def _consultar_crm(url,headers,params=None):
    r=requests.get(url,headers=headers,params=params,timeout=(3,10),allow_redirects=False)
    if r.status_code!=200: raise ValueError('Consulta CRM HTTP '+str(r.status_code))
    return r.json()


def _localizar_conversa(t,base,headers):
    # Só a mensagem confirmada identifica o chat; nunca escolher o mais recente.
    busca=_consultar_crm(base+'/contacts/search',headers,{'q':t['telefone'][-11:]})
    for contato in busca.get('payload',[]):
        if re.sub(r'\D','',contato.get('phone_number') or '')!=t['telefone']:continue
        ident=str(contato.get('id',''))
        if not ident.isdigit():continue
        conversas=_consultar_crm(base+'/contacts/'+ident+'/conversations',headers)
        for conversa in conversas.get('payload',[]):
            chat=str(conversa.get('id',''))
            if str(conversa.get('inbox_id'))!=t['inbox_id'] or not chat.isdigit():continue
            before=None
            for _ in range(10):
                pagina=_consultar_crm(base+'/conversations/'+chat+'/messages',headers,{'before':before} if before else None)
                mensagens=pagina.get('payload',[])
                if not isinstance(mensagens,list):raise ValueError('Lista de mensagens inválida')
                if any(str(m.get('id'))==t['message_id'] and str(m.get('conversation_id'))==chat for m in mensagens):return chat
                ids=[int(m['id']) for m in mensagens if str(m.get('id','')).isdigit()]
                if not ids or (before is not None and min(ids)>=before):break
                before=min(ids)
    raise ValueError('Conversa da mensagem ainda não localizada')


def processar_etiqueta(get_db,cfg,token):
    with closing(get_db()) as c:
        row=c.execute("SELECT * FROM etiquetas_disparo WHERE estado='pendente' AND tentar_em<=? ORDER BY tentar_em LIMIT 1",(time.time(),)).fetchone()
    if not row:return False
    t=dict(row);tentativas=t['tentativas']+1;chat=t['chat_id'];detalhe='';estado_final='aplicada'
    try:
        account,token=credenciais(cfg,token)
        if account!=t['account_id']:raise ValueError('Conta VendeAI alterada; etiqueta não aplicada')
        base=CRM+account;headers={'api_access_token':token}
        if not chat:
            chat=_localizar_conversa(t,base,headers)
            with closing(get_db()) as c,c:
                c.execute('UPDATE etiquetas_disparo SET chat_id=? WHERE account_id=? AND message_id=?',(chat,account,t['message_id']))
        # GET imediatamente antes do POST, pois a API substitui a lista inteira.
        labels=_consultar_crm(base+'/conversations/'+chat+'/labels',headers).get('payload')
        if not isinstance(labels,list) or not all(isinstance(v,str) for v in labels):raise ValueError('Etiquetas atuais não confirmadas')
        if 'disparo' not in labels:
            r=requests.post(base+'/conversations/'+chat+'/labels',headers=headers,json={'labels':list(dict.fromkeys(labels+['disparo']))},timeout=(3,10),allow_redirects=False)
            if r.status_code!=200:raise ValueError('Marcação CRM HTTP '+str(r.status_code))
            confirmado=_consultar_crm(base+'/conversations/'+chat+'/labels',headers).get('payload')
            if not isinstance(confirmado,list) or 'disparo' not in confirmado:raise ValueError('Etiqueta não confirmada pelo CRM')
    except HTTPException:
        detalhe='Credencial VendeAI indisponível'
    except requests.RequestException:
        detalhe='Falha de comunicação com o CRM'
    except (ValueError,KeyError,TypeError,AttributeError):
        detalhe='Não foi possível localizar ou etiquetar a conversa; conferir no CRM'
    if detalhe:estado_final='pendente' if tentativas<3 else 'erro'
    with closing(get_db()) as c,c:
        c.execute('UPDATE etiquetas_disparo SET estado=?,tentativas=?,tentar_em=?,detalhe=? WHERE account_id=? AND message_id=?',
                  (estado_final,tentativas,time.time()+5*2**tentativas,detalhe,t['account_id'],t['message_id']))
        if estado_final=='erro':
            c.execute('INSERT INTO eventos(numero_id,tipo,detalhe,criado_em) VALUES(?,?,?,?)',
                      (t['numero_id'],'erro_etiqueta_disparo','Etiqueta disparo pendente de revisão. Mensagem '+t['message_id']+'; conversa '+str(chat or 'não identificada')+'. O envio não será repetido.',agora()))
    return True
