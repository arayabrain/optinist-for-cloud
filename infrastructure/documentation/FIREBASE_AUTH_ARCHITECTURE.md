# Firebase Authentication: Backend-Proxied Identity and Credential Delivery

## Executive Summary

- **Firebase Authentication is the only Firebase product used** by this project: no Firestore, no Realtime Database, no Firebase Hosting, no Cloud Messaging, no Firebase Storage.
- **Firebase owns credentials and identity only**: password hashes, the user **UID**, ID/refresh tokens, and transactional email. Roles, organizations, subscription tier, and quota live in MySQL.
- **The browser never talks to Firebase directly**: the FastAPI backend proxies every Firebase call, which is the single most surprising property of this codebase.
- **The Firebase UID is the join key**: `users.uid` is a unique column, and a user is only usable when the Firebase account and the MySQL row both exist and agree.
- **Firebase token verification can be switched off** with `USE_FIREBASE_TOKEN=False` for local development and standalone deployments, which is the reason the proxy design exists. Switching off the *email* path is a separate flag, `USE_FIREBASE_EMAIL=False`.
- **No Firebase custom claims are used**: authorization is decided entirely from the MySQL record found by UID.

### Re-verifying This Document

Many claims below, such as are "X does not exist" can go stale without anything failing, so re-run these before trusting the doc.

```bash
# The frontend has no Firebase dependency. This is what the whole design rests on.
grep -rn "REACT_APP_FIREBASE" --exclude-dir=node_modules . ; grep '"firebase"' frontend/package.json

# No Firebase emulator anywhere.
ls firebase.json .firebaserc 2>/dev/null ; grep -rn "FIREBASE_AUTH_EMULATOR_HOST\|firebase-tools" --exclude-dir=node_modules .

# The elaborate reset-email chain still has no callers.
grep -rn "AuthEmailService.send_password_reset_email\|send_password_reset_email_via_firebase" studio/ --include="*.py" | grep -v tests

# INITIAL_FIREBASE_UID is still unread by the application.
grep -rn "INITIAL_FIREBASE_UID" studio/

# Constants quoted in prose below still hold.
grep -rn "_CACHE_TTL_SECONDS\|max_workers" studio/app/common/core/auth/auth_helper.py
```

If any of these now returns something unexpected, fix the relevant section rather than working around it.

---

## Key Architectural Principles

1. **Backend-proxied authentication**
   - The frontend contains zero Firebase SDK code: `frontend/package.json` has no `firebase` dependency and no `REACT_APP_FIREBASE_*` variables exist.
   - The backend calls Firebase on the user's behalf with two libraries, `firebase_admin` (privileged) and `pyrebase4` (client REST wrapper run server-side).
   - Trade-off: the server receives the plaintext password on every login, because it must call `sign_in_with_email_and_password` itself. Token refresh is hand-rolled in `frontend/src/utils/axios.ts` rather than handled by an SDK.
   - Do not add the Firebase JS SDK to the frontend without a deliberate migration plan: standalone mode, the ALB premium-routing middleware, and the `ExToken` path all depend on the current shape.

2. **Split ownership between Firebase and MySQL**
   - Firebase is the authority for "can this person prove who they are".
   - MySQL is the authority for "what is this person allowed to do".
   - Disabling a user in the Firebase console blocks login but leaves the DB record and all data intact until deleted through the application.

3. **Credential files are the runtime contract, not environment variables**
   - Both libraries initialize from JSON files at fixed paths resolved in `studio/app/dir_path.py`.
   - Every environment differs only in how those two files get placed: by hand locally, from GitHub secrets in CI, from Secrets Manager on AWS.
   - The web API key is read out of `firebase_config.json` rather than from the environment, so email sending depends on that file being present.

4. **Firebase must be optional**
   - `pyrebase_app` is allowed to be `None` when `IS_STANDALONE` is set and `firebase_config.json` is missing.
   - With `USE_FIREBASE_TOKEN=False` the backend authenticates with a locally-signed HS256 JWT passed in the `ExToken` header.

