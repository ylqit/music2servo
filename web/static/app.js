'use strict';

/* music2servo 可视化界面。
 *
 * 三条原则：
 * 1. 前端不重算任何 DSP。所有曲线、时间轴、脉宽都由后端算好，这里只画和放。
 * 2. 时间轴一律用后端给的 times_s 查表插值，不按 pwm_hz 自己铺栅格
 *    （那样会踩到 frame_count 注释里那个除法/乘法差一帧的浮点陷阱）。
 * 3. 尾巴以 <audio>.currentTime 为时钟，用 requestAnimationFrame 查表插值，
 *    不用 setInterval —— 音频卡顿时画面跟着卡，声画才不会越走越偏。
 */

const MODES = ['envelope', 'brightness', 'pitch', 'beat'];

// 与 viz.py 的 _PRIMARY / _SECONDARY / _TERTIARY / _QUATERNARY 保持一致，
// 图例颜色跨 CLI 与网页统一
const MODE_COLORS = {
  envelope: '#4B3FE3',
  brightness: '#22A5F7',
  pitch: '#F59E0B',
  beat: '#10B981',
};

const MODE_LABELS = {
  envelope: '包络',
  brightness: '亮度',
  pitch: '音高',
  beat: '节拍',
};

const NEUTRAL = '#A1A1AA';
const INK = '#52525B';

/* 尾巴朝上：枢轴在机体上沿，尾巴向上伸出，刻度画在上半圈。
 * py 取 187、h 取 296 是让「刻度顶」与「±120° 刻度末端」到画布上下边距相等（各 35）：
 * 顶端 y = 187 − 152 = 35，±120°（小刻度半径 148）最低 y = 187 + 148·cos60° = 261。 */
const TAIL = {w: 400, h: 296, px: 200, py: 187};

const TAIL_TICK_STEP = 15;
/* 刻度间隔（°）。刻度范围不写死，由后端 servo.angle_max_deg（舵机单侧极限）决定。 */

/* 表单元数据。字段名与 label 都取自 config.py 与 CLI 参数名，
 * 默认值不写在这里 —— 由 /api/config 从 PipelineConfig() 提取后作为 placeholder。 */
