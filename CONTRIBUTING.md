# Contributing

Note: the original authors no longer actively maintain this project. Maintainers
are volunteers, so reviews may be slow. Thank you for your patience.

## Set up

Python 3.12 or newer.

    python -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev]"

## Style

    ruff check src

Line length is 100. Please keep new code typed (`mypy` is configured as strict).

## Pull requests

1. Fork and create a branch.
2. Keep the change focused.
3. Open a PR and fill in the template.
4. Never commit secrets, personal data or real farm files. Use made-up example fields.

By contributing you agree your work is licensed under Apache-2.0.
