"""Histórico de mudanças observadas, sem presumir datas anteriores à coleta."""
def migrar(c):
    c.execute("""CREATE TABLE IF NOT EXISTS qualidade_historico (
        id INTEGER PRIMARY KEY, numero_id INTEGER NOT NULL,
        anterior TEXT, atual TEXT, detectado_em TEXT NOT NULL)""")
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
        'SELECT anterior,atual,detectado_em FROM qualidade_historico WHERE numero_id=? ORDER BY id DESC LIMIT 5',
        (numero_id,))]
