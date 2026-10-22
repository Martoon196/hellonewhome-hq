/* Hello New Home — shared behaviour: mobile nav, postcode routing to the local
   team, England/Wales content tabs, enquiry forms, SA calculator. */
(function () {
  "use strict";

  // Set formEndpoint to a form service URL (Formspree, Basin, your CRM webhook)
  // to post enquiries. Left empty, forms fall back to WhatsApp / email.
  var CONFIG = {
    formEndpoint: "/api/lead",
    hqWhatsapp: "447513552133",
    hqPhone: "01792 807 051",
    email: "lettings@hellonewhome.co.uk"
  };

  var AREAS = window.HNH_AREAS || [];
  var areasLoading = null;

  // The finder needs the areas list. /areas.js sets it inline; if that script failed
  // (blocked, cached badly) fetch the same rows from /api/areas before answering.
  function loadAreas(cb) {
    if (AREAS.length || !window.fetch) { cb(); return; }
    if (!areasLoading) {
      areasLoading = fetch("/api/areas", { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (list) { if (Array.isArray(list) && list.length) AREAS = list; }, function () { /* keep the empty list */ });
    }
    areasLoading.then(cb, cb);
  }
  var WALES = ["CF", "LD", "LL", "NP", "SA"];
  // The same lists as services/postcodes.py (see there): TD straddles the border, so TD15, Berwick-upon-Tweed,
  // is England and the rest of TD is Scotland.
  var SCOTLAND = ["AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW", "KY", "ML", "PA", "PH", "ZE"];
  var SCOTLAND_BORDER = ["TD"];           // mostly Scotland; TD15 is England
  var BORDER_ENGLAND = ["TD15"];          // Berwick-upon-Tweed
  var NI = ["BT"];

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function hasRealNumber(a) { return a && a.whatsapp && !/X/.test(a.whatsapp); }

  // Postcode → { outward, areaCode, region, area|null, covered }
  function locate(pc) {
    var clean = String(pc || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (clean.length < 2) return null;
    var outward = clean.length > 4 ? clean.slice(0, -3) : clean;
    if (!/^[A-Z]{1,2}\d[A-Z\d]?$/.test(outward)) return null;
    var m = outward.match(/^[A-Z]{1,2}/);
    var areaCode = m[0];
    var region = WALES.indexOf(areaCode) > -1 ? "wales" : SCOTLAND.indexOf(areaCode) > -1 ? "scotland"
      : SCOTLAND_BORDER.indexOf(areaCode) > -1 ? (BORDER_ENGLAND.indexOf(outward) > -1 ? "england" : "scotland")
      : NI.indexOf(areaCode) > -1 ? "northern-ireland" : "england";
    // Same two passes as services/postcodes.locate on the server: an area listing the exact district
    // (KT1) wins over one listing the whole postcode area (KT), whatever their order.
    var area = null;
    for (var i = 0; i < AREAS.length && !area; i++) {
      if ((AREAS[i].postcodes || []).indexOf(outward) > -1) area = AREAS[i];
    }
    for (var j = 0; j < AREAS.length && !area; j++) {
      if ((AREAS[j].postcodes || []).indexOf(areaCode) > -1) area = AREAS[j];
    }
    return { outward: outward, areaCode: areaCode, region: region, area: area, covered: !!area };
  }
  window.hnhLocate = locate;

  function areaBySlug(slug) {
    for (var i = 0; i < AREAS.length; i++) if (AREAS[i].slug === slug) return AREAS[i];
    return null;
  }

  function waLink(number, text) {
    return "https://wa.me/" + number + "?text=" + encodeURIComponent(text);
  }

  // ---- nav ----
  var toggle = document.getElementById("navToggle");
  var nav = document.getElementById("siteNav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  var year = document.getElementById("year");
  if (year) year.textContent = String(new Date().getFullYear());

  // ---- England / Wales tabs ----
  function setRegionTabs(region) {
    if (region !== "england" && region !== "wales") return;
    Array.prototype.forEach.call(document.querySelectorAll("[data-region-tabs]"), function (group) {
      Array.prototype.forEach.call(group.querySelectorAll("[data-region-tab]"), function (btn) {
        var on = btn.getAttribute("data-region-tab") === region;
        btn.classList.toggle("is-active", on);
        btn.setAttribute("aria-selected", on ? "true" : "false");
      });
      Array.prototype.forEach.call(group.querySelectorAll("[data-region-panel]"), function (panel) {
        panel.hidden = panel.getAttribute("data-region-panel") !== region;
      });
    });
    try { localStorage.setItem("hnh-region", region); } catch (e) { /* private mode */ }
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-region-tab]"), function (btn) {
    btn.addEventListener("click", function () { setRegionTabs(btn.getAttribute("data-region-tab")); });
  });
  (function initTabs() {
    var saved = null;
    try { saved = localStorage.getItem("hnh-region"); } catch (e) { /* ignore */ }
    var fromPage = document.body.getAttribute("data-region");
    setRegionTabs(fromPage || saved || "england");
  })();

  // ---- area finder (home + areas page) ----
  function renderFinder(result, out) {
    if (!result) { out.innerHTML = '<p class="finder-msg">That doesn\'t look like a UK postcode. Try the first half, like SA1 or DH1.</p>'; return; }
    var a = result.area;
    if (a) {
      var who = a.role === "hq" ? "our head office team" : esc(a.partner) + ", our Local Partner";
      var wa = hasRealNumber(a) ? a.whatsapp : CONFIG.hqWhatsapp;
      out.innerHTML =
        '<div class="finder-card"><span class="pill pill-dark">' + esc(result.outward) + ' · ' + (a.region === "wales" ? "Wales" : "England") + '</span>' +
        '<h3>Your local team: Hello New Home ' + esc(a.name) + '</h3>' +
        '<p>Looked after locally by ' + who + '. Contracts, compliance and rent collection run from head office. Same capped fee everywhere.</p>' +
        '<div class="cta-row"><a class="btn btn-primary" href="/areas/' + encodeURIComponent(a.slug) + '">Hello New Home ' + esc(a.name) + '</a>' +
        '<a class="btn btn-wa" href="' + waLink(wa, "Hi, I'd like to talk about my property in " + result.outward + ".") + '" target="_blank" rel="noopener">WhatsApp</a></div></div>';
    } else if (result.region === "scotland" || result.region === "northern-ireland") {
      out.innerHTML = '<div class="finder-card"><h3>Not yet in ' + (result.region === "scotland" ? "Scotland" : "Northern Ireland") + '</h3><p>Different law, different register. We\'ll open there with a Local Partner who knows it. <a href="/partners-programme">Could that be you?</a></p></div>';
    } else {
      out.innerHTML =
        '<div class="finder-card"><span class="pill">' + esc(result.outward) + ' · ' + (result.region === "wales" ? "Wales" : "England") + '</span>' +
        '<h3>No Local Partner in ' + esc(result.outward) + ' yet</h3>' +
        '<p>Head office can still take your property on: we manage remotely with vetted local trades, and open a Local Partner in your area as soon as one is ready. Or, if you know your patch, <a href="/partners-programme">open it yourself</a>.</p>' +
        '<div class="cta-row"><a class="btn btn-primary" href="/valuation">Free rent review</a>' +
        '<a class="btn btn-wa" href="' + waLink(CONFIG.hqWhatsapp, "Hi, I have a property in " + result.outward + ". Can you cover it?") + '" target="_blank" rel="noopener">Ask head office</a></div></div>';
    }
    setRegionTabs(result.region);
  }
  Array.prototype.forEach.call(document.querySelectorAll("form[data-finder]"), function (form) {
    var out = form.parentNode.querySelector("[data-finder-out]");
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      loadAreas(function () { renderFinder(locate(form.postcode.value), out); });
    });
  });

  // ---- enquiry forms ----
  function serialise(form) {
    var lines = [];
    var data = {};
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.type === "submit" || el.type === "button") return;
      if ((el.type === "radio" || el.type === "checkbox") && !el.checked) return;
      var label = form.querySelector('label[for="' + el.id + '"]');
      var key = el.dataset.label || (label ? label.textContent.trim() : el.name);
      if (el.value) { data[el.name] = el.value; lines.push(key + ": " + el.value); }
    });
    return { data: data, text: lines.join("\n") };
  }

  function routeForm(form, payload) {
    // Work out the area from a postcode field, an address field, or ?area= / hidden input.
    var pcField = form.querySelector('[name="postcode"]');
    var addrField = form.querySelector('[name="address"]');
    var slugField = form.querySelector('[name="area"]');
    var result = null;
    if (pcField && pcField.value) result = locate(pcField.value);
    if (!result && addrField && addrField.value) {
      var m = addrField.value.toUpperCase().match(/\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?\b/);
      if (m) result = locate(m[1] + (m[2] || ""));
    }
    var area = (result && result.area) || (slugField && areaBySlug(slugField.value)) || null;
    if (area) {
      payload.data.area = area.slug;
      payload.text += "\nArea: " + area.name + (area.role === "hq" ? " (head office)" : " (Local Partner: " + area.partner + ")");
    } else if (result) {
      payload.data.area = "unassigned-" + result.region;
      payload.text += "\nArea: not yet covered (" + result.outward + ", " + result.region + ")";
    }
    return area;
  }

  // served: the "area" object from the /api/lead response ({slug, name, partner, whatsapp}) when the
  // server routed the lead; its number wins over the browser-side lookup because it is the one HQ set.
  function showDone(form, payload, area, served) {
    var done = form.parentNode.querySelector(".form-done");
    if (!done) return;
    var subject = form.dataset.subject || "Website enquiry";
    var text = subject + "\n" + payload.text;
    var wa = done.querySelector("[data-wa]");
    var mail = done.querySelector("[data-mail]");
    var pre = done.querySelector("pre");
    var who = done.querySelector("[data-who]");
    var number = hasRealNumber(served) ? served.whatsapp : (hasRealNumber(area) ? area.whatsapp : CONFIG.hqWhatsapp);
    if (wa) wa.href = waLink(number, text);
    if (mail) mail.href = "mailto:" + CONFIG.email + "?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(payload.text);
    if (pre) pre.textContent = text;
    var target = area;
    if (served && served.slug) {
      var local = areaBySlug(served.slug);
      // A lead the server kept with head office (no partner yet, or a service the partner does not offer) names
      // head office as its partner; say so rather than calling head office the area's Local Partner.
      target = { name: served.name, partner: served.partner, role: /head office/i.test(served.partner || "") ? "hq" : (local ? local.role : "partner") };
    }
    if (who) who.textContent = target ? ("It goes to " + (target.role === "hq" ? "our head office team" : target.partner + ", your Local Partner in " + target.name) + ".") : "It goes to our head office team.";
    var copy = done.querySelector("[data-copy]");
    if (copy) {
      copy.onclick = function () {
        var ok = function () { copy.textContent = "Copied"; };
        var fail = function () {
          if (pre) { var r = document.createRange(); r.selectNodeContents(pre); var s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }
          copy.textContent = "Select and copy";
        };
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(ok, fail); else fail();
      };
    }
    form.hidden = true;
    done.classList.add("is-shown");
    done.setAttribute("tabindex", "-1");
    done.focus();
  }

  // The service an enquiry form asks for, as the server reads it (services.routing.lead_service): the form's
  // data-service, or the rent review's "I'm interested in" answer (a shared house makes management HMO management).
  var INTEREST_SERVICES = { "full management": "residential_management", "rent guarantee": "residential_management",
                            "hmo": "hmo_management", "short let": "shortlet_management" };
  function formService(form) {
    if (form.dataset.service) return form.dataset.service;
    var picked = form.querySelector('input[name="interest"]:checked');
    var service = picked ? (INTEREST_SERVICES[picked.value.toLowerCase()] || "") : "";
    var type = form.querySelector('[name="type"]');
    if (service === "residential_management" && type && /^hmo/i.test(type.value)) service = "hmo_management";
    return service;
  }

  Array.prototype.forEach.call(document.querySelectorAll("form[data-enquiry]"), function (form) {
    // Pre-fill area from ?area=slug and show who will pick it up: head office when the area's partner does not
    // offer the service the form asks for.
    var slugField = form.querySelector('input[type="hidden"][name="area"]');
    var note = form.querySelector("[data-area-note]");
    var qs = location.search.match(/[?&]area=([a-z0-9-]+)/);
    if (qs && slugField) {
      slugField.value = qs[1];
      var showTeam = function () {
        var a = areaBySlug(qs[1]);
        if (!a || !note) return;
        var service = formService(form);
        if (service && a.role !== "hq" && a.services && a.services[service] === false) {
          note.textContent = "This goes to our head office team: Hello New Home " + a.name + " does not offer it locally yet.";
        } else {
          note.textContent = "Your local team: Hello New Home " + a.name + " (" + (a.role === "hq" ? "head office" : a.partner) + ").";
        }
      };
      loadAreas(showTeam);
      form.addEventListener("change", showTeam);
    }
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (!form.checkValidity()) { form.reportValidity(); return; }
      var payload = serialise(form);
      payload.data._subject = form.dataset.subject || "Website enquiry";
      payload.data._page = location.pathname;
      var area = routeForm(form, payload);
      var btn = form.querySelector('[type="submit"]');
      if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
      var finish = function (reply) { showDone(form, payload, area, reply && reply.ok && reply.area ? reply.area : null); };
      var failed = function () { finish(null); };
      if (CONFIG.formEndpoint && window.fetch) {
        fetch(CONFIG.formEndpoint, { method: "POST", headers: { "Content-Type": "application/json", "Accept": "application/json" }, body: JSON.stringify(payload.data) })
          .then(function (r) { return r.json(); })
          .then(finish, failed);
      } else { finish(null); }
    });
  });

  // ---- SA quick calculator (illustrative) ----
  var calc = document.getElementById("saCalc");
  if (calc) {
    var out = document.getElementById("saCalcOut");
    var fmt = function (n) { return "£" + Math.round(n).toLocaleString("en-GB"); };
    var run = function () {
      var rent = parseFloat(calc.rent.value) || 0;
      var nightly = parseFloat(calc.nightly.value) || 0;
      var occ = (parseFloat(calc.occ.value) || 0) / 100;
      var nights = 365 * occ;
      var gross = nightly * nights;
      var platform = gross * 0.15;
      var bills = 12 * (parseFloat(calc.bills.value) || 0);
      var cleaning = (nights / 2.5) * 45;
      var net = gross - platform - bills - cleaning;
      var mgmt = net * 0.18;
      var saOwner = net - mgmt;
      var llMgmt = Math.min(rent * 12 * 0.12, 150 * 12);
      var llOwner = rent * 12 - llMgmt;
      out.innerHTML =
        '<div class="stats">' +
        '<div class="stat"><strong>' + fmt(llOwner / 12) + '</strong><span>per month as a long let, after our capped fee</span></div>' +
        '<div class="stat"><strong>' + fmt(saOwner / 12) + '</strong><span>per month as a short let, after platform fees, bills, cleaning and management</span></div>' +
        '<div class="stat"><strong>' + (saOwner > llOwner ? "+" : "") + fmt((saOwner - llOwner) / 12) + '</strong><span>difference per month (' + Math.round(nights) + ' nights let)</span></div>' +
        '</div>';
    };
    calc.addEventListener("input", run);
    calc.addEventListener("submit", function (e) { e.preventDefault(); run(); });
    run();
  }
})();
