"""
Browser-based page analyzer using Playwright.
Used when no frontend source path is provided.
Navigates to each URL, extracts DOM structure: inputs, buttons,
links, forms, headings - and returns a compact summary for prompts.
"""

import shutil
from dataclasses import dataclass, field
from typing import Optional

_SYSTEM_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium")
_SYSTEM_CHROME_ARGS  = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote",
]


def _chrome_launch_kwargs() -> dict:
    """Return channel+args when system Chrome is available, otherwise plain Playwright Chromium."""
    if any(shutil.which(n) for n in _SYSTEM_CHROME_NAMES):
        return {"channel": "chrome", "args": _SYSTEM_CHROME_ARGS}
    return {}


@dataclass
class PageAnalysis:
    url: str
    title: str
    form_fields: list[dict] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    has_login_form: bool = False
    is_spa: bool = False                   # True when SPA patterns detected
    hidden_inputs: list[dict] = field(default_factory=list)   # inputs present but hidden
    dynamic_title: bool = False            # True when title changes after JS render
    interactive_map: list[dict] = field(default_factory=list)  # click-and-observe results
    error: Optional[str] = None


def analyze_page(
    url: str,
    login: str = "",
    password: str = "",
    login_url: str = "",
) -> PageAnalysis:
    """
    Open the page in a headless browser and extract its structure.
    If login credentials are provided, authenticate first.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return PageAnalysis(url=url, title="", error="Playwright not installed")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **_chrome_launch_kwargs())
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.set_default_timeout(15000)

        try:
            # Authenticate if credentials provided
            if login and password and login_url:
                _do_login(page, login_url, login, password)

            page.goto(url, wait_until="domcontentloaded")
            _wait_for_spa_render(page)
        except PWTimeout:
            pass  # Partial load is fine - extract what's available
        except Exception as e:
            browser.close()
            return PageAnalysis(url=url, title="", error=str(e))

        _dismiss_overlays(page)
        result = _extract(page, url)
        result.is_spa = _detect_spa(page)
        result.dynamic_title = _detect_dynamic_title(page)
        result.hidden_inputs = _extract_hidden_inputs(page)
        result.interactive_map = _click_and_observe(page)
        browser.close()
        return result


def analyze_pages(
    urls: list[str],
    base_url: str,
    login: str = "",
    password: str = "",
    login_url: str = "/login",
    log_callback=None,
) -> dict[str, PageAnalysis]:
    """Analyze multiple pages. Returns dict keyed by URL path."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {url: PageAnalysis(url=url, title="", error="Playwright not installed") for url in urls}

    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **_chrome_launch_kwargs())
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.set_default_timeout(15000)

        # Authenticate once for the whole session
        if login and password:
            full_login_url = base_url.rstrip("/") + "/" + login_url.lstrip("/")
            if log_callback:
                log_callback(f"  Logging in at {login_url}...")
            _do_login(page, full_login_url, login, password)
            if log_callback:
                log_callback(f"  Login successful.")

        for url_path in urls:
            full_url = base_url.rstrip("/") + url_path
            if log_callback:
                log_callback(f"  Scanning page: {url_path}")
            nav_error = None
            try:
                page.goto(full_url, wait_until="domcontentloaded")
                _wait_for_spa_render(page)
            except Exception as e:
                nav_error = _short_error(str(e))
                if log_callback:
                    log_callback(f"  Warning: could not load {url_path} - {nav_error}")
                results[url_path] = PageAnalysis(url=url_path, title="", error=nav_error)
                continue

            _dismiss_overlays(page)
            analysis = _extract(page, url_path)
            analysis.is_spa = _detect_spa(page)
            analysis.dynamic_title = _detect_dynamic_title(page)
            analysis.hidden_inputs = _extract_hidden_inputs(page)
            analysis.interactive_map = _click_and_observe(page)
            results[url_path] = analysis

        browser.close()
    return results


