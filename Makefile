.PHONY: dev lint test test-full test-distributed test-external test-slow test-fast migrate verify-evidence

dev:
	docker compose -f deploy/docker-compose.dev.yml up -d

lint:
	ruff check .
	mypy packages control-plane execution-gateway security adapters

test:
	pytest -q -m "not external and not distributed and not slow"

test-full:
	pytest -q

test-distributed:
	pytest -q -m "distributed"

test-external:
	pytest -q -m "external"

test-slow:
	pytest -q -m "slow"

test-fast:
	pytest -q -m "not external and not distributed and not slow"

verify-evidence:
	python scripts/verify-evidence-tiers.py --check-only

verify-evidence-full:
	python scripts/verify-evidence-tiers.py

migrate:
	alembic upgrade head

scaffold-verify:
	find . -type f | sort | head -n 200
