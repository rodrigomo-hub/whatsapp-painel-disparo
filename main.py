import re
import os
import asyncio
import sqlite3
import unicodedata
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
ROTAS_API_KEY = {"/disparar", "/api/disparo-lote", "/api/pode-disparar"}
# Token da GTI SMS - fallback quando nao ha numero de WhatsApp disponivel
# ou o envio via Meta falha. Sem esse token configurado, o fallback fica
# desligado e o comportamento continua identico a hoje (so WhatsApp).
SMS_GTI_TOKEN = os.environ.get("SMS_GTI_TOKEN", env.get("SMS_GTI_TOKEN", ""))
sessoes_ativas = set()

app = FastAPI()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # /webhook precisa ficar livre: quem chama e a Meta, nao um usuario logado
        # no painel, entao nao existe cookie de sessao nessas chamadas.
        # /webhook: chamado pela Meta. /o/: link curto que o lead abre pelo SMS.
        # Nenhum dos dois tem cookie de sessao, entao ficam fora da autenticacao.
        caminhos_livres = ["/login", "/static", "/webhook", "/o/"]
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS configuracao (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            modo TEXT NOT NULL DEFAULT 'automatico',
            hora_inicio TEXT NOT NULL DEFAULT '08:00',
            hora_fim TEXT NOT NULL DEFAULT '20:00',
            dias_semana TEXT NOT NULL DEFAULT '1,2,3,4,5,6,7',
            sms_habilitado INTEGER NOT NULL DEFAULT 0,
            sms_modo TEXT NOT NULL DEFAULT 'fallback',
            sms_template TEXT NOT NULL DEFAULT 'Oi {1}! Voce tem uma oferta disponivel. Fale com a gente: {link}',
            whatsapp_destino TEXT NOT NULL DEFAULT '',
            whatsapp_mensagem_template TEXT NOT NULL DEFAULT 'Ola, recebi um SMS sobre uma oferta.',
            link_base_url TEXT NOT NULL DEFAULT ''
        )
    """)
    # migra bancos que ja existiam antes do fallback de SMS existir - PRECISA
    # rodar antes do INSERT OR IGNORE abaixo, senao a tabela antiga (que ja
    # existia sem essas colunas) faz o INSERT falhar com "no column named X".
    colunas_config = [r["name"] for r in conn.execute("PRAGMA table_info(configuracao)").fetchall()]
    if "sms_habilitado" not in colunas_config:
        conn.execute("ALTER TABLE configuracao ADD COLUMN sms_habilitado INTEGER NOT NULL DEFAULT 0")
    if "sms_modo" not in colunas_config:
        # 'fallback' = so envia SMS quando o WhatsApp falhar/nao tiver numero.
        # 'sempre' = envia SMS em todo disparo, mesmo quando o WhatsApp da certo (custa mais).
        conn.execute("ALTER TABLE configuracao ADD COLUMN sms_modo TEXT NOT NULL DEFAULT 'fallback'")
    if "sms_template" not in colunas_config:
        conn.execute("ALTER TABLE configuracao ADD COLUMN sms_template TEXT NOT NULL DEFAULT 'Oi {1}! Voce tem uma oferta disponivel. Fale com a gente: {link}'")
    if "whatsapp_destino" not in colunas_config:
        conn.execute("ALTER TABLE configuracao ADD COLUMN whatsapp_destino TEXT NOT NULL DEFAULT ''")
    if "whatsapp_mensagem_template" not in colunas_config:
        conn.execute("ALTER TABLE configuracao ADD COLUMN whatsapp_mensagem_template TEXT NOT NULL DEFAULT 'Ola, recebi um SMS sobre uma oferta.'")
    if "link_base_url" not in colunas_config:
        conn.execute("ALTER TABLE configuracao ADD COLUMN link_base_url TEXT NOT NULL DEFAULT ''")

    # linha unica de configuracao (id sempre 1) - cria com os defaults se ainda nao existir.
    # So roda depois das migracoes acima, garantindo que todas as colunas ja existem.
    conn.execute("""
        INSERT OR IGNORE INTO configuracao
        (id, modo, hora_inicio, hora_fim, dias_semana, sms_habilitado, sms_modo, sms_template, whatsapp_destino, whatsapp_mensagem_template, link_base_url)
        VALUES (1, 'automatico', '08:00', '20:00', '1,2,3,4,5,6,7', 0, 'fallback',
                'Oi {1}! Voce tem uma oferta disponivel. Fale com a gente: {link}',
                '', 'Ola, recebi um SMS sobre uma oferta.', '')
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS links_curtos (
            codigo TEXT PRIMARY KEY,
            disparo_id INTEGER,
            telefone TEXT NOT NULL,
            cliques INTEGER NOT NULL DEFAULT 0,
            criado_em TEXT NOT NULL,
            ultimo_clique_em TEXT
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
    if "canal" not in colunas_disparos:
        # whatsapp ou sms - qual canal foi de fato usado nesse disparo
        # (sms so aparece quando o whatsapp falhou/nao tinha numero e o
        # fallback estava habilitado).
        conn.execute("ALTER TABLE disparos ADD COLUMN canal TEXT NOT NULL DEFAULT 'whatsapp'")
    if "sms_uid" not in colunas_disparos:
        # uid retornado pela GTI SMS - usado pra consultar status (DLR) depois.
        conn.execute("ALTER TABLE disparos ADD COLUMN sms_uid TEXT")

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


def carregar_configuracao() -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM configuracao WHERE id = 1").fetchone()
    conn.close()
    cfg = dict(row)
    cfg["dias_semana"] = [int(d) for d in cfg["dias_semana"].split(",") if d]
    cfg["sms_habilitado"] = bool(cfg["sms_habilitado"])
    return cfg


def dentro_do_horario_permitido() -> tuple:
    """
    Confere se o horario global de funcionamento permite disparo agora.
    Retorna (permitido: bool, motivo: str | None).
    Modo 'automatico' sempre permite (comportamento de sempre, 24/7).
    Modo 'horario' so permite dentro da janela hora_inicio-hora_fim E no
    dia da semana configurado (1=segunda ... 7=domingo, ISO).
    Nao suporta janela que cruza a meia-noite (ex: 22:00-06:00) - pensado
    pra horario comercial dentro do mesmo dia.
    """
    cfg = carregar_configuracao()
    if cfg["modo"] != "horario":
        return True, None

    agora = datetime.now(TZ)
    dia_semana_hoje = agora.isoweekday()  # 1=segunda ... 7=domingo

    if dia_semana_hoje not in cfg["dias_semana"]:
        nomes_dias = {1: "segunda", 2: "terca", 3: "quarta", 4: "quinta", 5: "sexta", 6: "sabado", 7: "domingo"}
        return False, f"Fora do horario de funcionamento (hoje, {nomes_dias[dia_semana_hoje]}, nao esta configurado como dia ativo)"

    hora_inicio = datetime.strptime(cfg["hora_inicio"], "%H:%M").time()
    hora_fim = datetime.strptime(cfg["hora_fim"], "%H:%M").time()
    hora_agora = agora.time()

    if not (hora_inicio <= hora_agora <= hora_fim):
        return False, f"Fora do horario de funcionamento (permitido das {cfg['hora_inicio']} as {cfg['hora_fim']}, agora sao {agora.strftime('%H:%M')})"

    return True, None


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


def registrar_disparo(numero_id, cpf, nome_lead, telefone, status, erro="", wamid="", canal="whatsapp", sms_uid=""):
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO disparos (numero_id, cpf, nome_lead, telefone_destino, status, erro, criado_em, wamid, canal, sms_uid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (numero_id, cpf, nome_lead, telefone, status, erro, agora_iso(), wamid or None, canal, sms_uid or None),
    )
    conn.commit()
    disparo_id = cursor.lastrowid
    conn.close()
    return disparo_id


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


def normalizar_texto_sms(texto: str) -> str:
    """
    A GTI SMS so aceita GSM-7 puro: sem acento, sem cedilha, sem emoji.
    Remove acentuacao (NFKD decompoe 'ç' em 'c' + marca, 'ã' em 'a' + til
    etc, e o encode/decode descarta as marcas) e troca qualquer caractere
    fora do ASCII basico por espaco, pra nunca quebrar o envio.
    """
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^\x20-\x7E]", " ", sem_acento)


