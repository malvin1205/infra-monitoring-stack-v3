---
name: InfraWatch
description: Unified infrastructure monitoring, availability tracking, and alert orchestration stack
colors:
  primary: "#38BDF8"
  success: "#22C55E"
  warning: "#ffd500"
  critical: "#EF4444"
  neutral-bg: "#020408"
  neutral-surface: "#060A12"
  neutral-card: "#080D17"
  text-primary: "#F8FAFC"
  text-secondary: "#94A3B8"
  text-muted: "#7E8B9B"
  white: "#ffffff"
typography:
  display:
    fontFamily: "Inter, -apple-system, system-ui, sans-serif"
    fontSize: "26px"
    fontWeight: 700
    lineHeight: 1.2
  headline:
    fontFamily: "Inter, -apple-system, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.3
  title:
    fontFamily: "Inter, -apple-system, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontFamily: "Inter, -apple-system, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  subhead:
    fontFamily: "Inter, -apple-system, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1.4
  label:
    fontFamily: "JetBrains Mono, Fira Code, monospace"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1
  caption:
    fontFamily: "Inter, -apple-system, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.4
  micro:
    fontFamily: "JetBrains Mono, Fira Code, monospace"
    fontSize: "10px"
    fontWeight: 500
    lineHeight: 1
rounded:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.success}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  button-secondary:
    backgroundColor: "{colors.neutral-card}"
    textColor: "{colors.text-secondary}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  button-danger:
    backgroundColor: "{colors.critical}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
---

# Design System: InfraWatch

## Overview

**Creative North Star: "The Obsidian NOC Telemetry"**

InfraWatch is an industrial-grade operations interface engineered for mission-critical monitoring in control rooms and engineering command desks. The design pairs a pitch-black obsidian canvas (`#020408`) with razor-sharp, high-contrast status fills and luminous semantic telemetry. Every element is calibrated for zero-ambiguity split-second comprehension from 5 meters away on a wallboard display, while remaining refined, responsive, and comfortable for multi-hour desktop SRE investigations.

Drawing inspiration from high-density avionics and the clean utilitarian discipline of tools like Linear, Vercel, and Better Stack, the visual language minimizes decorative noise. Surfaces step forward through deliberate tonal layering and hairline white borders rather than heavy blur or muddy shadows. When the infrastructure is healthy, the interface recedes into an unobtrusive dark stillness; when an outage occurs, high-saturation signal fills and audible alert cues immediately seize operational focus.

**Key Characteristics:**
- Pitch-black obsidian backdrop (`#020408`) maximizing contrast and reducing display fatigue.
- Saturated semantic status gradients for instantaneous health recognition (Online, Slow, Down, Maintenance).
- Dedicated monospace typography (`JetBrains Mono`) for all metric telemetry, latencies, IPs, and codes.
- Tonal surface stratification with subtle hairline borders (`rgba(255, 255, 255, 0.08)`).
- Responsive grid architecture scaling seamlessly from mobile emergency triage to 4K NOC wallboards.

## Colors

The palette is anchored by deep obsidian void tones and elevated strictly through purposeful tactical semantics.

### Primary
- **Electric Cyan** (`#38BDF8`): System accent used for interactive selections, focus halos, active navigation tabs, and system brand marks. Used selectively to never compete with operational alarms.

### Secondary
- **Emerald Pulse** (`#22C55E`): Healthy operational indicator. Applied to `.hc-up` host tiles, success badges, and standard confirmation action buttons.
- **Amber Hazard** (`#ffd500`): Degraded or slow response warning state. Indicates latency threshold breaches or high-latency warning conditions.
- **Signal Crimson** (`#EF4444`): Critical down state. Exclusively indicates probe failures, active outages, firing alerts, and destructive administrative actions.

### Neutral
- **Obsidian Void** (`#020408`): Canvas root background and body backdrop.
- **Surface Elevated** (`#050810`): Elevated container background for topnav bars, summary sections, and filter strips.
- **Card Surface** (`#080D17`): Individual component cards, modal bodies, and drawer panels.
- **Surface Hover** (`#11182B`): Interactive hover feedback for list items, secondary buttons, and table rows.
- **Pure Slate** (`#F8FAFC`): Primary readable text, metric values, and prominent headings.
- **Muted Slate** (`#94A3B8`): Secondary descriptive text, labels, and table header descriptions.
- **Ghost Slate** (`#7E8B9B`): Helper text, timestamps, and inactive iconography.

