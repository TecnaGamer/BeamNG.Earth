// Standalone floating "Generation Addons" window in the CEF UI layer.
// Single-instance, in-place DOM updates (never rebuilds innerHTML on a timer, never resets drag position),
// with Minimize ("Min"), Close ("X"), Refresh World, and dual-mode Hardware/Addon Benchmark controls:
//   - Mode 1: Free Drive (30s)
//   - Mode 2: Point-to-Point Route Auto-Drive (selectable via CEF-native stepper + route buttons,
//             with Slow 80 km/h [50 mph, 1.0 km], Fast 200 km/h [124 mph, 2.5 km], and Ultra 300 km/h [186 mph, 3.75 km],
//             plus Wait Full Load vs. Immediate Start).

const ROOT_ID = "bng-earth-addons-floating-root"
const STYLE_ID = "bng-earth-addons-floating-style"

const EFFORT_PROFILES = [
  { id: "low", label: "Low" },
  { id: "balanced", label: "Balanced" },
  { id: "high", label: "High" },
  { id: "ultra", label: "Ultra" },
  { id: "auto_fps", label: "Auto-FPS" },
]

const DEFAULT_ROUTES = [
  { id: "ventoux_d974", shortName: "Mont Ventoux D974 (Climb)", duration: 45 },
  { id: "tavel_vineyards", shortName: "Tavel Vineyards -> Lirac", duration: 45 },
  { id: "lirac_tavel", shortName: "Lirac Streets -> Vineyards", duration: 45 },
  { id: "sault_lavender", shortName: "Sault Lavender Fields (D942)", duration: 45 },
]

const SPEED_TIERS = [
  { id: "slow", label: "Slow: 80 km/h", sub: "50 mph · 1.0 km" },
  { id: "fast", label: "Fast: 200 km/h", sub: "124 mph · 2.5 km" },
  { id: "ultra", label: "Ultra: 300 km/h", sub: "186 mph · 3.75 km" },
]

function getState() {
  if (!window.__bngEarthAddonsState) {
    window.__bngEarthAddonsState = {
      visible: false,
      minimized: false,
      addonProvence: true,
      sourceIgnRpg: true,
      sourceIgnOrtho: true,
      sourceProvenceBiomes: true,
      effortProfile: "balanced",
      benchMode: "free", // "free" | "route"
      benchRouteId: "ventoux_d974",
      benchSpeedId: "slow", // "slow" | "fast" | "ultra"
      benchLoadMode: "full", // "full" (Wait Full Load) | "cold" (Immediate Start)
      routes: DEFAULT_ROUTES,
      note: "",
      benchRunning: false,
      benchStage: "recording",
      benchStageText: "",
      benchElapsed: 0,
      benchDuration: 30,
      benchSummary: "",
      benchFolder: "",
      benchHasUrl: false,
      issueRepo: "Madderose/BeamNG.Earth",
      benchPollTimer: null,
      posX: 28,
      posY: 84,
      noteTimer: null,
    }
  }
  return window.__bngEarthAddonsState
}

// Always pass a callback function to bngApi.engineLua so BeamNG wraps expressions in `return (...)`
// instead of executing them as bare Lua statements.
function engineLua(code, cb) {
  if (window.bngApi && window.bngApi.engineLua) {
    window.bngApi.engineLua(code, typeof cb === "function" ? cb : () => {})
  }
}

