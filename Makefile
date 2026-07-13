# Dexter — loop harness gate (see CLAUDE.md "Backlog drain loop")

.PHONY: loop-test
loop-test:
	@echo "Running agentic-loop harness tests (pytest)..."
	python3 -m pytest tests/scripts -q
