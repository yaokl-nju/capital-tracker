#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec "${PYTHON:-.venv/bin/python}" run.py --tracker all "$@"
