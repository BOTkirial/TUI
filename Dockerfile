# Image unique contenant les TUI (commandes : http-tui / sql-tui / docker-tui)
FROM python:3.12-slim

# CLI docker + plugin compose (binaires statiques) pour docker-tui ;
# le démon est celui de l'hôte, via le socket monté par docker-compose.yml
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /app
COPY pyproject.toml ./
COPY common/ ./common/
COPY http-client/httptui/ ./http-client/httptui/
COPY sql-client/sqltui/ ./sql-client/sqltui/
COPY docker-client/dockertui/ ./docker-client/dockertui/
RUN pip install --no-cache-dir .

# dossier de travail partagé avec l'hôte (monté par docker-compose)
WORKDIR /work
ENV TERM=xterm-256color
# le conteneur reste vivant ; on lance les TUI via `docker compose exec`
CMD ["sleep", "infinity"]
