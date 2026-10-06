"""Estimativas de comissão e lançamentos individuais da equipe."""
import atribuicao_equipe
import json,re,uuid
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from datetime import datetime,date
from contextlib import closing
from fastapi import HTTPException
from zoneinfo import ZoneInfo
TZ=ZoneInfo('America/Sao_Paulo')
REGRAS={'69639':{24:450,36:520,48:520},'69620':{24:270,36:520,48:520},'69612':{24:500,36:500,48:500,60:550},'69604':{24:400,36:430,48:430,60:450},'69647':{24:270,36:300,48:300}}

def migrar(c):
 c.execute('''CREATE TABLE IF NOT EXISTS comissoes_regras(banco TEXT NOT NULL,produto TEXT NOT NULL,tabela TEXT NOT NULL,prazo INTEGER NOT NULL,inicio TEXT NOT NULL,percentual_bp INTEGER NOT NULL,PRIMARY KEY(banco,produto,tabela,prazo,inicio))''')
 c.execute('''CREATE TABLE IF NOT EXISTS propostas_equipe(id TEXT PRIMARY KEY,dia TEXT NOT NULL,identificacao TEXT NOT NULL,tabela TEXT NOT NULL,prazo INTEGER NOT NULL,valor_centavos INTEGER NOT NULL,percentual_bp INTEGER,comissao_centavos INTEGER,revisao INTEGER NOT NULL,excluido INTEGER NOT NULL DEFAULT 0,atualizado_em TEXT NOT NULL)''')
 cols={r[1] for r in c.execute('PRAGMA table_info(propostas_equipe)')}
 for nome,tipo in [('cpf',"TEXT NOT NULL DEFAULT ''"),('telefone',"TEXT NOT NULL DEFAULT ''"),('waba_manual',"TEXT NOT NULL DEFAULT ''"),('proposta_em',"TEXT NOT NULL DEFAULT ''"),('waba_id',"TEXT NOT NULL DEFAULT ''"),('numero_origem_id','INTEGER'),('disparo_origem_id','INTEGER'),('atribuicao_fonte',"TEXT NOT NULL DEFAULT 'nao_identificada'")]:
  if nome not in cols:c.execute(f'ALTER TABLE propostas_equipe ADD COLUMN {nome} {tipo}')
 c.execute('CREATE INDEX IF NOT EXISTS idx_equipe_dia ON propostas_equipe(dia,excluido)')
 c.execute('CREATE TABLE IF NOT EXISTS equipe_operacoes(id TEXT PRIMARY KEY,corpo TEXT NOT NULL,resposta TEXT NOT NULL)')

def carregar_regras(c):return [dict(r) for r in c.execute('SELECT * FROM comissoes_regras ORDER BY tabela,prazo,inicio')]
def semear(c):
 for tabela,prazos in REGRAS.items():
  for prazo,bp in prazos.items():c.execute("INSERT OR IGNORE INTO comissoes_regras VALUES('facta','clt',?,?,?,?)",(tabela,prazo,'2026-09-14',bp))

def moeda(v):
 try:
  s=str(v).strip().removeprefix('R$').strip()
  if ',' in s:
   if not re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+),[0-9]{1,2}',s):raise ValueError()
   s=s.replace('.','').replace(',','.')
  if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?',s):raise ValueError()
  d=Decimal(s)
  if not 0<d<=Decimal('999999999.99'):raise ValueError()
  return int(d*100)
 except (ValueError,InvalidOperation):raise HTTPException(400,'Informe um valor positivo com até duas casas decimais.')

def estimar(regras,dados,dia,centavos):
 tabela=str(dados.get('table_code') or '').strip()
 if not tabela:
  m=re.match(r'^\s*(\d+)\s*(?:-|$)',str(dados.get('table_name') or ''));tabela=m[1] if m else ''
 try:prazo=int(dados.get('number_of_payments'))
 except (ValueError,TypeError):prazo=0
 banco=str(dados.get('bank') or '').strip().lower();produto=str(dados.get('product') or '').strip().lower()
 matches=[r for r in regras if r['banco']==banco and r['produto']==produto and r['tabela']==tabela and r['prazo']==prazo and dia and r['inicio']<=dia]
 regra=max(matches,key=lambda r:r['inicio']) if matches else None
 bp=regra['percentual_bp'] if regra else None
 cents=int((Decimal(centavos)*bp/10000).quantize(Decimal('1'),rounding=ROUND_HALF_UP)) if bp is not None and centavos is not None else None
 return {'tabela':tabela,'percentual':bp/100 if bp is not None else None,'valor':cents/100 if cents is not None else None,'centavos':cents,'percentual_bp':bp}

