FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY vendeai_mailing.py .
COPY fila_disparos.py .
COPY planilhas.py .
COPY transbordo.py .
COPY imagens_template.py .
COPY templates_lote.py .
COPY vendeai_eventos.py .
COPY resultados.py .
COPY optout.py .
COPY conexoes_normais.py .
COPY oficiais_vendeai.py .
COPY templates ./templates

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:80/login', timeout=2)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--proxy-headers", "--forwarded-allow-ips=*"]
