#!/usr/bin/env python3
"""
Mnemos Dashboard — unified visualization for the memory system.

Generates a single self-contained HTML file with four tabs:
  - Overview: stats, timeline chart, distributions
  - Graph: interactive force-directed memory visualization
  - Sessions: encoding quality tracking
  - Projects: project-level knowledge browser

Usage:
    python -m mnemos.visualization.app                              # build + serve
    python -m mnemos.visualization.app --build-only                 # just generate HTML
    python -m mnemos.visualization.app --agent-id vektor            # different agent
    python -m mnemos.visualization.app --port 9000                  # custom port

Or via CLI:
    mnemos ui
    mnemos ui --agent-id vektor --port 9000
"""

from __future__ import annotations

import argparse
import http.server
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from mnemos.visualization.data import extract_all


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_html(data: dict, agent_id: str) -> str:
    """Generate the complete dashboard HTML from extracted data."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    stats = data["stats"]

    # Serialize data for JS
    engrams_json = json.dumps(data["engrams"])
    connections_json = json.dumps(data["connections"])
    timeline_json = json.dumps(data["timeline"])
    projects_json = json.dumps(data["projects"])
    sessions_json = json.dumps(data["sessions"])
    stats_json = json.dumps(stats)
    beliefs_json = json.dumps(data["beliefs"])

    # Pre-build HTML fragments
    stats_cards = _build_stats_cards(stats)
    distributions = _build_distributions(stats)
    recent_engrams = _build_recent_engrams(data["engrams"][:10])
    session_list = _build_session_list(data["sessions"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mnemos — {agent_id}</title>
<style>
{CSS_TOKENS}
{CSS_RESET}
{CSS_TOPBAR}
{CSS_TABS}
{CSS_STATS}
{CSS_TIMELINE}
{CSS_DISTRIBUTIONS}
{CSS_RECENT}
{CSS_GRAPH}
{CSS_GRAPH_PANEL}
{CSS_SESSIONS}
{CSS_PROJECTS}
</style>
</head>
<body>

<div class="topbar">
  <div class="health-dot"></div>
  <h1>MNEMOS</h1>
  <span class="agent-id">{agent_id}</span>
  <span class="topbar-stats">{stats['total_active']} engrams &middot; {stats['total_connections']} connections</span>
  <nav class="tabs">
    <button class="tab active" data-tab="overview">overview</button>
    <button class="tab" data-tab="graph">graph</button>
    <button class="tab" data-tab="sessions">sessions</button>
    <button class="tab" data-tab="projects">projects</button>
  </nav>
</div>

<!-- ═══ OVERVIEW TAB ═══ -->
<div class="view active" id="view-overview">
  <div class="view-scroll">
    <div class="stats-grid">{stats_cards}</div>
    <div class="section-label">encoding timeline</div>
    <div class="timeline-wrap"><canvas id="timeline-canvas"></canvas></div>
    {distributions}
    <div class="section-label">recent encodings</div>
    {recent_engrams}
  </div>
</div>

<!-- ═══ GRAPH TAB ═══ -->
<div class="view" id="view-graph">
  <div class="graph-filters">
    <button class="filter-btn active" data-filter="all">all</button>
    <button class="filter-btn kind-semantic active" data-filter="semantic">semantic</button>
    <button class="filter-btn kind-episodic active" data-filter="episodic">episodic</button>
    <button class="filter-btn kind-procedural active" data-filter="procedural">procedural</button>
  </div>
  <canvas id="graph-canvas"></canvas>
  <div class="graph-panel" id="graph-panel">
    <button class="panel-close" onclick="closeGraphPanel()">&times;</button>
    <div id="graph-panel-body"></div>
  </div>
</div>

<!-- ═══ SESSIONS TAB ═══ -->
<div class="view" id="view-sessions">
  <div class="two-pane">
    <div class="pane-sidebar">
      <div class="pane-header">indexed sessions</div>
      {session_list}
    </div>
    <div class="pane-detail" id="session-detail">
      <div class="pane-empty">Select a session to view extracted memories</div>
    </div>
  </div>
</div>

<!-- ═══ PROJECTS TAB ═══ -->
<div class="view" id="view-projects">
  <div class="two-pane">
    <div class="pane-sidebar">
      <div class="pane-header">projects</div>
      <div id="project-list"></div>
    </div>
    <div class="pane-detail" id="project-detail">
      <div class="pane-empty">Select a project to view its knowledge</div>
    </div>
  </div>
</div>

<script>
// ═══ DATA ═══
const engrams = {engrams_json};
const connections = {connections_json};
const timelineData = {timeline_json};
const projectsData = {projects_json};
const sessionsData = {sessions_json};
const statsData = {stats_json};
const beliefsData = {beliefs_json};

// Index engrams by ID
const engramMap = {{}};
engrams.forEach(e => engramMap[e.id] = e);

{JS_TABS}
{JS_TIMELINE}
{JS_GRAPH}
{JS_SESSIONS}
{JS_PROJECTS}

// Init
initTabs();
drawTimeline();
initGraph();
initSessions();
initProjects();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════

CSS_TOKENS = """
@property --pulse { syntax: '<number>'; initial-value: 0.4; inherits: false; }
@keyframes breathe { 0%, 100% { --pulse: 0.3; } 50% { --pulse: 0.8; } }
@keyframes secondaryExpand { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }

:root {
  --bg-void: #07070a; --bg-deep: #0d0d11; --bg-surface: #121216; --bg-card: #16161b;
  --ink: rgba(244, 242, 238, 0.94);
  --text-primary: rgba(240, 238, 234, 0.88);
  --text-secondary: rgba(210, 208, 204, 0.65);
  --text-tertiary: rgba(180, 178, 174, 0.42);
  --text-ghost: rgba(155, 153, 149, 0.28);
  --text-whisper: rgba(130, 128, 124, 0.16);
  --border: rgba(220, 218, 214, 0.08); --border-hover: rgba(220, 218, 214, 0.14);
  --border-subtle: rgba(220, 218, 214, 0.045);
  --accent-semantic: #7ca8c9; --accent-episodic: #c97ca8; --accent-procedural: #c9a87c;
  --accent-health: #5eba7d;
  --font-sans: 'SF Pro Display', -apple-system, sans-serif;
  --font-mono: 'SF Mono', 'Geist Mono', 'JetBrains Mono', monospace;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-premium: cubic-bezier(0.22, 1, 0.36, 1);
}
"""

CSS_RESET = """
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; overflow: hidden; background: var(--bg-void); color: var(--text-secondary);
  font-family: var(--font-sans); font-size: 13px; -webkit-font-smoothing: antialiased; }
