# Project Guardrails: InfraWatch Monitoring Stack (v3)

This document serves as the authoritative technical specification and architectural guardrail for **InfraWatch v3**. Any future AI agent or human developer working on this codebase MUST read, understand, and strictly adhere to these guardrails before making code modifications.

---

## 1. Project Identity

- **Official Name:** InfraWatch (Infrastructure Monitoring Stack v3 / InfraWatch NOC Console).
- **Primary Repository:** `infra-monitoring-stack-v3`.
- **Target Audience:** Network Operation Center (NOC) Engineers, SysAdmins, IT Incident Responders.
- **Display Target:** Enterprise NOC TV Wallboards (High-contrast, long-distance readability, automated audio sirens).
- **Core Technology Stack:**
  - **Backend:** Python 3.12 (Docker Container) / Flask 3.1.0, Gunicorn 23.0.0 (`Dockerfile` L1-17, `requirements.txt` L1-2).
  - **Metrics Engine:** Prometheus v2.54.1 time-series evaluation engine (`docker-compose.yml` L11, `README.md` L156).
  - **Probe Engine:** Blackbox Exporter v0.25.0 HTTP/HTTPS & ICMP Ping availability probe (`README.md` L49, L160).
  - **Frontend:** Vanilla HTML5, Vanilla JavaScript (`alarm.js`), Vanilla CSS (`noc.css`), Jinja2 (`alarm.html`).

---

## 2. Core Purpose

### Primary Objective
InfraWatch v3 is built to provide **real-time, zero-clutter network service availability monitoring** tailored specifically for continuous TV display in Network Operation Center (NOC) rooms.

### Problem Solved
In v2, internal hardware metrics (CPU, RAM, Swap, Disk Space, Disk I/O) from Prometheus *Node Exporter* created visual noise on main NOC wallboards and consumed massive backend memory. InfraWatch v3 intentionally eliminates host hardware metric telemetry to focus 100% on **Blackbox Probe Availability (Status UP/DOWN, Latency in ms, HTTP Status Code, ICMP Ping)** (`README.md` L27-40).

### Core Functions (The Reason This Project Exists)
1. **Real-time Visual Beacons:** High-contrast status cards (Green = Online, Yellow = Warning/Maintenance, Red = Critical Down) visible from distance on TV screens (`alarm.html` L86-90, `alarm.js` L1150-1250).
2. **Automated MP3 Audio Alarm Siren:** Immediate MP3 siren playback upon detecting any `CRITICAL` target downtime (`alarm.html` L18-50, `alarm.js` L4050-4100).
3. **1-Click Alarm Acknowledgment:** Instant wallboard button to silence audio sirens during active incident coordination (`templates/alarm.html` L92-97, `alarm.js` L4080-4120).
4. **Enterprise SLA & Availability Analytics:** Precise calculation of 4 availability metrics (`fleet_aggregate` weighted SLA, `fleet_average`, `health_ratio`, `per_server`) (`fleet_availability.py` L1-120, `app.py` L1220-1418).
5. **Maintenance Window Suppression:** Planned downtime scheduling (`/api/maintenance`) to prevent false siren alarms and unneeded log pollution (`app.py` L398-434).
6. **Alert Correlation & Cascade Suppression:** Parent-child dependency tree (`/api/dependencies`) to de-emphasize downstream hosts when a core gateway is down (`app.py` L845-860).

---

## 3. Core Architecture

### Component Map
```text
Target Infrastructure (HTTP/HTTPS, ICMP Ping, Ports)
       │
       ▼
Blackbox Exporter Probe Engine (v0.25.0)
       │
       ▼
Prometheus Time-Series Engine (:9090) (Scrape: 2s)
       │
       ├──────────────────────────────────────────┐
       ▼                                          ▼
Synthetic Alert Poller (15s)          Alertmanager Webhook (Option)
(Prometheus Probe Fallback)           (External Receiver /webhook)
       │                                          │
       └────────────────────┬─────────────────────┘
                            ▼
       InfraWatch Engine Backend (Python Flask - app.py)
       ├── Single-Flight Query Lock (_FETCH_LOCKS)
       ├── Failover Prometheus Pool (PROMETHEUS_CANDIDATES)
       ├── Maintenance Window Suppression (/api/maintenance)
       ├── Alert Correlation Engine (/api/dependencies)
       ├── Fleet SLA & Availability Engine (fleet_availability.py)
       └── Thread-Safe State Retention (_WEBHOOK_LOCK)
                            │
                            ▼
       InfraWatch TV Wallboard Console (http://localhost:5000)
       ├── Audio Consent Splash Overlay & Autoplay Permission
       ├── High-Contrast Visual Beacon (Healthy / Critical)
       ├── Live Response Time (ms) & HTTP Status Code
       ├── 1-Click Acknowledge Alarm & Audio Mute
       └── CSV Incident Report Exporter
```

