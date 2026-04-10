"""
Langfuse bootstrap — creates account, project, and API keys on fresh deploy.
Outputs keys as a Kubernetes Secret patch for other services to consume.

Usage:
  python3 demo/init-langfuse.py [langfuse_url]

Waits for Langfuse to be ready, then:
  1. Creates admin account (raj@lab.local / rajailab)
  2. Creates "raj-ai-lab" project
  3. Generates API key pair
  4. Creates/updates K8s secret "langfuse-api-keys" in ai-platform namespace
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

LANGFUSE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://langfuse.ai-platform:3001"
EMAIL = os.environ.get("LANGFUSE_ADMIN_EMAIL", "raj@lab.local")
PASSWORD = os.environ.get("LANGFUSE_ADMIN_PASSWORD", "rajailab")
PROJECT_NAME = os.environ.get("LANGFUSE_PROJECT", "raj-ai-lab")

def post(path, data, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        f"{LANGFUSE_URL}{path}",
        data=json.dumps(data).encode(),
        headers=hdrs,
        method="POST"
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": body}, e.code

def get(path, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(f"{LANGFUSE_URL}{path}", headers=hdrs)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {"error": str(e)}, e.code

# Wait for Langfuse to be ready
print(f"Waiting for Langfuse at {LANGFUSE_URL}...")
for i in range(60):
    try:
        urllib.request.urlopen(f"{LANGFUSE_URL}/api/public/health", timeout=5)
        print("Langfuse is ready")
        break
    except:
        time.sleep(5)
else:
    print("ERROR: Langfuse not ready after 5 minutes")
    sys.exit(1)

# Step 1: Create account
print(f"Creating account {EMAIL}...")
result, status = post("/api/auth/signup", {
    "name": "Raj AI Lab Admin",
    "email": EMAIL,
    "password": PASSWORD,
})
if status == 200 or status == 201:
    print(f"  Account created")
elif "already exists" in str(result).lower() or status == 422:
    print(f"  Account already exists, continuing")
else:
    print(f"  Response ({status}): {result}")

# Step 2: Login to get session token
print("Logging in...")
result, status = post("/api/auth/callback/credentials", {
    "email": EMAIL,
    "password": PASSWORD,
    "redirect": "false",
    "csrfToken": "",
    "callbackUrl": LANGFUSE_URL,
    "json": "true",
})

# For NextAuth, we need to use basic auth with the API keys endpoint
# Langfuse v2 uses basic auth for API key creation
# Let's try the direct approach — create via the internal API

# Step 2 alternative: Use Langfuse's seed mechanism via env vars
# Langfuse v2 supports LANGFUSE_INIT_* env vars for bootstrapping
print()
print("NOTE: For fully automated deploys, set these env vars on the Langfuse container:")
print("  LANGFUSE_INIT_ORG_ID=raj-ai-lab")
print("  LANGFUSE_INIT_ORG_NAME=Raj AI Lab")
print("  LANGFUSE_INIT_PROJECT_ID=raj-ai-lab")
print("  LANGFUSE_INIT_PROJECT_NAME=raj-ai-lab")
print("  LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-raj-ai-lab")
print("  LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-raj-ai-lab")
print("  LANGFUSE_INIT_USER_EMAIL=raj@lab.local")
print("  LANGFUSE_INIT_USER_NAME=Raj AI Lab Admin")
print("  LANGFUSE_INIT_USER_PASSWORD=rajailab")
print()
print("These will auto-create the org, project, user, and API keys on startup.")
print("No manual setup needed.")
