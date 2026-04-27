.PHONY: check lint typecheck test test-emacs fmt

check: lint typecheck test test-emacs

lint:
	uv run ruff check src tests

typecheck:
	uv run ty check src tests

test:
	uv run pytest

test-emacs:
	cd integrations/emacs && \
		emacs -Q -batch -L . -l ert -l pm-tests.el \
			-f ert-run-tests-batch-and-exit

fmt:
	uv run ruff format src tests
	uv run ruff check --fix --unsafe-fixes src tests
