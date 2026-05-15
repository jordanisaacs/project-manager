.PHONY: check lint typecheck test test-emacs fmt hooks-install hooks-uninstall

check: lint typecheck test test-emacs

lint:
	@uv run ruff check src tests || { \
		echo ""; \
		echo "lint failed — don't use noqa / type: ignore to silence."; \
		echo "fix the underlying issue (rename, split, narrow types, etc.)."; \
		exit 1; \
	}
	@uv run ruff format --check src tests || { \
		echo ""; \
		echo "format check failed — run 'make fmt' to apply ruff format."; \
		exit 1; \
	}

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

# Pre-commit hook setup.
#
# `core.hooksPath` is the only way to install a hook in a worktree (no
# `.git/hooks` directory there). If you already have a global
# `core.hooksPath` (e.g. Databricks corporate hooks), our
# `.githooks/pre-commit` chains to it via the `$PM_PRE_COMMIT_CHAIN`
# env var so the upstream secret-scan / commit-msg checks still run —
# point that env var at the global hook's directory before committing.
hooks-install:
	git config core.hooksPath .githooks
	@echo "pre-commit hook installed (runs 'make lint')."
	@echo "If you had a global core.hooksPath, set PM_PRE_COMMIT_CHAIN=<old path>"
	@echo "in your shell so .githooks/pre-commit chains to it."

hooks-uninstall:
	git config --unset core.hooksPath || true
	@echo "pre-commit hook removed."