::selection { background: rgba(220,218,214,0.10); }
::-webkit-scrollbar { width: 3px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
"""

CSS_TOPBAR = """
.topbar {
  position: fixed; top: 0; left: 0; right: 0; z-index: 100;
  display: flex; align-items: center; gap: 12px;
  padding: 0 24px; height: 44px;
  background: rgba(7, 7, 10, 0.90); backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  box-shadow: 0 1px 12px rgba(0, 0, 0, 0.3);
}
.health-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--accent-health);
  opacity: var(--pulse); animation: breathe 5s ease-in-out infinite; flex-shrink: 0; }
.topbar h1 { font-family: var(--font-mono); font-size: 10px; font-weight: 500;
  color: var(--text-primary); letter-spacing: 0.12em; }
.agent-id { font-family: var(--font-mono); font-size: 9px; color: var(--text-ghost);
  letter-spacing: 0.03em; }
.topbar-stats { font-family: var(--font-mono); font-size: 9px; color: var(--text-tertiary);
  letter-spacing: 0.03em; margin-left: auto; }
"""

CSS_TABS = """
.tabs { display: flex; gap: 4px; margin-left: 16px; }
.tab { font-family: var(--font-mono); font-size: 9px; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--text-ghost); background: none; border: none;
  padding: 4px 12px; cursor: pointer; position: relative; transition: color 150ms; }
.tab:hover { color: var(--text-tertiary); }
.tab.active { color: var(--text-secondary); }
.tab.active::after { content: ''; position: absolute; bottom: -1px; left: 12px; right: 12px;
  height: 2px; background: var(--text-ghost); border-radius: 1px; }
.view { display: none; position: fixed; top: 44px; left: 0; right: 0; bottom: 0; }
.view.active { display: flex; }
.view-scroll { flex: 1; overflow-y: auto; padding: 24px 32px 64px; max-width: 1200px; }
"""

CSS_STATS = """
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; margin-bottom: 28px; }
.stat-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.stat-val { font-family: var(--font-mono); font-size: 24px; font-weight: 300; color: var(--ink); letter-spacing: -0.02em; }
.stat-label { font-family: var(--font-mono); font-size: 8px; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-ghost); margin-top: 4px; }
.section-label { font-family: var(--font-mono); font-size: 9px; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-ghost); margin-bottom: 12px; margin-top: 8px; }
"""

CSS_TIMELINE = """
.timeline-wrap { height: 180px; margin-bottom: 28px; background: var(--bg-card);
  border: 1px solid var(--border); border-radius: 8px; padding: 16px; position: relative; }
.timeline-wrap canvas { width: 100%; height: 100%; }
.timeline-tooltip { position: absolute; background: var(--bg-deep); border: 1px solid var(--border);
  border-radius: 6px; padding: 8px 10px; font-family: var(--font-mono); font-size: 9px;
  color: var(--text-secondary); pointer-events: none; display: none; z-index: 5;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
"""

CSS_DISTRIBUTIONS = """
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; align-items: start; margin-bottom: 24px; }
@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }
.dist-section { margin-bottom: 8px; }
.dist-row { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.dist-label { font-family: var(--font-mono); font-size: 9px; color: var(--text-tertiary);
  min-width: 100px; text-align: right; letter-spacing: 0.01em; }
.dist-bar { flex: 1; height: 12px; background: var(--bg-surface); border-radius: 3px; overflow: hidden; }
.dist-fill { height: 100%; border-radius: 3px; }
.dist-val { font-family: var(--font-mono); font-size: 9px; color: var(--text-ghost); min-width: 28px; text-align: right; }
.fill-semantic { background: var(--accent-semantic); opacity: 0.5; }
.fill-episodic { background: var(--accent-episodic); opacity: 0.5; }
.fill-procedural { background: var(--accent-procedural); opacity: 0.5; }
.fill-default { background: rgba(200, 198, 194, 0.18); }
"""

CSS_RECENT = """
.recent-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 14px; margin-bottom: 6px; display: flex; gap: 10px; align-items: flex-start; }
.recent-kind { font-family: var(--font-mono); font-size: 8px; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--text-ghost); border: 1px solid var(--border-subtle);
  padding: 1px 5px; border-radius: 100px; flex-shrink: 0; margin-top: 2px; }
.recent-content { font-size: 12px; color: var(--text-secondary); line-height: 1.4;
  flex: 1; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.recent-date { font-family: var(--font-mono); font-size: 9px; color: var(--text-ghost); flex-shrink: 0; }
"""

CSS_GRAPH = """
#view-graph { position: relative; }
#graph-canvas { flex: 1; width: 100%; height: 100%; cursor: grab; }
#graph-canvas:active { cursor: grabbing; }
.graph-filters { position: absolute; top: 12px; left: 20px; z-index: 5; display: flex; gap: 4px; }
.filter-btn { font-family: var(--font-mono); font-size: 9px; text-transform: uppercase;
  letter-spacing: 0.06em; padding: 4px 12px; border-radius: 4px; background: transparent;
  border: 1px solid var(--border); color: var(--text-ghost); cursor: pointer; transition: all 150ms; }
.filter-btn:hover { border-color: var(--border-hover); color: var(--text-tertiary); }
.filter-btn.active { background: rgba(220,218,214,0.05); border-color: rgba(220,218,214,0.18); color: var(--text-secondary); }
.filter-btn.kind-semantic.active { color: var(--accent-semantic); border-color: rgba(124,168,201,0.30); background: rgba(124,168,201,0.05); }
.filter-btn.kind-episodic.active { color: var(--accent-episodic); border-color: rgba(201,124,168,0.30); background: rgba(201,124,168,0.05); }
.filter-btn.kind-procedural.active { color: var(--accent-procedural); border-color: rgba(201,168,124,0.30); background: rgba(201,168,124,0.05); }
"""

CSS_GRAPH_PANEL = """
.graph-panel { position: absolute; top: 0; right: 0; bottom: 0; width: 280px;
  background: var(--bg-deep); border-left: 1px solid var(--border-subtle);
  padding: 20px 16px; overflow-y: auto; z-index: 10;
  transform: translateX(100%); opacity: 0;
  transition: transform 500ms var(--ease-premium), opacity 280ms var(--ease-out); }
.graph-panel.open { transform: translateX(0); opacity: 1; }
.panel-close { position: absolute; top: 12px; right: 12px; font-size: 14px;
  color: var(--text-ghost); background: none; border: none; cursor: pointer; padding: 2px 4px; line-height: 1; }
.panel-close:hover { color: var(--text-tertiary); }
.panel-kind { font-size: 10px; letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--text-ghost); border: 1px solid var(--border); padding: 1px 6px;
  border-radius: 100px; display: inline-block; margin-bottom: 6px; }