function injectStyles() {
  let style = document.getElementById(STYLE_ID)
  if (!style) {
    style = document.createElement("style")
    style.id = STYLE_ID
    document.head.appendChild(style)
  }
  style.textContent = `
    #${ROOT_ID} {
      position: fixed;
      z-index: 99999;
      font-family: "Overpass", "Noto Sans", sans-serif;
      color: #f0f2f5;
      user-select: none;
      pointer-events: auto;
    }
    .bng-earth-addons-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: rgba(20, 24, 30, 0.92);
      border: 1px solid rgba(255, 102, 0, 0.65);
      border-radius: 6px;
      padding: 6px 12px;
      color: #fff;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.55);
    }
    .bng-earth-addons-pill:hover {
      background: rgba(34, 40, 50, 0.96);
      border-color: #ff6600;
    }
    .bng-earth-addons-card {
      width: 392px;
      background: rgba(20, 24, 30, 0.94);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 8px;
      box-shadow: 0 10px 28px rgba(0, 0, 0, 0.68);
      padding: 12px 14px 14px;
      backdrop-filter: blur(6px);
    }
    .bng-earth-addons-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding-bottom: 10px;
      margin-bottom: 8px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      cursor: move;
    }
    .bng-earth-addons-title {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 16px;
      font-weight: 700;
      font-style: italic;
      color: #ffffff;
    }
    .bng-earth-addons-slash {
      display: inline-block;
      width: 6px;
      height: 18px;
      background: #ff6600;
      transform: skewX(-16deg);
    }
    .bng-earth-addons-hdrbtns {
      display: flex;
      align-items: center;
      gap: 5px;
    }
    .bng-earth-addons-hdrbtn {
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.16);
      color: #ddd;
      border-radius: 4px;
      padding: 2px 8px;
      font-size: 12px;
      line-height: 1.3;
      cursor: pointer;
    }
    .bng-earth-addons-hdrbtn:hover {
      background: rgba(255, 102, 0, 0.25);
      border-color: #ff6600;
      color: #fff;
    }
    .bng-earth-addons-hdrbtn.close:hover {
      background: rgba(220, 50, 50, 0.8);
      border-color: #ff5555;
      color: #fff;
    }
    .bng-earth-addons-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: rgba(38, 44, 54, 0.85);
      border-radius: 5px;
      padding: 7px 10px;
      margin-bottom: 5px;
      font-size: 13px;
      cursor: pointer;
    }
    .bng-earth-addons-row.sub {
      padding-left: 20px;
      background: rgba(30, 35, 44, 0.8);
      font-size: 12.5px;
      color: #d6dce5;
    }
    .bng-earth-addons-row:hover {
      background: rgba(50, 58, 70, 0.92);
    }
    .bng-earth-switch {
      width: 34px;
      height: 18px;
      border-radius: 10px;
      background: rgba(120, 130, 145, 0.55);
      position: relative;
      flex-shrink: 0;
      transition: background 0.15s ease;
    }
    .bng-earth-switch.on {
      background: #ff6600;
    }
    .bng-earth-switch::after {
      content: "";
      position: absolute;
      top: 2px;
      left: 2px;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: #ffffff;
      transition: transform 0.15s ease;
    }
    .bng-earth-switch.on::after {
      transform: translateX(16px);
    }
    .bng-earth-effort-box {
      background: rgba(38, 44, 54, 0.85);
      border-radius: 5px;
      padding: 8px 10px;
      margin-bottom: 6px;
    }
    .bng-earth-effort-label {
      font-size: 11.5px;
      color: #b8c2d0;
      margin-bottom: 5px;
    }
    .bng-earth-effort-pills {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 4px;
    }
    .bng-earth-effort-btn {
      background: rgba(20, 24, 30, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.15);
      color: #cfd6e0;
      border-radius: 4px;
      padding: 4px 2px;
      font-size: 11px;
      text-align: center;
      cursor: pointer;
    }
    .bng-earth-effort-btn.active {
      background: #ff6600;
      border-color: #ff6600;
      color: #fff;
      font-weight: 700;
    }
    .bng-earth-bench-box {
      background: rgba(32, 38, 48, 0.88);
      border: 1px solid rgba(88, 166, 255, 0.25);
      border-radius: 5px;
      padding: 8px 10px;
      margin-bottom: 8px;
    }
    .bng-earth-bench-modes {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 5px;
      margin-bottom: 6px;
    }
    .bng-earth-mode-btn {
      background: rgba(20, 24, 30, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.16);
      color: #cfd6e0;
      border-radius: 4px;
      padding: 4px 6px;
      font-size: 11.5px;
      text-align: center;
      cursor: pointer;
    }
    .bng-earth-mode-btn.active {
      background: rgba(56, 139, 253, 0.35);
      border-color: #58a6ff;
      color: #fff;
      font-weight: 700;
    }
    .bng-earth-route-stepper {
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: rgba(16, 20, 26, 0.95);
      border: 1px solid rgba(88, 166, 255, 0.45);
      border-radius: 4px;
      padding: 3px 4px;
      margin-bottom: 5px;
    }
    .bng-earth-step-btn {
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.18);
      color: #fff;
      border-radius: 3px;
      padding: 2px 9px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
    }
    .bng-earth-step-btn:hover {
      background: rgba(56, 139, 253, 0.45);
      border-color: #79c0ff;
    }
    .bng-earth-route-cur {
      font-size: 12px;
      font-weight: 600;
      color: #e6f0ff;
      text-align: center;
      flex: 1;
      padding: 0 6px;
    }
    .bng-earth-route-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4px;
      margin-bottom: 6px;
    }
    .bng-earth-route-pill {
      background: rgba(20, 24, 30, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.14);
      color: #c6d0dc;
      border-radius: 4px;
      padding: 4px 5px;
      font-size: 10.5px;
      text-align: left;
      cursor: pointer;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .bng-earth-route-pill:hover {
      background: rgba(45, 55, 70, 0.92);
    }
    .bng-earth-route-pill.active {
      background: rgba(56, 139, 253, 0.32);
      border-color: #58a6ff;
      color: #ffffff;
      font-weight: 700;
    }
    .bng-earth-speed-pills {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 4px;
      margin-bottom: 6px;
    }
    .bng-earth-speed-btn {
      background: rgba(20, 24, 30, 0.88);
      border: 1px solid rgba(255, 255, 255, 0.15);
      color: #cfd6e0;
      border-radius: 4px;
      padding: 4px 3px;
      font-size: 11px;
      text-align: center;
      cursor: pointer;
      line-height: 1.25;
    }
    .bng-earth-speed-btn .sub {
      display: block;
      font-size: 9.5px;
      color: #8fa0b5;
      font-weight: 400;
    }
    .bng-earth-speed-btn.active {
      background: rgba(255, 102, 0, 0.32);
      border-color: #ff6600;
      color: #fff;
      font-weight: 700;
    }
    .bng-earth-speed-btn.active .sub {
      color: #ffd6b8;
    }
    .bng-earth-load-pills {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4px;
    }
    .bng-earth-load-btn {
      background: rgba(20, 24, 30, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.14);
      color: #c6d0dc;
      border-radius: 4px;
      padding: 4px 5px;
      font-size: 10.5px;
      text-align: center;
      cursor: pointer;
    }
    .bng-earth-load-btn.active {
      background: rgba(46, 160, 67, 0.32);
      border-color: #3fb950;
      color: #ffffff;
      font-weight: 700;
    }
    .bng-earth-actions-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
    }
    .bng-earth-refresh-btn {
      width: 100%;
      background: rgba(255, 102, 0, 0.18);
      border: 1px solid #ff6600;
      color: #ffffff;
      border-radius: 5px;
      padding: 7px 8px;
      font-size: 12.5px;
      font-weight: 600;
      cursor: pointer;
      text-align: center;
    }
    .bng-earth-refresh-btn:hover {
      background: #ff6600;
    }
    .bng-earth-refresh-btn.force-start {
      background: rgba(46, 160, 67, 0.32);
      border-color: #3fb950;
    }
    .bng-earth-refresh-btn.force-start:hover {
      background: #2ea043;
    }
    .bng-earth-bench-btn {
      width: 100%;
      background: rgba(56, 139, 253, 0.18);
      border: 1px solid rgba(88, 166, 255, 0.8);
      color: #e6f0ff;
      border-radius: 5px;
      padding: 7px 8px;
      font-size: 12.5px;
      font-weight: 600;
      cursor: pointer;
      text-align: center;
    }
    .bng-earth-bench-btn:hover {
      background: rgba(56, 139, 253, 0.45);
      border-color: #79c0ff;
    }
    .bng-earth-bench-btn.running {
      background: rgba(220, 120, 20, 0.35);
      border-color: #ffaa00;
      color: #fff;
    }
    .bng-earth-bench-result {
      margin-top: 8px;
      padding: 8px 10px;
      background: rgba(26, 32, 42, 0.92);
      border: 1px solid rgba(88, 166, 255, 0.45);
      border-radius: 5px;
      font-size: 11.5px;
      color: #d6e4f5;
    }
    .bng-earth-bench-repo {
      font-size: 10.5px;
      color: #8b9bb0;
      margin-top: 3px;
    }
    .bng-earth-bench-result-btns {
      display: flex;
      gap: 6px;
      margin-top: 6px;
    }
    .bng-earth-bench-subbtn {
      flex: 1;
      background: rgba(255, 255, 255, 0.1);
      border: 1px solid rgba(255, 255, 255, 0.22);
      color: #fff;
      border-radius: 4px;
      padding: 5px 6px;
      font-size: 11.5px;
      font-weight: 600;
      cursor: pointer;
      text-align: center;
    }
    .bng-earth-bench-subbtn:hover {
      background: rgba(255, 102, 0, 0.35);
      border-color: #ff6600;
    }
    .bng-earth-addons-note {
      font-size: 11.5px;
      color: #ff9944;
      text-align: center;
      margin-top: 6px;
      min-height: 15px;
    }
  `
}

