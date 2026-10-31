"""Arquivo versionado de conversas. Armazenamento local não é backup externo."""
import asyncio, hashlib, hmac, io, json, os, re, secrets, time, zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo
import requests
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

CRM='https://crm.vendeaitecnologia.com.br'
HOSTS={'crm.vendeaitecnologia.com.br','app.vendeaitecnologia.com.br'}
TZ=ZoneInfo('America/Sao_Paulo')

def migrar(c):
    schema='''
    CREATE TABLE IF NOT EXISTS dossies_config(id INTEGER PRIMARY KEY,automatico INTEGER NOT NULL DEFAULT 0);
    INSERT OR IGNORE INTO dossies_config VALUES(1,0);
    CREATE TABLE IF NOT EXISTS dossies_jobs(id TEXT PRIMARY KEY,conta TEXT NOT NULL,chat TEXT NOT NULL,dono TEXT NOT NULL,estado TEXT NOT NULL,criado REAL NOT NULL,detalhe TEXT NOT NULL DEFAULT '',versao_id TEXT,tentativas INTEGER NOT NULL DEFAULT 0,tentar_em REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS dossies_versoes(id TEXT PRIMARY KEY,conta TEXT NOT NULL,chat TEXT NOT NULL,versao INTEGER NOT NULL,fingerprint TEXT NOT NULL,sha256 TEXT NOT NULL,criado REAL NOT NULL,mensagens INTEGER NOT NULL,anexos INTEGER NOT NULL,pendencias TEXT NOT NULL,UNIQUE(conta,chat,versao));
    CREATE TABLE IF NOT EXISTS dossies_copias(versao_id TEXT NOT NULL,provedor TEXT NOT NULL,chave TEXT NOT NULL,sha256 TEXT NOT NULL,verificado_em REAL,PRIMARY KEY(versao_id,provedor));
    CREATE TABLE IF NOT EXISTS dossies_usuarios(id TEXT PRIMARY KEY,nome TEXT NOT NULL UNIQUE,senha TEXT NOT NULL,ativo INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS dossies_permissoes(usuario TEXT NOT NULL,conta TEXT NOT NULL,chat TEXT NOT NULL,PRIMARY KEY(usuario,conta,chat));
    CREATE TABLE IF NOT EXISTS dossies_sessoes(hash TEXT PRIMARY KEY,usuario TEXT NOT NULL,expira REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS dossies_auditoria(id INTEGER PRIMARY KEY,usuario TEXT NOT NULL,acao TEXT NOT NULL,referencia TEXT NOT NULL,criado REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS dossies_gatilhos(conta TEXT NOT NULL,proposta TEXT NOT NULL,PRIMARY KEY(conta,proposta));
    CREATE TABLE IF NOT EXISTS dossies_login_limite(chave TEXT PRIMARY KEY,tentativas INTEGER NOT NULL,inicio REAL NOT NULL);
    '''
    for statement in schema.split(';'):
        if statement.strip():c.execute(statement)

def auditar(c,u,a,ref):
    c.execute('INSERT INTO dossies_auditoria(usuario,acao,referencia,criado) VALUES(?,?,?,?)',(u,a,ref,time.time()))

def referencia(s,conta):
    s=str(s).strip()
    if re.fullmatch(r'[1-9][0-9]{0,14}',s):return s
    p=urlparse(s)
    if p.scheme=='https' and p.hostname in HOSTS:
        m=re.fullmatch(r'/(?:app/accounts|chats)/(\d+)/(?:inbox/\d+/)?conversations/(\d+)/?',p.path)
        if m and m[1]==str(conta):return m[2]
    raise HTTPException(400,'Informe o número ou link da conversa desta conta.')

def senha_hash(s,salt=None):
    salt=salt or secrets.token_hex(16)
    return salt+':'+hashlib.pbkdf2_hmac('sha256',s.encode(),salt.encode(),310000).hex()

def permitido(c,u,conta,chat):
    return u=='admin' or c.execute('SELECT 1 FROM dossies_permissoes WHERE usuario=? AND conta=? AND chat=?',(u,conta,chat)).fetchone() is not None

