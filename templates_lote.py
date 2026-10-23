"""Criação de templates por WABA, com resultado persistido e sem repetição cega."""
import base64
import json
import re
import time
import uuid
from contextlib import closing
from urllib.parse import quote
import requests
from fastapi import HTTPException

GRAPH = 'https://graph.facebook.com/v21.0/'


def migrar(c):
    c.execute('CREATE TABLE IF NOT EXISTS template_lotes (id TEXT PRIMARY KEY, payload TEXT NOT NULL, criado_em REAL NOT NULL)')
    c.execute('''CREATE TABLE IF NOT EXISTS template_lote_itens (
        lote_id TEXT NOT NULL, waba_id TEXT NOT NULL, nome TEXT NOT NULL, idioma TEXT NOT NULL,
        estado TEXT NOT NULL DEFAULT 'AGUARDANDO', detalhe TEXT NOT NULL DEFAULT '',
        meta_id TEXT NOT NULL DEFAULT '', meta_status TEXT NOT NULL DEFAULT '', atualizado_em REAL NOT NULL,
        PRIMARY KEY(lote_id,waba_id))''')
    if 'meta_categoria' not in {r[1] for r in c.execute('PRAGMA table_info(template_lote_itens)')}:
        c.execute("ALTER TABLE template_lote_itens ADD COLUMN meta_categoria TEXT NOT NULL DEFAULT ''")


def validar(body):
    if not isinstance(body, dict):
        raise HTTPException(400, 'Dados inválidos.')
    nome, texto = body.get('name', ''), body.get('body', '')
    if not isinstance(nome, str) or not re.fullmatch('[a-z0-9_]{1,512}', nome):
        raise HTTPException(400, 'Nome: use letras minúsculas, números e sublinhado.')
    if not isinstance(texto, str) or not texto.strip() or len(texto) > 1024:
        raise HTTPException(400, 'A mensagem deve ter entre 1 e 1.024 caracteres.')
    idioma = body.get('language', 'pt_BR')
    if idioma not in ('pt_BR', 'en_US', 'es', 'pt_PT'):
        raise HTTPException(400, 'Idioma inválido.')
    categoria = body.get('category', 'MARKETING')
    if categoria not in ('MARKETING', 'UTILITY'):
        raise HTTPException(400, 'Escolha Marketing ou Utilidade.')
    variaveis = sorted(set(int(v) for v in re.findall(r'\{\{([1-9][0-9]*)\}\}', texto)))
    resto = re.sub(r'\{\{[1-9][0-9]*\}\}', '', texto)
    if '{{' in resto or '}}' in resto or variaveis != list(range(1, len(variaveis)+1)):
        raise HTTPException(400, 'Use variáveis sequenciais: {{1}}, {{2}}…')
    exemplos = body.get('examples', [])
    if not isinstance(exemplos, list) or len(exemplos) != len(variaveis) or any(not isinstance(v, str) or not v.strip() or len(v)>1024 for v in exemplos):
        raise HTTPException(400, 'Preencha um exemplo para cada variável.')
    footer = body.get('footer', '')
    if not isinstance(footer, str) or len(footer)>60 or '{{' in footer:
        raise HTTPException(400, 'Rodapé: até 60 caracteres, sem variáveis.')
    botoes = body.get('buttons', [])
    if not isinstance(botoes, list) or len(botoes)>3:
        raise HTTPException(400, 'Use até três botões de resposta ou até dois de ação.')
    limpos=[]
    for b in botoes:
        if not isinstance(b,dict) or b.get('type') not in ('QUICK_REPLY','URL','PHONE_NUMBER') or not isinstance(b.get('text'),str) or not 1<=len(b['text'].strip())<=25:
            raise HTTPException(400,'Botão inválido: título de até 25 caracteres.')
        item={'type':b['type'],'text':b['text'].strip()}
        if b['type']=='URL':
            url=b.get('url','')
            if not isinstance(url,str) or not url.startswith('https://') or '{{' in url or len(url)>2000:
                raise HTTPException(400,'Use um link HTTPS fixo no botão.')
            item['url']=url
        if b['type']=='PHONE_NUMBER':
            telefone=b.get('phone_number','')
            if not isinstance(telefone,str) or not re.fullmatch(r'\+[1-9][0-9]{7,14}',telefone):
                raise HTTPException(400,'Telefone do botão: use +DDI e números.')
            item['phone_number']=telefone
        limpos.append(item)
    tipos=[b['type'] for b in limpos]
    if any(t!='QUICK_REPLY' for t in tipos) and ('QUICK_REPLY' in tipos or len(tipos)>2 or len(set(tipos))!=len(tipos)):
        raise HTTPException(400,'Escolha respostas rápidas OU até dois botões de ação (um link e um telefone).')
    header=body.get('header_text','')
    if not isinstance(header,str) or len(header)>60 or '{{' in header:
        raise HTTPException(400,'Cabeçalho: até 60 caracteres, sem variáveis.')
    imagem=body.get('image','')
    if header and imagem:raise HTTPException(400,'Escolha cabeçalho de texto ou imagem.')
    if body.get('image_required') and not imagem:raise HTTPException(400,'Anexe a imagem de exemplo deste modelo.')
    if imagem:
        decodificar(imagem)
    wabas=body.get('wabas')
    if not isinstance(wabas,list) or not 1<=len(wabas)<=100 or any(not isinstance(w,str) or not w.isdigit() for w in wabas):
        raise HTTPException(400,'Selecione de 1 a 100 WABAs.')
    return {'name':nome,'body':texto,'language':idioma,'category':categoria,'examples':exemplos,'footer':footer,'buttons':limpos,'image':imagem,'header_text':header,'wabas':list(dict.fromkeys(wabas))}