### Key Subsystems & Dependencies
- **Prometheus Candidate Pool (`PROMETHEUS_CANDIDATES`, `load_endpoints()`):** Maintains a priority-ordered pool of candidate Prometheus URLs to execute failover when the primary endpoint disconnects (`app.py` L143-181, L353-364).
- **Single-Flight Lock (`_FETCH_LOCKS`) & Query Cache (`PROMETHEUS_CACHE`):** Coalesces concurrent PromQL calls and caches responses for 8 seconds with periodic 30-second background pruning (`app.py` L272-315, L344-382).
- **Synthetic Alert Poller Thread (`start_alert_poller`):** A daemon thread ticking every 15s (`ALERT_POLL_INTERVAL_SECONDS = 15`) that queries `probe_success` and synthesizes alert events when no external Alertmanager is configured (`app.py` L1686-1837).
- **Authoritative Webhook Backoff (`_LAST_WEBHOOK_AT`):** Pauses the synthetic poller for 120s (`WEBHOOK_ACTIVE_WINDOW_SECONDS = 120`) whenever a POST arrives at `/webhook` (`app.py` L525, L1752).

---

## 4. End-to-End Data Flow

```text
[ Target Server / Host ]
          │ (Probed via HTTP/ICMP)
          ▼
[ Blackbox Exporter ]
          │ (Exposes probe_* metrics)
          ▼
[ Prometheus Engine ] ──(Every 2s scrape)
          │
          ├──> API /api/v1/query & /api/v1/targets
          │          │
          │          ▼
          │    [ InfraWatch Backend (app.py) ]
          │          ├── fetch_prometheus_json() (Guarded by _FETCH_LOCKS & PROMETHEUS_CACHE)
          │          ├── Synthetic Alert Poller Thread (15s tick)
          │          │     ├── compute_state_transitions()
          │          │     └── record_alert_event() (Guarded by _WEBHOOK_LOCK)
          │          │           ├── Checks get_active_maintenance()
          │          │           └── Writes status.json, logs.json, history.json
          │          │
          │          ├── GET /instances
          │          │     ├── Classifies failures via classify_scrape_failure()
          │          │     ├── Applies maintenance via get_active_maintenance()
          │          │     └── Applies correlation via apply_correlation_suppression()
          │          │
          │          └── GET /api/availability
          │                └── Calculates SLA metrics via fleet_availability.py
          │
          ▼
    [ NOC Wallboard UI (alarm.js) ] ──(Polls /instances every 5s, /status every 5s)
          ├── Updates status pills, summary counters, & host grid cards
          └── Checks status.json: if CRITICAL & !muted & !acknowledged ──> Plays alarm.mp3
```

---

## 5. Critical Components

1. **`app.py` (Backend Monolith):** Handles routing, PromQL query caching, single-flight locking, synthetic alert polling, error classification, and state file serialization.
2. **`fleet_availability.py` (SLA Engine):** Calculates weighted `fleet_aggregate`, unweighted `fleet_average`, `health_ratio`, and per-server availability.
3. **`alarm.js` (Frontend Controller):** Manages live polling timers, DOM card rendering, audio autoplay consent, alarm mute/ack state, and sparkline graphs.
4. **`status.json` (Primary Alarm State):** Stores global system state (`NORMAL`, `WARNING`, `CRITICAL`) and active firing alerts list.
5. **`targets/websites.yml` (Dynamic File SD):** Prometheus `file_sd_configs` target list dynamically edited via `/api/targets`.

---

## 6. DO NOT BREAK List

