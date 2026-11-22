.PHONY: install run lint format test clean

# Create venv and install crepear + dev tools in editable mode
install:
	uv venv
	uv pip install -e ".[dev]"

# Run the CLI directly from the virtual environment
run:
	.venv/bin/crepear

# Check code style and bugs with ruff
lint:
	.venv/bin/ruff check src tests

# Auto-format code with ruff
format:
	.venv/bin/ruff format src tests

# Run unit tests
test:
	.venv/bin/pytest -v

# Remove build artifacts and caches
clean:
	rm -rf .venv .pytest_cache .ruff_cache src/*.egg-info build dist