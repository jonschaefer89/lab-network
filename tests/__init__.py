"""Tests run offline against fake services, never with the API key of the person running them."""
import os
import tempfile

from labnetwork.connectors import openalex

os.environ.pop("OPENALEX_API_KEY", None)
openalex.KEY_FILE = os.path.join(tempfile.gettempdir(), "labnetwork-tests-no-openalex-key")