5. **Firebase error strings are never surfaced raw**
   - The raw exception string from a failed Firebase REST call embeds the request URL, which contains the web API key.
   - `_extract_firebase_error()` parses the error body instead. A dedicated regression suite guards this.

---

## Architecture Overview

```
Browser  ---- email + password ---->  OptiNiSt Backend  ---- Firebase Auth REST ---->  Firebase
         <--- ID token + refresh ---  (FastAPI)         <--- UID + tokens -----------

Browser  ---- Bearer <ID token> --->  OptiNiSt Backend  ---- verify_id_token ------->  Firebase
                                          |
                                          +--> SELECT * FROM users WHERE uid = <firebase uid>
```

### Responsibility Matrix

| Responsibility                  | Firebase                       | MySQL / Stripe                          |
|---------------------------------|--------------------------------|-----------------------------------------|
| Password storage                | Yes - Exclusive                | No - passwords are never stored in the DB |
| UID issuance                    | Yes - Exclusive                | No - mirrored into `users.uid`          |
| ID and refresh token issuance   | Yes - Exclusive                | No                                      |
| Token signature verification    | Yes - via Admin SDK            | No                                      |
| Verification and reset email    | Yes - Identity Toolkit REST    | No                                      |
| Roles and admin flag            | No - no custom claims are used | Yes - `user_roles` joined to `roles`    |
| Organization membership         | No                             | Yes - `organization`                    |
| Subscription tier and plan      | No                             | Yes - MySQL plus Stripe                 |
| Storage quota                   | No                             | Yes                                     |
| Experiment and workflow data    | No                             | Yes - RDS, S3, EFS                      |

### Library Split

| Library          | Initialized in                             | Credential                                | Used for                                                                                                             |
|------------------|--------------------------------------------|-------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| `firebase_admin` | `studio/app/common/core/users/__init__.py` | `firebase_private.json` (service account) | `verify_id_token`, `create_user`, `update_user`, `delete_user`, `get_user`, `get_user_by_email`, `create_custom_token` |
| `pyrebase`       | `studio/app/common/core/auth/__init__.py`  | `firebase_config.json` (web config)       | `sign_in_with_email_and_password`, `refresh`, `send_password_reset_email`, `sign_in_with_custom_token`                 |

---

## Implementation Details

### Firebase UID to database user linkage

The link is a single unique string column in `studio/app/common/models/user.py`:

```sql
-- Key constraint: the Firebase UID is unique across all users
UniqueConstraint("uid", name="idx_uid")
```

Every authenticated request resolves to:

```sql
-- Key constraint: soft-deleted users must not authenticate
SELECT * FROM users WHERE uid = :firebase_uid AND active IS TRUE
```

`get_current_user()` then enriches that row with roles, subscription, and storage quota from MySQL. `get_admin_user()` gates on the DB-derived admin flag, not on any Firebase claim.

```
Firebase                    MySQL
--------                    -----
uid  -------------------->  users.uid  (UNIQUE; see the column in models/user.py)
email                       users.email      (duplicated copy)
email_verified              not mirrored, queried live via the Admin SDK
                            users.active     (soft-delete flag)
                            users.organization_id -> organization
                            user_roles -> roles     (admin flag lives HERE)
```

Deletion consistency across the two systems is tracked in MySQL by the `DeletionStep` enum in `studio/app/common/models/subscription.py`, which has `firebase_pending` and `firebase_deleted` states plus a stored Firebase UID used by `check_firebase_account_exists()`.

### Both systems must agree

| Firebase                 | MySQL                    | Result                                   |
|--------------------------|--------------------------|------------------------------------------|
| exists, verified         | exists, active           | Normal login                             |
| exists, not verified     | exists, active           | `403 Email address is not verified`      |
| exists                   | missing                  | `404 User not found`                     |
| missing                  | exists                   | Login fails at the pyrebase sign-in step |
| exists                   | exists, `active = false` | `404 User not found`                     |

