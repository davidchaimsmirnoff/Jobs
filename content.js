// ================== Config ==================
const INACTIVITY_MS = 3000; // 3s of no growth => "stopped"
const POLL_MS = 250;        // frequent polling to catch shadow-DOM updates
const GAIN = 0.12;          // louder default so you can hear it

// ============ Tiny tone synth (no audio files) ============
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
function tone(freq = 880, ms = 120, gainVal = GAIN, type = "sine") {
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = type;
  osc.frequency.value = freq;
  gain.gain.value = gainVal;
  osc.connect(gain).connect(audioCtx.destination);
  osc.start();
  setTimeout(() => osc.stop(), ms);
}

const playWaiting = () => { tone(660, 90); setTimeout(() => tone(660, 90), 110); };
const playStart   = () => tone(1046.5, 120);
const playStop    = () => { tone(440, 120); setTimeout(() => tone(329.6, 120), 130); };

// Resume audio on first user gesture (Chrome auto-play policy)
function resumeAudio() { if (audioCtx.state === "suspended") audioCtx.resume(); }
["click","keydown","pointerdown"].forEach(ev =>
  window.addEventListener(ev, resumeAudio, { once: true, capture: true })
);

// ============ Status chip (so you know it’s running) ============
let chip;
function ensureChip() {
  if (chip) return;
  chip = document.createElement("div");
  chip.textContent = "⏺ idle";
  Object.assign(chip.style, {
    position: "fixed", zIndex: 2147483647, right: "8px", bottom: "8px",
    font: "12px system-ui, sans-serif", background: "rgba(0,0,0,0.65)",
    color: "#fff", padding: "6px 8px", borderRadius: "999px",
    boxShadow: "0 2px 10px rgba(0,0,0,0.35)", userSelect: "none"
  });
  chip.title = "Alt+T to pick an element to track";
  document.documentElement.appendChild(chip);
}
function setChip(text, color="#fff") {
  ensureChip();
  chip.textContent = text;
  chip.style.color = color;
}

// ============ Shadow-DOM aware querying ============
function* deepWalk(node, includeSelf = true) {
  if (includeSelf) yield node;
  const kids = node instanceof ShadowRoot ? node.children : node.childNodes;
  for (const child of kids) {
    if (child.nodeType === 1) { // ELEMENT_NODE
      // traverse shadow root if open
      if (child.shadowRoot) yield* deepWalk(child.shadowRoot, true);
      yield* deepWalk(child, true);
    }
  }
}
function deepQueryAll(selector, root = document) {
  const results = [];
  for (const n of deepWalk(root, true)) {
    if (n.nodeType === 1) {
      try {
        if (n.matches && n.matches(selector)) results.push(n);
        // Also search descendants the normal way for performance
        if (n.querySelectorAll) {
          n.querySelectorAll(selector).forEach(el => results.push(el));
        }
      } catch {}
    }
  }
  return results;
}

// Site profiles
const SITE_PROFILES = [
  {
    name: "chatgpt",
    hostRe: /(^|\.)chatgpt\.com$|(^|\.)chat\.openai\.com$/i,
    assistantSelectors: [
      '[data-message-author-role="assistant"] .markdown',
      '[data-message-author-role="assistant"]',
      '.markdown, .prose'
    ],
    userSelectors: [
      '[data-message-author-role="user"]'
    ],
  },
  {
    name: "replit",
    hostRe: /(^|\.)replit\.(com|dev|app)$/i,
    assistantSelectors: [
      // Broad guesses for Ghostwriter/AI panes:
      '[data-testid*="ai"]',
      '[data-testid*="message-bubble"][data-kind*="ai"]',
      '.ghostwriter-output',
      '.ai-output',
      '[role="log"]',
      '[aria-live="polite"]',
      '.markdown, .prose'
    ],
    userSelectors: [
      '[data-testid*="message-bubble"][data-kind*="user"]'
    ],
  }
];

function pickSiteProfile() {
  const host = location.hostname;
  for (const p of SITE_PROFILES) if (p.hostRe.test(host)) return p;
  return {
    name: "generic",
    assistantSelectors: ['[aria-live="polite"]', '[role="log"]', '.markdown', '.prose', 'article'],
    userSelectors: ['[data-role*="user" i]', '[aria-label*="user" i]']
  };
}
const PROFILE = pickSiteProfile();

// ============ State machine ============
let phase = "idle"; // "idle" | "waiting" | "writing"
let lastLen = 0;
let stableSince = 0;
let trackedNode = null;
let lastPickTs = 0;

function setPhase(newPhase) {
  if (phase === newPhase) return;
  phase = newPhase;
  if (phase === "writing") setChip("✍️ writing", "#7cf");
  else if (phase === "waiting") setChip("⏳ waiting", "#ffd54d");
  else setChip("⏺ idle", "#fff");
}

