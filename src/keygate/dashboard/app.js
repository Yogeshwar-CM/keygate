/* =============================================================================
   keygate dashboard — vanilla, no build step, no dependencies.

   Everything here talks to /admin/api. That API is unauthenticated: the whole
   dashboard is only as private as the interface keygate is bound to.
   ============================================================================= */

"use strict";

/* ── tiny helpers ─────────────────────────────────────────────────────────── */

const $ = (sel, root = document) => root.querySelector(sel);

/** Escape anything that lands in innerHTML. Labels and names are user input. */
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Money, tuned for LLM spend: fractions of a cent still have to be visible. */
function usd(value, digits = 4) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n > 0 && n < 10 ** -digits) return "<$" + (10 ** -digits).toFixed(digits);
  return (
    "$" +
    n.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    })
  );
}

const budgetText = (v) => (v === null || v === undefined ? "unlimited" : usd(v, 2));

const int = (v) => Number(v || 0).toLocaleString("en-US");

/** ISO-8601 from the store → local "MM-DD HH:MM:SS". */
function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts.endsWith("Z") ? ts : ts + "Z");
  if (Number.isNaN(d.getTime())) return ts.slice(0, 19).replace("T", " ");
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(
    d.getMinutes()
  )}:${p(d.getSeconds())}`;
}

function ago(ts) {
  if (!ts) return "never";
  const d = new Date(ts.endsWith("Z") ? ts : ts + "Z");
  const secs = (Date.now() - d.getTime()) / 1000;
  if (!Number.isFinite(secs)) return "—";
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

/* ── budget meter ─────────────────────────────────────────────────────────── */

function meter(spent, budget) {
  const s = Number(spent || 0);
  if (budget === null || budget === undefined) {
    return `<div class="meter meter--none">
      <div class="meter__track"></div>
      <div class="meter__label"><span>${esc(usd(s))} spent</span><span>unlimited</span></div>
    </div>`;
  }
  const b = Number(budget);
  const ratio = b > 0 ? Math.min(s / b, 1) : 1;
  const over = b > 0 ? s / b : 1;
  const tone = over >= 0.9 ? " meter--danger" : over >= 0.75 ? " meter--warn" : "";
  const left = Math.max(0, b - s);
  return `<div class="meter${tone}" title="${esc(usd(s))} of ${esc(usd(b, 2))} used">
    <div class="meter__track"><div class="meter__fill" style="width:${(
      ratio * 100
    ).toFixed(1)}%"></div></div>
    <div class="meter__label">
      <span>${(over * 100).toFixed(over >= 0.1 ? 0 : 1)}%</span>
      <span>${esc(usd(left, b >= 1 ? 2 : 4))} left</span>
    </div>
  </div>`;
}

function statusBadge(status) {
  const n = Number(status);
  if (n >= 500) return `<span class="badge badge--danger">${n}</span>`;
  if (n >= 400) return `<span class="badge badge--warning">${n}</span>`;
  return `<span class="badge badge--success">${n}</span>`;
}

function emptyState(title, body, cmd) {
  return `<div class="empty">
    <span class="empty__icon" aria-hidden="true">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"
           stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
        <rect x="2.5" y="3.5" width="11" height="9" rx="2"/><path d="M2.5 6.5h11"/>
      </svg>
    </span>
    <div class="empty__title">${esc(title)}</div>
    <div class="empty__body">${body}</div>
    ${cmd ? `<code class="empty__cmd">${esc(cmd)}</code>` : ""}
  </div>`;
}

/* ── API ──────────────────────────────────────────────────────────────────── */

async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  } else if (method !== "GET") {
    // Always give the server a Content-Length; it refuses to guess.
    init.headers["Content-Type"] = "application/json";
    init.body = "{}";
  }
  let res;
  try {
    res = await fetch("/admin/api" + path, init);
  } catch (err) {
    throw new Error("cannot reach keygate — is the server still running?");
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (err) {
    /* fall through to the status-based message */
  }
  if (!res.ok) {
    const message = (data && data.error && data.error.message) || `HTTP ${res.status}`;
    const error = new Error(message);
    error.code = data && data.error && data.error.code;
    throw error;
  }
  return data;
}

const qs = (params) => {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== "" && v !== null && v !== undefined && v !== false)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
  return parts.length ? "?" + parts.join("&") : "";
};

/* ── toasts ───────────────────────────────────────────────────────────────── */

function toast(title, description, tone = "success") {
  const node = document.createElement("div");
  node.className = "toast" + (tone ? ` toast--${tone}` : "");
  node.innerHTML = `<div class="grow">
      <div class="toast__title">${esc(title)}</div>
      ${description ? `<div class="toast__description">${esc(description)}</div>` : ""}
    </div>`;
  $("#toaster").appendChild(node);
  const kill = () => {
    node.dataset.leaving = "true";
    setTimeout(() => node.remove(), 200);
  };
  setTimeout(kill, tone === "danger" ? 7000 : 4000);
  node.addEventListener("click", kill);
}

const fail = (err) => toast("Failed", err.message || String(err), "danger");

/* ── modal ────────────────────────────────────────────────────────────────── */

const overlay = () => $("#overlay");

let lastFocus = null;

function closeModal() {
  const node = overlay();
  if (node.hidden) return;
  node.hidden = true;
  node.innerHTML = "";
  if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  lastFocus = null;
}

const modalOpen = () => !overlay().hidden;

/**
 * A form modal. `fields` are rendered in order; `onSubmit(values)` may throw or
 * reject, in which case the message is shown inline and the modal stays open.
 */
function openModal({ title, description, fields = [], submit, wide, onSubmit, extra }) {
  lastFocus = document.activeElement;
  const node = overlay();
  node.hidden = false;
  node.innerHTML = `
    <form class="modal${wide ? " modal--wide" : ""}" id="modal-form" autocomplete="off">
      <div class="modal__header">
        <div class="grow">
          <h2 class="modal__title" id="modal-title">${esc(title)}</h2>
          ${description ? `<p class="modal__description">${description}</p>` : ""}
        </div>
        <button class="btn btn--ghost btn--sm btn--icon" type="button" data-close
                title="Close (Esc)" aria-label="Close">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor"
               stroke-width="1.5" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>
        </button>
      </div>
      <div class="modal__body">
        <div class="modal__error" id="modal-error" hidden></div>
        <div class="form-grid form-grid--stack">
          ${fields.map(fieldHTML).join("")}
        </div>
        ${extra || ""}
      </div>
      <div class="modal__footer">
        <button class="btn" type="button" data-close>Cancel</button>
        <button class="btn btn--accent" type="submit" id="modal-submit">${esc(
          submit || "Save"
        )}</button>
      </div>
    </form>`;

  node.querySelectorAll("[data-close]").forEach((b) =>
    b.addEventListener("click", closeModal)
  );
  node.addEventListener("mousedown", (ev) => {
    if (ev.target === node) closeModal();
  });

  const form = $("#modal-form");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const values = {};
    for (const field of fields) {
      const input = form.elements[field.name];
      values[field.name] = input ? input.value.trim() : "";
    }
    const button = $("#modal-submit");
    button.disabled = true;
    try {
      await onSubmit(values);
      closeModal();
    } catch (err) {
      const box = $("#modal-error");
      box.hidden = false;
      box.textContent = err.message || String(err);
      button.disabled = false;
    }
  });

  const first = form.querySelector("input:not([readonly]), select");
  if (first) first.focus();
}

function fieldHTML(field) {
  const id = "f-" + field.name;
  const common = `id="${id}" name="${esc(field.name)}" class="input${
    field.mono ? " input--mono" : ""
  }"`;
  const control =
    field.type === "select"
      ? `<select id="${id}" name="${esc(field.name)}" class="select">${field.options
          .map(
            (o) =>
              `<option value="${esc(o.value)}"${
                o.value === field.value ? " selected" : ""
              }>${esc(o.label)}</option>`
          )
          .join("")}</select>`
      : `<input ${common} type="${field.type || "text"}" value="${esc(
          field.value || ""
        )}" placeholder="${esc(field.placeholder || "")}"${
          field.readonly ? " readonly" : ""
        }${field.step ? ` step="${esc(field.step)}"` : ""}${
          field.min !== undefined ? ` min="${esc(field.min)}"` : ""
        }>`;
  return `<label class="field" for="${id}">
    <span class="field__label">${esc(field.label)}</span>
    ${control}
    ${field.hint ? `<span class="field__hint">${field.hint}</span>` : ""}
  </label>`;
}

