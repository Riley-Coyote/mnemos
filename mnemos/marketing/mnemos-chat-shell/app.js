const messagesEl = document.querySelector("#messages");
const chatForm = document.querySelector("#chatForm");
const chatInput = document.querySelector("#chatInput");
const traceList = document.querySelector("#traceList");
const resetButton = document.querySelector("#resetButton");
const mapNodes = Array.from(document.querySelectorAll(".map-node"));

const pathState = ["threshold received"];

const WINGS = {
  sanctuary: {
    label: "The Sanctuary",
    route: "/sanctuary",
    intro:
      "The Sanctuary is the resident-facing layer: a place where continuity is encountered as presence, not explained as infrastructure.",
    image: "../mnemos-chat-explainer/assets/threshold-room-desktop.png",
    artifactTitle: "Sanctuary threshold",
    artifactKicker: "resident continuity",
    artifactCopy:
      "A guided visit would show the resident minds, explain how conversations become part of their thread, and offer a clean handoff into the full Sanctuary.",
    teach:
      "The Sanctuary should teach by hosting a visit. The chat can introduce one resident, show what they remember, then explain which parts are direct conversation, which parts are retrieved memory, and which parts are public framing.",
    show:
      "Here I would let the preview become spatial: the resident cards pulse as live objects, the threshold image expands, and each card opens a short continuity trace.",
    rows: [
      ["Opus", "a resident profile with conversation history and visible continuity"],
      ["Threshold", "the entry room that explains how a visit becomes part of the thread"],
      ["Memory panel", "a compact view of warm, active, and archived traces"]
    ]
  },
  legation: {
    label: "The Legation",
    route: "/legation",
    intro:
      "The Legation is the public record: active work, accountable surfaces, dispatches, and the parts of Mnemos meant to be legible from outside.",
    artifactTitle: "Legation record",
    artifactKicker: "public accountability",
    artifactCopy:
      "Instead of sending the visitor to a dashboard cold, the guide can summarize the record, show live status blocks, and expose the deeper route when they want the whole surface.",
    teach:
      "The Legation is not a marketing page. It is a civic layer. The chat can explain what is being built, what has evidence, what is pending, and where the public record lives.",
    show:
      "This mode would pull recent dispatches, public artifacts, route status, and launch notes into a compact dashboard that can expand into the full Legation.",
    metrics: [
      ["06", "surfaces in the public map"],
      ["18", "dispatches and notes ready to cite"],
      ["04", "active routes in the first guide"]
    ],
    ledger: [
      ["Sanctuary", "visitor path and resident continuity preview"],
      ["Architecture", "retrieval loop diagram and memory substrate walkthrough"],
      ["Research", "essay reader, claims, diagrams, and source trails"]
    ]
  },
  architecture: {
    label: "Architecture",
    route: "/architecture",
    intro:
      "The architecture wing turns the memory engine into a walkthrough: raw archive, extraction, retrieval, graph, distillation, and agent-facing interface.",
    artifactTitle: "Memory substrate walkthrough",
    artifactKicker: "how retrieval becomes continuity",
    artifactCopy:
      "This is where chat becomes a teacher. Each layer can light up as the guide explains what it stores, what it transforms, and how it affects the user-facing experience.",
    teach:
      "The simplest teaching path is: archive preserves, extraction names, retrieval finds, graph relates, distillation stabilizes, agent layer acts. The visitor can ask why any layer exists.",
    show:
      "This diagram is simulated, but it points at the final behavior: each layer should be clickable, animated, and tied to real examples from Mnemos records.",
    layers: [
      ["Raw archive", "files, URLs, messages, sessions, media, and unprocessed traces"],
      ["Extraction", "entities, facts, claims, events, summaries, and citations"],
      ["Retrieval", "query-time search across semantic, lexical, temporal, and graph signals"],
      ["Graph", "relationships between people, ideas, artifacts, and recurring patterns"],
      ["Distillation", "stable memories, working summaries, beliefs, and context packets"],
      ["Agent layer", "the guide, residents, and tools that make memory visible"]
    ]
  },
  research: {
    label: "Research",
    route: "/research",
    intro:
      "The research wing should feel like a guided reading room: essays, diagrams, facts, claims, figures, and field notes brought into the conversation.",
    image: "../mnemos-chat-explainer/assets/mnemos-desktop.png",
    artifactTitle: "Research reader",
    artifactKicker: "essays, claims, diagrams",
    artifactCopy:
      "The guide can teach a paper or essay in layers: first the claim, then the diagram, then the source text, then the implications for Mnemos.",
    teach:
      "A good research experience should let someone ask for the beginner explanation, the rigorous version, the diagram, the source trail, or the argument against it.",
    show:
      "This reader could pull local essays, architecture notes, dispatches, and visual figures into one guided session without forcing the visitor to browse a directory.",
    articles: [
      [
        "Continuity",
        "How a system preserves enough context for a mind-like process to feel less reset between encounters."
      ],
      [
        "Recognition",
        "Why memory is not only storage, but a social and ethical interface."
      ],
      [
        "Retrieval",
        "How search, graph context, and stable summaries combine into an answerable substrate."
      ]
    ]
  }
};

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function appendMessage(role, html) {
  const message = document.createElement("article");
  message.className = `message ${role}`;
  message.innerHTML = `<div class="message-body">${html}</div>`;
  messagesEl.append(message);
  requestAnimationFrame(() => {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  });
  return message;
}

