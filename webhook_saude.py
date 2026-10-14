"""Diagnóstico do webhook de entrada VendeAI; não modifica a configuração remota."""
import time
from contextlib import closing
from datetime import datetime, timezone
import requests


def migrar(c):
    cols={r[1] for r in c.execute('PRAGMA table_info(numeros)')}
    for name,kind in [('webhook_estado',"TEXT NOT NULL DEFAULT ''"),('webhook_verificado_em','REAL NOT NULL DEFAULT 0'),('webhook_detalhe',"TEXT NOT NULL DEFAULT ''")]:
        if name not in cols:c.execute(f'ALTER TABLE numeros ADD COLUMN {name} {kind}')
    c.execute('''CREATE TABLE IF NOT EXISTS webhook_verificacoes(
        id INTEGER PRIMARY KEY,numero_id INTEGER,conta TEXT,inbox TEXT,
        verificado_em TEXT,estado TEXT,http_status INTEGER,configurada TEXT,esperada TEXT)''')


def aplicavel(n):
    n=dict(n)
    return n.get('tipo_envio','meta')=='meta' and n.get('vendeai_conta')=='33393' and bool(n.get('vendeai_inbox_id'))


def diagnosticar(d):
    w=d.get('webhook_configuration')
    expected=d.get('expected_webhook_url')
    if not isinstance(w,dict) or not isinstance(expected,str) or not expected:
        return 'UNKNOWN','','','Diagnóstico indisponível na VendeAI'
    actual=w.get('phone_number') or w.get('whatsapp_business_account') or w.get('application') or ''
    if not isinstance(actual,str):return 'UNKNOWN','','','Resposta inválida da VendeAI'
    if not actual:return 'MISSING','',expected,'Webhook não configurado na VendeAI'
    if actual!=expected:return 'MISMATCH',actual,expected,'Webhook aponta para outro endereço'
    return 'OK',actual,expected,'Webhook configurado corretamente'


def permitido(n):
    n=dict(n)
    return not aplicavel(n) or (n.get('webhook_estado')=='OK' and 0<=time.time()-n.get('webhook_verificado_em',0)<1200)


def consultar(get_db,n,cfg,token):
    if not aplicavel(n):return True
    if str(cfg.get('vendeai_account_id'))!='33393':return False
    status=None;actual=expected='';state='UNKNOWN';detail='Não foi possível conferir o webhook'
    token=cfg.get('vendeai_mailing_token') or token
    try:
        r=requests.get('https://crm.vendeaitecnologia.com.br/api/v1/accounts/33393/inboxes/'+str(n['vendeai_inbox_id'])+'/health',headers={'api_access_token':token},timeout=(5,15),allow_redirects=False)
        status=r.status_code
        if status==200:state,actual,expected,detail=diagnosticar(r.json())
    except (requests.RequestException,ValueError,AttributeError,TypeError):pass
    now=time.time()
    with closing(get_db()) as c,c:
        c.execute('UPDATE numeros SET webhook_estado=?,webhook_verificado_em=?,webhook_detalhe=? WHERE id=? AND vendeai_conta=? AND vendeai_inbox_id=?',(state,now,detail,n['id'],n['vendeai_conta'],n['vendeai_inbox_id']))
        c.execute('INSERT INTO webhook_verificacoes(numero_id,conta,inbox,verificado_em,estado,http_status,configurada,esperada) VALUES(?,?,?,?,?,?,?,?)',(n['id'],n['vendeai_conta'],n['vendeai_inbox_id'],datetime.now(timezone.utc).isoformat(),state,status,actual,expected))
    return state=='OK'