/** The one screen that shows a plaintext key. It never comes back. */
function openKeyModal(result) {
  lastFocus = document.activeElement;
  const node = overlay();
  node.hidden = false;
  node.innerHTML = `
    <div class="modal modal--wide">
      <div class="modal__header">
        <div class="grow">
          <h2 class="modal__title" id="modal-title">Virtual key for ${esc(
            result.user
          )}</h2>
          <p class="modal__description">
            Budget ${esc(budgetText(result.budget_usd))}${
    result.label ? ` · label <span class="mono">${esc(result.label)}</span>` : ""
  } · prefix <span class="mono">${esc(result.prefix)}</span>
          </p>
        </div>
      </div>
      <div class="modal__body">
        <div class="keyout">
          <code class="keyout__value" id="minted-key">${esc(result.key)}</code>
          <button class="btn btn--accent btn--sm" type="button" id="copy-key">Copy</button>
        </div>
        <div class="warn">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor"
               stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
               style="flex-shrink:0;margin-top:2px">
            <path d="M8 2.8 14.2 13H1.8L8 2.8Z"/><path d="M8 6.6v2.8"/><path d="M8 11.4h.01"/>
          </svg>
          <span>
            <strong>Shown once.</strong> keygate stores only the SHA-256 digest, so
            this exact string cannot be recovered — copy it now. Lost it? Revoke
            <span class="mono">${esc(result.prefix)}</span> and mint another.
          </span>
        </div>
      </div>
      <div class="modal__footer">
        <button class="btn btn--accent" type="button" data-close>Done</button>
      </div>
    </div>`;
  node.querySelectorAll("[data-close]").forEach((b) =>
    b.addEventListener("click", closeModal)
  );
  $("#copy-key").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(result.key);
      toast("Copied", "The key is on your clipboard.");
    } catch (err) {
      const range = document.createRange();
      range.selectNodeContents($("#minted-key"));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast("Select and copy", "Clipboard access was refused.", "danger");
    }
  });
  $("#copy-key").focus();
}