### 🔴 Critical (Modifying will cause system failure or data corruption)
1. **Thread Synchronization in `record_alert_event()` (`app.py` L435-513):** Must remain guarded by `_WEBHOOK_LOCK`. Removing this lock causes file write races and corrupts `status.json`, `logs.json`, and `history.json`.
2. **Single-Flight Lock `_FETCH_LOCKS` (`app.py` L282-315, L344-382):** Must be preserved to prevent Prometheus server overload when multiple clients/TV displays open the dashboard simultaneously.
3. **Synthetic Poller Webhook Backoff (`app.py` L1752):** Must check `time.time() - _LAST_WEBHOOK_AT[0] < WEBHOOK_ACTIVE_WINDOW_SECONDS`. Removing this causes duplicate alert events when Alertmanager is present.
4. **Maintenance Suppression in Event Pipeline (`app.py` L429-431):** `if is_now_firing and get_active_maintenance(instance, job): return False`. Must remain inside `record_alert_event()` so both Webhook and Synthetic Poller honor maintenance windows.
5. **Audio Autoplay Splash Screen Overlay (`templates/alarm.html` L18-50, `alarm.js` L4050-4100):** Browsers block automated audio playback unless unlocked by an explicit user click. The splash screen MUST be presented on initial page load.

### 🟠 Important (Requires strict caution during refactoring)
6. **Scrape Failure Categorization (`app.py` L956-990):** `classify_scrape_failure()` must return concise categories (`DNS`, `Refused`, `Timeout`, `No Route`, `TLS`, `HTTP <code>`, `Unknown`) formatted for TV card labels.
7. **Orphaned Alert Auto-Resolution (`app.py` L1722-1748):** `_reconcile_orphaned_alerts()` must automatically clear `TargetDown` alerts when targets are deleted. Removing this permanently locks `status.json` in `CRITICAL` state.
8. **File-SD YAML Formatting (`app.py` L254-270):** `save_website_targets()` must output `- targets: ["url"]` with `job: "blackbox_http"`. Prometheus target ingestion depends on this exact YAML structure.
9. **Weighted Aggregate SLA Formula (`fleet_availability.py` L87-90):** `fleet_aggregate` MUST be calculated using total monitored minutes capacity, not a simple unweighted mean.

### 🟡 Flexible (Safe for enhancements)
10. UI styling adjustments in `noc.css`.
11. Additional time-range options in `rangeFilterChips`.
12. Additional export formats (e.g. JSON export alongside existing CSV exporter).

---

## 7. Stable Contracts

The following JSON API contracts MUST NOT be altered, as frontend rendering and external healthcheck engines directly rely on these field names:

- **`GET /instances` Response Item:**
  ```json
  {
    "instance": "string",
    "job": "string",
    "health": "up" | "down" | "unknown",
    "responseTimeMs": number,
    "httpStatusCode": number | null,
    "lastScrape": "string",
    "scrapeUrl": "string",
    "lastError": "string",
    "failureCategory": "string" | null,
    "failureDetail": "string" | null,
    "downSince": number,
    "maintenance": boolean,
    "suppressedBy": "string" | null,
    "dependsOn": "string" | null
  }
  ```
- **`GET /status` Response:**
  ```json
  {
    "status": "NORMAL" | "WARNING" | "CRITICAL",
    "alerts": [ { "key": "string", "name": "string", "severity": "string", "instance": "string", "summary": "string", "time": number } ],
    "updated": number
  }
  ```
- **`GET /health` Response:**
  ```json
  {
    "ok": boolean,
    "server_time": number,
    "components": {
      "prometheus": { "ok": boolean, "url": "string" },
      "monitoring_api": { "ok": boolean },
      "alarm_service": { "ok": boolean, "last_tick_seconds_ago": number | null },
      "storage": { "ok": boolean }
    }
  }
  ```

---

## 8. Important Business Logic

### SLA & Availability Formulas (`fleet_availability.py`)
1. **Per-Server Availability (`availability_pct`):**
   $$\text{Availability \%} = \max\left(0, \min\left(100, \frac{\text{Denominator Minutes} - \text{Downtime Minutes}}{\text{Denominator Minutes}} \times 100\right)\right)$$
2. **New Target Denominator Clamping:**
   If a server's age (current time minus `created_at`) is less than the requested filter period, its age is used as the denominator (`fleet_availability.py` L140-142). This prevents brand-new targets from being penalized for period time before they existed.
3. **Weighted Fleet Aggregate Availability (`fleet_aggregate`):**
   $$\text{Fleet Aggregate \%} = \frac{\sum \text{Denominator Minutes} - \sum \text{Downtime Minutes}}{\sum \text{Denominator Minutes}} \times 100$$
   *Note:* This represents official management SLA availability (`README.md` L104).
