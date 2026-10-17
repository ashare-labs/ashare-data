#!/bin/sh
set -eu
project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
local_data_env=${ASHARE_LOCAL_DATA_ENV:-"$project/.venv-local-data"}
if [ ! -x "$local_data_env/bin/ashare-data" ]; then
    printf '%s\n' 'Create the dedicated local-data environment in this checkout:' \
        'UV_PROJECT_ENVIRONMENT=.venv-local-data uv sync --locked --no-config --python 3.12 --extra local-data' >&2
    exit 2
fi
exec "$local_data_env/bin/ashare-data" local-daily "$@"
