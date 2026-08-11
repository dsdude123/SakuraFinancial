FROM python:3.11-slim

# tesseract provides OCR for photographed/scanned receipts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY libs/common /opt/sakura-common
RUN pip install --no-cache-dir /opt/sakura-common

COPY services/receipts/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/receipts/receipts_service /app/receipts_service

EXPOSE 8004
CMD ["uvicorn", "receipts_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8004"]
