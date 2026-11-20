FROM python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
RUN apk add --no-cache --upgrade "krb5-libs>=1.22.3-r0" "zlib>=1.4-r0"
LABEL org.opencontainers.image.title="library-reader-obsidian"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN printf '%s\n' 'pip==26.3 --hash=sha256:f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13' | python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /dev/stdin
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock \
    && python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip
COPY server.py config.py access.py reader.py ./
COPY tests/*.py ./tests/
RUN chmod 0555 /app /app/tests && chmod 0444 /app/*.py /app/tests/*.py /app/requirements.lock
USER 10001:10001
RUN python -c "import server"
CMD ["python", "/app/server.py"]
