/* Girder diff viewer (WS-08 WP 12.6) — dependency-free unified-diff renderer.
 *
 * Converts raw unified diff text into a two-column line-numbered table:
 *   - `+` lines get class diff-add (green), `-` lines diff-del (red),
 *     `@@` hunk headers diff-hunk, file headers diff-meta, context plain.
 * Usable two ways:
 *   - statically: window.GirderDiff.renderAll() swaps every
 *     <pre class="diff-src">{{ diff }}</pre> for the rendered table;
 *   - programmatically: GirderDiff.buildTable(GirderDiff.parseUnified(text), document).
 * Colors come from style.css custom properties (light/dark aware).
 * UMD-lite tail lets node's require() load it for unit tests.
 */
(function (global) {
  "use strict";

  var HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

  /* Parse unified diff text into rows:
   *   { type: "meta"|"hunk"|"add"|"del"|"ctx", oldNo: int|null, newNo: int|null, text: str }
   */
  function parseUnified(text) {
    var rows = [];
    if (!text) return rows;
    var lines = String(text).split("\n");
    // A trailing newline produces a spurious empty final element — drop it.
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    var oldNo = null;
    var newNo = null;
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var m = HUNK_RE.exec(line);
      if (m) {
        oldNo = parseInt(m[1], 10);
        newNo = parseInt(m[3], 10);
        rows.push({ type: "hunk", oldNo: null, newNo: null, text: line });
      } else if (line.indexOf("diff --git ") === 0 ||
                 line.indexOf("index ") === 0 ||
                 line.indexOf("--- ") === 0 ||
                 line.indexOf("+++ ") === 0 ||
                 line.indexOf("new file mode ") === 0 ||
                 line.indexOf("deleted file mode ") === 0 ||
                 line.indexOf("\\ No newline") === 0) {
        rows.push({ type: "meta", oldNo: null, newNo: null, text: line });
      } else if (line.charAt(0) === "+") {
        rows.push({ type: "add", oldNo: null, newNo: newNo, text: line.slice(1) });
        if (newNo !== null) newNo += 1;
      } else if (line.charAt(0) === "-") {
        rows.push({ type: "del", oldNo: oldNo, newNo: null, text: line.slice(1) });
        if (oldNo !== null) oldNo += 1;
      } else {
        if (line.charAt(0) === " ") line = line.slice(1);
        rows.push({ type: "ctx", oldNo: oldNo, newNo: newNo, text: line });
        if (oldNo !== null) oldNo += 1;
        if (newNo !== null) newNo += 1;
      }
    }
    return rows;
  }

  /* Build the <table class="diff-table"> for parsed rows. `doc` is the DOM
   * document (or any object with createElement/createTextNode), so tests can
   * supply a stub. Old/new line numbers get their own columns. */
  function buildTable(rows, doc) {
    var table = doc.createElement("table");
    table.className = "diff-table";
    var tbody = doc.createElement("tbody");
    table.appendChild(tbody);
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var tr = doc.createElement("tr");
      tr.className = "diff-" + r.type;
      var cOld = doc.createElement("td");
      cOld.className = "diff-lineno";
      cOld.textContent = r.oldNo === null ? "" : String(r.oldNo);
      var cNew = doc.createElement("td");
      cNew.className = "diff-lineno";
      cNew.textContent = r.newNo === null ? "" : String(r.newNo);
      var cCode = doc.createElement("td");
      cCode.className = "diff-code";
      cCode.textContent = r.text;
      tr.appendChild(cOld);
      tr.appendChild(cNew);
      tr.appendChild(cCode);
      tbody.appendChild(tr);
    }
    return table;
  }

  function render(text, doc) {
    return buildTable(parseUnified(text), doc);
  }

  /* Swap every <pre class="diff-src"> under `root` (default: document) for
   * its rendered table, keeping the pre as a hidden original. Safe to call
   * repeatedly — already-rendered blocks are skipped. */
  function renderAll(root) {
    root = root || document;
    var pres = root.querySelectorAll("pre.diff-src");
    for (var i = 0; i < pres.length; i++) {
      var pre = pres[i];
      if (pre.getAttribute("data-diff-rendered") === "1") continue;
      var table = render(pre.textContent, pre.ownerDocument || document);
      pre.parentNode.insertBefore(table, pre.nextSibling);
      pre.setAttribute("data-diff-rendered", "1");
      pre.hidden = true;
    }
  }

  var api = { parseUnified: parseUnified, buildTable: buildTable, render: render, renderAll: renderAll };
  global.GirderDiff = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : this);
