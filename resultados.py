"""Leitura dos eventos preservados. Nenhuma chamada externa ou alteração de dados."""
import ranking_wabas
import comissoes
import custos_meta
import bisect
import json
import math
import re
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/Sao_Paulo')
PAGO = 'LIQUIDATED_TO_CUSTOMER'
STATUS = {'FORMALIZATION_REQUESTED': 'Aguardando formalização',
          'FORMALIZATION_COMPLETED': 'Formalização concluída', 'PROCESSING': 'Em processamento',
          'PENDED_AI_HANDLE': 'Pendência em atendimento', 'PENDED_BANK': 'Pendência no banco',
          'CANCELED': 'Cancelado', PAGO: 'Pago ao cliente'}


def migrar(c):
    comissoes.migrar(c)
    c.execute('''CREATE TABLE IF NOT EXISTS propostas_ia_importadas (
        account_id TEXT NOT NULL, codigo TEXT NOT NULL, cliente TEXT NOT NULL,
        produto TEXT NOT NULL, banco TEXT NOT NULL, valor_centavos INTEGER NOT NULL,
        status TEXT NOT NULL, data_referencia TEXT NOT NULL, fonte TEXT NOT NULL,
        arquivo_sha256 TEXT NOT NULL, importado_em TEXT NOT NULL,
        PRIMARY KEY(account_id,codigo))''')
    c.execute('''CREATE TABLE IF NOT EXISTS ajustes_equipe_mensais (
        mes TEXT PRIMARY KEY, valor_centavos INTEGER NOT NULL,
        motivo TEXT NOT NULL, criado_em TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pagamentos_manuais (
        dia TEXT PRIMARY KEY, quantidade INTEGER NOT NULL CHECK(quantidade>=0),
        valor_centavos INTEGER NOT NULL CHECK(valor_centavos>=0),
        revisao INTEGER NOT NULL DEFAULT 1, atualizado_em TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pagamentos_ia_historicos (
        account_id TEXT NOT NULL, codigo TEXT NOT NULL, proposta_id TEXT NOT NULL DEFAULT '',
        dia TEXT NOT NULL, valor_centavos INTEGER NOT NULL CHECK(valor_centavos>=0),
        data_referencia TEXT NOT NULL, fonte TEXT NOT NULL, arquivo_sha256 TEXT NOT NULL,
        importado_em TEXT NOT NULL, PRIMARY KEY(account_id,codigo))''')


def conciliar_historico(propostas, historicos, inicio, fim, atividade):
    """Substitui somente a contribuição financeira de códigos conciliados.

    Não fabrica eventos nem atribui histórico a números/horários. A subtração
    dinâmica impede duplicação se um webhook do mesmo contrato chegar depois.
    """
    aliases = defaultdict(set)
    for key, p in propostas.items():
        for codigo in [p['id'], str(p['dados'].get('proposal_number') or '')]:
            if codigo: aliases[(key[0], codigo)].add(key)
    delta = 0
    sem_valor = atividade['pagos_sem_valor']
    vistos = set()
    registros = 0
    for h in historicos:
        keys = aliases.get((h['account_id'], h['codigo']), set()) | aliases.get((h['account_id'], h['proposta_id']), set())
        if len(keys) > 1 or (keys and next(iter(keys)) in vistos):
            raise ValueError('Histórico com identificação de contrato ambígua.')
        if keys:
            key = next(iter(keys)); vistos.add(key); p = propostas[key]
            if p['pago'] and inicio <= p['pago'] < fim:
                v = valor(p['dados'].get('liquid_value'))
                if v is None: sem_valor -= 1
                else: delta -= int(Decimal(str(v))*100)
        if inicio.date().isoformat() <= h['dia'] < fim.date().isoformat():
            delta += h['valor_centavos']; registros += 1
    total = int(Decimal(str(round(atividade['valor_pago'], 2)))*100) + delta
    return {'quantidade': len(atividade['pagos']), 'valor': total/100,
            'sem_valor': sem_valor, 'ajuste_historico': delta/100, 'historicos_no_periodo': registros}