def decodificar(imagem):
    try:
        if not isinstance(imagem,str) or len(imagem)>7_000_000:raise ValueError()
        prefixo,valor=imagem.split(',',1)
        mime={'data:image/jpeg;base64':'image/jpeg','data:image/png;base64':'image/png'}[prefixo]
        dados=base64.b64decode(valor,validate=True)
        assinatura=b'\x89PNG\r\n\x1a\n' if mime=='image/png' else b'\xff\xd8\xff'
        if not dados.startswith(assinatura) or len(dados)>5_000_000:raise ValueError()
        return dados,mime
    except (ValueError,KeyError):raise HTTPException(400,'Envie JPEG ou PNG de até 5 MB.')


def componentes(p,handle=None):
    c=[]
    if p['image']:
        c.append({'type':'HEADER','format':'IMAGE','example':{'header_handle':[handle]}})
    if p.get('header_text'):c.append({'type':'HEADER','format':'TEXT','text':p['header_text']})
    corpo={'type':'BODY','text':p['body']}
    if p['examples']:corpo['example']={'body_text':[p['examples']]}
    c.append(corpo)
    if p['footer']:c.append({'type':'FOOTER','text':p['footer']})
    if p['buttons']:c.append({'type':'BUTTONS','buttons':p['buttons']})
    return c


def criar_lote(get_db,body):
    p=validar(body);ident=uuid.uuid4().hex
    with closing(get_db()) as c,c:
        for w in p['wabas']:
            if not c.execute('SELECT 1 FROM wabas_salvas WHERE waba_id=?',(w,)).fetchone():raise HTTPException(400,'Uma das WABAs não está cadastrada.')
        c.execute('INSERT INTO template_lotes VALUES(?,?,?)',(ident,json.dumps(p),time.time()))
        c.executemany('INSERT INTO template_lote_itens(lote_id,waba_id,nome,idioma,atualizado_em) VALUES(?,?,?,?,?)',[(ident,w,p['name'],p['language'],time.time()) for w in p['wabas']])
    return {'id':ident}


def resumo(get_db,ident):
    with closing(get_db()) as c:
        lote=c.execute('SELECT * FROM template_lotes WHERE id=?',(ident,)).fetchone()
        if not lote:raise HTTPException(404,'Criação não encontrada.')
        itens=[dict(r) for r in c.execute('SELECT i.*,COALESCE(w.nome,i.waba_id) as waba_nome FROM template_lote_itens i LEFT JOIN wabas_salvas w ON w.waba_id=i.waba_id WHERE lote_id=?',(ident,))]
    p=json.loads(lote['payload'])
    return {'id':ident,'name':p['name'],'language':p['language'],'categoria_solicitada':p['category'],'itens':itens}


