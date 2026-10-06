"""Lembretes de assinatura, separados de campanhas e baseados em webhooks."""
import json
import re
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from fastapi import HTTPException
import campanhas_sms
import optout

TZ = ZoneInfo('America/Sao_Paulo')
TEXTOS = [
    'Falta sua assinatura para seguir com a proposta na Facta. Confira as condicoes e assine pelo link: {link}',
    'Ainda quer continuar com sua proposta? Confira as condicoes e conclua sua assinatura na Facta: {link}',
    'Sua proposta na Facta continua aguardando assinatura. Se deseja seguir, acesse e finalize: {link}']

def agora(): return datetime.now(TZ)
def data(v):
    try:
        t=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        return t.replace(tzinfo=TZ) if t.tzinfo is None else t.astimezone(TZ)
    except (ValueError,TypeError): return None

def migrar(c):
    c.execute('''CREATE TABLE IF NOT EXISTS lembretes_sms_config(id INTEGER PRIMARY KEY CHECK(id=1),
      habilitado INTEGER NOT NULL DEFAULT 0,intervalos TEXT NOT NULL,mensagens TEXT NOT NULL,
      idade_maxima_dias INTEGER NOT NULL DEFAULT 7,cursor INTEGER NOT NULL DEFAULT 0)''')
    c.execute('INSERT OR IGNORE INTO lembretes_sms_config(id,intervalos,mensagens) VALUES(1,?,?)',
              (json.dumps([30,240,1440]),json.dumps(TEXTOS)))
    c.execute('''CREATE TABLE IF NOT EXISTS lembretes_sms_propostas(
      conta TEXT NOT NULL,proposta TEXT NOT NULL,nome TEXT NOT NULL DEFAULT '',telefone TEXT NOT NULL DEFAULT '',
      link TEXT NOT NULL DEFAULT '',banco TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT '',
      criado_em TEXT NOT NULL,atualizado_em TEXT NOT NULL,evento_id INTEGER NOT NULL,
      formalizado_em TEXT,pago_em TEXT,cancelado_em TEXT,PRIMARY KEY(conta,proposta))''')
    c.execute('''CREATE TABLE IF NOT EXISTS lembretes_sms_envios(
      id INTEGER PRIMARY KEY AUTOINCREMENT,conta TEXT NOT NULL,proposta TEXT NOT NULL,etapa INTEGER NOT NULL,
      telefone TEXT NOT NULL,mensagem TEXT NOT NULL,estado TEXT NOT NULL,criado_em TEXT NOT NULL,
      enviado_em TEXT,erro TEXT NOT NULL DEFAULT '',sms_uid TEXT NOT NULL DEFAULT '',disparo_id INTEGER,
      UNIQUE(conta,proposta,etapa))''')
    c.execute('CREATE INDEX IF NOT EXISTS lembretes_sms_fone ON lembretes_sms_envios(telefone,criado_em)')

def config(c):
    d=dict(c.execute('SELECT * FROM lembretes_sms_config WHERE id=1').fetchone())
    d['habilitado']=bool(d['habilitado']);d['intervalos']=json.loads(d['intervalos']);d['mensagens']=json.loads(d['mensagens'])
    return d

def salvar(get_db,body):
    if type(body.get('habilitado')) is not bool: raise HTTPException(400,'Informe se os lembretes estão ativos.')
    ints=body.get('intervalos');msgs=body.get('mensagens');days=body.get('idade_maxima_dias',7)
    if not isinstance(ints,list) or not 1<=len(ints)<=3 or any(type(v) is not int or not 30<=v<=10080 for v in ints):
        raise HTTPException(400,'Configure de 1 a 3 lembretes, com intervalos de 30 a 10.080 minutos.')
    if not isinstance(msgs,list) or len(msgs)!=len(ints) or any(not isinstance(v,str) or v.count('{link}')!=1 or '{' in v.replace('{link}','') or '}' in v.replace('{link}','') or len(v)>160 for v in msgs):
        raise HTTPException(400,'Cada mensagem deve conter {link} uma vez e ter até 160 caracteres.')
    msgs=[''.join(ch for ch in unicodedata.normalize('NFKD',v) if not unicodedata.combining(ch)) for v in msgs]
    if any(len(v.replace('{link}','https://facta.ly/12345678'))>160 or any(ord(ch)>126 or ord(ch)<32 for ch in v) for v in msgs):
        raise HTTPException(400,'Use texto simples sem emojis, com espaço para o link Facta nos 160 caracteres.')
    if type(days) is not int or not 1<=days<=30: raise HTTPException(400,'Use idade máxima de 1 a 30 dias.')
    with closing(get_db()) as c,c:
        c.execute('UPDATE lembretes_sms_config SET habilitado=?,intervalos=?,mensagens=?,idade_maxima_dias=? WHERE id=1',
                  (int(body['habilitado']),json.dumps(ints),json.dumps(msgs),days))
    return {'ok':True}

