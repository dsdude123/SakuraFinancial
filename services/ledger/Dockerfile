FROM python:3.11-slim

WORKDIR /app

COPY libs/common /opt/sakura-common
RUN pip install --no-cache-dir /opt/sakura-common

COPY services/ledger/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/ledger/ledger_service /app/ledger_service

EXPOSE 8001
CMD ["uvicorn", "ledger_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8001"]
