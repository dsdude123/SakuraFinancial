FROM python:3.11-slim

WORKDIR /app

COPY libs/common /opt/sakura-common
RUN pip install --no-cache-dir /opt/sakura-common

COPY services/stocks/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/stocks/stocks_service /app/stocks_service

EXPOSE 8003
CMD ["uvicorn", "stocks_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8003"]