const GROUPS = [
  {
    key: 'source',
    title: '输入与解码 source',
    fields: [
      {name: 'sr', label: '采样率 sr (Hz)', step: 1, int: true},
      {name: 'max_duration_s', label: '只取前 N 秒 max_duration_s', step: 0.5, nullable: true},
      {name: 'url_timeout_s', label: 'URL 超时 url_timeout_s (s)', step: 1},
    ],
  },
  {
    key: 'analysis',
    title: '帧化与特征 analysis',
    fields: [
      {name: 'n_fft', label: 'FFT 长度 n_fft', step: 256, int: true},
      {name: 'hop_length', label: '帧移 hop_length', step: 64, int: true},
      {name: 'band_low_hz', label: '低频段边界 band_low_hz (Hz)', step: 10, pair: true},
      {name: 'band_high_hz', label: '高频段边界 band_high_hz (Hz)', step: 100, pair: true},
      {name: 'f0_min_hz', label: '基频下限 f0_min_hz (Hz)', step: 10},
      {name: 'f0_max_hz', label: '基频上限 f0_max_hz (Hz)', step: 10},
      {name: 'f0_voiced_ceiling', label: '浊音判决上限 f0_voiced_ceiling', step: 0.01},
    ],
  },
  {
    key: 'segment',
    title: '高潮截取 segment',
    fields: [
      {name: 'window_s', label: '目标片段长度 window_s (s)', step: 1},
      {name: 'smooth_s', label: '打分平滑 smooth_s (s)', step: 0.1},
      {name: 'energy_weight', label: '能量权重 energy_weight', step: 0.05},
      {name: 'novelty_weight', label: '新颖度权重 novelty_weight', step: 0.05},
      {name: 'norm_low_pct', label: '打分归一低分位 norm_low_pct', step: 1},
      {name: 'norm_high_pct', label: '打分归一高分位 norm_high_pct', step: 1},
      {name: 'onset_align_s', label: '起点吸附 onset_align_s (s)', step: 0.1},
      {name: 'fade_s', label: '首尾淡入淡出 fade_s (s)', step: 0.05},
      {name: 'manual_start_s', label: '手动起点 manual_start_s (s)', step: 0.1, nullable: true},
      {name: 'manual_end_s', label: '手动终点 manual_end_s (s)', step: 0.1, nullable: true},
    ],
  },
  {
    key: 'mapping',
    title: '映射层 mapping',
    fields: [
      {name: 'invert', label: '翻转方向 invert', bool: true},
      {name: 'gain', label: '线性增益 gain', step: 0.05},
      {name: 'smooth_s', label: '映射平滑 smooth_s (s)', step: 0.01},
      {name: 'norm_low_pct', label: '归一低分位 norm_low_pct', step: 1},
      {name: 'norm_high_pct', label: '归一高分位 norm_high_pct', step: 1},
      {name: 'pitch_floor_hz', label: '音高下限 pitch_floor_hz (Hz)', step: 10},
      {name: 'pitch_ceil_hz', label: '音高上限 pitch_ceil_hz (Hz)', step: 10},
      {name: 'beat_swing_s', label: '节拍摆动周期 beat_swing_s (s)', step: 0.05, nullable: true},
      {name: 'beat_bpm_range', label: '节拍 BPM 搜索范围 beat_bpm_range', step: 5, pair: true},
      {name: 'beat_threshold', label: 'onset 判决系数 beat_threshold', step: 0.1},
      {name: 'beat_min_dynamics_db', label: '最低响度动态 beat_min_dynamics_db (dB)', step: 0.5},
    ],
  },
  {
    key: 'shaping',
    title: '成形层 shaping',
    fields: [
      {name: 'low_pct', label: '拉满低分位 low_pct', step: 1},
      {name: 'high_pct', label: '拉满高分位 high_pct', step: 1},
      {name: 'lpf_hz', label: '低通截止 lpf_hz (Hz)', step: 0.5},
      {name: 'lpf_order', label: '低通阶数 lpf_order', step: 1, int: true},
      {name: 'control_hz', label: '控制帧率 control_hz (Hz)', step: 1},
      {name: 'max_rate_per_s', label: '限速 max_rate_per_s (行程/s)', step: 0.1},
      {name: 'deadzone', label: '死区 deadzone', step: 0.005},
      {name: 'center_ms', label: '首尾回中 center_ms (ms)', step: 50, int: true},
    ],
  },
  {
    key: 'servo',
    title: 'PWM 层 servo',
    fields: [
      {name: 'pwm_hz', label: 'PWM 频率 pwm_hz (Hz)', step: 1},
      {name: 'pulse_min_us', label: '最小脉宽 pulse_min_us (µs)', step: 50},
      {name: 'pulse_max_us', label: '最大脉宽 pulse_max_us (µs)', step: 50},
      {name: 'pulse_center_us', label: '中位脉宽 pulse_center_us (µs)', step: 50},
      {name: 'angle_max_deg', label: '行程对应角度 angle_max_deg (°)', step: 5},
    ],
  },
];

const state = {
  defaults: null,
  report: null,
  baseJob: null,
  file: null,
  fileB64: null,
  charts: {},
  tailRot: null,
  tailTicks: null,
};

const els = {};

function $(id) {
  return document.getElementById(id);
}

/* ------------------------------------------------------------------ 工具 */

function fmtNum(v, digits = 2) {
  return String(Number(v.toFixed(digits)));
}

function fmtTime(v) {
  if (v < 0) return '0s';
  if (v < 60) return `${v.toFixed(1)}s`;
  const m = Math.floor(v / 60);
  const s = v - m * 60;
  return `${m}:${s < 10 ? '0' : ''}${s.toFixed(1)}`;
}

function niceTicks(min, max, count) {
  if (!(max > min)) return [min];
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = mag * (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10);
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step) {
    out.push(Number(v.toFixed(6)));
  }
  return out;
}

/** 在升序 times 上二分插值。播放中每帧都要调，不能线性扫。 */
function lookup(t, times, values) {
  const n = times.length;
  if (!n) return 0;
  if (t <= times[0]) return values[0];
  if (t >= times[n - 1]) return values[n - 1];
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= t) lo = mid;
    else hi = mid;
  }
  const span = times[hi] - times[lo];
  const k = span > 0 ? (t - times[lo]) / span : 0;
  return values[lo] + (values[hi] - values[lo]) * k;
}

