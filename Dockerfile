FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    HOME=/data XDG_DATA_HOME=/data/.local/share FASTMCP_HOME=/data/.fastmcp \
    FASTMCP_ENABLE_RICH_TRACEBACKS=0 FASTMCP_LOG_LEVEL=ERROR \
    READER_CONFIG=/config/config.json READER_DATA=/data
WORKDIR /app
COPY pyproject.toml ./
COPY telegram_reader ./telegram_reader
RUN pip install --no-cache-dir . && \
    useradd --uid 10001 --create-home reader && \
    mkdir -p /data /config && chown 10001:10001 /data /config
LABEL io.mikhail.app="telegram-reader-mcp"
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "-m", "telegram_reader"]
CMD ["serve"]
