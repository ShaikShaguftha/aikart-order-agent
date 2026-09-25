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

    // Radio-group steps: keep that step's Next/Submit button disabled
    // until one option in the group is selected.
    steps.forEach(function (step) {
        var radios = step.querySelectorAll('input[type="radio"]');
        if (!radios.length) return;

        var advanceBtn = step.querySelector('[data-next], [type="submit"]');
        radios.forEach(function (radio) {
            radio.addEventListener('change', function () {
                if (advanceBtn) advanceBtn.disabled = false;
            });
        });
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

    form.addEventListener('submit', function (e) {
        e.preventDefault();

        var data = new FormData(form);
        var answers = {
            email: (data.get('email') || '').trim(),
            storeUrl: (data.get('storeUrl') || '').trim(),
            platform: data.get('platform') || '',
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
            if (window.luintixChat) window.luintixChat.open();
        });
    }

    showStep(0);
})();
