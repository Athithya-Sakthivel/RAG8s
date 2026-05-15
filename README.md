# Get started

## Prerequisites
1. Docker installed, running *without* sudo access
2. **Visual Studio Code with the Dev Containers extension installed (for a deterministic environments): [https://code.visualstudio.com/docs/devcontainers/containers](https://code.visualstudio.com/docs/devcontainers/containers)**
3. **An AWS account with sufficient IAM permissions (AdministratorAccess or equivalent) to manage**:
   * Amazon EKS (Elastic Kubernetes Service)
   * EC2, VPCs, Subnets, and Security Groups
   * Amazon S3
   * IAM Roles, Policies, and Instance Profiles
   **AWS Free Tier is sufficient for development and testing purposes.**
4. **A Cloudflare account with a registered domain, with permissions to manage DNS records and create Cloudflare Tunnels (cloudflared)**

## Clone the repo and build the devcontainer(Reproducible). This will take 10-20 minutes. 
```sh 
cd $HOME && rm -rf E2E-RAG-System && git clone https://github.com/Athithya-Sakthivel/E2E-RAG-System.git && cd E2E-RAG-System && code .
```
> ctrl + shift + P -> paste `Dev containers: Rebuild Container Without Cache` and enter

### Open a new terminal and login to your gh account
```sh
git config --global user.name "Your Name" && git config --global user.email you@example.com
gh auth login

? What account do you want to log into? GitHub.com
? What is your preferred protocol for Git operations? SSH
? Generate a new SSH key to add to your GitHub account? No
? How would you like to authenticate GitHub CLI? Login with a web browser

! First copy your one-time code: <code>
- Press Enter to open github.com in your browser... 
✓ Authentication complete. Press Enter to continue...
```

### Create a private repo in your gh account

```sh
export REPO_NAME="E2E-RAG-System" # or any name
git remote remove origin 2>/dev/null || true
gh repo create "$REPO_NAME" --private >/dev/null 2>&1
REMOTE_URL="https://github.com/$(gh api user | jq -r .login)/$REPO_NAME.git"
git remote add origin "$REMOTE_URL" 2>/dev/null || true
git branch -M main 2>/dev/null || true
git push -u origin main
git pull
git remote -v
echo "[INFO] A private repo '$REPO_NAME' created and pushed. Only visible from your account."
```


### Login to aws and bootstrap the terrraform infrastructure
```sh
export TF_VAR_environment="staging"
export TF_VAR_region="ap-south-1"
export TF_VAR_cluster_name="rag-eks-staging"
export TF_VAR_github_repository="Athithya-Sakthivel/E2E-RAG-System"
export TF_VAR_system_nodegroup_replicas=4
bash src/infra/terraform/aws/run.sh --create --env staging
```
<details>
<summary>▶ Expected output</summary>

![alt text](src/scripts/archive/images/tf.png)

</details>

### Login to the eks cluster as public endpoint enabled for staging cluster.
```sh
aws eks update-kubeconfig --region ap-south-1 --name rag-eks-staging
```

### Setup argocd
```sh
export GIT_PAT=ghp_   # https://github.com/settings/tokens/new
bash src/infra/core/argo_setup.sh --rollout
```


### Bootstrap karpenter for bursty, stateless workloads
```sh
### ---- REQUIRED INPUTS ----
export GH_REPO="https://github.com/Athithya-Sakthivel/E2E-RAG-System.git"
export GH_BRANCH="main"
export AWS_REGION="ap-south-1"
#### -----OPTIONAL INPUTS ----
export INSTANCE_CATEGORIES="c"
export INSTANCE_GENERATIONS="3"
export EXCLUDED_INSTANCE_TYPES="t2,t3,t4g"
export SPOT_ENABLED="true"
export ON_DEMAND_BASE_CAPACITY="2"
export CPU_LIMIT="50"
export MAX_NODE_COUNT="10"
export CONSOLIDATION_ENABLED="true"
export CONSOLIDATION_POLICY="WhenUnderutilized"
export TTL_SECONDS_UNTIL_EXPIRED="259200"
export TTL_SECONDS_AFTER_EMPTY="300"
bash src/scripts/eks/bootstrap_karpenter.sh --rollout
```

<details>
<summary>▶ Expected output</summary>

![alt text](src/scripts/archive/images/karpenter.png)

</details>

