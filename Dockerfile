# Barrier Lens web app. ffmpeg normalises clips; the face detector (YuNet, pinned hash) is fetched at build time so
# a live check never waits for it. Keys come from the host's environment, never from the image.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
COPY data data
RUN python -c "from app import blur; print(blur.ensure_model())"
ENV PORT=8000
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