4. **Fleet Average Availability (`fleet_average`):** Unweighted arithmetic mean of `availability_pct` across all scored servers (`fleet_availability.py` L85).
5. **Health Ratio (`health_ratio`):** Percentage of monitored servers with zero downtime during the window (`fleet_availability.py` L92).

---

## 9. State & Storage Rules

State files are located in `alarm/` and managed via atomic temporary file writes (`save_json()` in `app.py` L134-142):

| File Path | Storage Role | Lifecycle / Retention |
| --- | --- | --- |
| `status.json` | **Primary Source of Truth** for global alarm state | Overwritten on state transition by `record_alert_event()` |
| `logs.json` | Incident Log History | Capped at `MAX_LOGS = 200` items (`app.py` L94, L510) |
| `history.json` | Historical Incident Records | Capped at `MAX_HISTORY = 1000` items (`app.py` L93, L511) |
| `history_archive.json` | Historical Incident Overflow Archive | Prepended with overflow items from `history.json` (`app.py` L107-115) |
| `maintenance.json` | Scheduled Planned Maintenance Windows | JSON list managed via `/api/maintenance` CRUD routes |
| `dependencies.json` | Parent-Child Correlation Tree Links | JSON list managed via `/api/dependencies` CRUD routes |
| `endpoints.json` | Active and Candidate Prometheus Endpoints | Managed via `/api/endpoints` CRUD routes |
| `deleted_targets.json` | Deleted / Hidden Scrape Targets List | Managed via `/api/targets` DELETE route |
| `targets/websites.yml` | Prometheus Dynamic File-SD Target File | Read by Prometheus Blackbox HTTP job (`app.py` L254-270) |

---

## 10. API Contracts

Complete REST API inventory in `app.py`:

| Route Endpoint | Method | Input Parameters | Output Response | Consumer | Stable Contract |
| --- | --- | --- | --- | --- | --- |
| `/` | `GET` | None | HTML Render (`alarm.html`) | Browser | Yes |
| `/instances` | `GET` | `job` (optional) | `{ ok, targets: [...], available_jobs: [...], source, prometheus_url }` | Wallboard UI (`alarm.js`) | **Yes** |
| `/status` | `GET` | None | `{ status: "NORMAL"|"WARNING"|"CRITICAL", alerts: [...], updated }` | Wallboard UI (`alarm.js`) | **Yes** |
| `/logs` | `GET` | `limit` (default 50) | `[ { time, event, name, severity, instance, summary, duration_seconds }, ... ]` | Wallboard UI (`alarm.js`) | **Yes** |
| `/history` | `GET` | None | `[ { key, name, severity, instance, status, duration_seconds }, ... ]` | Wallboard UI (`alarm.js`) | **Yes** |
| `/webhook` | `POST` | Alertmanager Webhook JSON | `{ ok: true }` | External Alertmanager | **Yes** |
| `/health` | `GET` | None | `{ ok: bool, server_time, components: {...} }` | Docker Healthcheck & UI | **Yes** |
| `/api/availability` | `GET` | `minutes` / `days`, `job`, `end` | `{ ok: true, period_minutes, counts, overall, fleet_aggregate, ... }` | Wallboard UI (`alarm.js`) | **Yes** |
| `/api/target-history` | `GET` | `target`, `minutes`, `end` | `{ ok: true, target, period_minutes, events: [...], latency_points: [...] }` | Target Detail Modal | **Yes** |
| `/api/targets` | `GET` | None | `{ ok: true, targets: [...] }` | Target Modal | Yes |
| `/api/targets` | `POST` | `{ url }` | `{ ok: true, targets: [...] }` | Target Modal | Yes |
| `/api/targets` | `DELETE` | `{ url }` | `{ ok: true }` | Target Modal | Yes |
| `/api/maintenance` | `GET` | None | `{ ok: true, windows: [...] }` | Maintenance Modal | Yes |
| `/api/maintenance` | `POST` | `{ target, scope, start, end, reason }` | `{ ok: true, window: {...} }` | Maintenance Modal | Yes |
| `/api/maintenance/<id>` | `DELETE` | None | `{ ok: true }` | Maintenance Modal | Yes |
| `/api/dependencies` | `GET` | None | `{ ok: true, dependencies: [...] }` | Correlation Modal | Yes |
| `/api/dependencies` | `POST` | `{ child, parent }` | `{ ok: true, dependency: {...} }` | Correlation Modal | Yes |
| `/api/dependencies/<id>` | `DELETE` | None | `{ ok: true }` | Correlation Modal | Yes |
| `/api/endpoints` | `GET` | None | `{ ok: true, active, endpoints: [...] }` | Settings Modal | Yes |
| `/api/endpoints` | `POST` | `{ url, set_active }` | `{ ok: true, active, endpoints: [...] }` | Settings Modal | Yes |
| `/api/endpoints/select` | `POST` | `{ url }` | `{ ok: true, active }` | Settings Modal | Yes |
| `/api/endpoints` | `DELETE` | `{ url }` | `{ ok: true, active, endpoints: [...] }` | Settings Modal | Yes |