### Named Rules
**The Strict Semantic Rule.** Green, yellow, and red are reserved strictly for operational status (Up, Degraded, Down). They must never be applied to decorative borders, marketing flourishes, or neutral branding elements.

**The 10-Percent Accent Rule.** Electric Cyan (`#38BDF8`) is the sole interactive accent; it must occupy ≤10% of any view, ensuring alerts retain visual dominance.

## Typography

**Display Font:** Inter, -apple-system, system-ui, sans-serif
**Body Font:** Inter, -apple-system, system-ui, sans-serif
**Label/Mono Font:** JetBrains Mono, Fira Code, monospace

**Character:** A dual-font architecture balancing Inter's modern neutral scanability for navigation and prose with JetBrains Mono's mechanical precision for numerical telemetry.

### Hierarchy
- **Display** (700 weight, `26px` / `--fs-2xl`, line-height `1.2`): Page titles, fleet summary metric digits, and large availability percentages.
- **Headline** (600 weight, `20px` / `--fs-xl`, line-height `1.3`): Modal titles, drawer target headings, and incident summaries.
- **Title** (600 weight, `16px` / `--fs-lg`, line-height `1.4`): Section dividers, card group headers, and dialog labels.
- **Body** (400 weight, `14px` / `--fs-base`, line-height `1.5`): Standard dialog instructions, user lists, setting descriptions. Max line length 70ch.
- **Label** (700 weight, `12px` / `--fs-sm`, letter-spacing `normal`, monospace): IP addresses, latency values (ms), HTTP status codes, and badge tags.
- **Caption** (500 weight, `11px` / `--fs-xs`, line-height `1.4`): Secondary metadata, table headers, and timestamp labels.
- **Micro** (500 weight, `10px` / `--fs-2xs`, monospace): Chart tick labels, sparkline axes, and ongoing duration counters.

### Named Rules
**The Telemetry Monospace Rule.** Any data point subject to temporal comparison (latencies, IPs, ports, error budgets, timestamps) must be rendered in JetBrains Mono to prevent jitter during real-time updates.

## Layout

The spatial model relies on a fixed-height operational header (`--topnav-h: 56px`), a horizontal fleet summary strip, a responsive host grid, and an off-canvas drawer (`--drawer-w: 360px`).

- **Spacing Scale (4px base):** `2px` (`--space-0-5`), `4px` (`--space-1`), `6px` (`--space-1-5`), `8px` (`--space-2`), `12px` (`--space-3`), `16px` (`--space-4`), `20px` (`--space-5`), `24px` (`--space-6`), `32px` (`--space-8`).
- **Host Grid Architecture:** Auto-filling grid (`repeat(auto-fill, minmax(120px, 1fr))`) governed by card floors (`--hc-min-w: 120px`, `--hc-min-h: 56px`). Cards never shrink below readability thresholds to cram hosts onto a screen.
- **Breakpoints:**
  - `xs`: 480px (mobile emergency triage)
  - `sm`: 640px (large phone / small tablet)
  - `md`: 768px (tablet console)
  - `lg`: 1024px (small laptop / standard display)
  - `xl`: 1400px (desktop workstation)
  - `tv`: 1920px (NOC wallboard display)

## Elevation & Depth

InfraWatch adopts a flat tonal layering model. Surfaces at rest sit in a coherent dark plane, with depth expressed by progressive lightness steps (`#020408` → `#060A12` → `#080D17`) enclosed in hairline borders (`rgba(255, 255, 255, 0.08)`). Drop shadows are ambient rather than structural, used almost exclusively to elevate floating overlays (drawers, dialogs) and to create glowing alert halos.

### Shadow Vocabulary
- **Subtle Rest** (`box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3), 0 1px 2px rgba(0, 0, 0, 0.2)`): Inputs, chips, and small controls.
- **Card Depth** (`box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4), 0 2px 6px rgba(0, 0, 0, 0.3)`): Hover states on host cards and elevated summaries.
- **Modal Elevation** (`box-shadow: 0 12px 40px rgba(0, 0, 0, 0.6), 0 4px 16px rgba(0, 0, 0, 0.4)`): Centered modal dialogs.
- **Drawer Boundary** (`box-shadow: -8px 0 40px rgba(0, 0, 0, 0.5)`): Slide-over inspection drawer.

### Named Rules
**The Rest-Flat Rule.** Surfaces are flat at rest. Drop shadows and radiant glows appear only as a response to interaction (hover, focus) or active incident alerts.

