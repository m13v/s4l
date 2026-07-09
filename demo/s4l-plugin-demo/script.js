const modes = {
  story: {
    title: "Show the S4L plugin as the social tool your agent reaches for.",
    deck:
      "A guided demo for founders who already live in Claude or ChatGPT: ask what is worth replying to, review drafts in your voice, approve posts, then let the queue report back.",
    referenceKicker: "Landing page reviewed",
    referenceTitle: "The buyer story is chat first, not dashboard first.",
    referenceImage: "assets/s4l-landing.png",
    proof: [
      ["No feed switching", "The conversations arrive inside the chat."],
      ["Contribution first", "Drafts answer the thread before mentioning a product."],
      ["Posts as you", "X and Reddit actions run from your connected account."],
      ["Approve first", "Autopilot is a later trust setting, not the default."],
      ["Result memory", "Views and votes teach the next run what worked."]
    ],
    notesTitle: "The agent stays conversational. S4L does the feed work.",
    notes: [
      "The landing page promise is simple: plug S4L into Claude or ChatGPT and let the feed come to the chat.",
      "The demo should show a real user sentence, a visible S4L tool call, a short review queue, and an approval moment.",
      "The value add is not a prettier dashboard. It is fewer tabs, better thread judgment, safer voice, and a feedback loop."
    ],
    recipe: [
      ["01", "Storyboard the buyer path", "Connect, surface, draft, approve, autopilot, report."],
      ["02", "Make the tool call visible", "Render project_config, queue_setup, post_drafts, and get_stats as plain-language beats."],
      ["03", "End on proof", "Show the live-results table and a report so the viewer sees the loop close."]
    ]
  },
  build: {
    title: "Investigate the repo, then explain the plugin machinery without making it feel heavy.",
    deck:
      "This view turns the demo into a blueprint: MCP tools, local runtime, queue-backed Claude jobs, persistent browser profiles, scheduled workers, and the S4L API.",
    referenceKicker: "Repo internals reviewed",
    referenceTitle: "The MCP server is intentionally thin. The pipeline brain stays in scripts and shell wrappers.",
    referenceImage: "assets/s4l-dashboard.png",
    proof: [
      ["MCP wrapper", "Tools expose setup, drafts, posting, stats, runtime, and dashboard."],
      ["Local browser", "Persistent Playwright profiles keep account sessions on the machine."],
      ["Queue worker", "Scheduled tasks drain local Claude jobs and return draft cards."],
      ["Launchd rails", "Background jobs run the repeatable parts on macOS."],
      ["API ledger", "Posts, stats, and state sync through the hosted S4L API."]
    ],
    notesTitle: "The build story: thin tool surface, durable local runtime.",
    notes: [
      "mcp/manifest.json names the desktop plugin S4L and advertises tools for engagement_mode, project_config, post_drafts, get_stats, runtime, queue_setup, dashboard, and connect_product.",
      "mcp/src/index.ts describes the wrapper as orchestration. Discovery, scoring, drafting prompts, posting, and feedback live in the pipeline scripts.",
      "README.md shows the install path: npx social-autoposter init, config/.env, launchd plists, Playwright MCP configs, browser profile dirs, and skill symlinks."
    ],
    recipe: [
      ["01", "Package the plugin", "Build the MCP server and bundle the panel UI into the .mcpb."],
      ["02", "Provision runtime", "Install scripts, skill files, launchd jobs, venv deps, and browser profiles."],
      ["03", "Register workers", "queue_setup returns the scheduled-task spec that keeps draft generation moving."],
      ["04", "Render review cards", "The panel and menu bar read the same review queue so approvals stay aligned."]
    ]
  },
  handoff: {
    title: "Prepare the website integration plan, but keep this demo unlinked for now.",
    deck:
      "This mode shows how the demo would become a sibling-site module later: a Remotion-style beat sequence, a React component, and an optional player on the S4L landing page.",
    referenceKicker: "Website sibling reviewed",
    referenceTitle: "The current site already has Next, Remotion, Framer Motion, and the S4L visual system.",
    referenceImage: "assets/s4l-landing.png",
    proof: [
      ["Standalone now", "No route, import, or navigation link has been added."],
      ["React later", "Port the data model and panels into a client component."],
      ["Remotion-ready", "Each beat maps to a timed composition scene."],
      ["CTA aligned", "Keep the existing desktop plugin and setup-call paths."],
      ["Proof reused", "Use the live results section as the closing evidence."]
    ],
    notesTitle: "Future handoff: use the same story, not a new marketing page.",
    notes: [
      "Add a hidden route or component only when ready, for example src/app/demo/s4l-plugin/page.tsx or src/components/s4l-plugin-demo.tsx.",
      "The website already depends on @remotion/player and remotion, so the same beat data can drive a player or an exportable composition.",
      "Use the current landing page arc: chat-first setup, real thread discovery, review before posting, autopilot after trust, and live results."
    ],
    recipe: [
      ["01", "Move data first", "Copy the beats and modes into a typed data file in the website repo."],
      ["02", "Port UI shell", "Convert the static panels to a client component using existing theme tokens."],
      ["03", "Optional Remotion layer", "Create a composition that consumes the same beats for video export."],
      ["04", "Wire after approval", "Only then add navigation, CTA tracking, and modal events."]
    ]
  }
};

