"""Área isolada do supervisor Milone.

O CRM nunca é exposto ao navegador. Toda conversa é filtrada no servidor antes
de entrar no cache e antes de qualquer ação.
"""
import asyncio
import bcrypt
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

TZ = ZoneInfo("America/Sao_Paulo")
CRM = "https://crm.vendeaitecnologia.com.br/api/v1/accounts/"
CACHE_TTL = 60
_cache = {"atualizado": 0, "conversas": [], "agentes": {}, "inboxes": {}}
_lock = threading.Lock()
_sessoes = set()


def _ids(nome):
    return {int(x) for x in os.getenv(nome, "").split(",") if x.strip().isdigit()}


def agentes_milone():
    return _ids("MILONE_AGENT_IDS")


def somente_leitura():
    return os.getenv("SUPERVISOR_READ_ONLY", "true").strip().lower() not in ("0", "false", "nao", "não")


def migrar(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS supervisor_milone_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        criado_em TEXT NOT NULL, usuario TEXT NOT NULL, acao TEXT NOT NULL,
        conversa_id INTEGER NOT NULL, destino TEXT NOT NULL DEFAULT '', detalhe TEXT NOT NULL DEFAULT ''
    )''')


def _normalizar(v):
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def _crm(cfg, token, caminho, params=None):
    conta = str(cfg.get("vendeai_account_id") or "33393")
    r = requests.get(CRM + conta + caminho, headers={"api_access_token": token}, params=params,
                     timeout=(5, 25), allow_redirects=False)
    if r.status_code in (429, 500, 502, 503, 504):
        time.sleep(0.8)
        r = requests.get(CRM + conta + caminho, headers={"api_access_token": token}, params=params,
                         timeout=(5, 25), allow_redirects=False)
    if r.status_code != 200:
        raise RuntimeError("CRM HTTP " + str(r.status_code))
    return r.json()


def _lista(dados):
    if isinstance(dados, list): return dados
    if not isinstance(dados, dict): return []
    # O white-label devolve conversas em {"data": {"meta": ..., "payload": [...]}}
    # enquanto outros endpoints podem devolver diretamente uma destas listas.
    for k in ("payload", "conversations", "messages", "data"):
        if isinstance(dados.get(k), list): return dados[k]
    if isinstance(dados.get("data"), dict):
        return _lista(dados["data"])
    return []


def _paginas_conversas(cfg, token, status):
    """Busca todas as páginas em paralelo depois de conhecer o total da 1ª."""
    primeira = _crm(cfg, token, "/conversations", {"status": status, "assignee_type": "all", "page": 1})
    itens = _lista(primeira)
    dados = primeira.get("data") if isinstance(primeira, dict) else {}
    meta = dados.get("meta") if isinstance(dados, dict) else {}
    try:
        total = int((meta or {}).get("all_count") or len(itens))
    except (TypeError, ValueError):
        total = len(itens)
    paginas = max(1, (total + 24) // 25)
    if paginas == 1:
        return itens
    with ThreadPoolExecutor(max_workers=6) as ex:
        futuros = [ex.submit(_crm, cfg, token, "/conversations", {"status": status, "assignee_type": "all", "page": p}) for p in range(2, paginas + 1)]
        for futuro in as_completed(futuros):
            itens.extend(_lista(futuro.result()))
    return itens


def _assignee(conversa):
    meta = conversa.get("meta") or {}
    a = meta.get("assignee") or conversa.get("assignee")
    if not a: return None
    if isinstance(a, dict): return a
    return {"id": a}


def _visivel(conversa):
    a = _assignee(conversa)
    return a is None or int(a.get("id") or 0) in agentes_milone()


def _mensagens(cfg, token, ident):
    return _lista(_crm(cfg, token, "/conversations/" + str(ident) + "/messages"))


def _mensagens_estaveis(cfg, token, ident):
    ultimo = None
    for tentativa in range(3):
        try:
            return _mensagens(cfg, token, ident)
        except Exception as exc:
            ultimo = exc
            time.sleep(0.7 * (tentativa + 1))
    raise ultimo


def _inteiro(valor, padrao=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return padrao


def _momento(valor, padrao):
    """Aceita timestamp Unix e data ISO que aparecem misturados no CRM."""
    try:
        return float(valor)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return padrao


def _publicas(mensagens):
    return [m for m in mensagens if not m.get("private") and _inteiro(m.get("message_type", 2), 2) in (0, 1)]


def _texto(m):
    if not isinstance(m, dict):
        return ""
    text = (m.get("content") or "").strip()
    if text: return text
    a = m.get("attachments") or []
    if a: return "[mídia]"
    return ""


def _calc(conversa, mensagens, agentes):
    publicas = sorted(_publicas(mensagens), key=lambda m: _inteiro(m.get("id")), reverse=True)
    clientes = [m for m in publicas if _inteiro(m.get("message_type", 2), 2) == 0]
    ultima_cliente = clientes[0] if clientes else None
    ultima = publicas[0] if publicas else None
    agora = time.time()
    horario_cliente = _momento((ultima_cliente or {}).get("created_at"), agora)
    espera = int(agora - horario_cliente) if ultima_cliente and ultima is ultima_cliente else None
    janela = int(horario_cliente + 86400 - agora) if ultima_cliente else None
    seguidas = 0
    for m in publicas:
        if _inteiro(m.get("message_type", 2), 2) != 0: break
        seguidas += 1
    ultimas_cliente = clientes[:3]
    textos = " ".join(_texto(m) for m in ultimas_cliente).lower()
    dados = bool(re.search(r"ag[êe]ncia|conta|pix|chave|cpf|banco|nubank|caixa|ita[úu]|bradesco|santander|autorizad|c[oó]digo|\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", textos))
    risco = bool(re.search(r"cancel|golpe|procon|reclame|reclama[cç][aã]o|advogado|den[úu]ncia|n[aã]o autorizei|estorno", textos))
    etiquetas = conversa.get("labels") or []
    nomes_etiquetas = {_normalizar(e.get("title") if isinstance(e, dict) else e) for e in etiquetas}
    ticket_alto = "ticketalto" in nomes_etiquetas
    a = _assignee(conversa)
    aid = int((a or {}).get("id") or 0)
    contato_vendedora = any(_inteiro(m.get("message_type", 2), 2) == 1 and _inteiro((m.get("sender") or {}).get("id")) == aid for m in publicas) if aid else False
    sender = (conversa.get("meta") or {}).get("sender") or {}
    return {
        "id": conversa.get("id"), "nome": sender.get("name") or "Sem nome",
        "telefone": sender.get("phone_number") or "", "inbox_id": conversa.get("inbox_id"),
        "assignee_id": aid or None, "assignee_nome": (a or {}).get("name") or "Sem atribuição",
        "espera_segundos": espera, "janela_segundos": janela, "sem_resposta": seguidas,
        "dados_enviados": dados, "risco": risco, "ticket_alto": ticket_alto, "ultima_mensagem": _texto(ultima_cliente)[:60],
        "sem_contato": bool(aid and not contato_vendedora), "can_reply": bool(conversa.get("can_reply", True)),
        "last_activity_at": conversa.get("last_activity_at"), "_mensagens": mensagens[-40:]
    }


def atualizar(get_cfg, token):
    cfg = get_cfg()
    if not token or not agentes_milone():
        raise RuntimeError("Configure token e IDs Milone no servidor")
    conversas = []
    # Os dois estados também são independentes; assim a primeira abertura não
    # fica esperando uma sequência longa de páginas do CRM.
    with ThreadPoolExecutor(max_workers=2) as ex:
        futuros = [ex.submit(_paginas_conversas, cfg, token, status) for status in ("open", "pending")]
        for futuro in as_completed(futuros):
            conversas.extend(x for x in futuro.result() if _visivel(x) and x.get("can_reply", True))
    agentes = {int(a.get("id")): a for a in _lista(_crm(cfg, token, "/agents")) if a.get("id")}
    inboxes = {int(i.get("id")): i for i in _lista(_crm(cfg, token, "/inboxes")) if i.get("id")}
    # O resumo da conversa contém só a última mensagem. Para alertas, janela
    # de 24h e mensagens sequenciais corretos, a grade é calculada pelo
    # histórico completo. As buscas ocorrem em paralelo, sem alterar o CRM.
    resultado = []
    falhas_historico = 0
    # Quatro chamadas simultâneas é o ritmo validado sem falhas pelo CRM
    # white-label. Cada histórico ainda possui três tentativas graduais.
    with ThreadPoolExecutor(max_workers=4) as ex:
        futuros = {ex.submit(_mensagens_estaveis, cfg, token, c["id"]): c for c in conversas}
        for futuro in as_completed(futuros):
            c = futuros[futuro]
            try:
                item = _calc(c, futuro.result(), agentes)
                if item["can_reply"]:
                    resultado.append(item)
            except Exception:
                # Uma tentativa final, fora do pico paralelo, recupera falhas
                # transitórias do CRM (429/5xx) sem esconder a conversa.
                try:
                    item = _calc(c, _mensagens_estaveis(cfg, token, c["id"]), agentes)
                    if item["can_reply"]:
                        resultado.append(item)
                except Exception:
                    falhas_historico += 1
    # Nunca troque uma visão íntegra por outra parcial. A visão já existente
    # no cache continua sendo usada até o CRM responder todos os históricos.
    if falhas_historico:
        raise RuntimeError(f"CRM não retornou {falhas_historico} histórico(s)")
    resultado.sort(key=lambda x: (not x["risco"], not x["dados_enviados"], -(x["sem_resposta"] >= 3), -(x["espera_segundos"] or 0), x["janela_segundos"] or 999999))
    with _lock:
        _cache.update({"atualizado": time.time(), "conversas": resultado, "agentes": agentes, "inboxes": inboxes})
    return _cache


def cache(get_cfg, token):
    if time.time() - _cache["atualizado"] > CACHE_TTL:
        atualizar(get_cfg, token)
    return _cache


def _autenticado(request):
    return request.cookies.get("supervisor_session") in _sessoes


def _exigir(request):
    if not _autenticado(request): raise HTTPException(401, "Não autenticado")


def registrar_rotas(app, templates, get_cfg, get_db, token):
    @app.get("/supervisor")
    def pagina(request: Request):
        if not _autenticado(request): return RedirectResponse("/supervisor/login")
        return templates.TemplateResponse(request, "supervisor_milone.html", {"somente_leitura": somente_leitura()})

    @app.get("/supervisor/login")
    def login_tela(request: Request):
        if _autenticado(request): return RedirectResponse("/supervisor")
        return templates.TemplateResponse(request, "supervisor_login.html", {"erro": ""})

    @app.post("/supervisor/login")
    async def login(request: Request):
        form = await request.form()
        usuario = str(form.get("usuario") or "")
        senha = str(form.get("senha") or "")
        correto = os.getenv("SUPERVISOR_USER", "")
        hash_ = os.getenv("SUPERVISOR_PASSWORD_HASH", "")
        if not correto or not hash_ or usuario != correto or not bcrypt.checkpw(senha.encode(), hash_.encode()):
            return templates.TemplateResponse(request, "supervisor_login.html", {"erro": "Usuário ou senha inválidos."}, status_code=401)
        sessao = secrets.token_urlsafe(32); _sessoes.add(sessao)
        r = RedirectResponse("/supervisor", status_code=303)
        r.set_cookie("supervisor_session", sessao, httponly=True, samesite="strict", secure=True, max_age=28800)
        return r

    @app.post("/supervisor/sair")
    def sair(request: Request):
        _sessoes.discard(request.cookies.get("supervisor_session", ""))
        r = RedirectResponse("/supervisor/login", status_code=303); r.delete_cookie("supervisor_session"); return r

    @app.get("/api/supervisor/painel")
    def painel(request: Request):
        _exigir(request)
        d = cache(get_cfg, token)
        cards = {"sem_atribuicao": {"abertas": 0, "esperando": 0, "sem_contato": 0, "ticket_alto": 0}}
        for aid in agentes_milone(): cards[str(aid)] = {"abertas": 0, "esperando": 0, "sem_contato": 0, "ticket_alto": 0, "nome": (d["agentes"].get(aid) or {}).get("name", "Vendedora"), "status": (d["agentes"].get(aid) or {}).get("availability_status", "offline")}
        saida=[]
        for x in d["conversas"]:
            chave = str(x["assignee_id"]) if x["assignee_id"] else "sem_atribuicao"; card=cards.get(chave)
            if card:
                card["abertas"] += 1; card["esperando"] += bool(x["espera_segundos"] is not None); card["sem_contato"] += bool(x["sem_contato"]); card["ticket_alto"] += bool(x["ticket_alto"])
            saida.append({k:v for k,v in x.items() if k != "_mensagens"})
        return {"atualizado_em": d["atualizado"], "somente_leitura": somente_leitura(), "cards": cards, "conversas": saida}

    @app.get("/api/supervisor/conversas/{ident}")
    def conversa(ident: int, request: Request):
        _exigir(request); d=cache(get_cfg, token); item=next((x for x in d["conversas"] if int(x["id"])==ident),None)
        if not item: raise HTTPException(404, "Conversa não autorizada ou não está aberta")
        # Histórico sob demanda: evita dezenas de chamadas ao CRM na carga da
        # grade e só expõe mensagens depois da conversa ter passado no filtro.
        try:
            item = dict(item)
            item["_mensagens"] = _mensagens(get_cfg(), token, ident)[-80:]
        except Exception:
            pass
        return {"conversa": item, "inbox": (d["inboxes"].get(int(item["inbox_id"] or 0)) or {}).get("name", "")}

    @app.post("/api/supervisor/conversas/{ident}/atribuir")
    async def atribuir(ident: int, request: Request):
        _exigir(request)
        if somente_leitura(): raise HTTPException(403, "Modo de teste: atribuição desabilitada")
        raise HTTPException(501, "Ative as ações depois de validar o modo de teste")

    @app.post("/api/supervisor/conversas/{ident}/nota")
    async def nota(ident: int, request: Request):
        _exigir(request)
        if somente_leitura(): raise HTTPException(403, "Modo de teste: notas desabilitadas")
        raise HTTPException(501, "Ative as ações depois de validar o modo de teste")