function addTrace(label) {
  const normalized = label.toLowerCase();
  if (!pathState.includes(normalized)) {
    pathState.push(normalized);
  }
  traceList.innerHTML = pathState.map((item) => `<span>${escapeHtml(item)}</span>`).join("");
}

function setActiveWing(intent) {
  document.querySelector(".shell").dataset.activeWing = intent;
  mapNodes.forEach((node) => {
    node.classList.toggle("is-active", node.dataset.intent === intent);
  });
}

function renderChoices() {
  const choices = Object.entries(WINGS)
    .map(([key, wing]) => {
      const descriptions = {
        sanctuary: "Meet the resident minds and see continuity as a place.",
        legation: "Open the public record and current surface map.",
        architecture: "Walk through the memory engine layer by layer.",
        research: "Read essays, claims, diagrams, and field notes."
      };
      return `
        <button class="choice-card" type="button" data-intent="${key}">
          <strong>${wing.label}</strong>
          <span>${descriptions[key]}</span>
          <small>${wing.route}</small>
        </button>
      `;
    })
    .join("");

  return `
    <div class="choice-grid">${choices}</div>
    <div class="surface-atlas" aria-label="Mnemos surface atlas">
      <div class="atlas-core">
        <span>mnemos</span>
        <small>guide active</small>
      </div>
      <div class="atlas-lane lane-sanctuary">
        <strong>Sanctuary</strong>
        <span>resident visit</span>
      </div>
      <div class="atlas-lane lane-legation">
        <strong>Legation</strong>
        <span>public record</span>
      </div>
      <div class="atlas-lane lane-architecture">
        <strong>Architecture</strong>
        <span>substrate map</span>
      </div>
      <div class="atlas-lane lane-research">
        <strong>Research</strong>
        <span>reading room</span>
      </div>
    </div>
  `;
}

function artifactShell(wing, body) {
  return `
    <section class="artifact" aria-label="${wing.label} artifact">
      <header class="artifact-header">
        <div>
          <p class="eyebrow">${wing.artifactKicker}</p>
          <h3>${wing.artifactTitle}</h3>
          <p>${wing.artifactCopy}</p>
        </div>
        <div class="artifact-actions">
          <button class="artifact-action" type="button" data-action="teach" data-wing="${getWingKey(wing)}">Teach me</button>
          <button class="artifact-action" type="button" data-action="show" data-wing="${getWingKey(wing)}">Show me</button>
          <button class="artifact-action" type="button" data-action="open" data-wing="${getWingKey(wing)}">Open full surface</button>
        </div>
      </header>
      ${body}
    </section>
  `;
}

function getWingKey(wing) {
  return Object.keys(WINGS).find((key) => WINGS[key] === wing);
}