function setNote(text, clearMs = 2500) {
  const st = getState()
  st.note = text
  updateDOM()
  if (st.noteTimer) clearTimeout(st.noteTimer)
  if (text && clearMs > 0) {
    st.noteTimer = setTimeout(() => {
      st.note = ""
      updateDOM()
    }, clearMs)
  }
}

export function syncFromLua() {
  engineLua("beamngEarth_settings and beamngEarth_settings.getAll() or nil", res => {
    if (!res) return
    const st = getState()
    st.addonProvence = res.addonProvence !== false
    st.sourceIgnRpg = res.sourceIgnRpg !== false
    st.sourceIgnOrtho = res.sourceIgnOrtho !== false
    st.sourceProvenceBiomes = res.sourceProvenceBiomes !== false
    st.effortProfile = res.effortProfile || "balanced"
    updateDOM()
  })
}

export function applyStatus(stRes) {
  const st = getState()
  if (!stRes) return
  const wasRunning = st.benchRunning
  st.benchRunning = !!stRes.benchRunning
  st.benchStage = stRes.stage || "recording"
  st.benchStageText = stRes.stageText || ""
  st.benchElapsed = stRes.elapsed || 0
  st.benchDuration = stRes.duration || 30
  if (stRes.lastSummary) st.benchSummary = stRes.lastSummary
  if (stRes.lastFolder) st.benchFolder = stRes.lastFolder
  if (stRes.issueRepo) st.issueRepo = stRes.issueRepo
  if (Array.isArray(stRes.routes) && stRes.routes.length) {
    st.routes = stRes.routes
  }
  st.benchHasUrl = !!stRes.lastUrl
  if (st.benchRunning) {
    if (st.benchStageText) st.note = st.benchStageText
    if (!st.benchPollTimer) {
      st.benchPollTimer = setInterval(pollBenchStatus, 500)
    }
  } else {
    if (st.benchPollTimer) {
      clearInterval(st.benchPollTimer)
      st.benchPollTimer = null
    }
    const staleNote = /^(Driving|Loading|Waiting|Preparing|World|Ready|Teleporting|Requesting)/i.test(st.note || "")
    if (wasRunning || staleNote) {
      setNote("Benchmark saved (report.json & issue.md ready)!", 5000)
    }
  }
  updateDOM()
}