def gravar(get_db,lote,waba,estado,detalhe='',meta_id='',meta_status='',meta_categoria=None):
    with closing(get_db()) as c,c:
        c.execute('UPDATE template_lote_itens SET estado=?,detalhe=?,meta_id=?,meta_status=?,atualizado_em=? WHERE lote_id=? AND waba_id=?',(estado,detalhe[:800],meta_id,meta_status,time.time(),lote,waba))
        if meta_categoria in ('UTILITY','MARKETING','AUTHENTICATION'):
            c.execute('UPDATE template_lote_itens SET meta_categoria=? WHERE lote_id=? AND waba_id=?',(meta_categoria,lote,waba))
            item=c.execute('SELECT nome,idioma FROM template_lote_itens WHERE lote_id=? AND waba_id=?',(lote,waba)).fetchone()
            c.execute("UPDATE numeros SET template_categoria=? WHERE waba_id=? AND template_nome=? AND COALESCE(NULLIF(template_idioma,''),'pt_BR')=? AND tipo_envio='meta'",(meta_categoria,waba,item['nome'],item['idioma']))



def erro_meta(d,token):
    e=d.get('error',{}) if isinstance(d,dict) else {}
    texto=str(e.get('error_user_msg') or e.get('message') or 'A Meta recusou a solicitação.')
    return ('Meta '+str(e.get('code',''))+': '+texto.replace(token,'[credencial]'))[:800]


def executar(get_db,consultar,lote,waba,apenas_consultar=False):
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM template_lote_itens WHERE lote_id=? AND waba_id=?',(lote,waba)).fetchone()
        if not row:raise HTTPException(404,'Item não encontrado.')
        p=json.loads(c.execute('SELECT payload FROM template_lotes WHERE id=?',(lote,)).fetchone()['payload'])
        cred=c.execute('SELECT access_token FROM wabas_salvas WHERE waba_id=?',(waba,)).fetchone()
        if not cred:raise HTTPException(404,'WABA não cadastrada.')
        if not apenas_consultar:
            if row['estado'] not in ('AGUARDANDO','ERRO'):
                return dict(row)
            ocupado=c.execute("SELECT 1 FROM template_lote_itens WHERE waba_id=? AND nome=? AND idioma=? AND estado IN ('PROCESSANDO','INCERTO') AND lote_id!=?",(waba,p['name'],p['language'],lote)).fetchone()
            if ocupado:raise HTTPException(409,'Já existe uma solicitação sem confirmação para este template. Consulte o resultado anterior.')
            c.execute("UPDATE template_lote_itens SET estado='PROCESSANDO',detalhe='',atualizado_em=? WHERE lote_id=? AND waba_id=?",(time.time(),lote,waba))
    token=cred['access_token'];post_iniciado=False
    try:
        lista=consultar(waba,token,'/message_templates','id,name,language,status,category')
        if 'erro' in lista:
            if not apenas_consultar:gravar(get_db,lote,waba,'ERRO','Não foi possível conferir templates existentes. Nenhuma criação foi enviada.')
            return resumo(get_db,lote)
        existente=next((t for t in lista['dados'] if t.get('name')==p['name'] and t.get('language')==p['language']),None)
        if existente:
            # Never overwrite a template with the same name, including a different body.
            estado='CRIADO' if row['meta_id'] and row['meta_id']==str(existente['id']) else 'EXISTENTE'
            gravar(get_db,lote,waba,estado,'Template localizado na Meta; conteúdo existente preservado.',str(existente['id']),existente.get('status','UNKNOWN'),existente.get('category'))
        elif apenas_consultar:
            # An inconclusive send must never be retried automatically.
            if row['estado'] in ('INCERTO','PROCESSANDO') and time.time()-row['atualizado_em']>120:
                gravar(get_db,lote,waba,'INCERTO','Ainda não localizado. Confira na Meta antes de qualquer nova tentativa.')
        else:
            handle=None
            headers={'Authorization':'Bearer '+token}
            if p['image']:
                dados,mime=decodificar(p['image'])
                r=requests.post(GRAPH+'app/uploads',params={'file_length':len(dados),'file_type':mime,'file_name':'exemplo.png' if mime=='image/png' else 'exemplo.jpg'},headers=headers,timeout=(5,30),allow_redirects=False)
                d=r.json();sessao=d.get('id','')
                if r.status_code!=200 or not isinstance(sessao,str) or not sessao.startswith('upload:') or '/' in sessao or '#' in sessao:
                    raise ValueError(erro_meta(d,token))
                r=requests.post(GRAPH+sessao,headers={'Authorization':'OAuth '+token,'file_offset':'0','Content-Type':mime},data=dados,timeout=(5,45),allow_redirects=False)
                d=r.json();handle=d.get('h')
                if r.status_code!=200 or not isinstance(handle,str) or not handle:raise ValueError(erro_meta(d,token))
            post_iniciado=True
            r=requests.post(GRAPH+waba+'/message_templates',json={'name':p['name'],'language':p['language'],'category':p['category'],'components':componentes(p,handle)},headers=headers,timeout=(5,30),allow_redirects=False)
            d=r.json()
            if r.status_code==200 and d.get('id'):
                gravar(get_db,lote,waba,'CRIADO','Solicitação aceita pela Meta.',str(d['id']),d.get('status','UNKNOWN'),d.get('category'))
            elif 400<=r.status_code<500 and isinstance(d.get('error'),dict):
                gravar(get_db,lote,waba,'ERRO',erro_meta(d,token))
            else:gravar(get_db,lote,waba,'INCERTO','Resposta sem confirmação. Consulte o resultado antes de tentar novamente.')
    except (requests.RequestException,ValueError,TypeError,AttributeError) as e:
        if not apenas_consultar:
            gravar(get_db,lote,waba,'INCERTO' if post_iniciado else 'ERRO','Resposta sem confirmação; use Consultar.' if post_iniciado else ('Falha antes da criação. '+(str(e) if isinstance(e,ValueError) else 'Confira conexão, arquivo e permissões.')).replace(token,'[credencial]'))
    return resumo(get_db,lote)


