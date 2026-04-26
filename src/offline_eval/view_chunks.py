# python3 src/scripts/local/force_sync_s3_local_fs.py --download

import json

import pyarrow.parquet as pq

PATH = "data/chunked/3796b1255a7675d1852cc7722906e7106439bb6db1af80d92e67409bda8af0c3.parquet"

table = pq.read_table(PATH)
rows = table.to_pylist()

JSON_FIELDS = {
    "figures",
    "tags",
    "layout_tags",
    "heading_path",
    "headings",
    "token_range",
    "original_manifest",
}

def try_parse(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v

for row in rows:
    for k in JSON_FIELDS:
        if k in row:
            row[k] = try_parse(row[k])
    print(json.dumps(row, indent=2, ensure_ascii=False))
