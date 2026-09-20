"""Cadastro de campanha documentado pela VendeAI; sem efeitos de envio de mensagens."""
import re
import secrets
import time
from datetime import datetime

import requests
from fastapi import HTTPException


MAILING_URL = "https://ia.vendeaitecnologia.com.br"
MAILING_PATH = "/api/message-handler/mailing/leads/"
CONFIG_COLUMNS = {
    "vendeai_mailing_url": f"TEXT NOT NULL DEFAULT '{MAILING_URL}'",
    "vendeai_mailing_token": "TEXT NOT NULL DEFAULT ''",
    "vendeai_produto_padrao": "TEXT NOT NULL DEFAULT 'clt'",
    "vendeai_campanha_padrao": "TEXT NOT NULL DEFAULT ''",
    "vendeai_falha_modo": "TEXT NOT NULL DEFAULT 'bloquear'",
}


def migrar(conn):
    existentes = {r["name"] for r in conn.execute("PRAGMA table_info(configuracao)")}
    for nome, tipo in CONFIG_COLUMNS.items():
        if nome not in existentes:
            conn.execute(f"ALTER TABLE configuracao ADD COLUMN {nome} {tipo}")
    # Compartilhado por processos e mantido entre reinicios. Nunca armazena leads/token.
    conn.execute("CREATE TABLE IF NOT EXISTS vendeai_chamadas (account_id TEXT NOT NULL, instante REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vendeai_chamadas ON vendeai_chamadas(account_id, instante)")


def texto(dados, *chaves):
    for chave in chaves:
        valor = dados.get(chave)
        if valor is None or valor == "":
            continue
        if not isinstance(valor, str):
            raise HTTPException(400, f"{chave} deve ser texto")
        if valor.strip():
            return valor.strip()
    return ""


def preparar_lead(body, cfg):
    if not isinstance(body, dict):
        raise HTTPException(400, "Cada lead deve ser um objeto")
    extra = body.get("info_extra") or {}
    if not isinstance(extra, dict):
        raise HTTPException(400, "info_extra deve ser um objeto")
    telefone = re.sub(r"\D", "", texto(body, "whatsapp", "telefone", "phone"))
    # Decide pelo comprimento: DDD 55 tambem e um DDD brasileiro valido.
    if len(telefone) in (10, 11):
        telefone = "55" + telefone
    if len(telefone) == 12 and telefone.startswith("55") and telefone[4] in "6789":
        telefone = telefone[:4] + "9" + telefone[4:]
    variaveis = body.get("variaveis_template", [])
    if not isinstance(variaveis, list) or not all(isinstance(v, str) for v in variaveis):
        raise HTTPException(400, "variaveis_template deve ser uma lista de textos")
    nascimento = texto(body, "nascimento", "birth_date") or texto(extra, "nascimento", "birth_date")
    if nascimento:
        try:
            nascimento = datetime.strptime(nascimento, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            pass  # A API valida datas DD/MM/AAAA e retorna o erro por item.
    lead = {
        "phone": telefone,
        "product": texto(body, "produto", "product") or cfg["vendeai_produto_padrao"],
    }
    opcionais = {
        "cpf": texto(body, "cpf"),
        "name": texto(body, "nome", "name"),
        "birth_date": nascimento,
        "email": texto(body, "email") or texto(extra, "email") or f"lead{secrets.token_hex(12)}@gmail.com",
        "campaign": texto(body, "campanha", "campaign") or cfg["vendeai_campanha_padrao"],
    }
    lead.update({k: v for k, v in opcionais.items() if v})
    return {"mailing": lead, "variaveis_template": variaveis}


def reservar_chamada(get_db, account_id):
    agora = time.time()
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM vendeai_chamadas WHERE instante <= ?", (agora - 86400,))
        chamadas = [r[0] for r in conn.execute(
            "SELECT instante FROM vendeai_chamadas WHERE account_id = ? ORDER BY instante", (account_id,)
        )]
        recentes = [t for t in chamadas if t > agora - 60]
        espera = 0
        if len(chamadas) >= 1440:
            espera = max(espera, chamadas[-1440] + 86400 - agora)
        if len(recentes) >= 10:
            espera = max(espera, recentes[-10] + 60 - agora)
        if not espera:
            conn.execute("INSERT INTO vendeai_chamadas VALUES (?, ?)", (account_id, agora))
        conn.commit()
        return int(espera) + 1 if espera else 0
    finally:
        conn.close()


def cadastrar(leads, cfg, token_env, get_db):
    """Resultado por indice. Somente confirma itens explicitamente aceitos pela API."""
    def todos(status, motivo=None, **extras):
        return [{"status": status, **({"motivo": motivo} if motivo else {}), **extras} for _ in leads]

    if not cfg["vendeai_habilitado"]:
        return todos("desabilitado")
    if not leads:
        return []
    if len(leads) > 20000:
        return todos("erro", "LOTE_EXCEDE_20000")
    token = cfg["vendeai_mailing_token"] or token_env
    account = cfg["vendeai_account_id"]
    if not token or not account:
        return todos("erro", "CONFIGURACAO_INCOMPLETA")
    espera = reservar_chamada(get_db, account)
    if espera:
        return todos("erro", "LIMITE_LOCAL", tentar_apos_segundos=espera)
    try:
        resposta = requests.post(
            cfg["vendeai_mailing_url"].rstrip("/") + MAILING_PATH,
            json={"account_id": account, "crm_api_access_token": token, "leads": leads},
            headers={"Content-Type": "application/json"}, timeout=(5, 30),
            allow_redirects=False,
        )
        if resposta.status_code != 200:
            extras = {"http_status": resposta.status_code}
            if resposta.status_code == 429:
                retry = resposta.headers.get("Retry-After", "60")
                extras["tentar_apos_segundos"] = int(retry) if retry.isdigit() else 60
            return todos("erro", f"HTTP_{resposta.status_code}", **extras)
        payload = resposta.json()
        falhas = payload.get("failing_reasons", [])
        quantidade_falhas = payload.get("fail_count_mailings")
        quantidade_sucessos = payload.get("success_count_mailings")
        if (not isinstance(falhas, list) or type(quantidade_falhas) is not int
                or type(quantidade_sucessos) is not int
                or quantidade_falhas != len(falhas)
                or quantidade_sucessos < 0
                or quantidade_sucessos + quantidade_falhas != len(leads)):
            return todos("erro", "RESPOSTA_INVALIDA")
        erros = {}
        motivos_documentados = {"INVALID_CPF", "INVALID_PHONE", "INVALID_EMAIL", "INVALID_BIRTH_DATE", "MISSING_PHONE", "MISSING_PRODUCT"}
        for falha in falhas:
            indice = falha.get("index")
            if type(indice) is not int or not 0 <= indice < len(leads) or indice in erros:
                return todos("erro", "RESPOSTA_INVALIDA")
            motivo = falha.get("reason")
            erros[indice] = motivo if motivo in motivos_documentados else "LEAD_REJEITADO"
        return [
            {"status": "erro", "motivo": erros[i]} if i in erros else {"status": "cadastrado", "validade_dias": 7}
            for i in range(len(leads))
        ]
    except requests.RequestException:
        # Nao replica corpo, URL de excecao ou token em logs/respostas.
        return todos("erro", "FALHA_COMUNICACAO")
    except (ValueError, TypeError, AttributeError):
        return todos("erro", "RESPOSTA_INVALIDA")


def bloqueado(resultado, cfg):
    return resultado["status"] == "erro" and cfg["vendeai_falha_modo"] == "bloquear"
