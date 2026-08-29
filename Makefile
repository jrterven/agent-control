.PHONY: bootstrap dev migrate api mock tunnels test test-remote-readonly test-remote-control-dev-mutations build prototype

bootstrap:
	python3 -m venv .venv
	.venv/bin/python -m pip install -e packages/hermes-client -e 'apps/api[test]' -e 'apps/mock-hermes[test]'
	npm install

dev:
	npm run dev

migrate:
	.venv/bin/alembic -c apps/api/alembic.ini upgrade head

api: migrate
	.venv/bin/hermes-control-api

mock:
	.venv/bin/mock-hermes --dashboard-port 19119 --api-port 18642

tunnels:
	scripts/tunnels/hermes-tunnels.sh run

test:
	.venv/bin/python -m pytest tests/backend tests/mock
	npm test
	npm run prototype:check

test-remote-readonly:
	@test "$${HERMES_REMOTE_TESTS}" = "1" || (echo "Set HERMES_REMOTE_TESTS=1 explicitly" && exit 2)
	.venv/bin/python -m pytest -q tests/remote/test_remote_readonly.py

test-remote-control-dev-mutations:
	@test "$${HERMES_REMOTE_TESTS}" = "1" || (echo "Set HERMES_REMOTE_TESTS=1 explicitly" && exit 2)
	@test "$${HERMES_REMOTE_MUTATION_TESTS}" = "1" || (echo "Set HERMES_REMOTE_MUTATION_TESTS=1 explicitly" && exit 2)
	@test "$${HERMES_REMOTE_MUTATIONS}" = "I_UNDERSTAND_CONTROL_DEV_ONLY" || (echo "Set the exact HERMES_REMOTE_MUTATIONS sentinel" && exit 2)
	@test "$${HERMES_TEST_PROFILE}" = "control-dev" || (echo "Set HERMES_TEST_PROFILE=control-dev exactly" && exit 2)
	.venv/bin/python -m pytest -q tests/remote/test_remote_control_dev_mutations.py

build:
	npm run build
	npm run prototype:build

prototype:
	npm run prototype:dev