/* ── state ────────────────────────────────────────────────────────────────── */

const state = {
  view: "overview",
  overview: null,
  data: null,
  users: [],
  live: true,
  busy: false,
  filters: {
    userQuery: "",
    keyUser: "",
    keyAll: false,
    usageSince: "24h",
    usageUser: "",
    usageDetail: false,
    auditLimit: "100",
  },
};

const ICONS = {
  overview: `<path d="M2.5 9.5 6 5.5l3 3 4.5-5.5"/><path d="M2.5 13.5h11"/>`,
  users: `<circle cx="6" cy="6" r="2.4"/><path d="M2 13.2c.5-2.2 2.1-3.4 4-3.4s3.5 1.2 4 3.4"/><path d="M11 4.2a2.2 2.2 0 0 1 0 4.2"/>`,
  keys: `<circle cx="5" cy="8" r="2.6"/><path d="M7.6 8h6.4"/><path d="M12 8v2.4"/>`,
  usage: `<path d="M3 12.5V8"/><path d="M6.5 12.5V4"/><path d="M10 12.5V9.5"/><path d="M13.5 12.5V6"/>`,
  audit: `<path d="M4 2.5h5.5L13 6v7.5H4z"/><path d="M9.3 2.6V6H13"/><path d="M6 9h5"/><path d="M6 11h3.5"/>`,
};

const VIEWS = [
  { id: "overview", label: "Overview", title: "Overview", sub: "workspace at a glance" },
  { id: "users", label: "Users", title: "Users", sub: "identity and budgets" },
  { id: "keys", label: "Keys", title: "Virtual keys", sub: "kg_v1_… issued to users" },
  { id: "usage", label: "Usage", title: "Usage", sub: "metered requests and spend" },
  { id: "audit", label: "Audit", title: "Audit log", sub: "who changed what" },
];

/* ── views ────────────────────────────────────────────────────────────────── */