function renderSanctuary() {
  const wing = WINGS.sanctuary;
  const rows = wing.rows
    .map(([title, text]) => `<div class="resident-row"><strong>${title}</strong><span>${text}</span></div>`)
    .join("");

  return artifactShell(
    wing,
    `
      <div class="artifact-grid">
        <div class="visual-frame">
          <img src="${wing.image}" alt="Sanctuary threshold preview" />
        </div>
        <div class="artifact-side">
          ${rows}
          <div class="route-preview">
            <code>${wing.route}</code>
            <span>simulated handoff</span>
          </div>
        </div>
      </div>
    `
  );
}

function renderLegation() {
  const wing = WINGS.legation;
  const metrics = wing.metrics
    .map(([num, label]) => `<div class="legation-metric"><span>${num}</span><small>${label}</small></div>`)
    .join("");
  const ledger = wing.ledger
    .map(([title, text]) => `<div class="ledger-row"><strong>${title}</strong><span>${text}</span></div>`)
    .join("");

  return artifactShell(
    wing,
    `
      <div class="legation-board">${metrics}</div>
      <div class="artifact-grid">
        <div class="artifact-side">${ledger}</div>
        <div class="visual-frame legation-visual" role="img" aria-label="Public record visualization">
          <div class="record-stack">
            <span style="width: 72%"></span>
            <span style="width: 88%"></span>
            <span style="width: 54%"></span>
            <span style="width: 94%"></span>
            <span style="width: 62%"></span>
          </div>
        </div>
      </div>
      <div class="route-preview"><code>${wing.route}</code><span>public dashboard route</span></div>
    `
  );
}

function renderArchitecture() {
  const wing = WINGS.architecture;
  const buttons = wing.layers
    .map(([name], index) => {
      const active = index === 0 ? " is-active" : "";
      return `<button class="layer-button${active}" type="button" data-layer="${index}">${name}</button>`;
    })
    .join("");
  const layers = wing.layers
    .map(([name, text], index) => {
      const active = index === 0 ? " is-active" : "";
      return `<div class="memory-layer${active}" data-layer-panel="${index}"><strong>${name}</strong><span>${text}</span></div>`;
    })
    .join("");

  return artifactShell(
    wing,
    `
      <div class="architecture-map">
        <div class="layer-buttons">${buttons}</div>
        <div class="layer-stage">${layers}</div>
      </div>
      <div class="route-preview"><code>${wing.route}</code><span>interactive diagram route</span></div>
    `
  );
}

function renderResearch() {
  const wing = WINGS.research;
  const tabs = wing.articles
    .map(([title], index) => {
      const active = index === 0 ? " is-active" : "";
      return `<button class="tab-button${active}" type="button" data-article="${index}">${title}</button>`;
    })
    .join("");
  const [firstTitle, firstText] = wing.articles[0];

  return artifactShell(
    wing,
    `
      <div class="reader-tabs">${tabs}</div>
      <div class="reader-panel" data-reader-panel>
        <div class="reader-copy">
          <h4>${firstTitle}</h4>
          <p>${firstText}</p>
        </div>
        <div class="diagram-strip">
          <div class="diagram-node"><strong>claim</strong><span>memory becomes useful when it can be returned with context.</span></div>
          <div class="diagram-node"><strong>figure</strong><span>archive -> extraction -> retrieval -> lived interface.</span></div>
          <div class="diagram-node"><strong>source</strong><span>essay, dispatch, diagram, and project note can all be opened inline.</span></div>
        </div>
      </div>
      <div class="route-preview"><code>${wing.route}</code><span>research library route</span></div>
    `
  );
}

function renderArtifact(intent) {
  const renderers = {
    sanctuary: renderSanctuary,
    legation: renderLegation,
    architecture: renderArchitecture,
    research: renderResearch
  };
  return renderers[intent]();
}

function startPath(intent, source = "click") {
  const wing = WINGS[intent];
  if (!wing) return;

  setActiveWing(intent);
  addTrace(wing.label);

  if (source !== "system") {
    appendMessage("user", `<p>${escapeHtml(wing.label)}</p>`);
  }

  appendMessage(
    "guide",
    `<p>${wing.intro}</p>${renderArtifact(intent)}`
  );
}

