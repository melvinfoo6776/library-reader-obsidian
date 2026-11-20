FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
LABEL org.opencontainers.image.title="library-reader-obsidian"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock
COPY server.py config.py access.py reader.py ./
COPY tests/*.py ./tests/
RUN chmod 0555 /app /app/tests && chmod 0444 /app/*.py /app/tests/*.py /app/requirements.lock
USER 10001:10001
RUN python -c "import server"
CMD ["python", "/app/server.py"]
