"""Fila persistente. Cadastro em lote e envio somente depois da confirmacao."""
import hashlib
import json
import os
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from fastapi import HTTPException

ATIVOS = ("aguardando_cadastro", "cadastrando", "pronto", "enviando")
CONFIG_COLUMNS = {
    "fila_habilitada": "INTEGER NOT NULL DEFAULT 0",
    "fila_intervalo_segundos": "INTEGER NOT NULL DEFAULT 300",
}


def migrar(conn):
    colunas = {r["name"] for r in conn.execute("PRAGMA table_info(configuracao)")}
    for campo, tipo in CONFIG_COLUMNS.items():
        if campo not in colunas:
            conn.execute(f"ALTER TABLE configuracao ADD COLUMN {campo} {tipo}")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fila_disparos (
            id TEXT PRIMARY KEY, chave TEXT NOT NULL, fingerprint TEXT NOT NULL,
            destino TEXT NOT NULL, payload TEXT NOT NULL, origem TEXT NOT NULL,
            numeros TEXT NOT NULL, status TEXT NOT NULL,
            criado_em REAL NOT NULL, atualizado_em REAL NOT NULL,
            tentar_em REAL NOT NULL DEFAULT 0, cadastro_em REAL NOT NULL DEFAULT 0,
            tentativas INTEGER NOT NULL DEFAULT 0,
            cadastro TEXT NOT NULL DEFAULT '{}', resultado TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_fila_chave ON fila_disparos(chave, criado_em);
        CREATE INDEX IF NOT EXISTS idx_fila_status ON fila_disparos(status, tentar_em, criado_em);
        CREATE TABLE IF NOT EXISTS fila_controle (
            id INTEGER PRIMARY KEY CHECK(id=1), proxima_rodada REAL NOT NULL DEFAULT 0,
            bloqueado_ate REAL NOT NULL DEFAULT 0, ultimo_erro TEXT NOT NULL DEFAULT ''
        );
        INSERT OR IGNORE INTO fila_controle(id) VALUES(1);
    """)


def destino(cfg):
    return cfg["vendeai_mailing_url"].rstrip("/") + "|" + cfg["vendeai_account_id"]


def serializar(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@contextmanager
def trava_worker(db_path):
    """Exclusao entre processos/containers no mesmo volume; liberada pelo SO ao morrer."""
    caminho = Path(str(db_path) + ".fila.lock")
    with caminho.open("a+b") as arquivo:
        if os.name == "nt":
            import msvcrt
            if arquivo.tell() == 0:
                arquivo.write(b"0")
                arquivo.flush()
            arquivo.seek(0)
            try:
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                arquivo.seek(0)
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(arquivo, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(arquivo, fcntl.LOCK_UN)


class Fila:
    def __init__(self, get_db, db_path):
        self.get_db = get_db
        self.db_path = db_path

    @staticmethod
    def publico(row, controle=None):
        payload = json.loads(row["payload"])
        cadastro = json.loads(row["cadastro"])
        resultado = json.loads(row["resultado"])
        resposta = {
            "id": row["id"], "fila_id": row["id"], "status": row["status"],
            "telefone": payload["mailing"]["phone"], "nome": payload["mailing"].get("name", ""),
            "criado_em": row["criado_em"], "atualizado_em": row["atualizado_em"],
            "tentativas_cadastro": row["tentativas"], "vendeai": cadastro or {"status": "aguardando"},
            "resultado": resultado,
            "consultar_em": f"/api/fila/{row['id']}",
        }
        if row["status"] == "aguardando_cadastro" and controle:
            resposta["tentar_em"] = max(row["tentar_em"], controle["proxima_rodada"], controle["bloqueado_ate"])
        else:
            resposta["tentar_em"] = row["tentar_em"] or None
        return resposta

    def enfileirar(self, itens, cfg):
        """Transacao unica; retries mantem o email gerado e nao criam novos envios."""
        agora = time.time()
        saida = []
        with closing(self.get_db()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            for preparado, original, origem, numeros, chave_explicita in itens:
                if chave_explicita is not None and (not isinstance(chave_explicita, str) or not 1 <= len(chave_explicita.strip()) <= 200):
                    raise HTTPException(400, "idempotency_key deve ter de 1 a 200 caracteres")
                base = json.loads(serializar(preparado))
                # Email sintetico nao participa da identidade: um retry nao deve mudar o cadastro.
                if not (original.get("email") or "").strip() and not ((original.get("info_extra") or {}).get("email") or "").strip():
                    base["mailing"].pop("email", None)
                fingerprint = hashlib.sha256(serializar([destino(cfg), base, origem, sorted(numeros)]).encode()).hexdigest()
                chave = "exp:" + chave_explicita.strip() if chave_explicita else "auto:" + fingerprint
                row = conn.execute(
                    "SELECT * FROM fila_disparos WHERE chave=? AND (criado_em>=? OR chave LIKE 'exp:%' OR status IN ('aguardando_cadastro','cadastrando','pronto','enviando','incerto')) ORDER BY criado_em DESC LIMIT 1",
                    (chave, agora - 86400),
                ).fetchone()
                if row and row["fingerprint"] != fingerprint:
                    raise HTTPException(409, "idempotency_key ja usada com dados diferentes")
                repetido = row is not None
                if not row:
                    identificador = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO fila_disparos(id,chave,fingerprint,destino,payload,origem,numeros,status,criado_em,atualizado_em) VALUES(?,?,?,?,?,?,?,'aguardando_cadastro',?,?)",
                        (identificador, chave, fingerprint, destino(cfg), serializar(preparado), origem, serializar(numeros), agora, agora),
                    )
                    row = conn.execute("SELECT * FROM fila_disparos WHERE id=?", (identificador,)).fetchone()
                saida.append((dict(row), repetido))
            controle = conn.execute("SELECT * FROM fila_controle WHERE id=1").fetchone()
            if not controle["proxima_rodada"]:
                conn.execute("UPDATE fila_controle SET proxima_rodada=? WHERE id=1", (agora + cfg["fila_intervalo_segundos"],))
            controle = conn.execute("SELECT * FROM fila_controle WHERE id=1").fetchone()
            return [{**self.publico(row, controle), "duplicado": repetido,
                     "status": "enfileirado" if row["status"] in ATIVOS else row["status"],
                     "estado_fila": row["status"]} for row, repetido in saida]

    def consultar(self, identificador):
        with closing(self.get_db()) as conn:
            row = conn.execute("SELECT * FROM fila_disparos WHERE id=?", (identificador,)).fetchone()
            if not row:
                raise HTTPException(404, "Item da fila nao encontrado")
            controle = conn.execute("SELECT * FROM fila_controle WHERE id=1").fetchone()
            return self.publico(row, controle)

    def resumo(self, limit=50):
        with closing(self.get_db()) as conn:
            controle = dict(conn.execute("SELECT * FROM fila_controle WHERE id=1").fetchone())
            contagens = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM fila_disparos GROUP BY status")}
            rows = conn.execute("SELECT * FROM fila_disparos ORDER BY criado_em DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
            return {"contagens": contagens, **controle, "itens": [self.publico(r, controle) for r in rows]}

    def cancelar(self, identificador):
        with closing(self.get_db()) as conn, conn:
            cur = conn.execute("UPDATE fila_disparos SET status='cancelado',atualizado_em=? WHERE id=? AND status IN ('aguardando_cadastro','pronto')", (time.time(), identificador))
            if not cur.rowcount:
                raise HTTPException(409, "Item em processamento ou ja finalizado; nao pode ser cancelado")
        return self.consultar(identificador)

    def alterar(self, identificador, **campos):
        campos["atualizado_em"] = time.time()
        with closing(self.get_db()) as conn, conn:
            conn.execute(f"UPDATE fila_disparos SET {','.join(k+'=?' for k in campos)} WHERE id=?", [*campos.values(), identificador])

    def processar(self, carregar_cfg, cadastrar, enviar, horario, parar=lambda: False):
        with trava_worker(self.db_path) as obtida:
            if not obtida:
                return
            # Com o lock exclusivo, itens em transito so podem ter vindo de um processo encerrado.
            with closing(self.get_db()) as conn, conn:
                conn.execute("UPDATE fila_disparos SET status='aguardando_cadastro',atualizado_em=? WHERE status='cadastrando'", (time.time(),))
                conn.execute("UPDATE fila_disparos SET status='incerto',atualizado_em=?,resultado=? WHERE status='enviando'", (time.time(), serializar({"motivo": "Processo interrompido durante envio. Conferir historico antes de reenviar."})))
            cfg = carregar_cfg()
            if not cfg["vendeai_habilitado"] or parar():
                return
            # Nao consumir cadastro/validade fora da janela de envio.
            if not horario()[0]:
                return
            self.cadastrar_rodada(cfg, cadastrar)
            inicio = time.monotonic()
            for _ in range(20):
                if parar() or time.monotonic() - inicio > 10:
                    break
                cfg = carregar_cfg()
                if not cfg["vendeai_habilitado"] or not horario()[0]:
                    break
                agora = time.time()
                with closing(self.get_db()) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute("SELECT * FROM fila_disparos WHERE status='pronto' AND tentar_em<=? AND destino=? ORDER BY criado_em LIMIT 1", (agora, destino(cfg))).fetchone()
                    if not row:
                        break
                    if row["cadastro_em"] <= agora - 7 * 86400:
                        conn.execute("UPDATE fila_disparos SET status='aguardando_cadastro',tentar_em=0 WHERE id=?", (row["id"],))
                        continue
                    conn.execute("UPDATE fila_disparos SET status='enviando',atualizado_em=? WHERE id=?", (agora, row["id"]))
                try:
                    resultado = enviar(json.loads(row["payload"]), json.loads(row["cadastro"]), row["origem"], json.loads(row["numeros"]))
                except Exception:
                    # Envio e efeito externo: repetir apos excecao/crash pode duplicar mensagem.
                    self.alterar(row["id"], status="incerto", resultado=serializar({"motivo": "Falha durante envio. Conferir historico antes de reenviar."}))
                    continue
                if resultado.get("motivo_codigo") in ("nenhum_numero_disponivel", "fora_do_horario"):
                    self.alterar(row["id"], status="pronto", tentar_em=time.time() + 30, resultado=serializar(resultado))
                else:
                    self.alterar(row["id"], status="sucesso" if resultado.get("status") == "sucesso" else "erro", tentar_em=0, resultado=serializar(resultado))

    def cadastrar_rodada(self, cfg, cadastrar):
        agora = time.time()
        intervalo = cfg["fila_intervalo_segundos"]
        with closing(self.get_db()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            controle = conn.execute("SELECT * FROM fila_controle WHERE id=1").fetchone()
            if max(controle["proxima_rodada"], controle["bloqueado_ate"]) > agora:
                return
            rows = conn.execute("SELECT * FROM fila_disparos WHERE status='aguardando_cadastro' AND tentar_em<=? AND destino=? ORDER BY criado_em LIMIT 20000", (agora, destino(cfg))).fetchall()
            if not rows:
                conn.execute("UPDATE fila_controle SET proxima_rodada=0 WHERE id=1")
                return
            # API faz upsert por telefone. Nao misturar campanhas diferentes do mesmo telefone no mesmo lote.
            selecionados = []
            telefones = {json.loads(r["payload"])["mailing"]["phone"] for r in conn.execute("SELECT payload FROM fila_disparos WHERE status IN ('pronto','enviando')")}
            for row in rows:
                phone = json.loads(row["payload"])["mailing"]["phone"]
                if phone not in telefones:
                    telefones.add(phone)
                    selecionados.append(row)
            if not selecionados:
                return
            for row in selecionados:
                conn.execute("UPDATE fila_disparos SET status='cadastrando',tentativas=tentativas+1,atualizado_em=? WHERE id=?", (agora, row["id"]))
            conn.execute("UPDATE fila_controle SET proxima_rodada=? WHERE id=1", (agora + intervalo,))
        try:
            resultados = cadastrar([json.loads(r["payload"])["mailing"] for r in selecionados], cfg)
            if len(resultados) != len(selecionados):
                raise ValueError("Resposta sem todos os itens")
        except Exception:
            resultados = [{"status": "erro", "motivo": "FALHA_COMUNICACAO"} for _ in selecionados]
        agora = time.time()
        pausa = 0
        motivo_pausa = ""
        with closing(self.get_db()) as conn, conn:
            for row, resultado in zip(selecionados, resultados):
                if resultado.get("status") == "cadastrado":
                    conn.execute("UPDATE fila_disparos SET status='pronto',cadastro=?,cadastro_em=?,tentar_em=0,atualizado_em=? WHERE id=?", (serializar(resultado), agora, agora, row["id"]))
                    continue
                motivo = resultado.get("motivo", "RESPOSTA_INVALIDA")
                permanente = motivo in {"INVALID_CPF", "INVALID_PHONE", "INVALID_EMAIL", "INVALID_BIRTH_DATE", "MISSING_PHONE", "MISSING_PRODUCT", "LEAD_REJEITADO", "HTTP_400", "HTTP_404", "HTTP_422"}
                espera = max(intervalo, int(resultado.get("tentar_apos_segundos", intervalo)))
                conn.execute("UPDATE fila_disparos SET status=?,cadastro=?,tentar_em=?,atualizado_em=? WHERE id=?", ("erro" if permanente else "aguardando_cadastro", serializar(resultado), 0 if permanente else agora + espera, agora, row["id"]))
                if not permanente:
                    pausa = max(pausa, agora + espera)
                    motivo_pausa = motivo
            conn.execute("UPDATE fila_controle SET bloqueado_ate=?,ultimo_erro=? WHERE id=1", (pausa, motivo_pausa))
