"""Capture the top ~3 screens of each brand homepage via Edge CDP, with
    their own chat widgets / popups removed.
Usage: python scratch/capture_brand_shots.py landing/brands/shots [slug ...]"""
import base64, json, os, subprocess, sys, tempfile, time, urllib.request
import websocket

SITES = {
    "xyxxapparels": "https://xyxxcrew.com/",
    "thebearhouse": "https://thebearhouse.com/",
    "zouk": "https://zouk.co.in/",
    "neemans": "https://neemans.com/",
    "consciouschemist": "https://consciouschemist.com/",
    "foxtale": "https://foxtale.in/",
    "chemistatplay": "https://innovist.com/pages/chemist-at-play-store",
    "countrybean": "https://countrybean.in/",
    "godesi": "https://godesi.in/",
    "ragecoffee": "https://ragecoffee.com/",
    "thejuneshop": "https://thejuneshop.com/",
    "beco": "https://www.letsbeco.com/",
    "sleepycat": "https://sleepycat.in/",
    "superbottoms": "https://superbottoms.com/",
    "headsupfortails": "https://headsupfortails.com/",
    "dailyobjects": "https://www.dailyobjects.com/",
    "stuffcool": "https://www.stuffcool.com/",
    "portronics": "https://www.portronics.com/",
    "dotandkey": "https://www.dotandkey.com/",
    "bewakoof": "https://www.bewakoof.com/",
    "koskii": "https://www.koskii.com/",
}

W, H, SCREENS = 1920, 1080, 3
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0")

CLEANUP = r"""
(() => {
  const W = innerWidth, H = innerHeight;
  const CHAT = ['tidio','gorgias','freshchat','fc_frame','freshworks','intercom','zoko','wati','gallabox',
    'interakt','limechat','verloop','haptik','ymdiv','yellow','whatsapp','kapture','botpenguin','tawk','crisp',
    'chatbot','chat-widget','chatwidget','livechat','zendesk','launcher','messenger','bik-','bikapp','quickreply',
    'spurtnow','spur-','reward','loyalty','nector','smile-ui','growave','onesignal','izooto','pushowl','wonderpush','webpush','push-','notification','subscribe-prompt','optin','gupshup','helpshift','smartsupp','olark','drift','hubspot-messages','superchat','engati'];
  const hide = el => el && el.style && el.style.setProperty('display', 'none', 'important');
  const killed = [];
  // 1. known chat vendors / WhatsApp links
  document.querySelectorAll('body *').forEach(el => {
    const sig = ((el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : '') + ' ' +
      (el.getAttribute('src') || '') + ' ' + (el.getAttribute('title') || '') + ' ' + el.tagName).toLowerCase();
    if (el.tagName === 'A' && /wa\.me|api\.whatsapp|whatsapp\.com/.test(el.getAttribute('href') || '')) {
      const cs = getComputedStyle(el); if (cs.position === 'fixed' || el.closest('[style*="fixed"]')) { hide(el); killed.push('wa'); }
    }
    if (CHAT.some(k => sig.includes(k))) {
      const r = el.getBoundingClientRect();
      // only floating things, never page sections
      let n = el, fixed = false;
      for (let i = 0; n && i < 6; i++, n = n.parentElement) if (getComputedStyle(n).position === 'fixed') { fixed = true; break; }
      if (fixed || el.tagName === 'IFRAME') { const wide = n && n.getBoundingClientRect().width > W * 0.6; hide(fixed && !wide ? n : el); killed.push(sig.slice(0, 40)); }
    }
  });
  // 2. any fixed element: popups/backdrops (big), bottom-right floaters, bottom bars
  const scan = (root) => root.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) {
      const inner = [...el.shadowRoot.querySelectorAll('*')].some(c => {
        const cs = getComputedStyle(c); if (cs.position !== 'fixed') return false;
        const r = c.getBoundingClientRect(); return r.width > 0 && r.right > W - 320 && r.bottom > H - 360;
      });
      if (inner) { hide(el); killed.push('shadow:' + el.tagName); }
    }
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') return;
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (cs.position === 'sticky') return;
    const area = r.width * r.height;
    const isTopBar = r.top < 320 && r.height < 320 && r.width > W * 0.6;
    if (isTopBar) return;
    if (area > 0.25 * W * H) { hide(el); killed.push('big:' + el.tagName + '.' + el.className); return; }
    if (r.right > W - 320 && r.bottom > H - 360) { hide(el); killed.push('br:' + el.tagName + '.' + el.className); return; }
    if (r.top > H * 0.55 && r.width > W * 0.5) { hide(el); killed.push('bottombar:' + el.tagName); return; }
    // remaining floaters (rewards pills, side tabs, back-to-top) would sit mid-page in a still image
    if (r.top > 2) { hide(el); killed.push('float:' + el.tagName + '.' + el.className); }
  });
  scan(document);
  // whatever floats at the bottom-right corner (our bot's spot), even inside zero-size wrappers
  for (const [x, y] of [[W - 40, H - 40], [W - 70, H - 70], [W - 40, H - 120], [W - 120, H - 40]]) {
    const hit = document.elementFromPoint(x, y);
    for (let n = hit; n && n !== document.body; n = n.parentElement) {
      if (getComputedStyle(n).position === 'fixed') {
        if (n.getBoundingClientRect().width < W * 0.6) { hide(n); killed.push('pt:' + n.tagName + '.' + n.className); }
        break;
      }
    }
  }
  // logos that fade in on load
  if (!document.getElementById('__cap_css')) {
    const st = document.createElement('style'); st.id = '__cap_css';
    st.textContent = 'header img, header svg, [class*="logo" i] img, [class*="logo" i] svg { opacity: 1 !important; }';
    document.head.appendChild(st);
  }
  document.querySelectorAll('dialog[open]').forEach(d => { d.close && d.close(); hide(d); });
  for (const e of [document.documentElement, document.body]) {
    e.style.setProperty('overflow', 'auto', 'important');
    e.style.setProperty('position', 'static', 'important');
  }
  document.body.classList.remove('modal-open', 'overflow-hidden', 'no-scroll', 'locked');
  return killed.join(' | ');
})()
"""