---

## 11. Deployment Invariants

- **Container Environment:** Runs in Docker via `docker-compose.yml` (`service: alarm`, built from `alarm/Dockerfile`).
- **Python Version:** 3.12-slim base image (`Dockerfile` L1).
- **Process Manager:** Gunicorn 23.0.0 running with `--workers 1 --threads 4` (`Dockerfile` L17).
  > [!IMPORTANT]
  > Gunicorn worker count MUST remain set to 1 unless JSON storage is replaced with a database. Increasing worker processes will cause state file write collisions.
- **Port Exposure:** `5000:5000` (`docker-compose.yml` L9).
- **Environment Variables:**
  - `PROMETHEUS_URL`: Default `http://192.168.9.16:9090` (`docker-compose.yml` L11, `app.py` L143).
  - `TARGETS_FILE`: Path to dynamic file SD (`app.py` L208).
  - `JOB_FILTER`: Default job filter string (`app.py` L182).
  - `ALERT_POLL_INTERVAL`: Poller interval in seconds (default 15s) (`app.py` L1686).
  - `DISABLE_ALERT_POLLER`: Set to `"1"` during automated unit tests to suppress background threads (`test_alert_transitions.py` L9).

---

## 12. Frontend Dependencies

- **Auto-Polling Interval:** Configurable in UI header dropdown (`#scrapeIntervalSelect`: 2s, 5s default, 10s, 30s) (`templates/alarm.html` L126-131, `alarm.js` L1140-1160).
- **Audio Siren Trigger:** Requires explicit user gesture on `#enterDashboardBtn` to unlock `HTML5 Audio` playback for `alarm.mp3` (`templates/alarm.html` L41-46, `alarm.js` L4050-4100).
- **DOM Element Binding:** Frontend relies on specific DOM element IDs: `#globalHealthBadge`, `#ackAlarmBtn`, `#instancesBody`, `#soundToggleBtn`, `#endpointSelect`, `#jobSelect`, `#splashOverlay`.
- **Gzip Support:** Client MUST send `Accept-Encoding: gzip` for compressed payloads (`app.py` L48-61).

---

## 13. Testing Baseline

