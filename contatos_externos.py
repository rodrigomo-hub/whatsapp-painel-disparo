"""Sincroniza contatos enviados com o sistema externo de leads.

O disparo nunca espera esta chamada. Cada envio confirmado cria uma tarefa
persistida e o worker a entrega depois, com tentativas seguras.
"""
import re
import time
from contextlib import closing

import requests


URL_PADRAO = "https://app.esteiradeleads.shop/webhook/contato-externo"
MAX_TENTATIVAS = 5


def migrar(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS contatos_externos_pendentes (
        disparo_id INTEGER PRIMARY KEY,
        cpf TEXT NOT NULL,
        contato_zap TEXT NOT NULL,
        estado TEXT NOT NULL DEFAULT 'pendente',
        tentativas INTEGER NOT NULL DEFAULT 0,
        tentar_em REAL NOT NULL DEFAULT 0,
        http_status INTEGER,
        erro TEXT NOT NULL DEFAULT '',
        criado_em TEXT NOT NULL,
        enviado_em TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_contatos_externos_pendentes ON contatos_externos_pendentes(estado,tentar_em)')


def digitos(valor):
    return re.sub(r'\D', '', str(valor or ''))


def agendar(conn, disparo_id, cpf, telefone, criado_em, configuracao):
    """Registra somente dados completos e com a integração habilitada."""
    if not configuracao.get('contato_externo_habilitado'):
        return
    cpf, telefone = digitos(cpf).zfill(11), digitos(telefone)
    if len(cpf) != 11 or len(telefone) < 10:
        return
    conn.execute('''INSERT OR IGNORE INTO contatos_externos_pendentes
        (disparo_id,cpf,contato_zap,criado_em) VALUES (?,?,?,?)''',
        (disparo_id, cpf, telefone, criado_em))


def processar_um(get_db, configuracao, agora_iso):
    cfg = configuracao()
    if not cfg.get('contato_externo_habilitado') or not cfg.get('contato_externo_token'):
        return False
    with closing(get_db()) as conn:
        item = conn.execute('''SELECT * FROM contatos_externos_pendentes
            WHERE estado='pendente' AND tentar_em<=? ORDER BY disparo_id LIMIT 1''', (time.time(),)).fetchone()
        if not item:
            return False
        conn.execute("UPDATE contatos_externos_pendentes SET estado='enviando' WHERE disparo_id=? AND estado='pendente'", (item['disparo_id'],))
        if not conn.total_changes:
            conn.commit()
            return False
        conn.commit()
    status, erro = None, ''
    try:
        r = requests.post(cfg.get('contato_externo_url') or URL_PADRAO,
            headers={'Content-Type':'application/json','X-Service-Token':cfg['contato_externo_token']},
            json={'cpf':item['cpf'], 'contato_zap':item['contato_zap']}, timeout=(4,15), allow_redirects=False)
        status = r.status_code
        if not 200 <= status < 300:
            erro = 'HTTP '+str(status)+': '+(r.text or '')[:300]
    except requests.RequestException as exc:
        erro = type(exc).__name__+': '+str(exc)[:240]
    with closing(get_db()) as conn:
        if not erro:
            conn.execute("UPDATE contatos_externos_pendentes SET estado='sucesso',http_status=?,erro='',enviado_em=? WHERE disparo_id=?", (status, agora_iso(), item['disparo_id']))
        else:
            tentativas = item['tentativas'] + 1
            estado = 'erro' if tentativas >= MAX_TENTATIVAS else 'pendente'
            # 1, 5, 20 e 60 min: falhas não interferem no próximo disparo.
            espera = (60,300,1200,3600)[min(tentativas-1,3)]
            conn.execute('''UPDATE contatos_externos_pendentes
                SET estado=?,tentativas=?,tentar_em=?,http_status=?,erro=? WHERE disparo_id=?''',
                (estado,tentativas,time.time()+espera,status,erro,item['disparo_id']))
        conn.commit()
    return True