const views = {
  /* -- overview ----------------------------------------------------------- */

  async overview() {
    const [o, u] = await Promise.all([api("/overview"), api("/users")]);
    state.overview = o;
    state.users = u.users;
    return { o, users: u.users };
  },

  renderOverview({ o, users }) {
    const ranked = users
      .slice()
      .sort((a, b) => b.spent_usd - a.spent_usd)
      .slice(0, 6);

    const tiles = [
      { label: "Total spend", value: usd(o.total_spent_usd), foot: `${int(o.requests_total)} requests all time`, tone: "accent" },
      { label: "Users", value: int(o.users), foot: `${int(o.live_keys)} live keys` },
      { label: "Spend · 24h", value: usd(o.spent_24h), foot: `${int(o.requests_24h)} requests` },
      {
        label: "Errors · 24h",
        value: int(o.errors_24h),
        foot: o.errors_24h ? "check the usage log" : "clean window",
        tone: o.errors_24h ? "danger" : "",
      },
      {
        label: "Upstream key",
        value: o.upstream_key_configured ? "set" : "missing",
        foot: o.upstream_key_configured ? "proxy can reach upstream" : "requests will 503",
        tone: o.upstream_key_configured ? "" : "danger",
      },
    ];

    return `
      <section class="stats">
        ${tiles
          .map(
            (t) => `<article class="stat">
              <span class="eyebrow">${esc(t.label)}</span>
              <span class="stat__value${t.tone ? ` stat__value--${t.tone}` : ""}">${esc(
              t.value
            )}</span>
              <span class="stat__foot">${esc(t.foot)}</span>
            </article>`
          )
          .join("")}
      </section>

      <div class="split">
        <section class="card">
          <div class="card__header">
            <div>
              <h2 class="card__title">Budgets</h2>
              <p class="card__description">Lifetime spend against each user's cap.</p>
            </div>
            <button class="btn btn--sm" type="button" data-action="goto" data-view="users">
              All users
            </button>
          </div>
          <div class="card__body card__body--flush">
            ${
              ranked.length
                ? `<div class="budgets">${ranked
                    .map(
                      (u) => `<div class="budget">
                        <div style="min-width:0">
                          <div class="budget__name">${esc(u.name)}</div>
                          <div class="budget__sub">${esc(usd(u.spent_usd))} of ${esc(
                        budgetText(u.budget_usd)
                      )} · ${int(u.live_keys)} key${u.live_keys === 1 ? "" : "s"}</div>
                        </div>
                        ${meter(u.spent_usd, u.budget_usd)}
                      </div>`
                    )
                    .join("")}</div>`
                : emptyState(
                    "No users yet",
                    "Add someone, then mint them a virtual key. They point their client at this gateway and every request gets attributed.",
                    "keygate user add alice --budget 25"
                  )
            }
          </div>
        </section>

        <section class="card">
          <div class="card__header">
            <div>
              <h2 class="card__title">Recent requests</h2>
              <p class="card__description">Newest first, straight off the request log.</p>
            </div>
            <button class="btn btn--sm" type="button" data-action="goto" data-view="usage">
              Full log
            </button>
          </div>
          <div class="card__body card__body--flush">
            ${
              o.recent.length
                ? `<div class="table-wrap"><table class="table">
                    <thead><tr>
                      <th>When</th><th>User</th><th>Model</th><th>Status</th>
                      <th class="cnum">Cost</th>
                    </tr></thead>
                    <tbody>${o.recent
                      .map(
                        (r) => `<tr>
                          <td class="cmono">${esc(when(r.ts))}</td>
                          <td class="primary">${esc(r.user || "—")}</td>
                          <td class="cmono">${esc(r.model || "—")}</td>
                          <td>${statusBadge(r.status)}</td>
                          <td class="cnum">${esc(usd(r.cost_usd))}</td>
                        </tr>`
                      )
                      .join("")}</tbody>
                  </table></div>`
                : emptyState(
                    "Nothing proxied yet",
                    `Point a client at <span class="mono">${esc(
                      location.origin
                    )}/v1</span> with <span class="mono">Authorization: Bearer kg_v1_…</span> and requests will show up here.`,
                    "curl " + location.origin + "/v1/chat/completions"
                  )
            }
          </div>
        </section>
      </div>`;
  },

  /* -- users -------------------------------------------------------------- */

  async users() {
    const data = await api("/users");
    state.users = data.users;
    return data;
  },

  renderUsers({ users }) {
    const q = state.filters.userQuery.toLowerCase();
    const shown = q
      ? users.filter(
          (u) =>
            u.name.toLowerCase().includes(q) ||
            (u.email || "").toLowerCase().includes(q)
        )
      : users;

    return `<section class="card">
      <div class="card__header">
        <div>
          <h2 class="card__title">Users</h2>
          <p class="card__description">${int(users.length)} user${
      users.length === 1 ? "" : "s"
    } · budgets are lifetime caps, enforced on every proxied request.</p>
        </div>
        <div class="card__tools">
          <input class="input input--search" type="search" id="user-search"
                 placeholder="Filter users" value="${esc(state.filters.userQuery)}"
                 aria-label="Filter users">
          <button class="btn btn--accent" type="button" data-action="add-user">
            Add user
          </button>
        </div>
      </div>
      <div class="card__body card__body--flush">
        ${
          shown.length
            ? `<div class="table-wrap"><table class="table">
                <thead><tr>
                  <th class="primary">User</th><th>Email</th>
                  <th class="cnum">Budget</th><th class="cnum">Spent</th>
                  <th>Remaining</th><th class="cnum">Keys</th><th>State</th>
                  <th class="cact"></th>
                </tr></thead>
                <tbody>${shown.map(userRow).join("")}</tbody>
              </table></div>`
            : users.length
            ? emptyState("No match", `Nothing matches <span class="mono">${esc(
                state.filters.userQuery
              )}</span>.`)
            : emptyState(
                "No users yet",
                "Use <strong>Add user</strong> above, or the CLI. A user is just a name, an optional email and an optional lifetime budget.",
                "keygate user add alice --budget 25"
              )
        }
      </div>
    </section>`;
  },

  /* -- keys --------------------------------------------------------------- */

  async keys() {
    const [data, users] = await Promise.all([
      api("/keys" + qs({ user: state.filters.keyUser, all: state.filters.keyAll })),
      api("/users"),
    ]);
    state.users = users.users;
    return data;
  },

  renderKeys({ keys }) {
    const options = [`<option value="">All users</option>`]
      .concat(
        state.users.map(
          (u) =>
            `<option value="${esc(u.name)}"${
              u.name === state.filters.keyUser ? " selected" : ""
            }>${esc(u.name)}</option>`
        )
      )
      .join("");

    return `<section class="card">
      <div class="card__header">
        <div>
          <h2 class="card__title">Virtual keys</h2>
          <p class="card__description">Only the SHA-256 digest is stored. Revoking takes effect on the next request.</p>
        </div>
        <div class="card__tools">
          <select class="select select--inline" id="key-user" aria-label="Filter by user">
            ${options}
          </select>
          <button class="btn btn--sm" type="button" data-action="toggle-revoked"
                  aria-pressed="${state.filters.keyAll}">
            ${state.filters.keyAll ? "All keys" : "Live only"}
          </button>
          <button class="btn btn--accent" type="button" data-action="mint"
                  ${state.users.length ? "" : "disabled"}>Mint key</button>
        </div>
      </div>
      <div class="card__body card__body--flush">
        ${
          keys.length
            ? `<div class="table-wrap"><table class="table">
                <thead><tr>
                  <th>Prefix</th><th class="primary">User</th><th>Label</th>
                  <th class="cnum">Budget</th><th class="cnum">Spent</th>
                  <th>Last used</th><th>State</th><th class="cact"></th>
                </tr></thead>
                <tbody>${keys.map(keyRow).join("")}</tbody>
              </table></div>`
            : state.users.length
            ? emptyState(
                state.filters.keyUser ? "No keys for this user" : "No live keys",
                "Mint one above and hand the plaintext to its owner — it is shown exactly once.",
                `keygate user mint ${state.filters.keyUser || "alice"}`
              )
            : emptyState(
                "No users to mint for",
                "Keys belong to users. Add a user first.",
                "keygate user add alice --budget 25"
              )
        }
      </div>
    </section>`;
  },

  /* -- usage -------------------------------------------------------------- */

  async usage() {
    const f = state.filters;
    const [data, users] = await Promise.all([
      api(
        "/usage" +
          qs({
            since: f.usageSince,
            user: f.usageUser,
            detail: f.usageDetail,
            limit: 200,
          })
      ),
      api("/users"),
    ]);
    state.users = users.users;
    return data;
  },

  renderUsage(data) {
    const f = state.filters;
    const userOptions = [`<option value="">All users</option>`]
      .concat(
        state.users.map(
          (u) =>
            `<option value="${esc(u.name)}"${
              u.name === f.usageUser ? " selected" : ""
            }>${esc(u.name)}</option>`
        )
      )
      .join("");
    const sinceOptions = [
      ["1h", "Last hour"],
      ["24h", "Last 24 hours"],
      ["7d", "Last 7 days"],
      ["30d", "Last 30 days"],
      ["", "All time"],
    ]
      .map(
        ([v, label]) =>
          `<option value="${v}"${v === f.usageSince ? " selected" : ""}>${label}</option>`
      )
      .join("");

    const head = `<div class="card__header">
      <div>
        <h2 class="card__title">${f.usageDetail ? "Request log" : "Spend by user"}</h2>
        <p class="card__description">${
          f.usageDetail
            ? "Every proxied request, newest first — including the ones keygate refused."
            : "Rolled up over the selected window. Budget and remaining are lifetime figures."
        }</p>
      </div>
      <div class="card__tools">
        <select class="select select--inline" id="usage-since" aria-label="Time window">
          ${sinceOptions}
        </select>
        <select class="select select--inline" id="usage-user" aria-label="Filter by user">
          ${userOptions}
        </select>
        <button class="btn btn--sm" type="button" data-action="toggle-detail"
                aria-pressed="${f.usageDetail}">
          ${f.usageDetail ? "Detail" : "Summary"}
        </button>
      </div>
    </div>`;

    if (data.detail) {
      return `<section class="card">${head}
        <div class="card__body card__body--flush">
          ${
            data.rows.length
              ? `<div class="table-wrap"><table class="table">
                  <thead><tr>
                    <th>When</th><th class="primary">User</th><th>Key</th><th>Model</th>
                    <th>Status</th><th class="cnum">In</th><th class="cnum">Out</th>
                    <th class="cnum">Cost</th><th>Note</th>
                  </tr></thead>
                  <tbody>${data.rows
                    .map(
                      (r) => `<tr>
                        <td class="cmono">${esc(when(r.ts))}</td>
                        <td class="primary">${esc(r.user || "—")}</td>
                        <td class="cmono">${esc(r.key_prefix || "—")}</td>
                        <td class="cmono">${esc(r.model || "—")}</td>
                        <td>${statusBadge(r.status)}</td>
                        <td class="cnum">${int(r.prompt_tokens)}</td>
                        <td class="cnum">${int(r.completion_tokens)}</td>
                        <td class="cnum">${esc(usd(r.cost_usd))}</td>
                        <td class="cmono">${esc(r.note || "")}</td>
                      </tr>`
                    )
                    .join("")}</tbody>
                </table></div>`
              : emptyState(
                  "No requests in this window",
                  "Widen the window, or send a request through the gateway.",
                  "keygate usage --detail"
                )
          }
        </div>
      </section>`;
    }

    const rows = data.summary;
    return `<section class="card">${head}
      <div class="card__body card__body--flush">
        ${
          rows.length
            ? `<div class="table-wrap"><table class="table">
                <thead><tr>
                  <th class="primary">User</th><th class="cnum">Requests</th>
                  <th class="cnum">Errors</th><th class="cnum">In</th>
                  <th class="cnum">Out</th><th class="cnum">Cost</th>
                  <th class="cnum">Budget</th><th>Remaining</th>
                </tr></thead>
                <tbody>${rows
                  .map(
                    (r) => `<tr>
                      <td class="primary">${esc(r.user)}</td>
                      <td class="cnum">${int(r.requests)}</td>
                      <td class="cnum">${
                        r.errors
                          ? `<span class="badge badge--danger">${int(r.errors)}</span>`
                          : "0"
                      }</td>
                      <td class="cnum">${int(r.prompt_tokens)}</td>
                      <td class="cnum">${int(r.completion_tokens)}</td>
                      <td class="cnum">${esc(usd(r.cost_usd))}</td>
                      <td class="cnum">${esc(budgetText(r.budget_usd))}</td>
                      <td class="cmeter">${meter(r.cost_usd, r.budget_usd)}</td>
                    </tr>`
                  )
                  .join("")}
                </tbody>
              </table></div>
              <div class="budget" style="border-top:1px solid var(--border)">
                <span class="eyebrow">Total for window</span>
                <span class="num" style="text-align:right;font-size:var(--t-lg)">${esc(
                  usd(data.total_usd)
                )}</span>
              </div>`
            : emptyState(
                "Nothing metered yet",
                "Requests appear here as soon as a virtual key is used against the gateway.",
                "keygate usage"
              )
        }
      </div>
    </section>`;
  },

  /* -- audit -------------------------------------------------------------- */

  async audit() {
    return api("/audit" + qs({ limit: state.filters.auditLimit }));
  },

  renderAudit({ rows }) {
    const limits = ["50", "100", "250", "1000"]
      .map(
        (v) =>
          `<option value="${v}"${
            v === state.filters.auditLimit ? " selected" : ""
          }>Last ${v}</option>`
      )
      .join("");
    return `<section class="card">
      <div class="card__header">
        <div>
          <h2 class="card__title">Audit log</h2>
          <p class="card__description">Every mutation, tagged with where it came from — <span class="mono">cli</span> or <span class="mono">dashboard</span>.</p>
        </div>
        <div class="card__tools">
          <select class="select select--inline" id="audit-limit" aria-label="Rows">
            ${limits}
          </select>
        </div>
      </div>
      <div class="card__body card__body--flush">
        ${
          rows.length
            ? `<div class="table-wrap"><table class="table">
                <thead><tr>
                  <th>When</th><th>Actor</th><th class="primary">Action</th>
                  <th>Target</th><th>Detail</th>
                </tr></thead>
                <tbody>${rows
                  .map(
                    (r) => `<tr>
                      <td class="cmono">${esc(when(r.ts))}</td>
                      <td><span class="badge${
                        r.actor === "dashboard" ? " badge--accent" : ""
                      }">${esc(r.actor)}</span></td>
                      <td class="primary cmono" style="color:var(--text)">${esc(
                        r.action
                      )}</td>
                      <td class="cmono">${esc(r.target || "—")}</td>
                      <td class="cmono cwide">${esc(r.detail || "")}</td>
                    </tr>`
                  )
                  .join("")}</tbody>
              </table></div>`
            : emptyState("Nothing logged yet", "Create a user or mint a key and it lands here.")
        }
      </div>
    </section>`;
  },
};

