"""``python -m cisco_exporter``: the same entry point as the console script."""
import sys

from .cli import main

sys.exit(main())