def integrar_propostas_importadas(propostas, importadas):
    aliases = defaultdict(set)
    for key, p in propostas.items():
        for codigo in [p['id'], str(p['dados'].get('proposal_number') or '')]:
            if codigo: aliases[(key[0], codigo)].add(key)
    usados = set()
    for h in importadas:
        t = momento(h['data_referencia'])
        if not t:
            raise ValueError('Proposta importada sem data de referência válida.')
        keys = aliases.get((h['account_id'], h['codigo']), set())
        if len(keys) > 1:
            raise ValueError('Código importado corresponde a mais de uma proposta.')
        key = next(iter(keys)) if keys else (h['account_id'], 'importado:'+h['codigo'])
        if key in usados:
            raise ValueError('Mais de um código importado corresponde à mesma proposta.')
        usados.add(key)
        p = propostas.setdefault(key, {'id': h['codigo'], 'chat': None, 'criado': None,
                                       'pago': None, 'atualizado': t, 'dados': {}, 'status': ''})
        p['importada'] = True
        p['nome_importado'] = h['cliente']
        if p['criado'] is None:
            p['criado'] = t
            p['criacao_referencia'] = True
        for field, val in [('proposal_number', h['codigo']), ('bank', h['banco']),
                           ('product', h['produto']), ('liquid_value', h['valor_centavos']/100)]:
            if p['dados'].get(field) in (None, ''): p['dados'][field] = val
        # Não regride um status recebido depois da referência do arquivo.
        if t >= p['atualizado']:
            p['status'] = h['status']; p['atualizado'] = t
        if h['status'] == PAGO and p['pago'] is None:
            p['pago'] = t


def validar_dia(dia):
    try:
        d = date.fromisoformat(dia)
        if d.isoformat() != dia or d > datetime.now(TZ).date():
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, 'Informe uma data válida, até hoje.')
    return dia


def manual_dia(get_db, dia):
    validar_dia(dia)
    with closing(get_db()) as c:
        row = c.execute('SELECT * FROM pagamentos_manuais WHERE dia=?', (dia,)).fetchone()
    return dict(row) if row else {'dia': dia, 'quantidade': 0, 'valor_centavos': 0,
                                 'revisao': 0, 'atualizado_em': None}


