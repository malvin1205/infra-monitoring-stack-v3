/* Pure chart math shared by the drawer's response-time sparkline and the
 * history chart. No DOM, no `this` — just number-crunching and an SVG
 * <defs> string.
 */

/**
 * "Nice" axis scale: given a data min/max, return rounded tick bounds, an
 * even step, the tick list, and a formatter. maxTicks is a target, not a
 * hard cap.
 */
export function calculateNiceScale(minVal, maxVal, maxTicks = 4) {
  let min = 0;
  if (minVal < 0) min = minVal;

  let max = Math.max(min + 0.1, maxVal);
  let rawMax = max > 0 ? max * 1.15 : 1.0;
  let range = rawMax - min;

  let rawStep = range / maxTicks;
  let exponent = Math.floor(Math.log10(rawStep));
  let fraction = rawStep / Math.pow(10, exponent);

  let niceFraction;
  if (fraction < 1.25) niceFraction = 1;
  else if (fraction < 2.5) niceFraction = 2;
  else if (fraction < 3.75) niceFraction = 3;
  else if (fraction < 7.5) niceFraction = 5;
  else niceFraction = 10;

  let step = niceFraction * Math.pow(10, exponent);
  if (step <= 0) step = 1;

  let niceMin = Math.floor(min / step) * step;
  if (niceMin < 0 && min >= 0) niceMin = 0;

  let niceMax = Math.ceil(rawMax / step) * step;
  while (niceMax < max) {
    niceMax += step;
  }

  let ticks = [];
  for (let tick = niceMin; tick <= niceMax + (step * 0.0001); tick += step) {
    const cleanTick = Math.round(tick * 10000) / 10000;
    ticks.push(cleanTick);
  }

  const formatTick = (val) => {
    if (val >= 1000) {
      return val.toLocaleString('en-US', { maximumFractionDigits: 0 });
    }
    if (step >= 10) {
      return val.toFixed(2);
    }
    if (step >= 1) {
      return val.toFixed(2);
    }
    if (step >= 0.1) {
      return val.toFixed(2);
    }
    return val.toFixed(3);
  };

  return {
    niceMin,
    niceMax,
    step,
    ticks,
    formatTick
  };
}

/**
 * SVG <defs> with the green/amber/red latency gradient, with colour stops
 * placed at the 100/300/500 ms marks within the given range.
 */
export function buildMSGradientDefs(rangeMin, rangeMax, gradIdLine, gradIdArea) {
  const rangeSpan = Math.max(0.1, rangeMax - rangeMin);

  const getOffsetPct = (msVal) => {
    const ratio = (msVal - rangeMin) / rangeSpan;
    return Math.max(0, Math.min(100, ratio * 100)).toFixed(1);
  };

  const off100 = getOffsetPct(100);
  const off300 = getOffsetPct(300);
  const off500 = getOffsetPct(500);

  const topColor = rangeMax > 500 ? '#EF4444' : (rangeMax > 200 ? '#F59E0B' : '#22C55E');
  const topOpacity = rangeMax > 500 ? '0.30' : '0.18';

  return `
      <defs>
        <linearGradient id="${gradIdLine}" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stop-color="#22C55E"/>
          <stop offset="${off100}%" stop-color="#22C55E"/>
          <stop offset="${off300}%" stop-color="#F59E0B"/>
          <stop offset="${off500}%" stop-color="#EF4444"/>
          <stop offset="100%" stop-color="${topColor}"/>
        </linearGradient>
        <linearGradient id="${gradIdArea}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${topColor}" stop-opacity="${topOpacity}"/>
          <stop offset="60%" stop-color="#22C55E" stop-opacity="0.08"/>
          <stop offset="100%" stop-color="#22C55E" stop-opacity="0.0"/>
        </linearGradient>
      </defs>`;
}
