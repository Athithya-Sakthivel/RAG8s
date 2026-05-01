core:
	kind delete cluster --name local-cluster || true && kind create cluster --name local-cluster && \
	bash src/infra/core/default_storage_class.sh

rollout-qdrant:
	python3 infra/generators/qdrant_cluster.py --rollout
rollout-qdrant-with-flux:
	python3 infra/generators/qdrant_cluster.py --rollout --flux


delete-qdrant:
	kubectl delete ns qdrant


POSTGRES_SH := bash src/infra/core/postgres_cluster.sh

.PHONY: pg-cluster pg-backup pg-restore-latest pg-restore-time

pg-cluster:
	$(POSTGRES_SH) deploy --create-initial-backup false

pg-backup:
	$(POSTGRES_SH) backup --wait

pg-restore-latest:
	$(POSTGRES_SH) deploy --restore latest --force-recreate

pg-restore-time:
	@test -n "$$TARGET_TIME" || (echo "ERROR: TARGET_TIME must be set (RFC3339)" && exit 1)
	$(POSTGRES_SH) deploy --restore time --target-time "$$TARGET_TIME" --force-recreate


dense-image:
	bash apps/dense/test_and_push_dense.sh

rollout-dense:
	python3 infra/generators/dense.py --rollout

delete-dense:
	python3 infra/generators/dense.py --delete


sparse-image:
	bash apps/sparse/test_and_push_sparse.sh

rollout-sparse:
	python3 infra/generators/sparse.py --rollout

delete-sparse:
	python3 infra/generators/sparse.py --delete


base-index-image:
	bash apps/index/build_and_push_base_image.sh

index-image:
	bash apps/index/build_and_push_image.sh

rollout-indexing-cronjob:
	python3 infra/generators/indexing_cronjob.py --rollout

delete-indexing-cronjob:
	kubectl delete ns indexing


frontend-image:
	bash apps/inference/frontend/build_and_push_frontend.sh

retrieval-image:
	bash apps/inference/retrieval/test_and_push_retriever.sh


rollout-reranker:
	python3 infra/generators/reranker.py --rollout

reranker-image:
	bash apps/reranker/test_and_push_reranker.sh

delete-reranker:
	python3 infra/generators/reranker.py --delete


push:
	git add .
	git commit -m "new"
	git push origin main