def receber(c,d,ident,quando):
    ch=d.get('chat_summary') or {};p=d.get('proposal') or d.get('proposal_status') or {}
    if not isinstance(ch,dict) or not isinstance(p,dict):return
    conta=str(ch.get('account_id') or '');pid=str(p.get('proposal_id') or '')
    if not conta or not pid:return
    old=c.execute('SELECT * FROM lembretes_sms_propostas WHERE conta=? AND proposta=?',(conta,pid)).fetchone()
    if old and old['evento_id']>=ident:return
    ct=(ch.get('details') or {}).get('contact') or {}
    status=str(p.get('proposal_status') or p.get('status') or (old['status'] if old else ''))
    def campo(k,source,key):return str(source.get(key) or (old[k] if old else '') or '')
    formal=(old['formalizado_em'] if old else None) or (quando if status=='FORMALIZATION_COMPLETED' else None)
    pago=(old['pago_em'] if old else None) or (quando if status=='LIQUIDATED_TO_CUSTOMER' else None)
    cancel=(old['cancelado_em'] if old else None) or (quando if status=='CANCELED' else None)
    c.execute('''INSERT INTO lembretes_sms_propostas VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(conta,proposta) DO UPDATE SET nome=excluded.nome,telefone=excluded.telefone,
      link=excluded.link,banco=excluded.banco,status=excluded.status,atualizado_em=excluded.atualizado_em,
      evento_id=excluded.evento_id,formalizado_em=excluded.formalizado_em,pago_em=excluded.pago_em,cancelado_em=excluded.cancelado_em''',
      (conta,pid,campo('nome',ct,'name'),optout.telefone(campo('telefone',ct,'phone')),
       campo('link',p,'formalization_link'),campo('banco',p,'bank'),status,old['criado_em'] if old else quando,
       quando,ident,formal,pago,cancel))

def sincronizar(get_db):
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE');cursor=config(c)['cursor']
        rows=c.execute('SELECT id,payload_raw,recebido_em,conta_confirmada,repetido_de FROM vendeai_eventos WHERE id>? ORDER BY id LIMIT 1000',(cursor,)).fetchall()
        for r in rows:
            if r['conta_confirmada'] and r['repetido_de'] is None:
                try:receber(c,json.loads(r['payload_raw']),r['id'],r['recebido_em'])
                except (ValueError,TypeError,AttributeError):pass
        if rows:c.execute('UPDATE lembretes_sms_config SET cursor=? WHERE id=1',(rows[-1]['id'],))
        return len(rows)==1000

def link_valido(v):
    v=str(v).strip()
    if '://' not in v:v='https://'+v
    try:
        u=urlparse(v)
        return v if u.scheme=='https' and u.hostname=='facta.ly' and not u.username and not u.password and not u.port and u.path not in ('','/') and not re.search(r'\s',v) else ''
    except ValueError:return ''

def motivo(c,p,cfg,now,conta):
    if p['conta']!=str(conta):return 'Outra conta'
    if p['status']!='FORMALIZATION_REQUESTED' or p['formalizado_em'] or p['pago_em'] or p['cancelado_em']:return 'Não está aguardando assinatura'
    if p['banco'].lower()!='facta':return 'Outro banco'
    if not data(p['criado_em']) or now-data(p['criado_em'])>timedelta(days=cfg['idade_maxima_dias']):return 'Proposta antiga'
    if not re.fullmatch(r'55[1-9][0-9]9[0-9]{8}',p['telefone']):return 'Telefone inválido'
    if not link_valido(p['link']):return 'Link Facta inválido'
    if c.execute('SELECT 1 FROM optout WHERE telefone=?',(p['telefone'],)).fetchone():return 'Lista negra'
    return ''

def recuperar(get_db):
    with closing(get_db()) as c,c:
        c.execute("UPDATE disparos SET status='incerto',erro='Lembrete interrompido; sem reenvio automático.' WHERE id IN (SELECT disparo_id FROM lembretes_sms_envios WHERE estado='enviando')")
        c.execute("UPDATE lembretes_sms_envios SET estado='incerto',erro='Interrompido; confira no provedor. Sem reenvio automático.' WHERE estado='enviando'")