const beats = [
  {
    label: "01",
    kicker: "01 / Connect",
    tab: "Connect the plugin",
    title: "Install once, then start from chat.",
    summary: "The buyer does not learn a new social app.",
    outcome: "No new social dashboard to learn.",
    queueTitle: "Setup checklist",
    queueCount: "4 checks",
    chats: [
      ["user", "User", "Set me up on S4L plugin end to end."],
      ["tool", "S4L tool", "project_config checks runtime, connects X, scans the profile, and discovers product context."],
      ["agent", "Agent", "Your account is connected. I found the product, voice cues, and the topics S4L should watch."]
    ],
    queue: [
      ["Ready", "X session detected", "Persistent browser profile is available for the connected handle.", true],
      ["Ready", "Product context found", "Website, target audience, and conservative claims are captured.", false],
      ["Ready", "Voice guardrails saved", "Topics, hard lines, and engagement style are written to config.", false]
    ],
    flow: [
      ["Chat host", "Claude or ChatGPT", "User asks in natural language", true],
      ["MCP tool", "project_config", "Connect and infer setup", true],
      ["Runtime", "Local installer", "Scripts, env, launchd, profiles", false],
      ["Browser", "Playwright profile", "Auth stays local", false],
      ["API", "S4L ledger", "Project state saved", false]
    ],
    terminal: [
      "$ npx social-autoposter init",
      "copy scripts, skill files, setup skill, browser MCP configs",
      "create ~/.claude/browser-profiles/{twitter,reddit,linkedin}",
      "project_config action: connect_x -> profile_scan -> save"
    ]
  },
  {
    label: "02",
    kicker: "02 / Surface",
    tab: "Find buyers",
    title: "Ask what is worth replying to. The feed comes back filtered.",
    summary: "S4L turns scrolling into a short queue.",
    outcome: "High-fit threads beat keyword alerts.",
    queueTitle: "Threads worth entering",
    queueCount: "3 picks",
    chats: [
      ["user", "User", "Anything worth replying to on X today for our AI testing tool?"],
      ["tool", "S4L tool", "queue worker searches X and Reddit topics, dedupes prior skips, ranks by fit and momentum."],
      ["agent", "Agent", "Three threads look useful. The top one is a founder asking how to test AI-generated code before it reaches production."]
    ],
    queue: [
      ["X", "Founder asking how to test AI-generated code", "Good fit: problem-aware, active replies, product mention optional.", true],
      ["Reddit", "Developers debating whether AI atrophies engineering skill", "Good fit: strong context, contribution-first answer needed.", false],
      ["X", "Thread on code review bottlenecks after AI adoption", "Good fit: high-intent topic, safer as a reply than a pitch.", false]
    ],
    flow: [
      ["Chat host", "User asks", "No feed switching", true],
      ["MCP tool", "queue_setup", "Worker task specs exist", true],
      ["Pipeline", "Candidate search", "Scripts rank and dedupe", true],
      ["Review queue", "Draft cards", "Only fit threads surface", false],
      ["API", "Skipped URLs", "Avoids repeats", false]
    ],
    terminal: [
      "launchd -> skill/run-twitter-cycle.sh",
      "find candidates using project search topics",
      "filter previously posted and explicitly skipped URLs",
      "write review queue batch for the dashboard"
    ]
  },
  {
    label: "03",
    kicker: "03 / Draft",
    tab: "Draft in voice",
    title: "S4L drafts the useful reply first, then earns the product mention.",
    summary: "The output sounds like the account, not a campaign.",
    outcome: "Contribution first, product second.",
    queueTitle: "Draft review card",
    queueCount: "1 ready",
    chats: [
      ["agent", "Agent", "I drafted one reply in your voice. It answers the testing pain first and keeps the link as a light tail."],
      ["draft", "Draft", "The scary bit is not AI writing code. It is losing the habit of proving the change works. I have started treating AI-generated PRs like code from a new hire: run the checks first, then read it. That catches more than review vibes ever did."],
      ["agent", "Agent", "You can approve, edit, reject, or ask S4L to learn from why this missed."]
    ],
    queue: [
      ["Draft 1", "Approve-ready reply", "Tone: pattern recognizer. Product mention: optional tail link.", true],
      ["Guardrail", "No hard claim", "Avoids guaranteed outcomes and keeps the claim grounded.", false],
      ["Memory", "Uses past winners", "Feedback report weights replies that got views without spam signals.", false]
    ],
    flow: [
      ["Context", "Thread + project", "Specific situation", true],
      ["Voice", "Config + profile", "Tone and never-say list", true],
      ["Prompt", "Feedback report", "Past winners and misses", true],
      ["Draft", "Review card", "Editable before posting", true],
      ["Learn", "Review event", "Approval reason stored", false]
    ],
    terminal: [
      "top_performers.py --platform twitter --project Assrt",
      "run_claude.sh routes queued draft job to Claude worker",
      "draft card appended to REVIEW_QUEUE_ID=review-queue",
      "nothing posts until post_drafts receives approval"
    ]
  },
  {
    label: "04",
    kicker: "04 / Approve",
    tab: "Post safely",
    title: "Approval posts from the user's account, through the local browser rail.",
    summary: "The social action stays attached to the real account session.",
    outcome: "Approval is explicit and auditable.",
    queueTitle: "Approved drafts",
    queueCount: "2 actions",
    chats: [
      ["user", "User", "Post draft 1. Keep draft 2 for later."],
      ["tool", "S4L tool", "post_drafts posts selected cards, verifies the URL, logs the result, and leaves the rest queued."],
      ["agent", "Agent", "Posted draft 1 and captured the URL. Draft 2 is still waiting in the queue."]
    ],
    queue: [
      ["Posted", "Draft 1", "URL captured and status moved out of review.", true],
      ["Queued", "Draft 2", "Still editable. It is not dropped by posting another card.", false],
      ["Logged", "Post result", "Platform, project, style, prompt variant, and timestamp are persisted.", false]
    ],
    flow: [
      ["Chat host", "Approval", "Specific draft numbers", true],
      ["MCP tool", "post_drafts", "Only chosen cards", true],
      ["Browser", "X profile", "Local session posts", true],
      ["Verify", "Permalink", "Capture live URL", true],
      ["API", "Stats row", "Tracked for feedback", false]
    ],
    terminal: [
      "post_drafts batch=review-queue approved=1",
      "open persistent browser profile for twitter",
      "compose -> submit -> verify permalink",
      "INSERT post row with project_name and engagement_style"
    ]
  },
  {
    label: "05",
    kicker: "05 / Autopilot",
    tab: "Report back",
    title: "Once trusted, S4L drafts in the background and reports what happened.",
    summary: "Autopilot starts as draft autopilot. Posting autopilot is opt-in.",
    outcome: "Background help with visible control.",
    queueTitle: "Weekly report",
    queueCount: "Live stats",
    chats: [
      ["user", "User", "Turn on the draft schedule for this lane."],
      ["tool", "S4L tool", "queue_setup returns the worker spec. Scheduled tasks drain jobs and keep draft cards arriving."],
      ["agent", "Agent", "Draft autopilot is running. I will surface new review cards and show stats after posts go live."]
    ],
    queue: [
      ["Drafted", "9 review cards", "Nothing posted without approval unless posting autopilot is explicitly enabled.", true],
      ["Shipped", "4 approved replies", "The report links every live post back to its source thread.", false],
      ["Learned", "2 style updates", "Approved/rejected reasons flow into learned preferences.", false]
    ],
    flow: [
      ["Schedule", "queue_setup", "Task spec created", true],
      ["Worker", "s4l-worker", "Drains Claude jobs", true],
      ["Menu bar", "Review cards", "User approves", true],
      ["Stats", "get_stats", "Views and votes", true],
      ["Loop", "Feedback digest", "Next run improves", true]
    ],
    terminal: [
      "queue_setup -> create_scheduled_task(taskId=s4l-worker)",
      "launchd kicker keeps the local draft cycle warm",
      "get_stats reads live post and engagement stats",
      "feedback digest writes learned_preferences into config"
    ]
  }
];

