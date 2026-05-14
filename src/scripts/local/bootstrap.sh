aws eks update-kubeconfig --region ap-south-1 --name rag-eks-staging && sudo curl -L -o /usr/local/bin/kubectl https://s3.us-west-2.amazonaws.com/amazon-eks/1.35.3/2026-04-08/bin/linux/amd64/kubectl && sudo chmod +x /usr/local/bin/kubectl && kubectl version --client && sudo dnf install git -y && cd ~