.panel-timestamp { font-size: 10px; color: var(--text-ghost); margin-bottom: 16px; }
.panel-content { font-size: 13px; line-height: 1.6; color: var(--text-primary); margin-bottom: 16px; }
.panel-section-wrap { margin-bottom: 16px; }
.panel-section { font-size: 10px; font-weight: 500; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--text-ghost); margin-bottom: 8px; }
.panel-kv { display: flex; align-items: baseline; gap: 8px; font-size: 11px; margin-bottom: 4px; }
.panel-kv-label { color: var(--text-ghost); flex-shrink: 0; }
.panel-kv-value { color: var(--text-tertiary); font-family: var(--font-mono); }
.panel-tags { display: flex; gap: 4px; flex-wrap: wrap; }
.panel-tag { font-size: 9px; color: var(--text-ghost); border: 1px solid rgba(220,218,214,0.06);
  padding: 1px 5px; border-radius: 100px; font-family: var(--font-mono); }
.panel-conn { padding: 10px 0; border-bottom: 1px solid var(--border-subtle);
  cursor: pointer; transition: all 150ms; }
.panel-conn:hover { background: rgba(220,218,214,0.032); margin: 0 -8px; padding: 10px 8px; border-radius: 6px; }
.panel-conn:last-child { border-bottom: none; }
.panel-conn-header { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.panel-conn-kind { font-size: 9px; letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--text-ghost); border: 1px solid var(--border-subtle); padding: 0 4px; border-radius: 100px; }
.panel-conn-str { width: 32px; height: 2px; background: var(--bg-card); border-radius: 1px;
  overflow: hidden; margin-left: auto; }
.panel-conn-str-fill { height: 100%; background: var(--text-ghost); border-radius: 1px; }
.panel-conn-label { font-size: 11px; color: var(--text-tertiary); line-height: 1.4;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.panel-conn.expanded { background: rgba(220,218,214,0.04); margin: 0 -8px; padding: 10px 8px;
  border-radius: 6px; border-color: rgba(220,218,214,0.04); }
.panel-secondary { margin-top: 8px; padding-top: 8px; border-top: 1px solid rgba(220,218,214,0.06);
  animation: secondaryExpand 280ms var(--ease-premium); }
.panel-secondary-impact { font-size: 11px; color: var(--text-tertiary); line-height: 1.45; margin-bottom: 8px; }
.panel-secondary-metrics { display: flex; gap: 10px; }
.panel-sec-metric { display: flex; align-items: center; gap: 4px; flex: 1; }
.panel-sec-metric-label { font-size: 9px; color: var(--text-ghost); font-family: var(--font-mono); flex-shrink: 0; }
.panel-sec-metric-track { flex: 1; height: 2px; background: var(--bg-card); border-radius: 1px; overflow: hidden; }
.panel-sec-metric-fill { height: 100%; background: var(--text-ghost); border-radius: 1px; }
.panel-sec-metric-val { font-size: 9px; color: var(--text-ghost); font-family: var(--font-mono);
  min-width: 24px; text-align: right; }
.panel-secondary-conncount { font-size: 9px; color: var(--text-ghost); font-family: var(--font-mono); margin-top: 6px; }
"""

CSS_SESSIONS = """
.two-pane { display: flex; height: 100%; }
.pane-sidebar { width: 280px; min-width: 280px; border-right: 1px solid var(--border-subtle);
  overflow-y: auto; padding: 16px 0; }
.pane-header { font-family: var(--font-mono); font-size: 9px; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-ghost); padding: 0 16px; margin-bottom: 12px; }
.pane-detail { flex: 1; overflow-y: auto; padding: 20px 24px; }
.pane-empty { font-size: 12px; color: var(--text-ghost); padding: 40px 0; }
.session-item { display: flex; align-items: center; gap: 8px; padding: 8px 16px;
  cursor: pointer; border-left: 2px solid transparent; transition: all 150ms; }
.session-item:hover { background: rgba(220,218,214,0.032); }
.session-item.active { background: rgba(220,218,214,0.05); border-left-color: var(--text-ghost); }
.session-dot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }
.session-dot.ok { background: var(--accent-health); }
.session-dot.skip { background: var(--text-ghost); }
.session-info { flex: 1; min-width: 0; }
.session-id { font-family: var(--font-mono); font-size: 9px; color: var(--text-tertiary);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.session-meta { font-family: var(--font-mono); font-size: 8px; color: var(--text-ghost); margin-top: 1px; }
.session-count { font-family: var(--font-mono); font-size: 9px; color: var(--text-ghost); flex-shrink: 0; }
.memory-item { padding: 10px 0; border-bottom: 1px solid var(--border-subtle); }
.memory-item:last-child { border-bottom: none; }
.memory-header { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.memory-kind { font-size: 9px; letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--text-ghost); border: 1px solid var(--border-subtle); padding: 0 4px; border-radius: 100px; }
.memory-conf { font-family: var(--font-mono); font-size: 9px; color: var(--text-ghost); margin-left: auto; }
.memory-content { font-size: 12px; color: var(--text-secondary); line-height: 1.45; margin-bottom: 4px; }
.memory-tags { display: flex; gap: 3px; flex-wrap: wrap; }
.memory-tag { font-family: var(--font-mono); font-size: 8px; color: var(--text-ghost);
  border: 1px solid rgba(220,218,214,0.04); padding: 0 4px; border-radius: 100px; }
"""

CSS_PROJECTS = """
.project-item { padding: 8px 16px; cursor: pointer; border-left: 2px solid transparent;
  transition: all 150ms; display: flex; justify-content: space-between; align-items: center; }
.project-item:hover { background: rgba(220,218,214,0.032); }
.project-item.active { background: rgba(220,218,214,0.05); border-left-color: var(--text-ghost); }
.project-name { font-family: var(--font-mono); font-size: 11px; color: var(--text-tertiary); }
.project-count { font-family: var(--font-mono); font-size: 9px; color: var(--text-ghost); }
.project-header { margin-bottom: 20px; }
.project-title { font-size: 15px; color: var(--text-primary); margin-bottom: 4px; }
.project-meta { font-family: var(--font-mono); font-size: 9px; color: var(--text-ghost); }
.project-group { margin-bottom: 20px; }
.project-group-label { font-family: var(--font-mono); font-size: 9px; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-ghost); margin-bottom: 8px; }
.project-memory { padding: 6px 0; border-bottom: 1px solid var(--border-subtle);
  font-size: 12px; color: var(--text-secondary); line-height: 1.4; cursor: pointer; transition: color 150ms; }
