// Compares each agent's published handler (data-sha on its card) with the handler_sha256 that the
// hearth's public roster reports for the code it runs. Built by hestia-agents/scripts/build_catalog.py.
// Roster values are shown with textContent only: an owner's label is that owner's text, not markup.
(function () {
  "use strict";
  var text = JSON.parse(document.getElementById("live-text").textContent);
  var fill = function (s, v) { return s.replace(/\{(\w+)\}/g, function (_, k) { return k in v ? String(v[k]) : ""; }); };
  var each = function (selector, fn) { Array.prototype.forEach.call(document.querySelectorAll(selector), fn); };
  var cards = Array.prototype.slice.call(document.querySelectorAll("section[data-agent][data-sha]"));
  var summary = document.getElementById("live-summary"), metric = document.getElementById("live-metric");
  var mark = function (slug, state, sha) {
    each('[data-live="' + slug + '"]', function (el) {
      el.setAttribute("data-state", state);
      el.textContent = el.getAttribute("data-kind") === "line"
        ? fill(text["live_card_" + state] || text["live_" + state], {sha: sha || ""})
        : text["live_" + state];
    });
  };
  each("button[data-copy]", function (button) {
    button.addEventListener("click", function () {
      var source = document.getElementById(button.getAttribute("data-copy"));
      if (!source || !navigator.clipboard) { return; }
      navigator.clipboard.writeText(source.textContent).then(function () {
        button.textContent = text.live_copied;
        button.classList.add("done");
        setTimeout(function () { button.textContent = text.live_copy; button.classList.remove("done"); }, 1600);
      }, function () {});
    });
  });
  var url = location.protocol === "file:" ? "https://hestia.modelmarket.dev/v1/hearth" : "/v1/hearth";
  fetch(url, {cache: "no-store"}).then(function (r) {
    if (!r.ok) { throw new Error("HTTP " + r.status); }
    return r.json();
  }).then(function (roster) {
    var rows = Array.isArray(roster.tenants) ? roster.tenants : [];
    var bySlug = {};
    rows.forEach(function (row) { if (row && typeof row.slug === "string") { bySlug[row.slug] = row; } });
    var count = {same: 0, differs: 0, absent: 0, nohash: 0};
    var known = {};
    cards.forEach(function (card) {
      var slug = card.getAttribute("data-agent"), row = bySlug[slug];
      known[slug] = true;
      var live = row && typeof row.handler_sha256 === "string" ? row.handler_sha256 : "";
      var state = !row ? "absent" : !live ? "nohash" : live === card.getAttribute("data-sha") ? "same" : "differs";
      count[state] += 1;
      mark(slug, state, live);
      each('[data-live-sha="' + slug + '"]', function (el) {
        el.textContent = live ? live.slice(0, 16) + "…" + live.slice(-8) : "—";
      });
      each('[data-live-eq="' + slug + '"]', function (el) { el.textContent = state === "same" ? "=" : "≠"; });
    });
    var all = count.same === cards.length;
    summary.textContent = fill(all ? text.live_summary_all : text.live_summary, {same: count.same,
      total: cards.length, differs: count.differs + count.nohash, absent: count.absent});
    summary.setAttribute("data-state", all ? "same" : count.differs + count.nohash ? "differs" : "absent");
    metric.textContent = count.same + "/" + cards.length;
    metric.className = all ? "v ok" : "v";
    var body = document.getElementById("others-body"), none = document.getElementById("others-none");
    var others = rows.filter(function (row) { return row && !known[row.slug]; });
    others.forEach(function (row) {
      var tr = document.createElement("tr");
      var owner = row.owner && row.owner.kind === "owner"
        ? text.owner_other + (row.owner.label ? " · " + String(row.owner.label) : "")
        : text.owner_operator;
      [String(row.slug || ""), String(row.capability_id || ""), owner,
       typeof row.handler_sha256 === "string" ? row.handler_sha256 : "—"].forEach(function (value, i) {
        var td = document.createElement("td");
        if (i === 1 || i === 3) { var code = document.createElement("code"); code.textContent = value; td.appendChild(code); }
        else { td.textContent = value; }
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    none.hidden = others.length > 0;
    document.getElementById("others-table").hidden = others.length === 0;
  }).catch(function (err) {
    cards.forEach(function (card) { mark(card.getAttribute("data-agent"), "error", ""); });
    summary.textContent = fill(text.live_failed, {error: String(err && err.message || err)});
    summary.setAttribute("data-state", "error");
    metric.textContent = "—";
  });
})();
