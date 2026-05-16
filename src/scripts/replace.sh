
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" \
python3 - <<'PY'
from pathlib import Path
import os
import re

account_id = os.environ["ACCOUNT_ID"]
region = "ap-south-1"

def indent_len(s: str) -> int:
    return len(s) - len(s.lstrip(" "))

for path in Path(".github/workflows").rglob("*"):
    if path.suffix.lower() not in {".yml", ".yaml"}:
        continue

    original = path.read_text(encoding="utf-8")

    # Fix: Handle both quoted and unquoted account IDs
    text = re.sub(
        r'(^\s*AWS_ACCOUNT_ID:\s*)"?\d+"?(\s*$)',
        lambda m: f'{m.group(1)}"{account_id}"{m.group(2)}',
        original,
        flags=re.M,
    )
    
    text = re.sub(
        r"(^\s*AWS_REGION:\s*).*$",
        lambda m: f"{m.group(1)}{region}",
        text,
        flags=re.M,
    )

    lines = text.splitlines()
    out = []
    i = 0

    while i < len(lines):
        line = lines[i]

        m = re.match(r"^(\s*)run:\s*\|\s*$", line)
        if not m:
            out.append(line)
            i += 1
            continue

        run_indent = len(m.group(1))
        block = [line]
        i += 1

        while i < len(lines):
            cur = lines[i]
            if cur.strip() and indent_len(cur) <= run_indent:
                break
            block.append(cur)
            i += 1

        if any(re.match(r"^\s*set -euo pipefail\s*$", b) for b in block):
            new_block = []
            inserted = False
            for b in block:
                if re.match(r"^\s*export\s+TF_VAR_region=", b):
                    continue
                new_block.append(b)
                setm = re.match(r"^(\s*)set -euo pipefail\s*$", b)
                if setm and not inserted:
                    indent = setm.group(1)
                    new_block.append(f'{indent}export TF_VAR_region="{region}"')
                    inserted = True
            block = new_block

        out.extend(block)

    new_text = "\n".join(out) + ("\n" if original.endswith("\n") else "")
    if new_text != original:
        path.write_text(new_text, encoding="utf-8")
        print(f"updated {path}")
PY

git add . && git commit -m "trigger all image CI" && git push origin main