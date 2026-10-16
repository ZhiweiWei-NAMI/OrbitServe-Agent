#!/usr/bin/env python3
from orbitserve.cli import main
raise SystemExit(main(['accept', *__import__('sys').argv[1:]]))
