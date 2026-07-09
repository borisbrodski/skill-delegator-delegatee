#!/usr/bin/env python3
"""Make delegation scripts executable."""
import os
from pathlib import Path

scripts = [
    'src/verify_delegation_setup.py',
    'src/delegation_worker.py',
]

for script in scripts:
    path = Path(__file__).parent.parent / script
    if path.exists():
        os.chmod(path, 0o755)
        print(f"Made executable: {path}")
    else:
        print(f"Not found: {path}")
