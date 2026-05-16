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
export TF_VAR_region="ap-south-1"
export TF_VAR_github_repository="Athithya-Sakthivel/E2E-RAG-System"

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

### Run this script to replace account id and aws region to trigger ci worklows to push rag images to ecr. After 5 min open https://<REPO_URL>/actions
```sh
bash src/scripts/replace.sh
```

<details>
<summary>▶ Expected output</summary>

![alt text](src/scripts/archive/images/ecr_push.png)

</details>

### Setup argocd
```sh
export GIT_PAT=ghp_   # https://github.com/settings/tokens/new
bash src/infra/core/argo_setup.sh --rollout
```

<details>
<summary>▶ Expected output</summary>

![alt text](src/scripts/archive/images/argo_setup.png)

</details>



### Bootstrap karpenter for bursty, stateless workloads
```sh
### ---- REQUIRED INPUTS ----
export GH_REPO="https://github.com/Athithya-Sakthivel/E2E-RAG-System.git"
export GH_BRANCH="main"
export AWS_REGION="ap-south-1"
bash src/scripts/eks/bootstrap_karpenter.sh --rollout
```

<details>
<summary>▶ Expected output</summary>

![alt text](src/scripts/archive/images/karpenter.png)

</details>

### Deploy the full E2E RAG indexing pipeline (Qdrant → FastEmbed → Indexing CronJob). [Docs](src/indexing_pipeline/README.md)
 * NOTE: Karpenter may take longer than 5-15 minutes to provision EC2 instances if the
 * cheapest matching instance type is not available in your account/region.
 * It will automatically retry with other c-family types. This is normal.
 * Pods stay Pending until a compatible instance launches successfully.

```sh
bash src/scripts/eks/run_indexing_pipeline.sh
```

<details>
<summary>▶ Expected output</summary>

![alt text](src/scripts/archive/images/indexing.png)

</details>


### Provision the cloudflare resources. The script waits till you login to cloudflare and authorize the correct root domain

```sh
export CLOUDFLARE_ACCOUNT_ID=
export CLOUDFLARE_GLOBAL_API_KEY=
export CLOUDFLARE_EMAIL="athithya651@gmail.com"  
export DOMAIN="athithya.site"   # root domain
bash src/infra/terraform/cloudflare/run.sh --apply
export CLOUDFLARE_TUNNEL_TOKEN="$(tofu -chdir=src/infra/terraform/cloudflare output -raw cloudflare_tunnel_token)"
export CLOUDFLARE_TUNNEL_NAME="$(tofu -chdir=src/infra/terraform/cloudflare output -raw cloudflare_tunnel_name)"
export CLOUDFLARE_TUNNEL_ID="$(tofu -chdir=src/infra/terraform/cloudflare output -raw cloudflare_tunnel_id)"
python3 src/infra/core/cloudflared_setup.py --write

```

<details>
<summary>▶ Expected output</summary>

![alt text](src/scripts/archive/images/tunnel.png)

</details>

### Export these env vars and deploy the inference services retriever, frontend, valkey and cloudflared tunnel.  
[Env vars](https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/google/#usage)

```sh
export GOOGLE_CLIENT_ID=
export GOOGLE_CLIENT_SECRET=
export MS_CLIENT_ID=
export MS_CLIENT_SECRET=
export MICROSOFT_ALLOWED_TENANT_IDS=
export MICROSOFT_ALLOWED_DOMAINS=
bash src/scripts/eks/run_inference_pipeline.sh
```

<details>
<summary>▶ Expected output</summary>

![alt text](src/scripts/archive/images/inference_svc.png)

</details>



### Setup the observability stack with optionally slack/pagerduty connection credentials
```sh
export CLICKHOUSE_USER=vector
export CLICKHOUSE_PASSWORD=vectorpass
export SLACK_WEBHOOK_URL=
export PAGERDUTY_ROUTING_KEY=
export ADMIN_PASSWORD=grafana
bash src/infra/observability/setup.sh
```

Now Open your browser to access these services
https Routes:
- rag.<domain>      -> frontend service
- argocd.<domain>   -> Argo CD server
- grafana.<domain>  -> Grafana service

rag.<domain> will open the rag chat ui, login with a google/microsoft account , default configs allows all accounts