const els = {
  title: document.getElementById("demo-title"),
  deck: document.getElementById("mode-deck"),
  proofStrip: document.getElementById("proof-strip"),
  referenceImage: document.getElementById("reference-image"),
  referenceKicker: document.getElementById("reference-kicker"),
  referenceTitle: document.getElementById("reference-title"),
  beatList: document.getElementById("beat-list"),
  beatKicker: document.getElementById("beat-kicker"),
  beatTitle: document.getElementById("beat-title"),
  beatOutcome: document.getElementById("beat-outcome"),
  chatLog: document.getElementById("chat-log"),
  queueTitle: document.getElementById("queue-title"),
  queueCount: document.getElementById("queue-count"),
  queueItems: document.getElementById("queue-items"),
  flowMap: document.getElementById("flow-map"),
  terminalLog: document.getElementById("terminal-log"),
  notesTitle: document.getElementById("notes-title"),
  notesList: document.getElementById("notes-list"),
  recipeList: document.getElementById("recipe-list"),
  playToggle: document.getElementById("play-toggle"),
  playIcon: document.getElementById("play-icon")
};

let currentMode = "story";
let currentBeat = 0;
let playing = true;
let timer = null;

function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function textEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text;
  return el;
}

function renderMode() {
  const mode = modes[currentMode];
  els.title.textContent = mode.title;
  els.deck.textContent = mode.deck;
  els.referenceImage.src = mode.referenceImage;
  els.referenceKicker.textContent = mode.referenceKicker;
  els.referenceTitle.textContent = mode.referenceTitle;

  clearNode(els.proofStrip);
  mode.proof.forEach(([title, body]) => {
    const item = document.createElement("div");
    item.className = "proof-item";
    item.appendChild(textEl("strong", "", title));
    item.appendChild(textEl("span", "", body));
    els.proofStrip.appendChild(item);
  });

  els.notesTitle.textContent = mode.notesTitle;
  clearNode(els.notesList);
  mode.notes.forEach((note) => {
    els.notesList.appendChild(textEl("li", "", note));
  });

  clearNode(els.recipeList);
  mode.recipe.forEach(([num, title, body]) => {
    const step = document.createElement("div");
    step.className = "recipe-step";
    step.appendChild(textEl("span", "", num));
    const copy = document.createElement("div");
    copy.appendChild(textEl("strong", "", title));
    copy.appendChild(textEl("p", "", body));
    step.appendChild(copy);
    els.recipeList.appendChild(step);
  });
}

