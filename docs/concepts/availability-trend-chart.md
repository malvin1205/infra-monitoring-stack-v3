# Concept — Availability Trend chart redesign

Target: the **Availability Trend** line chart in the Availability Breakdown modal
(`#avbTrendPlot`, rendered by `InstancesPage._renderAvailabilityTrend`,
data from `_build_fleet_trend`).

Deliverables: this doc + `availability-trend-chart.html` (static mockup, real
`.avb` tokens, fake 30-slot data).

---

## 1. Dribbble research

Live crawl of dribbble.com, 2026-09-09. Five shots pulled, monitoring /
observability / SLA dashboards. What each does that ours doesn't:

| Shot | Designer | Move worth stealing |
|------|----------|--------------------|
| Industrial IoT Dashboard / "IndustryOS" | Ilias Miah | **Vertical gradient area fill** under the line (line colour → transparent), not a flat tint. Thin 1.5px bright stroke on top. y-axis `0/25/50/75/100`, x-axis `00–24h`. Uptime KPI `99.8%` with a green `↗ 0.3% vs last week` delta chip beside it. |
| Prism — Infrastructure Analytics | Create Big Supply | **Baseline comparison** everywhere: every metric shows `−0.01% lower vs. 7d mean`. Chart header carries `Window: 60s` / `Sample` selectors. A **median band** the series "floats" around to expose micro-oscillation. `P95` marker sitting on a bar. |
| SAAS Insurance Claim Dashboard | The Cherry-Haus | KPI cards with `▲ +5.0%` / `▼ −4.0%` coloured delta chips + `Last 7 days` caption. **`SLA Status Distribution`** as a 3-segment bar: `Breached 98 / At Risk 187 / On Track 407`. |
| DeFi Portfolio (the reference shot you gave) | — | Bars **coloured by state** (green / amber / red) with a solid **white baseline curve** overlaid. **Floating tooltip card** (value, delta, "Total value on Apr 15"). Range tabs `Week/Month/Quarter/Year`. Time **scrubber** under the axis. Out-of-range bars **ghosted**, not dropped. |
| Zendeeps uptime dashboard | Zendeeps | `99.59%` headline, area traffic chart + response-time bars + service-status table stacked in one card — one glance, no navigation. |

Common thread across all five: **a raw line is not enough**. Every strong one
adds (a) a reference the eye can judge against — target line, median band,
prior-period delta; (b) state colour — the line/markers change colour when a
threshold is crossed; (c) a gradient fill for depth; (d) a richer hover card.

---

## 2. Current chart — what it has, what's missing

Has: single `--accent` line, flat 12%-opacity area, 100% pinned at top with a
dynamic lower bound snapped below the worst slot, 3 dashed gridlines, 5 x-ticks,
crosshair + dot + one-line mono tooltip (`label · 99.42%`), empty-state polyline.

Missing:
- **No SLA reference.** `get_sla_target_pct()` (default 99.9) is the number the
  whole modal is scored against, but the trend line floats with nothing to
  judge it against. A 99.2% slot and a 99.95% slot look the same.
- **No prior-period context.** Is the fleet getting better or worse? Unknown.
- **Flat colour.** A breach and a healthy wobble are the same blue.
- **Gaps vanish.** `_build_fleet_trend` drops NaN slots silently — the line
  connects across a monitoring outage as if nothing happened.
- **Thin hover.** Time + % only. Not "how many incidents in that slot".

---

## 3. The concept

Keep the footprint (one card, ~128px plot, SVG string render, `.avb` tokens,
light+dark, no deps). Add four layers, each independently sheddable if the data
isn't there.

### 3a. SLA target line + breach zone
Horizontal dashed line at `data.sla_target_pct` (fall back to 99.9). The band
**below** it filled at ~6% `--crit` — a permanent "here be dragons" zone.
Force the y-floor to `min(existing floor, target − 0.15)` so the line is always
on screen even on a flawless fleet.

### 3b. State-coloured line + dip markers
Split the stroke: segments at-or-above target draw `--ok`, segments below draw
`--crit`. Any slot below target gets a 3px marker dot. This is the DeFi shot's
"colour by state" applied to a line instead of bars.

### 3c. Gradient area + prior-period ghost
Area fill becomes a vertical gradient (`--ok` at the line → transparent at the
floor). Behind it, a faint second line = the **same fleet, one window earlier**
(`trend_prev`, optional) so the delta is visible, not just stated.

