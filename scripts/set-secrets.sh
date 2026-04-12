#!/usr/bin/env bash
set -euo pipefail

# Propagate secrets from workspace .env to GitHub Actions for magpie-backend.
# Usage: bash scripts/set-secrets.sh
#
# Reads from c:\Personal\repos\.env using Python (not bash source)
# because the workspace .env contains & in URLs and non-ASCII inline comments.

REPO="Abdul-Muizz1310/magpie-backend"
ENV_FILE="c:/Personal/repos/.env"

SECRETS=(
  DATABASE_URL
  R2_ACCOUNT_ID
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  OPENROUTER_API_KEY
  GITHUB_PAT_SCRAPE_HEALER
  LANGFUSE_PUBLIC_KEY
  LANGFUSE_SECRET_KEY
)

for secret in "${SECRETS[@]}"; do
  value=$(python -c "
import re, sys
with open('$ENV_FILE', encoding='utf-8') as f:
    for line in f:
        m = re.match(r'^${secret}=(.+?)(?:\s+#.*)?$', line.strip())
        if m:
            print(m.group(1), end='')
            sys.exit(0)
sys.exit(1)
" 2>/dev/null) || { echo "SKIP $secret (not found in .env)"; continue; }

  printf '%s' "$value" | gh secret set "$secret" --repo "$REPO" --body -
  echo "SET  $secret"
done
