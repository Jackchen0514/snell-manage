# Snell Manager API

HTTP API for managing multiple Snell v5 proxy instances.

## Security model

Two independent layers:

| Layer | Mechanism | Purpose |
|-------|-----------|---------|
| URL prefix | Random 16-char hex path segment | Hides the API from port scanners |
| Bearer token | 64-char hex token in `Authorization` header | Authenticates each request |

Both are generated together by `snell-manage keygen` and stored in `/etc/snell/`.

---

## Setup

```bash
# 1. Install snell-server binary
snell-manage install

# 2. Install API service (Python deps + systemd)
snell-manage api-install

# 3. Generate URL prefix + Bearer token
snell-manage keygen
```

Sample output of `keygen`:

```
Prefix:    3f9a1c2d8b4e7f05
Token:     a1b2c3d4...64-char-hex...

Base URL:    http://1.2.3.4:2086/3f9a1c2d8b4e7f05
Swagger UI:  http://1.2.3.4:2086/3f9a1c2d8b4e7f05/docs
Auth header: Authorization: Bearer a1b2c3d4...
```

To view credentials at any time:

```bash
snell-manage keyshow
```

To rotate (generates new prefix AND token, restart required):

```bash
snell-manage keygen
systemctl restart snell-api
```

---

## Base URL

```
http://<server>:2086/<prefix>
```

Interactive docs (Swagger UI):

```
http://<server>:2086/<prefix>/docs
```

---

## Authentication

Every request must include:

```
Authorization: Bearer <token>
```

Responses on auth failure:

| HTTP | Reason |
|------|--------|
| 401  | Header missing or malformed |
| 403  | Token incorrect |
| 503  | Token file not initialised |

---

## Endpoints

### Setup

#### `POST /<prefix>/install`
Download and install the snell-server binary and systemd template unit.

**Response `200`**
```json
{ "message": "snell-server installed to /usr/local/bin/snell-server" }
```

---

### Users

#### `GET /<prefix>/users`
List all users.

**Response `200`**
```json
[
  { "username": "alice", "port": 8388, "psk": "abc123", "status": "running" },
  { "username": "bob",   "port": 8389, "psk": "xyz789", "status": "stopped" }
]
```

---

#### `POST /<prefix>/users`
Add a new user. Port is auto-assigned from 8388 when omitted.

**Request body**
```json
{
  "username": "alice",
  "port": 8388
}
```

**Response `201`**
```json
{ "username": "alice", "port": 8388, "psk": "generated-psk", "status": "running" }
```

**Errors**

| Code | Reason |
|------|--------|
| 400  | Invalid username or port |
| 409  | Username or port already in use |
| 503  | snell-server not installed |

---

#### `GET /<prefix>/users/{username}`
Get a single user.

**Response `200`**
```json
{ "username": "alice", "port": 8388, "psk": "abc123", "status": "running" }
```

---

#### `DELETE /<prefix>/users/{username}`
Stop, disable, and remove a user.

**Response `204` No Content**

---

#### `POST /<prefix>/users/{username}/{action}`
Control a user's instance. `action` ∈ `start` | `stop` | `restart`.

**Response `200`**
```json
{ "message": "restarted snell@alice" }
```

---

### Surge

#### `GET /<prefix>/surge`
Surge proxy lines for all users.

**Response `200`**
```json
[
  { "username": "alice", "line": "alice = snell, 1.2.3.4, 8388, psk=abc123, version=5" },
  { "username": "bob",   "line": "bob   = snell, 1.2.3.4, 8389, psk=xyz789, version=5" }
]
```

---

#### `GET /<prefix>/surge/{username}`
Surge proxy line for one user.

**Response `200`**
```json
{ "username": "alice", "line": "alice = snell, 1.2.3.4, 8388, psk=abc123, version=5" }
```

### Quota

#### `GET /<prefix>/quota`
List quota for all users.

**Response `200`**
```json
[
  {
    "username": "alice",
    "limit": 107374182400,
    "limit_human": "100.0 GB",
    "used": 53687091200,
    "used_human": "50.0 GB",
    "plan": "monthly",
    "expire": "2026-05-02",
    "next_reset": "2026-05-02",
    "blocked": false,
    "block_reason": null
  }
]
```

---

#### `GET /<prefix>/quota/{username}`
Get quota for a single user.

**Response `200`** — same schema as above.

**Errors**

| Code | Reason |
|------|--------|
| 404  | No quota set for this user |

---

#### `POST /<prefix>/quota/{username}`
Set or update quota. Accepts human-readable sizes: `100GB`, `50G`, `1TB`, `512MB`.

**Request body**
```json
{
  "limit": "100GB",
  "plan": "monthly"
}
```

`plan` ∈ `monthly` | `quarterly` | `yearly`

**Response `201`** — quota object.

**Errors**

| Code | Reason |
|------|--------|
| 400  | Invalid plan value |
| 404  | User does not exist |

---

#### `DELETE /<prefix>/quota/{username}`
Remove quota limit (user becomes unlimited).

**Response `204` No Content**

---

#### `POST /<prefix>/quota/{username}/reset`
Reset this month's usage counter to 0 and unblock user (if blocked by quota).

**Response `200`** — updated quota object.

---

#### `POST /<prefix>/quota/{username}/renew`
Renew subscription. Extends `expire` by one plan period from today.

**Request body** (all fields optional)
```json
{
  "plan": "yearly"
}
```

Omit `plan` to renew using the existing plan.

**Response `200`** — updated quota object.

---

## curl examples

```bash
PREFIX="3f9a1c2d8b4e7f05"
TOKEN="a1b2c3d4..."
BASE="http://localhost:2086/$PREFIX"
AUTH="Authorization: Bearer $TOKEN"

# List users
curl -s -H "$AUTH" $BASE/users | jq

# Add user (auto port)
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"username":"alice"}' $BASE/users | jq

# Add user (fixed port)
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"username":"bob","port":9000}' $BASE/users | jq

# Restart a user
curl -s -X POST -H "$AUTH" $BASE/users/alice/restart | jq

# Delete a user
curl -s -X DELETE -H "$AUTH" $BASE/users/alice

# Get all Surge lines
curl -s -H "$AUTH" $BASE/surge | jq -r '.[].line'

# ── Quota ──────────────────────────────────────────────────────────────────────

# Set quota: 100GB/month, monthly plan
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"limit":"100GB","plan":"monthly"}' $BASE/quota/alice | jq

# Set quota: 100GB/month, quarterly plan
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"limit":"100GB","plan":"quarterly"}' $BASE/quota/alice | jq

# Set quota: 100GB/month, yearly plan
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"limit":"100GB","plan":"yearly"}' $BASE/quota/alice | jq

# View quota for one user
curl -s -H "$AUTH" $BASE/quota/alice | jq

# View quota for all users
curl -s -H "$AUTH" $BASE/quota | jq

# Reset this month's usage (unblocks user)
curl -s -X POST -H "$AUTH" $BASE/quota/alice/reset | jq

# Renew subscription (same plan)
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{}' $BASE/quota/alice/renew | jq

# Renew and upgrade plan
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"plan":"yearly"}' $BASE/quota/alice/renew | jq

# Remove quota limit
curl -s -X DELETE -H "$AUTH" $BASE/quota/alice
```
