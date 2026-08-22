import os
import pathlib

import yaml

REPO = pathlib.Path(os.environ.get("KINE_REPO", "/repo"))


def load() -> dict:
    with (REPO / "catalogue.yml").open() as fh:
        return yaml.safe_load(fh)["apps"]