function pollBenchStatus() {
  engineLua("beamngEarth_report and beamngEarth_report.status() or nil", applyStatus)
}

function startOrStopBenchmark() {
  const st = getState()
  if (st.benchRunning) {
    engineLua("beamngEarth_report and beamngEarth_report.uiStopBench() or nil", res => {
      if (st.benchPollTimer) {
        clearInterval(st.benchPollTimer)
        st.benchPollTimer = null
      }
      st.benchRunning = false
      st.benchStageText = ""
      pollBenchStatus()
      setNote(res && res.cancelled ? "Benchmark cancelled" : "Benchmark stopped & saved", 3000)
    })
    return
  }
  const modeArg = st.benchMode === "route"
    ? `"route", ${JSON.stringify(st.benchRouteId || "ventoux_d974")}, ${JSON.stringify(st.benchSpeedId || "slow")}, ${JSON.stringify(st.benchLoadMode || "full")}`
    : `"free", 30`
  engineLua(`beamngEarth_report and beamngEarth_report.uiStartBench(${modeArg}) or nil`, res => {
    if (!res || !res.ok) {
      setNote((res && res.error) || "Load BeamNG.Earth level first", 3500)
      return
    }
    st.benchRunning = true
    st.benchStage = st.benchMode === "route" ? "staging" : "recording"
    st.benchElapsed = 0
    st.benchDuration = res.duration || 45
    setNote(
      st.benchMode === "route"
        ? `Preparing route (${st.benchLoadMode === "cold" ? "immediate start" : "waiting full load"}): ${res.route || st.benchRouteId}...`
        : "Recording 30s free-drive benchmark (keep driving)...",
      0
    )
    if (st.benchPollTimer) clearInterval(st.benchPollTimer)
    st.benchPollTimer = setInterval(pollBenchStatus, 500)
    updateDOM()
  })
}

