.PHONY: dev lint test migrate verify-evidence

dev:
	docker compose -f deploy/docker-compose.dev.yml up -d

lint:
	ruff check .
	mypy packages control-plane execution-gateway security adapters

test:
	pytest -q

verify-evidence:
	python scripts/verify-evidence-tiers.py --check-only

verify-evidence-full:
	python scripts/verify-evidence-tiers.py

migrate:
	alembic upgrade head

scaffold-verify:
	find . -type f | sort | head -n 200
