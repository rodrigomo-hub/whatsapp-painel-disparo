import re
import os
import csv
import json
import io
import asyncio
import sqlite3
import unicodedata
import requests
import vendeai_mailing
import fila_disparos
import planilhas
import transbordo
import imagens_template
import templates_lote
import textos_templates
import link_conversa
import campanhas_sms
import lembretes_sms
import vendeai_eventos
import resultados
import comissoes
import custos_meta
import ranking_wabas
import gupshup
import qualidade_historico
import optout
import conexoes_normais
import oficiais_vendeai
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, quote
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import secrets
from contextlib import closing
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse, Response
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
ROTAS_API_KEY = {"/api/disponibilidade-consulta", "/disparar", "/api/disparo-lote", "/api/pode-disparar", "/api/resposta-lead", "/api/vendeai/atualizar-atributos-conversa", "/api/transbordo/receber", "/api/transbordo/disponibilidade"}
# Token da GTI SMS - fallback quando nao ha numero de WhatsApp disponivel
# ou o envio via Meta falha. Sem esse token configurado, o fallback fica
# desligado e o comportamento continua identico a hoje (so WhatsApp).
SMS_GTI_TOKEN = os.environ.get("SMS_GTI_TOKEN", env.get("SMS_GTI_TOKEN", ""))
# Token do CRM usado no cadastro de campanha; o painel permite um override
# protegido. Nunca devolvemos o valor nos endpoints de configuracao.
VENDEAI_API_TOKEN = os.environ.get("VENDEAI_API_TOKEN", env.get("VENDEAI_API_TOKEN", ""))
sessoes_ativas = set()

# Cache do saldo de creditos da GTI SMS - consultado no maximo a cada
# CACHE_SALDO_TTL_SEGUNDOS, pra nao bater na API deles a cada disparo
# (mais lento e arriscado se a API deles cair no meio de um lote grande).
_cache_saldo_sms = {"valor": None, "expira_em": None, "atualizado_em": None, "erro": None}
CACHE_SALDO_TTL_SEGUNDOS = 300  # 5 minutos

