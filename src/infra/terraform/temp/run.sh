cd /workspace/src/infra/terraform/local
tofu init
tofu plan
tofu apply --auto-approve
export BEDROCK_GUARDRAIL_IDENTIFIER=$(tofu output -raw bedrock_guardrail_arn)
export BEDROCK_GUARDRAIL_VERSION=$(tofu output -raw bedrock_guardrail_version_id)
echo $BEDROCK_GUARDRAIL_IDENTIFIER
echo $BEDROCK_GUARDRAIL_VERSION
cd -


cd /workspace/src/infra/terraform/local && tofu destroy --auto-approve


aws bedrock list-guardrails \
  --query "guardrails[].arn" \
  --output text | tr '\t' '\n' | while read arn; do
    echo "Deleting $arn"
    aws bedrock delete-guardrail --guardrail-identifier "$arn"
done
aws bedrock list-guardrails