function openBenchFolder() {
  engineLua("beamngEarth_report and beamngEarth_report.openLastFolder() or nil", res => {
    if (res && res.ok) {
      setNote("Opened report folder in file explorer", 3000)
    } else {
      setNote((res && res.error) || "Could not open folder", 3000)
    }
  })
}

function copyBenchReport() {
  engineLua("beamngEarth_report and beamngEarth_report.copyLastReport() or nil", res => {
    if (res && res.ok) {
      setNote("Copied issue.md report to clipboard", 3000)
    } else {
      setNote((res && res.error) || "Could not copy report", 3000)
    }
  })
}

function openBenchIssue() {
  engineLua("beamngEarth_report and beamngEarth_report.openLastIssueUrl() or nil", res => {
    if (res && res.ok) {
      setNote(`Opened GitHub (${res.repo || "ticket"}) in browser`, 4000)
    } else {
      setNote((res && res.error) || "Could not open GitHub ticket", 3000)
    }
  })
}

function toggleBool(key) {
  const st = getState()
  st[key] = !st[key]
  setNote("Applying & refreshing world...", 0)
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiSet(${JSON.stringify(key)}, ${st[key] ? "true" : "false"}) or nil`, () => {
    setNote("Updated", 2000)
  })
}

function setEffort(id) {
  const st = getState()
  st.effortProfile = id
  setNote(`Effort profile: ${id}...`, 0)
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiSet("effortProfile", ${JSON.stringify(id)}) or nil`, () => {
    setNote(`Effort: ${id}`, 2000)
  })
}

function setBenchMode(mode) {
  const st = getState()
  st.benchMode = mode
  updateDOM()
}

function setBenchRoute(routeId) {
  const st = getState()
  st.benchRouteId = routeId
  updateDOM()
}

function stepBenchRoute(delta) {
  const st = getState()
  const list = Array.isArray(st.routes) && st.routes.length ? st.routes : DEFAULT_ROUTES
  let idx = list.findIndex(r => r.id === st.benchRouteId)
  if (idx < 0) idx = 0
  idx = (idx + delta + list.length) % list.length
  st.benchRouteId = list[idx].id
  updateDOM()
}

function setBenchSpeed(speedId) {
  const st = getState()
  st.benchSpeedId = speedId
  updateDOM()
}

function setBenchLoadMode(loadMode) {
  const st = getState()
  st.benchLoadMode = loadMode
  if (st.benchRunning && st.benchStage === "staging") {
    engineLua(`beamngEarth_report and beamngEarth_report.uiSetLoadMode(${JSON.stringify(loadMode)}) or nil`, () => {
      pollBenchStatus()
    })
  }
  updateDOM()
}

function triggerRefreshOrForceStart() {
  const st = getState()
  if (st.benchRunning && st.benchStage === "staging") {
    engineLua("beamngEarth_report and beamngEarth_report.uiForceStartNow() or nil", () => {
      pollBenchStatus()
    })
    return
  }
  setNote("Refreshing nearby vegetation & buildings...", 0)
  engineLua("beamngEarth_settings and beamngEarth_settings.uiRefreshWorld() or nil", () => {
    setNote("World refresh triggered", 2500)
  })
}

function attachDrag(headerEl, rootEl) {
  let dragging = false
  let startX = 0, startY = 0, origX = 0, origY = 0
  headerEl.addEventListener("mousedown", e => {
    if (e.target.tagName === "BUTTON") return
    const st = getState()
    dragging = true
    startX = e.clientX
    startY = e.clientY
    origX = st.posX
    origY = st.posY
    const onMove = ev => {
      if (!dragging) return
      st.posX = Math.max(8, Math.min(window.innerWidth - 400, origX + (ev.clientX - startX)))
      st.posY = Math.max(8, Math.min(window.innerHeight - 80, origY + (ev.clientY - startY)))
      rootEl.style.left = `${st.posX}px`
      rootEl.style.top = `${st.posY}px`
    }
    const onUp = () => {
      dragging = false
      window.removeEventListener("mousemove", onMove)
      window.removeEventListener("mouseup", onUp)
    }
    window.addEventListener("mousemove", onMove)
    window.addEventListener("mouseup", onUp)
  })
}

