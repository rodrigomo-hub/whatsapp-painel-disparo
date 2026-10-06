"""Identificação histórica dos remetentes e ranking de pagamentos atribuídos."""

def migrar(c):
    c.execute('''CREATE TABLE IF NOT EXISTS remetentes_historicos(
        numero_id INTEGER PRIMARY KEY,nome TEXT NOT NULL,waba_id TEXT NOT NULL,
        phone_number_id TEXT NOT NULL,tipo_envio TEXT NOT NULL,fonte TEXT NOT NULL)''')
    c.execute('''INSERT OR IGNORE INTO remetentes_historicos
        SELECT id,nome,waba_id,phone_number_id,tipo_envio,'cadastro' FROM numeros''')
    for evento,sufixo,ref in [('AFTER INSERT','insert','NEW'),('AFTER UPDATE','update','NEW'),('BEFORE DELETE','delete','OLD')]:
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS remetente_snapshot_{sufixo} {evento} ON numeros
            BEGIN INSERT OR REPLACE INTO remetentes_historicos
            VALUES({ref}.id,{ref}.nome,{ref}.waba_id,{ref}.phone_number_id,{ref}.tipo_envio,'cadastro'); END''')


def calcular(numeros,remetentes,nomes,equipe=()):
    grupos={};sem=0
    for n in numeros:
        quantidade=len(n['pagos']) if isinstance(n['pagos'],set) else n['pagos']
        if not quantidade:continue
        remetente=remetentes.get(n['numero_id'],{})
        waba=remetente.get('waba_id','')
        if not waba or remetente.get('tipo_envio')=='vendeai_normal':
            sem+=quantidade;continue
        g=grupos.setdefault(waba,{'waba_id':waba,'nome':nomes.get(waba) or 'WABA '+waba,
                                 'pagos':0,'valor_pago':0,'ia_pagos':0,'ia_valor':0,'equipe_pagos':0,'equipe_valor':0,'numeros':[]})
        g['pagos']+=quantidade;g['valor_pago']+=n['valor_pago'];g['ia_pagos']+=quantidade;g['ia_valor']+=n['valor_pago']
        g['numeros'].append({'nome':remetente.get('nome') or n['numero'],
                            'pagos':quantidade,'valor_pago':round(n['valor_pago'],2),'origem':'IA'})
    for e in equipe:
        waba=e.get('waba_id')
        if not waba:continue
        g=grupos.setdefault(waba,{'waba_id':waba,'nome':nomes.get(waba) or 'WABA '+waba,
                                 'pagos':0,'valor_pago':0,'ia_pagos':0,'ia_valor':0,'equipe_pagos':0,'equipe_valor':0,'numeros':[]})
        v=e['valor_centavos']/100
        g['pagos']+=1;g['valor_pago']+=v;g['equipe_pagos']+=1;g['equipe_valor']+=v
        n=remetentes.get(e.get('numero_origem_id'),{})
        g['numeros'].append({'nome':n.get('nome') or 'WABA informada no lançamento','pagos':1,'valor_pago':v,'origem':'Equipe'})
    for g in grupos.values():
        g['ia_valor']=round(g['ia_valor'],2);g['equipe_valor']=round(g['equipe_valor'],2)
        g['valor_pago']=round(g['valor_pago'],2)
        g['numeros'].sort(key=lambda x:(-x['pagos'],-x['valor_pago'],x['nome']))
    return {'ranking':sorted(grupos.values(),key=lambda g:(-g['pagos'],-g['valor_pago'],g['nome'])),
            'pagos_atribuidos':sum(g['pagos'] for g in grupos.values()),'pagos_envio_sem_waba':sem}
