# Convenience targets for the opentrons monorepo.
#
# Two packages, two deploy paths:
#   - control software  -> `make deploy-control`
#   - robot software     -> `make update-robots`

COMPOSE_DIR := control/opentrons_control

.PHONY: help install-dev lint test build-wheel update-robots deploy-control

help:
	@echo "Targets:"
	@echo "  install-dev    Install dev tooling + both packages (editable)"
	@echo "  lint           Run pre-commit (ruff, mypy, ...) on all files"
	@echo "  test           Run the test suite"
	@echo "  build-wheel    Build the opentrons_drivers wheel into dist/wheels"
	@echo "  update-robots  Build + deploy the drivers wheel to every robot"
	@echo "  deploy-control Rebuild and restart the control Docker stack"

install-dev:
	pip install -r dev-requirements.txt
	pip install -e ./drivers -e ./control

lint:
	pre-commit run --all-files

test:
	pytest

# Build the robot wheel only (no deployment). Output: ./dist/wheels.
build-wheel:
	ot-build-wheel

# Bump drivers/pyproject.toml version (and deploy.toml expected_version)
# first, then run this on the control machine. Pass ARGS="--dry-run" etc.
update-robots:
	ot-update-robots $(ARGS)

# Bump control/pyproject.toml version first, then redeploy the stack.
deploy-control:
	cd $(COMPOSE_DIR) && docker compose up -d --build
