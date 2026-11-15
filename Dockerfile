FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Tesseract provides OCR for scanned bills and photographed receipts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN DJANGO_SECRET_KEY=build-only-static-collection-key python manage.py collectstatic --noinput \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app app \
    && mkdir -p /app/media \
    && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --timeout 180"]