/** 状态栏保留最近一次结果不清空 —— 「完成，用时 XXXms」正是判断缓存有没有命中的依据。 */
function setStatus(text, isError = false) {
  els.status.textContent = text;
  els.status.classList.toggle('error', Boolean(isError));
}

/* ------------------------------------------------------------------ 表单 */

function buildForm(defaults) {
  const host = els.configForm;
  host.textContent = '';

  for (const group of GROUPS) {
    const values = defaults[group.key] || {};
    const details = document.createElement('details');
    details.open = group.key === 'shaping' || group.key === 'servo';

    const summary = document.createElement('summary');
    summary.textContent = group.title;
    details.appendChild(summary);

    const grid = document.createElement('div');
    grid.className = 'grid';

    for (const field of group.fields) {
      grid.appendChild(buildField(group.key, field, values[field.name]));
    }

    details.appendChild(grid);
    host.appendChild(details);
  }
}

function buildField(groupKey, field, fallback) {
  const wrap = document.createElement('div');
  const id = `cfg-${groupKey}-${field.name}`;

  if (field.bool) {
    wrap.className = 'field bool';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.id = id;
    input.dataset.group = groupKey;
    input.dataset.name = field.name;
    input.checked = Boolean(fallback);
    const label = document.createElement('span');
    label.textContent = field.label;
    wrap.append(input, label);
    return wrap;
  }

  wrap.className = 'field';
  const label = document.createElement('span');
  label.textContent = field.label;
  wrap.appendChild(label);

  if (field.pair) {
    const pair = document.createElement('div');
    pair.className = 'pair';
    for (let i = 0; i < 2; i += 1) {
      pair.appendChild(numberInput(`${id}-${i}`, groupKey, field.name, '', field.step));
    }
    wrap.appendChild(pair);
    return wrap;
  }

  const hint = fallback === null || fallback === undefined ? '默认：无' : `默认：${fallback}`;
  wrap.appendChild(numberInput(id, groupKey, field.name, hint, field.step));
  return wrap;
}

function numberInput(id, groupKey, name, placeholder, step) {
  const input = document.createElement('input');
  input.type = 'number';
  input.id = id;
  input.step = String(step || 'any');
  input.placeholder = placeholder;
  input.dataset.group = groupKey;
  input.dataset.name = name;
  return input;
}

/** 收集表单。留空 = 不下发该字段，后端就会用 config.py 里的默认值。 */
function readConfig() {
  const cfg = {};
  for (const group of GROUPS) {
    const obj = {};
    for (const field of group.fields) {
      if (field.bool) {
        obj[field.name] = $(`cfg-${group.key}-${field.name}`).checked;
        continue;
      }
      if (field.pair) {
        const a = $(`cfg-${group.key}-${field.name}-0`).value.trim();
        const b = $(`cfg-${group.key}-${field.name}-1`).value.trim();
        if (a !== '' && b !== '') obj[field.name] = [Number(a), Number(b)];
        continue;
      }
      const raw = $(`cfg-${group.key}-${field.name}`).value.trim();
      if (raw === '') continue;
      obj[field.name] = field.int ? Math.round(Number(raw)) : Number(raw);
    }
    cfg[group.key] = obj;
  }
  return cfg;
}

function resetParams() {
  for (const group of GROUPS) {
    for (const field of group.fields) {
      if (field.bool) {
        $(`cfg-${group.key}-${field.name}`).checked = Boolean(
          (state.defaults[group.key] || {})[field.name],
        );
        continue;
      }
      if (field.pair) {
        $(`cfg-${group.key}-${field.name}-0`).value = '';
        $(`cfg-${group.key}-${field.name}-1`).value = '';
        continue;
      }
      $(`cfg-${group.key}-${field.name}`).value = '';
    }
  }
  setStatus('已恢复默认参数');
}

function selectedModes() {
  const picked = MODES.filter((mode) => $(`mode-${mode}`).checked);
  return picked.length ? picked : MODES.slice();
}

