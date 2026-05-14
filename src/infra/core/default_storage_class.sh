cat >/tmp/eks-default-sc.yaml <<'YAML'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: default-storage-class
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  encrypted: "true"
  csi.storage.k8s.io/fstype: ext4
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
YAML

kubectl delete storageclass --all

kubectl apply -f /tmp/eks-default-sc.yaml

kubectl get storageclass -o wide
kubectl get storageclass default-storage-class -o jsonpath='{.metadata.name}{" default="}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{" provisioner="}{.provisioner}{" type="}{.parameters.type}{"\n"}'