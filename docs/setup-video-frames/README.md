# Mnemos setup how-to — video frames

Six 1280×720 (16:9) keyframes for the Mnemos setup how-to video, in the locked
all-monochrome design system (full system in [`../setup-video-script.md`](../setup-video-script.md) §2).

## Files
| File | Type | Frame |
|---|---|---|
| `01-title.html`  | animated (canvas) | Living substrate + wordmark + tagline. |
| `02-install.svg` | static | Install → `mnemos doctor` → **ready**. |
| `03-connect.svg` | static | Connect to client (`mnemos mcp install claude --write`). |
| `04-tools.svg`   | static | Nine tools; `handoff` and `capture` in use. |
| `05-hermes.svg`  | static | Hermes — Sidecar vs Provider. |
| `06-close.html`  | animated (canvas) | Fuller settled substrate + CTA. |

## How to use
- The **`.html`** frames are self-contained — double-click to open in any browser and watch the
  motion (tap the canvas to fire a thought). They are the **animation reference**: a video tool
  screen-captures them, or a designer rebuilds the canvas. The connectome logic + palette are inline.
- The **`.svg`** frames are static and scale crisply; render at 1280×720 for the video.
- Fonts: **Inter** (brand voice) + **JetBrains Mono** (technical). The frames fall back to system
  stacks; for pixel-final renders, load Inter + JetBrains Mono.

## The one rule
Everything is monochrome on near-black; **the brightest element on each frame is the thing that just
came alive** — a memory formed, a tool used, a connection made. No accent hue. Keep it that way.
