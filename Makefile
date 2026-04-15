.PHONY: install start test lint db-start db-stop

install:
	python -m venv .venv
	. .venv/bin/activate && pip install -e ".[dev]"
	cd web && npm install 2>/dev/null || echo "web/ not yet built — skipping npm install"

start: db-start
	. .venv/bin/activate && python -m agent.start

test:
	. .venv/bin/activate && pytest agent/tests/ -v

lint:
	. .venv/bin/activate && ruff check agent/ && black --check agent/

db-start:
	docker-compose up -d postgres

db-stop:
	docker-compose down
