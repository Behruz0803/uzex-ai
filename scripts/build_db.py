#!/usr/bin/env python3
# Wrapper for uzex.etl
import sys
sys.path.insert(0, ".")
from uzex.etl import build_db
import pathlib
if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv)>1 else "/tmp/uzex_spot_data"
    db = sys.argv[2] if len(sys.argv)>2 else "./data/uzex.db"
    build_db(src, db)