def validar_item(item,regras):
 dia=str(item.get('dia') or '')
 try:
  d=date.fromisoformat(dia)
  if d.isoformat()!=dia or d>datetime.now(TZ).date():raise ValueError()
 except ValueError:raise HTTPException(400,'Informe uma data válida até hoje.')
 nome=str(item.get('identificacao') or '').strip()
 if not nome or len(nome)>180:raise HTTPException(400,'Informe o cliente ou identificação da proposta (até 180 caracteres).')
 if not re.fullmatch(r'[0-9]+',str(item.get('prazo',''))):raise HTTPException(400,'Informe um prazo inteiro em parcelas.')
 cents=moeda(item.get('valor'))
 est=estimar(regras,{'bank':'facta','product':'clt','table_code':item.get('tabela'),'number_of_payments':item.get('prazo')},dia,cents)
 if est['centavos'] is None:raise HTTPException(400,'Não há regra de comissão para esta tabela, prazo e data.')
 return (dia,nome,est['tabela'],int(item['prazo']),cents,est['percentual_bp'],est['centavos'])

def operar(get_db,body):
 try:op=str(uuid.UUID(str(body.get('operacao_id'))))
 except ValueError:raise HTTPException(400,'Identificador de operação inválido.')
 payload=json.dumps(body,sort_keys=True,ensure_ascii=False)
 with closing(get_db()) as c,c:
  c.execute('BEGIN IMMEDIATE')
  done=c.execute('SELECT * FROM equipe_operacoes WHERE id=?',(op,)).fetchone()
  if done:
   if done['corpo']!=payload:raise HTTPException(409,'Esta operação já foi utilizada. Reabra o formulário.')
   return json.loads(done['resposta'])
  acao=body.get('acao');now=datetime.now(TZ).isoformat();ids=[]
  if acao=='converter':
   dia=body.get('dia');old=c.execute('SELECT * FROM pagamentos_manuais WHERE dia=?',(dia,)).fetchone()
   if not old or old['revisao']!=body.get('revisao') or old['valor_centavos']<=0:raise HTTPException(409,'O total antigo mudou. Reabra o lançamento.')
   items=body.get('propostas')
   if not isinstance(items,list) or not 1<=len(items)<=200:raise HTTPException(400,'Informe entre 1 e 200 propostas.')
   if any(not isinstance(i,dict) for i in items):raise HTTPException(400,'Proposta inválida.')
   regras=carregar_regras(c);vals=[validar_item(dict(i,dia=dia),regras) for i in items]
   if sum(v[4] for v in vals)!=old['valor_centavos']:raise HTTPException(400,'A soma das propostas deve ser igual ao total antigo. Nada foi alterado.')
   for item,v in zip(items,vals):
    ident=str(uuid.uuid4());ids.append(ident)
    c.execute('INSERT INTO propostas_equipe(id,dia,identificacao,tabela,prazo,valor_centavos,percentual_bp,comissao_centavos,revisao,atualizado_em) VALUES(?,?,?,?,?,?,?,?,1,?)',(ident,*v,now))
    atribuicao_equipe.salvar(c,ident,dict(item,dia=dia))
   c.execute('UPDATE pagamentos_manuais SET valor_centavos=0,revisao=revisao+1,atualizado_em=? WHERE dia=?',(now,dia))
  elif acao in ('salvar','excluir'):
   ident=str(body.get('id') or uuid.uuid4());old=c.execute('SELECT * FROM propostas_equipe WHERE id=?',(ident,)).fetchone()
   if type(body.get('revisao')) is not int or body['revisao']!=(old['revisao'] if old else 0) or (old and old['excluido']):raise HTTPException(409,'Este lançamento mudou. Atualize a lista e tente novamente.')
   if acao=='excluir':
    if not old:raise HTTPException(404,'Lançamento não encontrado.')
    c.execute('UPDATE propostas_equipe SET excluido=1,revisao=revisao+1,atualizado_em=? WHERE id=?',(now,ident))
   else:
    v=validar_item(body,carregar_regras(c))
    c.execute('''INSERT INTO propostas_equipe(id,dia,identificacao,tabela,prazo,valor_centavos,percentual_bp,comissao_centavos,revisao,atualizado_em) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET dia=excluded.dia,identificacao=excluded.identificacao,tabela=excluded.tabela,prazo=excluded.prazo,valor_centavos=excluded.valor_centavos,percentual_bp=excluded.percentual_bp,comissao_centavos=excluded.comissao_centavos,revisao=excluded.revisao,atualizado_em=excluded.atualizado_em''',(ident,*v,body['revisao']+1,now))
    atribuicao_equipe.salvar(c,ident,body,old)
   ids=[ident]
  else:raise HTTPException(400,'Operação inválida.')
  result={'ok':True,'ids':ids}
  c.execute('INSERT INTO equipe_operacoes VALUES(?,?,?)',(op,payload,json.dumps(result)))
  return result