def gerar_codigo_curto(tamanho=6) -> str:
    alfabeto = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem 0/O/1/l/I pra evitar confusao visual
    return "".join(secrets.choice(alfabeto) for _ in range(tamanho))


def gerar_link_curto(disparo_id, telefone) -> str | None:
    """
    Cria um link curto novo (redireciona pro WhatsApp de destino
    configurado) e devolve a URL completa pronta pra colar na mensagem.
    Devolve None se link_base_url nao estiver configurada ainda -
    nesse caso, quem chamou deve mandar o SMS sem link.
    """
    cfg = carregar_configuracao()
    base = cfg["link_base_url"].rstrip("/")
    if not base:
        return None

    conn = get_db()
    for _ in range(5):  # tenta ate 5x se colidir com um codigo existente (extremamente raro)
        codigo = gerar_codigo_curto()
        existe = conn.execute("SELECT 1 FROM links_curtos WHERE codigo = ?", (codigo,)).fetchone()
        if not existe:
            break
    else:
        conn.close()
        return None

    conn.execute(
        "INSERT INTO links_curtos (codigo, disparo_id, telefone, cliques, criado_em) VALUES (?, ?, ?, 0, ?)",
        (codigo, disparo_id, telefone, agora_iso()),
    )
    conn.commit()
    conn.close()
    return f"{base}/o/{codigo}"


