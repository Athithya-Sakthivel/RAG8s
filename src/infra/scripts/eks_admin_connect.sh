


cd src/infra/terraform/aws && aws ssm start-session \
  --region "$(tofu output -raw aws_region)" \
  --target "$(tofu output -raw eks_admin_instance_id)" \
  --document-name AWS-StartInteractiveCommand \
  --parameters 'command=[
    "aws eks update-kubeconfig --region '"$(tofu output -raw aws_region)"' --name '"$(tofu output -raw cluster_name)"'",
    "kubectl get nodes -o wide",
    "kubectl get pods -A -o wide",
    "bash"
  ]'
