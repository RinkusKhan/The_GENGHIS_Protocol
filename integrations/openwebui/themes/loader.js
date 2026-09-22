// GENGHIS theme loader for Open WebUI. Overlaid onto /static/loader.js (Open WebUI ships that file EMPTY and loads
// it on every page — its intended extension hook).
//  1. Applies the chosen theme (localStorage.genghisTheme) as data-genghis-theme on <html>; custom.css keys on it.
//  2. Adds a "GENGHIS" group to Open WebUI's own Theme dropdown (Settings → General → Theme) so a theme is picked
//     where every user already looks. Choosing one also puts Open WebUI on Dark underneath (our accents key on .dark);
//     choosing a stock entry clears ours. The gallery at /static/genghis-themes.html is a preview of the same set.
//  3. ?genghis_theme=<name> in the URL applies once (a bookmark per theme); "none" clears.
(function () {
  var THEMES = [["khan", "Khan"], ["blackwell", "Blackwell"], ["rizen", "Rizen' Up"], ["arc", "Arc Light"],
                ["steppe", "Steppe Night"], ["overclock", "Overclock"], ["tundra", "Tundra"], ["foundry", "Foundry"], ["ordu", "Ordu"]];
  var DEFAULT_THEME = "ordu";                 // the install default (Michael's pick, 2026-09-15); "none" = stock look
  function apply(name) {
    try {
      if (name) { localStorage.setItem("genghisTheme", name); document.documentElement.setAttribute("data-genghis-theme", name); }
      else { localStorage.setItem("genghisTheme", "none"); document.documentElement.removeAttribute("data-genghis-theme"); }
    } catch (e) {}
  }
  // "" = the user chose the stock look (stored as "none"); null = never chose -> the default
  function current() {
    try {
      var v = localStorage.getItem("genghisTheme");
      if (v === null) return null;
      return v === "none" ? "" : v;
    } catch (e) { return ""; }
  }
  try {
    var q = new URLSearchParams(location.search).get("genghis_theme");
    if (q) apply(q === "none" ? "" : q);
  } catch (e) {}
  var cur = current();
  if (cur === null) {
    // First visit in this browser: the default theme, and Open WebUI on Dark underneath (our palettes are dark).
    // Only when the user has not picked an Open WebUI theme either -- we never override a choice.
    try {
      var ow = localStorage.getItem("theme");            // Open WebUI writes "system" itself before we run
      if (!ow || ow === "system") { localStorage.setItem("theme", "dark"); document.documentElement.classList.add("dark"); }
    } catch (e) {}
    apply(DEFAULT_THEME);
  } else {
    apply(cur);
  }

  // --- extend Open WebUI's Theme <select> ---
  function isThemeSelect(sel) {
    if (!sel || sel.tagName !== "SELECT" || sel.dataset.genghis) return false;
    var vals = Array.prototype.map.call(sel.options, function (o) { return o.value; });
    return vals.indexOf("oled-dark") >= 0 && vals.indexOf("system") >= 0;
  }
  function extend(sel) {
    sel.dataset.genghis = "1";
    var g = document.createElement("optgroup"); g.label = "GENGHIS";
    THEMES.forEach(function (t) { var o = document.createElement("option"); o.value = "genghis-" + t[0]; o.textContent = "✦ " + t[1]; g.appendChild(o); });
    sel.appendChild(g);
    var cur = current();
    if (cur) sel.value = "genghis-" + cur;                   // show what is actually on (the default included)
    var relaying = false;
    sel.addEventListener("change", function (ev) {
      if (relaying) return;                                  // the synthetic "dark" change below is not a user choice
      var v = sel.value;
      if (v.indexOf("genghis-") === 0) {
        ev.stopImmediatePropagation();                       // Open WebUI must not see an unknown theme name
        apply(v.slice(8));
        // put Open WebUI on Dark underneath, through its own handler, then show our entry again
        relaying = true;
        sel.value = "dark"; sel.dispatchEvent(new Event("change", { bubbles: true }));
        relaying = false;
        setTimeout(function () { sel.value = v; }, 0);
      } else {
        apply("");                                           // a stock theme: ours steps aside
      }
    }, true);
  }
  function scan(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll("select").forEach(function (s) { if (isThemeSelect(s)) extend(s); });
  }
  var mo = new MutationObserver(function (muts) { muts.forEach(function (m) { m.addedNodes.forEach(scan); }); });
  function start() { scan(document.body); mo.observe(document.body, { childList: true, subtree: true }); }
  if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
})();
