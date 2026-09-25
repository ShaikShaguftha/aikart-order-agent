// Footer year
document.getElementById('year').textContent = new Date().getFullYear();

// "How Luintix Works" accordion - only one item open at a time; opening
// one closes whichever other item was open, and cross-fades the phone
// video to that item's own clip (data-video on the trigger button).
const demoListTriggers = document.querySelectorAll('.demo-list-trigger');

/* ---------- Text panel: measured-height expand/collapse ---------- */
// max-height is set inline to the panel's real content height (not a
// guessed cap) so the transition stays smooth regardless of how long a
// given description is.

const OPEN_DELAY = 100; // ms - new panel starts opening slightly after the old one begins closing
let openTimer = null;

function closeDemoPanel(item) {
    const trigger = item.querySelector('.demo-list-trigger');
    const panel = item.querySelector('.demo-list-panel');

    trigger.setAttribute('aria-expanded', 'false');
    item.classList.remove('is-open');
    panel.style.opacity = '0';
    panel.style.maxHeight = '0px';
}

function openDemoPanel(item) {
    const trigger = item.querySelector('.demo-list-trigger');
    const panel = item.querySelector('.demo-list-panel');

    trigger.setAttribute('aria-expanded', 'true');
    item.classList.add('is-open');
    panel.style.maxHeight = panel.scrollHeight + 'px';
    panel.style.opacity = '1';
}

/* ---------- Visual panel: cross-fade between two stacked videos ---------- */

const phoneVideos = [document.getElementById('phoneVideoA'), document.getElementById('phoneVideoB')];
let activeVideoIndex = 0;
let videoSwapToken = 0;

function swapDemoVideo(src) {
    if (!src) return;

    const outgoing = phoneVideos[activeVideoIndex];
    const incoming = phoneVideos[1 - activeVideoIndex];
    const outgoingSrc = outgoing.querySelector('source').getAttribute('src');
    if (outgoingSrc === src) return; // already showing this clip

    const token = ++videoSwapToken; // invalidates any in-flight swap if clicked again

    incoming.classList.remove('is-active');
    incoming.pause();
    incoming.querySelector('source').setAttribute('src', src);
    incoming.load();

    function crossfade() {
        if (token !== videoSwapToken) return; // a newer click superseded this swap

        activeVideoIndex = 1 - activeVideoIndex;
        incoming.classList.add('is-active');
        outgoing.classList.remove('is-active');

        // Only start playback once the incoming clip has fully faded in -
        // starting it mid-fade reads as janky.
        incoming.addEventListener('transitionend', function onFadeIn(e) {
            if (e.propertyName !== 'opacity') return;
            incoming.removeEventListener('transitionend', onFadeIn);
            if (token !== videoSwapToken) return;
            incoming.currentTime = 0;
            incoming.play().catch(() => { /* autoplay may be blocked until interaction */ });
        });

        // Pause/reset the outgoing clip only once it's actually hidden.
        outgoing.addEventListener('transitionend', function onFadeOut(e) {
            if (e.propertyName !== 'opacity') return;
            outgoing.removeEventListener('transitionend', onFadeOut);
            if (token !== videoSwapToken) return;
            outgoing.pause();
            outgoing.currentTime = 0;
        });
    }

    // Wait until the incoming clip has enough data to play smoothly before
    // fading it in, so the fade-in doesn't reveal a stalled/buffering frame.
    if (incoming.readyState >= 3) {
        crossfade();
    } else {
        incoming.addEventListener('canplay', crossfade, { once: true });
    }
}

/* ---------- Wire it up ---------- */

demoListTriggers.forEach((trigger) => {
    trigger.addEventListener('click', () => {
        const item = trigger.closest('.demo-list-item');
        const wasOpen = trigger.getAttribute('aria-expanded') === 'true';

        if (openTimer) {
            clearTimeout(openTimer);
            openTimer = null;
        }

        demoListTriggers.forEach((otherTrigger) => {
            if (otherTrigger.getAttribute('aria-expanded') === 'true') {
                closeDemoPanel(otherTrigger.closest('.demo-list-item'));
            }
        });

        if (wasOpen) return; // it was the open one - just closed it, nothing else to do

        openTimer = setTimeout(() => {
            openDemoPanel(item);
            openTimer = null;
        }, OPEN_DELAY);

        swapDemoVideo(trigger.getAttribute('data-video'));
    });
});
