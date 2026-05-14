

kubectl create ns indexing || true
kubectl create ns inference || true
export AWS_REGION="$(cd src/infra/terraform/aws && tofu output -raw aws_region)" && \
export AWS_DEFAULT_REGION="$AWS_REGION" && \
export REGION="$AWS_REGION" && \
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" && \
if [ -z "$REGION" ]; then REGION="$(aws ec2 describe-availability-zones --query 'AvailabilityZones[0].RegionName' --output text)"; fi && \
echo "Using account=$ACCOUNT_ID region=$REGION" && \
echo "frontend  -> ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/frontend:staging" && \
echo "retriever -> ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/retriever:staging" && \
echo "dense     -> ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dense_model:staging" && \
echo "sparse    -> ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/sparse_model:staging" && \
echo "reranker  -> ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/reranker:staging" && \
echo "indexer   -> ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/indexer:staging" && \
kubectl create configmap frontend-image -n inference --from-literal=image="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/frontend:staging" --dry-run=client -o yaml | kubectl apply -f - && \
kubectl create configmap retriever-image -n inference --from-literal=image="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/retriever:staging" --dry-run=client -o yaml | kubectl apply -f - && \
kubectl create configmap dense-image -n inference --from-literal=image="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dense_model:staging" --dry-run=client -o yaml | kubectl apply -f - && \
kubectl create configmap sparse-image -n inference --from-literal=image="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/sparse_model:staging" --dry-run=client -o yaml | kubectl apply -f - && \
kubectl create configmap reranker-image -n inference --from-literal=image="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/reranker:staging" --dry-run=client -o yaml | kubectl apply -f - && \
kubectl create configmap index-image -n indexing --from-literal=image="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/indexer:staging" --dry-run=client -o yaml | kubectl apply -f - && \
echo && \
kubectl get configmaps -n inference frontend-image retriever-image dense-image sparse-image reranker-image && \
kubectl get configmaps -n indexing index-image