.project-memory:hover { color: var(--text-primary); }
.project-memory:last-child { border-bottom: none; }
.project-memory-meta { font-family: var(--font-mono); font-size: 9px; color: var(--text-ghost); margin-top: 2px; }
"""


# ══════════════════════════════════════════════════════════════
# JS
# ══════════════════════════════════════════════════════════════

JS_TABS = """
function initTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('view-' + tab.dataset.tab).classList.add('active');
      location.hash = tab.dataset.tab;

      // Resize canvas on tab switch
      if (tab.dataset.tab === 'graph') setTimeout(resizeGraph, 50);
      if (tab.dataset.tab === 'overview') setTimeout(drawTimeline, 50);
    });
  });

  // Handle hash on load
  const hash = location.hash.slice(1);
  if (hash && document.querySelector(`[data-tab="${hash}"]`)) {
    document.querySelector(`[data-tab="${hash}"]`).click();
  }
}
"""

JS_TIMELINE = """
function drawTimeline() {
  const canvas = document.getElementById('timeline-canvas');
  if (!canvas || !canvas.parentElement) return;
  const wrap = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const W = wrap.clientWidth - 32;
  const H = wrap.clientHeight - 32;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const days = Object.keys(timelineData).sort();
  if (days.length === 0) {
    ctx.fillStyle = 'rgba(155,153,149,0.28)';
    ctx.font = '10px "SF Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText('No timeline data yet', W/2, H/2);
    return;
  }

  const kinds = ['semantic', 'episodic', 'procedural'];
  const colors = { semantic: 'rgba(124,168,201,0.4)', episodic: 'rgba(201,124,168,0.4)', procedural: 'rgba(201,168,124,0.4)' };

  // Compute stacked totals
  let maxTotal = 0;
  const stacked = days.map(d => {
    const vals = {};
    let total = 0;
    kinds.forEach(k => { vals[k] = (timelineData[d] || {})[k] || 0; total += vals[k]; });
    maxTotal = Math.max(maxTotal, total);
    return { day: d, vals, total };
  });
  maxTotal = Math.max(maxTotal, 1);

  const pad = { left: 40, right: 16, top: 12, bottom: 24 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;
  const barW = Math.max(4, Math.min(40, plotW / days.length - 2));

  // Y-axis grid
  ctx.strokeStyle = 'rgba(220,218,214,0.04)';
  ctx.lineWidth = 0.5;
  const ySteps = Math.min(maxTotal, 5);
  for (let i = 0; i <= ySteps; i++) {
    const y = pad.top + plotH - (i / ySteps) * plotH;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
    ctx.fillStyle = 'rgba(155,153,149,0.20)';
    ctx.font = '9px "SF Mono", monospace';
    ctx.textAlign = 'right';
    ctx.fillText(Math.round(i / ySteps * maxTotal), pad.left - 6, y + 3);
  }

  // Bars (stacked)
  stacked.forEach((s, i) => {
    const x = pad.left + (i / days.length) * plotW + (plotW / days.length - barW) / 2;
    let yOffset = 0;
    kinds.forEach(k => {
      const val = s.vals[k];
      if (val === 0) return;
      const barH = (val / maxTotal) * plotH;
      const y = pad.top + plotH - yOffset - barH;
      ctx.fillStyle = colors[k];
      ctx.beginPath();
      ctx.roundRect(x, y, barW, barH, 2);
      ctx.fill();
      yOffset += barH;
    });

    // Date label
    ctx.fillStyle = 'rgba(155,153,149,0.20)';
    ctx.font = '8px "SF Mono", monospace';
    ctx.textAlign = 'center';
    const label = s.day.slice(5); // MM-DD
    ctx.fillText(label, x + barW / 2, H - 4);
  });
}
"""

JS_GRAPH = """
// Graph engine (adapted from mnemos-graph.py)
const PRIMES = [3, 5, 7, 11];
const NODE_BASE_RADIUS = 2.5, NODE_MAX_RADIUS = 5, HIT_RADIUS = 12;
const REPULSION = 400, ATTRACTION = 0.004, DAMPING = 0.82, CENTER_PULL = 0.0008;
let graphNodes = [], graphEdges = [], graphNodeMap = {};
let graphSelectedId = null, graphHoveredId = null;
let activeKinds = new Set(['semantic', 'episodic', 'procedural']);
let gCamX = 0, gCamY = 0, gCamZoom = 1;
let gIsDragging = false, gDragDist = 0, gDragStartX, gDragStartY, gCamStartX, gCamStartY;
let gSimulating = true, gSimTicks = 0;
let gCanvas, gCtx, gW, gH;

function initGraph() {
  gCanvas = document.getElementById('graph-canvas');
  if (!gCanvas) return;
  gCtx = gCanvas.getContext('2d');

  // Count connections for hub detection
  const connCount = {};
  connections.forEach(c => { connCount[c.source] = (connCount[c.source]||0)+1; connCount[c.target] = (connCount[c.target]||0)+1; });

  engrams.forEach((e, i) => {
    const angle = (i / engrams.length) * Math.PI * 2;
    const r = 100 + Math.random() * 250;
    const nc = connCount[e.id] || 0;
    const isHub = nc >= 6;
    const baseR = isHub ? NODE_BASE_RADIUS * 1.6 : NODE_BASE_RADIUS;
    const maxR = isHub ? NODE_MAX_RADIUS * 1.4 : NODE_MAX_RADIUS;
    const node = { ...e, x: Math.cos(angle)*r, y: Math.sin(angle)*r, vx: 0, vy: 0,
      radius: baseR + (e.strength||0.5) * (maxR - baseR),
      phase: Math.random() * Math.PI * 2, breathPeriod: PRIMES[i % PRIMES.length],
      visible: true, isHub, connCount: nc };
    graphNodes.push(node);
    graphNodeMap[e.id] = node;
  });

  connections.forEach(c => {
    if (graphNodeMap[c.source] && graphNodeMap[c.target])
      graphEdges.push({ ...c, fromNode: graphNodeMap[c.source], toNode: graphNodeMap[c.target] });
  });

  for (let i = 0; i < 500; i++) { graphSimulate(0.016); gSimTicks++; }

  if (graphNodes.length > 0) {
    let cx=0,cy=0; graphNodes.forEach(n=>{cx+=n.x;cy+=n.y;});
    gCamX = -(cx/graphNodes.length); gCamY = -(cy/graphNodes.length);
  }

  gCanvas.addEventListener('mousedown', e => { gDragDist=0; gIsDragging=true; gDragStartX=e.offsetX; gDragStartY=e.offsetY; gCamStartX=gCamX; gCamStartY=gCamY; });
  gCanvas.addEventListener('mousemove', e => {
    if (gIsDragging) { const dx=e.offsetX-gDragStartX,dy=e.offsetY-gDragStartY; gDragDist=Math.sqrt(dx*dx+dy*dy);
      if(gDragDist>5){gCamX=gCamStartX+dx/gCamZoom;gCamY=gCamStartY+dy/gCamZoom;}}
    const node=findGraphNode(e.offsetX,e.offsetY); graphHoveredId=node?node.id:null;
    gCanvas.style.cursor=node?'pointer':(gIsDragging&&gDragDist>5?'grabbing':'grab');
  });
  gCanvas.addEventListener('mouseup', ()=>{gIsDragging=false;});
  gCanvas.addEventListener('wheel', e=>{e.preventDefault();gCamZoom=Math.max(0.1,Math.min(5,gCamZoom*(e.deltaY>0?0.92:1.08)));},{passive:false});
  gCanvas.addEventListener('click', e=>{
    if(gDragDist>5)return;const node=findGraphNode(e.offsetX,e.offsetY);
    if(node){graphSelectedId=node.id;showGraphPanel(node);}else{closeGraphPanel();}
  });

  document.querySelectorAll('.filter-btn').forEach(btn=>{
    btn.addEventListener('click',()=>{
      const f=btn.dataset.filter;
      if(f==='all'){const all=activeKinds.size===3;activeKinds=all?new Set():new Set(['semantic','episodic','procedural']);}
      else{activeKinds.has(f)?activeKinds.delete(f):activeKinds.add(f);}
      graphNodes.forEach(n=>{n.visible=activeKinds.has(n.kind);});
      document.querySelectorAll('.filter-btn').forEach(b=>{
        const bf=b.dataset.filter;
        b.classList.toggle('active',bf==='all'?activeKinds.size===3:activeKinds.has(bf));
      });
    });
  });

  requestAnimationFrame(renderGraph);
}

function resizeGraph() {
  if (!gCanvas || !gCanvas.parentElement) return;
  const dpr = window.devicePixelRatio || 1;
  gW = gCanvas.parentElement.clientWidth; gH = gCanvas.parentElement.clientHeight;
  gCanvas.width = gW * dpr; gCanvas.height = gH * dpr;
  gCanvas.style.width = gW + 'px'; gCanvas.style.height = gH + 'px';
  gCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function graphSimulate(dt) {
  const vis = graphNodes.filter(n=>n.visible);
  for(let i=0;i<vis.length;i++){for(let j=i+1;j<vis.length;j++){
    const a=vis[i],b=vis[j];let dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||1;
    let f=REPULSION/(d*d),fx=(dx/d)*f,fy=(dy/d)*f;a.vx-=fx;a.vy-=fy;b.vx+=fx;b.vy+=fy;}}
  graphEdges.forEach(e=>{if(!e.fromNode.visible||!e.toNode.visible)return;
    let dx=e.toNode.x-e.fromNode.x,dy=e.toNode.y-e.fromNode.y,d=Math.sqrt(dx*dx+dy*dy)||1;
    let f=d*ATTRACTION*(e.strength||0.5),fx=(dx/d)*f,fy=(dy/d)*f;
    e.fromNode.vx+=fx;e.fromNode.vy+=fy;e.toNode.vx-=fx;e.toNode.vy-=fy;});
  vis.forEach(n=>{n.vx-=n.x*CENTER_PULL;n.vy-=n.y*CENTER_PULL;n.vx*=DAMPING;n.vy*=DAMPING;n.x+=n.vx;n.y+=n.vy;});
}

let gLastTime = 0;
function renderGraph(ts) {
  if (!gCanvas) return;
  if (!gW) resizeGraph();
  const dt = Math.min((ts-gLastTime)/1000, 0.05); gLastTime = ts;
  if (gSimulating && gSimTicks < 700) { graphSimulate(dt*0.15); gSimTicks++;
    let e=0; graphNodes.forEach(n=>{e+=n.vx*n.vx+n.vy*n.vy;}); if(e<0.01&&gSimTicks>500)gSimulating=false; }

  gCtx.clearRect(0,0,gW,gH);
  // Vignette
  const vig=gCtx.createRadialGradient(gW/2,gH/2,gW*0.15,gW/2,gH/2,gW*0.7);
  vig.addColorStop(0,'rgba(14,14,18,0)');vig.addColorStop(0.6,'rgba(7,7,10,0)');vig.addColorStop(1,'rgba(3,3,5,0.4)');
  gCtx.fillStyle=vig;gCtx.fillRect(0,0,gW,gH);

  gCtx.save();gCtx.translate(gW/2+gCamX*gCamZoom,gH/2+gCamY*gCamZoom);gCtx.scale(gCamZoom,gCamZoom);
  const t=ts/1000;

  const connIds=new Set();
  if(graphSelectedId){graphEdges.forEach(e=>{if(e.source===graphSelectedId)connIds.add(e.target);if(e.target===graphSelectedId)connIds.add(e.source);});}

  // Edges
  graphEdges.forEach(e=>{if(!e.fromNode.visible||!e.toNode.visible)return;
    const hi=graphSelectedId&&(e.source===graphSelectedId||e.target===graphSelectedId);
    gCtx.beginPath();gCtx.moveTo(e.fromNode.x,e.fromNode.y);gCtx.lineTo(e.toNode.x,e.toNode.y);
    gCtx.strokeStyle=`rgba(210,208,204,${hi?0.28:(graphSelectedId?0.025:0.06)})`;gCtx.lineWidth=hi?1.0:0.5;gCtx.stroke();
  });

  // Nodes
  graphNodes.forEach(n=>{if(!n.visible)return;
    const breath=Math.sin(2*Math.PI*t/n.breathPeriod+n.phase);
    const r=n.radius*(0.92+0.08*breath);
    const isSel=n.id===graphSelectedId,isHov=n.id===graphHoveredId,isConn=connIds.has(n.id);
    const hubBoost=n.isHub?0.12:0;
    let alpha=0.30+n.accessibility*0.35+breath*0.04+hubBoost;
    if(isSel)alpha=1.0;else if(isConn)alpha=0.75;else if(isHov)alpha=0.85;else if(graphSelectedId)alpha*=0.25;

    const gr=gCtx.createRadialGradient(n.x,n.y,r*0.3,n.x,n.y,r*1.8);
    gr.addColorStop(0,`rgba(220,218,214,${alpha*0.4})`);gr.addColorStop(0.5,`rgba(200,198,194,${alpha*0.12})`);gr.addColorStop(1,'rgba(180,178,174,0)');
    gCtx.fillStyle=gr;gCtx.beginPath();gCtx.arc(n.x,n.y,r*1.8,0,Math.PI*2);gCtx.fill();
    gCtx.fillStyle=`rgba(230,228,224,${alpha})`;gCtx.beginPath();gCtx.arc(n.x,n.y,r,0,Math.PI*2);gCtx.fill();
    if(isSel){gCtx.strokeStyle='rgba(240,238,234,0.6)';gCtx.lineWidth=0.8;gCtx.beginPath();gCtx.arc(n.x,n.y,r+4,0,Math.PI*2);gCtx.stroke();}
  });
  gCtx.restore();
  requestAnimationFrame(renderGraph);
}

function findGraphNode(sx,sy){const wx=(sx-gW/2-gCamX*gCamZoom)/gCamZoom,wy=(sy-gH/2-gCamY*gCamZoom)/gCamZoom;
  let cl=null,cd=Infinity;graphNodes.forEach(n=>{if(!n.visible)return;const dx=n.x-wx,dy=n.y-wy,d=Math.sqrt(dx*dx+dy*dy);
  if(d<HIT_RADIUS&&d<cd){cl=n;cd=d;}});return cl;}

function showGraphPanel(node) {
  const panel=document.getElementById('graph-panel');const body=document.getElementById('graph-panel-body');
  const months=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  let dateStr='';if(node.created_at){const d=new Date(node.created_at);dateStr=`created ${months[d.getMonth()]} ${d.getDate()}`;}
  const nodeConns=graphEdges.filter(e=>e.source===node.id||e.target===node.id);
  const kv=(l,v)=>`<div class="panel-kv"><span class="panel-kv-label">${l}</span><span class="panel-kv-value">${v}</span></div>`;
  const connHtml=nodeConns.slice(0,20).map(e=>{
    const oid=e.source===node.id?e.target:e.source;const o=graphNodeMap[oid];if(!o)return'';
    const pre=(o.impact||o.content).substring(0,80);const sp=((e.strength||0.5)*100).toFixed(0);
    return `<div class="panel-conn" data-target="${oid}"><div class="panel-conn-header"><span class="panel-conn-kind">${o.kind||'semantic'}</span><div class="panel-conn-str"><div class="panel-conn-str-fill" style="width:${sp}%"></div></div></div><div class="panel-conn-label">${pre}</div></div>`;
  }).join('');
  const tagsHtml=node.tags.map(t=>`<span class="panel-tag">${t}</span>`).join('');

  body.innerHTML=`<span class="panel-kind">${node.kind}</span><div class="panel-timestamp">${dateStr}</div>
    <div class="panel-content">${(node.content||'').substring(0,400)}</div>
    <div class="panel-section-wrap"><div class="panel-section">metrics</div>${kv('strength',node.strength.toFixed(2))}${kv('stability',node.stability.toFixed(2))}${kv('accessibility',node.accessibility.toFixed(2))}</div>
    <div class="panel-section-wrap"><div class="panel-section">encoding</div>${kv('depth',node.encoding_depth||'unknown')}${kv('surprise',(node.surprise_level||0).toFixed(2))}${kv('attention',(node.attention_level||0.5).toFixed(2))}</div>
    <div class="panel-section-wrap"><div class="panel-section">source</div>${kv('type',node.source_type)}${kv('confidence',node.confidence.toFixed(2))}</div>
    <div class="panel-section-wrap"><div class="panel-section">connections (${nodeConns.length})</div>${connHtml||kv('none','')}</div>
    ${node.tags.length?`<div class="panel-section-wrap"><div class="panel-section">tags</div><div class="panel-tags">${tagsHtml}</div></div>`:''}
    <div class="panel-section-wrap"><div class="panel-section">history</div>${kv('reconsolidations',node.reconsolidation_count||0)}${kv('access count',node.access_count||0)}</div>`;

  // Connection expansion
  body.querySelectorAll('.panel-conn').forEach(card=>{card.addEventListener('click',evt=>{
    evt.stopPropagation();const tid=card.dataset.target;const tgt=graphNodeMap[tid];if(!tgt)return;
    const ex=card.querySelector('.panel-secondary');if(ex){ex.remove();card.classList.remove('expanded');return;}
    body.querySelectorAll('.panel-conn.expanded').forEach(c=>{c.classList.remove('expanded');const s=c.querySelector('.panel-secondary');if(s)s.remove();});
    card.classList.add('expanded');
    const mm=(l,v)=>{const p=((v||0)*100).toFixed(0);return`<div class="panel-sec-metric"><span class="panel-sec-metric-label">${l}</span><div class="panel-sec-metric-track"><div class="panel-sec-metric-fill" style="width:${p}%"></div></div><span class="panel-sec-metric-val">${p}%</span></div>`;};
    const tc=graphEdges.filter(e=>e.source===tid||e.target===tid);
    const sec=document.createElement('div');sec.className='panel-secondary';
    sec.innerHTML=`<div class="panel-secondary-impact">${(tgt.impact||tgt.content).substring(0,150)}</div><div class="panel-secondary-metrics">${mm('str',tgt.strength)}${mm('stb',tgt.stability)}${mm('acc',tgt.accessibility)}</div><div class="panel-secondary-conncount">${tc.length} connections</div>`;
    card.appendChild(sec);sec.addEventListener('dblclick',()=>{graphSelectedId=tid;showGraphPanel(tgt);gCamX=-tgt.x;gCamY=-tgt.y;});
  });});
  panel.classList.add('open');
}

function closeGraphPanel(){graphSelectedId=null;document.getElementById('graph-panel').classList.remove('open');}
"""

JS_SESSIONS = """
function initSessions() {
  document.querySelectorAll('.session-item').forEach(item => {
    item.addEventListener('click', () => {
      document.querySelectorAll('.session-item').forEach(i => i.classList.remove('active'));
      item.classList.add('active');
      showSessionDetail(item.dataset.session);
    });
  });
}

function showSessionDetail(sessionKey) {
  const detail = document.getElementById('session-detail');
  const session = sessionsData[sessionKey];
  if (!session) { detail.innerHTML = '<div class="pane-empty">Session not found</div>'; return; }

  const eids = session.engram_ids || [];
  const memories = eids.map(id => engramMap[id]).filter(Boolean);
  const indexedAt = (session.indexed_at || '').substring(0, 19);
  const sizeKb = ((session.size || 0) / 1024).toFixed(0);
  const skipped = session.skipped || '';

  let memoriesHtml = '';
  if (memories.length === 0) {
    memoriesHtml = `<div class="pane-empty">${skipped ? 'Skipped: ' + skipped : 'No memories extracted'}</div>`;
  } else {
    memoriesHtml = memories.map(m => {
      const tags = m.tags.map(t => `<span class="memory-tag">${t}</span>`).join('');
      const conns = connections.filter(c => c.source === m.id || c.target === m.id).length;
      return `<div class="memory-item">
        <div class="memory-header"><span class="memory-kind">${m.kind}</span><span class="memory-conf">${m.confidence.toFixed(2)} conf · ${conns} conn</span></div>
        <div class="memory-content">${(m.impact || m.content).substring(0, 200)}</div>
        ${m.tags.length ? `<div class="memory-tags">${tags}</div>` : ''}
      </div>`;
    }).join('');
  }

  detail.innerHTML = `
    <div class="panel-section-wrap">
      <div class="panel-section">session</div>
      <div class="panel-kv"><span class="panel-kv-label">id</span><span class="panel-kv-value" style="font-size:9px">${sessionKey}</span></div>
      <div class="panel-kv"><span class="panel-kv-label">indexed</span><span class="panel-kv-value">${indexedAt}</span></div>
      <div class="panel-kv"><span class="panel-kv-label">size</span><span class="panel-kv-value">${sizeKb} KB</span></div>
      <div class="panel-kv"><span class="panel-kv-label">memories</span><span class="panel-kv-value">${session.memories_encoded || 0}</span></div>
    </div>
    <div class="panel-section">extracted memories (${memories.length})</div>
    ${memoriesHtml}
  `;
}
"""

JS_PROJECTS = """
function initProjects() {
  const list = document.getElementById('project-list');
  const projectNames = Object.keys(projectsData).sort((a,b) => projectsData[b].length - projectsData[a].length);

  list.innerHTML = projectNames.map(name => {
    const count = projectsData[name].length;
    return `<div class="project-item" data-project="${name}">
      <span class="project-name">${name}</span>
      <span class="project-count">${count}</span>
    </div>`;
  }).join('');

  list.querySelectorAll('.project-item').forEach(item => {
    item.addEventListener('click', () => {
      list.querySelectorAll('.project-item').forEach(i => i.classList.remove('active'));
      item.classList.add('active');
      showProjectDetail(item.dataset.project);
    });
  });
}

function showProjectDetail(projectName) {
  const detail = document.getElementById('project-detail');
  const eids = projectsData[projectName] || [];
  const memories = eids.map(id => engramMap[id]).filter(Boolean);

  // Group by type
  const groups = { decisions: [], patterns: [], lessons: [], facts: [], events: [] };
  memories.forEach(m => {
    if (m.kind === 'episodic') groups.events.push(m);
    else if (m.tags.includes('decision')) groups.decisions.push(m);
    else if (m.tags.includes('pattern')) groups.patterns.push(m);
    else if (m.tags.includes('lesson')) groups.lessons.push(m);
    else groups.facts.push(m);
  });

  // Date range
  const dates = memories.map(m => m.created_at).filter(Boolean).sort();
  const range = dates.length ? `${dates[0].slice(0,10)} — ${dates[dates.length-1].slice(0,10)}` : '';

  let groupsHtml = '';
  for (const [label, items] of Object.entries(groups)) {
    if (items.length === 0) continue;
    const itemsHtml = items.map(m => {
      const preview = (m.impact || m.content).substring(0, 120);
      const date = (m.created_at || '').slice(5, 10);
      return `<div class="project-memory" data-engram-id="${m.id}">
        ${preview}
        <div class="project-memory-meta">str ${m.strength.toFixed(2)} · ${date}</div>
      </div>`;
    }).join('');
    groupsHtml += `<div class="project-group"><div class="project-group-label">${label} (${items.length})</div>${itemsHtml}</div>`;
  }

  detail.innerHTML = `
    <div class="project-header">
      <div class="project-title">${projectName}</div>
      <div class="project-meta">${memories.length} memories · ${range}</div>
    </div>
    ${groupsHtml || '<div class="pane-empty">No memories in this project</div>'}
  `;

  // Click memory → jump to graph
  detail.querySelectorAll('.project-memory').forEach(el => {
    el.addEventListener('click', () => {
      const eid = el.dataset.engramId;
      const node = graphNodeMap[eid];
      if (node) {
        graphSelectedId = eid; gCamX = -node.x; gCamY = -node.y;
        showGraphPanel(node);
        document.querySelector('[data-tab="graph"]').click();
      }
    });
  });
}
"""


# ══════════════════════════════════════════════════════════════
# HTML Builders
# ══════════════════════════════════════════════════════════════

def _build_stats_cards(stats: dict) -> str:
    cards = [
        (stats["total_active"], "active engrams"),
        (stats["total_connections"], "connections"),
        (stats["avg_connections"], "avg conn/engram"),
        (stats["total_beliefs"], "beliefs"),
        (stats["sessions_indexed"], "sessions indexed"),
        (stats["total_encoded"], "total encoded"),
        (stats["total_dormant"], "dormant"),
    ]
    return "".join(
        f'<div class="stat-card"><div class="stat-val">{v}</div><div class="stat-label">{l}</div></div>'
        for v, l in cards
    )


def _build_distributions(stats: dict) -> str:
    def dist_section(title: str, data: dict, color_fn=None) -> str:
        if not data:
            return ""
        max_val = max(data.values()) or 1
        rows = ""
        for k, v in data.items():
            pct = int(v / max_val * 100)
            cls = color_fn(k) if color_fn else "fill-default"
            rows += (
                f'<div class="dist-row"><span class="dist-label">{escape_html(str(k))}</span>'
                f'<div class="dist-bar"><div class="dist-fill {cls}" style="width:{pct}%"></div></div>'
                f'<span class="dist-val">{v}</span></div>'
            )
        return f'<div class="dist-section"><div class="section-label">{title}</div>{rows}</div>'

    kind_colors = {"semantic": "fill-semantic", "episodic": "fill-episodic", "procedural": "fill-procedural"}

    html = '<div class="two-col">'
    html += dist_section("memory types", stats["kind_counts"], lambda k: kind_colors.get(k, "fill-default"))
    html += dist_section("connection types", stats["conn_type_counts"])
    html += '</div><div class="two-col">'
    html += dist_section("strength distribution", stats["str_buckets"])
    html += dist_section("accessibility distribution", stats["acc_buckets"])
    html += '</div><div class="two-col">'
    html += dist_section("top tags", stats["tag_counts"])
    html += dist_section("source types", stats["source_counts"])
    html += '</div>'
    return html


def _build_recent_engrams(engrams: list) -> str:
    html = ""
    for e in engrams:
        content = escape_html((e.get("impact") or e.get("content", ""))[:120])
        kind = e.get("kind", "semantic")
        date = (e.get("created_at") or "")[:10]
        html += (
            f'<div class="recent-card">'
            f'<span class="recent-kind">{kind}</span>'
            f'<span class="recent-content">{content}</span>'
            f'<span class="recent-date">{date}</span>'
            f'</div>'
        )
    return html


def _build_session_list(sessions: dict) -> str:
    sorted_sessions = sorted(
        sessions.items(),
        key=lambda x: x[1].get("indexed_at", ""),
        reverse=True,
    )
    html = ""
    for key, info in sorted_sessions:
        short_key = key[:16] + "..." if len(key) > 16 else key
        count = info.get("memories_encoded", 0)
        skipped = info.get("skipped", "")
        indexed_at = (info.get("indexed_at") or "")[:10]
        size_kb = (info.get("size", 0) / 1024)
        dot_class = "ok" if count > 0 else "skip"

        html += (
            f'<div class="session-item" data-session="{escape_html(key)}">'
            f'<div class="session-dot {dot_class}"></div>'
            f'<div class="session-info">'
            f'<div class="session-id">{escape_html(short_key)}</div>'
            f'<div class="session-meta">{indexed_at} · {size_kb:.0f}kb</div>'
            f'</div>'
            f'<span class="session-count">{count}</span>'
            f'</div>'
        )
    return html


# ══════════════════════════════════════════════════════════════
# Server
# ══════════════════════════════════════════════════════════════

def serve(html: str, port: int = 8401, db_path: str = "", agent_id: str = "default"):
    """Serve the dashboard with auto-rebuild on database changes."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(handler_state["html"].encode())
            elif self.path == "/api/reload":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                changed = handler_state["reload_event"].wait(timeout=30)
                if changed:
                    handler_state["reload_event"].clear()
                self.wfile.write(json.dumps({"reload": changed}).encode())
            else:
                self.send_error(404)

        def log_message(self, fmt, *args):
            pass

    handler_state = {"html": html, "reload_event": threading.Event()}

    def watcher():
        db = Path(db_path)
        last_mtime = db.stat().st_mtime if db.exists() else 0
        while True:
            time.sleep(5)
            if db.exists():
                mtime = db.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    try:
                        data = extract_all(str(db), agent_id)
                        handler_state["html"] = build_html(data, agent_id)
                        handler_state["reload_event"].set()
                        print(f"  rebuilt: {data['stats']['total_active']} engrams")
                    except Exception as e:
                        print(f"  rebuild error: {e}")

    if db_path:
        t = threading.Thread(target=watcher, daemon=True)
        t.start()

    print(f"Mnemos UI serving at http://localhost:{port}")
    print(f"Agent: {agent_id} | DB: {db_path}")
    print("Press Ctrl+C to stop.\n")

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Mnemos Dashboard")
    parser.add_argument("--agent-id", default="claude-field", help="Agent ID")
    parser.add_argument("--db-path", help="Database path (default: ~/.mnemos/{agent-id}.db)")
    parser.add_argument("--port", type=int, default=8401, help="Server port")
    parser.add_argument("--build-only", action="store_true", help="Generate HTML without serving")
    parser.add_argument("--output", help="Output path (default: ./mnemos-ui.html)")
    args = parser.parse_args()

    db_path = args.db_path or str(Path.home() / ".mnemos" / f"{args.agent_id}.db")

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        print(f"Run 'mnemos init' first, or specify --db-path")
        sys.exit(1)

    print(f"Extracting data from {db_path}...")
    data = extract_all(db_path, args.agent_id)
    html = build_html(data, args.agent_id)
    print(f"  {data['stats']['total_active']} engrams, {data['stats']['total_connections']} connections")

    if args.build_only:
        out = args.output or "mnemos-ui.html"
        Path(out).write_text(html, encoding="utf-8")
        print(f"Built {out}")
    else:
        if args.output:
            Path(args.output).write_text(html, encoding="utf-8")
        serve(html, args.port, db_path, args.agent_id)


if __name__ == "__main__":
    main()
