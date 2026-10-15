# Local Docker stack shortcuts (deploy/docker-compose.dev.yml). `make help` lists them.
export LOCAL_UID := $(shell id -u)
export LOCAL_GID := $(shell id -g)
# The root .env sets COMPOSE_FILE and the compose-only knobs, so this is the same as plain `docker compose` in the root.
COMPOSE := docker compose
ARGS ?= -q

.PHONY: help env build up down logs logs-setup ps shell manage test lint reset

help:
	@echo "make up            build and start db, redis, setup (migrate + seed), api :8000, worker, beat"
	@echo "make down          stop the stack (data kept)"
	@echo "make reset         stop and DELETE the dev database volume"
	@echo "make logs          follow api/worker/beat logs;  make logs-setup  shows migrate/seed output + admin link"
	@echo "make test          run pytest in a container;    make test ARGS=\"catalog -q -x\""
	@echo "make lint          black --check, isort --check, flake8, makemigrations --check, lint-imports"
	@echo "make manage CMD=…  run a manage.py command;      make shell  opens a bash shell in the api image"

# First run: create the two local env files from their templates; .env.docker gets a random DB password.
env: .env .env.docker

.env:
	cp .env.compose.example .env

.env.docker:
	sed "s/^DB_PASSWORD=.*/DB_PASSWORD=$$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')/" .env.docker.example > .env.docker
	chmod 600 .env.docker

build: env
	$(COMPOSE) build

up: env
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps

down: env
	$(COMPOSE) down

reset: env
	$(COMPOSE) --profile test down -v

logs: env
	$(COMPOSE) logs -f api worker beat

logs-setup: env
	$(COMPOSE) logs setup

ps: env
	$(COMPOSE) ps

shell: env
	$(COMPOSE) run --rm --no-deps api bash

manage: env
	$(COMPOSE) run --rm api python manage.py $(CMD)

# The test services bind-mount the working tree (`..:/app`), so pytest imports whatever
# is on disk *now* -- not what was there when the run started. Editing a file or switching
# branches mid-run therefore corrupts the run silently: it keeps printing dots, slows to a
# crawl as imports miss, and its pass/fail means nothing. Both have happened. The snapshot
# below is not a style check: a dirty tree is the one state in which a long run cannot be
# trusted, so the runner says so up front instead of handing back a green that is noise.
# `make test DIRTY=1` runs anyway, for the ordinary case of testing work in progress that
# you then leave alone until it finishes.
.PHONY: _tree-stable
_tree-stable:
	@if [ -z "$(DIRTY)" ] && [ -n "$$(git status --porcelain 2>/dev/null)" ]; then \
		echo "make: the working tree has uncommitted changes."; \
		echo "      The test container mounts it live, so any edit while this runs invalidates the result."; \
		echo "      Commit or stash first, or pass DIRTY=1 and then leave the tree alone until it finishes."; \
		exit 1; \
	fi

test: env _tree-stable
	$(COMPOSE) run --rm test $(ARGS)

# A second runner on its own database, so two suites can run at once. pytest names its
# database after DB_NAME, so `test` and `test2` never contend for `test_flarize_docker`.
test2: env _tree-stable
	$(COMPOSE) run --rm test2 $(ARGS)

lint: env
	$(COMPOSE) run --rm --no-deps api sh -c "black --check . && isort --check-only . && flake8 && python manage.py makemigrations --check --dry-run && lint-imports"
