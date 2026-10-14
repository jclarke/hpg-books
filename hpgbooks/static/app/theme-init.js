// Applies the saved light/dark choice before first paint (external file: the CSP allows no inline script).
(function () {
  try {
    var saved = window.localStorage.getItem("hpb-theme");
    if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
  } catch (e) {
    /* storage blocked: follow the OS setting */
  }
})();
