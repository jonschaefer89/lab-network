// Paths view: shortest paths between any two people (or from the lab) under a stated traversal rule.

import * as api from "../api.js";
import { filterParams, go, state } from "../state.js";
import { append, chip, clear, degreeText, extLink, h, noteMark, ordinal } from "../ui.js";
import { personPicker } from "./forms.js";
import { routeList } from "./profile.js";

export class PathsView {
  constructor(el, app) {
    this.el = el;
    this.app = app;
    this.built = false;
  }

  build() {
    this.fromPick = personPicker(this.app, { value: state.from || "lab", allowLab: true, placeholder: "The lab or a person" });
    this.toPick = personPicker(this.app, { value: state.to, placeholder: "Person to reach",
      onOutside: (name) => this.lookup(name) });
    this.outsideBox = h("input", { type: "text", class: "outside-name", placeholder: "Name of someone not in the network", "aria-label": "Name to look up" });
    const lookUp = () => { if (this.outsideBox.value.trim().length >= 3) this.lookup(this.outsideBox.value.trim()); };
    this.outsideBox.addEventListener("keydown", (e) => { if (e.key === "Enter") lookUp(); });
    this.mode = h("select", { "aria-label": "Traversal rule" },
      h("option", { value: "any" }, "Any direction (default)"),
      h("option", { value: "directed" }, "Recorded direction only"));
    this.result = h("div", { "aria-live": "polite" });
    const find = h("button", { type: "button", class: "btn btn-primary", onclick: () => {
      if (!this.toPick.value) { this.toPick.input.focus(); return; }
      const again = state.view === "paths" && state.to === this.toPick.value && state.from === (this.fromPick.value || "lab") &&
        state.mode === this.mode.value;
      this.outsideFor = undefined;
      go({ view: "paths", from: this.fromPick.value || "lab", to: this.toPick.value, mode: this.mode.value });
      if (again) this.show();         // the address is unchanged, so nothing else redraws the result
    } }, "Find shortest paths");
    this.el.append(h("div", { class: "page-inner" },
      h("h1", { class: "page-title" }, "Paths"),
      h("p", { class: "page-lede" }, "Find the shortest chains of recorded relationships between two people.", noteMark(
        h("span", { class: "note-line" }, "Length counts relationship steps only; confidence and strength are not used " +
          "as distance. Every equally short path is listed, longer ones are not."),
        h("span", { class: "note-line" }, "Relationship filters from the map apply here too. Change them with the " +
          "Relationships menu on the map."))),
      h("div", { class: "toolbar" },
        h("label", { class: "check" }, "From ", this.fromPick),
        h("label", { class: "check" }, "To ", this.toPick),
        h("label", { class: "check" }, "Rule ", this.mode), find),
      h("div", { class: "toolbar" },
        h("label", { class: "check" }, "Not in the network? ", this.outsideBox),
        h("button", { type: "button", class: "btn", onclick: lookUp }, "Look up in OpenAlex"),
        noteMark(h("span", { class: "note-line" }, "Lab Network searches OpenAlex for their author record, reads their " +
          "papers, and lists the people in your network who are authors on them."),
        h("span", { class: "note-line" }, "Nothing is saved. Each lookup uses about $0.001 of the daily OpenAlex allowance."))),
      this.result));
    this.built = true;
  }

