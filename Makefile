core:
	kind delete cluster --name local-cluster || true && kind create cluster --name local-cluster && \
	bash src/infra/core/default_storage_class.sh

rollout-qdrant:
	python3 infra/generators/qdrant_cluster.py --rollout
rollout-qdrant-with-flux:
	python3 infra/generators/qdrant_cluster.py --rollout --flux


delete-qdrant:
	kubectl delete ns qdrant


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

setup-flux:
	curl -s https://fluxcd.io/install.sh | sudo FLUX_VERSION=2.7.5 bash || true
	python3 infra/setup/setup_fluxcd.py --auto-push



delete-flux:
	kubectl delete ns flux-system --grace-period=0 --force --wait=false || true
	kubectl get crd | grep fluxcd.io | awk '{print $$1}' | xargs -r kubectl delete crd --grace-period=0 --force || true
	kubectl delete crd gitrepositories.source.toolkit.fluxcd.io helmrepositories.source.toolkit.fluxcd.io --grace-period=0 --force || true
	kubectl get ns flux-system -o json 2>/dev/null | jq 'del(.spec.finalizers)' | kubectl replace --raw "/api/v1/namespaces/flux-system/finalize" -f - || true
