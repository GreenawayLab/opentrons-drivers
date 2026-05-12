# opentrons_control

Control plane for Opentrons robots. Provides a single internet-reachable
HTTP entry point (the **proxy**) that fronts a session-aware **backend**,
which in turn drives agents running on each robot over SSH (bootstrap)
and HTTP (runtime).

## Architecture

```
                          ┌─────────┐
   External clients ─────▶│  Proxy  │◀──── Lab UI uploads
                          └────┬────┘
                               │     control plane (HTTP)
                               ▼
                          ┌─────────┐
                          │ Backend │ ── SSH bootstrap ──┐
                          └────┬────┘                    │
                               │ HTTP forwards           │
                               ▼                         ▼
                       ┌───────────────┐         ┌────────────┐
                       │  Agent (OT-3) │ ◀────── │  Robot OS  │
                       └───────────────┘         └────────────┘
```

Three components, two networks:

- **proxy**: thin HTTP forwarder. Holds no business logic. Receives every
  external request, delegates session lifecycle calls to the backend,
  and routes action calls to the right agent. The only externally bound
  service.
- **backend**: control plane. Owns the session registry, drives SSH
  bootstrap of agents, and (eventually) hosts the manual-protocol
  runner. Reachable only by the proxy.
- **agents**: one per robot. Boot via `opentrons_execute`, listen on
  HTTP for action submissions. Out of scope of this repository.

The two docker networks are:

- `internal`: proxy ↔ backend. Marked `internal: true` so containers on
  it have no external connectivity.
- `robot_net`: proxy ↔ agents (and backend ↔ agents for bootstrap SSH).
  Bridges to the isolated subnet on which the robots live.

## Session lifecycle

```
client ──POST /sessions──▶ proxy ──POST /internal/sessions──▶ backend
                                                                │
                                                                ▼
                                                         SSH bootstrap
                                                          (60–90 s)
                                                                │
                                                                ▼
                                                     await agent /health
                                                                │
                                              ◀── 201 {token} ──┘
```

After the session is `active`, action calls flow through the proxy:

```
client ──POST /actions [Bearer: token]──▶ proxy
                                            │
                                            ▼
                          GET /internal/sessions/{token} on backend
                          (per request; no caching)
                                            │
                                            ▼
                          POST /actions on agent at returned URL
                                            │
                                ◀── upstream response verbatim ──
```

Abort tears down the session:

```
client ──DELETE /sessions/{token}──▶ proxy ──POST /internal/sessions/{token}/abort──▶ backend
                                                                                       │
                                                                                       ▼
                                                                          agent /abort + lock release
```

## Configuration

The backend is initialised from a JSON config file pointed at by the
`BACKEND_CONFIG` env var (default `/data/backend.json`).

```json
{
  "secrets": {
    "keys_dir": "/data/access"
  },
  "robots": {
    "ot-3": {
      "host": "10.0.0.3",
      "user": "root",
      "key_name": "ot3_id_ed25519",
      "agent_port": 9000
    },
    "ot-4": {
      "host": "10.0.0.4",
      "user": "root",
      "key_name": "ot4_id_ed25519"
    }
  }
}
```

`keys_dir` and `key_name` are resolved together at startup into absolute
key paths. The library itself never sees the raw key names — only fully
resolved `Robot` objects. Replace `__main__.py`'s loader with a different
mechanism (env-driven, secrets manager, etc.) if your deployment demands
it; `api.py` is config-source agnostic.

Recommended layout for the mounted `/data` directory:

```
/data/
├── backend.json
└── access/
    ├── ot3_id_ed25519
    └── ot4_id_ed25519
```

Keys must be readable by the backend user inside the container. Mount
the directory read-only.

## Proxy environment

| Variable        | Default                | Notes                                              |
|-----------------|------------------------|----------------------------------------------------|
| `BACKEND_URL`   | `http://backend:8000`  | Where the proxy reaches the backend.               |
| `PROXY_TIMEOUT` | `200`                  | Per-request outbound timeout in seconds.           |

The 200 s timeout exists to cover the long-poll on `POST /sessions`,
which blocks until the agent reports healthy (~60–90 s typical). Tighten
it only if you also tighten `DEFAULT_READINESS_TIMEOUT` in
`backend/app/launcher.py`.

## Backend environment

