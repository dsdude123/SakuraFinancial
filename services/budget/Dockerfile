FROM python:3.11-slim

WORKDIR /app

COPY libs/common /opt/sakura-common
RUN pip install --no-cache-dir /opt/sakura-common

COPY services/budget/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/budget/budget_service /app/budget_service

EXPOSE 8002
CMD ["uvicorn", "budget_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8002"]
