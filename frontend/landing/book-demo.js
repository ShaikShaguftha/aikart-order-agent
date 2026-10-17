// "Book a Demo" multi-step form: one question per screen, thin progress
// bar up top, Back on every step but the first. Submit shows a small glass
// toast with an animated tick and POSTs the answers to /api/book-demo
// (server.py saves them and emails support@aikart.co).
(function () {
    'use strict';

    var form = document.getElementById('demoForm');
    var steps = Array.prototype.slice.call(form.querySelectorAll('.demo-step'));
    var progressBar = document.getElementById('demoProgressBar');
    var progressWrap = document.getElementById('demoProgress');
    var totalSteps = steps.length;
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

    var SUCCESS_MSG = 'Your mail has been sent to Luintix, and our team will connect with you shortly.';
    var TOAST_MS = 3000;
    var PENDING_KEY = 'luintixPendingDemoRequests';

    var toast = document.createElement('div');
    toast.className = 'demo-toast';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    toast.innerHTML =
        '<div class="demo-toast-tick" aria-hidden="true">' +
            '<span class="demo-toast-ring"></span>' +
            '<span class="demo-toast-circle"></span>' +
            '<svg viewBox="0 0 64 64" fill="none"><path d="M21 33l7.5 7.5L44 25" stroke="currentColor" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
        '</div>' +
        '<p class="demo-toast-msg"></p>';
    var toastBackdrop = document.createElement('div');
    toastBackdrop.className = 'demo-toast-backdrop';
    document.body.appendChild(toastBackdrop);
    document.body.appendChild(toast);
    var toastMsg = toast.querySelector('.demo-toast-msg');
    var toastTimer;

    function hideToast() {
        clearTimeout(toastTimer);
        toast.classList.remove('is-visible');
        toastBackdrop.classList.remove('is-visible');
    }

    // Click anywhere on the blurred backdrop to dismiss early.
    toastBackdrop.addEventListener('click', hideToast);

    function showToast(message) {
        clearTimeout(toastTimer);
        toastMsg.textContent = message;
        // Drop and re-add is-visible (with a reflow between) so the tick
        // animation replays on every submit.
        toast.classList.remove('is-visible');
        void toast.offsetWidth;
        toast.classList.add('is-visible');
        toastBackdrop.classList.add('is-visible');
        toastTimer = setTimeout(hideToast, TOAST_MS);
    }

    // Submissions that couldn't reach /api/book-demo (server down, offline)
    // are kept in this browser and retried on the next page load, so the
    // visitor always gets the confirmation and the lead isn't dropped.
    function readPending() {
        try { return JSON.parse(localStorage.getItem(PENDING_KEY)) || []; } catch (e) { return []; }
    }

    function writePending(list) {
        try {
            if (list.length) localStorage.setItem(PENDING_KEY, JSON.stringify(list));
            else localStorage.removeItem(PENDING_KEY);
        } catch (e) { /* storage blocked - nothing more we can do */ }
    }

    function sendRequest(answers) {
        return fetch('/api/book-demo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(answers)
        }).then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
        });
    }

    function retryPending() {
        var pending = readPending();
        if (!pending.length) return;
        writePending([]);
        pending.forEach(function (answers) {
            sendRequest(answers).catch(function () {
                writePending(readPending().concat([answers]));
            });
        });
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();

        var data = new FormData(form);
        var answers = {
            email: (data.get('email') || '').trim(),
            storeUrl: (data.get('storeUrl') || '').trim(),
            platform: data.getAll('platform').filter(function (p) { return p !== 'Other'; }),
            volume: data.get('volume') || '',
            painPoint: data.get('painPoint') || ''
        };

        sendRequest(answers).catch(function (err) {
            console.error('Book a Demo submit failed, will retry on next visit:', err);
            writePending(readPending().concat([answers]));
        });

        // Close the modal (index.html) or reset the standalone page
        // (book-demo.html), then confirm with the toast on top.
        var modalCloseBtn = document.getElementById('bookDemoCloseBtn');
        if (modalCloseBtn) modalCloseBtn.click();
        window.luintixBookDemo.reset();
        showToast(SUCCESS_MSG);
    });

    retryPending();

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
