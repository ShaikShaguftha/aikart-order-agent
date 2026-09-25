/**
 * Luintix floating AI widget.
 *
 * The launcher avatar, its 13 emotions and the spring-back drag physics are
 * ported 1:1 from aiKart's own ScheduleMeetingWidget/RobotAvatar components
 * (this project's parent app) so the widget looks and moves exactly like the
 * one on aikart.co. Only the action wired to a click is different: instead
 * of opening a "schedule a meeting" card, it opens a live chat panel talking
 * to this project's own /api/chat endpoint (Groq-powered order agent).
 */
(function () {
    'use strict';

    /* ============ Robot avatar (ported from RobotAvatar.tsx) ============ */

    var CY = '#2563eb'; // locked to the site's brand blue
    var L = 36, R = 64, Y = 44;
    var STROKE = 'fill="none" stroke="' + CY + '" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"';

    var ROBOT_EMOTIONS = [
        'normal', 'happy', 'stars', 'love', 'wink', 'surprised', 'sleepy', 'question',
        'blush', 'dizzy', 'squint', 'angry', 'sad'
    ];

    function heart(cx) {
        return 'M' + cx + ' ' + (Y + 7) + ' C' + (cx - 11) + ' ' + (Y - 1) + ' ' + (cx - 6) + ' ' + (Y - 10) + ' ' + cx + ' ' + (Y - 4) +
            ' C' + (cx + 6) + ' ' + (Y - 10) + ' ' + (cx + 11) + ' ' + (Y - 1) + ' ' + cx + ' ' + (Y + 7) + 'Z';
    }
    function star(cx) {
        return 'M' + cx + ' ' + (Y - 9) + ' Q' + (cx + 1.5) + ' ' + (Y - 1.5) + ' ' + (cx + 9) + ' ' + Y +
            ' Q' + (cx + 1.5) + ' ' + (Y + 1.5) + ' ' + cx + ' ' + (Y + 9) +
            ' Q' + (cx - 1.5) + ' ' + (Y + 1.5) + ' ' + (cx - 9) + ' ' + Y +
            ' Q' + (cx - 1.5) + ' ' + (Y - 1.5) + ' ' + cx + ' ' + (Y - 9) + 'Z';
    }
    function spiral(cx) {
        return 'M' + cx + ' ' + Y + ' m0 -1 a1.5 1.5 0 1 1 -1.5 1.5 a3.5 3.5 0 1 1 3.5 3.5 a5.5 5.5 0 1 1 -5.5 -5.5 a7.5 7.5 0 1 1 7.5 7.5';
    }

    function eyesMarkup(emotion) {
        switch (emotion) {
            case 'love':
                return '<g fill="#ff4d6d"><path d="' + heart(L) + '"/><path d="' + heart(R) + '"/></g>';
            case 'stars':
                return '<g fill="' + CY + '"><path d="' + star(L) + '"/><path d="' + star(R) + '"/></g>';
            case 'sleepy':
                return '<g ' + STROKE + '>' +
                    '<path d="M' + (L - 8) + ' ' + (Y + 1) + ' h16"/><path d="M' + (R - 8) + ' ' + (Y + 1) + ' h16"/>' +
                    '<path d="M' + (R + 8) + ' ' + (Y - 10) + ' h5 l-5 5 h5" stroke-width="2"/></g>';
            case 'happy':
                return '<g ' + STROKE + '><path d="M' + (L - 8) + ' ' + (Y + 4) + ' l8 -9 l8 9"/><path d="M' + (R - 8) + ' ' + (Y + 4) + ' l8 -9 l8 9"/></g>';
            case 'wink':
                return '<g ' + STROKE + '><path d="M' + (L - 8) + ' ' + (Y + 1) + ' h16"/></g>' +
                    '<ellipse cx="' + R + '" cy="' + Y + '" rx="7" ry="8" fill="' + CY + '" stroke="none"/>';
            case 'dizzy':
                return '<g ' + STROKE + ' stroke-width="2.2"><path d="' + spiral(L) + '"/><path d="' + spiral(R) + '"/></g>';
            case 'angry':
                return '<g fill="' + CY + '">' +
                    '<path d="M' + (L - 10) + ' ' + (Y - 6) + ' L' + (L + 9) + ' ' + (Y + 1) + ' L' + (L + 6) + ' ' + (Y + 8) + ' L' + (L - 8) + ' ' + (Y + 6) + 'Z"/>' +
                    '<path d="M' + (R + 10) + ' ' + (Y - 6) + ' L' + (R - 9) + ' ' + (Y + 1) + ' L' + (R - 6) + ' ' + (Y + 8) + ' L' + (R + 8) + ' ' + (Y + 6) + 'Z"/></g>';
            case 'squint':
                return '<g ' + STROKE + '><path d="M' + (L - 8) + ' ' + (Y - 7) + ' l14 7 l-14 7"/><path d="M' + (R + 8) + ' ' + (Y - 7) + ' l-14 7 l14 7"/></g>';
            case 'surprised':
                return '<g fill="' + CY + '"><circle cx="' + L + '" cy="' + Y + '" r="7"/><circle cx="' + R + '" cy="' + Y + '" r="7"/></g>';
            case 'blush':
                return '<g ' + STROKE + ' stroke="#93c5fd"><path d="M' + (L - 8) + ' ' + (Y + 2) + ' q8 -9 16 0"/><path d="M' + (R - 8) + ' ' + (Y + 2) + ' q8 -9 16 0"/></g>' +
                    '<ellipse cx="' + (L - 2) + '" cy="' + (Y + 10) + '" rx="6" ry="3" fill="#ff6b81" opacity="0.7"/>' +
                    '<ellipse cx="' + (R + 2) + '" cy="' + (Y + 10) + '" rx="6" ry="3" fill="#ff6b81" opacity="0.7"/>';
            case 'question':
                return '<g ' + STROKE + '>' +
                    '<path d="M' + (L - 8) + ' ' + (Y + 2) + ' h16"/><path d="M' + (R - 8) + ' ' + (Y + 4) + ' h14"/>' +
                    '<path d="M' + (R + 8) + ' ' + (Y - 11) + ' q4 -2 4 2 q0 3 -3 4" stroke-width="2"/>' +
                    '<circle cx="' + (R + 9) + '" cy="' + (Y - 1) + '" r="0.8" fill="' + CY + '" stroke-width="1"/></g>';
            case 'sad':
                return '<g fill="' + CY + '">' +
                    '<path d="M' + (L - 8) + ' ' + (Y - 5) + ' h16 l-3 14 h-10z" opacity="0.9"/>' +
                    '<path d="M' + (R - 8) + ' ' + (Y - 5) + ' h16 l-3 14 h-10z" opacity="0.9"/></g>';
            default: // "normal"
                return '<g fill="' + CY + '">' +
                    '<path d="M' + (L - 9) + ' ' + (Y + 4) + ' q0 -13 9 -13 q9 0 9 13 q-9 -4 -18 0z"/>' +
                    '<path d="M' + (R - 9) + ' ' + (Y + 4) + ' q0 -13 9 -13 q9 0 9 13 q-9 -4 -18 0z"/></g>';
        }
    }

    function robotAvatarSvg(emotion, idPrefix) {
        return '<svg viewBox="0 0 100 88" aria-hidden="true">' +
            '<defs>' +
            '<radialGradient id="' + idPrefix + '-screen" cx="50%" cy="35%" r="75%"><stop offset="0%" stop-color="#12407a"/><stop offset="100%" stop-color="#020712"/></radialGradient>' +
            '<linearGradient id="' + idPrefix + '-shell" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#ffffff"/><stop offset="100%" stop-color="#c9ced6"/></linearGradient>' +
            '</defs>' +
            '<rect x="3" y="30" width="9" height="26" rx="4.5" fill="#111"/>' +
            '<rect x="9" y="33" width="2.4" height="20" rx="1.2" fill="#2563eb"/>' +
            '<rect x="88" y="30" width="9" height="26" rx="4.5" fill="#111"/>' +
            '<rect x="88.6" y="33" width="2.4" height="20" rx="1.2" fill="#2563eb"/>' +
            '<path d="M50 6 C78 6 92 26 92 50 C92 70 78 80 50 80 C22 80 8 70 8 50 C8 26 22 6 50 6Z" fill="url(#' + idPrefix + '-shell)"/>' +
            '<rect x="42" y="4.5" width="16" height="5" rx="2.5" fill="#2563eb"/>' +
            '<path d="M50 14 C74 14 85 28 85 48 C85 64 74 73 50 73 C26 73 15 64 15 48 C15 28 26 14 50 14Z" fill="url(#' + idPrefix + '-screen)"/>' +
            '<g>' + eyesMarkup(emotion) + '</g>' +
            '</svg>';
    }

    var SEND_SVG =
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none">' +
        '<path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' +
        '</svg>';

    var API_URL = '/api/chat';
    var SESSION_KEY = 'luintix_session_id';
    var SIZE = 72; // matches aiKart's launcher size

    function el(html) {
        var t = document.createElement('template');
        t.innerHTML = html.trim();
        return t.content.firstElementChild;
    }

    /* ============ Build DOM ============ */

    function buildWidget() {
        var root = el(
            '<div id="luintix-widget">' +
                '<button type="button" class="lw-launcher" id="lwLauncher" aria-label="Chat with Luintix" aria-expanded="false">' +
                    '<span class="lw-avatar" id="lwAvatar"></span>' +
                '</button>' +
                '<div class="lw-panel" id="lwPanel" role="dialog" aria-label="Luintix chat">' +
                    '<div class="lw-brand">' +
                        '<span class="lw-brand-mark" id="lwHeadAvatar"></span>' +
                        '<span class="lw-brand-name">Luintix</span>' +
                    '</div>' +
                    '<div class="lw-body" id="lwBody"></div>' +
                    '<form class="lw-input-row" id="lwForm">' +
                        '<span class="lw-status-dot" aria-hidden="true"></span>' +
                        '<input type="text" id="lwInput" placeholder="Chat with us" autocomplete="off" required>' +
                        '<button type="submit" class="lw-send" aria-label="Send">' + SEND_SVG + '</button>' +
                    '</form>' +
                '</div>' +
            '</div>'
        );
        document.body.appendChild(root);
        root.querySelector('#lwAvatar').innerHTML = robotAvatarSvg('normal', 'rb-launcher');
        root.querySelector('#lwHeadAvatar').innerHTML = robotAvatarSvg('happy', 'rb-head');
        return root;
    }

    /* ============ Chat logic (this project's own backend) ============ */

    function addMessage(body, text, sender, isHtml) {
        var msg = document.createElement('div');
        msg.className = 'lw-msg ' + sender;
        if (isHtml) { msg.innerHTML = text; } else { msg.textContent = text; }
        body.appendChild(msg);
        body.scrollTop = body.scrollHeight;
        return msg;
    }

    function addTyping(body) {
        var msg = document.createElement('div');
        msg.className = 'lw-msg bot';
        msg.innerHTML = '<span class="lw-typing"><span></span><span></span><span></span></span>';
        body.appendChild(msg);
        body.scrollTop = body.scrollHeight;
        return msg;
    }

    function renderBotContent(text) {
        if (window.marked && typeof window.marked.parse === 'function') {
            return window.marked.parse(text || 'No response returned.');
        }
        var div = document.createElement('div');
        div.textContent = text || 'No response returned.';
        return div.innerHTML;
    }

    function initChat(root) {
        var body = root.querySelector('#lwBody');
        var form = root.querySelector('#lwForm');
        var input = root.querySelector('#lwInput');

        addMessage(
            body,
            'Hi, I\'m <strong>Luintix</strong>, aiKart\'s AI order assistant. Ask me about tracking, cancellations, returns or refunds.',
            'bot',
            true
        );

        var sending = false;

        form.addEventListener('submit', function (e) {
            e.preventDefault();
            if (sending) return;

            var text = input.value.trim();
            if (!text) return;

            addMessage(body, text, 'user');
            input.value = '';
            sending = true;

            var typingEl = addTyping(body);

            fetch(API_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Company-ID': 'COMP-ALPHA' },
                body: JSON.stringify({
                    user_input: text,
                    session_id: localStorage.getItem(SESSION_KEY) || null
                })
            })
                .then(function (res) {
                    if (!res.ok) throw new Error('Server returned ' + res.status);
                    return res.json();
                })
                .then(function (data) {
                    if (data.session_id) localStorage.setItem(SESSION_KEY, data.session_id);
                    typingEl.innerHTML = renderBotContent(data.agent_response);
                    body.scrollTop = body.scrollHeight;
                })
                .catch(function (err) {
                    typingEl.textContent = 'Sorry, I couldn\'t reach the server (' + (err.message || 'connection error') + ').';
                })
                .finally(function () { sending = false; });
        });
    }

    /* ============ Launcher: drag physics + emotions (ported behavior) ============ */

    function initLauncher(root) {
        var launcher = root.querySelector('#lwLauncher');
        var avatarEl = root.querySelector('#lwAvatar');
        var panel = root.querySelector('#lwPanel');

        var pos = null;
        var dragging = false;
        var returning = false;
        var open = false;
        var hovered = false;
        var emoIdx = 0;
        var drag = null; // {dx, dy, sx, sy, moved}

        function home() {
            return { x: window.innerWidth - SIZE - 24, y: window.innerHeight - SIZE - 24 };
        }
        function clamp(x, y) {
            return {
                x: Math.min(Math.max(8, x), window.innerWidth - SIZE - 8),
                y: Math.min(Math.max(8, y), window.innerHeight - SIZE - 8)
            };
        }

        function applyPosition(withTransition) {
            root.style.transition = withTransition
                ? 'left .55s cubic-bezier(.34,1.4,.64,1), top .55s cubic-bezier(.34,1.4,.64,1)'
                : 'none';
            root.style.left = pos.x + 'px';
            root.style.top = pos.y + 'px';
        }

        function positionPanel() {
            // Flip the panel to whichever side keeps it on-screen, same rule
            // aiKart's widget uses for its popup card.
            var openBelow = pos.y < 340;
            var anchorRight = pos.x > window.innerWidth / 2;

            panel.style.top = openBelow ? (SIZE + 12) + 'px' : 'auto';
            panel.style.bottom = openBelow ? 'auto' : (SIZE + 12) + 'px';
            panel.style.left = anchorRight ? 'auto' : '0px';
            panel.style.right = anchorRight ? '36px' : 'auto';
            panel.style.transformOrigin =
                (openBelow ? 'top ' : 'bottom ') + (anchorRight ? 'right' : 'left');
        }

        function renderAvatar() {
            var emotion = (hovered || open) ? 'love' : ROBOT_EMOTIONS[emoIdx];
            avatarEl.innerHTML = robotAvatarSvg(emotion, 'rb-launcher');
        }

        function setOpen(next) {
            open = next;
            root.classList.toggle('open', open);
            launcher.setAttribute('aria-expanded', String(open));
            if (open) {
                positionPanel();
                setTimeout(function () {
                    var input = root.querySelector('#lwInput');
                    if (input) input.focus();
                }, 150);
            }
            renderAvatar();
        }

        // --- init position ---
        pos = clamp(home().x, home().y);
        applyPosition(false);

        window.addEventListener('resize', function () {
            if (dragging) return;
            pos = clamp(pos.x, pos.y);
            applyPosition(false);
            if (open) positionPanel();
        });

        // --- emotion cycling, like the reference widget ---
        setInterval(function () {
            emoIdx = (emoIdx + 1) % ROBOT_EMOTIONS.length;
            renderAvatar();
        }, 2600);
        renderAvatar();

        launcher.addEventListener('mouseenter', function () { hovered = true; renderAvatar(); });
        launcher.addEventListener('mouseleave', function () { hovered = false; renderAvatar(); });

        // --- drag physics (pointer events; spring back to the home corner) ---
        function pointerDown(e) {
            if (!pos) return;
            try { launcher.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
            drag = {
                dx: e.clientX - pos.x,
                dy: e.clientY - pos.y,
                sx: e.clientX,
                sy: e.clientY,
                moved: false
            };
        }

        function pointerMove(e) {
            if (!drag) return;
            if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 5) return;
            if (!drag.moved) {
                drag.moved = true;
                returning = false;
                dragging = true;
                launcher.classList.add('dragging');
                setOpen(false);
            }
            pos = clamp(e.clientX - drag.dx, e.clientY - drag.dy);
            applyPosition(false);
        }

        function pointerUp(e) {
            var d = drag;
            drag = null;
            if (!d) return;
            try { launcher.releasePointerCapture(e.pointerId); } catch (err) { /* noop */ }

            if (d.moved) {
                dragging = false;
                launcher.classList.remove('dragging');
                returning = true;
                pos = clamp(home().x, home().y);
                applyPosition(true);
                setTimeout(function () { returning = false; }, 650);
            } else {
                setOpen(!open);
            }
        }

        launcher.addEventListener('pointerdown', pointerDown);
        launcher.addEventListener('pointermove', pointerMove);
        launcher.addEventListener('pointerup', pointerUp);
        launcher.addEventListener('pointercancel', pointerUp);

        // --- close interactions (click outside / Escape; no in-panel close button) ---

        document.addEventListener('mousedown', function (e) {
            if (open && !root.contains(e.target)) setOpen(false);
        });
        document.addEventListener('touchstart', function (e) {
            if (open && !root.contains(e.target)) setOpen(false);
        }, { passive: true });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && open) setOpen(false);
        });

        // Small public hook so other scripts on the page (e.g. the
        // "Book a Demo" flow's "Chat with us now" button) can open the
        // widget without simulating pointer events on the launcher.
        window.luintixChat = { open: function () { setOpen(true); } };
    }

    function init() {
        var root = buildWidget();
        initLauncher(root);
        initChat(root);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
