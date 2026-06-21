.PHONY: install install-dev test test-verbose lint coverage ci

PYTHON ?= .venv/bin/python
PIP ?= .venv/bin/pip
RUFF ?= .venv/bin/ruff
COVERAGE ?= .venv/bin/coverage

install:
	python3 -m venv .venv
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt

install-dev: install
	$(PIP) install -r requirements-dev.txt

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -q

test-verbose:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

lint:
	$(RUFF) check src tests

coverage:
	PYTHONPATH=src $(COVERAGE) run -m unittest discover -s tests -q
	$(COVERAGE) report

ci: lint test coverage