def processar_um(get_db,conta,token,horario,saldo,now=None):
    now=now or agora()
    if sincronizar(get_db):return False # termina o histórico antes de decidir qualquer envio
    with closing(get_db()) as c:
        cfg=config(c)
    if not cfg['habilitado'] or not token or not horario()[0]:return False
    info=saldo()
    if info.get('erro') or info.get('valor') is None or info['valor']<=0:return False
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE');cfg=config(c)
        if not cfg['habilitado']:return False
        # Novo webhook ainda nao projetado: aguardar a proxima rodada.
        if c.execute('SELECT COALESCE(MAX(id),0) FROM vendeai_eventos').fetchone()[0]>cfg['cursor']:return False
        escolhido=None
        for p in c.execute("SELECT * FROM lembretes_sms_propostas WHERE conta=? AND status='FORMALIZATION_REQUESTED' ORDER BY criado_em",(str(conta),)).fetchall():
            if motivo(c,p,cfg,now,conta):continue
            env=c.execute('SELECT * FROM lembretes_sms_envios WHERE conta=? AND proposta=? ORDER BY etapa',(str(conta),p['proposta'])).fetchall()
            if any(e['estado']!='sucesso' for e in env) or len(env)>=len(cfg['intervalos']):continue
            etapa=len(env);base=data(env[-1]['enviado_em']) if env else data(p['criado_em'])
            if now<base+timedelta(minutes=cfg['intervalos'][etapa]):continue
            recentes=c.execute("SELECT criado_em FROM lembretes_sms_envios WHERE telefone=? AND estado IN ('sucesso','enviando','incerto') AND criado_em>=?",(p['telefone'],(now-timedelta(days=1)).isoformat())).fetchall()
            if len(recentes)>=3 or any(now-data(e['criado_em'])<timedelta(hours=4) for e in recentes):continue
            msg=cfg['mensagens'][etapa].replace('{link}',link_valido(p['link']))
            msg=''.join(ch for ch in unicodedata.normalize('NFKD',msg) if not unicodedata.combining(ch))
            if len(msg)>160 or any(ord(ch)>126 or ord(ch)<32 for ch in msg):continue # nunca cortar o link
            ident=c.execute("INSERT INTO lembretes_sms_envios(conta,proposta,etapa,telefone,mensagem,estado,criado_em) VALUES(?,?,?,?,?,'enviando',?)",(str(conta),p['proposta'],etapa+1,p['telefone'],msg,now.isoformat())).lastrowid
            disp=c.execute("INSERT INTO disparos(numero_id,nome_lead,telefone_destino,status,criado_em,canal,tipo_envio) VALUES(0,?,?,'pendente',?,'sms','lembrete_assinatura')",(p['nome'],p['telefone'],now.isoformat())).lastrowid
            c.execute('UPDATE lembretes_sms_envios SET disparo_id=? WHERE id=?',(disp,ident));escolhido=(dict(p),msg,ident,disp);break
    if not escolhido:return False
    p,msg,ident,disp=escolhido
    # Releitura imediatamente antes do efeito externo.
    sincronizar(get_db)
    with closing(get_db()) as c:
        atual=c.execute('SELECT * FROM lembretes_sms_propostas WHERE conta=? AND proposta=?',(str(conta),p['proposta'])).fetchone()
        cfg=config(c);bloqueio=motivo(c,atual,cfg,agora(),conta)
        if not cfg['habilitado'] or not horario()[0]:bloqueio='Envio pausado'
        if atual['link']!=p['link'] or atual['telefone']!=p['telefone']:bloqueio='Dados alterados antes do envio'
        if c.execute('SELECT COALESCE(MAX(id),0) FROM vendeai_eventos').fetchone()[0]>cfg['cursor']:bloqueio='Atualização em processamento'
    if bloqueio:estado,erro,uid='cancelado',bloqueio,''
    else:estado,erro,uid=campanhas_sms.enviar(token,p['telefone'],msg)
    with closing(get_db()) as c,c:
        c.execute('UPDATE lembretes_sms_envios SET estado=?,erro=?,sms_uid=?,enviado_em=? WHERE id=?',(estado,erro,uid,agora().isoformat() if estado=='sucesso' else None,ident))
        c.execute('UPDATE disparos SET status=?,erro=?,sms_uid=? WHERE id=?',(estado,erro,uid,disp))
    return True

def relatorio(get_db,conta,inicio,fim):
    with closing(get_db()) as c:
        cfg=config(c);rows=[dict(r) for r in c.execute('''SELECT e.*,p.nome,p.status,p.formalizado_em,p.pago_em FROM lembretes_sms_envios e
          JOIN lembretes_sms_propostas p ON p.conta=e.conta AND p.proposta=e.proposta WHERE e.conta=? ORDER BY e.id DESC''',(str(conta),))]
        pendentes=sum(not motivo(c,p,cfg,agora(),conta) for p in c.execute('SELECT * FROM lembretes_sms_propostas WHERE conta=?',(str(conta),)).fetchall())
    primeiros={}
    for r in rows:
        if r['estado']=='sucesso':primeiros[r['proposta']]=r
    coorte=[r for r in primeiros.values() if inicio<=data(r['enviado_em'])<fim]
    def depois(r,campo):
        t=data(r[campo]);s=data(r['enviado_em'])
        return bool(t and s<=t<=s+timedelta(days=7))
    periodo=[r for r in rows if inicio<=data(r['criado_em'])<fim]
    return {'config':cfg,'pendentes_elegiveis':pendentes,'envios':periodo[:100],
      'resumo':{'sms_enviados':sum(r['estado']=='sucesso' for r in periodo),
      'falhas':sum(r['estado'] in ('erro','incerto') for r in periodo),'propostas_lembradas':len(coorte),
      'formalizadas_apos':sum(depois(r,'formalizado_em') for r in coorte),'pagas_apos':sum(depois(r,'pago_em') for r in coorte)}}
