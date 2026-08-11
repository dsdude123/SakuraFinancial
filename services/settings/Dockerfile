FROM python:3.11-slim

WORKDIR /app

COPY libs/common /opt/sakura-common
RUN pip install --no-cache-dir /opt/sakura-common

COPY services/settings/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/settings/settings_service /app/settings_service

EXPOSE 8005
CMD ["uvicorn", "settings_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8005"]
