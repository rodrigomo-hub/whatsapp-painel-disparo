"""Atribuição da equipe a um disparo anterior, sem alterar classificação financeira."""
import re
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from fastapi import HTTPException

TZ=ZoneInfo('America/Sao_Paulo')

def cpf(v):
    s=re.sub(r'\D','',str(v or ''))
    return s.zfill(11) if 0<len(s)<=11 else ''

def telefone(v):
    s=re.sub(r'\D','',str(v or ''))
    if len(s) in (10,11):s='55'+s
    if len(s)==12 and s.startswith('55') and s[4] in '6789':s=s[:4]+'9'+s[4:]
    return s if s.startswith('55') and len(s) in (12,13) else ''

def instante(v):
    try:
        d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)
    except ValueError:return None

def encontrar(envios,documento,fone,referencia):
    doc,phone=cpf(documento),telefone(fone)
    if not referencia or not (doc or phone):return None
    candidatos=[]
    for s in envios:
        t=instante(s['criado_em'])
        if not t or not referencia-timedelta(days=7)<=t<=referencia:continue
        sd,sp=cpf(s.get('cpf')),telefone(s.get('telefone_destino'))
        # Se ambos foram informados, não aceitar um CPF/telefone contraditório.
        if doc and sd and doc!=sd:continue
        if phone and sp and phone!=sp:continue
        if (doc and doc==sd) or (phone and phone==sp):candidatos.append((t,s['id'],s))
    return max(candidatos,key=lambda x:(x[0],x[1]))[2] if candidatos else None

def salvar(c,ident,item,old=None):
    old=dict(old or {})
    contato_cpf=cpf(item.get('cpf',old.get('cpf','')))
    contato_fone=telefone(item.get('telefone',old.get('telefone','')))
    if item.get('cpf') and not contato_cpf:raise HTTPException(400,'CPF inválido: informe até 11 dígitos.')
    if item.get('telefone') and not contato_fone:raise HTTPException(400,'Telefone inválido: informe DDD e número.')
    modo=str(item.get('waba_manual',old.get('waba_manual','')) or '')
    quando=str(item.get('proposta_em',old.get('proposta_em','')) or '')
    if old and all(old.get(k,'')==v for k,v in [('cpf',contato_cpf),('telefone',contato_fone),('waba_manual',modo),('proposta_em',quando),('dia',item['dia'])]):
        return
    ref=instante(quando) if quando else min(datetime.fromisoformat(item['dia']).replace(tzinfo=TZ)+timedelta(days=1,microseconds=-1),datetime.now(TZ))
    if quando and (not ref or ref>datetime.now(TZ) or ref.date().isoformat()>item['dia']):
        raise HTTPException(400,'Data/hora da proposta deve ser válida e anterior ao pagamento.')
    waba='';numero=None;envio=None;fonte='nao_identificada'
    if modo and modo!='nenhuma':
        if not c.execute('SELECT 1 FROM wabas_salvas WHERE waba_id=?',(modo,)).fetchone():raise HTTPException(400,'WABA não encontrada.')
        waba=modo;fonte='manual'
    elif modo=='nenhuma':fonte='nao_identificada'
    elif contato_cpf or contato_fone:
        envios=[dict(r) for r in c.execute("SELECT id,numero_id,cpf,telefone_destino,criado_em FROM disparos WHERE status='sucesso' AND canal='whatsapp' AND julianday(criado_em)>=julianday(?) AND julianday(criado_em)<=julianday(?)",((ref-timedelta(days=7)).isoformat(),ref.isoformat()))]
        s=encontrar(envios,contato_cpf,contato_fone,ref)
        if s:
            r=c.execute('SELECT waba_id,tipo_envio FROM remetentes_historicos WHERE numero_id=?',(s['numero_id'],)).fetchone()
            if r and r['waba_id'] and r['tipo_envio']!='vendeai_normal':
                waba=r['waba_id'];numero=s['numero_id'];envio=s['id'];fonte='disparo' if quando else 'estimada_dia_pagamento'
    c.execute('''UPDATE propostas_equipe SET cpf=?,telefone=?,waba_manual=?,proposta_em=?,
        waba_id=?,numero_origem_id=?,disparo_origem_id=?,atribuicao_fonte=? WHERE id=?''',
        (contato_cpf,contato_fone,modo,quando,waba,numero,envio,fonte,ident))
