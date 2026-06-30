# Agent Desk — local task automation.
# Zero runtime dependencies: everything runs on the Python standard library
# plus the `gh`, `git`, and `codex` command-line tools.

PYTHON ?= python3
CONFIG ?= config/repos.toml

# Optional overrides for `make serve`, e.g. `make serve PORT=9000 HOST=0.0.0.0`.
HOST ?=
PORT ?=
SERVE_FLAGS :=
ifneq ($(HOST),)
SERVE_FLAGS += --host $(HOST)
endif
ifneq ($(PORT),)
SERVE_FLAGS += --port $(PORT)
endif

.DEFAULT_GOAL := help

.PHONY: help serve init test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

serve: ## Launch the dashboard + scheduler (http://127.0.0.1:8765; auto-increments port if busy)
	$(PYTHON) -m agent_desk serve --config $(CONFIG) $(SERVE_FLAGS)

init: ## Write an example config/repos.toml (does nothing if it exists)
	$(PYTHON) -m agent_desk init-config --path $(CONFIG)

test: ## Run the unit test suite
	$(PYTHON) -m unittest discover -s tests -v
