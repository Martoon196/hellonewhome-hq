/* Portal/admin helpers: copy buttons, confirm-on-submit, auto-slug. */
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-target]");
    if (!btn) return;
    var el = document.querySelector(btn.getAttribute("data-copy-target"));
    if (!el) return;
    var text = el.value !== undefined ? el.value : el.textContent;
    var done = function () { btn.classList.add("is-done"); btn.textContent = "Copied"; setTimeout(function () { btn.classList.remove("is-done"); btn.textContent = "Copy"; }, 1500); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { el.select && el.select(); });
    else if (el.select) el.select();
  });
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.dataset.confirm && !window.confirm(f.dataset.confirm)) e.preventDefault();
  });
  var name = document.getElementById("area_name"), slug = document.getElementById("area_slug");
  if (name && slug) {
    name.addEventListener("input", function () {
      if (slug.dataset.touched) return;
      slug.value = name.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    });
    slug.addEventListener("input", function () { slug.dataset.touched = "1"; });
  }
})();