app = FastAPI()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Se a requisicao veio pelo dominio configurado como link_base_url
        # (o link que vai no SMS), QUALQUER caminho nesse dominio - raiz,
        # /o/algo, /qualquercoisa - redireciona direto pro WhatsApp
        # configurado. Sem consulta a banco, sem lookup de codigo: e
        # estruturalmente impossivel dar 404 aqui, porque nao ha nada
        # que possa "nao ser encontrado" - e so montar a URL e redirecionar.
        host_requisicao = request.headers.get("host", "").split(":")[0].lower()
        if host_requisicao:
            cfg = carregar_configuracao()
            link_base = cfg.get("link_base_url", "")
            if link_base:
                host_configurado = urlparse(link_base).hostname
                if host_configurado and host_requisicao == host_configurado.lower():
                    destino = re.sub(r"\D", "", cfg.get("whatsapp_destino", ""))
                    if not destino:
                        # sem destino configurado - unico caso que nao da pra redirecionar,
                        # mas isso e erro de configuracao do admin, nao do cliente clicando
                        return JSONResponse({"detail": "WhatsApp de destino nao configurado"}, status_code=503)
                    mensagem = quote(cfg.get("whatsapp_mensagem_template", ""))
                    return RedirectResponse(f"https://wa.me/{destino}?text={mensagem}", status_code=302)

        # /webhook precisa ficar livre: quem chama e a Meta, nao um usuario logado
        # no painel, entao nao existe cookie de sessao nessas chamadas.
        if request.method == 'GET' and re.fullmatch(r'/midia-vendeai/[0-9a-f]{64}',request.url.path):
            return await call_next(request)
        caminhos_livres = ["/login", "/static", "/webhook"]
        if any(request.url.path.startswith(c) for c in caminhos_livres):
            return await call_next(request)

        # Chamadas automatizadas (n8n etc): aceita X-API-Key valida nas
        # rotas de disparo, sem precisar de login/cookie.
        if (request.url.path in ROTAS_API_KEY or (request.method == "GET" and re.fullmatch(r"/api/(?:fila|transbordo|transbordo/recebidos)/[0-9a-f]{32}", request.url.path))) and API_KEY:
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
            link_base_url TEXT NOT NULL DEFAULT '',
            vendeai_habilitado INTEGER NOT NULL DEFAULT 0,
            vendeai_url TEXT NOT NULL DEFAULT '',
            vendeai_account_id TEXT NOT NULL DEFAULT '',
            vendeai_inbox_id TEXT NOT NULL DEFAULT ''
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
    if "vendeai_habilitado" not in colunas_config:
        # Sincroniza automaticamente o contato (nome/telefone/cpf) no CRM
        # VendeAi (Chatwoot) a cada /disparar, pra IA/atendente de la ja
        # ter esses dados sem precisar perguntar de novo pro lead.
        conn.execute("ALTER TABLE configuracao ADD COLUMN vendeai_habilitado INTEGER NOT NULL DEFAULT 0")
    if "vendeai_url" not in colunas_config:
        conn.execute("ALTER TABLE configuracao ADD COLUMN vendeai_url TEXT NOT NULL DEFAULT ''")
    if "vendeai_account_id" not in colunas_config:
        conn.execute("ALTER TABLE configuracao ADD COLUMN vendeai_account_id TEXT NOT NULL DEFAULT ''")
    if "vendeai_inbox_id" not in colunas_config:
        conn.execute("ALTER TABLE configuracao ADD COLUMN vendeai_inbox_id TEXT NOT NULL DEFAULT ''")

    # linha unica de configuracao (id sempre 1) - cria com os defaults se ainda nao existir.
    # So roda depois das migracoes acima, garantindo que todas as colunas ja existem.
    conn.execute("""
        INSERT OR IGNORE INTO configuracao
        (id, modo, hora_inicio, hora_fim, dias_semana, sms_habilitado, sms_modo, sms_template, whatsapp_destino, whatsapp_mensagem_template, link_base_url, vendeai_habilitado, vendeai_url, vendeai_account_id, vendeai_inbox_id)
        VALUES (1, 'automatico', '08:00', '20:00', '1,2,3,4,5,6,7', 0, 'fallback',
                'Oi {1}! Voce tem uma oferta disponivel. Fale com a gente: {link}',
                '', 'Ola, recebi um SMS sobre uma oferta.', '', 0, '', '', '')
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
    if "pausado_por_qualidade" not in colunas_existentes:
        # Marca numeros pausados AUTOMATICAMENTE por qualidade RED (distinto de pausa manual).
        # Sem essa distincao, o loop de checagem (que so olha ativo=1) nunca mais
        # re-verifica um numero pausado por qualidade, entao ele nunca volta sozinho
        # mesmo que a qualidade tenha melhorado na Meta.
        conn.execute("ALTER TABLE numeros ADD COLUMN pausado_por_qualidade INTEGER NOT NULL DEFAULT 0")

    colunas_disparos = [r["name"] for r in conn.execute("PRAGMA table_info(disparos)").fetchall()]
    if 'tipo_envio' not in colunas_disparos:
        conn.execute("ALTER TABLE disparos ADD COLUMN tipo_envio TEXT NOT NULL DEFAULT ''")
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
    if "resposta_lead" not in colunas_disparos:
        # positivo / negativo - preenchido pelo CRM via POST /api/resposta-lead
        # quando o lead responde de verdade a conversa (que acontece no CRM,
        # nao aqui). NULL = ainda sem resposta classificada.
        conn.execute("ALTER TABLE disparos ADD COLUMN resposta_lead TEXT")
    if "resposta_recebida_em" not in colunas_disparos:
        conn.execute("ALTER TABLE disparos ADD COLUMN resposta_recebida_em TEXT")
    if "link_conversa" not in colunas_disparos:
        # URL opcional pro CRM mandar junto com a resposta - link direto
        # pra conversa la no CRM, pra clicar no painel e ir direto nela.
        conn.execute("ALTER TABLE disparos ADD COLUMN link_conversa TEXT")

    if "template_idioma" not in colunas_existentes:
        # Codigo do idioma do template aprovado na Meta (ex: pt_BR, pt_PT, en_US).
        # Precisa bater EXATAMENTE com o idioma que o template foi aprovado -
        # a Meta rejeita o envio (132001) se mandar pt_BR pra um template
        # aprovado como pt_PT, por exemplo.
        conn.execute("ALTER TABLE numeros ADD COLUMN template_idioma TEXT NOT NULL DEFAULT 'pt_BR'")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_disparos_wamid ON disparos(wamid)")
    if "template_sem_variaveis" not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN template_sem_variaveis INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE TABLE IF NOT EXISTS wabas_salvas (waba_id TEXT PRIMARY KEY, access_token TEXT NOT NULL)")
    colunas_waba={r['name'] for r in conn.execute('PRAGMA table_info(wabas_salvas)')}
    if 'nome' not in colunas_waba: conn.execute("ALTER TABLE wabas_salvas ADD COLUMN nome TEXT NOT NULL DEFAULT ''")
    if 'nome_consultado_em' not in colunas_waba: conn.execute("ALTER TABLE wabas_salvas ADD COLUMN nome_consultado_em REAL NOT NULL DEFAULT 0")
    conn.execute("INSERT OR IGNORE INTO wabas_salvas(waba_id,access_token) SELECT waba_id,access_token FROM numeros WHERE waba_id!='' AND access_token!='' ORDER BY id DESC")
    if 'pausado_por_template' not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN pausado_por_template INTEGER NOT NULL DEFAULT 0")
    if 'faixas_horario' not in {r['name'] for r in conn.execute('PRAGMA table_info(configuracao)')}:
        conn.execute("ALTER TABLE configuracao ADD COLUMN faixas_horario TEXT NOT NULL DEFAULT '[]'")
    if 'rodizio_tipo' not in {r['name'] for r in conn.execute('PRAGMA table_info(configuracao)')}:
        conn.execute("ALTER TABLE configuracao ADD COLUMN rodizio_tipo TEXT NOT NULL DEFAULT 'todos'")
    if 'presenca_meta' not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN presenca_meta TEXT NOT NULL DEFAULT 'nao_verificado'")
    if 'status_envio_meta' not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN status_envio_meta TEXT NOT NULL DEFAULT 'UNKNOWN'")
    if 'banimento_meta_ativo' not in colunas_existentes:
        conn.execute('ALTER TABLE numeros ADD COLUMN banimento_meta_ativo INTEGER NOT NULL DEFAULT 0')
        conn.execute("UPDATE numeros SET banimento_meta_ativo=1 WHERE status_envio_meta='BANNED'")
    conn.execute('''CREATE TABLE IF NOT EXISTS banimentos_meta (
        phone_number_id TEXT NOT NULL, dia TEXT NOT NULL, detectado_em TEXT NOT NULL,
        PRIMARY KEY(phone_number_id,dia))''')
    if 'observacao' not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN observacao TEXT NOT NULL DEFAULT ''")
    vendeai_eventos.migrar(conn)
    resultados.migrar(conn)
    custos_meta.migrar(conn)
    templates_lote.migrar(conn)
    campanhas_sms.migrar(conn)
    lembretes_sms.migrar(conn)
    conn.execute('CREATE TABLE IF NOT EXISTS sms_credencial(id INTEGER PRIMARY KEY CHECK(id=1),token TEXT NOT NULL)')
    textos_templates.migrar(conn)
    if 'limite_meta' not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN limite_meta TEXT NOT NULL DEFAULT ''")
    if 'meta_consultado_em' not in colunas_existentes:
        conn.execute("ALTER TABLE numeros ADD COLUMN meta_consultado_em TEXT")
    conexoes_normais.migrar(conn)
    oficiais_vendeai.migrar(conn)
    imagens_template.migrar(conn)
    vendeai_mailing.migrar(conn)
    fila_disparos.migrar(conn)
    transbordo.migrar(conn)
    ranking_wabas.migrar(conn)
    gupshup.migrar(conn)
    qualidade_historico.migrar(conn)
    conn.commit()
    conn.close()


def agora_iso() -> str:
    return datetime.now(TZ).isoformat()


def contagem_hoje(numero_id: int) -> int:
    conn = get_db()
    agora = datetime.now(TZ)
    inicio_dia = agora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE numero_id = ? AND status IN ('sucesso','incerto') AND criado_em >= ?",
        (numero_id, inicio_dia),
    ).fetchone()
    conn.close()
    return row["c"]


def contagem_ultima_hora(numero_id: int) -> int:
    conn = get_db()
    uma_hora_atras = (datetime.now(TZ) - timedelta(hours=1)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE numero_id = ? AND status IN ('sucesso','incerto') AND criado_em >= ?",
        (numero_id, uma_hora_atras),
    ).fetchone()
    conn.close()
    return row["c"]


def permitido_no_rodizio(numero, cfg=None):
    modo=(cfg if cfg is not None else carregar_configuracao()).get('rodizio_tipo','todos')
    normal=conexoes_normais.normal(numero)
    return modo=='todos' or (modo=='normais' and normal) or (modo=='oficiais' and not normal)


def disponivel_agora(numero: sqlite3.Row) -> bool:
    if not permitido_no_rodizio(numero):return False
    if gupshup.via(numero):
        return gupshup.elegivel(get_db,numero) and contagem_hoje(numero["id"]) < numero["limite_diario"] and contagem_ultima_hora(numero["id"]) < numero["limite_hora"]
    if conexoes_normais.normal(numero):
        return conexoes_normais.elegivel(get_db, numero) and contagem_hoje(numero["id"]) < numero["limite_diario"] and contagem_ultima_hora(numero["id"]) < numero["limite_hora"]
    if oficiais_vendeai.via(numero) and not oficiais_vendeai.elegivel(get_db,numero):
        return False
    if dict(numero).get("status_envio_meta") != "AVAILABLE":
        return False
    if dict(numero).get("template_header")=="IMAGE" and not dict(numero).get("template_imagem"):
        return False
    if dict(numero).get("pausado_por_template"):
        return False
    if not numero["ativo"]:
        return False
    if numero["qualidade"] not in ("GREEN", "YELLOW"):
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
    cfg["vendeai_habilitado"] = bool(cfg["vendeai_habilitado"])
    cfg["fila_habilitada"] = bool(cfg["fila_habilitada"])
    cfg["transbordo_habilitado"] = bool(cfg["transbordo_habilitado"])
    cfg["faixas_horario"] = json.loads(cfg["faixas_horario"])
    return cfg


def validar_faixas(faixas):
    if not isinstance(faixas,list) or len(faixas)>20:
        raise HTTPException(400,'Informe até 20 faixas de funcionamento.')
    ocupados={d:[] for d in range(1,8)}
    def minutos(v,final=False):
        if final and v=='24:00': return 1440
        if not isinstance(v,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',v):
            raise HTTPException(400,'Horários devem estar em HH:MM; 24:00 só é permitido no fim.')
        return int(v[:2])*60+int(v[3:])
    resultado=[]
    for faixa in faixas:
        if not isinstance(faixa,dict): raise HTTPException(400,'Faixa inválida.')
        inicio=minutos(faixa.get('inicio'));fim=minutos(faixa.get('fim'),True)
        dias=faixa.get('dias')
        if not isinstance(dias,list) or not dias or not all(type(d) is int and 1<=d<=7 for d in dias):
            raise HTTPException(400,'Selecione os dias de cada faixa.')
        if inicio>=fim: raise HTTPException(400,'O fim precisa ser depois do início. Divida faixas que atravessam a meia-noite.')
        for dia in set(dias):
            if any(inicio<b and fim>a for a,b in ocupados[dia]): raise HTTPException(400,'Existem faixas sobrepostas no mesmo dia.')
            ocupados[dia].append((inicio,fim))
        resultado.append({'inicio':faixa['inicio'],'fim':faixa['fim'],'dias':sorted(set(dias))})
    return resultado


def dentro_do_horario_permitido() -> tuple:
    """
    Confere se o horario global de funcionamento permite disparo agora.
    Retorna (permitido: bool, motivo: str | None).
    Modo 'automatico' sempre permite (comportamento de sempre, 24/7).
    Modo 'horario' usa as faixas por dia, com fim exclusivo. Sem faixas,
    preserva a janela unica e os dias legados (1=segunda ... 7=domingo).
    Nao suporta janela que cruza a meia-noite (ex: 22:00-06:00) - pensado
    pra horario comercial dentro do mesmo dia.
    """
    cfg = carregar_configuracao()
    if cfg["modo"] != "horario":
        return True, None

    agora = datetime.now(TZ)
    if cfg['faixas_horario']:
        minuto=agora.hour*60+agora.minute
        for f in cfg['faixas_horario']:
            inicio=int(f['inicio'][:2])*60+int(f['inicio'][3:])
            fim=int(f['fim'][:2])*60+int(f['fim'][3:])
            if agora.isoweekday() in f['dias'] and inicio<=minuto<fim:
                return True,None
        return False,'Fora das faixas de funcionamento do painel (horário de Brasília).'
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


def pausar_template(numero):
    n=dict(numero)
    # O template pertence à WABA; idioma diferencia variantes do mesmo nome.
    with closing(get_db()) as c,c:
        rows=c.execute("SELECT id FROM numeros WHERE template_nome=? AND template_idioma=? AND (waba_id=? AND waba_id!='' OR id=?)",
            (n['template_nome'],n['template_idioma'],n.get('waba_id',''),n['id'])).fetchall()
        for r in rows:
            cur=c.execute("UPDATE numeros SET ativo=0,pausado_por_qualidade=0,pausado_por_template=1,template_status='PAUSED' WHERE id=? AND pausado_por_template=0",(r['id'],))
            if cur.rowcount:
                c.execute('INSERT INTO eventos(numero_id,tipo,detalhe,criado_em) VALUES(?,?,?,?)',
                    (r['id'],'pausado_template','Template '+n['template_nome']+' pausado pela Meta por baixa qualidade. Troque o template ou aguarde aprovação antes de reativar.',agora_iso()))


def enviar_whatsapp(numero, telefone_destino, variaveis_template):
    if optout.bloqueado(get_db,telefone_destino):
        return False,"Bloqueado pela lista negra (opt-out)",""
    with closing(get_db()) as conn:
        atual = conn.execute('SELECT qualidade,status_envio_meta,ativo FROM numeros WHERE id=?', (numero['id'],)).fetchone()
    if not atual or not atual['ativo'] or atual['status_envio_meta'] != 'AVAILABLE' or atual['qualidade'] not in ('GREEN', 'YELLOW'):
        return False, 'Envio bloqueado: numero inativo, qualidade ou status da Meta nao permitem envio.', ''
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
        },
    }
    # Templates estaticos nao recebem nem mesmo componentes vazios.
    sem_variaveis = bool(dict(numero).get("template_sem_variaveis", False))
    if not sem_variaveis and any(not str(v).strip() for v in variaveis_template):
        return False, "Variaveis do template com valores vazios", ""
    if variaveis_template and not sem_variaveis:
        payload["template"]["components"] = [{"type": "body", "parameters": [
            {"type": "text", "text": v} for v in variaveis_template
        ]}]
    if dict(numero).get('template_header')=='IMAGE':
        try:
            media=imagens_template.media_id(get_db,numero)
        except HTTPException as exc:
            return False,str(exc.detail),''
        payload['template'].setdefault('components',[]).insert(0,{'type':'header','parameters':[{'type':'image','image':{'id':media}}]})
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            wamid = ""
            try:
                wamid = resp.json().get("messages", [{}])[0].get("id", "")
            except (ValueError, IndexError, KeyError):
                pass
            return True, "", wamid
        try:
            erro=resp.json().get('error',{})
            if str(erro.get('code'))=='132015':
                pausar_template(numero)
                return False,'Template pausado pela Meta por baixa qualidade (132015). Número pausado automaticamente.',''
        except (ValueError,AttributeError,TypeError): pass
        return False, resp.text[:500], ""
    except requests.RequestException as e:
        return False, str(e), ""


def verificar_presenca_meta():
    """Compara com lista completa da WABA; erro de acesso nunca confirma ausencia."""
    conn = get_db()
    numeros = conn.execute('SELECT * FROM numeros').fetchall()
    conn.close()
    grupos = {}
    for n in numeros:
        if conexoes_normais.normal(n) or gupshup.via(n): continue
        grupos.setdefault((n['waba_id'], n['access_token']), []).append(n)
    for (waba, token), grupo in grupos.items():
        ids, cursores = set(), set()
        after = None
        confirmado = False
        try:
            if not waba or not token:
                raise ValueError('Sem credenciais')
            for _ in range(100):
                params = {'fields': 'id', 'limit': 100}
                if after:
                    params['after'] = after
                r = requests.get('https://graph.facebook.com/v21.0/' + requests.utils.quote(waba, safe='') + '/phone_numbers',
                                 params=params, headers={'Authorization': 'Bearer ' + token}, timeout=(5, 15), allow_redirects=False)
                dados = r.json()
                if r.status_code != 200 or not isinstance(dados.get('data'), list):
                    raise ValueError('Lista indisponivel')
                ids.update(str(item['id']) for item in dados['data'])
                if not dados.get('paging', {}).get('next'):
                    confirmado = True
                    break
                after = dados.get('paging', {}).get('cursors', {}).get('after')
                if not after or after in cursores:
                    raise ValueError('Paginacao incompleta')
                cursores.add(after)
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            pass
        conn = get_db()
        for n in grupo:
            estado = ('presente' if str(n['phone_number_id']) in ids else 'ausente') if confirmado else 'indisponivel'
            conn.execute('UPDATE numeros SET presenca_meta=? WHERE id=?', (estado, n['id']))
        conn.commit()
        conn.close()


def registrar_banimento_meta(conn,numero_id,estado):
    if estado=='AVAILABLE':
        conn.execute('UPDATE numeros SET banimento_meta_ativo=0 WHERE id=?',(numero_id,))
    elif estado=='BANNED':
        mudou=conn.execute("UPDATE numeros SET banimento_meta_ativo=1 WHERE id=? AND banimento_meta_ativo=0 AND tipo_envio!='vendeai_normal'",(numero_id,)).rowcount
        if mudou:
            instante=datetime.fromisoformat(agora_iso()).astimezone(TZ)
            conn.execute('''INSERT OR IGNORE INTO banimentos_meta(phone_number_id,dia,detectado_em)
                SELECT phone_number_id,?,? FROM numeros WHERE id=?''',
                (instante.date().isoformat(),instante.isoformat(),numero_id))


def bloquear_numeros_waba(waba_id, health):
    if not waba_id or not isinstance(health, dict):
        return
    entidades = health.get('entities', [])
    banida = any(e.get('error_code') == 141014 for item in entidades for e in item.get('errors', []))
    bloqueada = health.get('can_send_message') == 'BLOCKED' or any(
        item.get('entity_type') == 'WABA' and item.get('can_send_message') == 'BLOCKED' for item in entidades)
    if banida or bloqueada:
        with closing(get_db()) as conn, conn:
            if banida:
                for n in conn.execute('SELECT id FROM numeros WHERE waba_id=?',(waba_id,)).fetchall():
                    registrar_banimento_meta(conn,n['id'],'BANNED')
            conn.execute('UPDATE numeros SET ativo=0,pausado_por_qualidade=0,status_envio_meta=? WHERE waba_id=?',
                         ('BANNED' if banida else 'BLOCKED', waba_id))


def verificar_qualidade(numero) -> str:
    if gupshup.via(numero): return "UNKNOWN"
    if conexoes_normais.normal(numero):
        conexoes_normais.consultar(get_db,numero,carregar_configuracao(),VENDEAI_API_TOKEN)
        return 'UNKNOWN'
    estado, qualidade, limite = 'UNKNOWN', 'UNKNOWN', ''
    headers = {'Authorization': 'Bearer ' + numero['access_token']}
    try:
        r = requests.get('https://graph.facebook.com/v21.0/' + numero['phone_number_id'],
                         params={'fields': 'quality_rating,status,whatsapp_business_manager_messaging_limit'}, headers=headers, timeout=15)
        data = r.json()
        if r.status_code == 200:
            qualidade = data.get('quality_rating', 'UNKNOWN')
            limite = data.get('whatsapp_business_manager_messaging_limit', '') or ''
            if data.get('status') == 'BANNED':
                estado = 'BANNED'
            elif data.get('status') == 'CONNECTED' and numero['waba_id']:
                w = requests.get('https://graph.facebook.com/v21.0/' + numero['waba_id'],
                                 params={'fields': 'health_status'}, headers=headers, timeout=15)
                health = w.json().get('health_status', {}) if w.status_code == 200 else {}
                bloquear_numeros_waba(numero['waba_id'], health)
                envio = health.get('can_send_message')
                banido = any(e.get('error_code') == 141014 for entity in health.get('entities', []) for e in entity.get('errors', []))
                estado = 'BANNED' if banido else ('AVAILABLE' if envio == 'AVAILABLE' else 'BLOCKED' if envio == 'BLOCKED' else 'UNKNOWN')
            elif data.get('status'):
                estado = 'DISCONNECTED'
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        pass
    with closing(get_db()) as conn, conn:
        registrar_banimento_meta(conn,numero['id'],estado)
        conn.execute('UPDATE numeros SET status_envio_meta=?,limite_meta=?,meta_consultado_em=? WHERE id=?', (estado, limite, agora_iso(), numero['id']))
        if estado in ('BANNED', 'BLOCKED', 'DISCONNECTED'):
            conn.execute('UPDATE numeros SET ativo=0,pausado_por_qualidade=0 WHERE id=?', (numero['id'],))
    return qualidade


def verificar_template_info(numero) -> tuple:
    if gupshup.via(numero):
        with closing(get_db()) as c:
            a=c.execute('SELECT app_id FROM gup_apps WHERE numero_id=?',(numero['id'],)).fetchone()
        if not a:return 'UNKNOWN','UNKNOWN',None
        try:gupshup.atualizar(get_db,a['app_id'])
        except HTTPException:return 'UNKNOWN','UNKNOWN',None
        with closing(get_db()) as c:
            n=c.execute('SELECT * FROM numeros WHERE id=?',(numero['id'],)).fetchone()
        return n['template_categoria'],n['template_status'],n['template_idioma']
    if conexoes_normais.normal(numero): return 'TEXTO', 'LOCAL', None
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
        escolhido = next((d for d in dados if d.get('language')==numero['template_idioma']),None) or next((d for d in candidatos if d.get("language") == "pt_BR"), candidatos[0])
        if escolhido.get('status')=='PAUSED': pausar_template(numero)

        header=next((c.get('format','') for c in escolhido.get('components',[]) if c.get('type')=='HEADER'),'')
        with closing(get_db()) as c,c:
            c.execute('UPDATE numeros SET template_header=? WHERE id=? AND template_nome=? AND template_idioma=?',(header,numero['id'],numero['template_nome'],escolhido.get('language')))
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
    numero=conn.execute('SELECT tipo_envio FROM numeros WHERE id=?',(numero_id,)).fetchone()
    tipo=numero['tipo_envio'] if numero and canal=='whatsapp' else ''
    cursor = conn.execute(
        "INSERT INTO disparos (numero_id, cpf, nome_lead, telefone_destino, status, erro, criado_em, wamid, canal, sms_uid, tipo_envio) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (numero_id, cpf, nome_lead, telefone, status, erro, agora_iso(), wamid or None, canal, sms_uid or None,tipo),
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


def _aplicar_pausa_ou_reativacao_qualidade(numero_id, numero_nome, ativo_atual, pausado_por_qualidade_atual, qualidade):
    """
    Centraliza a decisao de pausar/reativar um numero com base na qualidade.

    - Se a qualidade caiu pra RED e o numero estava ativo: pausa e marca
      pausado_por_qualidade=1 (pausa automatica, distinta de pausa manual).
    - Se a qualidade voltou a GREEN/YELLOW e o numero estava pausado POR QUALIDADE
      (nao manualmente): reativa sozinho.
    - Numeros pausados manualmente (ativo=0, pausado_por_qualidade=0) nunca sao
      mexidos aqui - so o usuario reativa esses.
    """
    with closing(get_db()) as c:
        atual=c.execute('SELECT pausado_por_template,status_envio_meta FROM numeros WHERE id=?',(numero_id,)).fetchone()
    if atual and (atual['pausado_por_template'] or atual['status_envio_meta'] != 'AVAILABLE'):
        return
    if qualidade == "RED":
        if ativo_atual:
            conn = get_db()
            conn.execute("UPDATE numeros SET ativo = 0, pausado_por_qualidade = 1 WHERE id = ?", (numero_id,))
            conn.commit()
            conn.close()
            registrar_evento(numero_id, "pausado_qualidade", f"Numero '{numero_nome}' pausado automaticamente - qualidade RED")
    elif qualidade in ("GREEN", "YELLOW"):
        if not ativo_atual and pausado_por_qualidade_atual:
            conn = get_db()
            conn.execute("UPDATE numeros SET ativo = 1, pausado_por_qualidade = 0 WHERE id = ? AND pausado_por_template=0", (numero_id,))
            conn.commit()
            conn.close()
            registrar_evento(numero_id, "reativado_qualidade", f"Numero '{numero_nome}' reativado automaticamente - qualidade voltou a {qualidade}")


def verificar_qualidade_pos_envio(numero):
    if conexoes_normais.normal(numero) or gupshup.via(numero): return 'UNKNOWN'
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
            _aplicar_pausa_ou_reativacao_qualidade(numero["id"], numero["nome"], numero["ativo"], numero["pausado_por_qualidade"], qualidade)
    except Exception as e:
        registrar_evento(numero["id"], "erro_checagem_pos_envio", str(e))
        return "UNKNOWN"
    return qualidade


def normalizar_telefone_br(numero: str) -> str:
    """
    Adiciona o 9o digito quando falta em celular brasileiro antigo
    (numeros de antes de 2012, formato DDD + 8 digitos, sem o 9 que
    virou obrigatorio pra celular). So mexe em numeros de 12 digitos
    (55 + DDD + 8) cujo primeiro digito local seja 6/7/8/9 (padrao de
    celular) - fixo (comeca com 2-5) fica como esta, ja que nao recebe
    SMS/WhatsApp mesmo e nao faz sentido "consertar".
    """
    limpo = re.sub(r"\D", "", numero or "")
    if not limpo.startswith("55") or len(limpo) != 12:
        return limpo
    ddd = limpo[2:4]
    local = limpo[4:]
    if local and local[0] in "6789":
        return f"55{ddd}9{local}"
    return limpo


def cadastrar_base_vendeai(leads, cfg):
    permitidos = [lead for lead in leads if not optout.bloqueado(get_db,lead.get('phone'))]
    respostas = iter(vendeai_mailing.cadastrar(permitidos, cfg, VENDEAI_API_TOKEN) if permitidos else [])
    telefones_permitidos = {lead['phone'] for lead in permitidos}
    resultados = [next(respostas) if lead['phone'] in telefones_permitidos else {'status':'erro','motivo':'OPTOUT'} for lead in leads]
    if cfg["vendeai_habilitado"]:
        sucessos = sum(r["status"] == "cadastrado" for r in resultados)
        motivos = sorted({r["motivo"] for r in resultados if r.get("motivo")})
        registrar_evento(None, "vendeai_cadastro_base",
                         f"Aceitos: {sucessos}; falhas: {len(resultados) - sucessos}; motivos: {', '.join(motivos)}")
    return resultados


def normalizar_texto_sms(texto: str) -> str:
    """
    A GTI SMS so aceita GSM-7 puro: sem acento, sem cedilha, sem emoji.
    Remove acentuacao (NFKD decompoe 'ç' em 'c' + marca, 'ã' em 'a' + til
    etc, e o encode/decode descarta as marcas) e troca qualquer caractere
    fora do ASCII basico por espaco, pra nunca quebrar o envio.
    """
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^\x20-\x7E]", " ", sem_acento)


def gerar_link_curto(disparo_id, telefone) -> str | None:
    """
    Devolve o link fixo (link_base_url), sem gerar codigo nem consultar
    banco - o mesmo link pra todo mundo. Isso garante que o link nunca
    pode dar 404: o dominio redireciona direto pro WhatsApp configurado
    pra QUALQUER caminho (ver AuthMiddleware), entao nao ha "codigo" pra
    "nao encontrar". A troca e nao dar mais pra saber qual telefone
    especifico clicou - so que o link chegou a existir.
    Devolve None se link_base_url nao estiver configurada ainda.
    """
    cfg = carregar_configuracao()
    base = cfg["link_base_url"].rstrip("/")
    if not base:
        return None
    return base


def _parsear_valor_monetario(bruto):
    """
    A GTI SMS devolve o saldo formatado como moeda (ex: 'R$0', 'R$ 1.234,56',
    as vezes com caracteres invisiveis de direcao de texto tipo \u200e no
    meio). Extrai o numero de qualquer formato assim, sem quebrar.
    """
    if bruto is None:
        return None
    texto = "".join(c for c in str(bruto) if c.isprintable()).strip()
    limpo = re.sub(r"[^\d,.\-]", "", texto)
    if not limpo:
        return None
    if "," in limpo and "." in limpo:
        # formato BR tipo 1.234,56 - ponto e milhar, virgula e decimal
        limpo = limpo.replace(".", "").replace(",", ".")
    elif "," in limpo:
        # so virgula - assume decimal BR (ex: 0,50)
        limpo = limpo.replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return None


def token_sms():
    with closing(get_db()) as c:
        r=c.execute('SELECT token FROM sms_credencial WHERE id=1').fetchone()
    return r['token'] if r else SMS_GTI_TOKEN


def consultar_saldo_sms(forcar=False) -> dict:
    """
    Consulta o saldo de creditos da GTI SMS, com cache de alguns minutos
    (CACHE_SALDO_TTL_SEGUNDOS) pra nao bater na API deles a cada disparo.
    Sempre devolve o mesmo formato de dict, mesmo em erro - nesse caso
    'valor' fica None e 'erro' explica o motivo (usado tanto pela tela
    quanto pela checagem antes de enviar).
    """
    agora = datetime.now(TZ)
    if not forcar and _cache_saldo_sms["atualizado_em"] is not None:
        idade = (agora - _cache_saldo_sms["atualizado_em"]).total_seconds()
        if idade < CACHE_SALDO_TTL_SEGUNDOS:
            return _cache_saldo_sms

    if not token_sms():
        _cache_saldo_sms.update({"valor": None, "expira_em": None, "atualizado_em": agora, "erro": "SMS_GTI_TOKEN nao configurado"})
        return _cache_saldo_sms

    try:
        resp = requests.get(
            "https://sms.gtisms.com/api/v3/balance",
            headers={"Authorization": f"Bearer {token_sms()}", "Accept": "application/json"},
            timeout=10,
        )
        corpo = resp.json()
        dados = corpo.get("data") or {}
        valor = _parsear_valor_monetario(dados.get("remaining_balance"))

        if resp.status_code!=200 or valor is None:
            _cache_saldo_sms.update({
                "valor": None, "expira_em": None, "atualizado_em": agora,
                "erro": f"Resposta inesperada da GTI SMS (HTTP {resp.status_code}): {resp.text[:300]}",
            })
        else:
            _cache_saldo_sms.update({
                "valor": valor, "expira_em": dados.get("expired_on"),
                "atualizado_em": agora, "erro": None,
            })
    except requests.RequestException as e:
        _cache_saldo_sms.update({"valor": None, "expira_em": None, "atualizado_em": agora, "erro": str(e)})
    except ValueError:
        _cache_saldo_sms.update({"valor": None, "expira_em": None, "atualizado_em": agora, "erro": f"Resposta nao-JSON da GTI SMS: {resp.text[:300]}"})

    return _cache_saldo_sms


def enviar_sms(telefone_destino: str, mensagem: str):
    """
    Envia SMS via GTI SMS. Retorna (sucesso: bool, erro: str, uid: str).
    Normaliza a mensagem (remove acento) automaticamente antes de mandar -
    quem chama nao precisa se preocupar com isso.
    """
    if optout.bloqueado(get_db,telefone_destino):return False,'Bloqueado pela lista negra (opt-out)',''
    if not token_sms():
        return False, "SMS_GTI_TOKEN nao configurado", ""

    mensagem_limpa = normalizar_texto_sms(mensagem)[:160]

    try:
        resp = requests.post(
            "https://sms.gtisms.com/api/v3/sms/send",
            headers={
                "Authorization": f"Bearer {token_sms()}",
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
    if optout.bloqueado(get_db,telefone):
        return {"status":"bloqueado","motivo_codigo":"optout","aceito":False}
    """
    Tenta o fallback de SMS. So dispara de fato se sms_habilitado estiver
    ligado na configuracao E token_sms() estiver setado no ambiente.
    Gera um link curto (se link_base_url estiver configurada) apontando
    pro WhatsApp de destino, monta a mensagem a partir do template salvo,
    envia, e registra no historico com canal='sms'.
    Retorna o dict de resposta pronto pra devolver em /disparar, ou None
    se o fallback nao estiver disponivel (nesse caso quem chamou decide
    o que responder).
    """
    cfg = carregar_configuracao()
    if not cfg["sms_habilitado"] or not token_sms():
        return None

    saldo = consultar_saldo_sms()
    if saldo["valor"] is not None and saldo["valor"] <= 0:
        registrar_disparo(
            NUMERO_ID_SMS_FALLBACK, cpf, nome_lead, telefone, "erro",
            erro="SEM SALDO: saldo de creditos GTI SMS zerado - envio nao foi tentado",
            canal="sms",
        )
        registrar_evento(None, "sms_sem_saldo", f"Fallback de SMS pulado - saldo GTI SMS zerado ({saldo['valor']})")
        return {
            "status": "erro",
            "numero_usado": "SMS (fallback)",
            "canal": "sms",
            "erro": "Sem saldo de creditos SMS disponivel",
            "erro_codigo": "sms_sem_saldo",
        }

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


def descobrir_conexoes_seguro():
    try:
        conexoes_normais.descobrir(get_db,carregar_configuracao(),VENDEAI_API_TOKEN)
    except HTTPException:
        pass
    try:
        oficiais_vendeai.descobrir(get_db,carregar_configuracao(),VENDEAI_API_TOKEN)
    except HTTPException:
        pass


async def loop_verificar_qualidade():
    while True:
        await asyncio.sleep(INTERVALO_CHECAGEM_QUALIDADE)
        try:
            await asyncio.to_thread(descobrir_conexoes_seguro)
            await asyncio.to_thread(verificar_presenca_meta)
            limpar_historico_antigo()
            conn = get_db()
            # Inclui tambem numeros pausados automaticamente por qualidade (nao so os
            # ativos) - senao um numero que caiu pra RED nunca mais eh reconferido e
            # fica pausado pra sempre, mesmo que a qualidade tenha melhorado na Meta.
            # Numeros pausados MANUALMENTE (pausado_por_qualidade=0) ficam de fora,
            # como deveria - so o usuario reativa esses.
            # Confere TODOS os numeros, inclusive os pausados manualmente - a checagem
            # so LE a qualidade na Meta (nao manda mensagem nenhuma), entao nao ha risco;
            # o beneficio e sempre ter a qualidade em dia pra qualquer numero, mesmo os
            # que estao pausados na mao. A pausa manual continua protegida: so reativa
            # sozinho quem foi pausado automaticamente (pausado_por_qualidade=1), nunca
            # quem o usuario pausou de proposito.
            numeros = conn.execute("SELECT * FROM numeros").fetchall()
            conn.close()

            for n in numeros:
                qualidade = await asyncio.to_thread(verificar_qualidade,n)
                categoria, status_template, idioma = await asyncio.to_thread(verificar_template_info,n)
                conn = get_db()
                conn.execute(
                    "UPDATE numeros SET qualidade = ?, template_categoria = ?, template_status = ? WHERE id = ?",
                    (qualidade, categoria, status_template, n["id"]),
                )
                conn.commit()
                conn.close()
                sincronizar_idioma_template(n["id"], n["nome"], n["template_idioma"], idioma)

                _aplicar_pausa_ou_reativacao_qualidade(n["id"], n["nome"], n["ativo"], n["pausado_por_qualidade"], qualidade)
        except Exception as e:
            registrar_evento(None, "erro_checagem_qualidade", str(e))


async def loop_optout(prioritario=False):
    while True:
        try:
            fez = await asyncio.to_thread(optout.processar_um,get_db,VENDEAI_API_TOKEN,prioritario)
        except Exception:
            fez = False
        await asyncio.sleep(0 if fez else 0.1)


async def loop_campanhas_sms():
    while not app.state.fila_parar:
        try:
            await asyncio.to_thread(campanhas_sms.processar_um,get_db,token_sms(),dentro_do_horario_permitido,consultar_saldo_sms)
        except Exception:
            campanhas_sms.recuperar(get_db)
        await asyncio.sleep(1)


async def loop_lembretes_sms():
    while not app.state.fila_parar:
        try:
            await asyncio.to_thread(lembretes_sms.processar_um,get_db,carregar_configuracao()['vendeai_account_id'],token_sms(),dentro_do_horario_permitido,consultar_saldo_sms)
        except Exception as erro:
            registrar_evento(None,'lembrete_sms_erro',type(erro).__name__)
        await asyncio.sleep(5)


async def loop_link_conversa():
    while True:
        try:
            cfg=carregar_configuracao()
            fez=await asyncio.to_thread(link_conversa.processar_um,get_db,cfg['vendeai_account_id'],cfg.get('vendeai_mailing_token') or VENDEAI_API_TOKEN)
        except Exception:
            fez=False
        await asyncio.sleep(0 if fez else 0.1)


async def loop_eventos_new():
    while True:
        try:
            await asyncio.to_thread(vendeai_eventos.processar_um,get_db)
        except Exception:
            pass  # O registro persistido permite investigar sem registrar credenciais.
        await asyncio.sleep(5)


async def loop_etiquetas_disparo():
    while True:
        try:
            fez=await asyncio.to_thread(conexoes_normais.processar_etiqueta,get_db,carregar_configuracao(),VENDEAI_API_TOKEN)
        except Exception:
            fez=False
        await asyncio.sleep(0.1 if fez else 2)


@app.on_event("startup")
async def startup():
    init_db()
    link_conversa.recuperar(get_db)
    app.state.link_tasks=[asyncio.create_task(loop_link_conversa()) for _ in range(2)]
    optout.recuperar(get_db)
    app.state.optout_tasks = [asyncio.create_task(loop_optout(i==0)) for i in range(4)]
    vendeai_eventos.recuperar_interrompidos(get_db)
    app.state.eventos_task = asyncio.create_task(loop_eventos_new())
    app.state.etiquetas_task = asyncio.create_task(loop_etiquetas_disparo())
    asyncio.create_task(loop_verificar_qualidade())
    app.state.fila_parar = False
    campanhas_sms.recuperar(get_db)
    app.state.sms_task=asyncio.create_task(loop_campanhas_sms())
    lembretes_sms.recuperar(get_db)
    app.state.lembretes_sms_task=asyncio.create_task(loop_lembretes_sms())
    app.state.fila_task = asyncio.create_task(loop_fila())


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
    Checagem rapida (consulta o banco local e, se necessario, o painel
    de destino; nao chama a Meta nem nenhuma API paga) pra saber se vale a pena seguir com uma
    consulta paga antes de tentar o disparo de verdade. Pensado pra ser
    chamado pelo n8n logo antes da consulta de CPF (ou qualquer consulta
    que tenha custo), evitando gastar a consulta a toa quando o disparo
    ja sabidamente vai falhar (fora do horario ou sem numero disponivel).

    Considera o fallback de SMS: se nao ha WhatsApp disponivel mas o
    fallback esta habilitado, ainda responde pode_disparar=true (o
    disparo vai sair por SMS).
    """
    if estado_lote()["lote"]:
        return {"pode_disparar": False, "motivo_codigo": "lote_em_andamento", "motivo": "Lote exclusivo em andamento", "numeros_disponiveis": 0, "canal_provavel": None}
    permitido, motivo_horario = dentro_do_horario_permitido()
    if not permitido and carregar_configuracao()['transbordo_habilitado']:
        destino=testar_destino_transbordo(carregar_configuracao())
        disponivel=destino.get('disponivel') is True
        return {'pode_disparar':disponivel,'motivo_codigo':None if disponivel else 'transbordo_indisponivel',
                'motivo':'Origem fora do horário; disponibilidade consultada no destino.',
                'numeros_disponiveis':0,'canal_provavel':'transbordo' if disponivel else None,
                'motivo_destino':destino.get('motivo_codigo'),'destino_consultado':True}
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
    if estado_lote()['pendentes'] and capacidade_lote()['capacidade_agora'] <= 0:
        disponiveis = []
    cfg = carregar_configuracao()
    sms_disponivel = cfg["sms_habilitado"] and bool(token_sms())

    if not disponiveis and not sms_disponivel and cfg['transbordo_habilitado']:
        destino = testar_destino_transbordo(cfg)
        return {'pode_disparar': destino.get('disponivel') is True,
                'motivo_codigo': None if destino.get('disponivel') else 'transbordo_indisponivel',
                'motivo': 'Consulta ao painel de destino; capacidade sera conferida novamente na entrada',
                'numeros_disponiveis':0,'canal_provavel':'transbordo' if destino.get('disponivel') else None,
                'motivo_destino':destino.get('motivo_codigo'), 'destino_consultado':True}
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


@app.post("/api/resposta-lead")
async def resposta_lead(request: Request):
    """
    Endpoint pro CRM chamar quando um lead responde de verdade a
    conversa (a conversa em si acontece no CRM, nao aqui - esse painel
    so dispara a mensagem inicial). Casa a resposta com o disparo mais
    recente feito pra aquele telefone, e classifica como positivo/negativo.
    Aceita opcionalmente link_conversa - URL da conversa no CRM, pra
    dar pra clicar e ir direto nela pelo painel.
    Protegido por X-API-Key, igual /disparar.
    """
    body = await request.json()
    telefone = re.sub(r"\D", "", str(body.get("telefone", "")))
    resposta = body.get("resposta", "")
    link_conversa = str(body.get("link_conversa", "")).strip() or None

    if not telefone:
        raise HTTPException(400, "Campo 'telefone' obrigatorio")
    if resposta not in ("positivo", "negativo"):
        raise HTTPException(400, "Campo 'resposta' deve ser 'positivo' ou 'negativo'")
    if link_conversa and not link_conversa.startswith(("http://", "https://")):
        raise HTTPException(400, "link_conversa deve comecar com http:// ou https://")

    conn = get_db()
    disparo = conn.execute(
        "SELECT id FROM disparos WHERE telefone_destino = ? ORDER BY id DESC LIMIT 1",
        (telefone,),
    ).fetchone()

    if disparo is None:
        conn.close()
        raise HTTPException(404, f"Nenhum disparo encontrado pro telefone {telefone}")

    conn.execute(
        "UPDATE disparos SET resposta_lead = ?, resposta_recebida_em = ?, link_conversa = ? WHERE id = ?",
        (resposta, agora_iso(), link_conversa, disparo["id"]),
    )
    conn.commit()
    conn.close()

    return {"status": "ok", "disparo_id": disparo["id"], "resposta_lead": resposta, "link_conversa": link_conversa}


@app.post("/api/vendeai/atualizar-atributos-conversa")
async def vendeai_atualizar_atributos_conversa(request: Request):
    """
    Atualiza atributos customizados de uma CONVERSA especifica na VendeAi
    (Chatwoot) - diferente de atributos de contato, esses so existem
    depois que a conversa ja foi criada de verdade (lead respondeu pelo
    numero real da VendeAi). Pensado pra ser chamado pelo n8n assim que
    detectar uma conversa nova, jogando de uma vez os dados de
    identificacao que ja temos (CPF, nome, data nascimento, etc.), pra
    a IA/atendente nao precisar perguntar de novo.

    O chamador manda o dicionario de atributos EXATAMENTE com as chaves
    que a VendeAi espera (definidas nas configuracoes de custom
    attributes de conversa de la) - esse endpoint nao tenta adivinhar
    nomes de campo, so repassa.

    merge=true na chamada pro Chatwoot: so as chaves enviadas sao
    atualizadas, o resto que ja estava na conversa continua intacto.
    """
    cfg = carregar_configuracao()
    if not cfg["vendeai_habilitado"] or not VENDEAI_API_TOKEN:
        raise HTTPException(503, "Integracao com VendeAi nao esta habilitada/configurada")

    base = cfg["vendeai_url"].rstrip("/")
    account_id = cfg["vendeai_account_id"]
    if not base or not account_id:
        raise HTTPException(503, "vendeai_url ou vendeai_account_id nao configurados")

    body = await request.json()
    conversation_id = body.get("conversation_id")
    atributos = body.get("atributos")

    if not conversation_id:
        raise HTTPException(400, "Campo 'conversation_id' obrigatorio")
    if not atributos or not isinstance(atributos, dict):
        raise HTTPException(400, "Campo 'atributos' obrigatorio e precisa ser um objeto/dicionario")

    try:
        resp = requests.post(
            f"{base}/api/v1/accounts/{account_id}/conversations/{conversation_id}/custom_attributes",
            headers={"api_access_token": VENDEAI_API_TOKEN, "Content-Type": "application/json"},
            json={"custom_attributes": atributos, "merge": True},
            timeout=10,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"Erro ao falar com a VendeAi: {e}")

    if resp.status_code != 200:
        raise HTTPException(502, f"VendeAi respondeu {resp.status_code}: {resp.text[:300]}")

    return {"status": "ok", "conversation_id": conversation_id, "custom_attributes": resp.json().get("custom_attributes")}


def variaveis_vazias_impedem(variaveis, numeros=None):
    if not any(not str(v).strip() for v in variaveis):
        return False
    with closing(get_db()) as conn:
        pool = conn.execute("SELECT * FROM numeros WHERE ativo=1 AND qualidade != 'RED'").fetchall()
    if numeros:
        pool = [n for n in pool if n['id'] in numeros]
    with closing(get_db()) as conn:
        return not pool or not all(n['template_sem_variaveis'] or textos_templates.fixos(conn,n) is not None for n in pool)


def estado_lote():
    with closing(get_db()) as conn:
        return dict(conn.execute("SELECT COUNT(*) pendentes, COALESCE(SUM(CASE WHEN origem='lote' THEN 1 ELSE 0 END),0) lote FROM fila_disparos WHERE status IN ('aguardando_cadastro','cadastrando','pronto','enviando')").fetchone())


def entrada_exclusiva(func):
    @wraps(func)
    async def protegida(request: Request):
        # Serializa admissao, inclusive enquanto o modo imediato aguarda APIs externas.
        with fila_disparos.trava_worker(str(DB_FILE) + '.entrada') as obtida:
            if not obtida:
                return JSONResponse({"status": "erro", "aceito": False, "motivo_codigo": "entrada_em_andamento", "motivo": "Outra entrada esta em processamento. Nada foi enfileirado."}, status_code=409)
            estado = estado_lote()
            if estado['lote'] or (func.__name__ == 'disparo_lote' and estado['pendentes']):
                return JSONResponse({"status": "erro", "aceito": False, "motivo_codigo": "fila_ocupada", "motivo": "Ha envios em andamento. Aguarde finalizar ou cancele os pendentes antes de iniciar outro lote."}, status_code=409)
            return await func(request)
    return protegida


def capacidade_lote(selecionados=None, descontar_fila=True):
    if selecionados is None:
        selecionados = []
    if not isinstance(selecionados, list) or not all(type(n) is int for n in selecionados):
        raise HTTPException(400, 'numeros_selecionados deve ser lista de IDs inteiros')
    with closing(get_db()) as conn:
        pool = conn.execute('SELECT * FROM numeros').fetchall()
    detalhes = []
    for n in pool:
        if selecionados and n['id'] not in selecionados:
            continue
        dia = max(0, n['limite_diario'] - contagem_hoje(n['id']))
        hora = max(0, n['limite_hora'] - contagem_ultima_hora(n['id']))
        elegivel = n['status_envio_meta'] == 'AVAILABLE' and bool(n['ativo']) and not n['pausado_por_template'] and n['qualidade'] in ('GREEN', 'YELLOW') and not (n['template_header']=='IMAGE' and not n['template_imagem'])
        if gupshup.via(n): elegivel = gupshup.elegivel(get_db,n)
        if conexoes_normais.normal(n): elegivel = conexoes_normais.elegivel(get_db,n)
        elif oficiais_vendeai.via(n): elegivel = elegivel and oficiais_vendeai.elegivel(get_db,n)
        elegivel = elegivel and permitido_no_rodizio(n)
        detalhes.append({'id': n['id'], 'nome': n['nome'], 'elegivel': elegivel,
                         'restante_dia': dia if elegivel else 0, 'restante_hora': hora if elegivel else 0,
                         'disponivel_agora': min(dia, hora) if elegivel else 0})
    permitido, motivo = dentro_do_horario_permitido()
    estado = estado_lote()
    bruta = sum(n['disponivel_agora'] for n in detalhes) if permitido else 0
    reservados = estado['pendentes'] if descontar_fila else 0
    return {'numeros': detalhes, 'restante_dia': sum(n['restante_dia'] for n in detalhes),
            'restante_hora': sum(n['restante_hora'] for n in detalhes),
            'capacidade_agora': max(0, bruta - reservados), 'capacidade_bruta': bruta,
            'reservados_fila': reservados,
            'horario_permitido': permitido, 'motivo_horario': motivo, 'fila_ocupada': bool(estado['pendentes']),
            'lote_em_andamento': bool(estado['lote'])}


@app.post('/api/lote/capacidade')
async def consultar_capacidade_lote(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, 'Corpo deve ser um objeto')
    return capacidade_lote(body.get('numeros_selecionados', []))


@app.get('/api/lote/modelo.csv')
def modelo_lote():
    return Response(content='\ufefftelefone;cpf;nome;nascimento;produto;campanha;variavel_1;variavel_2;variavel_3\r\n',
                    media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="modelo-leads.csv"'})


@app.post('/api/lote/arquivo')
async def importar_planilha(request: Request):
    async with request.form(max_files=1, max_fields=2) as form:
        arquivo = form.get('arquivo')
        if not arquivo or not hasattr(arquivo, 'read'):
            raise HTTPException(400, 'Envie o campo arquivo')
        conteudo = await arquivo.read(planilhas.MAX_BYTES + 1)
        linhas, erros = await asyncio.to_thread(planilhas.ler, arquivo.filename or '', conteudo)
    cfg = carregar_configuracao()
    total_linhas = len(linhas) + len(erros)
    leads, vistos, duplicados = [], set(), 0
    excluidos_optout = 0
    for row in linhas:
        try:
            preparado = vendeai_mailing.preparar_lead(row['lead'], cfg)
            m = preparado['mailing']
            if optout.bloqueado(get_db,m['phone']):
                excluidos_optout += 1
                continue
            if not re.fullmatch(r'55[1-9][0-9][0-9]{8,9}', m['phone']):
                raise HTTPException(400, 'Telefone brasileiro invalido')
            if variaveis_vazias_impedem(preparado['variaveis_template']):
                raise HTTPException(400, 'Variaveis do template com lacunas ou valores vazios')
            if m.get('cpf'):
                cpf = re.sub(r'\D', '', m['cpf'])
                if len(cpf) != 11 or len(set(cpf)) == 1 or any(int(cpf[k]) != (sum(int(cpf[i]) * (k + 1 - i) for i in range(k)) * 10 % 11) % 10 for k in (9, 10)):
                    raise HTTPException(400, 'CPF invalido')
            if m.get('email') and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', m['email']):
                raise HTTPException(400, 'Email invalido')
            if m.get('birth_date'):
                try:
                    nascimento = datetime.strptime(m['birth_date'], '%d/%m/%Y').date()
                    if nascimento > date.today() or (date.today() - nascimento).days > 120 * 366:
                        raise ValueError()
                except ValueError:
                    raise HTTPException(400, 'Nascimento invalido; use DD/MM/AAAA')
            if m['phone'] in vistos:
                duplicados += 1
                continue
            vistos.add(m['phone'])
            leads.append(row['lead'])
        except HTTPException as erro:
            erros.append({'linha': row['linha'], 'motivo': erro.detail})
    return {'leads': leads, 'total_linhas': total_linhas,
            'validos': len(leads), 'duplicados': duplicados, 'excluidos_optout': excluidos_optout, 'erros': erros,
            'pode_importar': bool(leads) and not erros}


@app.post("/disparar")
@entrada_exclusiva
async def disparar(request: Request):
    body = await request.json()
    cfg = carregar_configuracao()
    preparado = vendeai_mailing.preparar_lead(body, cfg)
    lead = preparado["mailing"]
    cpf = lead.get("cpf", "")
    nome_lead = lead.get("name", "")
    telefone = lead["phone"]
    variaveis = preparado["variaveis_template"]

    if not telefone:
        raise HTTPException(400, "Campo 'whatsapp' ou 'telefone' obrigatorio")

    if not re.fullmatch(r"55[1-9][0-9][0-9]{8,9}", telefone):
        raise HTTPException(400, "Telefone brasileiro invalido")

    if optout.bloqueado(get_db,telefone):
        return {"status":"bloqueado","motivo_codigo":"optout","aceito":False}
    chave = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
    anterior = transbordo.Transbordo(get_db).buscar(body,chave)
    if anterior:
        return transbordo.Transbordo.publico(anterior)
    existente = obter_fila().existente(preparado,body,cfg,chave)
    if existente:
        return JSONResponse(existente,status_code=202 if existente['status']=='enfileirado' else 200)
    impedimento = impedimento_entrada()
    if cfg['transbordo_habilitado'] and impedimento and impedimento['motivo_codigo'] in ('nenhum_numero_disponivel','fora_do_horario'):
        if request.headers.get('X-Transbordo-Hop') or body.get('_transbordo'):
            return {**impedimento,'aceito':False,'motivo_codigo':'transbordo_sem_reencaminhamento'}
        if urlparse(cfg['transbordo_url']).hostname == request.url.hostname:
            return {'status':'erro','aceito':False,'motivo_codigo':'transbordo_destino_proprio'}
        transbordo.validar_lead(lead)
        return await asyncio.to_thread(transbordo.Transbordo(get_db).encaminhar,body,chave,cfg,lead)

    if variaveis_vazias_impedem(variaveis):
        indices_vazios = [i for i, v in enumerate(variaveis, start=1) if not str(v).strip()]
        return {
            "status": "erro",
            "motivo": f"variaveis_template com valor vazio no(s) indice(s) {indices_vazios} - a Meta rejeita template com variavel vazia (erro 131008). Verifique os dados de origem (ex: nome do lead ausente).",
            "motivo_codigo": "variavel_vazia",
        }

    if cfg["vendeai_habilitado"] and cfg["fila_habilitada"]:
        impedimento = impedimento_entrada()
        if impedimento:
            return {**impedimento, "aceito": False}
        chave = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
        item = obter_fila().enfileirar([(preparado, body, "individual", [], chave)], cfg)[0]
        return JSONResponse(item, status_code=202 if item["status"] == "enfileirado" else 200)

    permitido, motivo_horario = dentro_do_horario_permitido()
    if not permitido:
        return {"status": "erro", "motivo": motivo_horario, "motivo_codigo": "fora_do_horario"}

    cadastro = (await asyncio.to_thread(cadastrar_base_vendeai, [lead], cfg))[0]
    if vendeai_mailing.bloqueado(cadastro, cfg):
        return {"status": "pendente", "motivo_codigo": "vendeai_cadastro_falhou",
                "motivo": "Disparo nao realizado: cadastro na VendeAI nao confirmado", "vendeai": cadastro}

    return executar_disparo_preparado(preparado, cadastro)


def capacidade_admissao(origem, numeros, cfg):
    # SMS tem seu proprio fallback; nao recebe o limite de WhatsApp.
    if origem != 'lote' and cfg['sms_habilitado'] and token_sms():
        return None
    return capacidade_lote(numeros, descontar_fila=False)['capacidade_agora']


def impedimento_entrada(origem="individual", numeros=None):
    motivo = impedimento_fila(origem, numeros)
    if motivo:
        return motivo
    vagas = capacidade_admissao(origem, numeros, carregar_configuracao())
    if vagas is not None and estado_lote()['pendentes'] >= vagas:
        return {'status': 'erro', 'motivo_codigo': 'nenhum_numero_disponivel',
                'motivo': 'Capacidade atual ja reservada pelos leads na fila. Nada foi enfileirado.'}
    return None


def impedimento_fila(origem="individual", numeros=None):
    permitido, motivo = dentro_do_horario_permitido()
    if not permitido:
        return {"status": "erro", "motivo_codigo": "fora_do_horario", "motivo": motivo}
    with closing(get_db()) as conn:
        pool = conn.execute("SELECT * FROM numeros WHERE ativo=1").fetchall()
    if numeros:
        pool = [n for n in pool if n["id"] in numeros]
    if any(disponivel_agora(n) for n in pool):
        return None
    cfg = carregar_configuracao()
    if origem != "lote" and cfg["sms_habilitado"] and token_sms():
        return None
    return {"status": "erro", "motivo_codigo": "nenhum_numero_disponivel",
            "motivo": "Sem numero disponivel agora. Entrada encerrada, sem envio posterior automatico."}


def enviar_por_numero(numero, telefone, variaveis, nome):
    if not permitido_no_rodizio(numero):
        return False,'TIPO_FORA_DO_RODIZIO: este tipo de numero nao esta selecionado',''
    if conexoes_normais.normal(numero):
        return conexoes_normais.enviar(get_db,numero,telefone,nome,carregar_configuracao(),VENDEAI_API_TOKEN,lambda p: optout.bloqueado(get_db,p))
    variaveis=textos_templates.resolver(get_db,numero,variaveis)
    if gupshup.via(numero):
        return gupshup.enviar(get_db,numero,telefone,variaveis,lambda p: optout.bloqueado(get_db,p))
    if oficiais_vendeai.via(numero):
        return oficiais_vendeai.enviar(get_db,numero,telefone,variaveis,carregar_configuracao(),VENDEAI_API_TOKEN,atualizar_status_numero,lambda p: optout.bloqueado(get_db,p))
    return enviar_whatsapp(numero,telefone,variaveis)


def status_resultado(sucesso,erro):
    return 'sucesso' if sucesso else 'incerto' if erro.startswith('ENVIO_INCERTO:') else 'erro'


def executar_disparo_preparado(preparado, cadastro, origem="individual", numeros=None):
    if optout.bloqueado(get_db,preparado["mailing"]["phone"]):
        return {"status":"bloqueado","motivo_codigo":"optout","aceito":False}
    lead = preparado["mailing"]
    telefone = lead["phone"]
    cpf = lead.get("cpf", "")
    nome_lead = lead.get("name", "")
    variaveis = preparado["variaveis_template"]
    permitido, motivo = dentro_do_horario_permitido()
    if not permitido:
        return {"status": "pendente", "motivo_codigo": "fora_do_horario", "motivo": motivo, "vendeai": cadastro}
    pool = None
    if numeros:
        with closing(get_db()) as conn:
            pool = conn.execute(f"SELECT * FROM numeros WHERE id IN ({','.join('?' for _ in numeros)}) AND ativo=1", numeros).fetchall()
    numero = proximo_numero_disponivel(_ultimo_usado["id"], pool=pool)
    if numero is None:
        resultado_sms = tentar_fallback_sms(cpf, nome_lead, telefone, variaveis) if origem != "lote" else None
        if resultado_sms is not None:
            return {**resultado_sms, "vendeai": cadastro}
        return {"status": "erro", "motivo": "Nenhum numero disponivel (todos pausados ou limite atingido)", "motivo_codigo": "nenhum_numero_disponivel", "vendeai": cadastro}

    sucesso, erro, wamid = enviar_por_numero(numero, telefone, variaveis, nome_lead)
    _ultimo_usado["id"] = numero["id"]

    registrar_disparo(numero["id"], cpf, nome_lead, telefone, status_resultado(sucesso,erro), erro, wamid)
    verificar_qualidade_pos_envio(numero)

    if not sucesso and (conexoes_normais.normal(numero) or oficiais_vendeai.via(numero) or gupshup.via(numero)):
        return {"status":status_resultado(sucesso,erro),"numero_usado":numero["nome"],"canal":"whatsapp","erro":erro,"vendeai":cadastro}
    if not sucesso:
        resultado_sms = tentar_fallback_sms(cpf, nome_lead, telefone, variaveis) if origem != "lote" else None
        if resultado_sms is not None:
            return {**resultado_sms, "vendeai": cadastro}
        return {
            "status": "erro",
            "numero_usado": numero["nome"],
            "canal": "whatsapp",
            "erro": erro,
            "vendeai": cadastro,
        }

    # WhatsApp deu certo. No modo "sempre", manda SMS tambem, alem do
    # WhatsApp (custa mais - e uma escolha explicita de quem configurou).
    cfg = carregar_configuracao()
    sms_extra = None
    if origem != "lote" and cfg["sms_habilitado"] and cfg["sms_modo"] == "sempre":
        sms_extra = tentar_fallback_sms(cpf, nome_lead, telefone, variaveis)

    return {
        "status": "sucesso",
        "numero_usado": numero["nome"],
        "canal": "whatsapp",
        "erro": None,
        "sms_extra": sms_extra,
        "vendeai": cadastro,
    }


@app.post("/api/disparo-lote")
@entrada_exclusiva
async def disparo_lote(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Corpo deve ser um objeto")
    cfg = carregar_configuracao()
    numeros_texto = vendeai_mailing.texto(body, "numeros_texto")
    numeros_selecionados = body.get("numeros_selecionados", [])
    if not isinstance(numeros_selecionados, list) or not all(type(n) is int for n in numeros_selecionados):
        raise HTTPException(400, "numeros_selecionados deve ser lista de IDs inteiros")
    if "leads" in body and numeros_texto:
        raise HTTPException(400, "Use leads ou numeros_texto, nao ambos")
    entradas = body.get("leads") if "leads" in body else [
        {"telefone": t} for t in re.split(r"[,\n\r; ]+", numeros_texto) if t
    ]
    if not isinstance(entradas, list) or not 1 <= len(entradas) <= 20000:
        raise HTTPException(400, "Envie de 1 a 20000 leads")

    formatados = []
    bloqueados = []
    preparados = {}
    originais = {}
    vistos = set()
    for entrada in entradas:
        preparado = vendeai_mailing.preparar_lead(entrada, cfg)
        limpo = preparado["mailing"]["phone"]
        if limpo not in vistos:
            vistos.add(limpo)
            if optout.bloqueado(get_db,limpo):
                bloqueados.append({"telefone":limpo,"status":"bloqueado","motivo_codigo":"optout"})
                continue
            formatados.append(limpo)
            preparados[limpo] = preparado
            originais[limpo] = entrada

    if not formatados:
        return {"total":len(bloqueados),"resultados":bloqueados}
    capacidade = capacidade_lote(numeros_selecionados)
    if len(formatados) > capacidade['capacidade_agora']:
        return JSONResponse({'status': 'erro', 'aceito': False, 'motivo_codigo': 'capacidade_insuficiente',
                             'motivo': 'Lote excede a capacidade restante de envio agora. Reduza o lote.',
                             'total': len(formatados)+len(bloqueados), 'capacidade': capacidade}, status_code=409)

    if cfg["vendeai_habilitado"] and cfg["fila_habilitada"]:
        impedimento = impedimento_fila("lote", numeros_selecionados)
        if impedimento:
            return {"total": len(formatados)+len(bloqueados), "aceito": False, "erro_geral": impedimento["motivo"],
                    "resultados": [{"telefone": t, **impedimento} for t in formatados]}
        itens, invalidos = [], list(bloqueados)
        for telefone in formatados:
            preparado = preparados[telefone]
            if not re.fullmatch(r"55[1-9][0-9][0-9]{8,9}", telefone) or variaveis_vazias_impedem(preparado["variaveis_template"], numeros_selecionados):
                invalidos.append({"telefone": telefone, "status": "invalido", "motivo": "Telefone ou variaveis invalidos"})
            else:
                itens.append((preparado, originais[telefone], "lote", numeros_selecionados, originais[telefone].get("idempotency_key")))
        resultados = obter_fila().enfileirar(itens, cfg) if itens else []
        return JSONResponse({"total": len(formatados)+len(bloqueados), "resultados": resultados + invalidos}, status_code=202)

    permitido, motivo_horario = dentro_do_horario_permitido()
    if not permitido:
        return {
            "total": len(formatados)+len(bloqueados),
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
        return {"total": len(formatados)+len(bloqueados), "resultados": bloqueados, "erro_geral": "Nenhum numero disponivel/selecionado"}

    erros_entrada = {}
    for telefone in formatados:
        if not re.fullmatch(r"55[1-9][0-9][0-9]{8,9}", telefone):
            erros_entrada[telefone] = "Telefone brasileiro invalido"
        elif variaveis_vazias_impedem(preparados[telefone]["variaveis_template"], numeros_selecionados):
            erros_entrada[telefone] = "variaveis_template contem valor vazio"
    validos = [t for t in formatados if t not in erros_entrada]
    cadastros = dict(zip(validos, await asyncio.to_thread(
        cadastrar_base_vendeai, [preparados[t]["mailing"] for t in validos], cfg
    )))

    resultados = list(bloqueados)
    idx_rr = 0
    for telefone in formatados:
        if telefone in erros_entrada:
            resultados.append({"telefone": telefone, "status": "invalido", "motivo": erros_entrada[telefone]})
            continue
        cadastro = cadastros[telefone]
        if vendeai_mailing.bloqueado(cadastro, cfg):
            resultados.append({"telefone": telefone, "status": "pendente", "motivo": "Cadastro VendeAI nao confirmado",
                               "motivo_codigo": "vendeai_cadastro_falhou", "vendeai": cadastro})
            continue

        preparado = preparados[telefone]
        lead = preparado["mailing"]
        enviado = False
        for _ in range(len(pool)):
            candidato = pool[idx_rr % len(pool)]
            idx_rr += 1
            if disponivel_agora(candidato):
                sucesso, erro, wamid = enviar_por_numero(candidato, telefone, preparado["variaveis_template"], lead.get("name", ""))
                registrar_disparo(candidato["id"], lead.get("cpf", ""), lead.get("name", "Disparo em lote"), telefone, status_resultado(sucesso,erro), erro, wamid)
                resultados.append({"telefone": telefone, "status": status_resultado(sucesso,erro), "numero_usado": candidato["nome"], "vendeai": cadastro, "erro": erro})
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
            resultados.append({"telefone": telefone, "status": "pendente", "motivo": "Sem numero disponivel no momento", "vendeai": cadastro})

    return {"total": len(formatados)+len(bloqueados), "resultados": resultados}


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
        _aplicar_pausa_ou_reativacao_qualidade(numero_id, n["nome"], n["ativo"], n["pausado_por_qualidade"], qualidade)

    return {"qualidade": qualidade, "template_categoria": categoria, "template_status": status_template}


@app.post("/api/atualizar-todos")
def atualizar_todos_status():
    descobrir_conexoes_seguro()
    conn = get_db()
    # Idem ao loop de 15min: confere todos, a pausa manual fica protegida pela
    # logica de _aplicar_pausa_ou_reativacao_qualidade.
    numeros = conn.execute("SELECT * FROM numeros").fetchall()
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

        _aplicar_pausa_ou_reativacao_qualidade(n["id"], n["nome"], n["ativo"], n["pausado_por_qualidade"], qualidade)

        extra = {}
        if conexoes_normais.normal(n):
            with closing(get_db()) as c:
                extra = {'tipo_envio':'vendeai_normal','conexao_status':c.execute('SELECT conexao_status FROM numeros WHERE id=?',(n['id'],)).fetchone()[0]}
        resultados.append({"nome": n["nome"], "qualidade": qualidade, "template_categoria": categoria, **extra})

    return {"total_atualizado": len(resultados), "resultados": resultados}


@app.post('/webhook/vendeai-eventos')
async def receber_eventos_vendeai(request: Request):
    cfg=carregar_configuracao()
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>2_000_000:
            raise HTTPException(413,'Evento acima do limite de 2 MB.')
    return await asyncio.to_thread(vendeai_eventos.receber,get_db,request.query_params.get('chave',''),bytes(raw),cfg['vendeai_account_id'])


@app.get('/api/vendeai-eventos')
def listar_eventos_vendeai():
    return vendeai_eventos.listar(get_db)


@app.get('/api/link-conversa')
def resumo_link_conversa():
    return link_conversa.resumo(get_db,carregar_configuracao()['vendeai_account_id'])


@app.patch('/api/link-conversa')
def configurar_link_conversa(body: dict):
    return link_conversa.configurar(get_db,carregar_configuracao()['vendeai_account_id'],body.get('ativo'))


@app.get('/api/vendeai-eventos/config')
def configuracao_eventos_vendeai(request: Request):
    with closing(get_db()) as c:
        cfg=c.execute('SELECT token,new_habilitado FROM vendeai_webhook_config WHERE id=1').fetchone()
        token=cfg['token']
    return JSONResponse({'webhook_path':'/webhook/vendeai-eventos?chave='+token,'new_habilitado':bool(cfg['new_habilitado'])},headers={'Cache-Control':'no-store'})


@app.get('/api/vendeai-new')
def listar_envios_new():
    with closing(get_db()) as c:
        return [dict(r) for r in c.execute('SELECT id,evento_id,proposta_id,estado,detalhe,http_status,enviado_em,resposta FROM vendeai_new_outbox ORDER BY id DESC LIMIT 100')]


@app.patch('/api/vendeai-eventos/config')
def configurar_eventos_new(body: dict):
    if type(body.get('new_habilitado')) is not bool:raise HTTPException(400,'Informe new_habilitado booleano.')
    with closing(get_db()) as c,c:
        cfg=json.loads(c.execute('SELECT new_config FROM vendeai_webhook_config WHERE id=1').fetchone()['new_config'])
        if body['new_habilitado'] and not cfg.get('modelo'):raise HTTPException(400,'Integração New ainda não configurada.')
        c.execute('UPDATE vendeai_webhook_config SET new_habilitado=? WHERE id=1',(int(body['new_habilitado']),))
    return {'ok':True}


@app.get('/api/vendeai-eventos/{ident}')
def evento_vendeai_original(ident: int):
    with closing(get_db()) as c:
        row=c.execute('SELECT payload_raw FROM vendeai_eventos WHERE id=?',(ident,)).fetchone()
    if not row:raise HTTPException(404,'Evento não encontrado.')
    return Response(content=row['payload_raw'],media_type='application/json',headers={'Cache-Control':'no-store'})


@app.get('/optout',response_class=HTMLResponse)
def pagina_optout(request: Request):
    return templates.TemplateResponse(request,'optout.html',{})


@app.get('/api/optout')
def consultar_optout():
    with closing(get_db()) as c:
        cfg=dict(c.execute('SELECT * FROM optout_config WHERE id=1').fetchone())
        cfg['textos']=json.loads(cfg['textos'])
        return JSONResponse({'config':cfg,'token_configurado':bool(VENDEAI_API_TOKEN),
            'total':c.execute('SELECT count(*) FROM optout').fetchone()[0],
            'contatos':[dict(r) for r in c.execute('SELECT telefone,nome,chat_id,campanha,ultimo_optout_em FROM optout ORDER BY ultimo_optout_em DESC LIMIT 100')],
            'tarefas':[dict(r) for r in c.execute('SELECT id,chat_id,passo,estado,detalhe FROM optout_tarefas ORDER BY id DESC LIMIT 100')],
            'passos':[dict(r) for r in c.execute('SELECT chat_id,passo,http_status,duracao_ms,desde_recebimento_ms,resultado FROM optout_passos ORDER BY id DESC LIMIT 100')]},headers={'Cache-Control':'no-store'})


@app.post('/api/optout/testar-exclusao')
def testar_exclusao_optout(body: dict):
    alvo=optout.telefone(body.get("telefone"))
    if not re.fullmatch(r"55[1-9][0-9][0-9]{8,9}",alvo):
        raise HTTPException(400,"Informe o telefone específico de teste com DDD.")
    if not VENDEAI_API_TOKEN:raise HTTPException(400,'Token do CRM não configurado.')
    with closing(get_db()) as c,c:
        if not c.execute('SELECT habilitado FROM optout_config WHERE id=1').fetchone()[0]:
            raise HTTPException(400,'Ative o encerramento automático primeiro.')
        c.execute('UPDATE optout_config SET teste_excluir_restantes=1,teste_telefone=? WHERE id=1',(alvo,))
    return {'ok':True,'mensagem':'O próximo novo opt-out desse telefone vai testar a exclusão direta da conversa.'}


@app.patch('/api/optout/config')
def configurar_optout(body: dict):
    textos=body.get('textos')
    if type(body.get('habilitado')) is not bool or not isinstance(textos,list) or not 1<=len(textos)<=100 or any(not isinstance(t,str) or not t.strip() or len(t)>200 for t in textos):
        raise HTTPException(400,'Informe a ativação e de 1 a 100 textos, de até 200 caracteres cada.')
    if body['habilitado'] and not VENDEAI_API_TOKEN:raise HTTPException(400,'Configure VENDEAI_API_TOKEN no servidor.')
    if 'excluir_conversa_primeiro' in body and type(body['excluir_conversa_primeiro']) is not bool:
        raise HTTPException(400,'Informe excluir_conversa_primeiro booleano.')
    with closing(get_db()) as c,c:
        c.execute('UPDATE optout_config SET habilitado=?,textos=? WHERE id=1',(int(body['habilitado']),json.dumps(list(dict.fromkeys(optout.normalizar(t) for t in textos)))))
        if 'excluir_conversa_primeiro' in body:
            c.execute('UPDATE optout_config SET excluir_conversa_primeiro=? WHERE id=1',(int(body['excluir_conversa_primeiro']),))
    return {'ok':True}


@app.get('/eventos-vendeai',response_class=HTMLResponse)
def pagina_eventos_vendeai(request: Request):
    return templates.TemplateResponse(request,'eventos_vendeai.html',{})


@app.get('/resultados', response_class=HTMLResponse)
def pagina_resultados(request: Request):
    return templates.TemplateResponse(request, 'resultados.html', {})


@app.get('/api/resultados')
def consultar_resultados(inicio: str = '', fim: str = '', busca: str = '', origem: str = '', status: str = '', pagina: int = 1):
    hoje = datetime.now(TZ).date()
    try:
        a = date.fromisoformat(inicio) if inicio else hoje
        b = date.fromisoformat(fim) if fim else hoje
    except ValueError:
        raise HTTPException(400, 'Informe datas válidas.')
    if b < a or (b-a).days > 365 or pagina < 1 or len(busca) > 150:
        raise HTTPException(400, 'Informe um período de até 366 dias e filtros válidos.')
    dados = resultados.calcular(get_db, datetime.combine(a, datetime.min.time(), TZ),
                                datetime.combine(b + timedelta(days=1), datetime.min.time(), TZ),
                                busca.strip(), origem, status, pagina)
    return JSONResponse(dados, headers={'Cache-Control': 'no-store'})


@app.get('/api/resultados/resumo')
def resumo_resultados():
    inicio=datetime.now(TZ).replace(hour=0,minute=0,second=0,microsecond=0)
    dados=resultados.calcular(get_db,inicio,inicio+timedelta(days=1),somente_resumo=True)
    return JSONResponse({'atualizado_em':dados['atualizado_em'],
        'atividade':{k:dados['atividade'][k] for k in ('simulacoes','valor_propostas','propostas_sem_valor')},
        'pagamentos':dados['pagamentos'],'comissoes':dados['comissoes']},headers={'Cache-Control':'no-store'})


@app.get('/api/comissoes/regras')
def regras_comissao():
    with closing(get_db()) as c:
        return comissoes.carregar_regras(c)


@app.post('/api/resultados/equipe')
def proposta_equipe(body: dict):
    return comissoes.operar(get_db, body)


@app.put('/api/resultados/custos-meta')
def configurar_custo_meta(body: dict):
    with closing(get_db()) as c:
        return custos_meta.salvar(c, body, agora_iso())


@app.get('/api/resultados/manuais/{dia}')
def consultar_pagamento_manual(dia: str):
    return JSONResponse(resultados.manual_dia(get_db, dia), headers={'Cache-Control': 'no-store'})


@app.put('/api/resultados/manuais/{dia}')
def salvar_pagamento_manual(dia: str, body: dict):
    return JSONResponse(resultados.salvar_manual(get_db, dia, body), headers={'Cache-Control': 'no-store'})


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
    banidos_hoje=conn.execute('SELECT COUNT(*) FROM banimentos_meta WHERE dia=?',(agora.date().isoformat(),)).fetchone()[0]
    inicio_dia = agora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE status = 'sucesso' AND criado_em >= ?",
        (inicio_dia,),
    ).fetchone()
    tipos=conn.execute("""SELECT
        COALESCE(SUM(CASE WHEN d.canal='whatsapp' AND COALESCE(NULLIF(d.tipo_envio,''),n.tipo_envio)='vendeai_normal' THEN 1 ELSE 0 END),0) AS normais,
        COALESCE(SUM(CASE WHEN d.canal='whatsapp' AND COALESCE(NULLIF(d.tipo_envio,''),n.tipo_envio) IN ('meta','gupshup') THEN 1 ELSE 0 END),0) AS oficiais,
        COALESCE(SUM(CASE WHEN d.canal='sms' THEN 1 ELSE 0 END),0) AS sms
        FROM disparos d LEFT JOIN numeros n ON n.id=d.numero_id
        WHERE d.status='sucesso' AND d.criado_em>=?""",(inicio_dia,)).fetchone()
    total_erros = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE status = 'erro' AND criado_em >= ?",
        (inicio_dia,),
    ).fetchone()
    total_positivos = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE resposta_lead = 'positivo' AND criado_em >= ?",
        (inicio_dia,),
    ).fetchone()
    total_negativos = conn.execute(
        "SELECT COUNT(*) as c FROM disparos WHERE resposta_lead = 'negativo' AND criado_em >= ?",
        (inicio_dia,),
    ).fetchone()
    conn.close()
    return {
        "total_sucesso_hoje": row["c"],
        "total_banidos_hoje": banidos_hoje,
        "total_normais_hoje": tipos['normais'],
        "total_oficiais_hoje": tipos['oficiais'],
        "total_sms_hoje": tipos['sms'],
        "total_outros_hoje": row['c']-tipos['normais']-tipos['oficiais']-tipos['sms'],
        "total_erro_hoje": total_erros["c"],
        "total_positivos_hoje": total_positivos["c"],
        "total_negativos_hoje": total_negativos["c"],
    }


@app.get("/api/estatisticas-diarias")
def estatisticas_diarias(dias: int = 30):
    """
    Resumo diario (enviados, erros, positivos, negativos) dos ultimos N
    dias, agrupado por dia local (TZ America/Sao_Paulo). Usado pela
    pagina /estatisticas pro grafico historico.
    """
    dias = max(1, min(dias, 365))
    conn = get_db()
    agora = datetime.now(TZ)
    inicio = (agora - timedelta(days=dias - 1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    rows = conn.execute(
        """
        SELECT
            substr(criado_em, 1, 10) as dia,
            SUM(CASE WHEN status = 'sucesso' THEN 1 ELSE 0 END) as total_sucesso,
            SUM(CASE WHEN status = 'erro' THEN 1 ELSE 0 END) as total_erro,
            SUM(CASE WHEN resposta_lead = 'positivo' THEN 1 ELSE 0 END) as total_positivos,
            SUM(CASE WHEN resposta_lead = 'negativo' THEN 1 ELSE 0 END) as total_negativos
        FROM disparos
        WHERE criado_em >= ?
        GROUP BY dia
        ORDER BY dia ASC
        """,
        (inicio,),
    ).fetchall()
    conn.close()

    # preenche os dias sem nenhum disparo com zero, pra o grafico nao ter buracos
    por_dia = {r["dia"]: dict(r) for r in rows}
    resultado = []
    for i in range(dias):
        d = (agora - timedelta(days=dias - 1 - i)).strftime("%Y-%m-%d")
        if d in por_dia:
            resultado.append(por_dia[d])
        else:
            resultado.append({"dia": d, "total_sucesso": 0, "total_erro": 0, "total_positivos": 0, "total_negativos": 0})

    return resultado


@app.get("/estatisticas", response_class=HTMLResponse)
def pagina_estatisticas(request: Request):
    return templates.TemplateResponse(request, "estatisticas.html", {})


@app.get("/api/configuracao")
def obter_configuracao():
    cfg = carregar_configuracao()
    cfg["transbordo_token_configurado"] = bool(cfg.pop("transbordo_token", ""))
    token_painel = cfg.pop("vendeai_mailing_token", "")
    cfg["vendeai_token_configurado"] = bool(token_painel or VENDEAI_API_TOKEN)
    cfg["vendeai_token_origem"] = "painel" if token_painel else ("ambiente" if VENDEAI_API_TOKEN else "ausente")
    cfg['sms_token_configurado']=bool(token_sms())
    return cfg


@app.patch("/api/configuracao")
async def atualizar_configuracao(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Corpo deve ser um objeto")
    if "fila_habilitada" in body and type(body["fila_habilitada"]) is not bool:
        raise HTTPException(400, "fila_habilitada deve ser booleano")
    if "fila_intervalo_segundos" in body and (type(body["fila_intervalo_segundos"]) is not int or not 60 <= body["fila_intervalo_segundos"] <= 3600):
        raise HTTPException(400, "fila_intervalo_segundos deve estar entre 60 e 3600")
    atual = carregar_configuracao()
    if 'transbordo_habilitado' in body and type(body['transbordo_habilitado']) is not bool:
        raise HTTPException(400,'transbordo_habilitado deve ser booleano')
    for campo in ('transbordo_url','transbordo_nome','transbordo_token'):
        if campo in body:
            if not isinstance(body[campo],str):
                raise HTTPException(400,campo+' deve ser texto')
            body[campo]=body[campo].strip()
    if body.get('transbordo_token') == '':
        del body['transbordo_token']
    if body.get('transbordo_remover_token') is True:
        body['transbordo_token']=''
    if body.get('transbordo_url'):
        body['transbordo_url']=transbordo.validar_url(body['transbordo_url'])
        if urlparse(body['transbordo_url']).hostname == request.url.hostname:
            raise HTTPException(400,'Destino nao pode ser este mesmo painel')
    prevista_transbordo={**atual,**body}
    if prevista_transbordo['transbordo_habilitado'] and not (prevista_transbordo['transbordo_url'] and prevista_transbordo['transbordo_token']):
        raise HTTPException(400,'Informe URL e chave API do destino antes de ativar o transbordo')
    if 'transbordo_url' in body and body['transbordo_url'] != atual['transbordo_url']:
        with closing(get_db()) as c:
            if c.execute("SELECT COUNT(*) FROM transbordos WHERE status IN ('encaminhado','incerto','enviando')").fetchone()[0]:
                raise HTTPException(409,'Consulte e resolva os transbordos pendentes antes de trocar o destino')
    if any(k in body and body[k].strip() != str(atual[k]).strip() for k in ("vendeai_account_id", "vendeai_mailing_url") if isinstance(body.get(k), str)):
        with closing(get_db()) as conn:
            pendentes = conn.execute("SELECT COUNT(*) FROM fila_disparos WHERE status IN ('aguardando_cadastro','cadastrando','pronto','enviando')").fetchone()[0]
        if pendentes:
            raise HTTPException(409, "Conclua ou cancele os itens pendentes antes de trocar a conta/URL da VendeAI")
    for campo in (*vendeai_mailing.CONFIG_COLUMNS, "vendeai_account_id"):
        if campo in body:
            if not isinstance(body[campo], str):
                raise HTTPException(400, f"{campo} deve ser texto")
            body[campo] = body[campo].strip()
    if "vendeai_mailing_token" in body and not body["vendeai_mailing_token"]:
        # Campo vazio no formulario preserva segredo existente.
        del body["vendeai_mailing_token"]
    if body.get("vendeai_remover_token") is True:
        body["vendeai_mailing_token"] = ""
    if "vendeai_mailing_url" in body:
        url = urlparse(body["vendeai_mailing_url"])
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise HTTPException(400, "vendeai_mailing_url deve ser uma URL base HTTPS, sem caminho ou credenciais")
    if "vendeai_falha_modo" in body and body["vendeai_falha_modo"] not in ("bloquear", "continuar"):
        raise HTTPException(400, "vendeai_falha_modo deve ser bloquear ou continuar")
    if "vendeai_habilitado" in body and type(body["vendeai_habilitado"]) is not bool:
        raise HTTPException(400, "vendeai_habilitado deve ser booleano")
    prevista = {**carregar_configuracao(), **body}
    if prevista["vendeai_habilitado"] and (not prevista["vendeai_account_id"] or not (prevista["vendeai_mailing_token"] or VENDEAI_API_TOKEN)):
        raise HTTPException(400, "Informe Account ID e token do CRM antes de habilitar a VendeAI")

    if "modo" in body and body["modo"] not in ("automatico", "horario"):
        raise HTTPException(400, "modo deve ser 'automatico' ou 'horario'")
    if 'rodizio_tipo' in body and body['rodizio_tipo'] not in ('todos','oficiais','normais'):
        raise HTTPException(400,'Escolha todos, oficiais ou normais para o rodízio.')

    if 'faixas_horario' in body:
        body['faixas_horario']=json.dumps(validar_faixas(body['faixas_horario']))
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

    if "vendeai_habilitado" in body:
        body["vendeai_habilitado"] = 1 if body["vendeai_habilitado"] else 0

    if "vendeai_url" in body and body["vendeai_url"] and not body["vendeai_url"].startswith(("http://", "https://")):
        raise HTTPException(400, "vendeai_url deve comecar com http:// ou https://")

    campos_validos = {
        "modo", "hora_inicio", "hora_fim", "dias_semana", "faixas_horario", "rodizio_tipo",
        "sms_habilitado", "sms_modo", "sms_template", "whatsapp_destino",
        "whatsapp_mensagem_template", "link_base_url",
        "vendeai_habilitado", "vendeai_url", "vendeai_account_id", "vendeai_inbox_id",
        *vendeai_mailing.CONFIG_COLUMNS, *fila_disparos.CONFIG_COLUMNS, *transbordo.CONFIG_COLUMNS,
    }
    atualizacoes = {k: v for k, v in body.items() if k in campos_validos}
    if not atualizacoes:
        raise HTTPException(400, "Nenhum campo valido informado")

    conn = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in atualizacoes)
    conn.execute(f"UPDATE configuracao SET {set_clause} WHERE id = 1", list(atualizacoes.values()))
    conn.commit()
    conn.close()

    registrar_evento(None, "configuracao_atualizada", f"Campos atualizados: {', '.join(sorted(atualizacoes))}")
    return obter_configuracao()


@app.put('/api/sms/credencial')
def salvar_credencial_sms(body: dict):
    token=body.get('token')
    if not isinstance(token,str) or not 1<=len(token.strip())<=4096 or any(c.isspace() for c in token.strip()):
        raise HTTPException(400,'Informe um token SMS válido, sem espaços.')
    with closing(get_db()) as c,c:
        c.execute('INSERT INTO sms_credencial(id,token) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token',(token.strip(),))
    _cache_saldo_sms.update(valor=None,expira_em=None,atualizado_em=None,erro=None)
    return {'configurado':True}


@app.get("/api/sms/saldo")
def saldo_sms():
    """Consulta o saldo de creditos disponivel na GTI SMS (com cache de alguns minutos)."""
    if not token_sms():
        return {"configurado": False, "saldo": None, "expira_em": None, "erro": None}
    info = consultar_saldo_sms()
    return {
        "configurado": True,
        "saldo": info["valor"],
        "expira_em": info["expira_em"],
        "erro": info["erro"],
    }


@app.get('/campanhas-sms',response_class=HTMLResponse)
def pagina_campanhas_sms(request: Request):
    return templates.TemplateResponse(request,'campanhas_sms.html',{})


@app.get('/lembretes-sms',response_class=HTMLResponse)
def pagina_lembretes_sms(request: Request):
    return templates.TemplateResponse(request,'lembretes_sms.html',{})


@app.get('/api/lembretes-sms')
def listar_lembretes_sms(inicio: str='',fim: str=''):
    try:
        a=datetime.strptime(inicio,'%Y-%m-%d').replace(tzinfo=TZ) if inicio else datetime.now(TZ).replace(hour=0,minute=0,second=0,microsecond=0)
        b=(datetime.strptime(fim,'%Y-%m-%d').replace(tzinfo=TZ) if fim else a)+timedelta(days=1)
        if b<=a:raise ValueError()
    except ValueError:raise HTTPException(400,'Período inválido.')
    d=lembretes_sms.relatorio(get_db,carregar_configuracao()['vendeai_account_id'],a,b)
    d['sms_configurado']=bool(token_sms())
    return d


@app.put('/api/lembretes-sms')
def salvar_lembretes_sms(body: dict):
    if body.get('habilitado') and not token_sms():raise HTTPException(400,'Configure o token de SMS primeiro.')
    return lembretes_sms.salvar(get_db,body)


@app.get('/api/campanhas-sms')
def listar_campanhas_sms():
    permitido,motivo=dentro_do_horario_permitido()
    return {'campanhas':campanhas_sms.listar(get_db),'mensagem':normalizar_texto_sms(carregar_configuracao()['sms_template']),
            'configurado':bool(token_sms()),'horario_permitido':permitido,'motivo_horario':motivo}


@app.post('/api/campanhas-sms')
async def criar_campanha_sms(request: Request):
    async with request.form(max_files=1,max_fields=3) as form:
        nome=form.get('nome','');arquivo=form.get('arquivo');texto=form.get('telefones','')
        if arquivo and getattr(arquivo,'filename',''):
            conteudo=await arquivo.read(planilhas.MAX_BYTES+1)
            linhas,erros=await asyncio.to_thread(planilhas.ler,arquivo.filename,conteudo)
        else:
            if not isinstance(texto,str) or len(texto)>1_000_000:raise HTTPException(400,'Lista inválida ou muito grande.')
            linhas=[{'linha':i,'lead':{'telefone':v}} for i,v in enumerate(re.split(r'[\r\n;,]+',texto),1) if v.strip()];erros=[]
    return await asyncio.to_thread(campanhas_sms.criar,get_db,nome,normalizar_texto_sms(carregar_configuracao()['sms_template']),linhas,erros)


@app.get('/api/campanhas-sms/{ident}')
def detalhe_campanha_sms(ident: str):return campanhas_sms.detalhe(get_db,ident)


@app.post('/api/campanhas-sms/{ident}/iniciar')
def iniciar_campanha_sms(ident: str):
    return campanhas_sms.iniciar(get_db,ident,token_sms(),dentro_do_horario_permitido,consultar_saldo_sms)


@app.post('/api/campanhas-sms/{ident}/cancelar')
def cancelar_campanha_sms(ident: str):return campanhas_sms.cancelar(get_db,ident)


def template_estatico(template):
    componentes = template.get("components")
    if not isinstance(componentes, list) or not componentes:
        return False
    if template.get("category") == "AUTHENTICATION":
        return False
    if not any(c.get("type")=="BODY" for c in componentes):
        return False
    for c in componentes:
        if c.get("type")=="HEADER" and c.get("format")=="IMAGE":
            continue
        if c.get("type") not in ("BODY", "HEADER", "FOOTER", "BUTTONS"):
            return False
        if c.get("type") == "HEADER" and c.get("format") != "TEXT":
            return False
        if c.get("type") == "BUTTONS" and any(b.get("type") not in ("URL", "PHONE_NUMBER", "QUICK_REPLY") for b in c.get("buttons", [])):
            return False
        if "{{" in json.dumps(c):
            return False
    return True


def token_waba(body):
    token=body.get('access_token','').strip()
    if token: return token
    with closing(get_db()) as c:
        row=c.execute('SELECT access_token FROM wabas_salvas WHERE waba_id=?',(body.get('waba_id','').strip(),)).fetchone()
    return row['access_token'] if row else ''


def atualizar_nome_waba(row):
    nome=None
    try:
        r=requests.get('https://graph.facebook.com/v21.0/'+requests.utils.quote(row['waba_id'],safe=''),
            params={'fields':'id,name'},headers={'Authorization':'Bearer '+row['access_token']},timeout=(3,5),allow_redirects=False)
        d=r.json()
        if r.status_code==200 and d.get('id')==row['waba_id'] and isinstance(d.get('name'),str) and d['name'].strip():
            nome=d['name'].strip()[:250]
    except (requests.RequestException,ValueError,AttributeError): pass
    with closing(get_db()) as c,c:
        c.execute('UPDATE wabas_salvas SET nome=COALESCE(?,nome),nome_consultado_em=? WHERE waba_id=? AND access_token=?',
            (nome,datetime.now().timestamp(),row['waba_id'],row['access_token']))


@app.get('/wabas/criar-templates', response_class=HTMLResponse)
def pagina_criar_templates(request: Request):
    return templates.TemplateResponse(request, 'criar_templates.html', {})


@app.post('/api/templates-lotes')
def criar_templates_lote(body: dict):
    return templates_lote.criar_lote(get_db, body)


@app.get('/api/templates-lotes')
def listar_templates_lotes():
    with closing(get_db()) as c:
        rows=c.execute('SELECT id,payload,criado_em FROM template_lotes ORDER BY criado_em DESC LIMIT 30').fetchall()
    return [{'id':r['id'],'nome':json.loads(r['payload'])['name'],'criado_em':r['criado_em']} for r in rows]


@app.get('/api/templates-lotes/{ident}')
def resumo_templates_lote(ident: str):
    return templates_lote.resumo(get_db, ident)


@app.post('/api/templates-lotes/{ident}/{waba}/executar')
def executar_templates_lote(ident: str, waba: str):
    return templates_lote.executar(get_db, consultar_meta_waba, ident, waba)


@app.post('/api/templates-lotes/{ident}/{waba}/consultar')
def consultar_templates_lote(ident: str, waba: str):
    return templates_lote.executar(get_db, consultar_meta_waba, ident, waba, apenas_consultar=True)


@app.get('/wabas', response_class=HTMLResponse)
def pagina_wabas(request: Request):
    return templates.TemplateResponse(request, 'wabas.html', {})


def consultar_meta_waba(waba_id, token, edge='', fields='id,name'):
    params = {'fields': fields, 'limit': 100} if edge else {'fields': fields}
    itens, cursores = [], set()
    for _ in range(100):
        try:
            r = requests.get('https://graph.facebook.com/v21.0/' + quote(waba_id, safe='') + edge,
                             params=params, headers={'Authorization': 'Bearer ' + token}, timeout=(5, 15), allow_redirects=False)
            data = r.json()
            if r.status_code != 200 or not isinstance(data, dict) or 'error' in data:
                return {'erro': 'Consulta indisponível. Verifique token e permissões.', 'http_status': r.status_code}
            if not edge:
                return {'dados': data}
            if not isinstance(data.get('data'), list):
                return {'erro': 'Resposta inesperada da Meta.'}
            itens.extend(data['data'])
            if not data.get('paging', {}).get('next'):
                return {'dados': itens}
            cursor = data.get('paging', {}).get('cursors', {}).get('after')
            if not cursor or cursor in cursores:
                return {'erro': 'A Meta não permitiu concluir a paginação.'}
            cursores.add(cursor)
            params['after'] = cursor
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            return {'erro': 'Não foi possível consultar a Meta agora.'}
    return {'erro': 'Lista muito extensa para concluir esta consulta.'}


@app.post('/api/wabas-painel')
def salvar_waba_painel(body: dict):
    waba_id = str(body.get('waba_id', '')).strip()
    token = body.get('access_token', '')
    if not waba_id.isdigit() or not isinstance(token, str):
        raise HTTPException(400, 'Informe um ID de WABA válido e um token.')
    token = token.strip()
    if not token:
        token = token_waba({'waba_id': waba_id})
    if not token:
        raise HTTPException(400, 'Informe o token para cadastrar esta WABA.')
    resultado = consultar_meta_waba(waba_id, token)
    if 'erro' in resultado or str(resultado.get('dados', {}).get('id')) != waba_id:
        raise HTTPException(400, 'Não foi possível validar a WABA. Confira ID, token e permissões.')
    nome = str(resultado['dados'].get('name', ''))[:250]
    with closing(get_db()) as conn, conn:
        conn.execute('INSERT INTO wabas_salvas(waba_id,access_token,nome,nome_consultado_em) VALUES(?,?,?,?) ON CONFLICT(waba_id) DO UPDATE SET access_token=excluded.access_token,nome=excluded.nome,nome_consultado_em=excluded.nome_consultado_em',
                     (waba_id, token, nome, datetime.now().timestamp()))
    return {'id': waba_id, 'nome': nome}


@app.get('/api/wabas-painel/{waba_id}')
def detalhes_waba_painel(waba_id: str):
    with closing(get_db()) as conn:
        row = conn.execute('SELECT * FROM wabas_salvas WHERE waba_id=?', (waba_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'WABA não cadastrada.')
    tarefas = [('', 'id,name,status,account_review_status,business_verification_status'),
               ('', 'health_status'),
               ('/phone_numbers', 'id,display_phone_number,verified_name,quality_rating,status,whatsapp_business_manager_messaging_limit'),
               ('/message_templates', 'id,name,status,category,language,components')]
    with ThreadPoolExecutor(max_workers=4) as pool:
        resultados = list(pool.map(lambda tarefa: consultar_meta_waba(waba_id, row['access_token'], *tarefa), tarefas))
    bloquear_numeros_waba(waba_id, resultados[1].get('dados', {}).get('health_status', {}))
    return JSONResponse({'id': waba_id, 'nome': row['nome'], 'consultado_em': agora_iso(),
                         'textos_fixos':textos_templates.listar(get_db,waba_id),
                         **dict(zip(['conta', 'saude', 'numeros', 'templates'], resultados))}, headers={'Cache-Control': 'no-store'})


@app.put('/api/wabas-painel/{waba_id}/textos-fixos')
def salvar_textos_template(waba_id: str, body: dict):
    return textos_templates.salvar(get_db,waba_id,body,consultar_meta_waba,oficiais_vendeai.estrutura)


@app.get('/api/wabas-painel/{waba_id}/textos-fixos')
def consultar_textos_template(waba_id: str):
    with closing(get_db()) as c:
        w=c.execute('SELECT access_token FROM wabas_salvas WHERE waba_id=?',(waba_id,)).fetchone()
    if not w:raise HTTPException(404,'WABA não cadastrada.')
    resposta=consultar_meta_waba(waba_id,w['access_token'],'/message_templates','id,name,language,status,category,components,parameter_format')
    if resposta.get('erro'):raise HTTPException(502,'Não foi possível carregar os templates. Tente buscar novamente.')
    return JSONResponse({'templates':resposta.get('dados',[]),'textos_fixos':textos_templates.listar(get_db,waba_id)},headers={'Cache-Control':'no-store'})


@app.get('/api/wabas-salvas')
def wabas_salvas():
    with closing(get_db()) as c:
        pendentes=[dict(r) for r in c.execute('SELECT * FROM wabas_salvas WHERE nome_consultado_em<?',(datetime.now().timestamp()-86400,))]
    if pendentes:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(atualizar_nome_waba,pendentes))
    with closing(get_db()) as c:
        rows=c.execute('SELECT waba_id,nome FROM wabas_salvas ORDER BY nome,waba_id').fetchall()
    return {'wabas':[r['waba_id'] for r in rows], 'detalhes':[{'id':r['waba_id'],'nome':r['nome']} for r in rows]}


@app.post("/api/buscar-templates")
async def buscar_templates(request: Request):
    body = await request.json()
    access_token = token_waba(body)
    waba_id = body.get("waba_id", "").strip()

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
                "idioma": t.get("language", "pt_BR"),
                "sem_variaveis": template_estatico(t),
                "header": next((c.get("format", "") for c in t.get("components", []) if c.get("type")=="HEADER"), ""),
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
    access_token = token_waba(body)
    waba_id = body.get("waba_id", "").strip()

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
    numeros = conn.execute("SELECT n.*, COALESCE(m.ativo,0) AS modelo_normal_ativo FROM numeros n LEFT JOIN modelos_normais m ON m.id=n.modelo_normal_id ORDER BY n.id").fetchall()
    historicos = {n["id"]: qualidade_historico.recentes(conn, n["id"]) for n in numeros}
    conn.close()
    return [
        {
            **dict(n),
            "historico_qualidade": historicos[n["id"]],
            "access_token": n["access_token"][:10] + "..." if n["access_token"] else "",
            "enviados_hoje": contagem_hoje(n["id"]),
            "enviados_hora": contagem_ultima_hora(n["id"]),
            "conexao_elegivel": conexoes_normais.elegivel(get_db,n) if conexoes_normais.normal(n) else None,
        }
        for n in numeros
    ]


def preparar_imagem_template(body, atual=None):
    atual=dict(atual or {})
    previsto={**atual,**body}
    header=atual.get('template_header','')
    chave=atual.get('template_imagem','')
    mudou=any(k in body and body[k]!=atual.get(k) for k in ('template_nome','template_idioma'))
    if mudou: chave=''
    if ('template_nome' in body or 'template_idioma' in body or 'imagem_base64' in body) and previsto.get('waba_id'):
        try:
            r=requests.get('https://graph.facebook.com/v21.0/'+requests.utils.quote(previsto['waba_id'],safe='')+'/message_templates',
                params={'name':previsto.get('template_nome','oferta_credito'),'limit':100},headers={'Authorization':'Bearer '+previsto['access_token']},timeout=(5,15),allow_redirects=False)
            if r.status_code!=200: raise ValueError()
            t=next((t for t in r.json().get('data',[]) if t.get('name')==previsto.get('template_nome','oferta_credito') and t.get('language')==previsto.get('template_idioma','pt_BR')),None)
            if not t: raise ValueError()
        except (requests.RequestException,ValueError,AttributeError):
            raise HTTPException(400,'Não foi possível confirmar o template na Meta. Busque os templates e selecione novamente.')
        header=next((c.get('format','') for c in t.get('components',[]) if c.get('type')=='HEADER'),'')
        body['template_sem_variaveis']=template_estatico(t)
        body['_template_status_confirmado']=t.get('status','UNKNOWN')
    elif mudou:
        header=''
    if body.get('imagem_base64'):
        if header!='IMAGE': raise HTTPException(400,'Selecione um template com cabeçalho de imagem na Meta.')
        chave=imagens_template.salvar(get_db,previsto,body['imagem_base64'],somente_local=previsto.get('via_envio')=='vendeai')
    if header=='IMAGE' and not chave:
        raise HTTPException(400,'Este template exige uma imagem. Anexe um arquivo JPEG ou PNG de até 5 MB.')
    return header,chave if header=='IMAGE' else ''


@app.post("/api/numeros")
async def adicionar_numero(request: Request):
    body = await request.json()
    if "template_sem_variaveis" in body and type(body["template_sem_variaveis"]) is not bool:
        raise HTTPException(400, "template_sem_variaveis deve ser booleano")
    body['waba_id']=body.get('waba_id','').strip()
    body['access_token']=token_waba(body)
    if not body['access_token']: raise HTTPException(400,'Informe o token para esta WABA.')
    with closing(get_db()) as consulta:
        if consulta.execute('SELECT 1 FROM numeros WHERE phone_number_id=?',(body['phone_number_id'],)).fetchone():
            raise HTTPException(409,'Este número já está cadastrado.')
    header,imagem=preparar_imagem_template(body)
    conn = get_db()
    conn.execute('BEGIN IMMEDIATE')
    if conn.execute('SELECT 1 FROM numeros WHERE phone_number_id=?',(body['phone_number_id'],)).fetchone():
        conn.rollback();conn.close()
        raise HTTPException(409,'Este número já está cadastrado.')
    cursor=conn.execute(
        "INSERT INTO numeros (nome, phone_number_id, access_token, template_nome, template_idioma, limite_diario, limite_hora, ativo, qualidade, waba_id, template_sem_variaveis) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'UNKNOWN', ?, ?)",
        (
            body["nome"], body["phone_number_id"], body["access_token"],
            body.get("template_nome", "oferta_credito"),
            body.get("template_idioma", "pt_BR"),
            body.get("limite_diario", 250),
            body.get("limite_hora", 20),
            body.get("waba_id", ""),
            int(body.get("template_sem_variaveis", False)),
        ),
    )
    conn.execute("UPDATE numeros SET template_header=?,template_imagem=? WHERE id=?",(header,imagem,cursor.lastrowid))
    if body['waba_id']:
        conn.execute('INSERT INTO wabas_salvas(waba_id,access_token) VALUES(?,?) ON CONFLICT(waba_id) DO UPDATE SET nome_consultado_em=CASE WHEN access_token!=excluded.access_token THEN 0 ELSE nome_consultado_em END,access_token=excluded.access_token',(body['waba_id'],body['access_token']))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.patch("/api/numeros/{numero_id}")
async def atualizar_numero(numero_id: int, request: Request):
    body = await request.json()
    body.pop("_template_status_confirmado",None)
    if "template_sem_variaveis" in body and type(body["template_sem_variaveis"]) is not bool:
        raise HTTPException(400, "template_sem_variaveis deve ser booleano")
    with closing(get_db()) as consulta:
        atual=consulta.execute('SELECT * FROM numeros WHERE id=?',(numero_id,)).fetchone()
    if not atual: raise HTTPException(404,'Número não encontrado')
    if gupshup.via(atual):
        if set(body) != {'ativo'} or type(body['ativo']) is not bool:
            raise HTTPException(400,'Edite esta conexão na aba Gupshup.')
        if body['ativo'] and not gupshup.elegivel(get_db,{**dict(atual),'ativo':1}):
            raise HTTPException(400,'Atualize o template e confira a conexão na aba Gupshup.')
        with closing(get_db()) as c,c:
            c.execute('UPDATE numeros SET ativo=? WHERE id=?',(int(body['ativo']),numero_id))
        return {'status':'ok'}
    if conexoes_normais.normal(atual):
        return await asyncio.to_thread(conexoes_normais.editar,get_db,atual,body)
    # Permite pausar mesmo quando a imagem ainda não foi configurada.
    imagem_config=None
    if any(k in body for k in ('template_nome','template_idioma','imagem_base64')):
        imagem_config=preparar_imagem_template(body,atual)
    if body.get('ativo') and atual['pausado_por_template'] and atual['template_status']!='APPROVED':
        raise HTTPException(400,'Template pausado pela Meta. Selecione outro aprovado ou atualize o status antes de reativar.')
    via=body.get('via_envio',atual['via_envio'])
    if via not in ('meta','vendeai'):raise HTTPException(400,'Caminho de envio inválido.')
    base_url=str(request.base_url).rstrip('/')
    if via=='vendeai' and body != {'ativo':False}:
        previsto={**dict(atual),**body}
        if imagem_config:previsto.update(template_header=imagem_config[0],template_imagem=imagem_config[1])
        t=await asyncio.to_thread(oficiais_vendeai.validar_via,get_db,previsto,carregar_configuracao(),VENDEAI_API_TOKEN,base_url)
        body['_template_status_confirmado']='APPROVED'
        body['template_sem_variaveis']=not bool(oficiais_vendeai.estrutura(t)[2])
    conn = get_db()
    conn.execute('UPDATE numeros SET via_envio=?,envio_base_url=? WHERE id=?',(via,base_url if via=='vendeai' else atual['envio_base_url'],numero_id))
    if body.get('ativo'):
        conn.execute('UPDATE numeros SET pausado_por_template=0 WHERE id=?',(numero_id,))
    if "ativo" in body:
        # Toggle manual (usuario) - sempre limpa a flag de pausa automatica: se o
        # usuario esta mexendo aqui, o numero passa a ser controlado manualmente,
        # seja ativando ou pausando de proposito.
        conn.execute("UPDATE numeros SET ativo = ?, pausado_por_qualidade = 0 WHERE id = ?", (int(body["ativo"]), numero_id))
    if "limite_diario" in body:
        conn.execute("UPDATE numeros SET limite_diario = ? WHERE id = ?", (body["limite_diario"], numero_id))
    if "limite_hora" in body:
        conn.execute("UPDATE numeros SET limite_hora = ? WHERE id = ?", (body["limite_hora"], numero_id))
    if "template_nome" in body and "template_sem_variaveis" not in body:
        conn.execute("UPDATE numeros SET template_sem_variaveis = 0 WHERE id = ? AND template_nome != ?", (numero_id, body["template_nome"]))
    if "template_nome" in body:
        conn.execute("UPDATE numeros SET template_nome = ? WHERE id = ?", (body["template_nome"], numero_id))
    if "template_sem_variaveis" in body:
        conn.execute("UPDATE numeros SET template_sem_variaveis = ? WHERE id = ?", (int(body["template_sem_variaveis"]), numero_id))
    if "template_idioma" in body:
        conn.execute("UPDATE numeros SET template_idioma = ? WHERE id = ?", (body["template_idioma"], numero_id))
    if body.get('_template_status_confirmado'):
        conn.execute('UPDATE numeros SET template_status=? WHERE id=?',(body['_template_status_confirmado'],numero_id))
        if body['_template_status_confirmado']=='APPROVED':
            conn.execute('UPDATE numeros SET pausado_por_template=0 WHERE id=?',(numero_id,))
    if imagem_config:
        conn.execute("UPDATE numeros SET template_header=?,template_imagem=? WHERE id=?",(*imagem_config,numero_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.patch('/api/numeros/{numero_id}/observacao')
async def salvar_observacao_numero(numero_id: int, request: Request):
    body = await request.json()
    texto = body.get('observacao') if isinstance(body, dict) else None
    if not isinstance(texto, str) or len(texto) > 5000:
        raise HTTPException(400, 'Informe um texto de até 5.000 caracteres.')
    with closing(get_db()) as conn, conn:
        r = conn.execute('UPDATE numeros SET observacao=? WHERE id=?', (texto, numero_id))
        if not r.rowcount:
            raise HTTPException(404, 'Número não encontrado')
    return {'status': 'ok'}


@app.delete("/api/numeros/{numero_id}")
def remover_numero(numero_id: int):
    conn = get_db()
    conn.execute("DELETE FROM numeros WHERE id = ?", (numero_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/historico")
def historico(limit: int = 100, resposta: str = "todos", canal: str = 'todos'):
    """
    resposta filtra por classificacao: 'todos' (padrao), 'positivo',
    'negativo', ou 'sem_resposta' (disparos que ainda nao tem
    resposta_lead classificada).
    """
    if resposta not in ("todos", "positivo", "negativo", "sem_resposta"):
        raise HTTPException(400, "resposta deve ser 'todos', 'positivo', 'negativo' ou 'sem_resposta'")

    if canal not in ('todos','sms','whatsapp'):raise HTTPException(400,'Canal inválido.')
    conn = get_db()
    base_query = """SELECT d.*, COALESCE(n.nome, 'SMS') as numero_nome,c.nome as campanha_sms_nome FROM disparos d
           LEFT JOIN numeros n ON n.id = d.numero_id LEFT JOIN campanhas_sms c ON c.id=d.campanha_sms_id"""
    filtros=[];params=[]
    if canal!='todos':filtros.append('d.canal=?');params.append(canal)
    if resposta=='sem_resposta':filtros.append('d.resposta_lead IS NULL')
    elif resposta!='todos':filtros.append('d.resposta_lead=?');params.append(resposta)
    rows=conn.execute(base_query+(' WHERE '+' AND '.join(filtros) if filtros else '')+' ORDER BY d.id DESC LIMIT ?',(*params,max(1,min(limit,1000)))).fetchall()

    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/exportar-respostas")
def exportar_respostas(tipo: str = "todos"):
    """
    Exporta em CSV os disparos filtrados por resposta_lead. tipo pode ser
    'positivo', 'negativo' ou 'todos' (todos os que tem alguma resposta
    classificada, positiva ou negativa - nao inclui os sem resposta).
    """
    if tipo not in ("positivo", "negativo", "todos"):
        raise HTTPException(400, "tipo deve ser 'positivo', 'negativo' ou 'todos'")

    conn = get_db()
    if tipo == "todos":
        rows = conn.execute(
            """SELECT d.*, COALESCE(n.nome, 'SMS (sem numero WhatsApp)') as numero_nome FROM disparos d
               LEFT JOIN numeros n ON n.id = d.numero_id
               WHERE d.resposta_lead IS NOT NULL
               ORDER BY d.resposta_recebida_em DESC""",
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT d.*, COALESCE(n.nome, 'SMS (sem numero WhatsApp)') as numero_nome FROM disparos d
               LEFT JOIN numeros n ON n.id = d.numero_id
               WHERE d.resposta_lead = ?
               ORDER BY d.resposta_recebida_em DESC""",
            (tipo,),
        ).fetchall()
    conn.close()

    saida = io.StringIO()
    escritor = csv.writer(saida, delimiter=";")
    escritor.writerow(["telefone", "nome_lead", "cpf", "numero_usado", "canal", "status_envio", "resposta", "disparado_em", "resposta_em", "link_conversa"])
    for r in rows:
        escritor.writerow([
            r["telefone_destino"], r["nome_lead"] or "", r["cpf"] or "",
            r["numero_nome"], r["canal"], r["status"],
            r["resposta_lead"] or "", r["criado_em"], r["resposta_recebida_em"] or "",
            r["link_conversa"] or "",
        ])

    nome_arquivo = f"respostas_{tipo}_{datetime.now(TZ).strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        content=saida.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


@app.get("/api/eventos")
def eventos(limit: int = 50):
    conn = get_db()
    rows = conn.execute("SELECT * FROM eventos ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/", response_class=HTMLResponse)
def painel(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


def obter_fila():
    return fila_disparos.Fila(get_db, DB_FILE, capacidade_admissao)


async def loop_fila():
    while not app.state.fila_parar:
        try:
            await asyncio.to_thread(obter_fila().processar, carregar_configuracao,
                                    cadastrar_base_vendeai, executar_disparo_preparado,
                                    dentro_do_horario_permitido, lambda: app.state.fila_parar, impedimento_fila)
        except Exception as erro:
            registrar_evento(None, "fila_worker_erro", type(erro).__name__)
        await asyncio.sleep(2)


@app.on_event("shutdown")
async def parar_fila():
    app.state.fila_parar = True
    if getattr(app.state,'sms_task',None):await app.state.sms_task
    if getattr(app.state,'lembretes_sms_task',None):await app.state.lembretes_sms_task
    if getattr(app.state,"etiquetas_task",None): app.state.etiquetas_task.cancel()
    for task in getattr(app.state,"optout_tasks",[]): task.cancel()
    task = getattr(app.state, "fila_task", None)
    if task:
        await task


@app.get("/api/fila")
def listar_fila(limit: int = 50):
    cfg = carregar_configuracao()
    return {**obter_fila().resumo(limit), "habilitada": cfg["fila_habilitada"],
            "integracao_habilitada": cfg["vendeai_habilitado"], "intervalo_segundos": cfg["fila_intervalo_segundos"],
            "horario_permitido": dentro_do_horario_permitido()[0]}


@app.get("/api/fila/{identificador}")
def consultar_fila(identificador: str):
    return obter_fila().consultar(identificador)


@app.post("/api/fila/{identificador}/cancelar")
def cancelar_fila(identificador: str):
    return obter_fila().cancelar(identificador)


def exigir_chave_transbordo(request):
    if not API_KEY or not secrets.compare_digest(request.headers.get('X-API-Key',''),API_KEY):
        raise HTTPException(401,'Chave API obrigatoria')


def disponibilidade_transbordo():
    cfg=carregar_configuracao()
    if not cfg['vendeai_habilitado'] or not cfg['fila_habilitada'] or not cfg['vendeai_account_id'] or not (cfg['vendeai_mailing_token'] or VENDEAI_API_TOKEN):
        return {'disponivel':False,'motivo_codigo':'destino_nao_configurado'}
    if estado_lote()['lote']:
        return {'disponivel':False,'motivo_codigo':'lote_em_andamento'}
    impedimento=impedimento_entrada()
    return {'disponivel':not bool(impedimento),'motivo_codigo':impedimento['motivo_codigo'] if impedimento else None}


@app.get('/api/transbordo/disponibilidade')
def consultar_disponibilidade_transbordo(request: Request):
    exigir_chave_transbordo(request)
    return disponibilidade_transbordo()


@app.post('/api/transbordo/receber')
async def receber_transbordo(request: Request):
    exigir_chave_transbordo(request)
    body=await request.json()
    if not isinstance(body,dict) or not isinstance(body.get('id'),str) or not re.fullmatch('[0-9a-f]{32}',body['id']) or not isinstance(body.get('lead'),dict):
        raise HTTPException(400,'Recibo e lead invalidos')
    tentativa=transbordo.registrar_recebido(get_db,body['id'],body['lead'])
    def recusar(motivo, codigo=409):
        transbordo.finalizar_recebido(get_db,tentativa,'recusado',motivo)
        return JSONResponse({'status':'erro','aceito':False,'recibo':body['id'],'motivo_codigo':motivo},status_code=codigo)
    try:
        with fila_disparos.trava_worker(str(DB_FILE)+'.entrada') as obtida:
            if not obtida:
                return recusar('entrada_em_andamento')
            cfg=carregar_configuracao()
            lead=transbordo.dados_lead(body['lead'])
            preparado=vendeai_mailing.preparar_lead(lead,cfg)
            if optout.bloqueado(get_db,preparado['mailing']['phone']):
                return recusar('optout')
            transbordo.validar_lead(preparado['mailing'])
            chave='transbordo:'+body['id']
            existente=obter_fila().existente(preparado,lead,cfg,chave)
            if existente:
                transbordo.finalizar_recebido(get_db,tentativa,'aceito',fila_id=existente['fila_id'])
                return {**existente,'recibo':body['id']}
            disponibilidade=disponibilidade_transbordo()
            if not disponibilidade['disponivel']:
                return recusar(disponibilidade['motivo_codigo'])
            if variaveis_vazias_impedem(preparado['variaveis_template']):
                return recusar('variaveis_incompativeis',400)
            item=obter_fila().enfileirar([(preparado,lead,'individual',[],chave)],cfg)[0]
            transbordo.finalizar_recebido(get_db,tentativa,'aceito',fila_id=item['fila_id'])
            return JSONResponse({**item,'recibo':body['id']},status_code=202)
    except HTTPException as exc:
        transbordo.finalizar_recebido(get_db,tentativa,'recusado',str(exc.detail)[:200])
        raise
    except Exception:
        transbordo.finalizar_recebido(get_db,tentativa,'incerto','falha_no_processamento')
        raise


@app.get('/api/transbordos/recebidos')
def listar_transbordos_recebidos():
    return {'itens':transbordo.listar_recebidos(get_db)}


@app.get('/api/transbordo/recebidos/{recibo}')
def recibo_transbordo(recibo: str,request: Request):
    exigir_chave_transbordo(request)
    with closing(get_db()) as c:
        row=c.execute('SELECT id FROM fila_disparos WHERE chave=?',('exp:transbordo:'+recibo,)).fetchone()
    if not row:
        raise HTTPException(404,'Recibo nao encontrado')
    item=obter_fila().consultar(row['id'])
    return {'recibo':recibo,'status':item['status']}


@app.get('/api/transbordos')
def listar_transbordos():
    return {'itens':transbordo.Transbordo(get_db).listar()}


@app.get('/api/transbordo/{id}')
async def consultar_transbordo(id: str):
    return await asyncio.to_thread(transbordo.Transbordo(get_db).consultar_destino,id,carregar_configuracao())


def testar_destino_transbordo(cfg):
    if not cfg['transbordo_url'] or not cfg['transbordo_token']:
        return {'conectado':False,'disponivel':False,'motivo_codigo':'configuracao_incompleta'}
    try:
        r=requests.get(cfg['transbordo_url']+'/api/transbordo/disponibilidade',headers={'X-API-Key':cfg['transbordo_token']},timeout=(5,10),allow_redirects=False)
        if r.status_code==200:
            d=r.json()
            if type(d.get('disponivel')) is bool:
                return {'conectado':True,'disponivel':d['disponivel'],'motivo_codigo':d.get('motivo_codigo')}
        return {'conectado':False,'disponivel':False,'motivo_codigo':'destino_http_'+str(r.status_code)}
    except (requests.RequestException,ValueError,AttributeError):
        return {'conectado':False,'disponivel':False,'motivo_codigo':'falha_comunicacao'}


@app.post('/api/transbordo/testar')
async def testar_transbordo():
    return await asyncio.to_thread(testar_destino_transbordo,carregar_configuracao())


@app.get('/api/disponibilidade-consulta')
def disponibilidade_consulta():
    resultado=pode_disparar()
    permitido=resultado['pode_disparar'] is True
    canal=resultado.get('canal_provavel')
    cfg=carregar_configuracao()
    motivos={
        'lote_em_andamento':'Disparo por planilha em andamento no painel de origem.',
        'fora_do_horario':'Fora do horário permitido no painel de origem.',
        'nenhum_numero_disponivel':'Sem capacidade local e sem encaminhamento disponível.',
        'transbordo_indisponivel':'Sem capacidade local e o destino não confirmou disponibilidade.'}
    return JSONResponse({
        'consultar_nova_vida':permitido,
        'pode_disparar':permitido,
        'destino_provavel':('transbordo' if canal=='transbordo' else 'local') if permitido else None,
        'nome_destino':(cfg['transbordo_nome'] or 'Painel de destino') if canal=='transbordo' else ('Seu painel' if permitido else None),
        'canal_provavel':canal,
        'motivo_codigo':resultado.get('motivo_codigo'),
        'motivo':'Há capacidade neste momento; pode seguir com a consulta.' if permitido else motivos.get(resultado.get('motivo_codigo'),resultado.get('motivo')),
        'destino_consultado':resultado.get('destino_consultado',False),
        'motivo_destino':resultado.get('motivo_destino'),
        'capacidade_reservada':False,
        'observacao':'Disponibilidade momentânea. A capacidade será conferida novamente ao receber o lead.'
    },headers={'Cache-Control':'no-store'})


@app.get('/api/numeros/{numero_id}/imagem-template')
def visualizar_imagem_template(numero_id: int):
    with closing(get_db()) as c:
        r=c.execute('SELECT i.dados,i.mime FROM numeros n JOIN imagens_template i ON i.id=n.template_imagem WHERE n.id=?',(numero_id,)).fetchone()
    if not r: raise HTTPException(404,'Imagem não encontrada')
    return Response(content=r['dados'],media_type=r['mime'],headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@app.get('/conexoes', response_class=HTMLResponse)
def pagina_conexoes(request: Request):
    return templates.TemplateResponse(request,'conexoes.html',{})


@app.get('/api/conexoes-normais')
def listar_conexoes_normais():
    cfg=carregar_configuracao()
    with closing(get_db()) as c:
        return {'padrao':conexoes_normais.padrao(get_db,cfg),'conexoes':[dict(r) for r in c.execute('SELECT * FROM conexoes_vendeai WHERE conta=? ORDER BY nome',(str(cfg['vendeai_account_id']),))],
                'modelos':[dict(r) for r in c.execute('SELECT * FROM modelos_normais ORDER BY nome')],
                'numeros':[{**dict(r),'conexao_elegivel':conexoes_normais.elegivel(get_db,r)} for r in c.execute("SELECT id,nome,vendeai_conta,vendeai_inbox_id,modelo_normal_id,limite_diario,limite_hora,ativo,conexao_status,meta_consultado_em FROM numeros WHERE tipo_envio='vendeai_normal'")]}


@app.post('/api/conexoes-normais/atualizar')
def descobrir_conexoes():
    total=conexoes_normais.descobrir(get_db,carregar_configuracao(),VENDEAI_API_TOKEN)
    with closing(get_db()) as c:
        numeros=c.execute("SELECT * FROM numeros WHERE tipo_envio='vendeai_normal'").fetchall()
    for n in numeros: verificar_qualidade(n)
    return {'total':total}


@app.post('/api/conexoes-normais/importar')
async def importar_conexao(request: Request):
    body=await request.json()
    if not isinstance(body,dict): raise HTTPException(400,'Envie um objeto.')
    return await asyncio.to_thread(conexoes_normais.importar,get_db,carregar_configuracao(),VENDEAI_API_TOKEN,body)


@app.patch('/api/conexoes-normais/padrao')
def salvar_padrao_normal(body: dict):
    return conexoes_normais.salvar_padrao(get_db,carregar_configuracao(),body)


@app.post('/api/conexoes-normais/importar-todas')
def importar_todas_normais():
    return conexoes_normais.importar_todas(get_db,carregar_configuracao(),VENDEAI_API_TOKEN)


@app.post('/api/modelos-normais')
async def criar_modelo_normal(request: Request):
    body=await request.json()
    if not isinstance(body,dict): raise HTTPException(400,'Envie um objeto.')
    return conexoes_normais.salvar_modelo(get_db,body)


@app.patch('/api/modelos-normais/{modelo_id}')
async def editar_modelo_normal(modelo_id: int, request: Request):
    body=await request.json()
    if not isinstance(body,dict): raise HTTPException(400,'Envie um objeto.')
    return conexoes_normais.salvar_modelo(get_db,body,modelo_id)


@app.get('/api/modelos-normais/{modelo_id}/imagem')
def imagem_modelo_normal(modelo_id: int):
    with closing(get_db()) as c:
        r=c.execute("SELECT i.dados,i.mime FROM modelos_normais m JOIN imagens_template i ON i.id=m.imagem AND i.phone_id='modelo-normal' WHERE m.id=?",(modelo_id,)).fetchone()
    if not r:raise HTTPException(404,'Imagem não encontrada.')
    return Response(content=r['dados'],media_type=r['mime'],headers={'Cache-Control':'private, no-store'})


@app.get('/oficiais-vendeai',response_class=HTMLResponse)
def pagina_oficiais_vendeai(request: Request):
    return templates.TemplateResponse(request,'oficiais_vendeai.html',{})


@app.get('/api/oficiais-vendeai')
def listar_oficiais_vendeai():
    return oficiais_vendeai.listar(get_db,carregar_configuracao())


@app.post('/api/oficiais-vendeai/atualizar')
def descobrir_oficiais_vendeai():
    return {'total':oficiais_vendeai.descobrir(get_db,carregar_configuracao(),VENDEAI_API_TOKEN)}


@app.post('/api/oficiais-vendeai/vincular')
async def vincular_oficial_vendeai(request: Request):
    body=await request.json()
    if not isinstance(body,dict):raise HTTPException(400,'Envie um objeto.')
    return await asyncio.to_thread(oficiais_vendeai.vincular,get_db,carregar_configuracao(),VENDEAI_API_TOKEN,body)


@app.get('/midia-vendeai/{token}')
def midia_publica_vendeai(token: str):
    row=oficiais_vendeai.midia(get_db,token)
    return Response(content=row['dados'],media_type=row['mime'],headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@app.get('/gupshup', response_class=HTMLResponse)
def pagina_gupshup(request: Request):
    return templates.TemplateResponse(request,'gupshup.html',{})

@app.get('/api/gupshup')
def listar_gupshup():
    return gupshup.listar(get_db)

@app.post('/api/gupshup')
def salvar_gupshup(body: dict):
    return gupshup.salvar(get_db,body)

@app.post('/api/gupshup/{app_id}/atualizar')
def atualizar_gupshup(app_id: str):
    return gupshup.atualizar(get_db,app_id)

@app.put('/api/gupshup/{app_id}')
def configurar_gupshup(app_id: str,body: dict):
    return gupshup.configurar(get_db,app_id,body)


@app.get('/api/gupshup/{app_id}/webhook')
def webhook_gupshup_config(app_id: str):
    with closing(get_db()) as c:
        a=c.execute('SELECT webhook_token FROM gup_apps WHERE app_id=?',(app_id,)).fetchone()
    if not a:raise HTTPException(404,'Aplicativo não encontrado.')
    return JSONResponse({'path':'/webhook/gupshup/'+app_id+'?chave='+a['webhook_token']},headers={'Cache-Control':'no-store'})

@app.post('/webhook/gupshup/{app_id}')
async def webhook_gupshup(app_id: str,request: Request):
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>1000000:raise HTTPException(413,'Evento acima de 1 MB.')
    return await asyncio.to_thread(gupshup.receber,get_db,app_id,request.query_params.get('chave',''),bytes(raw))
