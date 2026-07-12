FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY tradingbot_v7000 ./tradingbot_v7000
COPY config.v7000.yaml ./config.v7000.yaml
COPY requirements-v7000-patch.txt ./requirements-v7000-patch.txt

RUN pip install --no-cache-dir -r requirements-v7000-patch.txt || true

EXPOSE 80

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
