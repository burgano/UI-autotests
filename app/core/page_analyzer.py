"""
Browser-based page analyzer using Playwright.
Used when no frontend source path is provided.
Navigates to each URL, extracts DOM structure: inputs, buttons,
links, forms, headings - and returns a compact summary for prompts.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PageAnalysis:
    url: str
    title: str
    form_fields: list[dict] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    has_login_form: bool = False
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
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.set_default_timeout(15000)

        try:
            # Authenticate if credentials provided
            if login and password and login_url:
                _do_login(page, login_url, login, password)

            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass  # Partial load is fine - extract what's available
        except Exception as e:
            browser.close()
            return PageAnalysis(url=url, title="", error=str(e))

        _dismiss_overlays(page)
        result = _extract(page, url)
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
        browser = p.chromium.launch(headless=True)
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
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass  # Partial load is fine - extract what's available
            except Exception as e:
                nav_error = _short_error(str(e))
                if log_callback:
                    log_callback(f"  Warning: could not load {url_path} - {nav_error}")
                results[url_path] = PageAnalysis(url=url_path, title="", error=nav_error)
                continue

            _dismiss_overlays(page)
            results[url_path] = _extract(page, url_path)

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

    # Build a combined exclusion selector to skip ad/cookie containers
    _exclude = ", ".join(_AD_CONTAINER_SELECTORS)

    # Extract input fields (skip fields inside ad/cookie containers)
    try:
        inputs = page.locator(
            f"input:not([type='hidden']):not({_exclude} input), "
            f"textarea:not({_exclude} textarea), "
            f"select:not({_exclude} select)"
        ).all()
        for inp in inputs[:20]:
            try:
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

    # Extract buttons (skip buttons inside ad/cookie containers)
    try:
        btn_els = page.locator(
            f"button:not({_exclude} button), "
            f"input[type='submit']:not({_exclude} input), "
            f"a[role='button']:not({_exclude} a)"
        ).all()
        for btn in btn_els[:15]:
            try:
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


def to_route_info(analysis: PageAnalysis) -> dict:
    """Convert PageAnalysis to the same format as frontend_analyzer route dicts."""
    return {
        "path":          analysis.url,
        "title":         analysis.title,
        "component_file": "browser-analyzed",
        "form_fields":   analysis.form_fields,
        "buttons":       analysis.buttons,
        "links":         analysis.links,
        "headings":      analysis.headings,
        "api_calls":     [],
        "auth_required": analysis.has_login_form,
    }
