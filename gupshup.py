"""Gupshup: credenciais isoladas e qualidade conferida manualmente pelo operador."""
import json
import hashlib
import re
import secrets
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone

import requests
from fastapi import HTTPException

API = 'https://api.gupshup.io'


def preservar_eventos(itens, anteriores):
    por_id = {t['id']: t for t in anteriores}
    for t in itens:
        antigo = por_id.get(t['id'], {})
        if antigo.get('_evento_em', 0) > float(t.get('modifiedOn') or 0) / 1000:
            t['status'] = antigo['status']
            t['_evento_em'] = antigo['_evento_em']
    return itens


def via(n):
    return dict(n).get('tipo_envio') == 'gupshup'


def migrar(c):
    c.execute('''CREATE TABLE IF NOT EXISTS gup_apps (
        app_id TEXT PRIMARY KEY, token TEXT NOT NULL, nome TEXT NOT NULL DEFAULT '',
        telefone TEXT NOT NULL DEFAULT '', templates TEXT NOT NULL DEFAULT '[]',
        consultado REAL NOT NULL DEFAULT 0, erro TEXT NOT NULL DEFAULT '',
        bloqueado INTEGER NOT NULL DEFAULT 0, evento_em REAL NOT NULL DEFAULT 0,
        webhook_token TEXT NOT NULL, numero_id INTEGER UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS gup_eventos (
        app_id TEXT NOT NULL, chave TEXT NOT NULL, recebido_em REAL NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(app_id,chave))''')


def lista_templates(app_id, token):
    itens, vistos = [], set()
    try:
        for pagina in range(100):
            r = requests.get(f'{API}/wa/app/{app_id}/template', headers={'apikey': token},
                             params={'pageNo': pagina, 'pageSize': 100}, timeout=(5, 20), allow_redirects=False)
            if r.status_code != 200:
                raise HTTPException(400, f'Gupshup: consulta recusada (HTTP {r.status_code}). Confira App ID e token.')
            d = r.json()
            if d.get('status') != 'success' or not isinstance(d.get('templates'), list):
                raise ValueError()
            pagina_itens = d['templates']
            for t in pagina_itens:
                if not isinstance(t, dict) or not t.get('id') or t.get('appId', app_id) != app_id:
                    raise ValueError()
                if t['id'] in vistos:
                    raise ValueError()
                vistos.add(t['id']); itens.append(t)
            if len(pagina_itens) < 100:
                return itens
        raise ValueError()
    except (requests.RequestException, ValueError, TypeError):
        raise HTTPException(502, 'Não foi possível consultar todos os templates da Gupshup.')


