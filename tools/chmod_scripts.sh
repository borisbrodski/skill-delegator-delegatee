#!/bin/bash
# Make all scripts executable after installation

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

for f in "$SCRIPTS_DIR"/*.py; do
    [ -f "$f" ] && chmod +x "$f"
done

for f in "$SCRIPTS_DIR"/*.sh; do
    [ -f "$f" ] && chmod +x "$f"
done

BIN_DIR="$(dirname "$SCRIPTS_DIR")/bin"
if [ -d "$BIN_DIR" ]; then
    for f in "$BIN_DIR"/*; do
        [ -f "$f" ] && chmod +x "$f"
    done
fi

echo "Scripts made executable."
