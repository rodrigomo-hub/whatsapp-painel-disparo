"""HistÃ³rico de mudanÃ§as observadas, sem presumir datas anteriores Ã  coleta."""
def migrar(c):
    c.execute("""CREATE TABLE IF NOT EXISTS qualidade_historico (
        id INTEGER PRIMARY KEY, numero_id INTEGER NOT NULL,
        anterior TEXT, atual TEXT, detectado_em TEXT NOT NULL)""")
    cols = {r[1] for r in c.execute('PRAGMA table_info(qualidade_historico)')}
    if 'fonte' not in cols:
        c.execute("ALTER TABLE qualidade_historico ADD COLUMN fonte TEXT NOT NULL DEFAULT 'consulta'")
    if 'intervalo_inicio' not in cols:
        c.execute('ALTER TABLE qualidade_historico ADD COLUMN intervalo_inicio TEXT')
    c.execute('CREATE INDEX IF NOT EXISTS idx_qualidade_numero ON qualidade_historico(numero_id,id DESC)')
    c.execute("""CREATE TRIGGER IF NOT EXISTS registrar_mudanca_qualidade
        AFTER UPDATE OF qualidade ON numeros
        WHEN OLD.qualidade IS NOT NEW.qualidade AND NEW.tipo_envio != 'vendeai_normal'
        BEGIN
            INSERT INTO qualidade_historico(numero_id,anterior,atual,detectado_em)
            VALUES(NEW.id,OLD.qualidade,NEW.qualidade,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        END""")


def recentes(c, numero_id):
    return [dict(r) for r in c.execute(
        'SELECT anterior,atual,detectado_em,fonte,intervalo_inicio FROM qualidade_historico WHERE numero_id=? ORDER BY julianday(detectado_em) DESC,id DESC LIMIT 5',
        (numero_id,))]


def resumo_dia(c, dia):
    """Conta numeros distintos por direcao, somente entre qualidades conhecidas."""
    row=c.execute("""WITH mudancas AS (
        SELECT numero_id,
          CASE anterior WHEN 'RED' THEN 1 WHEN 'YELLOW' THEN 2 WHEN 'GREEN' THEN 3 END AS antes,
          CASE atual WHEN 'RED' THEN 1 WHEN 'YELLOW' THEN 2 WHEN 'GREEN' THEN 3 END AS depois
        FROM qualidade_historico
        WHERE date(detectado_em,'-3 hours')=?
    ) SELECT COUNT(DISTINCT CASE WHEN depois>antes THEN numero_id END),
             COUNT(DISTINCT CASE WHEN depois<antes THEN numero_id END)
      FROM mudancas""",(dia,)).fetchone()
    return {'subiram':row[0],'baixaram':row[1]}