def enviar_sms(telefone_destino: str, mensagem: str):
    """
    Envia SMS via GTI SMS. Retorna (sucesso: bool, erro: str, uid: str).
    Normaliza a mensagem (remove acento) automaticamente antes de mandar -
    quem chama nao precisa se preocupar com isso.
    """
    if not SMS_GTI_TOKEN:
        return False, "SMS_GTI_TOKEN nao configurado", ""

    mensagem_limpa = normalizar_texto_sms(mensagem)[:160]

    try:
        resp = requests.post(
            "https://sms.gtisms.com/api/v3/sms/send",
            headers={
                "Authorization": f"Bearer {SMS_GTI_TOKEN}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"recipient": telefone_destino, "message": mensagem_limpa},
            timeout=15,
        )
        corpo = resp.json()
        if resp.status_code == 200 and corpo.get("status") == "success":
            return True, "", corpo.get("data", {}).get("uid", "")
        return False, corpo.get("message", resp.text[:300]), ""
    except requests.RequestException as e:
        return False, str(e), ""
    except ValueError:
        return False, f"Resposta invalida da GTI SMS: {resp.text[:300]}", ""


def montar_mensagem_sms(template: str, variaveis: list, link: str | None) -> str:
    """Substitui {1} {2} {3}... e {link} no template configurado."""
    msg = template
    for i, v in enumerate(variaveis, start=1):
        msg = msg.replace(f"{{{i}}}", str(v))
    msg = msg.replace("{link}", link or "")
    return msg


NUMERO_ID_SMS_FALLBACK = 0  # sentinela: disparo que nao usou nenhum numero de WhatsApp (numero_id e NOT NULL)


def tentar_fallback_sms(cpf, nome_lead, telefone, variaveis):
    """
    Tenta o fallback de SMS. So dispara de fato se sms_habilitado estiver
    ligado na configuracao E SMS_GTI_TOKEN estiver setado no ambiente.
    Gera um link curto (se link_base_url estiver configurada) apontando
    pro WhatsApp de destino, monta a mensagem a partir do template salvo,
    envia, e registra no historico com canal='sms'.
    Retorna o dict de resposta pronto pra devolver em /disparar, ou None
    se o fallback nao estiver disponivel (nesse caso quem chamou decide
    o que responder).
    """
    cfg = carregar_configuracao()
    if not cfg["sms_habilitado"] or not SMS_GTI_TOKEN:
        return None

    disparo_id = registrar_disparo(NUMERO_ID_SMS_FALLBACK, cpf, nome_lead, telefone, "pendente", canal="sms")
    link = gerar_link_curto(disparo_id, telefone)
    mensagem = montar_mensagem_sms(cfg["sms_template"], variaveis, link)

    sucesso, erro, sms_uid = enviar_sms(telefone, mensagem)

    conn = get_db()
    conn.execute(
        "UPDATE disparos SET status = ?, erro = ?, sms_uid = ? WHERE id = ?",
        ("sucesso" if sucesso else "erro", erro, sms_uid or None, disparo_id),
    )
    conn.commit()
    conn.close()

    return {
        "status": "sucesso" if sucesso else "erro",
        "numero_usado": "SMS (fallback)",
        "canal": "sms",
        "erro": erro if not sucesso else None,
    }


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


@app.get("/api/pode-disparar")
def pode_disparar():
    """
    Checagem rapida e SEM CUSTO (so consulta o banco local, nao chama a
    Meta nem nenhuma API paga) pra saber se vale a pena seguir com uma
    consulta paga antes de tentar o disparo de verdade. Pensado pra ser
    chamado pelo n8n logo antes da consulta de CPF (ou qualquer consulta
    que tenha custo), evitando gastar a consulta a toa quando o disparo
    ja sabidamente vai falhar (fora do horario ou sem numero disponivel).

    Considera o fallback de SMS: se nao ha WhatsApp disponivel mas o
    fallback esta habilitado, ainda responde pode_disparar=true (o
    disparo vai sair por SMS).
    """
    permitido, motivo_horario = dentro_do_horario_permitido()
    if not permitido:
        return {
            "pode_disparar": False,
            "motivo": motivo_horario,
            "motivo_codigo": "fora_do_horario",
            "numeros_disponiveis": 0,
            "canal_provavel": None,
        }

    conn = get_db()
    numeros = conn.execute("SELECT * FROM numeros WHERE ativo = 1").fetchall()
    conn.close()

    disponiveis = [n for n in numeros if disponivel_agora(n)]
    cfg = carregar_configuracao()
    sms_disponivel = cfg["sms_habilitado"] and bool(SMS_GTI_TOKEN)

    if not disponiveis and not sms_disponivel:
        return {
            "pode_disparar": False,
            "motivo": "Nenhum numero disponivel no momento (todos pausados, com qualidade baixa, ou no limite diario/hora) e fallback de SMS desligado",
            "motivo_codigo": "nenhum_numero_disponivel",
            "numeros_disponiveis": 0,
            "canal_provavel": None,
        }

    return {
        "pode_disparar": True,
        "motivo": None,
        "motivo_codigo": None,
        "numeros_disponiveis": len(disponiveis),
        "canal_provavel": "whatsapp" if disponiveis else "sms",
    }


@app.post("/disparar")
async def disparar(request: Request):
    body = await request.json()
    cpf = body.get("cpf", "")
    nome_lead = body.get("nome", "")
    telefone = body.get("whatsapp", "") or body.get("telefone", "")
    variaveis = body.get("variaveis_template", [])

    if not telefone:
        raise HTTPException(400, "Campo 'whatsapp' ou 'telefone' obrigatorio")

    permitido, motivo_horario = dentro_do_horario_permitido()
    if not permitido:
        return {"status": "erro", "motivo": motivo_horario, "motivo_codigo": "fora_do_horario"}

    numero = proximo_numero_disponivel(_ultimo_usado["id"])
    if numero is None:
        resultado_sms = tentar_fallback_sms(cpf, nome_lead, telefone, variaveis)
        if resultado_sms is not None:
            return resultado_sms
        return {"status": "erro", "motivo": "Nenhum numero disponivel (todos pausados ou limite atingido)"}

    sucesso, erro, wamid = enviar_whatsapp(numero, telefone, variaveis)
    _ultimo_usado["id"] = numero["id"]

    registrar_disparo(numero["id"], cpf, nome_lead, telefone, "sucesso" if sucesso else "erro", erro, wamid)
    verificar_qualidade_pos_envio(numero)

    if not sucesso:
        resultado_sms = tentar_fallback_sms(cpf, nome_lead, telefone, variaveis)
        if resultado_sms is not None:
            return resultado_sms
        return {
            "status": "erro",
            "numero_usado": numero["nome"],
            "canal": "whatsapp",
            "erro": erro,
        }

    # WhatsApp deu certo. No modo "sempre", manda SMS tambem, alem do
    # WhatsApp (custa mais - e uma escolha explicita de quem configurou).
    cfg = carregar_configuracao()
    sms_extra = None
    if cfg["sms_habilitado"] and cfg["sms_modo"] == "sempre":
        sms_extra = tentar_fallback_sms(cpf, nome_lead, telefone, variaveis)

    return {
        "status": "sucesso",
        "numero_usado": numero["nome"],
        "canal": "whatsapp",
        "erro": None,
        "sms_extra": sms_extra,
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

    permitido, motivo_horario = dentro_do_horario_permitido()
    if not permitido:
        return {
            "total": len(formatados),
            "resultados": [{"telefone": t, "status": "pendente", "motivo": motivo_horario} for t in formatados],
            "erro_geral": motivo_horario,
        }

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


@app.get("/api/configuracao")
def obter_configuracao():
    return carregar_configuracao()


@app.patch("/api/configuracao")
async def atualizar_configuracao(request: Request):
    body = await request.json()

    if "modo" in body and body["modo"] not in ("automatico", "horario"):
        raise HTTPException(400, "modo deve ser 'automatico' ou 'horario'")

    for campo in ("hora_inicio", "hora_fim"):
        if campo in body:
            try:
                datetime.strptime(body[campo], "%H:%M")
            except ValueError:
                raise HTTPException(400, f"{campo} deve estar no formato HH:MM")

    if "dias_semana" in body:
        dias = body["dias_semana"]
        if not isinstance(dias, list) or not all(isinstance(d, int) and 1 <= d <= 7 for d in dias) or not dias:
            raise HTTPException(400, "dias_semana deve ser uma lista de inteiros de 1 (segunda) a 7 (domingo), nao vazia")
        body["dias_semana"] = ",".join(str(d) for d in sorted(set(dias)))

    if "sms_habilitado" in body:
        body["sms_habilitado"] = 1 if body["sms_habilitado"] else 0

    if "sms_modo" in body and body["sms_modo"] not in ("fallback", "sempre"):
        raise HTTPException(400, "sms_modo deve ser 'fallback' ou 'sempre'")

    if "link_base_url" in body and body["link_base_url"] and not body["link_base_url"].startswith(("http://", "https://")):
        raise HTTPException(400, "link_base_url deve comecar com http:// ou https://")

    campos_validos = {
        "modo", "hora_inicio", "hora_fim", "dias_semana",
        "sms_habilitado", "sms_modo", "sms_template", "whatsapp_destino",
        "whatsapp_mensagem_template", "link_base_url",
    }
    atualizacoes = {k: v for k, v in body.items() if k in campos_validos}
    if not atualizacoes:
        raise HTTPException(400, "Nenhum campo valido informado")

    conn = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in atualizacoes)
    conn.execute(f"UPDATE configuracao SET {set_clause} WHERE id = 1", list(atualizacoes.values()))
    conn.commit()
    conn.close()

    registrar_evento(None, "configuracao_atualizada", f"Configuracao de horario atualizada: {atualizacoes}")
    return carregar_configuracao()


@app.get("/o/{codigo}")
def abrir_link_curto(codigo: str):
    """
    Redireciona pro WhatsApp de destino configurado, e conta o clique.
    Publico (sem autenticacao) - e o link que o lead recebe por SMS.
    """
    conn = get_db()
    link = conn.execute("SELECT * FROM links_curtos WHERE codigo = ?", (codigo,)).fetchone()
    if link is None:
        conn.close()
        raise HTTPException(404, "Link nao encontrado")

    conn.execute(
        "UPDATE links_curtos SET cliques = cliques + 1, ultimo_clique_em = ? WHERE codigo = ?",
        (agora_iso(), codigo),
    )
    conn.commit()
    conn.close()

    cfg = carregar_configuracao()
    destino = re.sub(r"\D", "", cfg["whatsapp_destino"])
    if not destino:
        raise HTTPException(503, "Nenhum WhatsApp de destino configurado")

    from urllib.parse import quote
    mensagem = quote(cfg["whatsapp_mensagem_template"])
    return RedirectResponse(f"https://wa.me/{destino}?text={mensagem}", status_code=302)


@app.get("/api/sms/saldo")
def saldo_sms():
    """Consulta o saldo de creditos disponivel na GTI SMS."""
    if not SMS_GTI_TOKEN:
        return {"configurado": False, "saldo": None, "expira_em": None}
    try:
        resp = requests.get(
            "https://sms.gtisms.com/api/v3/balance",
            headers={"Authorization": f"Bearer {SMS_GTI_TOKEN}", "Accept": "application/json"},
            timeout=10,
        )
        corpo = resp.json()
        dados = corpo.get("data", {})
        return {
            "configurado": True,
            "saldo": dados.get("remaining_balance"),
            "expira_em": dados.get("expired_on"),
        }
    except Exception as e:
        return {"configurado": True, "saldo": None, "expira_em": None, "erro": str(e)}


@app.get("/api/links-curtos")
def listar_links_curtos(limit: int = 50):
    """Lista os links curtos gerados, mais recentes primeiro, com contagem de cliques."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM links_curtos ORDER BY criado_em DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
        """SELECT d.*, COALESCE(n.nome, 'SMS (sem numero WhatsApp)') as numero_nome FROM disparos d
           LEFT JOIN numeros n ON n.id = d.numero_id
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
