/**
 * Scroll-driven reveals for the landing page (GSAP + ScrollTrigger).
 *
 * This is progressive enhancement: the actual fade/slide transition still
 * lives in style.css's [data-reveal] / .is-visible rule, so GSAP is only
 * deciding *when* to flip that class. If the GSAP CDN fails to load,
 * everything below still resolves to visible content.
 */
(function () {
    'use strict';

    var hasGSAP = typeof window.gsap !== 'undefined' && typeof window.ScrollTrigger !== 'undefined';

    function revealNow(el) { el.classList.add('is-visible'); }

    if (!hasGSAP) {
        // CDN unreachable/offline: don't leave [data-reveal] content stuck
        // at opacity 0, just show everything immediately.
        document.querySelectorAll('[data-reveal]').forEach(revealNow);
        return;
    }

    gsap.registerPlugin(ScrollTrigger);

    document.querySelectorAll('[data-reveal]').forEach(function (el) {
        ScrollTrigger.create({
            trigger: el,
            start: 'top 88%',
            once: true,
            onEnter: function () { revealNow(el); }
        });
    });
})();