def modelo_aprovado(t):
    """Converte somente estruturas reproduzíveis, sem descartar componentes."""
    if t.get('status')!='APPROVED':raise HTTPException(400,'Escolha um template aprovado.')
    if t.get('parameter_format')=='NAMED':raise HTTPException(400,'Este modelo usa variáveis nomeadas, ainda não suportadas nesta tela.')
    p={'name':t['name'],'language':t['language'],'category':t['category'],'body':'','footer':'','buttons':[],'examples':[],'image':'','header_text':'','image_required':False,'wabas':['1']}
    seen=set()
    for c in t.get('components',[]):
        kind=c.get('type')
        if kind in seen:raise HTTPException(400,'Modelo com componentes repetidos não suportado.')
        seen.add(kind)
        if kind=='BODY':
            p['body']=c.get('text','')
            examples=c.get('example',{}).get('body_text',[])
            p['examples']=examples[0] if examples else ['Exemplo']*len(set(re.findall(r'\{\{([1-9][0-9]*)\}\}',p['body'])))
        elif kind=='HEADER':
            if c.get('format')=='IMAGE':p['image_required']=True
            elif c.get('format')=='TEXT':p['header_text']=c.get('text','')
            else:raise HTTPException(400,'Este tipo de cabeçalho ainda não pode ser copiado nesta tela.')
        elif kind=='FOOTER':p['footer']=c.get('text','')
        elif kind=='BUTTONS':p['buttons']=c.get('buttons',[])
        else:raise HTTPException(400,'Este modelo contém componentes ainda não suportados nesta tela.')
    validar(dict(p,image_required=False))
    p.pop('wabas')
    return p


def modelos(get_db,consultar,waba):
    with closing(get_db()) as c:
        cred=c.execute('SELECT access_token FROM wabas_salvas WHERE waba_id=?',(waba,)).fetchone()
        if not cred:raise HTTPException(404,'WABA não cadastrada.')
        antigos=[json.loads(r['payload']) for r in c.execute('SELECT payload FROM template_lotes ORDER BY criado_em DESC')]
    data=consultar(waba,cred['access_token'],'/message_templates','id,name,language,status,category,components,parameter_format')
    if data.get('erro'):raise HTTPException(502,'Não foi possível consultar os modelos na Meta.')
    out=[]
    for t in data.get('dados',[]):
        if t.get('status')!='APPROVED':continue
        item={'id':t.get('id'),'name':t.get('name'),'language':t.get('language'),'category':t.get('category')}
        try:
            p=modelo_aprovado(t)
            if p['image_required']:
                for old in antigos:
                    if waba in old.get('wabas',[]) and all(old.get(k)==p[k] for k in ('name','language','body')) and old.get('image'):
                        decodificar(old['image']);p['image']=old['image'];break
            item['modelo']=p
        except HTTPException as e:item['motivo']=e.detail
        out.append(item)
    return out