class Tab:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.i = 0

    def call(self, method, **params):
        self.i += 1
        my = self.i
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


def capture(slug, url, out):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")
    info = json.loads(urllib.request.urlopen(req).read())
    tab = Tab(info["webSocketDebuggerUrl"])
    try:
        tab.call("Page.enable")
        tab.call("Network.enable")
        tab.call("Network.setUserAgentOverride", userAgent=UA, acceptLanguage="en-IN,en;q=0.9", platform="Windows")
        tab.call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        tab.call("Page.addScriptToEvaluateOnNewDocument",
                 source="Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        tab.call("Page.navigate", url=url)
        t0 = time.time()
        while time.time() - t0 < 30:
            time.sleep(1)
            if tab.js("document.readyState") == "complete":
                break
        time.sleep(5)
        tab.js(CLEANUP)
        # scroll through to trigger lazy images/animations, then back up
        for y in range(0, H * SCREENS + H, 400):
            tab.js(f"window.scrollTo(0,{y})")
            time.sleep(0.35)
        tab.js("window.scrollTo(0,0)")
        time.sleep(4)
        killed = tab.js(CLEANUP)
        time.sleep(1.5)
        killed2 = tab.js(CLEANUP)
        height = tab.js("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)") or H
        clip_h = min(H * SCREENS, height)
        shot = tab.call("Page.captureScreenshot", format="jpeg", quality=88, captureBeyondViewport=True,
                        clip={"x": 0, "y": 0, "width": W, "height": clip_h, "scale": 1})
        path = os.path.join(out, slug + ".jpg")
        with open(path, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        title = tab.js("document.title")
        print(f"OK {slug} h={clip_h} title={title!r}\n   removed: {killed} || {killed2}", flush=True)
    except Exception as e:
        print(f"FAIL {slug}: {e}", flush=True)
    finally:
        try:
            tab.ws.close()
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/close/{info['id']}")
        except Exception:
            pass


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    slugs = sys.argv[2:] or list(SITES)
    prof = tempfile.mkdtemp(prefix="edgecap")
    proc = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                             "--disable-gpu", "--hide-scrollbars", "--mute-audio", "--no-first-run",
                             "--disable-blink-features=AutomationControlled", f"--window-size={W},{H}",
                             "--remote-allow-origins=*"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        for s in slugs:
            capture(s, SITES[s], out)
    finally:
        proc.kill()


if __name__ == "__main__":
    main()
