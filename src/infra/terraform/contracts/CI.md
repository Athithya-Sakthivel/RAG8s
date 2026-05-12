## GitHub Actions → AWS (OIDC) Contract for ECR (RAG System)

### Scope

This document defines how GitHub Actions workflows authenticate to AWS and push container images to **Amazon ECR** using **OIDC (no static credentials)**.

---

# ECR Repositories (Authoritative List)

Each service maps to a dedicated ECR repository:

```
frontend
retriever
dense-model
sparse-model
reranker
indexer
```

---

# IAM Role Model (Strict 1:1 Mapping)

Create **one IAM role per service**:

```
gh-actions-frontend
gh-actions-retriever
gh-actions-dense-model
gh-actions-sparse-model
gh-actions-reranker
gh-actions-indexer
```

### Invariant

* One role → one ECR repository
* No shared roles
* No wildcard repository access

---

# Trust Policy (OIDC)

Replace `<ACCOUNT_ID>` and `<OWNER>`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:<OWNER>/E2E-RAG-System:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

---

# Permissions Policy (ECR Push — Scoped)

Each role must be scoped to its **own repository only**.

Replace `<REGION>`, `<ACCOUNT_ID>`, `<REPO_NAME>`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRAuth",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ECRPushPull",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:CompleteLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:BatchGetImage"
      ],
      "Resource": "arn:aws:ecr:<REGION>:<ACCOUNT_ID>:repository/<REPO_NAME>"
    }
  ]
}
```

---

# Service → Repository Mapping

| Service   | Role                    | ECR Repo     |
| --------- | ----------------------- | ------------ |
| frontend  | gh-actions-frontend     | frontend     |
| retriever | gh-actions-retriever    | retriever    |
| dense     | gh-actions-dense-model  | dense-model  |
| sparse    | gh-actions-sparse-model | sparse-model |
| reranker  | gh-actions-reranker     | reranker     |
| indexer   | gh-actions-indexer      | indexer      |

---

# GitHub Actions Requirements

Each workflow must include:

## Permissions

```yaml
permissions:
  id-token: write
  contents: read
```

---

## AWS Authentication (OIDC)

```yaml
- name: Configure AWS credentials
  uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: arn:aws:iam::<ACCOUNT_ID>:role/gh-actions-<service>
    aws-region: <REGION>
```

---

## ECR Login

```yaml
- name: Login to ECR
  uses: aws-actions/amazon-ecr-login@v2
```

---

## Build + Push Pattern

```yaml
- name: Build and push image
  run: |
    IMAGE_URI=${{ steps.login-ecr.outputs.registry }}/<repo>:latest
    docker build -t $IMAGE_URI .
    docker push $IMAGE_URI
```

---

# Security Constraints (Non-Negotiable)

* No static AWS credentials in GitHub
* No wildcard (`*`) ECR repository permissions
* No shared IAM roles across services
* Trust policy must match **exact repo + branch**
* Default branch assumed: `main`

---

# Operational Notes

* Region must match ECR + EKS deployment region
* Roles can be created before repositories (policy attach later)
* Image tags should evolve beyond `latest` (e.g., commit SHA) for production
* Failures typically come from:

  * incorrect `sub` in trust policy
  * wrong repo name in IAM policy
  * missing `id-token: write`

---

# Outcome

* GitHub Actions authenticate via OIDC (STS)
* No long-lived credentials are stored
* Each service securely pushes to its own ECR repository
* Access is tightly scoped and auditable