  async show() {
    if (!this.built) this.build();
    this.mode.value = state.mode;
    if (state.from) {
      this.fromPick.value = state.from;
      this.fromPick.input.value = state.from === "lab" ? "The lab (any of its members)" : this.app.nameOf(state.from);
    }
    if (state.to) {
      this.toPick.value = state.to;
      this.toPick.input.value = this.app.nameOf(state.to);
    }
    // A lookup of someone outside the network stays on screen while people's pages open and close over it.
    if (this.outsideFor !== undefined && this.outsideFor === state.to) return;
    this.outsideFor = undefined;
    clear(this.result);
    if (!state.to) {
      this.result.append(h("p", { class: "dim" }, "Choose a person to reach.",
        noteMark("Starting from the lab finds the nearest lab member.")));
      return;
    }
    this.result.append(h("p", { class: "dim" }, "Finding paths…"));
    let r;
    try {
      r = await api.get("/api/path", { from: state.from || "lab", to: state.to, mode: state.mode, ...filterParams() });
    } catch (err) {
      clear(this.result).append(h("p", { class: "notice notice-error" }, err.message));
      return;
    }
    clear(this.result);
    const fromName = r.from === "lab" ? "the lab" : this.app.nameOf(r.from);
    const toName = this.app.nameOf(r.to);
    const panel = h("div", { class: "panel" });
    const rule = noteMark(r.status === "no-path" && r.coverageNote ? h("span", { class: "note-line" }, r.coverageNote) : null,
      h("span", { class: "note-line" }, r.rule));
    if (r.status === "same-person") {
      panel.append(h("h2", null, "Same person", rule), h("p", null, `${toName} is the starting point: zero steps.`));
    } else if (r.status === "no-path") {
      append(panel, [h("h2", null, "No recorded path", rule),
        h("p", null, `No chain of included relationship records leads from ${fromName} to ${toName} under this rule.`),
        state.mode === "directed" ? h("p", null, h("button", { type: "button", class: "btn btn-small",
          onclick: () => go({ mode: "any" }) }, "Try any direction")) : null]);
    } else {
      panel.append(h("h2", null, `${r.distance} step${r.distance === 1 ? "" : "s"} from ${fromName} to ${toName}`, rule),
        h("p", null, r.pathCount === 1 ? "One shortest path." :
          `${r.pathCount} equally short paths${r.truncated ? `; the first ${r.paths.length} are shown` : ""}.`));
      for (const p of r.paths) panel.append(routeList(p, this.app));
    }
    this.result.append(panel);
  }

  // ------------------------------------------------------------------ someone outside the network

  /** OpenAlex author records for a name; choosing one lists who in the network has published with them. */
  async lookup(name) {
    if (!this.built) this.build();
    this.outsideFor = state.to;
    this.outsideBox.value = name;
    clear(this.result).append(h("p", { class: "dim" }, `Looking up “${name}” in OpenAlex…`));
    let r;
    try {
      r = await api.get("/api/outside", { name });
    } catch (err) {
      clear(this.result).append(h("p", { class: "notice notice-error" }, err.message));
      return;
    }
    clear(this.result);
    const panel = h("div", { class: "panel" });
    this.result.append(panel);
    if (r.skipped) {
      panel.append(h("h2", null, "Not looked up"), h("p", null, r.note));
      return;
    }
    if (!r.candidates.length) {
      panel.append(h("h2", null, `No OpenAlex author named “${name}”`),
        h("p", null, "Check the spelling, or try the name as it appears on their papers."));
      return;
    }
    panel.append(h("h2", null, `Who is “${name}”?`, noteMark("OpenAlex can hold several people with the same name, and " +
        "can split one person across several records. Choose the record whose institutions and topics fit.")),
      h("p", null, `OpenAlex has ${r.candidates.length === 1 ? "one author record" : `${r.candidates.length} author records`} for this name.`));
    const ul = h("ul", { class: "outside-cands" });
    for (const c of r.candidates) {
      const where = c.lastKnownInstitutions.join("; ") || (c.affiliationHistory[0] || {}).institution || "Institution not recorded";
      ul.append(h("li", { class: "outside-cand" },
        h("div", null, h("strong", null, c.name), " · ", where),
        h("div", { class: "dim" }, `${c.worksCount} works`, c.topics.length ? ` · ${c.topics.slice(0, 3).join("; ")}` : "",
          " · ", extLink(c.url, "OpenAlex record")),
        c.person
          ? h("p", null, "Already in the network as ", h("button", { type: "button", class: "linklike", onclick: () => go({ sel: c.person }) },
            this.app.nameOf(c.person)), ". ", h("button", { type: "button", class: "btn btn-small",
            onclick: () => go({ view: "paths", from: "lab", to: c.person }) }, "Find shortest paths"))
          : h("button", { type: "button", class: "btn btn-small", onclick: () => this.connect(c) }, "This is the person")));
    }
    panel.append(ul);
  }

