# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **NOC Operators & Engineers**: Primary users monitoring infrastructure, server, website, and network availability in real time, conducting rapid triage upon alert firing from wallboard screens and desktop consoles.
- **SREs & System Administrators**: Configure monitored targets, Prometheus endpoints, maintenance windows, parent-child dependency trees, and analyze SLA trends, incident history, and error budgets.
- **Operations Viewers & Stakeholders**: Read-only observers tracking uptime, availability reports, and active maintenance states without configuration privileges.

## Product Purpose

InfraWatch provides unified real-time infrastructure monitoring, availability tracking, audible incident alerting, and SLA orchestration. Success means zero undetected service disruptions, immediate operator awareness, and the complete elimination of alert fatigue caused by cascade failures or scheduled maintenance.

## Positioning

A zero-friction, lightweight monitoring stack built specifically for NOC environments that couples a high-contrast ambient TV wallboard and audio siren with parent-child dependency correlation, maintenance silences, and a hybrid TSDB/hourly-bucket SLA availability engine—delivering immediate actionable awareness without the complexity of enterprise observability suites.

## Operating Context

- **NOC TV Wallboards**: Unattended large-format displays mounted in operations centers, requiring clear readability from across the room in diverse lighting environments with persistent audio alarm capability.
- **Desktop Command Console**: Interactive operator workstations for incident triage, acknowledging firing alarms, inspecting latency sparklines, adjusting maintenance schedules, and viewing system logs.
- **Probe Scraper Integration**: Ingests Prometheus Blackbox Exporter probe telemetry (`probe_success`, `probe_duration_seconds`, `probe_http_status_code`) with multi-endpoint auto-failover.
- **Alert Dispatch**: Asynchronous Telegram bot notifications for out-of-band operational escalations (FIRING / RESOLVED).

## Capabilities and Constraints

- **Capabilities**:
  - Real-time fleet health aggregation with sub-second status reflection.
  - Automated browser siren audio on DOWN status transitions with operator Acknowledge mute action.
  - Dependency correlation (parent-child topology) suppressing downstream child alarms when a parent gateway or host fails.
  - Maintenance window scheduling at target and job levels to prevent false positive alarms.
  - Deep SLA availability engine reconciling real-time TSDB samples with pre-aggregated SQLite hourly buckets and SLA error budget forecasting.
  - Distributed background workers for target polling, incident deduplication, and hourly rollups.
  - Multi-endpoint Prometheus support with automatic failover and per-endpoint job filtering.
  - Role-Based Access Control (Owner, Administrator, Read-Only Viewer) and API key authentication.
- **Constraints**:
  - Python/Flask backend utilizing SQLite under WAL mode (`BEGIN IMMEDIATE` write isolation) with write-through cache files (`status.json`, `history.json`).
  - Containerized deployment via Docker Compose with zero mandatory external cloud dependencies (fully on-premise and air-gapped capable).

## Brand Commitments

- **Name**: InfraWatch (v3)
- **Voice & Tone**: Mission-critical, concise, authoritative, high-contrast, pragmatic engineering focus.

## Evidence on Hand

- Runnable application codebase in `alarm/` (`app.py`, `templates/`, `static/`).
- Formal domain model and module boundary specifications in `CONTEXT.md`.
- Documented feature set, architectural notes, and production UI wallboard captures in `README.md`.

## Product Principles

1. **Glanceability First**: Visual status (Online, Down, Maintenance) and latency must be immediately comprehensible at a glance from across a room.
2. **Zero False Alarms & Fatigue**: Suppress cascade alerts through dependency trees and scheduled maintenance; every audible siren must demand real action.
3. **Dual-Speed Utility**: Seamlessly balance rapid ambient wallboard awareness with deep interactive desktop drill-downs for SLA analysis and administration.
4. **Fault-Tolerant Autonomy**: Operate resiliently with automatic Prometheus failover, write-through caching for zero-latency reads, and atomic transaction boundaries.

## Accessibility & Inclusion

- High-contrast visual indicators accompanied by explicit text badges and shape cues so status is never conveyed by hue alone.
- Multi-modal alert signaling combining visual banner alerts, audio sirens, and asynchronous Telegram messages.
