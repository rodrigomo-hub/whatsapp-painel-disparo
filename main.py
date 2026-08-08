import re
import os
import asyncio
import sqlite3
import requests
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import secrets
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
# Permite apontar pra um volume persistente fora do diretório do código
# (produção, Docker) — sem isso, o banco fica dentro de /app e é perdido
# a cada rebuild da imagem. Em dev local, sem essa env var, continua
# funcionando igual sempre (arquivo ao lado do main.py).
DB_FILE = Path(os.environ.get("DB_PATH", str(BASE_DIR / "painel.db")))
ENV_FILE = BASE_DIR / ".env"


def load_env(path):
    env = {}
    if path.exists():
        for linha in path.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            env[k.strip()] = v.strip()
    return env


env = load_env(ENV_FILE)
TZ = ZoneInfo("America/Sao_Paulo")
INTERVALO_CHECAGEM_QUALIDADE = 900  # 15 minutos

# Em produção (Docker), a credencial vem de variável de ambiente real
# (docker service --env-file), não do arquivo .env local — os.environ
# tem prioridade, o .env continua funcionando igual em dev local.
ADMIN_USER = os.environ.get("ADMIN_USER", env.get("ADMIN_USER", "admin"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", env.get("ADMIN_PASSWORD", ""))
# Token que a Meta manda de volta na verificacao do webhook (GET /webhook).
# Precisa ser o mesmo valor cadastrado no App da Meta (Configuracao > Webhooks).
WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", env.get("WEBHOOK_VERIFY_TOKEN", ""))
# Chave fixa pra chamadas automatizadas (n8n, outros sistemas) que nao tem
# como manter cookie de sessao. So vale para as rotas em ROTAS_API_KEY -
# nao da acesso a cadastro/exclusao de numeros nem a mais nada do painel.
API_KEY = os.environ.get("API_KEY", env.get("API_KEY", ""))
ROTAS_API_KEY = {"/disparar", "/api/disparo-lote"}
sessoes_ativas = set()

app = FastAPI()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # /webhook precisa ficar livre: quem chama e a Meta, nao um usuario logado
        # no painel, entao nao existe cookie de sessao nessas chamadas.
        caminhos_livres = ["/login", "/static", "/webhook"]
        if any(request.url.path.startswith(c) for c in caminhos_livres):
            return await call_next(request)

        # Chamadas automatizadas (n8n etc): aceita X-API-Key valida nas
        # rotas de disparo, sem precisar de login/cookie.
        if request.url.path in ROTAS_API_KEY and API_KEY:
            chave_recebida = request.headers.get("X-API-Key", "")
            if secrets.compare_digest(chave_recebida, API_KEY):
                return await call_next(request)

        token = request.cookies.get("session_token")
        if token not in sessoes_ativas:
            if request.url.path.startswith("/api"):
                return JSONResponse({"erro": "Nao autenticado"}, status_code=401)
            return RedirectResponse("/login")

        return await call_next(request)


app.add_middleware(AuthMiddleware)


def get_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS numeros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            phone_number_id TEXT NOT NULL,
            access_token TEXT NOT NULL,
            template_nome TEXT NOT NULL DEFAULT 'oferta_credito',
            limite_diario INTEGER NOT NULL DEFAULT 250,
            limite_hora INTEGER NOT NULL DEFAULT 20,
            ativo INTEGER NOT NULL DEFAULT 1,
            qualidade TEXT NOT NULL DEFAULT 'UNKNOWN',
            template_categoria TEXT NOT NULL DEFAULT 'UNKNOWN',
            template_status TEXT NOT NULL DEFAULT 'UNKNOWN',
            waba_id TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS disparos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_id INTEGER NOT NULL,
            cpf TEXT,
            nome_lead TEXT,
            telefone_destino TEXT,
            status TEXT NOT NULL,
            erro TEXT,
            criado_em TEXT NOT NULL,
            FOREIGN KEY (numero_id) REFERENCES numeros(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_id INTEGER,
            tipo TEXT NOT NULL,
            detalhe TEXT,
            criado_em TEXT NOT NULL
        )
    """)
    # migra bancos antigos que nao tinham essas colunas
    colunas_existentes = [r["name"] for r in conn.execute("PRAGMA table_info(numeros)").fetchall()]
    if "limite_hora" not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN limite_hora INTEGER NOT NULL DEFAULT 20")
    if "qualidade" not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN qualidade TEXT NOT NULL DEFAULT 'UNKNOWN'")
    if "template_categoria" not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN template_categoria TEXT NOT NULL DEFAULT 'UNKNOWN'")
    if "template_status" not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN template_status TEXT NOT NULL DEFAULT 'UNKNOWN'")
    if "waba_id" not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN waba_id TEXT NOT NULL DEFAULT ''")

    colunas_disparos = [r["name"] for r in conn.execute("PRAGMA table_info(disparos)").fetchall()]
    if "wamid" not in colunas_disparos:
        # id da mensagem devolvido pela Meta no envio - usado pra casar
        # com as atualizacoes de status que chegam depois via webhook.
        conn.execute("ALTER TABLE disparos ADD COLUMN wamid TEXT")
    if "status_entrega" not in colunas_disparos:
        # enviado / entregue / lido / falhou - preenchido pelo webhook.
        # Diferente de "status", que so indica se a chamada da API deu certo.
        conn.execute("ALTER TABLE disparos ADD COLUMN status_entrega TEXT")

    if "template_idioma" not in colunas_existentes:
        # Codigo do idioma do template aprovado na Meta (ex: pt_BR, pt_PT, en_US).
        # Precisa bater EXATAMENTE com o idioma que o template foi aprovado -
        # a Meta rejeita o envio (132001) se mandar pt_BR pra um template
        # aprovado como pt_PT, por exemplo.
        conn.execute("ALTER TABLE numeros ADD COLUMN template_idioma TEXT NOT NULL DEFAULT 'pt_BR'")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_disparos_wamid ON disparos(wamid)")
    conn.commit()
    conn.close()


def agora_iso() -> str:
    return datetime.now(TZ).isoformat()


def contagem_hoje(numero_id: int) -> int:
    conn = get_db()
    agora = datetime.now(TZ)
    inicio_dia = agora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE numero_id = ? AND status = 'sucesso' AND criado_em >= ?",
        (numero_id, inicio_dia),
    ).fetchone()
    conn.close()
    return row["c"]


def contagem_ultima_hora(numero_id: int) -> int:
    conn = get_db()
    uma_hora_atras = (datetime.now(TZ) - timedelta(hours=1)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE numero_id = ? AND status = 'sucesso' AND criado_em >= ?",
        (numero_id, uma_hora_atras),
    ).fetchone()
    conn.close()
    return row["c"]


def disponivel_agora(numero: sqlite3.Row) -> bool:
    if not numero["ativo"]:
        return False
    if numero["qualidade"] == "RED":
        return False
    if contagem_hoje(numero["id"]) >= numero["limite_diario"]:
        return False
    if contagem_ultima_hora(numero["id"]) >= numero["limite_hora"]:
        return False
    return True


def proximo_numero_disponivel(ultimo_usado_id, pool=None):
    if pool is None:
        conn = get_db()
        pool = conn.execute("SELECT * FROM numeros WHERE ativo = 1 ORDER BY id").fetchall()
        conn.close()

    disponiveis = [n for n in pool if disponivel_agora(n)]
    if not disponiveis:
        return None

    if ultimo_usado_id is None:
        return disponiveis[0]

    ids_disponiveis = [n["id"] for n in disponiveis]
    if ultimo_usado_id in ids_disponiveis:
        idx = ids_disponiveis.index(ultimo_usado_id)
        proximo_idx = (idx + 1) % len(ids_disponiveis)
    else:
        proximo_idx = 0

    proximo_id = ids_disponiveis[proximo_idx]
    return next(n for n in disponiveis if n["id"] == proximo_id)


_ultimo_usado = {"id": None}


def enviar_whatsapp(numero, telefone_destino, variaveis_template):
    url = f"https://graph.facebook.com/v21.0/{numero['phone_number_id']}/messages"
    headers = {
        "Authorization": f"Bearer {numero['access_token']}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": telefone_destino,
        "type": "template",
        "template": {
            "name": numero["template_nome"],
            "language": {"code": numero["template_idioma"] if numero["template_idioma"] else "pt_BR"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": v} for v in variaveis_template],
                }
            ] if variaveis_template else [],
        },
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            wamid = ""
            try:
                wamid = resp.json().get("messages", [{}])[0].get("id", "")
            except (ValueError, IndexError, KeyError):
                pass
            return True, "", wamid
        return False, resp.text[:500], ""
    except requests.RequestException as e:
        return False, str(e), ""


def verificar_qualidade(numero) -> str:
    url = f"https://graph.facebook.com/v21.0/{numero['phone_number_id']}"
    headers = {"Authorization": f"Bearer {numero['access_token']}"}
    try:
        r = requests.get(url, params={"fields": "quality_rating"}, headers=headers, timeout=15)
        data = r.json()
        return data.get("quality_rating", "UNKNOWN")
    except requests.RequestException:
        return "UNKNOWN"


def verificar_template_info(numero) -> tuple:
    """
    Busca categoria/status/idioma do template usando o waba_id cadastrado.
    Um mesmo nome de template pode ter mais de uma variante de idioma
    aprovada na Meta (pt_BR, pt_PT, en_US...) - nesse caso prioriza uma
    aprovada em pt_BR; se nao achar, pega a primeira aprovada; se nenhuma
    estiver aprovada, pega a primeira que vier mesmo assim.
    """
    headers = {"Authorization": f"Bearer {numero['access_token']}"}
    waba_id = numero["waba_id"]
    if not waba_id:
        return "UNKNOWN", "UNKNOWN", None
    try:
        r2 = requests.get(
            f"https://graph.facebook.com/v21.0/{waba_id}/message_templates",
            params={"name": numero["template_nome"]},
            headers=headers, timeout=15,
        )
        dados = r2.json().get("data", [])
        if not dados:
            return "UNKNOWN", "UNKNOWN", None

        aprovados = [d for d in dados if d.get("status") == "APPROVED"]
        candidatos = aprovados or dados
        escolhido = next((d for d in candidatos if d.get("language") == "pt_BR"), candidatos[0])

        return escolhido.get("category", "UNKNOWN"), escolhido.get("status", "UNKNOWN"), escolhido.get("language")
    except Exception:
        return "UNKNOWN", "UNKNOWN", None


def sincronizar_idioma_template(numero_id, numero_nome, idioma_atual, idioma_encontrado):
    """
    Atualiza numeros.template_idioma com o valor real vindo da Meta, se
    vier preenchido e for diferente do que esta salvo. Registra evento pra
    ficar rastreavel quando o idioma muda sozinho (ex: template reaprovado
    em outro idioma).
    """
    if not idioma_encontrado or idioma_encontrado == idioma_atual:
        return
    conn = get_db()
    conn.execute("UPDATE numeros SET template_idioma = ? WHERE id = ?", (idioma_encontrado, numero_id))
    conn.commit()
    conn.close()
    registrar_evento(
        numero_id, "idioma_template_sincronizado",
        f"Numero '{numero_nome}': idioma do template atualizado de '{idioma_atual}' para '{idioma_encontrado}' (buscado automaticamente na Meta)",
    )


def registrar_disparo(numero_id, cpf, nome_lead, telefone, status, erro="", wamid=""):
    conn = get_db()
    conn.execute(
        "INSERT INTO disparos (numero_id, cpf, nome_lead, telefone_destino, status, erro, criado_em, wamid) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (numero_id, cpf, nome_lead, telefone, status, erro, agora_iso(), wamid or None),
    )
    conn.commit()
    conn.close()


def registrar_evento(numero_id, tipo, detalhe):
    conn = get_db()
    conn.execute(
        "INSERT INTO eventos (numero_id, tipo, detalhe, criado_em) VALUES (?, ?, ?, ?)",
        (numero_id, tipo, detalhe, agora_iso()),
    )
    conn.commit()
    conn.close()


def verificar_qualidade_pos_envio(numero):
    """
    Confere a qualidade do numero logo depois de um envio, e pausa na hora
    se caiu pra RED - sem esperar o loop de fundo (que roda a cada
    INTERVALO_CHECAGEM_QUALIDADE). Isso fecha a janela de risco de
    continuar mandando mensagem com a qualidade ja degradada.
    Roda "best effort": qualquer erro aqui e so logado, nunca derruba o
    disparo que ja foi feito.
    """
    try:
        qualidade = verificar_qualidade(numero)
        conn = get_db()
        conn.execute("UPDATE numeros SET qualidade = ? WHERE id = ?", (qualidade, numero["id"]))
        conn.commit()
        conn.close()

        if qualidade == "RED":
            conn = get_db()
            conn.execute("UPDATE numeros SET ativo = 0 WHERE id = ?", (numero["id"],))
            conn.commit()
            conn.close()
            registrar_evento(
                numero["id"], "pausado_qualidade",
                f"Numero '{numero['nome']}' pausado automaticamente (checagem pos-envio) - qualidade RED",
            )
    except Exception as e:
        registrar_evento(numero["id"], "erro_checagem_pos_envio", str(e))
        return "UNKNOWN"
    return qualidade


DIAS_RETER_HISTORICO = 30
_ultima_limpeza = {"data": None}

def limpar_historico_antigo():
    hoje = date.today().isoformat()
    if _ultima_limpeza["data"] == hoje:
        return  # ja limpou hoje, nao repete
    conn = get_db()
    conn.execute(
        f"DELETE FROM disparos WHERE criado_em < datetime('now', '-{DIAS_RETER_HISTORICO} days')"
    )
    conn.commit()
    conn.close()
    _ultima_limpeza["data"] = hoje
    registrar_evento(None, "limpeza_automatica", f"Historico com mais de {DIAS_RETER_HISTORICO} dias removido")


async def loop_verificar_qualidade():
    while True:
        await asyncio.sleep(INTERVALO_CHECAGEM_QUALIDADE)
        try:
            limpar_historico_antigo()
            conn = get_db()
            numeros = conn.execute("SELECT * FROM numeros WHERE ativo = 1").fetchall()
            conn.close()

            for n in numeros:
                qualidade = verificar_qualidade(n)
                categoria, status_template, idioma = verificar_template_info(n)
                conn = get_db()
                conn.execute(
                    "UPDATE numeros SET qualidade = ?, template_categoria = ?, template_status = ? WHERE id = ?",
                    (qualidade, categoria, status_template, n["id"]),
                )
                conn.commit()
                conn.close()
                sincronizar_idioma_template(n["id"], n["nome"], n["template_idioma"], idioma)

                if qualidade == "RED":
                    conn = get_db()
                    conn.execute("UPDATE numeros SET ativo = 0 WHERE id = ?", (n["id"],))
                    conn.commit()
                    conn.close()
                    registrar_evento(n["id"], "pausado_qualidade", f"Numero '{n['nome']}' pausado automaticamente - qualidade RED")
        except Exception as e:
            registrar_evento(None, "erro_checagem_qualidade", str(e))


@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(loop_verificar_qualidade())


@app.get("/login", response_class=HTMLResponse)
def tela_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {"erro": None})


@app.post("/login")
async def fazer_login(request: Request):
    form = await request.form()
    usuario = form.get("usuario", "")
    senha = form.get("senha", "")

    if usuario == ADMIN_USER and senha == ADMIN_PASSWORD and ADMIN_PASSWORD:
        token = secrets.token_hex(32)
        sessoes_ativas.add(token)
        resposta = RedirectResponse("/", status_code=303)
        resposta.set_cookie("session_token", token, httponly=True, max_age=86400 * 7)
        return resposta

    return templates.TemplateResponse(request, "login.html", {"erro": "Usuario ou senha incorretos"})


@app.get("/logout")
def fazer_logout(request: Request):
    token = request.cookies.get("session_token")
    sessoes_ativas.discard(token)
    resposta = RedirectResponse("/login")
    resposta.delete_cookie("session_token")
    return resposta


@app.post("/disparar")
async def disparar(request: Request):
    body = await request.json()
    cpf = body.get("cpf", "")
    nome_lead = body.get("nome", "")
    telefone = body.get("whatsapp", "") or body.get("telefone", "")
    variaveis = body.get("variaveis_template", [])

    if not telefone:
        raise HTTPException(400, "Campo 'whatsapp' ou 'telefone' obrigatorio")

    numero = proximo_numero_disponivel(_ultimo_usado["id"])
    if numero is None:
        return {"status": "erro", "motivo": "Nenhum numero disponivel (todos pausados ou limite atingido)"}

    sucesso, erro, wamid = enviar_whatsapp(numero, telefone, variaveis)
    _ultimo_usado["id"] = numero["id"]

    registrar_disparo(numero["id"], cpf, nome_lead, telefone, "sucesso" if sucesso else "erro", erro, wamid)
    verificar_qualidade_pos_envio(numero)

    return {
        "status": "sucesso" if sucesso else "erro",
        "numero_usado": numero["nome"],
        "erro": erro if not sucesso else None,
    }


@app.post("/api/disparo-lote")
async def disparo_lote(request: Request):
    body = await request.json()
    numeros_texto = body.get("numeros_texto", "")
    numeros_selecionados = body.get("numeros_selecionados", [])

    brutos = re.split(r"[,\n\r; ]+", numeros_texto)
    formatados = []
    vistos = set()
    for b in brutos:
        limpo = re.sub(r"\D", "", b)
        if not limpo:
            continue
        if not limpo.startswith("55"):
            limpo = "55" + limpo
        if limpo not in vistos:
            vistos.add(limpo)
            formatados.append(limpo)

    conn = get_db()
    if numeros_selecionados:
        placeholders = ",".join("?" * len(numeros_selecionados))
        pool = [dict(r) for r in conn.execute(
            f"SELECT * FROM numeros WHERE id IN ({placeholders}) AND ativo = 1", numeros_selecionados
        ).fetchall()]
    else:
        pool = [dict(r) for r in conn.execute("SELECT * FROM numeros WHERE ativo = 1").fetchall()]
    conn.close()

    if not pool:
        return {"total": len(formatados), "resultados": [], "erro_geral": "Nenhum numero disponivel/selecionado"}

    resultados = []
    idx_rr = 0
    for telefone in formatados:
        # valida tamanho antes de tentar enviar (numero BR: 55 + DDD(2) + numero(8 ou 9) = 12 ou 13 digitos)
        if len(telefone) < 12 or len(telefone) > 13:
            resultados.append({"telefone": telefone, "status": "invalido", "motivo": f"Numero com {len(telefone)} digitos - formato invalido, nao foi enviado"})
            continue

        enviado = False
        for _ in range(len(pool)):
            candidato = pool[idx_rr % len(pool)]
            idx_rr += 1
            if disponivel_agora(candidato):
                sucesso, erro, wamid = enviar_whatsapp(candidato, telefone, [])
                registrar_disparo(candidato["id"], "", "Disparo em lote", telefone, "sucesso" if sucesso else "erro", erro, wamid)
                resultados.append({"telefone": telefone, "status": "sucesso" if sucesso else "erro", "numero_usado": candidato["nome"]})
                enviado = True

                # atualiza o candidato (que fica em memoria durante todo o lote) com
                # o resultado da checagem - sem isso, um numero pausado no meio do
                # lote continuaria sendo considerado disponivel pro resto do loop.
                qualidade = verificar_qualidade_pos_envio(candidato)
                candidato["qualidade"] = qualidade
                if qualidade == "RED":
                    candidato["ativo"] = 0
                break
        if not enviado:
            resultados.append({"telefone": telefone, "status": "pendente", "motivo": "Sem numero disponivel no momento"})

    return {"total": len(formatados), "resultados": resultados}


@app.post("/api/atualizar-status/{numero_id}")
def atualizar_status_numero(numero_id: int):
    conn = get_db()
    n = conn.execute("SELECT * FROM numeros WHERE id = ?", (numero_id,)).fetchone()
    conn.close()
    if n is None:
        raise HTTPException(404, "Numero nao encontrado")

    qualidade = verificar_qualidade(n)
    categoria, status_template, idioma = verificar_template_info(n)

    conn = get_db()
    conn.execute(
        "UPDATE numeros SET qualidade = ?, template_categoria = ?, template_status = ? WHERE id = ?",
        (qualidade, categoria, status_template, numero_id),
    )
    conn.commit()
    conn.close()
    sincronizar_idioma_template(numero_id, n["nome"], n["template_idioma"], idioma)

    if qualidade == "RED":
        conn = get_db()
        conn.execute("UPDATE numeros SET ativo = 0 WHERE id = ?", (numero_id,))
        conn.commit()
        conn.close()
        registrar_evento(numero_id, "pausado_qualidade", f"Numero '{n['nome']}' pausado automaticamente - qualidade RED")

    return {"qualidade": qualidade, "template_categoria": categoria, "template_status": status_template}


@app.post("/api/atualizar-todos")
def atualizar_todos_status():
    conn = get_db()
    numeros = conn.execute("SELECT * FROM numeros WHERE ativo = 1").fetchall()
    conn.close()

    resultados = []
    for n in numeros:
        qualidade = verificar_qualidade(n)
        categoria, status_template, idioma = verificar_template_info(n)

        conn = get_db()
        conn.execute(
            "UPDATE numeros SET qualidade = ?, template_categoria = ?, template_status = ? WHERE id = ?",
            (qualidade, categoria, status_template, n["id"]),
        )
        conn.commit()
        conn.close()
        sincronizar_idioma_template(n["id"], n["nome"], n["template_idioma"], idioma)

        if qualidade == "RED":
            conn = get_db()
            conn.execute("UPDATE numeros SET ativo = 0 WHERE id = ?", (n["id"],))
            conn.commit()
            conn.close()
            registrar_evento(n["id"], "pausado_qualidade", f"Numero '{n['nome']}' pausado automaticamente - qualidade RED")

        resultados.append({"nome": n["nome"], "qualidade": qualidade, "template_categoria": categoria})

    return {"total_atualizado": len(resultados), "resultados": resultados}


@app.get("/webhook")
def verificar_webhook(request: Request):
    """
    A Meta chama isso UMA vez, na hora que voce salva a config do webhook
    no App (Meta for Developers > WhatsApp > Configuration). Ela manda
    hub.mode=subscribe, hub.verify_token e hub.challenge; se o token bater
    com o nosso, devolvemos o challenge de volta (como texto puro) e ela
    considera o endpoint verificado.
    """
    modo = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if modo == "subscribe" and token == WEBHOOK_VERIFY_TOKEN and WEBHOOK_VERIFY_TOKEN:
        return PlainTextResponse(challenge)

    raise HTTPException(403, "Token de verificacao invalido")


def _processar_status(numero_id_meta_ignorado, status_obj):
    """Processa um item de value.statuses[] vindo do webhook da Meta."""
    wamid = status_obj.get("id", "")
    status = status_obj.get("status", "")  # sent | delivered | read | failed
    recipient = status_obj.get("recipient_id", "")
    erros = status_obj.get("errors", [])
    codigo_erro = erros[0].get("code") if erros else None
    mensagem_erro = erros[0].get("message", "") if erros else ""

    conn = get_db()
    disparo = conn.execute("SELECT * FROM disparos WHERE wamid = ?", (wamid,)).fetchone()
    numero_id = disparo["numero_id"] if disparo else None

    if disparo:
        conn.execute("UPDATE disparos SET status_entrega = ? WHERE wamid = ?", (status, wamid))
        conn.commit()
    conn.close()

    detalhe = f"Para {recipient}"
    if erros:
        detalhe += f": {mensagem_erro} (codigo {codigo_erro})"
    registrar_evento(numero_id, f"webhook_{status}", detalhe)

    if codigo_erro == 131042:
        registrar_evento(
            numero_id, "sem_pagamento",
            f"Detectado erro de pagamento (131042) para {recipient} - verifique qual numero e pause manualmente se necessario",
        )


@app.post("/webhook")
async def receber_webhook(request: Request):
    """
    Endpoint que recebe os eventos de fato (status de entrega, e mensagens
    recebidas do lead se ele responder). Formato oficial da Meta:
    entry[].changes[].value.{statuses[] | messages[]}.
    Sempre responde 200 rapido - se a Meta nao recebe 200 em poucos
    segundos ela reenvia o mesmo evento e tenta de novo depois.
    """
    try:
        body = await request.json()
    except ValueError:
        return {"status": "ignorado"}

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            valor = change.get("value", {})

            for status_obj in valor.get("statuses", []):
                try:
                    _processar_status(None, status_obj)
                except Exception as e:
                    registrar_evento(None, "erro_processar_webhook", str(e))

            for msg in valor.get("messages", []):
                # Lead respondeu a mensagem - so registramos o evento por enquanto,
                # o painel nao tem fluxo de conversa/resposta.
                remetente = msg.get("from", "")
                tipo_msg = msg.get("type", "")
                registrar_evento(None, "mensagem_recebida", f"De {remetente} ({tipo_msg})")

    return {"status": "recebido"}


@app.get("/api/total-hoje")
def total_hoje():
    conn = get_db()
    agora = datetime.now(TZ)
    inicio_dia = agora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE status = 'sucesso' AND criado_em >= ?",
        (inicio_dia,),
    ).fetchone()
    total_erros = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE status = 'erro' AND criado_em >= ?",
        (inicio_dia,),
    ).fetchone()
    conn.close()
    return {"total_sucesso_hoje": row["c"], "total_erro_hoje": total_erros["c"]}


@app.post("/api/buscar-templates")
async def buscar_templates(request: Request):
    body = await request.json()
    access_token = body.get("access_token", "")
    waba_id = body.get("waba_id", "")

    if not access_token or not waba_id:
        raise HTTPException(400, "access_token e waba_id sao obrigatorios")

    try:
        r = requests.get(
            f"https://graph.facebook.com/v21.0/{waba_id}/message_templates",
            params={"limit": 100},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        dados = r.json().get("data", [])
        templates = []
        for t in dados:
            corpo = ""
            for comp in t.get("components", []):
                if comp.get("type") == "BODY":
                    corpo = comp.get("text", "")
                    break
            templates.append({
                "nome": t.get("name"),
                "categoria": t.get("category"),
                "status": t.get("status"),
                "texto": corpo,
            })
        return {"templates": templates}
    except Exception as e:
        raise HTTPException(500, f"Erro ao buscar templates: {e}")


@app.get("/api/numero-token/{numero_id}")
def numero_token(numero_id: int):
    conn = get_db()
    n = conn.execute("SELECT access_token FROM numeros WHERE id = ?", (numero_id,)).fetchone()
    conn.close()
    if n is None:
        raise HTTPException(404, "Numero nao encontrado")
    return {"access_token": n["access_token"]}


@app.post("/api/buscar-numeros")
async def buscar_numeros(request: Request):
    body = await request.json()
    access_token = body.get("access_token", "")
    waba_id = body.get("waba_id", "")

    if not access_token or not waba_id:
        raise HTTPException(400, "access_token e waba_id sao obrigatorios")

    try:
        r = requests.get(
            f"https://graph.facebook.com/v21.0/{waba_id}/phone_numbers",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        dados = r.json().get("data", [])
        numeros = [
            {
                "id": n.get("id"),
                "numero_exibicao": n.get("display_phone_number"),
                "nome_verificado": n.get("verified_name"),
                "status_qualidade": n.get("quality_rating"),
            }
            for n in dados
        ]
        return {"numeros": numeros}
    except Exception as e:
        raise HTTPException(500, f"Erro ao buscar numeros: {e}")


@app.get("/api/numeros")
def listar_numeros():
    conn = get_db()
    numeros = conn.execute("SELECT * FROM numeros ORDER BY id").fetchall()
    conn.close()
    return [
        {
            **dict(n),
            "access_token": n["access_token"][:10] + "..." if n["access_token"] else "",
            "enviados_hoje": contagem_hoje(n["id"]),
            "enviados_hora": contagem_ultima_hora(n["id"]),
        }
        for n in numeros
    ]


@app.post("/api/numeros")
async def adicionar_numero(request: Request):
    body = await request.json()
    conn = get_db()
    conn.execute(
        "INSERT INTO numeros (nome, phone_number_id, access_token, template_nome, template_idioma, limite_diario, limite_hora, ativo, qualidade, waba_id) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'UNKNOWN', ?)",
        (
            body["nome"], body["phone_number_id"], body["access_token"],
            body.get("template_nome", "oferta_credito"),
            body.get("template_idioma", "pt_BR"),
            body.get("limite_diario", 250),
            body.get("limite_hora", 20),
            body.get("waba_id", ""),
        ),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.patch("/api/numeros/{numero_id}")
async def atualizar_numero(numero_id: int, request: Request):
    body = await request.json()
    conn = get_db()
    if "ativo" in body:
        conn.execute("UPDATE numeros SET ativo = ? WHERE id = ?", (int(body["ativo"]), numero_id))
    if "limite_diario" in body:
        conn.execute("UPDATE numeros SET limite_diario = ? WHERE id = ?", (body["limite_diario"], numero_id))
    if "limite_hora" in body:
        conn.execute("UPDATE numeros SET limite_hora = ? WHERE id = ?", (body["limite_hora"], numero_id))
    if "template_nome" in body:
        conn.execute("UPDATE numeros SET template_nome = ? WHERE id = ?", (body["template_nome"], numero_id))
    if "template_idioma" in body:
        conn.execute("UPDATE numeros SET template_idioma = ? WHERE id = ?", (body["template_idioma"], numero_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/api/numeros/{numero_id}")
def remover_numero(numero_id: int):
    conn = get_db()
    conn.execute("DELETE FROM numeros WHERE id = ?", (numero_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/historico")
def historico(limit: int = 100):
    conn = get_db()
    rows = conn.execute(
        """SELECT d.*, n.nome as numero_nome FROM disparos d
           JOIN numeros n ON n.id = d.numero_id
           ORDER BY d.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/eventos")
def eventos(limit: int = 50):
    conn = get_db()
    rows = conn.execute("SELECT * FROM eventos ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/", response_class=HTMLResponse)
def painel(request: Request):
    return templates.TemplateResponse(request, "index.html", {})
