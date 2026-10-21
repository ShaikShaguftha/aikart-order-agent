// "Book a Demo" as an in-page modal on index.html - no page navigation.
// The multi-step form logic itself lives in book-demo.js (shared with the
// standalone book-demo.html fallback); this file only opens/closes it.
(function () {
    'use strict';

    var overlay = document.getElementById('bookDemoOverlay');
    if (!overlay) return;

    var closeBtn = document.getElementById('bookDemoCloseBtn');
    var openBtns = document.querySelectorAll('[data-open-book-demo]');

    function openModal() {
        overlay.classList.add('is-open');
        document.body.classList.add('bdm-lock');
        if (window.luintixBookDemo) window.luintixBookDemo.reset();
    }

    function closeModal() {
        overlay.classList.remove('is-open');
        document.body.classList.remove('bdm-lock');
    }

    openBtns.forEach(function (btn) {
        btn.addEventListener('click', openModal);
    });

    if (closeBtn) closeBtn.addEventListener('click', closeModal);

    // Click the dark backdrop itself (not the card) to close.
    overlay.addEventListener('mousedown', function (e) {
        if (e.target === overlay) closeModal();
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && overlay.classList.contains('is-open')) closeModal();
    });
})();