function userRow(u) {
  return `<tr data-user="${esc(u.name)}">
    <td class="primary">${esc(u.name)}</td>
    <td>${esc(u.email || "—")}</td>
    <td class="cnum">${esc(budgetText(u.budget_usd))}</td>
    <td class="cnum">${esc(usd(u.spent_usd))}</td>
    <td class="cmeter">${meter(u.spent_usd, u.budget_usd)}</td>
    <td class="cnum">${int(u.live_keys)}</td>
    <td>${
      u.disabled
        ? `<span class="badge badge--danger"><span class="badge__dot"></span>disabled</span>`
        : `<span class="badge badge--success"><span class="badge__dot"></span>active</span>`
    }</td>
    <td class="cact"><span class="row-actions">
      <button class="btn btn--sm btn--ghost" type="button" data-action="budget"
              data-user="${esc(u.name)}" data-budget="${
    u.budget_usd === null || u.budget_usd === undefined ? "" : u.budget_usd
  }">Budget</button>
      <button class="btn btn--sm" type="button" data-action="mint"
              data-user="${esc(u.name)}">Mint key</button>
    </span></td>
  </tr>`;
}

function keyRow(k) {
  return `<tr>
    <td class="cmono" style="color:var(--accent)">${esc(k.prefix)}…</td>
    <td class="primary">${esc(k.user || "—")}</td>
    <td>${esc(k.label || "—")}</td>
    <td class="cnum">${esc(budgetText(k.budget_usd))}</td>
    <td class="cnum">${esc(usd(k.spent_usd))}</td>
    <td class="cmono">${esc(ago(k.last_used_at))}</td>
    <td>${
      k.revoked
        ? `<span class="badge badge--danger">revoked</span>`
        : `<span class="badge badge--success"><span class="badge__dot"></span>live</span>`
    }</td>
    <td class="cact">${
      k.revoked
        ? ""
        : `<span class="row-actions"><button class="btn btn--sm btn--danger" type="button"
             data-action="revoke" data-prefix="${esc(k.prefix)}">Revoke</button></span>`
    }</td>
  </tr>`;
}