def salvar(db, body):
    try:
        app_id = str(uuid.UUID(str(body.get('app_id', ''))))
    except ValueError:
        raise HTTPException(400, 'App ID inválido.')
    with closing(db()) as c:
        anterior = c.execute('SELECT * FROM gup_apps WHERE app_id=?', (app_id,)).fetchone()
    token = str(body.get('token', '')).strip() or (anterior['token'] if anterior else '')
    if not token or len(token) > 1024:
        raise HTTPException(400, 'Informe o token da Gupshup.')
    itens = lista_templates(app_id, token)
    with closing(db()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        salvo = c.execute('SELECT templates FROM gup_apps WHERE app_id=?',(app_id,)).fetchone()
        if salvo:itens = preservar_eventos(itens,json.loads(salvo['templates']))
        c.execute('''INSERT INTO gup_apps(app_id,token,templates,consultado,webhook_token)
            VALUES(?,?,?,?,?) ON CONFLICT(app_id) DO UPDATE SET token=excluded.token,
            templates=excluded.templates,consultado=excluded.consultado,erro='' ''',
            (app_id, token, json.dumps(itens), time.time(), secrets.token_urlsafe(32)))
    return {'app_id': app_id}


def atualizar(db, app_id):
    with closing(db()) as c:
        app = c.execute('SELECT * FROM gup_apps WHERE app_id=?', (app_id,)).fetchone()
    if not app:
        raise HTTPException(404, 'Aplicativo não encontrado.')
    try:
        itens = lista_templates(app_id, app['token'])
    except HTTPException as exc:
        with closing(db()) as c, c:
            c.execute('UPDATE gup_apps SET erro=? WHERE app_id=?', (exc.detail, app_id))
            c.execute("UPDATE numeros SET template_status='UNKNOWN' WHERE id=?", (app['numero_id'],))
        raise
    with closing(db()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        salvo = c.execute('SELECT templates FROM gup_apps WHERE app_id=?',(app_id,)).fetchone()
        itens = preservar_eventos(itens,json.loads(salvo['templates']))
        c.execute("UPDATE gup_apps SET templates=?,consultado=?,erro='' WHERE app_id=?",
                  (json.dumps(itens), time.time(), app_id))
        n = c.execute('SELECT * FROM numeros WHERE id=?', (app['numero_id'],)).fetchone()
        if n:
            t = next((t for t in itens if t['elementName'] == n['template_nome'] and t['languageCode'] == n['template_idioma']), {})
            c.execute('UPDATE numeros SET template_status=?,template_categoria=?,meta_consultado_em=? WHERE id=?',
                      (t.get('status', 'UNKNOWN'), t.get('category', 'UNKNOWN'), datetime.now(timezone.utc).isoformat(), n['id']))
    return {'ok': True}


def configurar(db, app_id, body):
    with closing(db()) as c:
        app = c.execute('SELECT * FROM gup_apps WHERE app_id=?', (app_id,)).fetchone()
    if not app:
        raise HTTPException(404, 'Aplicativo não encontrado.')
    nome = str(body.get('nome', '')).strip()
    telefone = re.sub(r'\D', '', str(body.get('telefone', '')))
    if not re.fullmatch(r'[A-Za-z0-9_\-]{1,100}', nome) or not re.fullmatch(r'55\d{10,11}', telefone):
        raise HTTPException(400, 'Informe o nome exato do aplicativo e o WhatsApp remetente com DDI 55.')
    for k in ('limite_diario', 'limite_hora'):
        if type(body.get(k)) is not int or not 0 <= body[k] <= 1000000:
            raise HTTPException(400, 'Limites devem ser números inteiros entre zero e 1.000.000.')
    if type(body.get('ativo')) is not bool:
        raise HTTPException(400, 'Ativo deve ser booleano.')
    atualizar(db, app_id)
    with closing(db()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        app = c.execute('SELECT * FROM gup_apps WHERE app_id=?', (app_id,)).fetchone()
        t = next((t for t in json.loads(app['templates']) if t['id'] == body.get('template_id')), None)
        if not t or t.get('templateType') != 'TEXT':
            raise HTTPException(400, 'Selecione um template de texto. Outros formatos ainda não estão disponíveis nesta integração.')
        if t.get('parameterFormat','POSITIONAL') != 'POSITIONAL' or any(not x.strip().isdigit() for x in re.findall(r'\{\{(.*?)\}\}',t.get('data',''))):
            raise HTTPException(400, 'Este formato de variáveis ainda não é suportado.')
        if body['ativo'] and (t['status'] != 'APPROVED' or app['bloqueado']):
            raise HTTPException(400, 'Não é possível ativar: template não aprovado ou conta bloqueada.')
        ident = app['numero_id']
        if not ident or not c.execute('SELECT 1 FROM numeros WHERE id=?', (ident,)).fetchone():
            ident = c.execute("INSERT INTO numeros(nome,phone_number_id,access_token,tipo_envio,ativo) VALUES(?,?,'','gupshup',0)",
                              (nome, 'gup:' + app_id)).lastrowid
        c.execute('''UPDATE numeros SET nome=?,template_nome=?,template_idioma=?,template_status=?,
            template_categoria=?,waba_id=?,limite_diario=?,limite_hora=?,ativo=?,template_sem_variaveis=?,
            qualidade='UNKNOWN',pausado_por_qualidade=0 WHERE id=?''',
            (nome+' +'+telefone, t['elementName'], t['languageCode'], t['status'], t.get('category','UNKNOWN'),
             str(t.get('wabaId','')), body['limite_diario'], body['limite_hora'], int(body['ativo']),
             int(not re.search(r'\{\{', t.get('data',''))), ident))
        c.execute('UPDATE gup_apps SET nome=?,telefone=?,numero_id=? WHERE app_id=?', (nome, telefone, ident, app_id))
    return {'numero_id': ident}


def elegivel(db, n):
    if not n['ativo'] or n['template_status'] != 'APPROVED':
        return False
    with closing(db()) as c:
        a = c.execute('SELECT * FROM gup_apps WHERE numero_id=?', (n['id'],)).fetchone()
    if a:
        t = next((t for t in json.loads(a['templates']) if t.get('elementName') == n['template_nome'] and t.get('languageCode') == n['template_idioma']), {})
        if t.get('status') != 'APPROVED' or t.get('templateType') != 'TEXT':return False
    return bool(a and a['nome'] and a['telefone'] and not a['bloqueado'] and not a['erro']
                and time.time() - a['consultado'] < 1200)


def enviar(db, n, destino, variaveis, bloqueado):
    with closing(db()) as c:
        atual = c.execute('SELECT * FROM numeros WHERE id=?', (n['id'],)).fetchone()
        a = c.execute('SELECT * FROM gup_apps WHERE numero_id=?', (n['id'],)).fetchone()
    if not atual or not elegivel(db, atual) or bloqueado(destino):
        return False, 'GUPSHUP_INDISPONIVEL: confira conexão, template e lista negra', ''
    t = next((t for t in json.loads(a['templates']) if t.get('elementName') == atual['template_nome'] and t.get('languageCode') == atual['template_idioma']), {})
    if t.get('templateType') != 'TEXT' or t.get('status') != 'APPROVED':
        return False, 'GUPSHUP_TEMPLATE: template indisponível', ''
    ids = sorted(set(re.findall(r'\{\{\s*(\d+)\s*\}\}', t.get('data',''))), key=int)
    params = [] if not ids else list(variaveis)
    if ids != [str(i+1) for i in range(len(ids))] or len(params) != len(ids) or any(not str(p).strip() for p in params):
        return False, 'GUPSHUP_VARIAVEIS: preencha todas as variáveis do template', ''
    try:
        r = requests.post(API+'/wa/api/v1/template/msg', headers={'apikey': a['token']},
            data={'channel':'whatsapp','source':a['telefone'],'destination':destino,'src.name':a['nome'],
                  'template':json.dumps({'id':t['id'],'params':[str(p) for p in params]})},
            timeout=(5,20), allow_redirects=False)
        if r.status_code >= 500:
            return False, 'ENVIO_INCERTO: Gupshup não confirmou o envio; não repetir automaticamente', ''
        if r.status_code not in (200,202):
            return False, f'GUPSHUP_HTTP_{r.status_code}: envio recusado', ''
        d = r.json()
        if d.get('status') in ('success','submitted') and d.get('messageId'):
            return True, '', d['messageId']
        return False, 'ENVIO_INCERTO: resposta Gupshup sem confirmação; confira no provedor', ''
    except (requests.RequestException, ValueError):
        return False, 'ENVIO_INCERTO: Gupshup não confirmou o envio; não repetir automaticamente', ''


def listar(db):
    with closing(db()) as c:
        return [dict(r) for r in c.execute('''SELECT a.app_id,a.nome,a.telefone,a.templates,a.consultado,a.erro,
            a.bloqueado,a.numero_id,n.template_nome,n.template_idioma,n.limite_diario,n.limite_hora,n.ativo
            FROM gup_apps a LEFT JOIN numeros n ON n.id=a.numero_id ORDER BY a.nome,a.app_id''')]


def receber(db, app_id, chave, raw):
    with closing(db()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        a = c.execute('SELECT * FROM gup_apps WHERE app_id=?', (app_id,)).fetchone()
        if not a or not secrets.compare_digest(chave, a['webhook_token']):
            raise HTTPException(403, 'Credencial inválida.')
        try:
            d = json.loads(raw)
            if not isinstance(d,dict) or d.get('appId',app_id) != app_id:
                raise ValueError()
            if a['nome'] and d.get('app',a['nome']) != a['nome']:
                raise ValueError()
            stamp = float(d['timestamp']) / 1000
            if not 0 < stamp <= time.time()+300:
                raise ValueError()
            p = d.get('payload',{})
            if not isinstance(p,dict):raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise HTTPException(400,'Evento inválido.')
        digest = hashlib.sha256(raw).hexdigest()
        inserted = c.execute('INSERT OR IGNORE INTO gup_eventos VALUES(?,?,?,?)',
                            (app_id,digest,time.time(),raw.decode('utf-8'))).rowcount
        if not inserted:return {'ok':True}
        if d.get('type') == 'account-event' and p.get('type') == 'status-event' and stamp >= a['evento_em']:
            status = str(p.get('payload',{}).get('status','')).upper()
            if status in ('ACCOUNT_DISABLE','DISABLE','ACCOUNT_RESTRICTED','ACCOUNT_VIOLATION'):
                c.execute('UPDATE gup_apps SET bloqueado=1,evento_em=? WHERE app_id=?',(stamp,app_id))
                c.execute('UPDATE numeros SET ativo=0 WHERE id=?',(a['numero_id'],))
            elif status == 'REINSTATE':
                c.execute('UPDATE gup_apps SET bloqueado=0,evento_em=? WHERE app_id=?',(stamp,app_id))
        if d.get('type') == 'template-event' and p.get('type','status-update') == 'status-update':
            itens = json.loads(a['templates'])
            for t in itens:
                if t['id'] == p.get('id') and stamp >= t.get('_evento_em',0):
                    status = str(p.get('status','')).upper()
                    if status not in ('APPROVED','PENDING','PAUSED','DISABLED','REJECTED','DELETED'):continue
                    t['status']=status;t['_evento_em']=stamp
                    c.execute('UPDATE numeros SET template_status=? WHERE id=? AND template_nome=? AND template_idioma=?',
                              (status,a['numero_id'],t['elementName'],t['languageCode']))
            c.execute('UPDATE gup_apps SET templates=? WHERE app_id=?',(json.dumps(itens),app_id))
    return {'ok':True}
