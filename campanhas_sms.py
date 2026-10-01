"""Campanhas SMS explícitas, com lista revisada e sem retomada automática."""
import json
import re
import uuid
import requests
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import HTTPException
import optout

def agora():return datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat()

def migrar(c):
    c.execute('''CREATE TABLE IF NOT EXISTS campanhas_sms(
        id TEXT PRIMARY KEY,nome TEXT NOT NULL,mensagem TEXT NOT NULL,estado TEXT NOT NULL DEFAULT 'rascunho',
        criado_em TEXT NOT NULL,iniciado_em TEXT,finalizado_em TEXT,motivo TEXT NOT NULL DEFAULT '',
        importacao TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS campanhas_sms_itens(
        campanha_id TEXT NOT NULL,telefone TEXT NOT NULL,estado TEXT NOT NULL DEFAULT 'pendente',
        disparo_id INTEGER,erro TEXT NOT NULL DEFAULT '',PRIMARY KEY(campanha_id,telefone))''')
    c.execute('CREATE INDEX IF NOT EXISTS sms_itens_estado ON campanhas_sms_itens(campanha_id,estado)')
    if 'campanha_sms_id' not in {r['name'] for r in c.execute('PRAGMA table_info(disparos)')}:
        c.execute('ALTER TABLE disparos ADD COLUMN campanha_sms_id TEXT')

def criar(get_db,nome,mensagem,linhas,erros):
    if not isinstance(nome,str) or not 1<=len(nome.strip())<=100:raise HTTPException(400,'Dê um nome à campanha, com até 100 caracteres.')
    if not mensagem.strip() or len(mensagem)>160 or '{' in mensagem or '}' in mensagem:
        raise HTTPException(400,'Configure uma mensagem SMS fixa, sem variáveis e com até 160 caracteres.')
    if not 1<=len(linhas)+len(erros)<=20000:raise HTTPException(400,'Use de 1 a 20.000 linhas.')
    total_linhas=len(linhas)+len(erros)
    vistos=set();validos=[];repetidos=bloqueados=0;erros=list(erros)
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        for row in linhas:
            p=optout.telefone(row['lead'].get('telefone'))
            if not re.fullmatch(r'55[1-9][0-9]9[0-9]{8}',p):
                erros.append({'linha':row['linha'],'motivo':'Celular inválido. Informe DDD + número.'});continue
            if p in vistos:repetidos+=1;continue
            vistos.add(p)
            if c.execute('SELECT 1 FROM optout WHERE telefone=?',(p,)).fetchone():bloqueados+=1;continue
            validos.append(p)
        if not validos:raise HTTPException(400,'Nenhum telefone válido disponível após remover inválidos e lista negra.')
        ident=uuid.uuid4().hex
        info={'linhas':total_linhas,
              'validos':len(validos),'repetidos':repetidos,'lista_negra':bloqueados,'invalidos':len(erros),'erros':erros[:50]}
        c.execute('INSERT INTO campanhas_sms(id,nome,mensagem,criado_em,importacao) VALUES(?,?,?,?,?)',(ident,nome.strip(),mensagem,agora(),json.dumps(info)))
        c.executemany('INSERT INTO campanhas_sms_itens(campanha_id,telefone) VALUES(?,?)',[(ident,p) for p in validos])
    return detalhe(get_db,ident)

def detalhe(get_db,ident):
    with closing(get_db()) as c:
        r=c.execute('SELECT * FROM campanhas_sms WHERE id=?',(ident,)).fetchone()
        if not r:raise HTTPException(404,'Campanha não encontrada.')
        d=dict(r);d['importacao']=json.loads(d['importacao'])
        d['contagens']={r['estado']:r['n'] for r in c.execute('SELECT estado,count(*) n FROM campanhas_sms_itens WHERE campanha_id=? GROUP BY estado',(ident,))}
        d['itens']=[dict(r) for r in c.execute('SELECT telefone,estado,erro FROM campanhas_sms_itens WHERE campanha_id=? ORDER BY rowid DESC LIMIT 100',(ident,))]
        return d

def listar(get_db):
    with closing(get_db()) as c:return [dict(r) for r in c.execute('SELECT id,nome,estado,criado_em FROM campanhas_sms ORDER BY criado_em DESC LIMIT 100')]

def parar(c,ident,motivo):
    c.execute("UPDATE campanhas_sms SET estado='encerrada',motivo=?,finalizado_em=? WHERE id=? AND estado IN ('rascunho','enviando')",(motivo,agora(),ident))
    c.execute("UPDATE campanhas_sms_itens SET estado='cancelado',erro=? WHERE campanha_id=? AND estado='pendente'",(motivo,ident))