This is the root cause of most "why can this account not log in" reports. Always check both sides.

Match on the **status code**, not the message text. The strings above are quoted from `auth.py` for recognisability, but they are prose and can be reworded without anything failing. The status codes are the part covered by `test_registrations_api_contract.py`, so those rot loudly.

### authenticate_user()

**File:** `studio/app/common/core/auth/auth.py`
**Purpose:** Exchange email and password for a token bundle, enforcing both the DB row and the Firebase email-verification state
**Input:** `db` (Session), `data` (UserAuth with email and password)
**Output:** `(Token, UserModel)`. The token bundle is `access_token` (the raw Firebase ID token, whose lifetime Firebase sets, currently one hour), `refresh_token` (an app-signed JWT wrapping the Firebase refresh token, lifetime from `REFRESH_TOKEN_EXPIRE_MINUTES`), and `ex_token` (the app's own HS256 JWT). Note the naming is counter-intuitive: `access_token` is Firebase's, `ex_token` is the one this app signs
**Calls:** `pyrebase_app.auth().sign_in_with_email_and_password()` -> DB lookup by `uid` -> `firebase_auth.get_user()` -> `create_access_token()` -> `create_refresh_token()`

Note that `firebase_auth.get_user()` runs before the DB-miss check is raised, so a missing DB row surfaces as `404` only after the Firebase round trip.

### create_user()

**File:** `studio/app/common/core/users/crud_users.py`
**Purpose:** Create the Firebase account and the MySQL row as one logical unit
**Input:** `db` (Session), registration payload
**Output:** Created user, or an HTTP error mapped from the Firebase error code
**Calls:** `firebase_auth.create_user()` -> DB insert with `uid=firebase_user.uid` -> `AuthEmailService.send_verification_email()`

Firebase error codes are mapped to HTTP status codes: `EMAIL_ALREADY_EXISTS`, `INVALID_EMAIL`, `WEAK_PASSWORD`, `OPERATION_NOT_ALLOWED`. If any step after account creation fails, a compensating `firebase_auth.delete_user()` runs so no orphaned Firebase account is left behind.

### Authenticated request path

**File:** `studio/app/common/core/auth/auth_helper.py`
**Purpose:** Turn a bearer token into a UID without paying the Firebase round trip on every request
**Input:** `Authorization: Bearer <ID token>`
**Output:** UID, or `401` with `WWW-Authenticate: Bearer realm="auth_required"`
**Calls:** process-local SHA256-keyed token cache lookup -> on miss, `firebase_auth.verify_id_token()` on the module-level `ThreadPoolExecutor` (see its `max_workers`) because the SDK call is synchronous -> UID resolved to the MySQL user, then cached on `request.state` for the remainder of the request

The cache lifetime is `_CACHE_TTL_SECONDS` in that file, currently 300 seconds. This is the only place in this document that states the value; everywhere else refers to "one cache TTL" so that changing the constant does not strand the prose. See Edge Case 2 for the operational consequence.

### Email sending

`AuthEmailService.send_verification_email()` falls back through Identity Toolkit REST, then pyrebase, then log-only, selected by `USE_FIREBASE_EMAIL` and whether `pyrebase_app` initialized. It is called from `studio/app/common/routers/registrations.py` and `crud_users.py`. Read the `if`/`elif`/`else` in that function for the live ordering rather than relying on this sentence.

`AuthEmailService.send_password_reset_email()` and `firebase_email_sender.send_password_reset_email_via_firebase()` implement the same fallback chain for resets, but as of this writing **nothing outside tests calls them**. The reset path that actually runs is the direct `pyrebase_app.auth().send_password_reset_email()` call in `auth.py::send_reset_password_mail()`, with no REST call and no fallback.

Before debugging a reset problem, confirm which path is live rather than trusting this paragraph, since a caller may have been added since:

```bash
grep -rn "send_password_reset_email" studio/ --include="*.py" | grep -v tests
```

Expect six hits, none of which is an external caller of the elaborate chain:

| Hit | What it is |
|-----|------------|
| `auth.py` inside `send_reset_password_mail()` | The live path, a direct pyrebase call |
| `auth_email_service.py` `def send_password_reset_email` | Entry point of the dead chain |
| `auth_email_service.py` import of `send_password_reset_email_via_firebase` | Import, not a call |
| `auth_email_service.py` call to `send_password_reset_email_via_firebase` | The dead chain's REST tier |
| `auth_email_service.py` call to `pyrebase_app.auth().send_password_reset_email` | The dead chain's pyrebase fallback tier |
| `firebase_email_sender.py` `def send_password_reset_email_via_firebase` | Bottom of the dead chain |

Note that four of the six sit inside the dead chain calling itself, which is why a raw hit count is misleading. What matters is whether anything **outside** `auth_email_service.py` and `firebase_email_sender.py` calls `AuthEmailService.send_password_reset_email`. A hit from a router or CRUD module means the chain is no longer dead and this section is stale.

### Admin impersonation

`POST /auth/proxy-login/{uid}` is gated by `Depends(get_admin_user)`. It calls `firebase_auth.create_custom_token(uid)` and then `pyrebase.sign_in_with_custom_token()`, returning a normal token bundle for the target user.

### API key leak protection

`auth.py::_extract_firebase_error()` reads the Firebase error **body** rather than stringifying the exception, because the raw exception string embeds the request URL and therefore the web API key. The regression suite is `studio/tests/app/common/core/auth/test_firebase_key_leak.py`. Preserve this behaviour when touching error handling in `authenticate_user()`, `refresh_current_user_token()`, `send_reset_password_mail()`, or either email sender.

---

## Edge Case Handling

### 1. Admin SDK Initialization Failure Is Silent

**Problem:** `studio/app/common/core/users/__init__.py` wraps `initialize_app()` in a bare `except Exception: pass`. A missing or malformed `firebase_private.json` produces no startup error, only blanket `401` responses on every authenticated request.

**Solution:**
- When "everything returns 401", check that `firebase_private.json` exists and parses before looking anywhere else.
- On AWS, confirm `cloud-startup.sh` actually fetched the secret: the file is written at container start, after which the container has no further dependency on Secrets Manager.

### 2. Disabled Users Stay Authenticated For One Cache TTL

**Problem:** The token-to-UID cache in `auth_helper.py` holds entries for `_CACHE_TTL_SECONDS`, so a disabled or deleted Firebase user, or a user whose password just changed, can keep authenticating until the entry expires. This is a deliberate performance trade-off, explained in a comment in that file.

This applies to **Firebase-side** changes only. Clearing `users.active` in MySQL takes effect on the next request, because the per-request query filters on `UserModel.active.is_(True)` and is never cached beyond the current request. If you need an immediate lockout, deactivating in the DB is the faster lever.

**Solution:**
- For a routine disable, accept the delay.
- To make Firebase-side revocation immediate, restart the backend tasks so the in-process cache is discarded. The cache is per process, so this means every task in every service, not one service: a single warm task keeps honouring the token.
- Or deactivate the DB row, which needs no restart.

### 3. Stale Firebase Account Breaks E2E Registration

**Problem:** The CI database starts empty while the Firebase project persists between runs. A leftover Firebase account makes registration return `400` without ever creating the MySQL row, so the whole suite fails at login.

**Solution:**
- `.github/scripts/e2e-bootstrap.sh` deletes the Firebase user by email before registering.
- Do the same by hand when this happens locally.

### 4. UIDs Are Not Portable Between Environments

**Problem:** Each environment has its own Firebase project. Accounts, UIDs, and the configured admin UID (`optinist_admin_uid` in each `.tfvars`) are meaningless across projects. A test account created for local development does not exist on the deployed dev site.

**Solution:**
- Treat access to one Firebase project as access to exactly that environment.
- When a login works locally but not on a deployed environment, confirm which project the running container's `firebase_config.json` points at before debugging anything else.

### 5. No Firebase Emulator Exists In This Repo

**Problem:** There is no `firebase.json`, no `.firebaserc`, no `firebase-tools`, and no `FIREBASE_AUTH_EMULATOR_HOST`. Local development and CI hit live Firebase projects.

**Solution:**
- Run with `USE_FIREBASE_TOKEN=False` **and** `USE_FIREBASE_EMAIL=False` whenever the test does not specifically exercise the Firebase path. `USE_FIREBASE_TOKEN=False` alone still sends registration email through the live project.
- Be aware of per-IP sign-in rate limits when running suites repeatedly.

---

## Configuration

### Credential Files

Both paths are resolved in `studio/app/dir_path.py` as `FIREBASE_PRIVATE_PATH` and `FIREBASE_CONFIG_PATH`:

| File                                       | Contents                                                                                     | Console source                                              |
|--------------------------------------------|----------------------------------------------------------------------------------------------|-------------------------------------------------------------|
| `studio/config/auth/firebase_config.json`  | Web app config: `apiKey`, `authDomain`, `databaseURL`, `projectId`, `storageBucket`, `messagingSenderId`, `appId`, `measurementId` | Project settings, General, Your apps, Web app, SDK setup     |
| `studio/config/auth/firebase_private.json` | Admin SDK service account JSON. Treat as a root credential                                    | Project settings, Service accounts, Generate new private key |

`studio/config/auth/.gitignore` ignores `*.json` except `*.example.json`, and `.dockerignore` excludes the same files from images, so neither can be committed or baked into a container by accident. Copy the two `*.example.json` files to start.

### How Each Environment Receives Them

| Environment         | Delivery mechanism                                                                                                                                                 |
|---------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Local development   | Developer places both files in `studio/config/auth/` by hand                                                                                                        |
| CI (GitHub Actions) | `.github/workflows/e2e.yml` writes them from the `E2E_FIREBASE_CONFIG_JSON` and `E2E_FIREBASE_PRIVATE_JSON` repository secrets                                       |
| AWS (ECS)           | `cloud-startup.sh` fetches `${ENV_PREFIX}-optinist/firebase/config` and `${ENV_PREFIX}-optinist/firebase/private-key` from Secrets Manager and creates both files. The image ships neither, so a failed fetch leaves the backend with no credentials at all |
| AWS (EC2 hosts)     | `infrastructure/scripts/app_setup.sh` does the equivalent, and additionally pip-installs `firebase-admin` and calls `auth.update_user(admin_uid, email_verified=True)` to bootstrap the admin account |

### Environment Variables

Firebase and auth environment variables (`USE_FIREBASE_TOKEN`, `USE_FIREBASE_EMAIL`, `IS_STANDALONE`, `FRONTEND_URL`, `SECRET_KEY`, `REFRESH_TOKEN_EXPIRE_MINUTES`) are documented in [AUTH_ROUTING_ARCHITECTURE.md](AUTH_ROUTING_ARCHITECTURE.md#firebase--auth-environment-variables).

Turning Firebase off is two independent switches, not one. To take it out of the picture completely:

```bash
USE_FIREBASE_TOKEN=False   # skip Firebase ID-token verification, use ExToken
IS_STANDALONE=True         # tolerate a missing firebase_config.json
USE_FIREBASE_EMAIL=False   # stop registration mail reaching the live project
```

The trap is `USE_FIREBASE_EMAIL`, which defaults to `True` in code. Setting only `USE_FIREBASE_TOKEN=False` still sends verification email through the live Firebase project and still needs `firebase_config.json` for the web API key. Check what the shipped example actually sets rather than trusting a copy of it here:

```bash
grep -E "USE_FIREBASE|IS_STANDALONE" studio/config/.env.example
```

### Terraform Variables

| Terraform variable          | Becomes                                                    |
|-----------------------------|------------------------------------------------------------|
| `firebase_config_json`      | Secrets Manager `${env}-optinist/firebase/config`          |
| `firebase_private_json`     | Secrets Manager `${env}-optinist/firebase/private-key`     |
| `optinist_admin_uid`        | The Firebase UID bootstrapped as the application admin     |
| `test_users[].firebase_uid` | Pre-existing Firebase accounts seeded as DB rows           |

The `.tfvars` file is the source of truth. Changing Firebase config means editing the tfvars, running `terraform apply`, and then restarting tasks so `cloud-startup.sh` re-fetches. IAM grants for both the EC2 instance role and the ECS task role are in `infrastructure/terraform/security.tf`. See [TERRAFORM_ARCHITECTURE.md](TERRAFORM_ARCHITECTURE.md#how-firebase-configuration-flows) for the full flow.

`INITIAL_FIREBASE_UID` is set on the ECS task definitions but, as of this writing, read by nothing in `studio/`. Confirm with the grep in "Re-verifying This Document" before assuming it is still unused; if you are looking for what bootstraps the admin account, that is `app_setup.sh`, not this variable.

---

## Operational Procedures

### A User Cannot Log In

```bash
# 1. Does the Firebase account exist, and is the email verified?
curl "https://<host>/api/register/verify-status/<email>"

# Expected: {"email_verified": true, "uid": "..."}
# 404 means no Firebase account for that address.
```

Use `https`, not `http`: the address sits in the URL path and the response carries the UID. This endpoint is unauthenticated, which makes it convenient for triage and also an account-existence and UID enumeration surface for anyone else, since `404` and `200` are distinguishable. A leaked UID is not by itself a premium-routing bypass, because routing IDs are `HMAC(routing_secret_key, uid)` and the secret is not exposed, but the endpoint is a candidate for rate limiting or an internal-only path.

```sql
-- 2. Does the MySQL row exist and is it active?
SELECT id, uid, email, active FROM users WHERE email = '<email>';
```

Interpret the pair using the "Both systems must agree" table above.

### Force-Verify An Account (Development And Test Only)

```python
import firebase_admin
from firebase_admin import auth, credentials

firebase_admin.initialize_app(
    credentials.Certificate("studio/config/auth/firebase_private.json")
)
auth.update_user(auth.get_user_by_email("test@example.com").uid, email_verified=True)
```

The same pattern is used by `.github/scripts/e2e-bootstrap.sh` and `infrastructure/scripts/create_test_users.py`.

### Rotate The Service Account Key

`<env>` throughout is the Terraform `environment` value, **not** a friendly name. Read it out of the tfvars rather than guessing, because production is `subscr`:

```bash
grep '^environment' infrastructure/terraform/environments/<file>.tfvars
# development.tfvars -> development     production.tfvars -> subscr
```

1. Firebase console, Project settings, Service accounts, Generate new private key.
2. Paste the JSON into `firebase_private_json` in the relevant `.tfvars`.
3. `terraform init -backend-config=backends/<env>.hcl -reconfigure`
4. `terraform apply -var-file=environments/<env>.tfvars` to publish a new Secrets Manager version.
5. Restart **every** service in the cluster so `cloud-startup.sh` re-fetches. Cycling only the main service leaves premium, public, and background tasks holding the old key on disk. Discover the list rather than typing it, so a newly added service is covered automatically:

```bash
CLUSTER=<env>-optinist-cloud-cluster
REGION=ap-northeast-1

SERVICES=$(aws ecs list-services --cluster "$CLUSTER" --region "$REGION" \
  --query 'serviceArns[]' --output text)
echo "Cycling: $SERVICES"

for SERVICE in $SERVICES; do
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --force-new-deployment --region "$REGION" >/dev/null
done
```

6. Wait for every service to reach a steady state. Each must report exactly one deployment before you continue:

```bash
aws ecs describe-services --cluster "$CLUSTER" --region "$REGION" \
  --services $SERVICES \
  --query 'services[].{name:serviceName,deployments:length(deployments),running:runningCount}'
```

7. Update the `E2E_FIREBASE_PRIVATE_JSON` repository secret if CI uses the same project.
8. Redistribute to developers' local `studio/config/auth/firebase_private.json`.
9. Only once step 6 shows a single deployment per service, delete the old key in the console. Rotation is not complete until the old key is gone, because old keys stay valid until explicitly deleted.

Deleting the old key before every service has cycled breaks Admin SDK calls on the services still holding it, including the `firebase_auth.get_user()` call on the login path. That is a login outage for the affected tiers, not a delayed background failure. At the time of writing the cluster runs four services (main, premium, public, background), but trust `list-services` over that count. See [INFRA_DEPLOYMENT_PROCEDURE.md](INFRA_DEPLOYMENT_PROCEDURE.md).

### Disable A User Immediately

Firebase console, Authentication, Users, then Disable account. That alone can take up to one token-cache TTL to bite. To lock the account out on the next request instead, clear `users.active` in MySQL, which bypasses the cache entirely. Restarting tasks also works, but must cover every task in every service to be effective, so the DB route is usually the faster lever.

---

## Firebase Console Reference

Sign in at https://console.firebase.google.com/ and select the project for the environment being worked on. Firebase projects are Google Cloud projects underneath, so the same IAM is visible at https://console.cloud.google.com/iam-admin/iam.

The navigation paths below are Google's UI, which is reorganized without warning and is the most rot-prone content in this document. Treat them as "roughly where to look", and search the console for the feature name if a path does not match what you see. The concepts (users, sign-in providers, email templates, authorized domains, service accounts, project members) are stable even when the menus are not.

| Console location                                       | Used for                                                                                                        |
|--------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| Build, Authentication, Users                           | Look up a user by email, read the UID, disable or enable an account, delete an account, flip `email_verified`     |
| Build, Authentication, Sign-in method                  | Confirm the Email/Password provider is enabled. If it is disabled, every login breaks                            |
| Build, Authentication, Templates                       | Verification and reset email bodies, sender name, reply-to. The action-link target comes from here, not `FRONTEND_URL` |
| Build, Authentication, Settings, Authorized domains    | Domains allowed to appear in action links                                                                        |
| Project settings, General                              | The web app config that `firebase_config_json` carries                                                           |
| Project settings, Service accounts                     | Generate the Admin SDK key that `firebase_private_json` carries                                                  |
| Project settings, Users and permissions                | Member and role management                                                                                       |

### Access Roles

Because only Firebase Authentication is used, the product-level roles are the tightest sensible grants. Prefer them over the basic Owner, Editor, and Viewer roles, which span the entire underlying Google Cloud project. Confirm the exact role IDs below in the IAM console before relying on them in a script; Google's predefined role catalogue changes independently of this repository.

| Role                            | ID                          | Grants                                                                                  |
|---------------------------------|-----------------------------|-----------------------------------------------------------------------------------------|
| Firebase Authentication Admin   | `roles/firebaseauth.admin`  | Create, read, update, delete users, and manage sign-in providers and config              |
| Firebase Authentication Viewer  | `roles/firebaseauth.viewer` | Read user records and auth config only. Enough for support triage                        |
| Firebase Admin                  | `roles/firebase.admin`      | Full read and write on all Firebase products, plus project-member management             |
| Firebase Viewer                 | `roles/firebase.viewer`     | Read-only on all Firebase products                                                       |

Grant `roles/firebaseauth.admin` on non-production projects to engineers who need to fix stuck test accounts. Default to `roles/firebaseauth.viewer` on production and reserve admin and owner for the people who run releases. Keep at least two owners on the production project so a single unavailable person cannot block access recovery.

Members are added in the console under Project settings, Users and permissions, Add member. Granting to a Google Group rather than to individuals keeps joiners and leavers out of Firebase IAM entirely. The equivalent CLI calls:

```bash
gcloud projects add-iam-policy-binding <FIREBASE_PROJECT_ID> \
  --member="user:<email>" --role="roles/firebaseauth.admin"

gcloud projects remove-iam-policy-binding <FIREBASE_PROJECT_ID> \
  --member="user:<email>" --role="roles/firebaseauth.admin"
```

Removing Firebase access is one step of offboarding, not all of it. Also revoke AWS IAM credentials, access to the store holding the `.tfvars` files, Stripe dashboard access, and repository access. Because a `.tfvars` file contains live secrets in plaintext and is copied to each engineer's machine, treat a non-amicable departure as requiring rotation of the Firebase service account key, the database passwords, `optinist_secret_key`, `routing_secret_key`, and the Stripe keys, rather than console revocation alone.

---

## Testing

| Suite                                                                | Covers                                                              |
|----------------------------------------------------------------------|---------------------------------------------------------------------|
| `studio/tests/app/common/core/auth/test_firebase_key_leak.py`        | The web API key never reaches logs or responses on any error path    |
| `studio/tests/app/common/routers/test_registrations_api_contract.py` | Registration and verification endpoint response contracts            |
| `.github/workflows/e2e.yml`                                          | End-to-end runs against a live Firebase project, credentials from repository secrets |

---

## Key Functions Reference

### Backend Auth (`studio/app/common/core/auth/auth.py`)

| Function | Purpose |
|----------|---------|
| `authenticate_user()` | Email and password to token bundle, enforcing DB row and email verification |
| `refresh_current_user_token()` | Unwrap the app refresh JWT and exchange it for a new Firebase ID token |
| `send_reset_password_mail()` | The reset path that actually runs, a direct pyrebase call |
| `login_with_uid()` | Admin impersonation via a Firebase custom token |
| `_extract_firebase_error()` | Read the error body so the request URL, and the API key in it, never leak |

### Auth Helper (`studio/app/common/core/auth/auth_helper.py`)

| Function | Purpose |
|----------|---------|
| `extract_uid_from_firebase_credential()` | Bearer ID token to UID, through the TTL cache |
| `extract_uid_from_firebase_jwt()` | UID extraction used by the secure routing middleware |
| `extract_uid_from_jwt_token()` | `ExToken` path used when `USE_FIREBASE_TOKEN=False` |

### User CRUD (`studio/app/common/core/users/crud_users.py`)

| Function | Purpose |
|----------|---------|
| `create_user()` | Firebase account plus MySQL row, with a compensating delete on failure |
| `delete_user()` | Removes both sides, tracked by the `DeletionStep` enum |
| `check_firebase_account_exists()` | Reconciliation check for records stuck in `firebase_pending` |

### Endpoints

| Endpoint | Notes |
|----------|-------|
| `POST /api/register` | Unauthenticated, creates both sides |
| `GET /api/register/verify-status/{email}` | Unauthenticated verification-state lookup |
| `POST /api/register/resend-verification` | Re-sends the verification email |
| `POST /auth/login` | Returns `access_token`, `refresh_token`, `ex_token` |
| `POST /auth/refresh` | Called by the frontend 401 interceptor |
| `POST /auth/send_reset_password_mail` | Query parameter `email` |
| `POST /auth/proxy-login/{uid}` | Admin only |

### Related Documents

| Document | Covers |
|----------|--------|
| [AUTH_ROUTING_ARCHITECTURE.md](AUTH_ROUTING_ARCHITECTURE.md) | Token lifecycle, subscription context, SPA routing, auth environment variables |
| [TERRAFORM_ARCHITECTURE.md](TERRAFORM_ARCHITECTURE.md) | How Firebase config reaches Secrets Manager and the running containers |
| [INFRA_DEPLOYMENT_PROCEDURE.md](INFRA_DEPLOYMENT_PROCEDURE.md) | Deployment and service-cycling procedure |
| [ALB_ROUTING_ARCHITECTURE.md](ALB_ROUTING_ARCHITECTURE.md) | Routing IDs derived from the Firebase UID |
