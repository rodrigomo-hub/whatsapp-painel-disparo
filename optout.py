"""Supressão persistente e encerramento prioritário no CRM."""
import json
import re
import time
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests

BASE = 'https://crm.vendeaitecnologia.com.br/api/v1/accounts/'
PASSOS = ('resolver', 'contato', 'desatribuir', 'ler_labels', 'marcar', 'excluir', 'confirmar')


def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', str(texto or '').strip().lower()) if unicodedata.category(c) != 'Mn')


def telefone(valor):
    p = re.sub(r'\D', '', str(valor or ''))
    if len(p) in (10, 11): p = '55' + p
    if len(p) == 12 and p.startswith('55') and p[4] in '6789': p = p[:4] + '9' + p[4:]
    return p


def migrar(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS optout_config(id INTEGER PRIMARY KEY CHECK(id=1), habilitado INTEGER NOT NULL DEFAULT 0, textos TEXT NOT NULL DEFAULT '["nao quero mensagens"]');
    INSERT OR IGNORE INTO optout_config(id) VALUES(1);
    CREATE TABLE IF NOT EXISTS optout(telefone TEXT PRIMARY KEY, cpf TEXT, nome TEXT, account_id TEXT, chat_id TEXT, campanha TEXT, texto_recebido TEXT, primeiro_optout_em TEXT, ultimo_optout_em TEXT);
    CREATE TABLE IF NOT EXISTS optout_tarefas(id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, chat_id TEXT NOT NULL, evento_id INTEGER, passo TEXT NOT NULL DEFAULT 'resolver', estado TEXT NOT NULL DEFAULT 'pendente', tentativas INTEGER NOT NULL DEFAULT 0, tentar_em REAL NOT NULL DEFAULT 0, contact_id TEXT, resolvida INTEGER NOT NULL DEFAULT 0, labels TEXT, criado_em REAL NOT NULL, detalhe TEXT NOT NULL DEFAULT '', UNIQUE(account_id,chat_id));
    CREATE TABLE IF NOT EXISTS optout_passos(id INTEGER PRIMARY KEY, tarefa_id INTEGER, chat_id TEXT, passo TEXT, http_status INTEGER, duracao_ms INTEGER, desde_recebimento_ms INTEGER, criado_em REAL, resultado TEXT);
    CREATE INDEX IF NOT EXISTS idx_optout_pendente ON optout_tarefas(estado,tentar_em);
    ''')
    if 'teste_excluir_restantes' not in {r['name'] for r in c.execute('PRAGMA table_info(optout_config)')}:
        c.execute('ALTER TABLE optout_config ADD COLUMN teste_excluir_restantes INTEGER NOT NULL DEFAULT 0')


def bloqueado(get_db, numero):
    with closing(get_db()) as c:
        return bool(c.execute('SELECT 1 FROM optout WHERE telefone=?', (telefone(numero),)).fetchone())


def receber(c, d, evento_id):
    cfg = c.execute('SELECT * FROM optout_config WHERE id=1').fetchone()
    if not cfg or not cfg['habilitado'] or d.get('event') != 'conversation_created': return False
    texto = d.get('first_message')
    if not isinstance(texto, str) or normalizar(texto) not in {normalizar(x) for x in json.loads(cfg['textos'])}: return False
    chat = d.get('chat_summary') or {}; details = chat.get('details') or {}
    contact = details.get('contact') or {}; session = details.get('session') or {}
    p = telefone(contact.get('phone')); chat_id = str(chat.get('chat_id') or ''); account = str(chat.get('account_id') or '')
    # Sem telefone ainda encerramos a conversa, mas nunca criamos uma supressão vazia.
    now = datetime.now(timezone.utc).isoformat()
    if p:
        cpf = re.sub(r'\D', '', str(contact.get('cpf') or ''))
        if cpf: cpf = cpf.zfill(11)
        c.execute('''INSERT INTO optout VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(telefone) DO UPDATE SET
        cpf=excluded.cpf,nome=excluded.nome,account_id=excluded.account_id,chat_id=excluded.chat_id,campanha=excluded.campanha,texto_recebido=excluded.texto_recebido,ultimo_optout_em=excluded.ultimo_optout_em''',
        (p,cpf,contact.get('name'),account,chat_id,session.get('campaign'),texto,now,now))
        # Cancela pendentes. A barreira final de envio também cobre itens já em processamento.
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='fila_disparos'").fetchone():
            for r in c.execute("SELECT id,payload FROM fila_disparos WHERE status IN ('aguardando_cadastro','cadastrando','pronto')").fetchall():
                if telefone(json.loads(r['payload'])['mailing']['phone']) == p:
                    c.execute("UPDATE fila_disparos SET status='cancelado',atualizado_em=?,resultado=? WHERE id=?", (time.time(),json.dumps({'motivo_codigo':'optout','status':'bloqueado'}),r['id']))
    if not chat_id.isdigit() or not account.isdigit(): return False
    passo = 'excluir_conversa' if cfg['teste_excluir_restantes'] else 'resolver'
    cursor=c.execute('INSERT OR IGNORE INTO optout_tarefas(account_id,chat_id,evento_id,criado_em,detalhe,passo) VALUES(?,?,?,?,?,?)', (account,chat_id,evento_id,time.time(),'' if p else 'Telefone ausente: supressão não registrada.',passo))
    if cursor.rowcount and passo=='excluir_conversa':
        c.execute('UPDATE optout_config SET teste_excluir_restantes=0 WHERE id=1')
    return True


def recuperar(get_db):
    with closing(get_db()) as c,c:
        c.execute("UPDATE optout_tarefas SET estado='pendente' WHERE estado='executando'")


def retry_after(value):
    try: return max(0, float(value))
    except (TypeError, ValueError):
        try: return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError): return 0


def processar_um(get_db, token, somente_resolver=False):
    if not token: return False
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT * FROM optout_tarefas WHERE estado='pendente' AND tentar_em<=?" + (" AND passo IN ('resolver','excluir_conversa')" if somente_resolver else "") + " ORDER BY CASE WHEN passo IN ('resolver','excluir_conversa') THEN 0 ELSE 1 END,criado_em LIMIT 1", (time.time(),)).fetchone()
        if not row: return False
        t = dict(row)
        c.execute("UPDATE optout_tarefas SET estado='executando' WHERE id=?", (t['id'],))
    step = t['passo']; root = BASE+t['account_id']; conv = root+'/conversations/'+t['chat_id']
    method,url,body = 'GET',conv,None
    if step=='excluir_conversa': method='DELETE'
    elif step=='resolver': method,url,body='POST',conv+'/toggle_status',{'status':'resolved'}
    elif step=='desatribuir': method,url,body='POST',conv+'/assignments',{'assignee_id':None}
    elif step=='ler_labels': url=conv+'/labels'
    elif step=='marcar': method,url,body='POST',conv+'/labels',{'labels':json.loads(t['labels'])}
    elif step=='excluir': method,url='DELETE',root+'/contacts/'+str(t['contact_id'])
    elif step=='confirmar': url=root+'/contacts/'+str(t['contact_id'])
    started=time.monotonic(); status=None; data={}; delay=0; failure=''; ok=False
    try:
        r=requests.request(method,url,json=body,headers={'api_access_token':token,'Content-Type':'application/json'},timeout=(1,2),allow_redirects=False)
        status=r.status_code; ok=(status==404) if step=='confirmar_conversa' else (200<=status<300 or status==404)
        delay=retry_after(r.headers.get('Retry-After'))
        if status!=404 and 200<=status<300 and step in ('contato','ler_labels'):
            data=r.json()
        if not ok: failure='http_'+str(status)
    except (requests.RequestException, ValueError, TypeError):
        ok=False; failure='falha_comunicacao_ou_resposta'
    duration=int((time.monotonic()-started)*1000)
    attempt=t['tentativas']+1
    retry=(status is None or status==429 or status>=500 or (step=='confirmar_conversa' and status==200)) and attempt<3
    with closing(get_db()) as c,c:
        c.execute('INSERT INTO optout_passos(tarefa_id,chat_id,passo,http_status,duracao_ms,desde_recebimento_ms,criado_em,resultado) VALUES(?,?,?,?,?,?,?,?)',(t['id'],t['chat_id'],step,status,duration,int((time.time()-t['criado_em'])*1000),time.time(),'ok' if ok else failure))
        if not ok and retry:
            c.execute("UPDATE optout_tarefas SET estado='pendente',tentativas=?,tentar_em=? WHERE id=?",(attempt,time.time()+max(delay,0.5*2**(attempt-1)),t['id']))
            return True
        detail=t['detalhe']
        if not ok: detail += ' '+step+': '+failure+'.'
        if step in ('excluir_conversa','confirmar_conversa'):
            if step=='excluir_conversa':
                proximo='confirmar_conversa' if ok else 'resolver'
                estado='pendente'
                if not ok: detail+=' Retomando encerramento padrão.'
            else:
                proximo=step
                estado='concluido' if ok and not detail.strip() else 'revisar'
                if not ok: detail+=' Exclusão da conversa não confirmada.'
            c.execute('UPDATE optout_tarefas SET passo=?,estado=?,tentativas=0,tentar_em=0,detalhe=? WHERE id=?',(proximo,estado,detail.strip(),t['id']))
            return True
        resolved=t['resolvida'] or (step=='resolver' and ok)
        contact_id=t['contact_id']; labels=t['labels']
        if step=='contato' and ok and status!=404:
            sender=(data.get('meta') or {}).get('sender') if isinstance(data,dict) else None
            ident=sender.get('id') if isinstance(sender,dict) else None
            if str(ident).isdigit(): contact_id=str(ident)
            else: detail+=' Contato não identificado.'
        if step=='contato' and status==404: detail+=' Conversa ausente; contact_id desconhecido.'
        if step=='ler_labels' and ok:
            values=data.get('payload') if isinstance(data,dict) else None
            if status==404: values=[]
            if isinstance(values,list) and all(isinstance(v,str) for v in values): labels=json.dumps(sorted(set(values+['nao_quer_mensagens'])))
            else: detail+=' Rótulos não confirmados; lista preservada.'
        idx=PASSOS.index(step)+1
        if idx<len(PASSOS) and PASSOS[idx]=='marcar' and labels is None: idx+=1
        if idx<len(PASSOS) and PASSOS[idx]=='excluir' and (not contact_id or not resolved):
            detail+=' Exclusão não executada: falta contato ou resolução confirmada.'; idx=len(PASSOS)
        # Uma falha no DELETE ainda é conferida por GET; um contato presente não é sucesso.
        state='pendente' if idx<len(PASSOS) else 'concluido'
        if step=='confirmar' and status!=404: detail+=' Exclusão não confirmada.'
        if state=='concluido' and detail.strip(): state='revisar'
        c.execute('''UPDATE optout_tarefas SET passo=?,estado=?,tentativas=0,tentar_em=0,contact_id=?,resolvida=?,labels=?,detalhe=? WHERE id=?''',
                  (PASSOS[idx] if idx<len(PASSOS) else step,state,contact_id,int(resolved),labels,detail.strip(),t['id']))
    return True