/* ── actions ──────────────────────────────────────────────────────────────── */

function addUserModal() {
  openModal({
    title: "Add user",
    description:
      "A user owns virtual keys and, optionally, a lifetime spend cap.",
    submit: "Add user",
    fields: [
      { name: "name", label: "Name", placeholder: "alice", hint: "Unique. Used everywhere in the CLI." },
      { name: "email", label: "Email", type: "email", placeholder: "alice@example.com (optional)" },
      {
        name: "budget_usd",
        label: "Budget (USD)",
        type: "number",
        step: "0.01",
        min: "0",
        placeholder: "leave blank for unlimited",
        hint: "Enforced across every key this user holds.",
      },
    ],
    async onSubmit(values) {
      if (!values.name) throw new Error("name is required");
      await api("/users", {
        method: "POST",
        body: {
          name: values.name,
          email: values.email || null,
          budget_usd: values.budget_usd === "" ? null : values.budget_usd,
        },
      });
      toast("User added", `${values.name} can now be minted a key.`);
      refresh();
    },
  });
}

function budgetModal(user, current) {
  openModal({
    title: `Budget for ${user}`,
    description: "Blank removes the cap. Spend already recorded is not reset.",
    submit: "Save budget",
    fields: [
      {
        name: "budget_usd",
        label: "Budget (USD)",
        type: "number",
        step: "0.01",
        min: "0",
        value: current,
        placeholder: "blank = unlimited",
      },
    ],
    async onSubmit(values) {
      await api("/users/" + encodeURIComponent(user), {
        method: "PATCH",
        body: { budget_usd: values.budget_usd === "" ? null : values.budget_usd },
      });
      toast(
        "Budget updated",
        `${user} → ${values.budget_usd === "" ? "unlimited" : usd(values.budget_usd, 2)}`
      );
      refresh();
    },
  });
}

