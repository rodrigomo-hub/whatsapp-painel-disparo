import re
from decimal import Decimal,InvalidOperation
from fastapi import HTTPException
FONTE='https://whatsappbusiness.com/products/platform-pricing/'

def migrar(c):
 c.execute('CREATE TABLE IF NOT EXISTS custos_meta(categoria TEXT PRIMARY KEY,valor TEXT NOT NULL,moeda TEXT NOT NULL,fonte TEXT NOT NULL,verificado_em TEXT NOT NULL)')
 cols={r[1] for r in c.execute('PRAGMA table_info(disparos)')}
 if cols and 'categoria_referencia' not in cols:c.execute("ALTER TABLE disparos ADD COLUMN categoria_referencia TEXT NOT NULL DEFAULT ''")
 if cols:
  c.execute('''CREATE TRIGGER IF NOT EXISTS snapshot_categoria_disparo AFTER INSERT ON disparos BEGIN UPDATE disparos SET categoria_referencia=COALESCE((SELECT template_categoria FROM numeros WHERE id=NEW.numero_id),'') WHERE id=NEW.id; END''')

def resumo(c,inicio,fim):
 taxas={r['categoria']:dict(r) for r in c.execute('SELECT * FROM custos_meta')}
 cols={r[1] for r in c.execute('PRAGMA table_info(numeros)')}
 fallback='n.template_categoria' if 'template_categoria' in cols else "''"
 rows=c.execute("SELECT d.*,COALESCE(NULLIF(d.tipo_envio,''),n.tipo_envio,'') tipo_calculo,"+fallback+" categoria_atual FROM disparos d LEFT JOIN numeros n ON n.id=d.numero_id WHERE d.status='sucesso' AND d.canal='whatsapp' AND julianday(d.criado_em)>=julianday(?) AND julianday(d.criado_em)<julianday(?)",(inicio.isoformat(),fim.isoformat())).fetchall()
 total=Decimal(0);calculados=sem=aproximados=0;categorias={};currency=next(iter(taxas.values()))['moeda'] if taxas else 'BRL'
 for row in rows:
  r=dict(row)
  if r['tipo_calculo'] not in ('meta','gupshup','gupshup_vendeai'):continue
  cat=r.get('categoria_referencia') or r.get('categoria_atual') or ''
  if cat not in taxas or not re.fullmatch(r'55\d{10,11}',re.sub(r'\D','',r.get('telefone_destino') or '')):
   sem+=1;continue
  taxa=taxas[cat];total+=Decimal(taxa['valor']);calculados+=1;aproximados+=not bool(r.get('categoria_referencia'));categorias[cat]=categorias.get(cat,0)+1
 return {'valor':float(total) if taxas else None,'moeda':currency,'calculados':calculados,'sem_referencia':sem,'categoria_atual_usada':aproximados,'categorias':categorias,'tarifas':list(taxas.values()),'fonte':FONTE}

def salvar(c,body,agora):
 currency=body.get('moeda')
 if currency not in ('BRL','USD'):raise HTTPException(400,'Selecione BRL ou USD.')
 valores={}
 for cat in ('UTILITY','MARKETING','AUTHENTICATION'):
  try:
   s=str(body.get(cat,'')).strip().replace(',','.')
   if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,6})?',s):raise ValueError()
   d=Decimal(s)
   if not 0<=d<=100:raise ValueError()
   valores[cat]=str(d)
  except (ValueError,InvalidOperation):raise HTTPException(400,'Informe as três tarifas com até seis casas decimais.')
 with c:
  for cat,v in valores.items():c.execute('INSERT OR REPLACE INTO custos_meta VALUES(?,?,?,?,?)',(cat,v,currency,'Tarifa informada pelo usuário',agora))
 return {'ok':True}
