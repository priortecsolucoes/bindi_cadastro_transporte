# Bindi - Cadastro - imagem para rodar no Railway como serviço contínuo
# (o próprio main.py repete o ciclo a cada SCHEDULE_INTERVAL_MINUTES).
# Selenium >= 4.6 resolve o chromedriver sozinho (Selenium Manager); a imagem só
# precisa do Google Chrome. Tag fixa para não pegar outra base Debian sem aviso.
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation \
    && wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    # SEM --no-install-recommends aqui: o .deb do Chrome declara bibliotecas de
    # runtime (libgbm, libnss3, libasound2...) como "Recommends"; sem elas o
    # Chrome/chromedriver morre na inicialização (status 127).
    && apt-get install -y /tmp/google-chrome.deb \
    && rm /tmp/google-chrome.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Sem display no container - headless é obrigatório.
ENV CHROME_HEADLESS=1
ENV PYTHONUNBUFFERED=1
# Estado que precisa sobreviver a deploys/restarts: monte um Volume do Railway em /data.
# Sem ele o histórico de FINALIZADOS some e as viagens seriam cadastradas de novo na Vibra.
ENV PROGRAMACOES_OUTPUT_PATH=/data/programacoes_vibra.csv
ENV VIBRA_COOKIES_PATH=/data/vibra_cookies.json
ENV CHROME_USER_DATA_DIR=/data/chrome_profile

CMD ["python", "main.py"]