def agendar(c,conta,chat,dono):
    old=c.execute("SELECT id FROM dossies_jobs WHERE conta=? AND chat=? AND dono=? AND estado IN ('pendente','coletando')",(conta,chat,dono)).fetchone()
    if old:return old[0]
    ident=secrets.token_hex(16)
    c.execute('INSERT INTO dossies_jobs(id,conta,chat,dono,estado,criado) VALUES(?,?,?,?,?,?)',(ident,conta,chat,dono,'pendente',time.time()))
    auditar(c,dono,'solicitar',ident)
    return ident

def pagamento(c,d):
    cfg=c.execute('SELECT automatico FROM dossies_config WHERE id=1').fetchone()
    if not cfg or not cfg[0]:return
    p=d.get('proposal') or d.get('proposal_status') or {};chat=d.get('chat_summary') or {}
    if not isinstance(p,dict) or (p.get('proposal_status') or p.get('status'))!='LIQUIDATED_TO_CUSTOMER':return
    conta=str(chat.get('account_id') or '');cid=str(chat.get('chat_id') or '');pid=str(p.get('proposal_id') or '')
    if not conta or not cid.isdigit() or not pid:return
    r=c.execute('INSERT OR IGNORE INTO dossies_gatilhos VALUES(?,?)',(conta,pid))
    if r.rowcount:agendar(c,conta,cid,'admin')

class LocalStorage:
    """Contrato de armazenamento: put/get; futuras cópias B2/Drive são verificadas à parte."""
    def __init__(self,root):self.root=Path(root)
    def path(self,key):
        if not re.fullmatch(r'[a-f0-9]{32}\.zip',key):raise ValueError('Chave inválida')
        return self.root/key
    def put(self,key,data):
        self.root.mkdir(parents=True,exist_ok=True)
        p=self.path(key)
        with p.open('xb') as f:f.write(data)
        os.chmod(p,0o600)
        digest=hashlib.sha256(data).hexdigest()
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('Integridade')
        return digest

def ler_api(base,token,path,raw):
    r=requests.get(base+path,headers={'api_access_token':token},timeout=(5,30),allow_redirects=False)
    if r.status_code!=200:raise RuntimeError('CRM respondeu HTTP '+str(r.status_code))
    if len(r.content)>20_000_000:raise RuntimeError('Resposta acima do limite')
    if sum(map(len,raw.values()))+len(r.content)>100_000_000:raise RuntimeError('Histórico acima do limite de coleta')
    key='raw/api-%04d.json'%len(raw)
    raw[key]=r.content
    raw[key+'.origem.json']=json.dumps({'endpoint':base+path,'coletado_em':datetime.now(timezone.utc).isoformat(),'http_status':r.status_code}).encode()
    return r.json()

def baixar(url):
    # Apenas hosts explícitos; nunca encaminhar o token CRM para mídia ou redirects.
    allowed=HOSTS|{x.strip().lower() for x in os.getenv('DOSSIE_MEDIA_HOSTS','').split(',') if x.strip()}
    for _ in range(6):
        p=urlparse(url)
        if p.scheme!='https' or p.hostname not in allowed or p.username or p.port not in (None,443):raise RuntimeError('Host de mídia não autorizado')
        with requests.get(url,timeout=(5,30),stream=True,allow_redirects=False) as r:
            if r.status_code in (301,302,303,307,308):
                from urllib.parse import urljoin
                url=urljoin(url,r.headers.get('Location',''));continue
            if r.status_code!=200:raise RuntimeError('Mídia HTTP '+str(r.status_code))
            b=bytearray()
            for chunk in r.iter_content(65536):
                b.extend(chunk)
                if len(b)>50_000_000:raise RuntimeError('Anexo acima de 50 MB')
            return bytes(b)
    raise RuntimeError('Redirecionamentos em excesso')

