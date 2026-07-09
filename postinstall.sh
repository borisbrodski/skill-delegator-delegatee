#!/bin/bash
set -e
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$SKILL_DIR/scripts/"*