function buildModeChips() {
  els.modesRow.textContent = '';
  for (const mode of MODES) {
    const chip = document.createElement('label');
    chip.className = 'chip';
    chip.title = mode;

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.id = `mode-${mode}`;
    box.checked = true;
    box.addEventListener('change', () => {
      if (!MODES.some((m) => $(`mode-${m}`).checked)) box.checked = true;
    });

    const text = document.createElement('span');
    text.textContent = MODE_LABELS[mode];
    chip.append(box, text);
    els.modesRow.appendChild(chip);
  }
}

/* ------------------------------------------------------------------ 画图 */

function svgEl(tag, attrs) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    node.setAttribute(key, String(value));
  }
  return node;
}

/**
 * 通用折线图。viewBox 宽度取容器实测像素宽，1 单位 = 1px，
 * 这样文字不会被 viewBox 缩放拉变形。返回 setPlayhead 供动画循环调用。
 */
function drawChart(host, spec) {
  host.textContent = '';
  const width = Math.max(320, Math.round(host.clientWidth || 640));
  const height = spec.height || 150;
  const pad = {l: 46, r: 12, t: 18, b: 22};
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;
  const [xMin, xMax] = spec.x;
  const [yMin, yMax] = spec.y;

  const sx = (t) => pad.l + (xMax > xMin ? ((t - xMin) / (xMax - xMin)) * plotW : 0);
  const sy = (v) => pad.t + plotH - (yMax > yMin ? ((v - yMin) / (yMax - yMin)) * plotH : 0);

  const svg = svgEl('svg', {
    class: 'chartSvg',
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    role: 'img',
  });

  for (const value of niceTicks(yMin, yMax, spec.yCount || 4)) {
    const y = Math.round(sy(value)) + 0.5;
    svg.appendChild(svgEl('line', {class: 'grid-line', x1: pad.l, x2: pad.l + plotW, y1: y, y2: y}));
    const text = svgEl('text', {
      class: 'axis-text',
      x: pad.l - 7,
      y: y + 4,
      'text-anchor': 'end',
    });
    text.textContent = fmtNum(value, 2);
    svg.appendChild(text);
  }

  for (const value of niceTicks(xMin, xMax, spec.xCount || 6)) {
    const x = Math.round(sx(value)) + 0.5;
    svg.appendChild(svgEl('line', {class: 'grid-line', x1: x, x2: x, y1: pad.t, y2: pad.t + plotH}));
    const text = svgEl('text', {
      class: 'axis-text',
      x,
      y: height - 6,
      'text-anchor': 'middle',
    });
    text.textContent = fmtTime(value);
    svg.appendChild(text);
  }

  if (spec.yTitle) {
    const text = svgEl('text', {class: 'axis-title', x: 4, y: 11});
    text.textContent = spec.yTitle;
    svg.appendChild(text);
  }

  for (const band of spec.bands || []) {
    const left = sx(Math.max(band[0], xMin));
    const right = sx(Math.min(band[1], xMax));
    svg.appendChild(
      svgEl('rect', {
        class: 'band',
        x: Math.round(left),
        y: pad.t,
        width: Math.max(0, Math.round(right - left)),
        height: plotH,
      }),
    );
    for (const edge of [left, right]) {
      svg.appendChild(
        svgEl('line', {
          class: 'band-edge',
          x1: Math.round(edge) + 0.5,
          x2: Math.round(edge) + 0.5,
          y1: pad.t,
          y2: pad.t + plotH,
        }),
      );
    }
  }

  for (const ref of spec.refLines || []) {
    const y = Math.round(sy(ref.value)) + 0.5;
    svg.appendChild(
      svgEl('line', {
        class: 'grid-line',
        x1: pad.l,
        x2: pad.l + plotW,
        y1: y,
        y2: y,
        'stroke-dasharray': '4 4',
      }),
    );
  }

  // 节拍点竖线：画在曲线之前，让曲线压在上面盖住线头
  for (const value of spec.beatLines || []) {
    const x = Math.round(sx(value)) + 0.5;
    if (x < pad.l || x > pad.l + plotW) continue;
    svg.appendChild(
      svgEl('line', {
        class: 'beat-line',
        x1: x,
        x2: x,
        y1: pad.t,
        y2: pad.t + plotH,
      }),
    );
  }

  for (const series of spec.series || []) {
    svg.appendChild(pathFor(series, sx, sy, pad, plotH));
  }

  const playhead = svgEl('line', {
    class: 'playhead',
    x1: pad.l,
    x2: pad.l,
    y1: pad.t,
    y2: pad.t + plotH,
    visibility: 'hidden',
  });
  svg.appendChild(playhead);
  host.appendChild(svg);

  return {
    sx,
    setPlayhead(t, visible) {
      if (!visible || !Number.isFinite(t)) {
        playhead.setAttribute('visibility', 'hidden');
        return;
      }
      const clamped = Math.min(Math.max(t, xMin), xMax);
      const x = Math.round(sx(clamped)) + 0.5;
      playhead.setAttribute('x1', x);
      playhead.setAttribute('x2', x);
      playhead.setAttribute('visibility', 'visible');
    },
  };
}