def pdf(conversa,msgs,interno=False):
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.pagesizes import A4
    styles=getSampleStyleSheet();out=io.BytesIO();story=[]
    def texto(s,style='BodyText'):
        # Quebra parágrafos extensos para preservar todas as mensagens no PDF.
        for i in range(0,len(str(s)) or 1,2000):story.append(Paragraph(escape(str(s)[i:i+2000]).replace('\n','<br/>'),styles[style]))
        story.append(Spacer(1,8))
    texto(('Registros internos' if interno else 'Conversa')+' #'+str(conversa.get('id','')),'Title')
    texto('Datas em horário de Brasília. Originais e anexos no pacote. Remetentes conforme informados pelo CRM.')
    for m in msgs:
        private=bool(m.get('private')) or m.get('message_type') in (2,'activity')
        if private!=interno:continue
        stamp=m.get('created_at');dt=datetime.fromtimestamp(stamp,TZ).strftime('%d/%m/%Y %H:%M:%S') if isinstance(stamp,(int,float)) else str(stamp or '')
        sender=m.get('sender') or {};name=sender.get('name') or ('Cliente' if m.get('message_type') in (0,'incoming') else 'Remetente não informado')
        texto(f"{dt} | {name} | Mensagem {m.get('id')}",'Heading3')
        texto(m.get('content') or '[Sem texto]')
        for a in m.get('attachments') or []:texto('Anexo '+str(a.get('id'))+' · '+str(a.get('file_type') or 'arquivo')+' — consulte arquivos/ e manifesto.json')
    if not story: texto('Sem registros.')
    SimpleDocTemplate(out,pagesize=A4,rightMargin=40,leftMargin=40,topMargin=40,bottomMargin=40).build(story)
    return out.getvalue()