function renderBeatList() {
  clearNode(els.beatList);
  beats.forEach((beat, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `beat-button${index === currentBeat ? " is-active" : ""}`;
    button.setAttribute("aria-pressed", index === currentBeat ? "true" : "false");
    button.appendChild(textEl("small", "", beat.kicker));
    button.appendChild(textEl("strong", "", beat.tab));
    button.appendChild(textEl("span", "", beat.summary));
    button.addEventListener("click", () => {
      pause();
      setBeat(index);
    });
    els.beatList.appendChild(button);
  });
}

function renderChat(beat) {
  clearNode(els.chatLog);
  beat.chats.forEach(([kind, name, text], index) => {
    const bubble = document.createElement("div");
    bubble.className = `bubble ${kind}`;
    bubble.style.animationDelay = `${index * 70}ms`;
    bubble.appendChild(textEl("strong", "", name));
    bubble.appendChild(textEl("p", "", text));
    els.chatLog.appendChild(bubble);
  });
}

function renderQueue(beat) {
  els.queueTitle.textContent = beat.queueTitle;
  els.queueCount.textContent = beat.queueCount;
  clearNode(els.queueItems);
  beat.queue.forEach(([meta, title, body, priority], index) => {
    const card = document.createElement("article");
    card.className = `queue-card${priority ? " is-priority" : ""}`;
    card.style.animationDelay = `${index * 80}ms`;

    const metaLine = document.createElement("div");
    metaLine.className = "meta";
    metaLine.appendChild(textEl("span", "", meta));
    metaLine.appendChild(textEl("span", "", priority ? "top fit" : "queued"));

    card.appendChild(metaLine);
    card.appendChild(textEl("h4", "", title));
    card.appendChild(textEl("p", "", body));
    els.queueItems.appendChild(card);
  });
}

