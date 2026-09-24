FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data/media \
    && chown -R app:app /app
USER 10001:10001

EXPOSE 8000
# RUN_MODE selects the process layout; migrations are handled inside the
# supervisor so they always precede whichever processes start:
#   web     uvicorn only (compose default)
#   worker  publishing worker only
#   all     both in one container, for hosts whose disks attach to one service
ENV RUN_MODE=web
CMD ["python", "-m", "backend.supervisor"]