function pathFor(series, sx, sy, pad, plotH) {
  const t = series.t;
  const v = series.v;
  const last = t.length - 1;
  const sw = series.width || 1.5;
  let d = '';

  if (series.fill) {
    const base = Math.round(pad.t + plotH);
    d = `M${Math.round(sx(t[0]))} ${base}`;
    for (let i = 0; i <= last; i += 1) {
      d += `L${Math.round(sx(t[i]))} ${Math.round(sy(v[i]))}`;
    }
    d += `L${Math.round(sx(t[last]))} ${base}Z`;
    return svgEl('path', {class: 'series area-fill', d});
  }

  for (let i = 0; i <= last; i += 1) {
    d += `${i === 0 ? 'M' : 'L'}${Math.round(sx(t[i]))} ${Math.round(sy(v[i]))}`;
  }
  const attrs = {
    class: 'series',
    d,
    stroke: series.color,
    'stroke-width': sw,
    'stroke-dasharray': series.dash || '',
    opacity: series.opacity === undefined ? 1 : series.opacity,
  };
  return svgEl('path', attrs);
}

function renderLegend(host, items) {
  host.textContent = '';
  for (const item of items) {
    const wrap = document.createElement('span');
    wrap.className = 'item';
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.borderTopWidth = `${item.width || 2}px`;
    swatch.style.borderTopColor = item.color;
    swatch.style.borderTopStyle = item.dash ? 'dashed' : 'solid';
    const text = document.createElement('span');
    text.textContent = item.label;
    wrap.append(swatch, text);
    host.appendChild(wrap);
  }
}

function drawPlaceholders() {
  for (const id of ['chartWave', 'chartScore', 'chartShape', 'chartPwm']) {
    const host = $(id);
    host.textContent = '';
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '尚未分析。';
    host.appendChild(hint);
  }
  renderLegend(els.legendShape, []);
  renderLegend(els.legendPwm, []);
}

/* ------------------------------------------------------------------ 尾巴 */

function buildTail() {
  const stage = els.tailStage;
  stage.textContent = '';

  const svg = svgEl('svg', {
    class: 'tailSvg',
    viewBox: `0 0 ${TAIL.w} ${TAIL.h}`,
    role: 'img',
    'aria-label': '船尾舵机摆尾示意',
  });

  // 刻度先建空组，等拿到后端 angle_max_deg 再由 setTailScale 填。
  // 范围由后端定（±舵机单侧极限，当前 ±120°），前端不自己写死 ±90。
  const ticks = svgEl('g', {class: 'tailTicks'});
  svg.appendChild(ticks);

  svg.appendChild(
    svgEl('line', {
      class: 'tailCenter',
      x1: TAIL.px,
      y1: TAIL.py,
      x2: TAIL.px,
      y2: TAIL.py - 156,
    }),
  );

  svg.appendChild(
    svgEl('rect', {class: 'tailServo', x: 168, y: 181, width: 64, height: 44, rx: 10}),
  );

  const rot = svgEl('g', {transform: `rotate(0 ${TAIL.px} ${TAIL.py})`});
  rot.appendChild(svgEl('path', {class: 'tailBody', d: 'M200 187 C 200 139, 212 99, 206 55'}));
  rot.appendChild(svgEl('circle', {class: 'tailTip', cx: 206, cy: 55, r: 5}));
  svg.appendChild(rot);

  stage.appendChild(svg);
  state.tailRot = rot;
  state.tailTicks = ticks;
  applyTailColor();
}

