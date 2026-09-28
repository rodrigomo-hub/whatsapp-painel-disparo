"""Imagens privadas persistidas no banco; IDs da Meta renovados antes do uso."""
import base64
import time
import uuid
from contextlib import closing
import requests
from fastapi import HTTPException


def migrar(c):
    cols={r['name'] for r in c.execute('PRAGMA table_info(numeros)')}
    for name in ('template_header','template_imagem'):
        if name not in cols: c.execute(f"ALTER TABLE numeros ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    c.execute('''CREATE TABLE IF NOT EXISTS imagens_template (
        id TEXT PRIMARY KEY, phone_id TEXT NOT NULL, dados BLOB NOT NULL,
        mime TEXT NOT NULL, media_id TEXT NOT NULL, enviado_em REAL NOT NULL)''')


def upload(numero, dados, mime):
    try:
        r=requests.post('https://graph.facebook.com/v21.0/'+requests.utils.quote(numero['phone_number_id'],safe='')+'/media',
            headers={'Authorization':'Bearer '+numero['access_token']},
            data={'messaging_product':'whatsapp','type':mime},
            files={'file':('imagem.png' if mime=='image/png' else 'imagem.jpg',dados,mime)},
            timeout=(5,30),allow_redirects=False)
        d=r.json()
        if r.status_code==200 and isinstance(d.get('id'),str) and d['id']:
            return d['id']
    except (requests.RequestException,ValueError,AttributeError): pass
    raise HTTPException(400,'Não foi possível enviar a imagem à Meta. Confira o arquivo, o token e as permissões; a configuração não foi salva.')


def salvar(get_db, numero, data_url):
    if not isinstance(data_url,str) or len(data_url)>7_000_000:
        raise HTTPException(400,'Envie uma imagem JPEG ou PNG de até 5 MB.')
    try:
        prefixo,conteudo=data_url.split(',',1)
        mime={'data:image/jpeg;base64':'image/jpeg','data:image/png;base64':'image/png'}[prefixo]
        dados=base64.b64decode(conteudo,validate=True)
        if not dados or len(dados)>5_000_000: raise ValueError()
        if mime=='image/png' and not dados.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError()
        if mime=='image/jpeg' and not dados.startswith(b'\xff\xd8\xff'): raise ValueError()
    except (ValueError,KeyError):
        raise HTTPException(400,'Arquivo inválido. Envie uma imagem JPEG ou PNG de até 5 MB.')
    media=upload(numero,dados,mime)
    chave=uuid.uuid4().hex
    with closing(get_db()) as c,c:
        c.execute('INSERT INTO imagens_template VALUES(?,?,?,?,?,?)',(chave,numero['phone_number_id'],dados,mime,media,time.time()))
    return chave


def media_id(get_db,numero):
    with closing(get_db()) as c:
        row=c.execute('SELECT * FROM imagens_template WHERE id=? AND phone_id=?',(numero['template_imagem'],numero['phone_number_id'])).fetchone()
    if not row: raise HTTPException(400,'Anexe a imagem obrigatória deste template antes de disparar.')
    if time.time()-row['enviado_em']<20*86400: return row['media_id']
    media=upload(numero,row['dados'],row['mime'])
    with closing(get_db()) as c,c:
        c.execute('UPDATE imagens_template SET media_id=?,enviado_em=? WHERE id=?',(media,time.time(),row['id']))
    return media
