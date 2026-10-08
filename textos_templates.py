"""Valores fixos de corpo, compartilhados por WABA, template e idioma."""
import json
from contextlib import closing
from fastapi import HTTPException


def migrar(c):
    c.execute('''CREATE TABLE IF NOT EXISTS textos_templates (
        waba_id TEXT NOT NULL, nome TEXT NOT NULL, idioma TEXT NOT NULL,
        ativo INTEGER NOT NULL DEFAULT 0, valores TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(waba_id,nome,idioma))''')


def listar(get_db,waba):
    with closing(get_db()) as c:
        return [{**dict(r),'ativo':bool(r['ativo']),'valores':json.loads(r['valores'])}
                for r in c.execute('SELECT * FROM textos_templates WHERE waba_id=?',(waba,))]


def salvar(get_db,waba,body,consultar,estrutura):
    nome,idioma,ativo=body.get('nome'),body.get('idioma'),body.get('ativo')
    if not isinstance(nome,str) or not nome or len(nome)>512 or not isinstance(idioma,str) or not idioma or len(idioma)>32 or type(ativo) is not bool:
        raise HTTPException(400,'Informe o template, idioma e a opção de textos fixos.')
    with closing(get_db()) as c:
        w=c.execute('SELECT access_token FROM wabas_salvas WHERE waba_id=?',(waba,)).fetchone()
    if not w:raise HTTPException(404,'WABA não cadastrada.')
    if not ativo:
        with closing(get_db()) as c,c:
            c.execute('UPDATE textos_templates SET ativo=0 WHERE waba_id=? AND nome=? AND idioma=?',(waba,nome,idioma))
        return {'status':'ok','ativo':False}
    valores=body.get('valores')
    if not isinstance(valores,list) or not 1<=len(valores)<=100 or any(v != {'tipo':'primeiro_nome'} and (not isinstance(v,str) or not v.strip() or len(v)>1024) for v in valores):
        raise HTTPException(400,'Preencha todas as variáveis com textos de até 1.024 caracteres.')
    resposta=consultar(waba,w['access_token'],'/message_templates','id,name,language,components,parameter_format')
    if resposta.get('erro'):raise HTTPException(502,'Não foi possível confirmar as variáveis na Meta. Tente novamente.')
    t=next((t for t in resposta.get('dados',[]) if t.get('name')==nome and t.get('language')==idioma),None)
    if not t:raise HTTPException(400,'Template não encontrado nesta WABA e idioma.')
    _,_,ids=estrutura(t)
    if not ids or len(valores)!=len(ids):raise HTTPException(400,'Preencha exatamente uma vez cada variável do corpo do template.')
    with closing(get_db()) as c,c:
        c.execute('''INSERT INTO textos_templates(waba_id,nome,idioma,ativo,valores) VALUES(?,?,?,1,?)
            ON CONFLICT(waba_id,nome,idioma) DO UPDATE SET ativo=1,valores=excluded.valores''',
            (waba,nome,idioma,json.dumps(valores,ensure_ascii=False)))
    return {'status':'ok','ativo':True}


def fixos(c,numero):
    n=dict(numero)
    if n.get('tipo_envio')=='vendeai_normal':return None
    r=c.execute('SELECT valores FROM textos_templates WHERE waba_id=? AND nome=? AND idioma=? AND ativo=1',
        (n.get('waba_id',''),n.get('template_nome',''),n.get('template_idioma') or 'pt_BR')).fetchone()
    return json.loads(r['valores']) if r else None


def resolver(get_db,numero,recebidos,nome=""):
    with closing(get_db()) as c:valores=fixos(c,numero)
    if valores is None:return recebidos
    if any(v == {'tipo':'primeiro_nome'} for v in valores):
        if not isinstance(nome,str) or not nome.strip():
            raise ValueError('NOME_AUSENTE: o template exige o primeiro nome do cliente')
        primeiro=nome.strip().split()[0].capitalize()
        return [primeiro if v == {'tipo':'primeiro_nome'} else v for v in valores]
    return valores
