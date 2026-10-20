.PHONY: up down build http sql docker logs

up:            ## démarre le conteneur TUI
	docker compose up -d

down:          ## arrête tout
	docker compose down

build:         ## (re)construit l'image des TUI
	docker compose build

http: up       ## ouvre le client HTTP
	docker compose exec tui http-tui

sql: up        ## ouvre le client SQL
	docker compose exec tui sql-tui

docker: up     ## ouvre le client Docker
	docker compose exec tui docker-tui

logs:          ## logs du conteneur TUI
	docker compose logs -f tui
