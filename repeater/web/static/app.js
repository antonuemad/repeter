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
    var submit = form.querySelector('button[type="submit"]');
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
    setupConfirm();
    setupUpload();
    setupTikTok();
  });
})();
