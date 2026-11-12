import sys

from . import ports
from .cli import main

ports.load()  # the adapters (adapter_*.py): what the viewers and the harnesses give to the command line
sys.exit(main())