def _do_login(page, login_url: str, login: str, password: str):
    from playwright.sync_api import TimeoutError as PWTimeout
    page.goto(login_url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    # Verify login page has a password field - if not, URL is wrong
    has_password = page.locator('input[type="password"]').count() > 0
    if not has_password:
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        raise ValueError(
            f"No login form found at '{login_url}' (title='{title}'). "
            f"Check the Login URL field and make sure it points to the sign-in page."
        )

    # Fill login field
    for selector in ['input[type="email"]', 'input[name="login"]',
                     'input[name="email"]', 'input[name="username"]',
                     'input[placeholder*="mail" i]', 'input[placeholder*="login" i]']:
        if page.locator(selector).count() > 0:
            page.locator(selector).first.fill(login)
            break

    page.locator('input[type="password"]').first.fill(password)

    # Submit
    for selector in ['button[type="submit"]', 'input[type="submit"]',
                     'button:has-text("Login")', 'button:has-text("Sign in")',
                     'button:has-text("Войти")']:
        if page.locator(selector).count() > 0:
            page.locator(selector).first.click()
            break

    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def _wait_for_spa_render(page) -> None:
    """
    Wait for a SPA (React/Vue/Angular) page to finish rendering.
    Uses wait_for_selector (no JS eval, CSP-safe) to detect when
    interactive elements appear in the DOM.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass

    # Wait until at least one interactive element appears (CSP-safe, no eval)
    for selector in ["button", "input", "a[href]", "[role='button']"]:
        try:
            page.wait_for_selector(selector, timeout=8000, state="attached")
            return  # Found something — DOM is ready
        except Exception:
            continue

    # Last resort: give the page extra time
    page.wait_for_timeout(3000)


def _short_error(msg: str) -> str:
    """Return the first line of a Playwright error, dropping the verbose 'Call log:' trace."""
    first_line = msg.split("\n")[0].strip()
    # Extract the key part: net::ERR_... or timeout description
    if "net::ERR_CONNECTION_REFUSED" in msg:
        return "Connection refused - site is unreachable. Check that the Base URL is correct and the server is running."
    if "net::ERR_NAME_NOT_RESOLVED" in msg:
        return "Domain not found - check the Base URL."
    if "net::ERR_" in msg:
        import re as _re
        code = _re.search(r"net::(ERR_\w+)", msg)
        return f"Network error: {code.group(1) if code else 'connection failed'}"
    if "TimeoutError" in msg or "Timeout" in first_line:
        return "Page load timed out - site is too slow or unreachable."
    return first_line[:200]


# Selectors for containers that typically hold ads or cookie/consent dialogs.
# Elements inside these are excluded from analysis.
_AD_CONTAINER_SELECTORS = (
    "[id*='cookie' i]", "[class*='cookie' i]",
    "[id*='consent' i]", "[class*='consent' i]",
    "[id*='gdpr' i]", "[class*='gdpr' i]",
    "[id*='privacy' i]", "[class*='privacy-banner' i]",
    "[id*='ad-' i]", "[class*='ad-banner' i]", "[class*='adsbygoogle' i]",
    "[id*='onetrust' i]", "[class*='onetrust' i]",
    "[id*='cookielaw' i]", "[class*='cookielaw' i]",
    "iframe",  # cross-origin ads in iframes - unreachable anyway
)


def _dismiss_overlays(page) -> None:
    """
    Try to close cookie consent banners and other overlays
    before extracting page structure, so they don't pollute the analysis.
    """
    accept_selectors = [
        # English
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        'button:has-text("Accept cookies")',
        'button:has-text("Accept")',
        'button:has-text("I agree")',
        'button:has-text("I Accept")',
        'button:has-text("Got it")',
        'button:has-text("Agree")',
        'button:has-text("OK")',
        'button:has-text("Allow all")',
        'button:has-text("Allow All")',
        # Russian - for Russian-language sites
        'button:has-text("Принять")',
        'button:has-text("Принять все")',
        'button:has-text("Согласен")',
        'button:has-text("Хорошо")',
        # Generic close buttons on overlay dialogs
        '[id*="cookie" i] button',
        '[class*="cookie" i] button',
        '[id*="consent" i] button',
        '[class*="consent" i] button',
        '[id*="onetrust" i] button',
        '[id*="cookielaw" i] button',
    ]
    for selector in accept_selectors:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=400):
                btn.click(timeout=400)
                page.wait_for_timeout(300)
                return  # One click is enough
        except Exception:
            continue


def _is_inside_ad_container(page, element) -> bool:
    """Return True if the element lives inside a known ad/cookie container."""
    try:
        for sel in _AD_CONTAINER_SELECTORS:
            # Check if any ancestor matches the ad selector
            if page.locator(sel).filter(has=element).count() > 0:
                return True
    except Exception:
        pass
    return False


def _extract(page, url_path: str) -> PageAnalysis:
    title = ""
    form_fields = []
    buttons = []
    links = []
    headings = []

    try:
        title = page.title()
    except Exception:
        pass

    # Extract input fields
    try:
        inputs = page.locator(
            "input:not([type='hidden']), textarea, select"
        ).all()
        for inp in inputs[:20]:
            try:
                if _is_inside_ad_container(page, inp):
                    continue
                field = {
                    "type":        inp.get_attribute("type") or "text",
                    "name":        inp.get_attribute("name") or inp.get_attribute("id") or "",
                    "placeholder": inp.get_attribute("placeholder") or "",
                    "required":    inp.get_attribute("required") is not None,
                    "maxlength":   inp.get_attribute("maxlength"),
                    "label":       _find_label(page, inp),
                }
                form_fields.append(field)
            except Exception:
                continue
    except Exception:
        pass

    # Extract buttons
    try:
        btn_els = page.locator(
            "button, input[type='submit'], a[role='button']"
        ).all()
        for btn in btn_els[:15]:
            try:
                if _is_inside_ad_container(page, btn):
                    continue
                text = (btn.inner_text() or btn.get_attribute("value") or "").strip()
                if text:
                    buttons.append(text[:60])
            except Exception:
                continue
    except Exception:
        pass

    # Extract navigation links
    try:
        link_els = page.locator("nav a, header a, [role='navigation'] a").all()
        for lnk in link_els[:15]:
            try:
                href = lnk.get_attribute("href") or ""
                text = lnk.inner_text().strip()
                if text and href:
                    links.append({"text": text[:40], "href": href[:80]})
            except Exception:
                continue
    except Exception:
        pass

    # Extract headings for context
    try:
        h_els = page.locator("h1, h2, h3").all()
        for h in h_els[:8]:
            try:
                text = h.inner_text().strip()
                if text:
                    headings.append(text[:60])
            except Exception:
                continue
    except Exception:
        pass

    has_login_form = any(
        f.get("type") == "password" for f in form_fields
    )

    return PageAnalysis(
        url=url_path,
        title=title,
        form_fields=form_fields,
        buttons=buttons,
        links=links,
        headings=headings,
        has_login_form=has_login_form,
    )


def _find_label(page, input_el) -> str:
    """Try to find the label text associated with an input."""
    try:
        input_id = input_el.get_attribute("id")
        if input_id:
            label = page.locator(f"label[for='{input_id}']")
            if label.count() > 0:
                return label.first.inner_text().strip()[:50]
    except Exception:
        pass
    return ""


def _click_and_observe(page) -> list[dict]:
    """
    For each visible interactive button/trigger on the page:
      1. Take a DOM snapshot (visible elements count + their selectors)
      2. Click the element
      3. Wait briefly
      4. Take another snapshot
      5. Record the diff: what NEW elements appeared, their tag/role/class/text
      6. Close/dismiss the opened popup (press Escape)

    Returns a list of dicts:
      {
        "trigger_text": "Type",
        "trigger_selector": "button[name='Type']",
        "opened_elements": [
          {"selector": ".lg6wo", "count": 12, "sample_text": "SQLi\nXSS\n..."},
          ...
        ]
      }

    This lets Claude know EXACTLY what to assert for ANY custom UI component —
    no guessing about ARIA roles or class name patterns.
    """
    results = []

    try:
        # Collect candidate trigger buttons (skip navigation/auth buttons)
        _SKIP_TEXTS = {
            "sign in", "login", "logout", "sign out", "menu", "close", "cancel",
            "settings", "help", "home", "back", "next", "previous",
        }
        btn_els = page.locator("button, [role='button'], [role='tab']").all()
        candidates = []
        for btn in btn_els[:20]:
            try:
                if not btn.is_visible():
                    continue
                text = (btn.inner_text() or "").strip()[:50]
                if not text or text.lower() in _SKIP_TEXTS:
                    continue
                candidates.append((text, btn))
            except Exception:
                continue

        for btn_text, btn in candidates[:8]:   # limit to 8 buttons to keep scan fast
            try:
                # Snapshot before click: collect all currently visible elements with classes
                before = _dom_signature(page)

                btn.click(timeout=3000)
                page.wait_for_timeout(600)

                # Snapshot after click
                after = _dom_signature(page)

                # Diff: elements that appeared (in after but not before)
                new_sigs = [sig for sig in after if sig not in before]

                if not new_sigs:
                    # Nothing new — this button doesn't open a popup
                    _close_popup(page)
                    continue

                # For each new element group, record selector + sample text
                opened = []
                for sig in new_sigs[:6]:
                    selector = sig["selector"]
                    try:
                        els = page.locator(selector)
                        count = els.count()
                        if count == 0:
                            continue
                        sample = _collect_sample_text(els, max_items=5)
                        opened.append({
                            "selector": selector,
                            "count": count,
                            "sample_text": sample,
                            "tag": sig["tag"],
                        })
                    except Exception:
                        continue

                if opened:
                    # Try to find a stable locator for the trigger itself
                    trigger_selector = _best_trigger_selector(btn, btn_text)
                    results.append({
                        "trigger_text": btn_text,
                        "trigger_selector": trigger_selector,
                        "opened_elements": opened,
                    })

                _close_popup(page)

            except Exception:
                try:
                    _close_popup(page)
                except Exception:
                    pass
                continue

    except Exception:
        pass

    return results


def _dom_signature(page) -> list[dict]:
    """
    Return a compact list of {selector, tag} for all currently visible
    non-trivial elements. Used to diff before/after a click.
    """
    try:
        return page.evaluate("""() => {
            const result = [];
            const seen = new Set();
            document.querySelectorAll('*').forEach(el => {
                const rect = el.getBoundingClientRect();
                if (rect.width < 20 || rect.height < 8) return;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return;

                const cls = typeof el.className === 'string'
                    ? el.className.trim().split(/\\s+/).filter(Boolean).slice(0, 3).join('.')
                    : '';
                const role = el.getAttribute('role') || '';
                const tag = el.tagName.toLowerCase();

                // Build a compact selector
                let sel = tag;
                if (cls) sel += '.' + cls;
                if (role) sel += `[role="${role}"]`;

                if (sel.length > 5 && !seen.has(sel)) {
                    seen.add(sel);
                    result.push({ selector: sel, tag: tag });
                }
            });
            return result.slice(0, 300);
        }""")
    except Exception:
        return []


def _collect_sample_text(locator, max_items: int = 5) -> str:
    """Collect sample inner text from up to max_items elements."""
    texts = []
    try:
        count = min(locator.count(), max_items)
        for i in range(count):
            try:
                t = locator.nth(i).inner_text().strip()[:40]
                if t:
                    texts.append(t)
            except Exception:
                continue
    except Exception:
        pass
    return " | ".join(texts) if texts else ""


def _best_trigger_selector(btn, btn_text: str) -> str:
    """Build the most stable selector for a trigger button."""
    try:
        role = btn.get_attribute("role") or ""
        aria_label = btn.get_attribute("aria-label") or ""
        data_testid = btn.get_attribute("data-testid") or ""

        if data_testid:
            return f"[data-testid='{data_testid}']"
        if aria_label:
            return f"[aria-label='{aria_label}']"
        tag = btn.evaluate("el => el.tagName.toLowerCase()")
        if role:
            return f"{tag}[role='{role}']"
        # Fall back to text-based role selector description
        return f"button with text '{btn_text}'"
    except Exception:
        return f"button with text '{btn_text}'"


def _close_popup(page) -> None:
    """Try to dismiss any open popup/dropdown."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass


def _detect_spa(page) -> bool:
    """Return True if the page appears to be a SPA (React/Vue/Angular/etc.)."""
    try:
        result = page.evaluate("""() => {
            // React root
            if (document.querySelector('#root, #app, [data-reactroot]')) return true;
            // __NEXT_DATA__ (Next.js), __nuxt (Nuxt), ng-version (Angular)
            if (window.__NEXT_DATA__ || window.__nuxt || document.querySelector('[ng-version]')) return true;
            // Check for hash or pushState routing patterns
            if (window.history && window.__vue_router__) return true;
            // Heuristic: very few server-rendered elements, most content loaded via JS
            const scriptTags = document.querySelectorAll('script[src]').length;
            const jsModules = document.querySelectorAll('script[type="module"]').length;
            if (scriptTags > 5 || jsModules > 0) return true;
            return false;
        }""")
        return bool(result)
    except Exception:
        return False


def _detect_dynamic_title(page) -> bool:
    """
    Return True if the page title appears to be set dynamically by JS
    (i.e., it's more specific than just the site name).
    """
    try:
        title = page.title()
        # If title is empty or very short it's likely not yet set
        if not title or len(title) < 4:
            return True
        # If the title contains a separator character (' – ', ' | ', ' - ')
        # it's very likely a SPA-set page-specific title
        if any(sep in title for sep in [' – ', ' | ', ' - ', ' · ']):
            return True
        return False
    except Exception:
        return False


def _extract_hidden_inputs(page) -> list[dict]:
    """
    Find input/textarea elements that are present in DOM but not visible.
    These are common in SPAs where fields are hidden until user interaction.
    Returns a compact description for the prompt so Claude knows to handle them.
    """
    hidden = []
    try:
        inputs = page.locator("input:not([type='hidden']), textarea").all()
        for inp in inputs[:30]:
            try:
                if inp.is_visible():
                    continue  # only interested in hidden ones
                placeholder = inp.get_attribute("placeholder") or ""
                name = inp.get_attribute("name") or inp.get_attribute("id") or ""
                tag = inp.evaluate("el => el.tagName.toLowerCase()")
                if placeholder or name:
                    hidden.append({
                        "tag": tag,
                        "placeholder": placeholder[:60],
                        "name": name[:40],
                    })
            except Exception:
                continue
    except Exception:
        pass
    return hidden


def to_route_info(analysis: PageAnalysis) -> dict:
    """Convert PageAnalysis to the same format as frontend_analyzer route dicts."""
    return {
        "path":           analysis.url,
        "title":          analysis.title,
        "component_file": "browser-analyzed",
        "form_fields":    analysis.form_fields,
        "buttons":        analysis.buttons,
        "links":          analysis.links,
        "headings":       analysis.headings,
        "api_calls":      [],
        "auth_required":  analysis.has_login_form,
        "is_spa":          analysis.is_spa,
        "dynamic_title":   analysis.dynamic_title,
        "hidden_inputs":   analysis.hidden_inputs,
        "interactive_map": analysis.interactive_map,
    }
