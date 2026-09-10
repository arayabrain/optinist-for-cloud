#
# optinist Makefile
#

############################## For Testing ##############################

define rm_unused_docker_containers
	docker ps -a --filter "status=exited" --filter "name=$(1)" --format "{{.ID}}" | xargs --no-run-if-empty docker rm
endef

define cleanup_test_env
	docker compose -f docker-compose.test.yml down
	docker compose -f docker-compose.test.yml rm -f
	@$(call rm_unused_docker_containers, $(1))
endef

define run_test_service
	docker compose -f docker-compose.test.yml build $(1)
	docker compose -f docker-compose.test.yml run $(1) $(2)
endef

PYTEST = poetry run pytest -s

.PHONY: test_run_all
test_run_all:
	# cleanup
	@$(call cleanup_test_env, test_studio_backend)
	@$(call cleanup_test_env, test_studio_frontend)
	# build containers once (performance optimization)
	docker compose -f docker-compose.test.yml build test_studio_backend
	docker compose -f docker-compose.test.yml build test_studio_frontend
	# backend tests (studio/tests/app/ only)
	docker compose -f docker-compose.test.yml run test_studio_backend $(PYTEST) studio/tests/app/ -m "not heavier_processing"
	# frontend tests
	docker compose -f docker-compose.test.yml run test_studio_frontend
	# lambda tests (reuse backend container)
	docker compose -f docker-compose.test.yml run test_studio_backend $(PYTEST) studio/tests/infrastructure/ -v

.PHONY: test_backend
test_backend:
	# cleanup
	@$(call cleanup_test_env, test_studio_backend)
	# build/run
	@$(call run_test_service, test_studio_backend, $(PYTEST) studio/tests/app/ -m "not heavier_processing")

.PHONY: test_backend_full
test_backend_full:
	# cleanup
	@$(call cleanup_test_env, test_studio_backend)
	# build/run
	@$(call run_test_service, test_studio_backend, $(PYTEST) studio/tests/app/)

.PHONY: test_frontend
test_frontend:
	# cleanup
	@$(call cleanup_test_env, test_studio_frontend)
	# build/run
	@$(call run_test_service, test_studio_frontend)

.PHONY: test_lambda
test_lambda:
	# cleanup
	@$(call cleanup_test_env, test_studio_backend)
	# build/run
	@$(call run_test_service, test_studio_backend, $(PYTEST) studio/tests/infrastructure/ -v)

.PHONY: test_contract
test_contract:
	# API contract tests - validates backend responses match frontend TypeScript interfaces
	# NOTE: These tests are a subset of test_backend. Use this target for running contract tests only.
	# cleanup
	@$(call cleanup_test_env, test_studio_backend)
	# build/run
	@$(call run_test_service, test_studio_backend, $(PYTEST) studio/tests/app/common/routers/test_*_contract.py -v)

.PHONY: alembic_check
alembic_check:
	# Migrate a fresh DB to head, then run `alembic check` to assert the
	# SQLAlchemy models and the migrations describe the same schema.
	# Run in a single shell with an EXIT trap so the DB stack is always torn
	# down, even when `alembic check` exits non-zero (expected while models
	# drift). set -e still propagates that non-zero status out to CI.
	@bash -euc '\
		compose="docker compose -f docker-compose.alembic-check.yml"; \
		trap "$$compose down -v" EXIT; \
		$$compose down -v; \
		$$compose build alembic_check; \
		$$compose run --rm alembic_check'

.PHONY: premium_lock_it
premium_lock_it:
	# Real-MySQL GET_LOCK integration test proving distributed_lock
	# serializes concurrent sessions.
	# Opt-in: needs a real database, so it is not part of per-PR CI.
	# Single shell with an EXIT trap so the throwaway DB is always torn down.
	@bash -euc '\
		compose="docker compose -f docker-compose.premium-lock-it.yml"; \
		trap "$$compose down -v" EXIT; \
		$$compose down -v; \
		$$compose build premium_lock_it; \
		$$compose run --rm premium_lock_it'

.PHONY: workflow_count_it
workflow_count_it:
	# Concurrent workflow-count integration test proving increments and
	# decrements serialize on the row over real connections.
	# Opt-in: needs a real database, so it is not part of per-PR CI.
	# Single shell with an EXIT trap so the throwaway DB is always torn down.
	@bash -euc '\
		compose="docker compose -f docker-compose.workflow-count-it.yml"; \
		trap "$$compose down -v" EXIT; \
		$$compose down -v; \
		$$compose build workflow_count_it; \
		$$compose run --rm workflow_count_it'


############################## For Building ##############################

VERSION := $(shell poetry version -s)

.PHONY: version
version:
	echo "Current Optinist Version: $(VERSION)"


.PHONY: build_frontend
build_frontend:
	# cleanup
	docker compose -f docker-compose.build.yml down
	docker compose -f docker-compose.build.yml rm -f
	@$(call rm_unused_docker_containers, studio-build-fe)
	# build/run
	docker compose -f docker-compose.build.yml build studio-build-fe
	docker compose -f docker-compose.build.yml run studio-build-fe

ROOT_PY := *.py
FORMAT_TARGETS := $(ROOT_PY) studio infrastructure
EXCLUDE_DIRS := infrastructure/terraform/.build

.PHONY: format
format:
	black $(FORMAT_TARGETS) --exclude $(EXCLUDE_DIRS)
	isort $(FORMAT_TARGETS) --skip $(EXCLUDE_DIRS)
	flake8 $(FORMAT_TARGETS) --exclude $(EXCLUDE_DIRS)
	codespell --skip="./dist,./frontend/node_modules,./logs"

.PHONY: docs
docs:
	rm -rf docs/_build/
	poetry install --with doc --no-root
	# sphinx-apidoc -f -o ./docs/_build/modules ./studio
	sphinx-autobuild -b html docs docs/_build --port 8001

.PHONY: local_build
local_build:
	rm -rf dist
	cd frontend && yarn install --ignore-scripts && yarn build
	poetry build


############################## For Deployment ##############################

.PHONY: push_testpypi
push_testpypi:
	poetry publish -r testpypi

.PHONY: install_testpypi
install_testpypi:
	pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ optinist==${ver}
	pip show optinist

.PHONY: build_test_docker
build_test_docker:
	docker build --no-cache -t optinist-release-test:${ver} -f studio/config/docker/Dockerfile .

.PHONY: run_test_docker
run_test_docker:
	docker run -it \
	-v ${volume}:/app/studio_data \
	--env OPTINIST_DIR="/app/studio_data" \
	--name optinist-release-test -d -p 8000:8000 optinist-release-test:${ver} \
	poetry run python main.py --host 0.0.0.0 --port 8000

.PHONY: push_pypi
push_pypi:
	poetry publish

.PHONY: push_dockerhub
push_dockerhub:
	docker build --rm -t oistncu/optinist:latest -f studio/config/docker/Dockerfile . --platform=linux/amd64
	docker tag oistncu/optinist:latest oistncu/optinist:${VERSION}
	docker push oistncu/optinist:${VERSION}
	docker push oistncu/optinist:latest
