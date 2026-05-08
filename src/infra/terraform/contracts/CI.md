## GitHub Actions → AWS (OIDC) Setup for ECR

### When this is needed

Use GitHub OIDC roles **only if workflows interact with AWS** (e.g., pushing images to ECR).

* If workflows only build/test locally → no AWS role needed
* If workflows push to **Amazon ECR** → OIDC role required

---

## Required IAM Roles

Create one role per repository:

* `gh-actions-flyte-elt-task`
* `gh-actions-flyte-train-task`
* `gh-actions-tabular-inference-service`

---

## Trust Policy (OIDC)

Use this trust policy for each role (replace `<ACCOUNT_ID>`, `<OWNER>`, `<REPO>`):

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
          "token.actions.githubusercontent.com:sub": "repo:<OWNER>/<REPO>:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

---

## Repository-Specific `sub` Values

Use the correct `sub` for each repo:

```
repo:<OWNER>/flyte-elt-task:ref:refs/heads/main
repo:<OWNER>/flyte-train-task:ref:refs/heads/main
repo:<OWNER>/tabular-inference-service:ref:refs/heads/main
```

---

## Permissions Policy (ECR Push)

Attach this policy to each role to allow pushing images to ECR:

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

## Notes

* Scope each role **per repo and branch** (no wildcards)
* Start with minimal permissions; expand only if required
* Roles can be created **without permissions initially** and updated later
* Do not use static AWS credentials in GitHub Actions when OIDC is enabled

---

## Outcome

* GitHub Actions authenticate to AWS via OIDC
* No long-lived credentials are stored
* Workflows can securely push images to **Amazon ECR**