def coletar(cfg,token,chat):
    base=CRM+'/api/v1/accounts/'+str(cfg['vendeai_account_id']);raw={};sources={}
    conv=ler_api(base,token,'/conversations/'+chat,raw)
    msgs={};before=None
    for _ in range(5000):
        path='/conversations/'+chat+'/messages'+('?before='+str(before) if before else '')
        page=ler_api(base,token,path,raw);items=page.get('payload') if isinstance(page,dict) else page
        if not isinstance(items,list):raise RuntimeError('Formato de mensagens desconhecido')
        if not items:break
        ids=[int(m['id']) for m in items];old=min(ids)
        if before is not None and old>=before:raise RuntimeError('Paginação sem avanço')
        for m in items:msgs[int(m['id'])]=m
        before=old
    else:raise RuntimeError('Paginação incompleta: limite atingido')
    inbox=conv.get('inbox_id')
    if inbox:ler_api(base,token,'/inboxes/'+str(inbox),raw)
    sender=(conv.get('meta') or {}).get('sender') or {}
    if sender.get('id'):ler_api(base,token,'/contacts/'+str(sender['id']),raw)
    ordered=sorted(msgs.values(),key=lambda m:int(m['id']));files=dict(raw);missing=[];count=0;size=sum(map(len,files.values()))
    for m in ordered:
        for a in m.get('attachments') or []:
            suffix=Path(urlparse(a.get('data_url') or '').path).suffix.lower()
            if not re.fullmatch(r'\.[a-z0-9]{1,8}',suffix):suffix='.bin'
            key=f"arquivos/m{int(m['id'])}-a{int(a.get('id',count))}{suffix}"
            try:
                data=baixar(a.get('data_url') or '')
                size+=len(data)
                if size>250_000_000:raise RuntimeError('Pacote acima de 250 MB')
                files[key]=data;sources[key]={'mensagem':m['id'],'anexo':a.get('id')};count+=1
            except Exception as e:missing.append({'mensagem':m['id'],'anexo':a.get('id'),'erro':str(e) if isinstance(e,RuntimeError) else 'Falha ao baixar arquivo'})
    files['conversa.pdf']=pdf(conv,ordered)
    files['anexo_interno.pdf']=pdf(conv,ordered,True)
    fingerprint=hashlib.sha256(json.dumps({'conversa':conv,'mensagens':ordered,'anexos':{k:hashlib.sha256(v).hexdigest() for k,v in files.items() if k.startswith('arquivos/')},'pendencias':missing},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return files,missing,len(ordered),count,fingerprint,sources

def processar(db,cfg,token,storage):
    with closing(db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM dossies_jobs WHERE estado='pendente' AND tentar_em<=? ORDER BY criado LIMIT 1",(time.time(),)).fetchone()
        if not row:return False
        job=dict(row);c.execute("UPDATE dossies_jobs SET estado='coletando',tentativas=tentativas+1 WHERE id=?",(job['id'],))
    try:
        if job['conta']!=str(cfg.get('vendeai_account_id')):raise RuntimeError('Conta do CRM mudou')
        if not token:raise RuntimeError('Token CRM não configurado')
        import shutil
        if shutil.disk_usage(storage.root.parent).free<2_000_000_000:raise RuntimeError('Espaço insuficiente para uma coleta segura')
        files,missing,n,a,fp,sources=coletar(cfg,token,job['chat'])
        with closing(db()) as c:
            events=c.execute('SELECT payload_raw FROM vendeai_eventos WHERE account_id=? AND chat_id=? AND conta_confirmada=1 ORDER BY id',(job['conta'],job['chat'])).fetchall()
        for i,e in enumerate(events):files[f'raw/webhook-{i:05d}.json']=e[0].encode() if isinstance(e[0],str) else e[0]
        fp=hashlib.sha256((fp+''.join(hashlib.sha256(v).hexdigest() for k,v in sorted(files.items()) if k.startswith('raw/webhook-'))).encode()).hexdigest()
        with closing(db()) as c:
            old=c.execute('SELECT * FROM dossies_versoes WHERE conta=? AND chat=? ORDER BY versao DESC LIMIT 1',(job['conta'],job['chat'])).fetchone()
        if old and old['fingerprint']==fp:
            ident=old['id']
        else:
            ident=secrets.token_hex(16);version=old['versao']+1 if old else 1
            manifest={'versao_formato':1,'conta':job['conta'],'conversa':job['chat'],'versao':version,'coletado_em':datetime.now(timezone.utc).isoformat(),'origem':CRM,'pendencias':missing,'arquivos':{k:{'sha256':hashlib.sha256(v).hexdigest(),'bytes':len(v),**sources.get(k,{})} for k,v in files.items()}}
            files['manifesto.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
            out=io.BytesIO()
            with zipfile.ZipFile(out,'w',zipfile.ZIP_STORED) as z:
                for k,v in files.items():z.writestr(k,v)
            sha=storage.put(ident+'.zip',out.getvalue())
            with closing(db()) as c,c:
                c.execute('INSERT INTO dossies_versoes VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,job['conta'],job['chat'],version,fp,sha,time.time(),n,a,json.dumps(missing)))
                c.execute('INSERT INTO dossies_copias VALUES(?,?,?,?,?)',(ident,'local',ident+'.zip',sha,time.time()))
        with closing(db()) as c,c:
            c.execute("UPDATE dossies_jobs SET estado=?,versao_id=?,detalhe=? WHERE id=?",('com_pendencias' if missing else 'concluido',ident,str(len(missing))+' anexo(s) pendente(s)' if missing else '',job['id']))
    except Exception as e:
        with closing(db()) as c,c:
            c.execute('UPDATE dossies_jobs SET estado=?,detalhe=?,tentar_em=? WHERE id=?',('pendente' if job['tentativas']<2 else 'erro',str(e)[:180] if isinstance(e,RuntimeError) else 'Falha na coleta; tente novamente.',time.time()+60*2**job['tentativas'],job['id']))
    return True

def instalar(main):
    app=main.app;db=main.get_db
    storage=LocalStorage(main.DB_FILE.parent/'dossies')
    def usuario(r):
        if r.cookies.get('session_token') in main.sessoes_ativas:return 'admin'
        token=r.cookies.get('dossie_session','')
        with closing(db()) as c:
            u=c.execute('SELECT s.usuario FROM dossies_sessoes s JOIN dossies_usuarios u ON u.id=s.usuario WHERE s.hash=? AND s.expira>? AND u.ativo=1',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        if not u:raise HTTPException(401,'Entre com seu acesso de dossiês.')
        return u[0]
    def admin(r):
        if usuario(r)!='admin':raise HTTPException(403,'Acesso exclusivo do administrador.')
    def escrita(r):
        if r.headers.get('origin') and urlparse(r.headers['origin']).netloc!=r.headers.get('host'):raise HTTPException(403,'Origem inválida')
        if r.headers.get('x-dossie-action')!='1':raise HTTPException(403,'Solicitação inválida')
    @app.get('/dossies',response_class=HTMLResponse)
    def pagina(request:Request):return main.templates.TemplateResponse('dossies.html',{'request':request})
    @app.post('/dossies/api/login')
    async def login(request:Request):
        escrita(request);b=await request.json();name=str(b.get('nome','')).strip().lower();pw=str(b.get('senha',''))
        if len(pw)>256:raise HTTPException(400,'Senha inválida')
        key=hashlib.sha256(name.encode()).hexdigest()
        with closing(db()) as c,c:
            row=c.execute('SELECT * FROM dossies_login_limite WHERE chave=?',(key,)).fetchone()
            if row and row['inicio']>time.time()-900 and row['tentativas']>=5:raise HTTPException(429,'Aguarde 15 minutos para tentar novamente.')
            c.execute('INSERT INTO dossies_login_limite VALUES(?,1,?) ON CONFLICT(chave) DO UPDATE SET tentativas=CASE WHEN inicio<? THEN 1 ELSE tentativas+1 END,inicio=CASE WHEN inicio<? THEN excluded.inicio ELSE inicio END',(key,time.time(),time.time()-900,time.time()-900))
        with closing(db()) as c,c:
            u=c.execute('SELECT * FROM dossies_usuarios WHERE nome=? AND ativo=1',(name,)).fetchone()
            check=senha_hash(pw,u['senha'].split(':')[0] if u else '0'*32)
            if not u or not hmac.compare_digest(check,u['senha']):raise HTTPException(401,'Login ou senha incorretos')
            token=secrets.token_urlsafe(32);c.execute('INSERT INTO dossies_sessoes VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),u['id'],time.time()+28800));c.execute('DELETE FROM dossies_login_limite WHERE chave=?',(key,));auditar(c,u['id'],'login','')
        response=JSONResponse({'ok':True});response.set_cookie('dossie_session',token,httponly=True,secure=True,samesite='strict',max_age=28800,path='/dossies');return response
    @app.get('/dossies/api/lista')
    def lista(request:Request):
        u=usuario(request)
        with closing(db()) as c:
            rows=c.execute('SELECT j.id,j.chat,j.estado,j.criado,j.detalhe,j.versao_id,v.versao,v.mensagens,v.anexos,v.sha256 FROM dossies_jobs j LEFT JOIN dossies_versoes v ON v.id=j.versao_id WHERE (?=\'admin\' OR (j.dono=? AND EXISTS(SELECT 1 FROM dossies_permissoes p WHERE p.usuario=? AND p.conta=j.conta AND p.chat=j.chat))) ORDER BY j.criado DESC LIMIT 100',(u,u,u)).fetchall()
            auto=c.execute('SELECT automatico FROM dossies_config WHERE id=1').fetchone()[0]
            users=[dict(x) for x in c.execute('SELECT id,nome,ativo FROM dossies_usuarios')] if u=='admin' else []
        return {'admin':u=='admin','automatico':bool(auto),'itens':[dict(x) for x in rows],'usuarios':users}
    @app.post('/dossies/api/gerar')
    async def gerar(request:Request):
        escrita(request);u=usuario(request);b=await request.json();conta=str(main.carregar_configuracao()['vendeai_account_id']);chat=referencia(b.get('conversa',''),conta)
        with closing(db()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            if not permitido(c,u,conta,chat):raise HTTPException(403,'Conversa não autorizada. Peça acesso ao administrador.')
            if c.execute("SELECT count(*) FROM dossies_jobs WHERE estado IN ('pendente','coletando')").fetchone()[0]>=100:raise HTTPException(429,'Fila cheia; aguarde as coletas.')
            ident=agendar(c,conta,chat,u)
        return {'id':ident}
    @app.post('/dossies/api/config')
    async def config(request:Request):
        escrita(request);admin(request);b=await request.json()
        if type(b.get('automatico')) is not bool:raise HTTPException(400,'Valor inválido')
        with closing(db()) as c,c:c.execute('UPDATE dossies_config SET automatico=? WHERE id=1',(int(b['automatico']),));auditar(c,'admin','automatico',str(b['automatico']))
        return {'ok':True}
    @app.post('/dossies/api/usuarios')
    async def users(request:Request):
        escrita(request);admin(request);b=await request.json();name=str(b.get('nome','')).strip().lower();pw=str(b.get('senha',''));chats=b.get('conversas',[]);conta=str(main.carregar_configuracao()['vendeai_account_id'])
        if not re.fullmatch(r'[a-z0-9_.@-]{3,80}',name) or name=='admin' or not 12<=len(pw)<=256:raise HTTPException(400,'Use login de 3–80 caracteres e senha com pelo menos 12 caracteres.')
        if not isinstance(chats,list) or len(chats)>500:raise HTTPException(400,'Lista inválida')
        chats=[referencia(x,conta) for x in chats]
        with closing(db()) as c,c:
            old=c.execute('SELECT id FROM dossies_usuarios WHERE nome=?',(name,)).fetchone();uid=old[0] if old else secrets.token_hex(16)
            c.execute('INSERT INTO dossies_usuarios VALUES(?,?,?,1) ON CONFLICT(nome) DO UPDATE SET senha=excluded.senha,ativo=1',(uid,name,senha_hash(pw)))
            c.execute('DELETE FROM dossies_sessoes WHERE usuario=?',(uid,));c.execute('DELETE FROM dossies_permissoes WHERE usuario=?',(uid,))
            c.executemany('INSERT INTO dossies_permissoes VALUES(?,?,?)',[(uid,conta,x) for x in chats]);auditar(c,'admin','configurar_usuario',uid)
        return {'ok':True}
    @app.post('/dossies/api/revogar/{uid}')
    def revoke(uid:str,request:Request):
        escrita(request);admin(request)
        with closing(db()) as c,c:c.execute('UPDATE dossies_usuarios SET ativo=0 WHERE id=?',(uid,));c.execute('DELETE FROM dossies_sessoes WHERE usuario=?',(uid,));auditar(c,'admin','revogar',uid)
        return {'ok':True}
    @app.get('/dossies/api/baixar/{jid}')
    def download(jid:str,request:Request):
        u=usuario(request)
        with closing(db()) as c,c:
            j=c.execute('SELECT * FROM dossies_jobs WHERE id=?',(jid,)).fetchone()
            if not j or (u!='admin' and j['dono']!=u) or not permitido(c,u,j['conta'],j['chat']):raise HTTPException(404,'Dossiê não encontrado')
            if not j['versao_id']:raise HTTPException(409,'Dossiê ainda não está pronto')
            v=c.execute('SELECT * FROM dossies_versoes WHERE id=?',(j['versao_id'],)).fetchone();p=storage.path(v['id']+'.zip')
            if not p.exists():raise HTTPException(503,'Arquivo indisponível')
            if hashlib.sha256(p.read_bytes()).hexdigest()!=v['sha256']:raise HTTPException(503,'Verificação de integridade falhou')
            auditar(c,u,'download',jid)
        return FileResponse(p,media_type='application/zip',filename=f"conversa-{j['chat']}-v{v['versao']}.zip",headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    async def worker():
        while True:
            try:
                cfg=main.carregar_configuracao();token=cfg.get('vendeai_mailing_token') or main.VENDEAI_API_TOKEN
                fez=await asyncio.to_thread(processar,db,cfg,token,storage)
            except Exception:fez=False
            await asyncio.sleep(2 if fez else 10)
    @app.on_event('startup')
    async def start():
        with closing(db()) as c,c:
            migrar(c);c.execute("UPDATE dossies_jobs SET estado='pendente' WHERE estado='coletando'")
        app.state.dossies_task=asyncio.create_task(worker())
