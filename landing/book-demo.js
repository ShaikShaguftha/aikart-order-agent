// "Book a Demo" multi-step form: one question per screen, thin progress
// bar up top, Back on every step but the first. No backend exists yet, so
// the final submit just logs the collected answers to the console.
(function () {
    'use strict';

    var form = document.getElementById('demoForm');
    var steps = Array.prototype.slice.call(form.querySelectorAll('.demo-step'));
    var progressBar = document.getElementById('demoProgressBar');
    var progressWrap = document.getElementById('demoProgress');
    var totalSteps = steps.length - 1; // last step is the outcome screen, not part of progress
    var currentIndex = 0;

    function stepNumber(step) {
        return Number(step.getAttribute('data-step'));
    }

    function setProgress(index) {
        var percent = Math.min(100, Math.round((stepNumber(steps[index]) / totalSteps) * 100));
        progressBar.style.width = percent + '%';
        progressWrap.setAttribute('aria-valuenow', String(percent));
    }

    function focusFirstField(step) {
        var field = step.querySelector('input');
        if (field) field.focus();
    }

    function showStep(index) {
        steps.forEach(function (step, i) {
            step.classList.toggle('is-active', i === index);
        });
        currentIndex = index;
        setProgress(index);
        focusFirstField(steps[index]);
    }

    function isEmailValid(value) {
        return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
    }

    function validateStep1() {
        var email = document.getElementById('fieldEmail');
        var storeUrl = document.getElementById('fieldStoreUrl');
        var valid = true;

        if (!isEmailValid(email.value.trim())) {
            email.classList.add('is-touched');
            valid = false;
        } else {
            email.classList.remove('is-touched');
        }

        if (!storeUrl.value.trim()) {
            storeUrl.classList.add('is-touched');
            valid = false;
        } else {
            storeUrl.classList.remove('is-touched');
        }

        return valid;
    }

    // Radio/checkbox steps: keep that step's Next/Submit button disabled
    // until at least one option in the group is selected. Radios can only
    // ever add a selection, but checkboxes (the platform step) can also
    // remove the last one, so this re-checks the whole group on every
    // change rather than just enabling unconditionally.
    var radioGatedButtons = [];
    steps.forEach(function (step) {
        var initialInputs = step.querySelectorAll('input[type="radio"], input[type="checkbox"]');
        if (!initialInputs.length) return;

        var advanceBtn = step.querySelector('[data-next], [type="submit"]');
        if (!advanceBtn) return;

        radioGatedButtons.push(advanceBtn);

        // Re-queried live each call (not the static NodeList above) so it
        // still sees checkboxes added later by the "Other" -> Add flow.
        function refreshAdvanceState() {
            var inputs = step.querySelectorAll('input[type="radio"], input[type="checkbox"]');
            var anyChecked = Array.prototype.some.call(inputs, function (el) { return el.checked; });
            advanceBtn.disabled = !anyChecked;
        }

        initialInputs.forEach(function (input) {
            input.addEventListener('change', refreshAdvanceState);
        });

        step.refreshAdvanceState = refreshAdvanceState;
    });

    form.querySelectorAll('[data-next]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var step = steps[currentIndex];

            if (stepNumber(step) === 1 && !validateStep1()) return;

            if (currentIndex < steps.length - 1) showStep(currentIndex + 1);
        });
    });

    form.querySelectorAll('[data-back]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            if (currentIndex > 0) showStep(currentIndex - 1);
        });
    });

    // "Other" platform: reveal a text field + Add button (shown/hidden in
    // CSS via :has()). Add turns the typed name into a real option box
    // alongside Shopify/WooCommerce/etc, selects it, and closes the
    // "Other" panel again (it auto-hides once "Other" itself is no longer
    // the checked radio in that group).
    var platformOtherText = document.getElementById('platformOtherText');
    var platformOtherAdd = document.getElementById('platformOtherAdd');
    var platformOtherPanel = document.getElementById('platformOtherPanel');
    var platformOtherRadio = document.getElementById('platformOther');

    if (platformOtherAdd) {
        platformOtherAdd.addEventListener('click', function () {
            var value = platformOtherText.value.trim();
            if (!value) {
                platformOtherText.focus();
                return;
            }

            var otherLabel = platformOtherRadio.closest('.demo-option');
            var grid = otherLabel.parentElement;

            var newLabel = document.createElement('label');
            newLabel.className = 'demo-option demo-option-custom';

            var newCheckbox = document.createElement('input');
            newCheckbox.type = 'checkbox';
            newCheckbox.name = 'platform';
            newCheckbox.value = value;
            newCheckbox.checked = true;

            var newSpan = document.createElement('span');
            newSpan.textContent = value;

            newLabel.appendChild(newCheckbox);
            newLabel.appendChild(newSpan);
            grid.insertBefore(newLabel, otherLabel);

            platformOtherText.value = '';
            platformOtherPanel.classList.remove('is-confirmed');
            // "Other" itself is a separate checkbox now, not mutually
            // exclusive with the new one - uncheck it so its text panel
            // closes again (driven by the :has() CSS rule).
            platformOtherRadio.checked = false;

            // Setting .checked programmatically doesn't fire 'change', so
            // refresh the button state here directly.
            var step = otherLabel.closest('.demo-step');
            if (step.refreshAdvanceState) step.refreshAdvanceState();
        });
    }

    if (platformOtherText) {
        platformOtherText.addEventListener('input', function () {
            platformOtherPanel.classList.remove('is-confirmed');
        });
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();

        var data = new FormData(form);
        var answers = {
            email: (data.get('email') || '').trim(),
            storeUrl: (data.get('storeUrl') || '').trim(),
            platform: data.getAll('platform'), // multi-select now
            volume: data.get('volume') || '',
            painPoint: data.get('painPoint') || ''
        };

        // No backend endpoint exists yet - log the collected answers.
        console.log('Book a Demo submission:', answers);

        showStep(steps.length - 1);
    });

    var chatNowBtn = document.getElementById('chatNowBtn');
    if (chatNowBtn) {
        chatNowBtn.addEventListener('click', function () {
            // When this runs inside the index.html modal, its overlay
            // (z-index 1000) sits above the chat widget (z-index 999), so
            // the chat panel would open invisibly behind it unless the
            // modal closes first.
            var modalCloseBtn = document.getElementById('bookDemoCloseBtn');
            if (modalCloseBtn) modalCloseBtn.click();

            if (window.luintixChat) window.luintixChat.open();
        });
    }

    // Public hook so book-demo-modal.js can start fresh every time the
    // modal is opened, instead of resuming wherever it was last left.
    window.luintixBookDemo = {
        reset: function () {
            form.reset();
            form.querySelectorAll('.is-touched').forEach(function (el) {
                el.classList.remove('is-touched');
            });
            if (platformOtherPanel) platformOtherPanel.classList.remove('is-confirmed');
            form.querySelectorAll('.demo-option-custom').forEach(function (el) { el.remove(); });
            radioGatedButtons.forEach(function (btn) { btn.disabled = true; });
            showStep(0);
        }
    };

    showStep(0);
})();