### 3d. KPI header row + richer tooltip
Above the plot, a one-line strip (borrow Prism / Cherry-Haus):

```
Fleet avg  99.94%   ▲ 0.12 pts vs prev 7d      Worst slot  99.10%      Breaches  2 / 168 slots
```

Tooltip becomes a small card: slot time range, availability %, incidents in
slot, `▲/▼` vs the previous slot.

### ASCII wireframe

```
┌─ Availability Trend · last 7d ───────────── [ 1h ▾ ]  ← slot size ─┐
│                                                                     │
│  Fleet avg 99.94%  ▲0.12 pts vs prev 7d   Worst 99.10%   Breaches 2 │
│                                                                     │
│ 100% ┤━━━━━━━━━━━━━━━━━━━●━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│      │        ╭╮        ╱ ╲            ╭─╮      (─ ─ area gradient)   │
│ 99.9%├ ─ ─ ─ ─╫╫─ ─ ─ ─╱─ ─╲─ ─ ─ ─ ─╱─ ─╲─ ─ ─ ─ ─ SLA target ─ ─ │
│      │░░░░░░░░╽╽░░░░░░░╱░░░░░╲░░░░░░░░╱░░░░░╲░░░░░░░░  breach zone   │
│ 99.6%┤        ▼         ●     ▼      (▼ = dip marker, red)          │
│      └───┬────────┬────────┬────────┬────────┬────                  │
│        -7d      -5d      -3d      -1d       now      · · gap = dash  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Colour mapping (existing `.avb` tokens — nothing new)

| Element | Token | Note |
|---------|-------|------|
| Line, at/above target | `--ok` (`--success`) | |
| Line, below target | `--crit` (`--critical`) | |
| Dip marker dot | `--crit` | 3px, `--surface` halo |
| Area gradient top | `--ok` @ 0.22 | |
| Area gradient bottom | `--ok` @ 0 | |
| SLA target line | `--text-low` | 1px dashed |
| Breach zone fill | `--crit` @ 0.06 | |
| Prior-period ghost line | `--text-low` @ 0.5 | 1px, no fill |
| Gridlines | current dashed `rgba(255,255,255,.09)`-ish | unchanged |
| Tooltip card bg | `--bg-elevated` | current |
| Delta chip ▲ | `--ok` | |
| Delta chip ▼ | `--crit` | |
| Gap connector | `--text-low` | 1px dashed between the two live points |

All resolve through the global tokens, so light mode + the palette swap both
just work.

---

## 5. Data contract

Works today with `{ trend: [{ts, availability_pct}], trend_start_ts,
trend_end_ts, trend_bucket_seconds }`.

Concept uses, each **optional** (feature degrades, never breaks):

| Field | Source | Enables |
|-------|--------|---------|
| `sla_target_pct` (already in payload) | `get_sla_target_pct()` | 3a target line + breach zone |
| `trend[i].incident_count` | `_build_fleet_trend` — add `sum(changes(probe_success[slot]))` companion query, or reuse the per-slot bucket incident count | tooltip incidents, dip markers |
| `trend[i].gap_before: true` | `_build_fleet_trend` — flag instead of silently skipping a NaN slot | 3c gap rendering |
| `trend_prev: [{ts, availability_pct}]` | `_build_fleet_trend` — same query shifted back one `window_seconds` | 3d ghost line + header delta |

If none arrive, the chart is still 3a-minus-target + gradient fill + coloured
line by comparing to a hardcoded 99.9 — strictly better than today at zero
backend change.

---

## 6. Phased plan

| Phase | Change | Files | Backend? |
|-------|--------|-------|----------|
| 1 | Gradient area fill + state-coloured line + SLA target line + breach zone (target from existing `sla_target_pct`, or 99.9 fallback). y-floor clamp. | `availability.js` `_renderAvailabilityTrend`, `breakdown-v2.css` | none |
| 2 | KPI header strip (fleet avg / worst slot / breach count — all derivable from `pts`). | `_modal_availability.html`, `availability.js`, `breakdown-v2.css` | none |
| 3 | `trend_prev` ghost line + `▲/▼ vs prev` delta in the strip. | `helpers.py` `_build_fleet_trend`, `availability.js` | one extra `query_range` |
| 4 | Per-slot `incident_count` + `gap_before`; richer tooltip card; dip markers; dashed gap connector. | `helpers.py`, `availability.js`, `breakdown-v2.css` | one companion query |

Phase 1 is the 80/20 and is pure frontend. The mockup shows the phase-1..4
end state.
