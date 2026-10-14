# The census web UI (ethocensus-web), v2 of the metadata server.
# Build from the repository root:
#   docker compose -f Docker/census-web/docker-compose.yml build
FROM python:3.12-slim
LABEL maintainer="Giorgio Gilestro <giorgio@gilest.ro>"

WORKDIR /app
COPY setup.py README.md ./
COPY ethoscope_metadata_db ./ethoscope_metadata_db
RUN pip install --no-cache-dir ".[web]"

# Reason: run as the owner of the census folder (uid/gid set in the compose file).
USER 1000:1000
EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/health', timeout=4)"
CMD ["ethocensus-web", "--host", "0.0.0.0", "--port", "8095"]