/**
 * 画刻度：以上垂方向为 0°，向两侧画到 ±limit。limit 取后端 servo.angle_max_deg，
 * 即舵机单侧最大转角（默认 ±120°），前端不重算、也不放大到极限之外。
 * 正 deg 偏向屏幕右侧。主要刻度（每 45°）更长。
 */
function setTailScale(angleMaxDeg) {
  const group = state.tailTicks;
  if (!group) return;
  group.textContent = '';

  const limit = Math.abs(Number(angleMaxDeg));
  if (!Number.isFinite(limit) || limit <= 0) return;

  for (let deg = -limit; deg <= limit + 1e-6; deg += TAIL_TICK_STEP) {
    const angle = Math.round(deg);
    const rad = (angle * Math.PI) / 180;
    const major = angle % 45 === 0;
    const inner = major ? 138 : 144;
    const outer = major ? 152 : 148;
    group.appendChild(
      svgEl('line', {
        class: 'tailTick',
        x1: TAIL.px + inner * Math.sin(rad),
        y1: TAIL.py - inner * Math.cos(rad),
        x2: TAIL.px + outer * Math.sin(rad),
        y2: TAIL.py - outer * Math.cos(rad),
      }),
    );
  }
}

function currentTailMode() {
  const report = state.report;
  if (!report) return MODES[0];
  const keys = Object.keys(report.modes);
  return report.modes[els.tailMode.value] ? els.tailMode.value : keys[0];
}

function applyTailColor() {
  const mode = currentTailMode();
  els.tailStage.style.setProperty('--tail-color', MODE_COLORS[mode] || MODE_COLORS.envelope);
}

/* ------------------------------------------------------------------ 绘制 */

