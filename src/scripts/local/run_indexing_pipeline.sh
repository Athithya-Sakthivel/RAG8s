#!/usr/bin/env bash

NAMESPACE="indexing"
MANIFEST_DIR="src/manifests/indexing-cronjob"
TEST_JOB_NAME="indexing-backup-manual"
WAIT_POLL=2
WAIT_TIMEOUT=120

log() { printf '%s\n' "$*"; }

log "Deploying indexing pipeline..."
python3 src/infra/rag/indexing_cronjob.py rollout

log "Waiting for CronJob indexing-backup-cronjob..."
start=$(date +%s)
while ! kubectl get cronjob indexing-backup-cronjob -n "$NAMESPACE" >/dev/null 2>&1; do
  if [ $(( $(date +%s) - start )) -ge $WAIT_TIMEOUT ]; then
    log "Timed out waiting for CronJob."
    exit 1
  fi
  sleep "$WAIT_POLL"
done

log "Deleting old job $TEST_JOB_NAME..."
kubectl delete job "$TEST_JOB_NAME" -n "$NAMESPACE" --ignore-not-found

log "Creating new job $TEST_JOB_NAME..."
kubectl create job --from=cronjob/indexing-backup-cronjob "$TEST_JOB_NAME" -n "$NAMESPACE"

log "Waiting for pod from job $TEST_JOB_NAME..."
start=$(date +%s)
POD=""
while [ -z "$POD" ]; do
  POD=$(kubectl get pods -n "$NAMESPACE" -l job-name="$TEST_JOB_NAME" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -n "$POD" ]; then
    log "Found pod: $POD"
    break
  fi
  if [ $(( $(date +%s) - start )) -ge $WAIT_TIMEOUT ]; then
    log "Timed out waiting for pod. Dumping job/events..."
    kubectl describe job "$TEST_JOB_NAME" -n "$NAMESPACE" || true
    kubectl get events -n "$NAMESPACE" --sort-by='.lastTimestamp' | tail -n 30 || true
    exit 1
  fi
  sleep "$WAIT_POLL"
done

log "Streaming logs from pod $POD..."
kubectl logs -n "$NAMESPACE" "$POD" -c indexer --follow --tail=200