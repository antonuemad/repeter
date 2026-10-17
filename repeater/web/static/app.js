/* Repeater dashboard: small progressive enhancements. Every form also works without JavaScript;
   the server validates everything again. */
(function () {
  "use strict";

  function onReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  // Native details keeps navigation usable without JavaScript. On small screens the
  // disclosure starts closed; Escape returns keyboard focus to its summary.
  function setupNavigation() {
    var nav = document.querySelector("[data-navigation]");
    if (!nav || !window.matchMedia) { return; }
    var desktop = window.matchMedia("(min-width: 1000px)");
    function adapt() { nav.open = desktop.matches; }
    adapt();
    desktop.addEventListener("change", adapt);
    nav.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !desktop.matches && nav.open) {
        nav.open = false;
        nav.querySelector("summary").focus();
      }
    });
  }

  // Ask before destructive actions: <form data-confirm="Are you sure?">
  function setupConfirm() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
    });
  }

  // Upload: refuse files over the limit before sending them, and show progress text.
  function setupUpload() {
    document.querySelectorAll("form[data-upload]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        var input = form.querySelector('input[type="file"]');
        var max = parseInt(form.getAttribute("data-max-bytes"), 10);
        if (input && input.files && input.files[0] && max && input.files[0].size > max) {
          event.preventDefault();
          window.alert(form.getAttribute("data-too-large"));
          return;
        }
        var button = form.querySelector('button[type="submit"]');
        if (button) {
          button.disabled = true;
          button.textContent = form.getAttribute("data-uploading");
        }
      });
    });
  }

  // TikTok post settings (Content Sharing Guidelines).
  function setupTikTok() {
    var form = document.querySelector("form[data-tiktok-form]");
    if (!form) {
      return;
    }
    var enabled = form.querySelector("[data-rule-enabled]");
    var privacy = form.querySelector("[data-privacy]");
    var disclose = form.querySelector("[data-disclose]");
    var options = form.querySelector("[data-disclose-options]");
    var organic = form.querySelector("[data-brand-organic]");
    var branded = form.querySelector("[data-brand-content]");
    var consent = form.querySelector("[data-consent]");
    var musicText = form.querySelector('[data-consent-text="music"]');
    var brandedText = form.querySelector('[data-consent-text="branded"]');
    var labelPreview = form.querySelector("[data-label-preview]");
    var submit = form.querySelector("[data-submit]") || form.querySelector('button[type="submit"]');
    // A disabled button does not fire hover events in every browser, so the explanation hangs
    // on a wrapper around it (TikTok Content Sharing Guidelines: hovering the disabled
    // publish button must show this sentence).
    var submitWrap = form.querySelector("[data-submit-wrap]");
    var discloseMessage = form.getAttribute("data-disclose-message") || "";
    var discloseHelp = form.querySelector("#disclose-help");
    if (!privacy || !disclose || !organic || !branded || !consent) {
      return;
    }
    var privateOption = privacy.querySelector("option[data-private]");
    var privateMessage = privateOption ? privateOption.getAttribute("title") : "";
    var yourBrandHelp = organic.closest("label").nextElementSibling;
    var brandedHelp = branded.closest("label").nextElementSibling;

    function mode() {
      var checked = form.querySelector("[data-mode]:checked");
      return checked ? checked.value : "direct";
    }

    function update() {
      var isBranded = disclose.checked && branded.checked;
      var needsChoice = disclose.checked && !organic.checked && !branded.checked;
      if (options) {
        options.hidden = !disclose.checked;
      }
      if (privateOption) {
        privateOption.disabled = isBranded;
      }
      if (musicText && brandedText) {
        musicText.hidden = isBranded;
        brandedText.hidden = !isBranded;
      }
      if (labelPreview) {
        if (isBranded) {
          labelPreview.textContent = brandedHelp ? brandedHelp.textContent : "";
        } else if (disclose.checked && organic.checked) {
          labelPreview.textContent = yourBrandHelp ? yourBrandHelp.textContent : "";
        } else {
          labelPreview.textContent = "";
        }
      }
      consent.required = mode() === "direct" && (!enabled || enabled.checked);
      if (submit) {
        submit.disabled = needsChoice;
        if (needsChoice) {
          if (submitWrap && discloseMessage) {
            submitWrap.setAttribute("title", discloseMessage);
          }
          if (discloseHelp) {
            submit.setAttribute("aria-describedby", "disclose-help");
          }
        } else {
          if (submitWrap) {
            submitWrap.removeAttribute("title");
          }
          submit.removeAttribute("aria-describedby");
        }
      }
    }

    branded.addEventListener("change", function () {
      if (branded.checked && privacy.value === "SELF_ONLY") {
        branded.checked = false;
        window.alert(privateMessage);
      }
      update();
    });
    [disclose, organic, privacy, enabled].forEach(function (el) {
      if (el) {
        el.addEventListener("change", update);
      }
    });
    form.querySelectorAll("[data-mode]").forEach(function (el) {
      el.addEventListener("change", update);
    });
    update();
  }

  onReady(function () {
    if (window.location.pathname === "/rules" && window.location.hash === "#workspace") {
      window.location.replace("/settings/workspace");
      return;
    }
    var accessForm = document.querySelector("[data-access-open]");
    if (accessForm && window.location.hash) {
      var token = new URLSearchParams(window.location.hash.slice(1)).get("token");
      window.history.replaceState(null, "", window.location.pathname);
      if (token && /^[A-Za-z0-9_-]{43}$/.test(token)) {
        accessForm.querySelector("[data-access-token]").value = token;
        accessForm.requestSubmit();
      }
    }
    document.querySelectorAll("[data-copy-link]").forEach(function (button) {
      button.addEventListener("click", async function () {
        var input = document.getElementById(button.dataset.copyLink);
        input.focus();
        input.select();
        if (navigator.clipboard && window.isSecureContext) {
          try {
            await navigator.clipboard.writeText(input.value);
            document.querySelector("[data-copy-status]").textContent = button.dataset.copied;
          } catch (_) { /* The selected text remains available for manual copy. */ }
        }
      });
    });
    setupNavigation();
    // The guide remains fully readable without JS; collapse only its mobile index.
    var guideIndex = document.querySelector('[data-guide-index]');
    if (guideIndex && window.matchMedia) {
      var guideDesktop = window.matchMedia('(min-width: 1200px)');
      function adaptGuide() { guideIndex.open = guideDesktop.matches; }
      adaptGuide();
      guideDesktop.addEventListener('change', adaptGuide);
      guideIndex.querySelectorAll('a').forEach(function (link) {
        link.addEventListener('click', function () {
          if (!guideDesktop.matches) { guideIndex.open = false; }
        });
      });
      document.querySelectorAll('.guide-back-index').forEach(function (link) {
        link.addEventListener('click', function () { guideIndex.open = true; });
      });
      // Preserve the guide's original shared /guide#topic links using only known topics.
      if (!new URLSearchParams(window.location.search).has('topic') && window.location.hash) {
        var oldTopic;
        try { oldTopic = decodeURIComponent(window.location.hash.slice(1)); } catch (_) { oldTopic = ''; }
        guideIndex.querySelectorAll('[data-guide-topic]').forEach(function (link) {
          if (link.dataset.guideTopic === oldTopic) { window.location.replace(link.href); }
        });
      }
    }
    setupConfirm();
    setupUpload();
    setupTikTok();
  });
})();

// Optional page-name filtering. Native checkboxes remain usable without JavaScript.
document.querySelectorAll('[data-plan-targets]').forEach((form) => {
  const input = form.querySelector('[data-plan-filter]');
  if (!input) return;
  form.querySelector('[data-filter-control]').hidden = false;
  const rows = [...form.querySelectorAll('[data-plan-account]')];
  input.addEventListener('input', () => {
    const term = input.value.trim().toLocaleLowerCase();
    let visible = 0;
    rows.forEach((row) => {
      row.hidden = !row.textContent.toLocaleLowerCase().includes(term);
      if (!row.hidden) visible += 1;
    });
    form.querySelector('[data-plan-no-matches]').hidden = visible !== 0;
  });
});