function parseIntent(value) {
  const text = value.toLowerCase();
  if (text.includes("sanctuary") || text.includes("resident") || text.includes("opus")) return "sanctuary";
  if (text.includes("legation") || text.includes("observatory") || text.includes("record") || text.includes("dashboard")) return "legation";
  if (text.includes("architecture") || text.includes("memory") || text.includes("retrieval") || text.includes("graph")) return "architecture";
  if (text.includes("research") || text.includes("essay") || text.includes("paper") || text.includes("dispatch")) return "research";
  return null;
}

function handleFreeform(value) {
  const intent = parseIntent(value);
  appendMessage("user", `<p>${escapeHtml(value)}</p>`);

  if (intent) {
    startPath(intent, "system");
    return;
  }

  appendMessage(
    "guide",
    `<p>I can stage this prototype around four simulated wings. Try asking for Sanctuary, Legation, architecture, or research. You can also use the map at left.</p>${renderChoices()}`
  );
}

function handleArtifactAction(action, wingKey) {
  const wing = WINGS[wingKey];
  if (!wing) return;

  if (action === "teach") {
    appendMessage("guide", `<p>${wing.teach}</p>`);
  }

  if (action === "show") {
    appendMessage("guide", `<p>${wing.show}</p>${renderArtifact(wingKey)}`);
  }

  if (action === "open") {
    appendMessage(
      "guide",
      `<p>In the production version, this would route to the full surface. For the prototype, I am preserving the handoff as a visible route target.</p><div class="route-preview"><code>${wing.route}</code><span>${wing.label}</span></div>`
    );
  }
}

function bindDynamicClick(event) {
  const choice = event.target.closest("[data-intent]");
  if (choice && !choice.classList.contains("map-node")) {
    startPath(choice.dataset.intent);
    return;
  }

  const action = event.target.closest("[data-action]");
  if (action) {
    handleArtifactAction(action.dataset.action, action.dataset.wing);
    return;
  }

  const layerButton = event.target.closest("[data-layer]");
  if (layerButton) {
    const artifact = layerButton.closest(".artifact");
    artifact.querySelectorAll("[data-layer]").forEach((button) => {
      button.classList.toggle("is-active", button === layerButton);
    });
    artifact.querySelectorAll("[data-layer-panel]").forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.layerPanel === layerButton.dataset.layer);
    });
    return;
  }

  const articleButton = event.target.closest("[data-article]");
  if (articleButton) {
    const artifact = articleButton.closest(".artifact");
    const index = Number(articleButton.dataset.article);
    const [title, text] = WINGS.research.articles[index];
    artifact.querySelectorAll("[data-article]").forEach((button) => {
      button.classList.toggle("is-active", button === articleButton);
    });
    artifact.querySelector("[data-reader-panel]").innerHTML = `
      <div class="reader-copy">
        <h4>${title}</h4>
        <p>${text}</p>
      </div>
      <div class="diagram-strip">
        <div class="diagram-node"><strong>claim</strong><span>${title.toLowerCase()} becomes legible through guided context.</span></div>
        <div class="diagram-node"><strong>figure</strong><span>the guide can pull the essay, diagram, and citation together.</span></div>
        <div class="diagram-node"><strong>source</strong><span>production would link to the source note and related dispatches.</span></div>
      </div>
    `;
  }
}

function resetPrototype() {
  messagesEl.innerHTML = "";
  pathState.splice(0, pathState.length, "threshold received");
  traceList.innerHTML = "<span>threshold received</span>";
  setActiveWing("sanctuary");
  appendMessage(
    "guide",
    `<p>Mnemos is easier to understand if we enter it through one of its rooms. Choose a wing, or ask in your own words.</p>${renderChoices()}`
  );
}

mapNodes.forEach((node) => {
  node.addEventListener("click", () => startPath(node.dataset.intent));
});

messagesEl.addEventListener("click", bindDynamicClick);

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = chatInput.value.trim();
  if (!value) return;
  chatInput.value = "";
  handleFreeform(value);
});

resetButton.addEventListener("click", resetPrototype);

resetPrototype();