function mintModal(presetUser) {
  const names = state.users.map((u) => u.name);
  if (!names.length) {
    toast("No users", "Add a user before minting a key.", "danger");
    return;
  }
  const user = presetUser && names.includes(presetUser) ? presetUser : names[0];
  openModal({
    title: "Mint virtual key",
    description: "The plaintext is displayed once and never stored.",
    submit: "Mint key",
    fields: [
      {
        name: "user",
        label: "User",
        type: "select",
        value: user,
        options: names.map((n) => ({ value: n, label: n })),
      },
      { name: "label", label: "Label", placeholder: "laptop, ci, notebook… (optional)" },
      {
        name: "budget_usd",
        label: "Key budget (USD)",
        type: "number",
        step: "0.01",
        min: "0",
        placeholder: "blank = only the user's cap applies",
      },
    ],
    async onSubmit(values) {
      const result = await api("/keys", {
        method: "POST",
        body: {
          user: values.user,
          label: values.label || null,
          budget_usd: values.budget_usd === "" ? null : values.budget_usd,
        },
      });
      closeModal();
      openKeyModal(result);
      refresh();
    },
  });
}

function revokeModal(prefix) {
  openModal({
    title: "Revoke key",
    description: `<span class="mono">${esc(
      prefix
    )}…</span> stops working immediately. Recorded spend is kept.`,
    submit: "Revoke",
    fields: [],
    async onSubmit() {
      await api("/keys/" + encodeURIComponent(prefix) + "/revoke", { method: "POST" });
      toast("Key revoked", `${prefix}… will be refused from now on.`);
      refresh();
    },
  });
}

/* ── rendering + routing ──────────────────────────────────────────────────── */

function renderNav() {
  $("#nav").innerHTML = VIEWS.map(
    (v) => `<button class="nav-item" type="button" data-view="${v.id}"
      ${v.id === state.view ? 'aria-current="page"' : ""}>
      <svg class="nav-item__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor"
           stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">${
             ICONS[v.id]
           }</svg>
      <span>${v.label}</span>
      <span class="nav-item__count" data-count="${v.id}"></span>
    </button>`
  ).join("");
}

function renderCounts() {
  const o = state.overview;
  if (!o) return;
  const set = (id, value) => {
    const node = $(`[data-count="${id}"]`);
    if (node) node.textContent = value;
  };
  set("users", int(o.users));
  set("keys", int(o.live_keys));
  set("usage", int(o.requests_24h));
}