function drawAll() {
  const report = state.report;
  if (!report) {
    drawPlaceholders();
    return;
  }

  const clipStart = report.clip.start_s;
  const clipDur = Math.max(report.clip.duration_s, 0.001);
  const duration = Math.max(report.duration_s, 0.001);
  const modes = Object.keys(report.modes);
  const servo = report.servo;
  setTailScale(servo.angle_max_deg);

  // 1. 整首波形包络
  state.charts.wave = drawChart(els.chartWave, {
    height: 130,
    x: [0, duration],
    y: [0, 1],
    yCount: 2,
    yTitle: '峰值',
    bands: [[clipStart, clipStart + clipDur]],
    series: [{t: report.wave.t, v: report.wave.peak, fill: true}],
  });

  // 2. 打分曲线：score 为主线，energy / novelty 为分量
  state.charts.score = drawChart(els.chartScore, {
    height: 150,
    x: [0, duration],
    y: [0, 1],
    yTitle: '归一化',
    bands: [[clipStart, clipStart + clipDur]],
    refLines: [{value: report.clip.score}],
    series: [
      {t: report.score.t, v: report.score.energy, color: MODE_COLORS.brightness, width: 1.2,
        dash: '5 4', opacity: 0.75},
      {t: report.score.t, v: report.score.novelty, color: MODE_COLORS.pitch, width: 1.2,
        dash: '5 4', opacity: 0.75},
      {t: report.score.t, v: report.score.score, color: MODE_COLORS.envelope, width: 2},
    ],
  });

  // 3. 片段：细线 = 映射层 raw，粗线 = 成形层 shaped
  const shapeSeries = [];
  for (const mode of modes) {
    const data = report.modes[mode];
    shapeSeries.push({
      t: data.raw.t, v: data.raw.v, color: MODE_COLORS[mode], width: 1.2, opacity: 0.45,
    });
    shapeSeries.push({
      t: data.shaped.t, v: data.shaped.v, color: MODE_COLORS[mode], width: 2.6,
    });
  }
  state.charts.shape = drawChart(els.chartShape, {
    height: 190,
    x: [0, clipDur],
    y: [-1.05, 1.05],
    yTitle: '归一化行程',
    refLines: [{value: 0}],
    beatLines: report.beat ? report.beat.beats_s : [],
    series: shapeSeries,
  });
  renderLegend(els.legendShape, [
    {color: NEUTRAL, width: 1.2, label: '细线 = 映射层原始输出'},
    {color: INK, width: 2.6, label: '粗线 = 成形层输出'},
    ...modes.map((mode) => ({
      color: MODE_COLORS[mode],
      width: 2.4,
      label: `${MODE_LABELS[mode]} ${mode}`,
    })),
    ...(report.beat ? [{color: MODE_COLORS.beat, width: 1, dash: true, label: '节拍点'}] : []),
  ]);

  // 4. PWM 脉宽。纵轴直接用 servo 标定范围，上下各留 100µs 余量
  state.charts.pwm = drawChart(els.chartPwm, {
    height: 170,
    x: [0, clipDur],
    y: [servo.pulse_min_us - 100, servo.pulse_max_us + 100],
    yTitle: '脉宽 (µs)',
    refLines: [{value: servo.pulse_center_us}],
    beatLines: report.beat ? report.beat.beats_s : [],
    series: modes.map((mode) => ({
      t: report.modes[mode].pwm.t,
      v: report.modes[mode].pwm.pulse_us,
      color: MODE_COLORS[mode],
      width: 2,
    })),
  });
  renderLegend(els.legendPwm, [
    ...modes.map((mode) => ({
      color: MODE_COLORS[mode],
      width: 2,
      label: `${MODE_LABELS[mode]} ${mode}`,
    })),
    {color: NEUTRAL, width: 1, dash: true, label: `中位 ${fmtNum(servo.pulse_center_us, 0)}µs`},
  ]);

  // 5. 播放器与下载链接
  els.clip.src = report.audio_url;
  els.csvLink.href = report.csv_url;

  const modeKeys = Object.keys(report.modes);
  const keep = modeKeys.includes(els.tailMode.value) ? els.tailMode.value : modeKeys[0];
  els.tailMode.textContent = '';
  for (const mode of modeKeys) {
    const option = document.createElement('option');
    option.value = mode;
    option.textContent = `${MODE_LABELS[mode] || mode} ${mode}`;
    els.tailMode.appendChild(option);
  }
  els.tailMode.value = keep;
  applyTailColor();

  els.clipMeta.textContent = [
    `来源 ${report.source}`,
    `${fmtNum(report.duration_s, 2)}s · ${report.sr}Hz`,
    `检测区间 ${fmtNum(clipStart, 2)} ~ ${fmtNum(clipStart + clipDur, 2)}s`,
    `得分 ${fmtNum(report.clip.score, 4)}`,
    `${report.clip.auto ? '自动' : '手动'}截取`,
    `栅格 ${fmtNum(report.analysis.grid_hz, 1)}Hz`,
    `限速单步上限 ${fmtNum(report.shaping.step_limit, 4)}`,
    ...(report.beat
      ? [
          `节拍 ${fmtNum(report.beat.bpm, 1)}BPM · 摆动 ${fmtNum(report.beat.swing_s, 2)}s` +
            (report.beat.auto ? '（自动检测）' : '（手动指定）'),
        ]
      : modeKeys.includes('beat')
        ? ['未检出节拍，beat 输出静止居中']
        : []),
  ].join(' · ');
}

/* ------------------------------------------------------------------ 动画 */