def salvar_manual(get_db, dia, body):
    validar_dia(dia)
    modo = body.get('modo', 'substituir')
    if modo not in ('adicionar', 'substituir'):
        raise HTTPException(400, 'Operação inválida.')
    rev = body.get('revisao')
    if type(rev) is not int or rev < 0:
        raise HTTPException(400, 'Informe uma revisão válida.')
    try:
        texto = str(body.get('valor')).strip()
        if texto.startswith('R$'):
            texto = texto[2:].strip()
        if ',' in texto:
            if not re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+),[0-9]{1,2}', texto):
                raise ValueError()
            texto = texto.replace('.', '').replace(',', '.')
        elif not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', texto):
            raise ValueError()
        v = Decimal(texto)
        if not v.is_finite() or not 0 <= v <= Decimal('999999999.99') or v * 100 != (v * 100).to_integral_value():
            raise ValueError()
        centavos = int(v * 100)
    except (ValueError, InvalidOperation):
        raise HTTPException(400, 'Informe um valor válido com até duas casas decimais.')
    with closing(get_db()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM pagamentos_manuais WHERE dia=?', (dia,)).fetchone()
        if modo == 'adicionar' and centavos <= 0:
            raise HTTPException(400, 'Informe um valor maior que zero para adicionar.')
        if modo == 'substituir' and row and row['valor_centavos'] == centavos:
            return dict(row)
        if rev != (row['revisao'] if row else 0):
            raise HTTPException(409, 'Este dia foi alterado em outra tela. Reabra o lançamento antes de salvar.')
        if modo == 'adicionar':
            centavos += row['valor_centavos'] if row else 0
            if centavos > 99999999999:
                raise HTTPException(400, 'O total do dia ultrapassa o valor permitido.')
        c.execute('''INSERT INTO pagamentos_manuais(dia,quantidade,valor_centavos,revisao,atualizado_em)
                     VALUES(?,?,?,?,?) ON CONFLICT(dia) DO UPDATE SET valor_centavos=excluded.valor_centavos,revisao=excluded.revisao,atualizado_em=excluded.atualizado_em''',
                  (dia, 0, centavos, rev+1, datetime.now(TZ).isoformat()))
        return dict(c.execute('SELECT * FROM pagamentos_manuais WHERE dia=?', (dia,)).fetchone())


def objeto(v):
    return v if isinstance(v, dict) else {}


def momento(v):
    try:
        d = datetime.fromisoformat(str(v))
        return (d.replace(tzinfo=TZ) if not d.tzinfo else d.astimezone(TZ))
    except (ValueError, TypeError):
        return None


def telefone(v):
    p = re.sub(r'\D', '', str(v or ''))
    if len(p) in (10, 11):
        p = '55' + p
    # Unifica a representação antiga dos celulares brasileiros.
    if len(p) == 12 and p.startswith('55') and p[4] in '6789':
        p = p[:4] + '9' + p[4:]
    return p if len(p) in (12, 13) and p.startswith('55') else ''


def valor(v):
    try:
        x = float(v)
        return x if not isinstance(v, bool) and math.isfinite(x) and x >= 0 else None
    except (ValueError, TypeError):
        return None


def calcular(get_db, inicio, fim, busca='', origem='', status='', pagina=1, agora=None):
    agora = agora or datetime.now(TZ)
    with closing(get_db()) as c:
        c.execute('BEGIN')
        remetentes = {r['numero_id']:dict(r) for r in c.execute('SELECT * FROM remetentes_historicos')} if c.execute("SELECT 1 FROM sqlite_master WHERE name='remetentes_historicos'").fetchone() else {}
        nomes_wabas = {r['waba_id']:r['nome'] for r in c.execute('SELECT waba_id,nome FROM wabas_salvas')} if c.execute("SELECT 1 FROM sqlite_master WHERE name='wabas_salvas'").fetchone() else {}
        eventos = c.execute('SELECT id,recebido_em,payload_raw FROM vendeai_eventos '
                            'WHERE conta_confirmada=1 AND repetido_de IS NULL ORDER BY id').fetchall()
        envios = c.execute("SELECT d.id,d.numero_id,d.telefone_destino,d.criado_em,d.canal,"
                           "COALESCE(NULLIF(d.tipo_envio,''),n.tipo_envio,'') tipo_envio,"
                           "n.nome FROM disparos d LEFT JOIN numeros n ON n.id=d.numero_id "
                           "WHERE d.status='sucesso'").fetchall()
        manuais = [dict(r) for r in c.execute('SELECT * FROM pagamentos_manuais WHERE dia>=? AND dia<? ORDER BY dia DESC',
                                             (inicio.date().isoformat(), fim.date().isoformat()))]
        regras = comissoes.carregar_regras(c)
        equipe = [dict(r) for r in c.execute('SELECT * FROM propostas_equipe WHERE excluido=0 AND dia>=? AND dia<? ORDER BY dia DESC,atualizado_em DESC',(inicio.date().isoformat(),fim.date().isoformat()))]
        custo = custos_meta.resumo(c,inicio,fim) if c.execute("SELECT 1 FROM sqlite_master WHERE name='custos_meta'").fetchone() else None
        historicos = [dict(r) for r in c.execute('SELECT * FROM pagamentos_ia_historicos')]
        importadas = [dict(r) for r in c.execute('SELECT * FROM propostas_ia_importadas')]
        ajustes_mensais = []
        for row in c.execute('SELECT * FROM ajustes_equipe_mensais ORDER BY mes DESC'):
            a = date.fromisoformat(row['mes']+'-01')
            b = date(a.year+a.month//12, a.month%12+1, 1)
            if a < fim.date() and b > inicio.date():
                ajustes_mensais.append(dict(row, incluido=inicio.date() <= a and fim.date() >= b))
    chats, propostas, sends, por_fone = {}, {}, {}, defaultdict(list)
    for row in envios:
        r = dict(row); t = momento(r['criado_em'])
        if not t:
            continue
        r['t'] = t
        r['tipo'] = ('normais' if r['tipo_envio'] == 'vendeai_normal' else
                     'oficiais' if r['tipo_envio'] in ('meta','gupshup') else 'outros') if r['canal'] == 'whatsapp' else 'outros'
        sends[r['id']] = r
        fone = telefone(r['telefone_destino'])
        if fone:
            por_fone[fone].append((t, r['id']))
    for items in por_fone.values():
        items.sort()
    primeiro_evento = None
    for row in eventos:
        try:
            d = objeto(json.loads(row['payload_raw']))
        except (ValueError, TypeError):
            continue
        e = str(d.get('event') or '')
        t = momento(row['recebido_em'])
        ch = objeto(d.get('chat_summary')); cid = str(ch.get('chat_id') or '')
        if not t or not cid or e.startswith('teste_'):
            continue
        primeiro_evento = min(primeiro_evento or t, t)
        key = (str(ch.get('account_id') or ''), cid)
        chat = chats.setdefault(key, {'primeiro': t, 'criado': None, 'simulacoes': [],
                                     'contato': {}, 'campanha': '', 'anuncio': False, 'envio': None})
        details = objeto(ch.get('details')); contact = objeto(details.get('contact'))
        chat['contato'].update({k: v for k, v in contact.items() if v not in (None, '')})
        session = objeto(details.get('session')); ref = objeto(details.get('referral'))
        chat['campanha'] = session.get('campaign') or chat['campanha']
        chat['anuncio'] |= bool(ref.get('ctwa_clid') or
                               (ref.get('ctwa_source_id') and str(ref.get('ctwa_source_type', '')).lower() == 'ad'))
        if e == 'conversation_created':
            chat['criado'] = min(chat['criado'] or t, t)
        if e == 'simulation_offered':
            chat['simulacoes'].append(t)
        p = objeto(d.get('proposal')) or objeto(d.get('proposal_status'))
        pid = str(p.get('proposal_id') or '')
        if not pid:
            continue
        prop = propostas.setdefault((key[0], pid), {'id': pid, 'chat': key, 'criado': None,
                                                   'pago': None, 'atualizado': t, 'dados': {}, 'status': ''})
        if str(d.get('origin') or '').strip().lower() == 'portal':
            prop['equipe_portal'] = True
        prop['dados'].update({k: v for k, v in p.items() if v not in (None, '')})
        estado = str(p.get('proposal_status') or p.get('status') or '')
        if estado and t >= prop['atualizado']:
            prop['status'] = estado
            prop['atualizado'] = t
        if e == 'proposal_created':
            prop['criado'] = min(prop['criado'] or t, t)
        if estado == PAGO:
            prop['pago'] = min(prop['pago'] or t, t)
    integrar_propostas_importadas(propostas, importadas)
    propostas_portal = {k:p for k,p in propostas.items() if p.get('equipe_portal')}
    propostas = {k:p for k,p in propostas.items() if not p.get('equipe_portal')}
    aliases_portal = {(k[0], str(codigo)) for k,p in propostas_portal.items()
                      for codigo in (p['id'],p['dados'].get('proposal_number')) if codigo}
    historicos_portal = [h for h in historicos if (h['account_id'],h['codigo']) in aliases_portal or (h['account_id'],h['proposta_id']) in aliases_portal]
    historicos = [h for h in historicos if h not in historicos_portal]
    # Atribuição conservadora: conversa criada + último envio ao telefone até 7 dias antes.
    # Evidência de anúncio prevalece; campanha isolada não comprova tráfego pago.
    for chat in chats.values():
        t = chat['criado']; items = por_fone.get(telefone(chat['contato'].get('phone')), [])
        idx = bisect.bisect_right(items, (t, math.inf)) - 1 if t else -1
        if not chat['anuncio'] and idx >= 0 and t - items[idx][0] <= timedelta(days=7):
            chat['envio'] = sends[items[idx][1]]
        chat['origem'] = ('trafego' if chat['anuncio'] else chat['envio']['tipo'] if chat['envio'] else 'nao_identificada')

    def dentro(t):
        return bool(t and inicio <= t < fim)

    def metricas():
        return {'disparos': 0, 'conversas': set(), 'simulacoes': set(), 'propostas': set(),
                'pagos': set(), 'valor_propostas': 0, 'valor_pago': 0,
                'propostas_sem_valor': 0, 'pagos_sem_valor': 0}

    def cliente(key):
        ct = chats[key]['contato']
        cpf = re.sub(r'\D', '', str(ct.get('cpf') or ''))
        return (key[0], cpf.zfill(11) if cpf else telefone(ct.get('phone')) or key[1])

    atividade, coorte = metricas(), metricas()
    grupos, numeros, janelas = {'oficiais': metricas(), 'normais': metricas()}, {}, {}

    def grupo(chat):
        tipo = chat['origem']; envio = chat['envio']
        g = grupos.setdefault(tipo, metricas())
        n = None
        if envio:
            nk = (envio['numero_id'], envio['tipo'])
            n = numeros.setdefault(nk, dict(metricas(), numero=envio['nome'] or 'Número removido',
                                            numero_id=envio['numero_id'], tipo=envio['tipo']))
        return [atividade, g] + ([n] if n is not None else [])

    def janela(envio):
        h = envio['t'].hour // 3 * 3
        return janelas.setdefault(h, dict(metricas(), inicio=h, fim=h+3))

    for s in sends.values():
        if not dentro(s['t']):
            continue
        g = grupos.setdefault(s['tipo'], metricas())
        n = numeros.setdefault((s['numero_id'], s['tipo']), dict(metricas(), numero=s['nome'] or 'Número removido',
                                                                numero_id=s['numero_id'], tipo=s['tipo']))
        for m in [atividade, coorte, g, n, janela(s)]:
            m['disparos'] += 1
    for key, chat in chats.items():
        targets = grupo(chat)
        for m in targets:
            if dentro(chat['criado']): m['conversas'].add(key)
            if any(dentro(t) for t in chat['simulacoes']): m['simulacoes'].add(cliente(key))
        if chat['envio'] and dentro(chat['envio']['t']):
            for m in [coorte, janela(chat['envio'])]:
                m['conversas'].add(key)
                if chat['simulacoes']: m['simulacoes'].add(cliente(key))
    comissao = {'prevista_ia':0, 'pagos_ia':0, 'pagos_equipe':sum(e['comissao_centavos'] or 0 for e in equipe)/100,
                'propostas_ia_sem_regra':0,'pagos_ia_sem_regra':0,
                'equipe_sem_regra':sum(m['valor_centavos'] for m in manuais)/100+sum(m['valor_centavos'] for m in ajustes_mensais if m['incluido'])/100}
    equipe_portal = []
    portal_valor = 0
    portal_pagos = 0
    portal_sem_valor = 0
    portal_sem_comissao = 0
    for key,p in propostas_portal.items():
        dados=p['dados'];v=valor(dados.get('liquid_value'));pago_ref=p['pago']
        hs=[h for h in historicos_portal if h['account_id']==key[0] and (h['codigo'] in (p['id'],str(dados.get('proposal_number') or '')) or h['proposta_id']==p['id'])]
        if hs:
            pago_ref=momento(hs[0]['dia']+'T12:00:00-03:00');v=hs[0]['valor_centavos']/100
        ref=(p['criado'] or p['atualizado']).date().isoformat()
        est=comissoes.estimar(regras,dados,ref,int(Decimal(str(v))*100) if v is not None else None)
        if dentro(pago_ref):
            portal_valor += v or 0;portal_pagos += 1;portal_sem_valor += v is None
            comissao['pagos_equipe'] += est['valor'] or 0
            portal_sem_comissao += est['valor'] is None
        if any(dentro(t) for t in (p['criado'],pago_ref,p['atualizado'])):
            ct=chats.get(p['chat'],{}).get('contato',{})
            equipe_portal.append({'id':p['id'],'nome':ct.get('name') or p.get('nome_importado',''),
                'criado_em':p['criado'].isoformat() if p['criado'] else None,
                'pago_em':pago_ref.isoformat() if pago_ref else None,'atualizado_em':p['atualizado'].isoformat(),
                'status':p['status'],'status_nome':STATUS.get(p['status'],p['status'] or 'Não informado'),
                'valor':v,'prazo':dados.get('number_of_payments'),'comissao':est,'origem':'portal'})
    equipe_portal.sort(key=lambda p:p['atualizado_em'],reverse=True)
    comissao['pagos_portal_sem_regra']=portal_sem_comissao
    lista = []
    for key, p in propostas.items():
        chat = chats.get(p['chat'], {'contato': {}, 'origem': 'nao_identificada', 'envio': None, 'campanha': ''})
        v = valor(p['dados'].get('liquid_value'))
        ref = (p['criado'] or p['atualizado']).date().isoformat()
        est = comissoes.estimar(regras,p['dados'],ref,int(Decimal(str(v))*100) if v is not None else None)
        pago_ref = p['pago']; pago_valor = v
        hs = [h for h in historicos if h['account_id']==key[0] and h['codigo'] in (p['id'],str(p['dados'].get('proposal_number') or ''))]
        if hs:
            pago_ref = momento(hs[0]['dia']+'T12:00:00-03:00');pago_valor=hs[0]['valor_centavos']/100
        if dentro(p['criado']) and pago_ref is None and p['status'] not in ('CANCELED', PAGO):
            comissao['prevista_ia'] += est['valor'] or 0
            comissao['propostas_ia_sem_regra'] += est['valor'] is None
        if dentro(pago_ref):
            est_pago = comissoes.estimar(regras,p['dados'],ref,int(Decimal(str(pago_valor))*100) if pago_valor is not None else None)
            comissao['pagos_ia'] += est_pago['valor'] or 0
            comissao['pagos_ia_sem_regra'] += est_pago['valor'] is None
        targets = grupo(chat)
        for m in targets:
            if dentro(p['criado']):
                m['propostas'].add(key); m['valor_propostas'] += v or 0
                m['propostas_sem_valor'] += v is None
            if dentro(p['pago']):
                m['pagos'].add(key); m['valor_pago'] += v or 0
                m['pagos_sem_valor'] += v is None
        if chat['envio'] and dentro(chat['envio']['t']):
            for m in [coorte, janela(chat['envio'])]:
                m['propostas'].add(key); m['valor_propostas'] += v or 0
                m['propostas_sem_valor'] += v is None
                if p['pago']:
                    m['pagos'].add(key); m['valor_pago'] += v or 0; m['pagos_sem_valor'] += v is None
        if not any(dentro(t) for t in [p['criado'], p['pago'], p['atualizado']]):
            continue
        ct = chat['contato']; dados = p['dados']
        item = {'id': p['id'], 'nome': ct.get('name') or p.get('nome_importado', ''), 'telefone': ct.get('phone', ''),
                'importada': bool(p.get('importada')), 'criacao_referencia': bool(p.get('criacao_referencia')),
                'criado_em': p['criado'].isoformat() if p['criado'] else None,
                'atualizado_em': p['atualizado'].isoformat(),
                'pago_em': p['pago'].isoformat() if p['pago'] else None,
                'status': p['status'], 'status_nome': STATUS.get(p['status'], p['status'] or 'Não informado'),
                'valor': v, 'parcela': valor(dados.get('installment_value')), 'prazo': dados.get('number_of_payments'),
                'banco': dados.get('bank', ''), 'produto': dados.get('product', ''),
                'link': dados.get('formalization_link', ''), 'origem': chat['origem'],
                'numero': chat['envio']['nome'] if chat['envio'] else '', 'campanha': chat['campanha']}
        item['comissao'] = est
        if origem and item['origem'] != origem: continue
        if status and item['status'] != status: continue
        if busca and busca.casefold() not in ' '.join(str(item[k] or '') for k in ['nome', 'telefone', 'id']).casefold(): continue
        lista.append(item)

    def fechar(m):
        return {k: len(v) if isinstance(v, set) else round(v, 2) if isinstance(v, float) else v for k, v in m.items()}

    lista.sort(key=lambda p: p['atualizado_em'], reverse=True)
    total = len(lista); pagina = min(pagina, max(1, (total+49)//50))
    ajuste_mensal = sum(m['valor_centavos'] for m in ajustes_mensais if m['incluido'])
    manual_valor = (sum(m['valor_centavos'] for m in manuais) + sum(e['valor_centavos'] for e in equipe) + ajuste_mensal) / 100
    manual_valor += portal_valor
    ia = conciliar_historico(propostas, historicos, inicio, fim, atividade)
    pagamentos = {'ia': ia,
                  'manual': {'valor': round(manual_valor,2), 'portal_valor':round(portal_valor,2),'portal_quantidade':portal_pagos,'sem_valor':portal_sem_valor},
                  'total': {'valor': round(ia['valor']+manual_valor, 2),
                            'sem_valor': ia['sem_valor']+portal_sem_valor}}
    return {'atualizado_em': agora.isoformat(), 'primeiro_evento': primeiro_evento.isoformat() if primeiro_evento else None,
            'comissoes': {**comissao, 'total_pagos':round(comissao['pagos_ia']+comissao['pagos_equipe'],2)}, 'equipe':equipe, 'equipe_portal':equipe_portal, 'custo_meta':custo,
            'pagamentos': pagamentos, 'manuais': manuais, 'ajustes_mensais': ajustes_mensais,
            'atividade': fechar(atividade), 'coorte': fechar(coorte),
            'wabas': ranking_wabas.calcular(numeros.values(),remetentes,nomes_wabas),
            'origens': {k: fechar(v) for k, v in grupos.items()},
            'numeros': [fechar(v) for v in numeros.values()],
            'janelas': [fechar(v) for k, v in sorted(janelas.items())],
            'propostas': lista[(pagina-1)*50:pagina*50], 'total_propostas': total, 'pagina': pagina}