function renderChrome() {
  const view = VIEWS.find((v) => v.id === state.view) || VIEWS[0];
  $("#view-title").textContent = view.title;
  $("#view-sub").textContent = view.sub;
  document.title = `keygate · ${view.title}`;
  $("#nav")
    .querySelectorAll(".nav-item")
    .forEach((b) => {
      if (b.dataset.view === state.view) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
  const o = state.overview;
  if (o) {
    $("#brand-version").textContent = "v" + o.version;
    const ws = $("#meta-workspace");
    ws.textContent = o.workspace;
    ws.title = o.workspace;
    const up = $("#meta-upstream");
    up.textContent = o.upstream_base_url;
    up.title = o.upstream_base_url;
  }
}

/** Re-rendering blows away focus; put it back so filter boxes stay usable. */
function withFocus(render) {
  const active = document.activeElement;
  const id = active && active.id;
  const start = active && active.selectionStart;
  const end = active && active.selectionEnd;
  render();
  if (!id) return;
  const restored = document.getElementById(id);
  if (!restored) return;
  restored.focus();
  if (start !== null && start !== undefined && restored.setSelectionRange) {
    try {
      restored.setSelectionRange(start, end);
    } catch (err) {
      /* not a text input */
    }
  }
}

function setBusy(busy) {
  state.busy = busy;
  const pip = $("#live-pip");
  pip.classList.toggle("pip--busy", busy);
  pip.classList.toggle("pip--paused", !busy && !state.live);
  $("#live-text").textContent = busy ? "syncing" : state.live ? "live" : "paused";
}

let renderToken = 0;

async function refresh({ silent = false } = {}) {
  const view = state.view;
  const token = ++renderToken;
  setBusy(true);
  const target = $("#view");
  if (!silent && !target.dataset.view) {
    target.innerHTML = `<div class="loading">Loading…</div>`;
  }
  try {
    const data = await views[view]();
    if (token !== renderToken || state.view !== view) return;
    // The overview endpoint is the only source for the sidebar counts; keep
    // them warm on every view so the rail never goes stale.
    if (view !== "overview") {
      api("/overview")
        .then((o) => {
          state.overview = o;
          renderChrome();
          renderCounts();
        })
        .catch(() => {});
    }
    const renderer = views["render" + view[0].toUpperCase() + view.slice(1)];
    withFocus(() => {
      target.innerHTML = renderer(data);
      target.dataset.view = view;
    });
    renderChrome();
    renderCounts();
  } catch (err) {
    if (token !== renderToken) return;
    if (silent) return; // a failed background poll shouldn't blank the screen
    target.innerHTML = `<section class="card"><div class="card__body">${emptyState(
      "Could not load this view",
      esc(err.message || String(err)),
      "keygate serve"
    )}</div></section>`;
    fail(err);
  } finally {
    if (token === renderToken) setBusy(false);
  }
}

function go(view, { replace = false } = {}) {
  if (!views[view]) view = "overview";
  state.view = view;
  const hash = "#/" + view;
  if (location.hash !== hash) {
    if (replace) history.replaceState(null, "", hash);
    else location.hash = hash;
  }
  $("#view").dataset.view = "";
  renderChrome();
  refresh();
}

/* ── wiring ───────────────────────────────────────────────────────────────── */

function onViewClick(ev) {
  const button = ev.target.closest("[data-action]");
  if (!button) return;
  const { action, user, budget, prefix, view } = button.dataset;
  if (action === "goto") return go(view);
  if (action === "add-user") return addUserModal();
  if (action === "budget") return budgetModal(user, budget);
  if (action === "mint") return mintModal(user);
  if (action === "revoke") return revokeModal(prefix);
  if (action === "toggle-revoked") {
    state.filters.keyAll = !state.filters.keyAll;
    return refresh();
  }
  if (action === "toggle-detail") {
    state.filters.usageDetail = !state.filters.usageDetail;
    return refresh();
  }
}

function onViewInput(ev) {
  const id = ev.target.id;
  if (id === "user-search") {
    state.filters.userQuery = ev.target.value;
    withFocus(() => {
      $("#view").innerHTML = views.renderUsers({ users: state.users });
    });
  }
}

function onViewChange(ev) {
  const id = ev.target.id;
  const value = ev.target.value;
  if (id === "key-user") state.filters.keyUser = value;
  else if (id === "usage-since") state.filters.usageSince = value;
  else if (id === "usage-user") state.filters.usageUser = value;
  else if (id === "audit-limit") state.filters.auditLimit = value;
  else return;
  refresh();
}

function boot() {
  renderNav();
  $("#nav").addEventListener("click", (ev) => {
    const button = ev.target.closest(".nav-item");
    if (button) go(button.dataset.view);
  });
  $("#view").addEventListener("click", onViewClick);
  $("#view").addEventListener("input", onViewInput);
  $("#view").addEventListener("change", onViewChange);
  $("#btn-refresh").addEventListener("click", () => refresh());
  $("#live-pip").addEventListener("click", () => {
    state.live = !state.live;
    setBusy(false);
    toast(
      state.live ? "Auto-refresh on" : "Auto-refresh paused",
      state.live ? "Reloading every 8 seconds." : "Use Refresh to update manually."
    );
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && modalOpen()) {
      ev.preventDefault();
      closeModal();
      return;
    }
    if (modalOpen() || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const tag = (ev.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;
    const index = "12345".indexOf(ev.key);
    if (index >= 0 && VIEWS[index]) go(VIEWS[index].id);
    if (ev.key === "r") refresh();
  });

  window.addEventListener("hashchange", () => {
    const view = (location.hash || "").replace(/^#\/?/, "") || "overview";
    if (view !== state.view) go(view);
  });

  setInterval(() => {
    if (state.live && !modalOpen() && !document.hidden && !state.busy) {
      refresh({ silent: true });
    }
  }, 8000);

  go((location.hash || "").replace(/^#\/?/, "") || "overview", { replace: true });
}

boot();
