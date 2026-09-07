.PHONY: dev lint test test-fast test-integration test-full test-distributed test-external test-slow migrate verify-evidence verify-evidence-full scaffold-verify

dev:
	docker compose -f deploy/docker-compose.dev.yml up -d

lint:
	ruff check .
	mypy packages control-plane execution-gateway security adapters

test-fast:
	pytest -q -m "unit"

test:
	pytest -q -m "unit or subsystem"

test-integration:
	pytest -q -m "integration"

test-full:
	pytest -q

test-distributed:
	pytest -q -m "distributed"

test-external:
	pytest -q -m "external"

test-slow:
	pytest -q -m "slow"

verify-evidence:
	python scripts/verify-evidence-tiers.py --check-only

verify-evidence-full:
	python scripts/verify-evidence-tiers.py

migrate:
	alembic upgrade head

scaffold-verify:
	find . -type f | sort | head -n 200
