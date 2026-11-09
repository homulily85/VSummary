#!/bin/sh
set -eu
umask 077

if [ -n "${NOTEBOOKLM_AUTH_JSON:-}" ]; then
    echo "VSummary requires file-backed NotebookLM authentication. Remove NOTEBOOKLM_AUTH_JSON." >&2
    exit 1
fi

profile_dir="$NOTEBOOKLM_HOME/profiles/$NOTEBOOKLM_PROFILE"
master_token="$profile_dir/master_token.json"

if [ ! -f "$master_token" ] || [ ! -r "$master_token" ] || [ ! -s "$master_token" ]; then
    echo "VSummary requires a readable, non-empty master_token.json in the NotebookLM profile." >&2
    exit 1
fi

if [ ! -w "$profile_dir" ] || [ ! -x "$profile_dir" ]; then
    echo "VSummary requires a writable NotebookLM profile to save refreshed cookies." >&2
    exit 1
fi

if [ ! -e "$profile_dir/storage_state.json" ]; then
    if ! notebooklm auth refresh --verify >/dev/null 2>&1; then
        echo "NotebookLM cookie setup failed. Check network access and the profile's master token." >&2
        exit 1
    fi
fi

exec vsummary