function buildDOMOnce() {
  injectStyles()
  let root = document.getElementById(ROOT_ID)
  if (root) root.remove()
  const st = getState()
  root = document.createElement("div")
  root.id = ROOT_ID
  root.style.left = `${st.posX}px`
  root.style.top = `${st.posY}px`

  const effortBtns = EFFORT_PROFILES.map(p => `
    <button class="bng-earth-effort-btn" data-effort="${p.id}">${p.label}</button>
  `).join("")

  const routePills = (st.routes || DEFAULT_ROUTES).map((r, idx) => `
    <button class="bng-earth-route-pill" data-route="${r.id}">${idx + 1}. ${r.shortName || r.name}</button>
  `).join("")

  const speedBtns = SPEED_TIERS.map(s => `
    <button class="bng-earth-speed-btn" data-speed="${s.id}">
      <span>${s.label}</span>
      <span class="sub">${s.sub}</span>
    </button>
  `).join("")

  root.innerHTML = `
    <div class="bng-earth-addons-pill" id="bng-earth-addons-pill-view" style="display:none;">
      <span class="bng-earth-addons-slash"></span>
      <span id="bng-earth-addons-pill-text">Generation Addons (ON)</span>
      <button class="bng-earth-addons-hdrbtn close" id="bng-earth-addons-pill-close" title="Close">X</button>
    </div>
    <div class="bng-earth-addons-card" id="bng-earth-addons-card-view">
      <div class="bng-earth-addons-header" id="bng-earth-addons-drag">
        <div class="bng-earth-addons-title">
          <span class="bng-earth-addons-slash"></span>
          <span>Generation Addons</span>
        </div>
        <div class="bng-earth-addons-hdrbtns">
          <button class="bng-earth-addons-hdrbtn" id="bng-earth-addons-min" title="Minimize to pill">Min</button>
          <button class="bng-earth-addons-hdrbtn close" id="bng-earth-addons-close" title="Close window">X</button>
        </div>
      </div>
      <div class="bng-earth-addons-row" data-key="addonProvence">
        <span>France & Provence Ground Truth Pack</span>
        <div class="bng-earth-switch" data-switch="addonProvence"></div>
      </div>
      <div id="bng-earth-addons-subrows">
        <div class="bng-earth-addons-row sub" data-key="sourceIgnRpg">
          <span>- IGN BD TOPO & RPG Parcels</span>
          <div class="bng-earth-switch" data-switch="sourceIgnRpg"></div>
        </div>
        <div class="bng-earth-addons-row sub" data-key="sourceIgnOrtho">
          <span>- IGN 20 cm Orthophoto Roof Tinting</span>
          <div class="bng-earth-switch" data-switch="sourceIgnOrtho"></div>
        </div>
        <div class="bng-earth-addons-row sub" data-key="sourceProvenceBiomes">
          <span>- Provence Crops & Biomes</span>
          <div class="bng-earth-switch" data-switch="sourceProvenceBiomes"></div>
        </div>
      </div>
      <div class="bng-earth-effort-box">
        <div class="bng-earth-effort-label">Ground-Truth Effort Profile (Layer B)</div>
        <div class="bng-earth-effort-pills">${effortBtns}</div>
      </div>
      <div class="bng-earth-bench-box">
        <div class="bng-earth-effort-label">Performance Benchmark Mode</div>
        <div class="bng-earth-bench-modes">
          <button class="bng-earth-mode-btn" data-mode="free">Free Drive (30s)</button>
          <button class="bng-earth-mode-btn" data-mode="route">Route A -> B (Auto)</button>
        </div>
        <div id="bng-earth-route-row" style="display:none;">
          <div class="bng-earth-effort-label">Select Route (click &lt; / &gt; or route button)</div>
          <div class="bng-earth-route-stepper">
            <button class="bng-earth-step-btn" id="bng-earth-route-prev" title="Previous route">&lt;</button>
            <div class="bng-earth-route-cur" id="bng-earth-route-cur">Mont Ventoux D974 (1/4)</div>
            <button class="bng-earth-step-btn" id="bng-earth-route-next" title="Next route">&gt;</button>
          </div>
          <div class="bng-earth-route-grid" id="bng-earth-route-grid">${routePills}</div>
          <div class="bng-earth-effort-label">Speed &amp; Adapted Route Distance (~45s drive)</div>
          <div class="bng-earth-speed-pills">${speedBtns}</div>
          <div class="bng-earth-effort-label">Start Trigger (Cold Spawn Readiness)</div>
          <div class="bng-earth-load-pills">
            <button class="bng-earth-load-btn" data-load="full">Wait Full Load (100%)</button>
            <button class="bng-earth-load-btn" data-load="cold">Immediate (Stream on Move)</button>
          </div>
        </div>
      </div>
      <div class="bng-earth-actions-grid">
        <button class="bng-earth-refresh-btn" id="bng-earth-addons-refresh">Refresh World</button>
        <button class="bng-earth-bench-btn" id="bng-earth-addons-bench">Benchmark (30s)</button>
      </div>
      <div class="bng-earth-bench-result" id="bng-earth-bench-result" style="display:none;">
        <div id="bng-earth-bench-summary"></div>
        <div class="bng-earth-bench-repo" id="bng-earth-bench-repo"></div>
        <div class="bng-earth-bench-result-btns">
          <button class="bng-earth-bench-subbtn" id="bng-earth-bench-folder">Open Folder</button>
          <button class="bng-earth-bench-subbtn" id="bng-earth-bench-copy">Copy Report</button>
          <button class="bng-earth-bench-subbtn" id="bng-earth-bench-issue">Post GitHub Ticket</button>
        </div>
      </div>
      <div class="bng-earth-addons-note" id="bng-earth-addons-note"></div>
    </div>
  `
  document.body.appendChild(root)

  attachDrag(root.querySelector("#bng-earth-addons-drag"), root)

  root.querySelector("#bng-earth-addons-min").addEventListener("click", e => {
    e.stopPropagation()
    st.minimized = true
    updateDOM()
  })
  root.querySelector("#bng-earth-addons-close").addEventListener("click", e => {
    e.stopPropagation()
    st.visible = false
    updateDOM()
  })
  root.querySelector("#bng-earth-addons-pill-view").addEventListener("click", e => {
    if (e.target.id === "bng-earth-addons-pill-close") return
    st.minimized = false
    syncFromLua()
    updateDOM()
  })
  root.querySelector("#bng-earth-addons-pill-close").addEventListener("click", e => {
    e.stopPropagation()
    st.visible = false
    updateDOM()
  })

  root.querySelectorAll(".bng-earth-addons-row").forEach(row => {
    row.addEventListener("click", () => toggleBool(row.getAttribute("data-key")))
  })
  root.querySelectorAll(".bng-earth-effort-btn").forEach(btn => {
    btn.addEventListener("click", () => setEffort(btn.getAttribute("data-effort")))
  })
  root.querySelectorAll(".bng-earth-mode-btn").forEach(btn => {
    btn.addEventListener("click", () => setBenchMode(btn.getAttribute("data-mode")))
  })
  root.querySelector("#bng-earth-route-prev").addEventListener("click", () => stepBenchRoute(-1))
  root.querySelector("#bng-earth-route-next").addEventListener("click", () => stepBenchRoute(1))
  root.querySelectorAll(".bng-earth-route-pill").forEach(btn => {
    btn.addEventListener("click", () => setBenchRoute(btn.getAttribute("data-route")))
  })
  root.querySelectorAll(".bng-earth-speed-btn").forEach(btn => {
    btn.addEventListener("click", () => setBenchSpeed(btn.getAttribute("data-speed")))
  })
  root.querySelectorAll(".bng-earth-load-btn").forEach(btn => {
    btn.addEventListener("click", () => setBenchLoadMode(btn.getAttribute("data-load")))
  })

  root.querySelector("#bng-earth-addons-refresh").addEventListener("click", triggerRefreshOrForceStart)
  root.querySelector("#bng-earth-addons-bench").addEventListener("click", startOrStopBenchmark)
  root.querySelector("#bng-earth-bench-folder").addEventListener("click", openBenchFolder)
  root.querySelector("#bng-earth-bench-copy").addEventListener("click", copyBenchReport)
  root.querySelector("#bng-earth-bench-issue").addEventListener("click", openBenchIssue)

  updateDOM()
}