- **Primary Test Runner:** Python `unittest` (`python -m unittest alarm/test_*.py`).
- **Test Modules:**
  - [test_alert_transitions.py](file:///c:/Users/dimi/Downloads/infra-monitoring-stack-v3/alarm/test_alert_transitions.py): State transition calculations, alert event deduplication, maintenance suppression, dependency correlation, and `/health` response keys.
  - [test_fleet_availability.py](file:///c:/Users/dimi/Downloads/infra-monitoring-stack-v3/alarm/test_fleet_availability.py): SLA availability formulas, server age denominator clamping, diverge tests between weighted vs. unweighted SLA.
  - [test_scrape_classification.py](file:///c:/Users/dimi/Downloads/infra-monitoring-stack-v3/alarm/test_scrape_classification.py): Failure categorization rules (DNS, Refused, Timeout, TLS, HTTP status) and regex validation.
  - [test_target_history.py](file:///c:/Users/dimi/Downloads/infra-monitoring-stack-v3/alarm/test_target_history.py): Target history API range filtering (Requires `pytest`).

---

## 14. Technical Debt vs. Core Requirements

| Category | Item | Classification | Actionable Guidance |
| --- | --- | --- | --- |
| **Feature** | High-contrast NOC status cards & Audio MP3 Siren | **Core Requirement** | MUST NOT BE REMOVED. Core reason project exists. |
| **Feature** | Weighted SLA math (`fleet_aggregate`) | **Core Requirement** | MUST NOT BE ALTERED. Management SLA standard. |
| **Feature** | Prometheus Failover Pool (`PROMETHEUS_CANDIDATES`) | **Core Requirement** | MUST BE PRESERVED for high availability. |
| **Architecture** | Single-file backend `app.py` (1,841 lines) | **Technical Debt** | Safe to refactor into Flask Blueprints if behavior is preserved. |
| **Architecture** | Single-file frontend `alarm.js` (193 KB) | **Technical Debt** | Safe to refactor into ES modules if DOM bindings remain intact. |
| **Testing** | `test_target_history.py` requires `pytest` | **Technical Debt** | Add `pytest` to `requirements.txt` or refactor to `unittest`. |
| **Security** | Hardcoded IP `192.168.9.16` in fallbacks | **Technical Debt** | Safe to parameterize via environment variables. |
| **Security** | Unauthenticated API endpoints | **Technical Debt** | Safe to add optional API key or Basic Auth middleware. |

---

## 15. Architecture Invariants

1. **Blackbox Probe Focus:** InfraWatch v3 MUST strictly monitor network service probe availability (Status UP/DOWN, Response Time ms, HTTP Code, ICMP Ping). Internal host hardware metrics (CPU/RAM/Disk) MUST NOT be added back to the primary wallboard display.
2. **Prometheus as Primary Data Source:** Prometheus PromQL query responses are the sole authoritative source for target metrics and historical uptime trends.
3. **Single Source of Truth for Status:** `status.json` is the sole source of truth for global alarm state (`NORMAL`, `WARNING`, `CRITICAL`) and active siren triggers.
4. **API-First Frontend:** The UI is an isolated single-page application communicating exclusively via JSON REST APIs.

---

## 16. Current Development Direction

InfraWatch v3 is actively developing towards a **feature-complete enterprise NOC display wallboard**. Recent development iterations focus on:
- **Semi-fullscreen target detail modals** with interactive response time sparklines, percentile latency stats (p95, max), and multi-tab historical event inspection.
- **Enhanced SLA metrics** with customizable time-range controls (5m, 15m, 1h, 6h, 24h, 7d, 30d, Custom Range).
- **Zero-touch Prometheus failover and job filter management**.

---

## 17. Safe Areas for Modification

- **Visual UI Layout & Styling:** Adding or modifying CSS styles in `noc.css` or dark/light theme tokens.
- **Time Range Filters:** Adding new time range options in `alarm.html` and `alarm.js`.
- **Export Capabilities:** Adding new report export formats (PDF/JSON export alongside CSV).
- **Test Infrastructure:** Adding new unit test files or adding `pytest` to `requirements.txt`.

---

## 18. High-Risk Areas for Modification

- **Thread Locking & Deduplication (`record_alert_event()` in `app.py`):** Modifying `_WEBHOOK_LOCK` or state dedupe logic risks race conditions and storage corruption.
- **SLA Calculation Core (`fleet_availability.py`):** Modifying denominator clamping or aggregate uptime math invalidates SLA reporting metrics.
- **Maintenance Suppression Hook (`get_active_maintenance()`):** Removing maintenance suppression checks from `record_alert_event()` causes false siren alarms during planned maintenance.
- **Browser Audio Consent Splash (`templates/alarm.html`):** Removing `#splashOverlay` breaks audio siren playback due to browser autoplay policies.

---

## 19. Unknowns / Things Requiring Further Verification

1. **Gunicorn Multi-Worker Behavior:** Gunicorn currently runs with `--workers 1`. The impact of running multiple worker processes on file-based JSON locks (`_WEBHOOK_LOCK`) requires verification before increasing worker count.
2. **Prometheus Scrape Interval Mismatch:** Code assumes `SCRAPE_INTERVAL_SECONDS = 2`. Verification is required if connected to remote Prometheus instances with different `scrape_interval` settings (e.g., 5s or 15s).

---

## 20. Final Development Rules

When developing or modifying **InfraWatch v3**, every AI agent and developer MUST follow these 5 mandatory rules:

1. **NEVER re-introduce Node Exporter hardware metrics** (CPU/RAM/Disk) onto the main NOC wallboard view.
2. **NEVER remove thread-level file locks (`_WEBHOOK_LOCK`)** or bypass `record_alert_event()` during alert state transitions.
3. **NEVER bypass browser audio autoplay consent handling** (`#splashOverlay`).
4. **NEVER change weighted aggregate SLA capacity math** in `fleet_availability.py` to unweighted averages.
5. **ALWAYS preserve existing REST API response schemas** for `/instances`, `/status`, `/health`, and `/api/availability`.
