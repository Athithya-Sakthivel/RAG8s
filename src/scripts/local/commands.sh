


aws s3 cp \
  s3://s3-temp-bucket-mlsecops-681802563986/qdrant/backups/ \
  s3://rag-staging-qdrant-backups-681802563986/ \
  --recursive

REPO_ROOT=$(pwd)
TOFU_DIR="$REPO_ROOT/src/infra/terraform/aws"
DATA_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.DATA_S3_BUCKET')"
BACKUP_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.QDRANT_BACKUPS_BUCKET')"
export PER_POD=true
export QDRANT_BACKUP_S3_PREFIX="qdrant/backups/"
bash src/scripts/backups_and_restore.sh restore

