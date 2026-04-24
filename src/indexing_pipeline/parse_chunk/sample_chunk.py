import io
import json
import os

import boto3
import pyarrow.parquet as pq

bucket = os.environ["DATA_S3_BUCKET"]
key = os.environ["DATA_S3_KEY"]

s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"))
obj = s3.get_object(Bucket=bucket, Key=key)

buf = io.BytesIO(obj["Body"].read())
table = pq.read_table(buf)
rows = table.to_pylist()

print(json.dumps(rows, indent=2, ensure_ascii=False))
