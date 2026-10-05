import sys

import pytest

from helpers import SCRIPTS
from profy import ProfanityFilter

# The sync scripts import each other as top-level modules, as they do when run
# from the command line.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(scope="session")
def english():
    return ProfanityFilter()


@pytest.fixture(scope="session")
def every_language():
    return ProfanityFilter(all_languages=True)