function tick() {
  requestAnimationFrame(tick);
  const report = state.report;
  if (!report || !state.tailRot) return;

  const local = els.clip.currentTime || 0;
  const clipStart = report.clip.start_s;
  const clipDur = report.clip.duration_s;

  if (state.charts.wave) state.charts.wave.setPlayhead(clipStart + local, true);
  if (state.charts.score) state.charts.score.setPlayhead(clipStart + local, true);
  if (state.charts.shape) state.charts.shape.setPlayhead(local, local <= clipDur);
  if (state.charts.pwm) state.charts.pwm.setPlayhead(local, local <= clipDur);

  const mode = currentTailMode();
  const data = report.modes[mode];
  if (!data) return;

  const pulse = lookup(local, data.pwm.t, data.pwm.pulse_us);
  const angle = lookup(local, data.pwm.t, data.pwm.angle_deg);
  const value = lookup(local, data.pwm.t, data.pwm.v);

  // 尾巴朝上，rotate(+deg) 本身就是向右摆，所以正行程（脉宽 > 中位）摆向屏幕右侧，
  // 与人的左右直觉一致，不需要取负号。硬件装反时用 mapping.invert 纠正。
  state.tailRot.setAttribute('transform', `rotate(${angle.toFixed(2)} ${TAIL.px} ${TAIL.py})`);
  els.rdPulse.textContent = `${fmtNum(pulse, 0)} µs`;
  els.rdAngle.textContent = `${fmtNum(angle, 1)}°`;
  els.rdValue.textContent = fmtNum(value, 3);
  els.rdTime.textContent = `${fmtNum(local, 2)} / ${fmtNum(clipDur, 2)} s`;
}

/* ------------------------------------------------------------------ 请求 */

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.onerror = () => reject(new Error('读取本地文件失败'));
    reader.readAsDataURL(file);
  });
}

async function analyze() {
  const sourcePath = els.sourcePath.value.trim();
  if (!state.file && !sourcePath) {
    setStatus('请先选择本地音频，或填写服务端路径 / URL', true);
    return;
  }

  els.analyze.disabled = true;
  setStatus('分析中…');
  const started = performance.now();

  try {
    if (state.file && !state.fileB64) {
      state.fileB64 = await readFileBase64(state.file);
    }

    const body = {
      config: readConfig(),
      modes: selectedModes(),
    };
    if (state.baseJob) body.base_job = state.baseJob;
    if (state.file) {
      body.audio_b64 = state.fileB64;
      body.audio_name = state.file.name;
    } else {
      body.source = sourcePath;
    }

    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({error: `HTTP ${response.status}`}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);

    state.report = data;
    state.baseJob = data.job_id;
    els.clip.currentTime = 0;
    drawAll();
    const ms = Math.round(performance.now() - started);
    setStatus(`完成，用时 ${ms}ms · job ${data.job_id}`);
  } catch (error) {
    setStatus(error.message || '分析失败', true);
  } finally {
    els.analyze.disabled = false;
  }
}

/* ------------------------------------------------------------------ 启动 */

function cacheElements() {
  for (const id of [
    'file', 'sourcePath', 'modesRow', 'analyze', 'resetParams', 'status', 'configForm',
    'chartWave', 'chartScore', 'clipMeta', 'chartShape', 'legendShape', 'chartPwm',
    'legendPwm', 'tailStage', 'tailMode', 'rdPulse', 'rdAngle', 'rdValue', 'rdTime',
    'clip', 'csvLink',
  ]) {
    els[id] = $(id);
  }
}

function bindEvents() {
  els.analyze.addEventListener('click', analyze);
  els.resetParams.addEventListener('click', resetParams);
  els.tailMode.addEventListener('change', applyTailColor);
  els.file.addEventListener('change', () => {
    const file = els.file.files && els.file.files[0];
    if (!file) return;
    state.file = file;
    state.fileB64 = null;
    state.baseJob = null;
    els.sourcePath.value = '';
    els.file.parentElement.querySelector('span').textContent = file.name;
    setStatus(`已选择 ${file.name}`);
  });

  els.sourcePath.addEventListener('input', () => {
    // 填了路径就以路径为准，清掉之前选中的文件 —— 否则 analyze 会继续上传那个旧文件。
    if (state.file) {
      state.file = null;
      state.fileB64 = null;
      els.file.value = '';
      els.file.parentElement.querySelector('span').textContent = '选择本地音频';
    }
    state.baseJob = null;
  });

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.report) drawAll();
    }, 160);
  });
}

async function init() {
  cacheElements();
  buildModeChips();
  buildTail();
  drawPlaceholders();
  bindEvents();
  requestAnimationFrame(tick);

  try {
    const response = await fetch('/api/config');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.defaults = await response.json();
    buildForm(state.defaults);
    setTailScale(state.defaults.servo.angle_max_deg);
  } catch (error) {
    setStatus(`读取默认参数失败：${error.message}`, true);
  }
}

init();
