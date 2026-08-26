.PHONY: dev lint test migrate

dev:
	docker compose -f deploy/docker-compose.dev.yml up -d

lint:
	ruff check .
	mypy packages control-plane execution-gateway security adapters

test:
	pytest -q

migrate:
	alembic upgrade head

scaffold-verify:
	find . -type f | sort | head -n 200
