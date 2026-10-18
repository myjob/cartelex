import os
import yaml

from config.settings import settings

_current_dir = os.path.dirname(os.path.abspath(__file__))
_config_path = os.path.join(_current_dir, "config.yaml")

def load_config():
    if not os.path.exists(_config_path):
        raise FileNotFoundError(f"Config file not found at {_config_path}")
    
    with open(_config_path, "r") as f:
        return yaml.safe_load(f)

CONFIG = load_config()

# Single owner for the connection string: settings.DATABASE_URL, which reads
# the process environment first and config_main/.env second. config.yaml no
# longer supplies it -- the yaml copy was unreachable to every caller that
# documents this value as "overridable via DATABASE_URL env var", so a
# containerised or otherwise relocated database was silently ignored.
DATABASE_URL = settings.DATABASE_URL
COLLECTION_NAME = CONFIG.get("collection_name")
SCHEMA = CONFIG.get("schema")
SEARCH_PATH = CONFIG.get("search_path")