  async connect(cand) {
    this.outsideAid = cand.openalexId;
    clear(this.result).append(h("p", { class: "dim" }, `Reading ${cand.name}'s papers in OpenAlex…`));
    let r;
    try {
      r = await api.get(`/api/outside/${encodeURIComponent(cand.openalexId)}`, filterParams());
    } catch (err) {
      clear(this.result).append(h("p", { class: "notice notice-error" }, err.message));
      return;
    }
    if (this.outsideAid !== cand.openalexId) return;
    clear(this.result);
    const panel = h("div", { class: "panel" });
    this.result.append(panel);
    const name = r.author.name || cand.name;
    if (r.person) {
      panel.append(h("h2", null, `${name} is in the network`),
        h("p", null, h("button", { type: "button", class: "btn btn-small", onclick: () => go({ view: "paths", from: "lab", to: r.person }) },
          `Find shortest paths to ${this.app.nameOf(r.person)}`)));
      return;
    }
    const rule = noteMark(h("span", { class: "note-line" }, `Read ${r.worksRead} of their papers. ${r.rule}`),
      h("span", { class: "note-line" }, "A shared paper links them to a coauthor in the network. It does not mean they know each other well."),
      h("span", { class: "note-line" }, "People in the network count only when the research library has linked their OpenAlex record."));
    if (!r.bridges.length) {
      panel.append(h("h2", null, `No one in the network has published with ${name}`, rule),
        h("p", null, "They may still be connected through someone further out, through grants, or through relationships " +
          "papers do not show."));
      return;
    }
    const closest = r.bridges[0];
    panel.append(h("h2", null, r.degree === null ? `${name}: no recorded route to the lab` :
        `${name} would be ${ordinal(r.degree)} degree`, rule),
      h("p", null, `${r.bridges.length} ${r.bridges.length === 1 ? "person" : "people"} in the network ` +
        `${r.bridges.length === 1 ? "has" : "have"} published with them. The closest to the lab ${r.bridges.length === 1 ? "is" : "are listed"} first.`));
    const routeBox = h("div");
    panel.append(routeBox);
    const ul = h("ul", { class: "outside-bridges" });
    for (const b of r.bridges) {
      const w = b.works[0];
      ul.append(h("li", { class: "outside-bridge" },
        h("div", null,
          h("button", { type: "button", class: "linklike", onclick: () => go({ sel: b.personId }) }, b.name), " ",
          chip(degreeText(b.degree), b.degree === 0 ? "chip-strong" : ""),
          b.shown ? null : chip("Outside the map filters", "chip-outline")),
        h("div", { class: "dim" }, [b.role, b.institution].filter(Boolean).join(", ") || "Position not recorded"),
        h("div", null, `${b.shared} shared ${b.shared === 1 ? "paper" : "papers"}`, w ? [", most recently ",
          w.url ? extLink(w.url, w.title) : w.title, w.year ? ` (${w.year})` : ""] : ""),
        h("button", { type: "button", class: "btn btn-small btn-quiet", onclick: () => this.route(routeBox, b, name, cand) },
          "Show route through them")));
    }
    panel.append(h("h3", null, "Coauthors in the network"), ul);
    this.route(routeBox, closest, name, cand);
  }

  /** The route from the lab to a coauthor, then the shared paper as the last step. */
  async route(box, bridge, name, cand) {
    clear(box).append(h("p", { class: "dim" }, "Finding the route…"));
    let r;
    try {
      r = await api.get("/api/path", { from: "lab", to: bridge.personId, mode: state.mode || "any", ...filterParams() });
    } catch (err) {
      clear(box).append(h("p", { class: "notice notice-error" }, err.message));
      return;
    }
    clear(box);
    const w = bridge.works[0];
    const last = () => [
      h("li", { class: "leg leg-research" }, h("p", null, `Coauthors on ${bridge.shared} ${bridge.shared === 1 ? "paper" : "papers"}`,
        w ? [", most recently ", w.url ? extLink(w.url, w.title) : w.title, w.year ? ` (${w.year})` : ""] : "", " (OpenAlex)")),
      h("li", { class: "stop" }, extLink(cand.url, name), h("span", { class: "dim" }, " · not in the network"))];
    box.append(h("h3", null, `Route through ${bridge.name}`));
    if (r.status === "same-person") {
      const ol = h("ol", { class: "route" }, h("li", { class: "stop lab" },
        h("button", { type: "button", class: "linklike", onclick: () => go({ sel: bridge.personId }) }, bridge.name)));
      append(ol, last());
      box.append(ol);
      return;
    }
    if (r.status === "no-path" || !r.paths.length) {
      box.append(h("p", null, `No recorded route from the lab to ${bridge.name} under the current relationship filters.`));
      return;
    }
    const ol = routeList(r.paths[0], this.app);
    append(ol, last());
    box.append(ol);
    if (r.pathCount > 1) box.append(h("p", { class: "dim" }, `One of ${r.pathCount} equally short routes to ${bridge.name}. `,
      h("button", { type: "button", class: "linklike", onclick: () => go({ view: "paths", from: "lab", to: bridge.personId }) }, "See them all")));
  }
}
