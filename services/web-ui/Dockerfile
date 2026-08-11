FROM python:3.11-slim

WORKDIR /app

COPY libs/common /opt/sakura-common
RUN pip install --no-cache-dir /opt/sakura-common

COPY services/web-ui/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/web-ui/webui_service /app/webui_service

EXPOSE 8000
CMD ["uvicorn", "webui_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
