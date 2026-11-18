"use strict";
(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const store = {
    get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  let token = store.get("ipamd.token");
  let vrf = "global";
  let current = null;   // selected prefix id
  let tree = [];

  async function api(path, opt = {}) {
    const h = { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) };
    const r = await fetch(path, { ...opt, headers: { ...h, ...(opt.headers || {}) } });
    if (r.status === 204) return null;
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = body.detail;
      throw new Error(Array.isArray(d) ? d.map(x => x.msg).join("; ") : d || `HTTP ${r.status}`);
    }
    return body;
  }

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 2200);
  }

  // ---- dialog helper: fields [{name,label,value,hint,type,options}] ----
  function ask(title, okLabel, fields, submit) {
    const dlg = $("#dlg");
    $("#dlg-title").textContent = title;
    $("#dlg-ok").textContent = okLabel;
    $("#dlg-err").textContent = "";
    $("#dlg-body").innerHTML = fields.map(f => `<label for="f-${f.name}">${esc(f.label)} ${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ""}</label>` +
      (f.options ? `<select id="f-${f.name}" name="${f.name}">${f.options.map(o => `<option value="${esc(o[0])}"${o[0] === f.value ? " selected" : ""}>${esc(o[1])}</option>`).join("")}</select>`
        : `<input id="f-${f.name}" name="${f.name}" value="${esc(f.value ?? "")}" ${f.type ? `type="${f.type}"` : ""} autocomplete="off" spellcheck="false">`)).join("");
    const form = $("#dlg-form");
    form.onsubmit = async (e) => {
      if (e.submitter && e.submitter.value === "cancel") return;
      e.preventDefault();
      const data = Object.fromEntries(new FormData(form));
      try { await submit(data); dlg.close(); }
      catch (err) { $("#dlg-err").textContent = err.message; }
    };
    dlg.showModal();
    const first = $("#dlg-body input, #dlg-body select");
    if (first) first.focus();
  }

  // ---- tree ----
  async function loadTree() {
    tree = await api(`/api/prefixes?vrf=${encodeURIComponent(vrf)}`);
    $("#tree").innerHTML = tree.map(p => {
      const hot = p.utilization >= 90;
      return `<li style="padding-left:${p.depth * 14}px"><button data-id="${p.id}" ${p.id === current ? 'aria-current="true"' : ""}>
        <span class="cidr">${esc(p.cidr)}${p.status !== "active" ? `<span class="status">${esc(p.status)}</span>` : ""}</span>
        <span class="pct">${p.utilization}%</span>
        <span class="bar${hot ? " hot" : ""}"><span style="width:${Math.min(100, p.utilization)}%"></span></span>
        ${p.description ? `<span class="desc">${esc(p.description)}</span>` : ""}
      </button></li>`;
    }).join("") || `<li class="desc" style="padding:8px;color:var(--ink2)">No prefixes in this VRF yet.</li>`;
  }
  $("#tree").addEventListener("click", (e) => { const b = e.target.closest("button[data-id]"); if (b) open(Number(b.dataset.id)); });

  async function loadVrfs() {
    const vrfs = await api("/api/vrfs");
    $("#vrf").innerHTML = vrfs.map(v => `<option value="${esc(v.name)}"${v.name === vrf ? " selected" : ""}>${esc(v.name)}</option>`).join("") +
      `<option value="__new">+ new VRF…</option>`;
  }
  $("#vrf").addEventListener("change", async (e) => {
    if (e.target.value === "__new") {
      e.target.value = vrf;
      ask("New VRF", "Create", [{ name: "name", label: "Name", hint: "e.g. customer-a, mgmt" }, { name: "description", label: "Description" }],
        async (d) => { await api("/api/vrfs", { method: "POST", body: JSON.stringify(d) }); vrf = d.name; await loadVrfs(); await loadTree(); });
      return;
    }
    vrf = e.target.value; current = null; showEmpty(); loadTree();
  });

  // ---- prefix detail ----
  function showEmpty() {
    $("#detail").innerHTML = `<div class="empty"><h1>Your address space, in one place.</h1><p>Pick a prefix on the left, or add one.</p></div>`;
  }

  function parents(p) {
    // prefixes in the tree that contain this one, outermost first
    const toNum = (ip) => ip.includes(":") ? null : ip.split(".").reduce((n, o) => n * 256 + Number(o), 0);
    const [ip, len] = p.cidr.split("/");
    const n = toNum(ip);
    if (n == null) return [];
    return tree.filter(t => {
      if (t.id === p.id || t.cidr.includes(":")) return false;
      const [tip, tlen] = t.cidr.split("/");
      const size = 2 ** (32 - Number(tlen));
      const start = toNum(tip);
      return Number(tlen) < Number(len) && n >= start && n < start + size;
    }).sort((a, b) => a.depth - b.depth);
  }

  async function open(id) {
    current = id;
    document.querySelectorAll("#tree button").forEach(b => b.setAttribute("aria-current", String(Number(b.dataset.id) === id)));
    const p = await api(`/api/prefixes/${id}`);
    const [base, lenS] = p.cidr.split("/");
    const v4 = !p.cidr.includes(":");
    const len = Number(lenS);
    const kids = tree.filter(t => t.id !== p.id && t.depth === p.depth + 1 && parents(t).some(x => x.id === p.id));
    const leaf = !p.children;
    const crumbs = parents(p);
    $("#detail").innerHTML = `
      ${crumbs.length ? `<div class="crumbs">${crumbs.map(c => `<button data-open="${c.id}">${esc(c.cidr)}</button>`).join(" › ")} ›</div>` : ""}
      <div class="title"><h1>${esc(p.cidr)}</h1><span class="desc">${esc(p.description || "no description")}</span></div>
      <div class="facts">
        <span><b>${p.utilization}%</b> used</span>
        <span><b>${p.size.toLocaleString()}</b> addresses</span>
        ${p.children ? `<span><b>${p.children}</b> subnets</span>` : ""}
        <span><b>${p.addresses}</b> assigned</span>
        ${leaf && v4 ? `<span><b>${Math.max(0, p.size - (len < 31 ? 2 : 0) - p.addresses).toLocaleString()}</b> free</span>` : ""}
        <span>status <b>${esc(p.status)}</b></span>
        ${p.site ? `<span>site <b>${esc(p.site)}</b></span>` : ""}
        ${p.vlan_vid ? `<span>VLAN <b>${p.vlan_vid}</b> ${esc(p.vlan_name || "")}</span>` : ""}
        <span>VRF <b>${esc(p.vrf)}</b></span>
      </div>
      <div class="actions">
        <button class="primary" id="a-alloc">Carve a subnet</button>
        ${leaf ? `<button id="a-next">Assign next free IP</button><button id="a-ip">Assign a specific IP</button>` : ""}
        <button id="a-edit">Edit</button>
        <button class="danger" id="a-del">Delete</button>
      </div>
      ${v4 && leaf && len >= 22 ? mapCard(p, base, len) : ""}
      ${p.free.length && !leaf ? `<section class="card"><h2>Free space</h2><div class="free">${p.free.slice(0, 40).map(f => `<span>${esc(f)}</span>`).join("")}${p.free.length > 40 ? `<span>+${p.free.length - 40} more</span>` : ""}</div></section>` : ""}
      ${kids.length ? `<section class="card"><h2>Subnets</h2><ul class="children">${kids.map(k => `<li><button data-open="${k.id}"><span>${esc(k.cidr)} ${esc(k.description)}</span><span>${k.utilization}% used</span></button></li>`).join("")}</ul></section>` : ""}
      ${p.addresses_list.length ? `<section class="card"><h2>Addresses</h2><div class="scroll"><table>
        <thead><tr><th>IP</th><th>Hostname</th><th>MAC</th><th>Status</th><th>Description</th><th></th></tr></thead>
        <tbody>${runs(p.addresses_list).map(r => r.length === 1 ? row(r[0]) : rangeRow(r)).join("")}</tbody></table></div></section>` : ""}`;

    $("#a-alloc").onclick = () => ask(`Carve a subnet out of ${p.cidr}`, "Carve", [
      { name: "prefix_length", label: "Size", hint: "prefix length", value: String(Math.min(v4 ? 32 : 128, len + (v4 ? 2 : 16))) },
      { name: "description", label: "Description", hint: "e.g. CCTV, guest Wi-Fi" },
      { name: "status", label: "Status", value: "active", options: [["active", "active"], ["reserved", "reserved"], ["container", "container (will be split further)"]] },
    ], async (d) => {
      const n = await api(`/api/prefixes/${p.id}/allocate`, { method: "POST", body: JSON.stringify({ ...d, prefix_length: Number(d.prefix_length) }) });
      toast(`Allocated ${n.cidr}`); await loadTree(); open(n.id);
    });
    if (leaf) {
      $("#a-next").onclick = async () => {
        const n = await api(`/api/prefixes/${p.id}/next-ip`);
        addIp(p, n.ip);
      };
      $("#a-ip").onclick = () => addIp(p, "");
    }
    $("#a-edit").onclick = () => ask(`Edit ${p.cidr}`, "Save", [
      { name: "description", label: "Description", value: p.description },
      { name: "status", label: "Status", value: p.status, options: [["active", "active"], ["reserved", "reserved"], ["container", "container"], ["deprecated", "deprecated"]] },
    ], async (d) => { await api(`/api/prefixes/${p.id}`, { method: "PATCH", body: JSON.stringify(d) }); await loadTree(); open(p.id); });
    $("#a-del").onclick = async () => {
      if (!confirm(`Delete ${p.cidr}${p.children ? ` and keep its ${p.children} subnets as orphans` : ""}?`)) return;
      try { await api(`/api/prefixes/${p.id}${p.children ? "?force=true" : ""}`, { method: "DELETE" }); current = null; showEmpty(); loadTree(); toast(`Deleted ${p.cidr}`); }
      catch (e) { toast(e.message); }
    };
    $("#detail").onclick = async (e) => {
      const o = e.target.closest("[data-open]");
      if (o) return open(Number(o.dataset.open));
      const rel = e.target.closest("[data-del-ip]");
      if (rel) {
        try { await api(`/api/addresses/${rel.dataset.delIp}`, { method: "DELETE" }); toast("Released"); await loadTree(); open(p.id); }
        catch (err) { toast(err.message); }
        return;
      }
      const cell = e.target.closest(".map i[data-ip]");
      if (cell && !cell.classList.contains("edge") && !cell.dataset.status) addIp(p, cell.dataset.ip);
    };
  }

  // consecutive anonymous entries with the same status + description (a DHCP
  // pool, a reserved block) read better as one range than as 100 rows
  function runs(list) {
    const num = (ip) => ip.includes(":") ? null : ip.split(".").reduce((n, o) => n * 256 + Number(o), 0);
    const out = [];
    for (const a of list) {
      const last = out[out.length - 1];
      const prev = last && last[last.length - 1];
      const anon = !a.hostname && !a.mac;
      if (prev && anon && !prev.hostname && !prev.mac && prev.status === a.status && prev.description === a.description &&
          num(a.ip) != null && num(a.ip) === num(prev.ip) + 1) last.push(a);
      else out.push([a]);
    }
    return out;
  }
  const row = (a) => `<tr><td class="mono">${esc(a.ip)}</td><td>${esc(a.hostname)}</td><td class="mono">${esc(a.mac)}</td>
    <td><span class="st ${a.status}">${esc(a.status)}</span></td><td>${esc(a.description)}</td>
    <td><button class="ghost danger" data-del-ip="${a.id}" aria-label="Release ${esc(a.ip)}">Release</button></td></tr>`;
  const rangeRow = (r) => `<tr><td class="mono">${esc(r[0].ip)} – ${esc(r[r.length - 1].ip.split(".").pop())}</td><td colspan="2" class="hint">${r.length} addresses</td>
    <td><span class="st ${r[0].status}">${esc(r[0].status)}</span></td><td>${esc(r[0].description)}</td><td></td></tr>`;

  function mapCard(p, base, len) {
    const size = 2 ** (32 - len);
    const start = base.split(".").reduce((n, o) => n * 256 + Number(o), 0);
    const byIp = new Map(p.addresses_list.map(a => [a.ip, a]));
    const ipStr = (n) => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".");
    let cells = "";
    for (let i = 0; i < size; i++) {
      const ip = ipStr(start + i);
      const a = byIp.get(ip);
      const edge = len < 31 && (i === 0 || i === size - 1);
      const cls = edge ? "edge" : a ? (a.status === "active" ? "used" : a.status) : "";
      const title = edge ? `${ip} (${i === 0 ? "network" : "broadcast"})` : a ? `${ip}  ${a.hostname || ""} ${a.mac || ""}`.trim() : `${ip} free, click to assign`;
      cells += `<i class="${cls}" data-ip="${ip}"${a ? ` data-status="${a.status}"` : ""} title="${esc(title)}"></i>`;
    }
    const cols = size <= 64 ? 16 : size <= 256 ? 16 : 32;
    return `<section class="card"><h2>Address map</h2><div class="map" style="--cols:${cols}">${cells}</div>
      <div class="legend"><span><i style="background:var(--used)"></i>in use</span><span><i style="background:var(--reserved)"></i>reserved</span><span><i style="background:var(--dhcp)"></i>DHCP</span><span><i style="background:var(--free)"></i>free</span></div>
      <p class="mapnote">Each square is one address, left to right from ${esc(ipStr(start))}. Click a free one to assign it.</p></section>`;
  }

  function addIp(p, ip) {
    ask(`Assign an address in ${p.cidr}`, "Assign", [
      { name: "ip", label: "IP address", value: ip },
      { name: "hostname", label: "Hostname" },
      { name: "mac", label: "MAC", hint: "optional, any format" },
      { name: "status", label: "Status", value: "active", options: [["active", "active"], ["reserved", "reserved"], ["dhcp", "DHCP pool / lease"]] },
      { name: "description", label: "Description" },
    ], async (d) => {
      const a = await api("/api/addresses", { method: "POST", body: JSON.stringify({ ...d, vrf: p.vrf }) });
      toast(`Assigned ${a.ip}`); await loadTree(); open(p.id);
    });
  }

  $("#new-prefix").onclick = () => ask("Add a prefix", "Add", [
    { name: "cidr", label: "Prefix", hint: "e.g. 10.20.0.0/16 or 2c0f:f4c0::/32" },
    { name: "description", label: "Description" },
    { name: "status", label: "Status", value: "active", options: [["active", "active"], ["container", "container (will be split into subnets)"], ["reserved", "reserved"]] },
  ], async (d) => {
    const p = await api("/api/prefixes", { method: "POST", body: JSON.stringify({ ...d, vrf }) });
    toast(`Added ${p.cidr}`); await loadTree(); open(p.id);
  });

  // ---- search ----
  $("#search").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("#q").value.trim();
    if (!q) return;
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (r.prefixes.length === 1 && !r.addresses.length) {
      if (r.prefixes[0].vrf !== vrf) { vrf = r.prefixes[0].vrf; await loadVrfs(); await loadTree(); }
      return open(r.prefixes[0].id);
    }
    $("#detail").innerHTML = `<div class="title"><h1 style="font-family:var(--sans)">Results for “${esc(q)}”</h1></div>
      ${r.addresses.length ? `<section class="card" style="margin-top:16px"><h2>Addresses</h2><table><thead><tr><th>IP</th><th>Hostname</th><th>MAC</th><th>Subnet</th></tr></thead><tbody>
        ${r.addresses.map(a => `<tr><td class="mono">${esc(a.ip)}</td><td>${esc(a.hostname)}</td><td class="mono">${esc(a.mac)}</td><td class="mono">${esc(a.prefix || "")}</td></tr>`).join("")}</tbody></table></section>` : ""}
      ${r.prefixes.length ? `<section class="card"><h2>Prefixes</h2><ul class="children">${r.prefixes.map(p => `<li><button data-open="${p.id}"><span>${esc(p.cidr)} ${esc(p.description)}</span><span>${esc(p.vrf)}</span></button></li>`).join("")}</ul></section>` : ""}
      ${!r.prefixes.length && !r.addresses.length ? `<p>Nothing matches. If it's an IP address, it isn't inside any prefix ipamd knows about.</p>` : ""}`;
    $("#detail").onclick = (ev) => { const o = ev.target.closest("[data-open]"); if (o) open(Number(o.dataset.open)); };
  });

  // ---- health + token ----
  async function loadHealth() {
    const issues = await api("/api/health");
    const b = $("#health-btn");
    b.hidden = !issues.length;
    b.textContent = `${issues.length} thing${issues.length === 1 ? "" : "s"} to check`;
    b.onclick = () => {
      $("#detail").innerHTML = `<div class="title"><h1 style="font-family:var(--sans)">Things to check</h1></div>
        <section class="card" style="margin-top:16px"><ul class="issues">${issues.map(i => `<li class="${i.severity}">${esc(i.message)}${i.prefix_id ? ` <button class="ghost" data-open="${i.prefix_id}">open</button>` : ""}</li>`).join("")}</ul></section>`;
      $("#detail").onclick = (ev) => { const o = ev.target.closest("[data-open]"); if (o) open(Number(o.dataset.open)); };
    };
  }
  $("#token-btn").onclick = () => ask("Write access", "Save", [{ name: "token", label: "API token", hint: "the IPAMD_TOKEN the server was started with", value: token, type: "password" }],
    async (d) => { token = d.token; store.set("ipamd.token", token); toast(token ? "Token saved in this browser" : "Token cleared"); });

  (async () => {
    await loadVrfs();
    await loadTree();
    loadHealth();
    setInterval(loadHealth, 30000);
    if (tree.length) open(tree[0].id);
  })();
})();
