REPO_ROOT=$(pwd)
TOFU_DIR="$REPO_ROOT/src/infra/terraform/aws"
DATA_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.DATA_S3_BUCKET')"
BACKUP_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.QDRANT_BACKUPS_BUCKET')"

# The restore script uses DATA_S3_BUCKET, not BACKUP_S3_BUCKET
echo "DATA_S3_BUCKET: $DATA_S3_BUCKET"
echo "BACKUP_S3_BUCKET: $BACKUP_S3_BUCKET"

# Copy backups to the DATA bucket where the restore script looks
aws s3 cp \
  s3://s3-temp-bucket-mlsecops-681802563986/qdrant/backups/ \
  "s3://${DATA_S3_BUCKET}/qdrant/backups/" \
  --recursive

# Verify
aws s3 ls "s3://${DATA_S3_BUCKET}/qdrant/backups/" --recursive

# Now restore
export PER_POD=true
export QDRANT_BACKUP_S3_PREFIX="qdrant/backups/"
bash src/scripts/backups_and_restore.sh restore


# Port forward Qdrant
kubectl port-forward -n qdrant svc/qdrant 6333:6333 & 
sleep 2
# Delete the semantic cache collection
curl -X DELETE http://localhost:6333/collections/default_rag_collection1__semantic_cache

# Verify it's gone
curl http://localhost:6333/collections/default_rag_collection1__semantic_cache

# Check remaining collections
curl http://localhost:6333/collections

# Cleanup
kill %1