## Shapes

Form language emphasizes industrial density and compact discipline.

- **Micro Radius** (`4px` / `--r-xs`): Compact chart items, mini duration pills, and inline code tags.
- **Small Radius** (`8px` / `--r-sm`): Buttons, host cards, input fields, and status badges.
- **Medium Radius** (`12px` / `--r-md`): Summary cards, dropdown panels, and sub-drawers.
- **Large Radius** (`16px` / `--r-lg`): Modal dialog containers.
- **Pill Radius** (`9999px` / `--r-full`): Status pills, filter chips, and circular icon buttons.

### Named Rules
**The Compact Radius Rule.** No container or tile may exceed 16px radius. Bubbly oversized roundings (>24px) are strictly forbidden to maximize information density.

## Components

### Buttons
Compact, tactile, and engineered for unambiguous action.
- **Shape:** Gently rounded corners (`8px` radius).
- **Primary (`.btn-primary`):** Background `#22C55E` with `#ffffff` text, padding `8px 16px`. Hover transitions to `#16a34a`.
- **Secondary (`.btn-secondary`):** Background `#11182B` (`--surface-hover`) with `#94A3B8` text. Hover transitions text to `#F8FAFC`.
- **Danger (`.btn-danger`):** Background `#EF4444` with `#ffffff` text. Hover transitions to `#dc2626`.
- **Focus:** High-contrast outline `2px solid #38BDF8` with `2px` offset.

### Host Cards (`.host-card`)
The signature tile component representing monitored targets.
- **Shape:** Rounded rectangle (`8px` radius) with `1px solid rgba(255, 255, 255, 0.12)`.
- **States:**
  - `hc-up`: Saturated emerald gradient (`linear-gradient(180deg, #00a854 0%, #007a3e 100%)`).
  - `hc-slow`: High-visibility orange gradient (`linear-gradient(180deg, #e65100 0%, #9e3d00 100%)`).
  - `hc-down`: High-urgency crimson gradient (`linear-gradient(180deg, #d32f2f 0%, #8e1513 100%)`).
  - `hc-maintenance`: Slate hatched pattern with dashed border.
  - `hc-suppressed`: Dimmed violet gradient for correlated child hosts.
- **Hover:** Lifts `translateY(-2px)` with brightness boost `1.08` and `box-shadow: 0 4px 12px rgba(0,0,0,0.4)`.

### Summary Cards (`.summary-card`)
Metrics overview tiles displaying count tallies and availability percentages.
- **Shape:** Rounded box (`12px` radius) with hairline border.
- **Background:** `#080D17` (`--bg-card`) in dark mode; tinted pastels in light mode.
- **Internal Padding:** `18px 16px 14px`.

### Inputs / Search Fields
High-legibility dark inputs designed for zero keyboard lag.
- **Style:** Background `#060A12`, border `1px solid rgba(255, 255, 255, 0.1)`, radius `8px`.
- **Focus:** Border shifts to Electric Cyan (`#38BDF8`) with subtle glow.

### Navigation Bar (`.topnav`)
Fixed 56px command strip carrying the brand title, real-time clock, endpoint selector, audio toggle, and profile controls.
- **Style:** Background `rgba(6, 10, 18, 0.85)` with `backdrop-filter: blur(12px)` and bottom border `1px solid rgba(255, 255, 255, 0.08)`.

## Do's and Don'ts

### Do:
- **Do** preserve the pitch-black canvas (`#020408`) as the foundational dark backdrop.
- **Do** format all real-time probe telemetry (latencies in ms, HTTP codes, IPs, timestamps) in `JetBrains Mono`.
- **Do** enforce a minimum host tile size of 120px by 56px so labels remain readable from across a room.
- **Do** use hairline borders (`rgba(255, 255, 255, 0.08)`) to separate dark panels cleanly.
- **Do** support full dual-theme parity (Dark default, Light via `[data-theme="light"]`) with WCAG AA minimum contrast.

### Don't:
- **Don't** use oversized playful rounded corners (>24px) that waste screen density on NOC wallboards.
- **Don't** use red or green decoratively; reserve semantic colors exclusively for operational health status.
- **Don't** use low-contrast grey-on-grey text; ensure secondary text maintains at least 4.5:1 contrast against its background.
- **Don't** introduce distracting ambient pulsing or animated background waves when the system is healthy.
