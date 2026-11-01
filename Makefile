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
	@echo "make test-frozen   same, against a snapshot of HEAD, so you can keep editing while it runs"
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

# The test services bind-mount the source they import, so pytest reads whatever is on
# disk *now*. With the default mount (the live working tree) an edit or a branch switch
# mid-run changes the imports half-way through: the run keeps printing dots, slows to a
# crawl as imports miss, and its pass/fail means nothing. That happened three times in
# one session, twice misread as resource contention.
#
# `_tree-stable` catches only the easy half -- starting dirty. It cannot catch an edit
# made *during* a run, which is the case that actually bit. So for anything long enough
# to want to keep working through, use `test-frozen`: it runs the suite against a
# detached git worktree of HEAD, an immutable snapshot, leaving the real tree free to
# edit. `ARGS` and `RUNNER` (test | test2) work the same as on `test`.
.PHONY: _tree-stable test-frozen
_tree-stable:
	@if [ -z "$(DIRTY)" ] && [ -n "$$(git status --porcelain 2>/dev/null)" ]; then \
		echo "make: the working tree has uncommitted changes."; \
		echo "      The test container mounts it live, so an edit while this runs invalidates the result."; \
		echo "      Options: commit or stash; or \`make test-frozen\` to test HEAD from a snapshot"; \
		echo "      and keep editing; or DIRTY=1 to run anyway and leave the tree alone."; \
		exit 1; \
	fi

FROZEN_DIR ?= $(CURDIR)/var/frozen
RUNNER ?= test

test-frozen: env
	@rm -rf "$(FROZEN_DIR)"
	@git worktree prune
	@git worktree add --detach --quiet "$(FROZEN_DIR)" HEAD
	@cp -a .env .env.docker "$(FROZEN_DIR)/" 2>/dev/null || true
	@echo "make: testing $$(git rev-parse --short HEAD) from a frozen worktree; the real tree is yours to edit."
	@SRC_DIR="$(FROZEN_DIR)" $(COMPOSE) run --rm $(RUNNER) $(ARGS); \
		status=$$?; git worktree remove --force "$(FROZEN_DIR)" 2>/dev/null || rm -rf "$(FROZEN_DIR)"; \
		git worktree prune; exit $$status

test: env _tree-stable
	$(COMPOSE) run --rm test $(ARGS)

# A second runner on its own database, so two suites can run at once. pytest names its
# database after DB_NAME, so `test` and `test2` never contend for `test_flarize_docker`.
test2: env _tree-stable
	$(COMPOSE) run --rm test2 $(ARGS)

lint: env
	$(COMPOSE) run --rm --no-deps api sh -c "black --check . && isort --check-only . && flake8 && python manage.py makemigrations --check --dry-run && lint-imports"
