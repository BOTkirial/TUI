.PHONY: up down build http sql docker logs

# docker exec et non « compose exec » : sa séquence de détachement par défaut
# (ctrl-p,ctrl-q) retient chaque ctrl+p jusqu'à la touche suivante (palette).
RUN = docker exec -it --detach-keys=ctrl-^,ctrl-^ $$(docker compose ps -q tui)

up:            ## démarre le conteneur TUI
	docker compose up -d

down:          ## arrête tout
	docker compose down

build:         ## (re)construit l'image des TUI
	docker compose build

http: up       ## ouvre le client HTTP
	$(RUN) http-tui

sql: up        ## ouvre le client SQL
	$(RUN) sql-tui

docker: up     ## ouvre le client Docker
	$(RUN) docker-tui

logs:          ## logs du conteneur TUI
	docker compose logs -f tui