| Variable          | Default              | Notes                              |
|-------------------|----------------------|------------------------------------|
| `BACKEND_CONFIG`  | `/data/backend.json` | Path to the config file.           |
| `BACKEND_HOST`    | `0.0.0.0`            | uvicorn bind host.                 |
| `BACKEND_PORT`    | `8000`               | uvicorn bind port.                 |

## Endpoints

### Proxy (external)

| Method | Path                  | Description                                       |
|--------|-----------------------|---------------------------------------------------|
| POST   | `/sessions`           | Create a session. Body forwarded to the backend.  |
| DELETE | `/sessions/{token}`   | Abort a session.                                  |
| POST   | `/actions`            | Submit an action to the session's agent.          |
| GET    | `/actions/current`    | Current slot view of the session's agent.         |
| GET    | `/actions/{job_id}`   | Snapshot of a specific job.                       |
| GET    | `/health`             | Proxy liveness.                                   |

Action endpoints require an `Authorization: Bearer <token>` header. The
token is returned in the response body of `POST /sessions`.

### Backend (internal, proxy-only)

| Method | Path                                          | Description                          |
|--------|-----------------------------------------------|--------------------------------------|
| POST   | `/internal/sessions`                          | Acquire robot, bootstrap, return token. |
| GET    | `/internal/sessions/{token}`                  | Route lookup. Returns `RouteTarget`. |
| GET    | `/internal/sessions/{token}/details`          | Full session view (debug/admin).     |
| POST   | `/internal/sessions/{token}/abort`            | Mark aborting, kill agent, release.  |
| GET    | `/robots`                                     | List configured robots.              |
| POST   | `/manual/protocols`                           | **501** — not implemented.           |
| GET    | `/health`                                     | Backend liveness.                    |

## Session-creation payload

```json
{
  "robot_id": "ot-3",
  "protocol_name": "screen_round_2",
  "mode": "auto",
  "postbox": {
    "base_config.json": { /* BaseConfig */ },
    "plate_round.json": { /* labware definition */ }
  },
  "client_id": "optional-client-identifier"
}
```

The `postbox` map is opaque to the launcher: keys become filenames in
the agent's `postbox/` directory on the OT; values are JSON-serialised
verbatim. Only `.json` filenames are supported in this revision. Whether
`base_config.json` is the only entry or there are twenty calibration
files, the same code path handles it.

## Running locally

```sh
docker compose up --build
```

The proxy is reachable on `https://localhost/`; the backend is not
externally exposed.

## PoC scope and known limitations

This is a proof-of-concept revision. The following are explicitly out of
scope and will need attention before any production use:

- **Manual mode is a stub.** `POST /manual/protocols` returns 501. The
  slicing pipeline that converts uploaded instruction documents into a
  queue of actions does not exist yet.
- **No agent-side abort endpoint.** `OTClient.abort()` is implemented
  against the expected contract, but the agent's HTTP layer does not yet
  have `POST /abort`. End-to-end abort will work once that lands.
- **No persistence.** The session registry is in-memory. Backend restart
  drops all session state; orphan agents on robots require manual
  cleanup (SSH in, kill the `opentrons_execute` process).
- **No authentication.** External requests are not authenticated by the
  proxy. The proxy → backend hop relies on the `internal` docker network
  as a trust boundary.
- **No retry, no rate limiting, no graceful drain.** Single failures
  fail; concurrent abuse is not handled.
- **`launch_id` is second-resolution.** Two launches of the same
  protocol within the same second on the same robot will collide on
  disk. Not a real concern at expected PoC throughput.
- **Long-poll on session creation.** `POST /sessions` holds the
  connection open for up to ~3 minutes. Clients must set a generous
  timeout. The alternative async pattern (return 202 with a token,
  client polls for `active`) is intentionally not implemented in this
  revision.

## Layout

```
opentrons_control/
├── backend/
│   ├── app/
│   │   ├── __main__.py        startup harness; config loader
│   │   ├── api.py             FastAPI factory
│   │   ├── bootstrap.py       SSH/SCP transport, agent process launch
│   │   ├── launcher.py        end-to-end session bootstrap orchestration
│   │   ├── ot_client.py       async HTTP client for agent
│   │   └── sessions.py        Robot, Session, SessionRegistry
│   └── Dockerfile
├── proxy/
│   ├── app/
│   │   └── main.py            FastAPI forwarder
│   └── Dockerfile
├── frontend/
├── data/                       mounted at runtime; contains backend.json + access/
├── docker-compose.yml
└── README.md
```