function renderFlow(beat) {
  clearNode(els.flowMap);
  beat.flow.forEach(([step, title, body, active]) => {
    const node = document.createElement("div");
    node.className = `flow-node${active ? " is-active" : ""}`;
    node.appendChild(textEl("span", "", step));
    node.appendChild(textEl("strong", "", title));
    node.appendChild(textEl("small", "", body));
    els.flowMap.appendChild(node);
  });
}

function renderTerminal(beat) {
  els.terminalLog.textContent = beat.terminal.map((line) => `> ${line}`).join("\n");
}

function setBeat(index) {
  currentBeat = (index + beats.length) % beats.length;
  const beat = beats[currentBeat];
  els.beatKicker.textContent = beat.kicker;
  els.beatTitle.textContent = beat.title;
  els.beatOutcome.textContent = beat.outcome;
  renderBeatList();
  renderChat(beat);
  renderQueue(beat);
  renderFlow(beat);
  renderTerminal(beat);
}

function setMode(modeName) {
  currentMode = modeName;
  document.querySelectorAll(".mode-tab").forEach((button) => {
    const active = button.dataset.mode === modeName;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  renderMode();
}

function pause() {
  playing = false;
  updatePlayIcon();
  if (timer) window.clearInterval(timer);
  timer = null;
}

function play() {
  playing = true;
  updatePlayIcon();
  if (timer) window.clearInterval(timer);
  timer = window.setInterval(() => setBeat(currentBeat + 1), 7200);
}

function updatePlayIcon() {
  if (playing) {
    els.playToggle.setAttribute("aria-label", "Pause autoplay");
    els.playIcon.innerHTML = '<path d="M9 6v12M15 6v12" />';
  } else {
    els.playToggle.setAttribute("aria-label", "Start autoplay");
    els.playIcon.innerHTML = '<path d="m8 5 11 7-11 7V5Z" />';
  }
}

document.querySelectorAll(".mode-tab").forEach((button) => {
  button.addEventListener("click", () => {
    pause();
    setMode(button.dataset.mode);
  });
});

document.getElementById("prev-beat").addEventListener("click", () => {
  pause();
  setBeat(currentBeat - 1);
});

document.getElementById("next-beat").addEventListener("click", () => {
  pause();
  setBeat(currentBeat + 1);
});

els.playToggle.addEventListener("click", () => {
  if (playing) {
    pause();
  } else {
    play();
  }
});

renderMode();
setBeat(0);
play();