def iniciar(get_db,ident,token,horario,saldo):
    if not token:raise HTTPException(400,'Configure o token de SMS antes de iniciar.')
    permitido,motivo=horario()
    if not permitido:raise HTTPException(409,motivo)
    info=saldo(True)
    if info.get('erro') or info.get('valor') is None:raise HTTPException(409,'Não foi possível confirmar o saldo no provedor. Tente novamente.')
    if info['valor']<=0:raise HTTPException(409,'Sem saldo de SMS disponível.')
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT estado FROM campanhas_sms WHERE id=?',(ident,)).fetchone()
        if not row:raise HTTPException(404,'Campanha não encontrada.')
        if row['estado']=='enviando':return {'status':'enviando'}
        if row['estado']!='rascunho':raise HTTPException(409,'Campanha já iniciada ou encerrada. Não será reenviada.')
        if c.execute("SELECT 1 FROM campanhas_sms WHERE estado='enviando'").fetchone():raise HTTPException(409,'Já há uma campanha SMS em andamento.')
        c.execute("UPDATE campanhas_sms SET estado='enviando',iniciado_em=? WHERE id=?",(agora(),ident))
    return {'status':'enviando'}

def cancelar(get_db,ident):
    with closing(get_db()) as c,c:parar(c,ident,'Cancelada pelo operador. Um envio já em andamento pode ser concluído.')
    return {'status':'encerrada'}

def recuperar(get_db):
    with closing(get_db()) as c,c:
        c.execute("UPDATE disparos SET status='incerto',erro='Processo interrompido; conferir no provedor, sem reenvio.' WHERE id IN (SELECT disparo_id FROM campanhas_sms_itens WHERE estado='enviando')")
        c.execute("UPDATE campanhas_sms_itens SET estado='incerto',erro='Processo interrompido; sem reenvio automático.' WHERE estado='enviando'")
        for r in c.execute("SELECT id FROM campanhas_sms WHERE estado='enviando'").fetchall():parar(c,r['id'],'Interrompida por reinício. Pendentes cancelados, sem retomada automática.')

def enviar(token,telefone,mensagem):
    try:
        r=requests.post('https://sms.gtisms.com/api/v3/sms/send',headers={'Authorization':'Bearer '+token,'Accept':'application/json'},
                        json={'recipient':telefone,'message':mensagem},timeout=(5,15),allow_redirects=False)
        if 200<=r.status_code<300:
            d=r.json()
            if d.get('status')=='success':return 'sucesso','',str((d.get('data') or {}).get('uid') or '')
            return 'erro','Provedor recusou o SMS. Confira a plataforma.',''
        return ('erro' if 400<=r.status_code<500 else 'incerto'),'HTTP '+str(r.status_code)+' no provedor de SMS.',''
    except (requests.RequestException,ValueError,AttributeError,TypeError):return 'incerto','Sem confirmação do provedor. Confira antes de reenviar.',''

def processar_um(get_db,token,horario,saldo):
    with closing(get_db()) as c:r=c.execute("SELECT * FROM campanhas_sms WHERE estado='enviando' ORDER BY criado_em LIMIT 1").fetchone()
    if not r:return False
    r=dict(r);permitido,motivo=horario();info=saldo() if permitido and token else {}
    if not permitido or not token or info.get('erro') or info.get('valor') is None or info['valor']<=0:
        with closing(get_db()) as c,c:parar(c,r['id'],motivo if not permitido else 'Saldo ou acesso SMS indisponível. Pendentes cancelados.')
        return True
    with closing(get_db()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT estado FROM campanhas_sms WHERE id=?',(r['id'],)).fetchone()[0]!='enviando':return False
        item=c.execute("SELECT * FROM campanhas_sms_itens WHERE campanha_id=? AND estado='pendente' ORDER BY rowid LIMIT 1",(r['id'],)).fetchone()
        if not item:
            c.execute("UPDATE campanhas_sms SET estado='concluida',finalizado_em=? WHERE id=?",(agora(),r['id']));return True
        phone=item['telefone']
        if c.execute('SELECT 1 FROM optout WHERE telefone=?',(phone,)).fetchone():
            c.execute("UPDATE campanhas_sms_itens SET estado='bloqueado',erro='Lista negra' WHERE campanha_id=? AND telefone=?",(r['id'],phone));return True
        disparo=c.execute("INSERT INTO disparos(numero_id,telefone_destino,status,criado_em,canal,tipo_envio,campanha_sms_id) VALUES(0,?,'pendente',?,'sms','',?)",(phone,agora(),r['id'])).lastrowid
        c.execute("UPDATE campanhas_sms_itens SET estado='enviando',disparo_id=? WHERE campanha_id=? AND telefone=?",(disparo,r['id'],phone))
    with closing(get_db()) as c:
        bloqueado=c.execute('SELECT 1 FROM optout WHERE telefone=?',(phone,)).fetchone()
        ativo=c.execute('SELECT estado FROM campanhas_sms WHERE id=?',(r['id'],)).fetchone()[0]=='enviando'
    if bloqueado or not ativo:estado,erro,uid='cancelado','Envio cancelado antes do POST.',''
    else:estado,erro,uid=enviar(token,phone,r['mensagem'])
    with closing(get_db()) as c,c:
        c.execute('UPDATE disparos SET status=?,erro=?,sms_uid=? WHERE id=?',(estado,erro,uid,disparo))
        c.execute('UPDATE campanhas_sms_itens SET estado=?,erro=? WHERE campanha_id=? AND telefone=?',(estado,erro,r['id'],phone))
        if estado in ('erro','incerto'):parar(c,r['id'],'Envio sem confirmação ou recusado. Confira o provedor antes de uma nova campanha.')
    return True