function textLenOf(el) {
  if (!el) return 0;
  const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  return text.length;
}

function pickAssistantNode() {
  for (const sel of (PROFILE.assistantSelectors || [])) {
    const nodes = deepQueryAll(sel);
    if (nodes.length) return nodes[nodes.length - 1];
  }
  // Generic fallback: largest text container in common containers (deep)
  const cands = deepQueryAll('article, [role="log"], [aria-live], .markdown, .prose, div');
  let best = null, bestLen = 0;
  for (const n of cands) {
    const len = textLenOf(n);
    if (len > bestLen) { bestLen = len; best = n; }
  }
  return best;
}

function getLatestAssistantTextLen() {
  const now = Date.now();
  if (!trackedNode || (now - lastPickTs > 1500)) {
    const newNode = pickAssistantNode();
    if (newNode && newNode !== trackedNode) {
      trackedNode = newNode;
      lastPickTs = now;
    }
  }
  return trackedNode ? textLenOf(trackedNode) : 0;
}

function tick() {
  const now = Date.now();
  const len = getLatestAssistantTextLen();

  if (len > lastLen + 1) {
    if (phase !== "writing") {
      setPhase("writing");
      playStart();
    }
    stableSince = now;
  } else {
    if (phase === "writing" && (now - stableSince > INACTIVITY_MS)) {
      setPhase("idle");
      playStop();
    }
  }
  lastLen = len;
}

// Always poll (shadow DOM updates may not bubble mutations)
setInterval(tick, POLL_MS);

// MutationObserver still helps when not in shadow DOM
const observer = new MutationObserver(() => tick());
observer.observe(document.documentElement, {
  subtree: true,
  childList: true,
  characterData: true
});

// --------- detect when the user sends a question ---------
function onKeydown(e) {
  // Alt+T: manual picker
  if (e.altKey && e.key.toLowerCase() === "t") {
    togglePicker();
    return;
  }
  const isEnter = e.key === "Enter";
  const isMod = e.shiftKey || e.altKey || e.ctrlKey || e.metaKey;
  if (!isEnter || isMod) return;

  const t = e.target;
  const looksLikeInput = t && (
    t.tagName === "TEXTAREA" ||
    t.tagName === "INPUT" ||
    t.getAttribute("role") === "textbox" ||
    t.isContentEditable
  );
  if (!looksLikeInput) return;

  const val = (t.value || t.textContent || "").trim();
  if (!val) return;

  setPhase("waiting");
  playWaiting();
}
document.addEventListener("keydown", onKeydown, true);

document.addEventListener("click", (e) => {
  const btn = e.target.closest('button, [role="button"]');
  if (!btn) return;
  const label = (btn.ariaLabel || btn.getAttribute("aria-label") || btn.textContent || "").toLowerCase();
  if (/(send|submit|ask|run)/i.test(label) || /paper|plane|enter|svg/i.test(btn.innerHTML)) {
    setPhase("waiting");
    playWaiting();
  }
}, true);

// ============ Manual picker (Alt+T) ============
let picking = false;
let hoverOutline = null;

function togglePicker() {
  picking = !picking;
  if (picking) startPicker();
  else stopPicker();
}
function startPicker() {
  setChip("🔎 click output to track (Alt+T to cancel)", "#9f9");
  document.addEventListener("mousemove", onHover, true);
  document.addEventListener("click", onPick, true);
}
function stopPicker() {
  setChip("⏺ idle", "#fff");
  document.removeEventListener("mousemove", onHover, true);
  document.removeEventListener("click", onPick, true);
  if (hoverOutline && hoverOutline.remove) hoverOutline.remove();
  hoverOutline = null;
}
function onHover(e) { highlight(e.target); }
function onPick(e) {
  e.preventDefault(); e.stopPropagation();
  trackedNode = e.target;
  lastPickTs = Date.now();
  picking = false;
  stopPicker();
  setChip("✅ tracking selection", "#9f9");
}
function highlight(el) {
  if (!hoverOutline) {
    hoverOutline = document.createElement("div");
    Object.assign(hoverOutline.style, {
      position: "fixed", pointerEvents: "none", zIndex: 2147483647,
      border: "2px solid #4af", borderRadius: "4px", boxShadow: "0 0 8px #4af7",
      background: "transparent"
    });
    document.documentElement.appendChild(hoverOutline);
  }
  const r = el.getBoundingClientRect();
  Object.assign(hoverOutline.style, {
    left: `${r.left + window.scrollX}px`,
    top: `${r.top + window.scrollY}px`,
    width: `${r.width}px`,
    height: `${r.height}px`,
    display: "block"
  });
}

// Init chip
ensureChip();
setChip("⏺ idle");