export function updateDOM() {
  const root = document.getElementById(ROOT_ID)
  if (!root) return
  const st = getState()
  if (!st.visible) {
    root.style.display = "none"
    return
  }
  root.style.display = "block"
  const pill = root.querySelector("#bng-earth-addons-pill-view")
  const card = root.querySelector("#bng-earth-addons-card-view")
  if (st.minimized) {
    pill.style.display = "inline-flex"
    card.style.display = "none"
    const pillText = root.querySelector("#bng-earth-addons-pill-text")
    if (pillText) pillText.textContent = `Generation Addons (${st.addonProvence ? "ON" : "OFF"})`
    return
  }
  pill.style.display = "none"
  card.style.display = "block"

  ;["addonProvence", "sourceIgnRpg", "sourceIgnOrtho", "sourceProvenceBiomes"].forEach(k => {
    const sw = root.querySelector(`[data-switch="${k}"]`)
    if (sw) sw.classList.toggle("on", !!st[k])
  })
  const sub = root.querySelector("#bng-earth-addons-subrows")
  if (sub) sub.style.display = st.addonProvence ? "block" : "none"

  root.querySelectorAll(".bng-earth-effort-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-effort") === st.effortProfile)
  })
  root.querySelectorAll(".bng-earth-mode-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-mode") === st.benchMode)
  })
  const routeRow = root.querySelector("#bng-earth-route-row")
  if (routeRow) {
    routeRow.style.display = st.benchMode === "route" ? "block" : "none"
  }

  const list = Array.isArray(st.routes) && st.routes.length ? st.routes : DEFAULT_ROUTES
  let curIdx = list.findIndex(r => r.id === st.benchRouteId)
  if (curIdx < 0) curIdx = 0
  const curRoute = list[curIdx] || DEFAULT_ROUTES[0]
  const curEl = root.querySelector("#bng-earth-route-cur")
  if (curEl) {
    curEl.textContent = `${curRoute.shortName || curRoute.name} (${curIdx + 1}/${list.length})`
  }
  root.querySelectorAll(".bng-earth-route-pill").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-route") === st.benchRouteId)
  })
  root.querySelectorAll(".bng-earth-speed-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-speed") === st.benchSpeedId)
  })
  root.querySelectorAll(".bng-earth-load-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-load") === st.benchLoadMode)
  })

  const refreshBtn = root.querySelector("#bng-earth-addons-refresh")
  if (refreshBtn) {
    const isStaging = !!(st.benchRunning && st.benchStage === "staging")
    refreshBtn.classList.toggle("force-start", isStaging)
    refreshBtn.textContent = isStaging ? "Start Drive Now" : "Refresh World"
  }

  const benchBtn = root.querySelector("#bng-earth-addons-bench")
  if (benchBtn) {
    benchBtn.classList.toggle("running", !!st.benchRunning)
    if (st.benchRunning) {
      if (st.benchStage === "staging") {
        benchBtn.textContent = `Loading (${st.benchElapsed}s) · Stop`
      } else {
        benchBtn.textContent = `Stop (${st.benchElapsed}/${st.benchDuration}s)`
      }
    } else {
      const spLabel = st.benchSpeedId === "ultra" ? "300 km/h" : (st.benchSpeedId === "fast" ? "200 km/h" : "80 km/h")
      benchBtn.textContent = st.benchMode === "route" ? `Run Route (${spLabel})` : "Benchmark (30s)"
    }
  }
  const resBox = root.querySelector("#bng-earth-bench-result")
  const resSum = root.querySelector("#bng-earth-bench-summary")
  const resRepo = root.querySelector("#bng-earth-bench-repo")
  if (resBox && resSum) {
    if (!st.benchRunning && st.benchSummary) {
      resBox.style.display = "block"
      resSum.textContent = `Last Benchmark: ${st.benchSummary}`
      if (resRepo) {
        resRepo.textContent = `Ticket target: github.com/${st.issueRepo || "Madderose/BeamNG.Earth"}`
      }
    } else {
      resBox.style.display = "none"
    }
  }
  const noteEl = root.querySelector("#bng-earth-addons-note")
  if (noteEl) {
    noteEl.textContent = st.note || ""
    noteEl.style.display = st.note ? "block" : "none"
  }
}

export function open() {
  const st = getState()
  st.visible = true
  st.minimized = false
  if (!document.getElementById(ROOT_ID)) buildDOMOnce()
  syncFromLua()
  pollBenchStatus()
  updateDOM()
}

export function close() {
  const st = getState()
  st.visible = false
  updateDOM()
}

export function toggle() {
  const st = getState()
  st.visible = !st.visible
  if (st.visible) {
    st.minimized = false
    syncFromLua()
    pollBenchStatus()
  }
  updateDOM()
}

export function start() {
  stop()
  window.bngEarthAddonsWindow = { open, close, toggle, updateDOM, syncFromLua, applyStatus, stop }
  buildDOMOnce()
  syncFromLua()
  pollBenchStatus()
}

export function stop() {
  const st = getState()
  if (st.benchPollTimer) {
    clearInterval(st.benchPollTimer)
    st.benchPollTimer = null
  }
  const root = document.getElementById(ROOT_ID)
  if (root) root.remove()
}
