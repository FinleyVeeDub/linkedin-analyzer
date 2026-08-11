#!/usr/bin/env python3
"""
LinkedIn Background Server - Runs independently, keeps browser open.
Serves data to LM Studio via GET /profile (JSON response, format unchanged).

v3 changes:
  - Adapted to LinkedIn's 2026 SDUI profile layout (hashed CSS classes, no h1, no JSON-LD)
  - Text-based extraction replaces brittle CSS selectors (resilient to layout changes)
  - Sections detected by h2 heading text (Info, Serviceleistungen, etc.) instead of aria-labels
  - Experience/Education extracted via detail page navigation (/details/experience/, /details/education/)
  - Skills via /overlay/top-skills-details/ instead of /details/skills/
  - JSON-LD removal (LinkedIn no longer serves it on profile pages)
"""
import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright._impl._errors import TargetClosedError
from session_manager import SessionManager
from config import settings
import uvicorn


# ---------------------------------------------------------------------------
# Browser Manager
# ---------------------------------------------------------------------------

class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.session_manager = SessionManager("linkedin_session")
        # Serialize all page interactions to avoid concurrent navigations
        # causing "Execution context was destroyed" races.
        self.page_lock = asyncio.Lock()

    async def start(self) -> bool:
        if self.browser and not self.browser.is_closed():
            return False  # already running
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=settings.BROWSER_HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        await self.session_manager.load_session(self.context)

        # Do not inject static consent-cookie values.
        # LinkedIn rotates consent mechanics and stale hardcoded values can
        # increase redirects to privacy/consent pages.

        self.page = await self.context.new_page()

        # Anti-detection
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de', 'en'] });
        """)
        return True  # newly started

    @staticmethod
    def _is_consent_url(url: str | None) -> bool:
        if not url:
            return False
        lowered = url.lower()
        return any(token in lowered for token in [
            "/cookie",
            "cookie-policy",
            "consent",
            "legal/privacy",
        ])

    async def _goto_with_fallback(self, url: str):
        """
        Navigate with a less brittle wait strategy.
        LinkedIn sometimes never reaches "load" due trackers/consent overlays.
        """
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=settings.BROWSER_TIMEOUT)
        except PlaywrightTimeoutError:
            current = self.page.url if self.page else ""
            if "linkedin.com" not in current:
                raise

    async def dismiss_cookie_consent(self) -> bool:
        """Try to dismiss cookie consent in page and embedded frames."""
        if not self.page:
            return False

        consent_selectors = [
            'div.artdeco-global-alert--cookie_consent button[data-test-global-alert-action="0"]',
            '.artdeco-global-alert--cookie_consent .artdeco-global-alert__action-wrapper button:first-of-type',
            'button[action-type="ACCEPT"]',
            'button[data-tracking-control-name="ga-cookie.consent.accept.v4"]',
            '#onetrust-accept-btn-handler',
            'button[data-test-cookie-banner-accept-btn]',
            'button:has-text("Alle akzeptieren")',
            'button:has-text("Alle Cookies akzeptieren")',
            'button:has-text("Akzeptieren")',
            'button:has-text("Zustimmen")',
            'button:has-text("Einverstanden")',
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("Allow all")',
            'button:has-text("I agree")',
            '[aria-label*="Accept"][role="button"]',
            '[aria-label*="Akzept"][role="button"]',
            '[aria-label*="consent"][role="button"]',
            '[id*="accept"]',
            '[data-test*="accept"]',
        ]

        clicked = False

        async def _click_in_context(ctx) -> bool:
            nonlocal clicked
            for sel in consent_selectors:
                try:
                    btn = await ctx.query_selector(sel)
                    if btn:
                        await btn.click(timeout=2000, force=True)
                        clicked = True
                        return True
                except Exception:
                    pass

            # Generic fallback for unknown consent UIs (text/aria based)
            try:
                js_clicked = await ctx.evaluate("""() => {
                    const pat = /(accept|akzept|zustimmen|einverstanden|allow all|accept all|i agree|consent)/i;
                    const candidates = Array.from(
                        document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"], a')
                    );
                    for (const el of candidates) {
                        const txt = (
                            (el.innerText || el.textContent || el.value || '') + ' ' +
                            (el.getAttribute('aria-label') || '')
                        ).trim();
                        if (!txt || !pat.test(txt)) continue;
                        const style = window.getComputedStyle(el);
                        if (style && (style.display === 'none' || style.visibility === 'hidden')) continue;
                        el.click();
                        return true;
                    }
                    return false;
                }""")
                if js_clicked:
                    clicked = True
                    return True
            except Exception:
                pass
            return False

        for _ in range(3):
            contexts = [self.page]
            try:
                contexts.extend(self.page.frames)
            except Exception:
                pass

            for ctx in contexts:
                await _click_in_context(ctx)

            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass

            await self.page.wait_for_timeout(900)
            if not self._is_consent_url(self.page.url) and not await self.has_cookie_banner():
                return clicked

        # Cookie hint fallback (non-static values only)
        try:
            if self.context:
                await self.context.add_cookies([
                    {"name": "li_sugr", "value": "accepted", "domain": ".linkedin.com", "path": "/", "sameSite": "None", "secure": True},
                    {"name": "aam_uuid", "value": "accepted", "domain": ".linkedin.com", "path": "/", "sameSite": "None", "secure": True},
                ])
                clicked = True
        except Exception:
            pass

        return clicked

    async def has_cookie_banner(self) -> bool:
        if not self.page:
            return False
        try:
            return await self.page.evaluate("""() => {
                return !!document.querySelector(
                    '.artdeco-global-alert--cookie_consent, [data-test-global-alert][class*="cookie_consent"]'
                );
            }""")
        except Exception:
            return False

    async def recover_from_consent(self, locale: str = "de") -> bool:
        """Try to recover from consent redirects by hopping via feed and retrying profile."""
        if not self.page:
            return False
        base_url = f"{settings.LINKEDIN_URL}/in/me/"
        profile_url = base_url if locale == "de" else f"{base_url}?locale=en_US"
        feed_url = f"{settings.LINKEDIN_URL}/feed/"

        for _ in range(3):
            await self.dismiss_cookie_consent()
            try:
                await self._goto_with_fallback(feed_url)
                await self.page.wait_for_timeout(900)
            except Exception:
                pass
            await self.dismiss_cookie_consent()
            try:
                await self._goto_with_fallback(profile_url)
                await self.page.wait_for_timeout(1100)
            except Exception:
                pass
            if not self._is_consent_url(self.page.url) and not await self.has_cookie_banner():
                return True
        return False

    async def check_login(self) -> bool:
        """Fast login check – does NOT navigate."""
        if not self.page:
            return False
        try:
            url = self.page.url
            # Logged-out / challenged states must never count as logged in.
            if any(seg in url for seg in ["/authwall", "/login", "/uas/login", "/checkpoint"]):
                return False
            if any(seg in url for seg in ["/feed/", "/in/", "/search/", "/messaging/", "/mynetwork/", "/jobs/"]):
                return True
            try:
                await self.page.wait_for_selector('button:has-text("Sign in")', timeout=500)
                return False
            except Exception:
                pass
            return "linkedin.com" in url
        except Exception:
            return False

    async def navigate_to_profile(self, locale: str = "de") -> bool:
        """
        Navigate to own LinkedIn profile.
        locale: 'de' (default) or 'en' (appends ?locale=en_US)
        """
        base_url = f"{settings.LINKEDIN_URL}/in/me/"
        profile_url = base_url if locale == "de" else f"{base_url}?locale=en_US"

        await self._goto_with_fallback(profile_url)
        await self.page.wait_for_timeout(1200)

        consent_dismissed = await self.dismiss_cookie_consent()
        has_banner = await self.has_cookie_banner()
        if has_banner or consent_dismissed or self._is_consent_url(self.page.url):
            await self._goto_with_fallback(profile_url)
            await self.page.wait_for_timeout(1200)

        if self._is_consent_url(self.page.url):
            await self.dismiss_cookie_consent()
            await self._goto_with_fallback(profile_url)
            await self.page.wait_for_timeout(1200)

        if self._is_consent_url(self.page.url):
            await self.recover_from_consent(locale=locale)
            await self.page.wait_for_timeout(1000)

        current_url = self.page.url
        if any(seg in current_url for seg in ["/login", "/authwall", "/checkpoint"]):
            return False
        if self._is_consent_url(current_url):
            return False

        await self.page.wait_for_timeout(1200)

        scroll_height = await self.page.evaluate("document.body.scrollHeight")
        step = 400
        current_pos = 0
        while current_pos < scroll_height:
            current_pos = min(current_pos + step, scroll_height)
            try:
                await self.page.evaluate(f"window.scrollTo(0, {current_pos})")
            except Exception as e:
                # Another navigation can briefly invalidate the JS context.
                # Do not fail hard here; proceed with best-effort scrolling.
                if "Execution context was destroyed" in str(e):
                    break
                raise
            await self.page.wait_for_timeout(180)

        await self.page.evaluate("window.scrollTo(0, 0)")
        await self.page.wait_for_timeout(1000)
        return True

    async def save(self):
        if self.context:
            await self.session_manager.save_session(self.context)

    async def stop(self):
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

    async def restart(self) -> bool:
        """Full teardown + fresh launch. Returns True if the browser restarted."""
        await self.stop()
        try:
            return await self.start()
        except Exception:
            return False


# ---------------------------------------------------------------------------
# App Lifespan (replaces deprecated @app.on_event)
# ---------------------------------------------------------------------------

browser_mgr: BrowserManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser_mgr
    browser_mgr = BrowserManager()
    await browser_mgr.start()
    yield
    if browser_mgr:
        await browser_mgr.stop()


# Sentinel returned when browser is not ready
NOT_READY = "Not ready"

app = FastAPI(title="LinkedIn Background Server", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helper: JSON-LD extraction
# ---------------------------------------------------------------------------

async def _extract_json_ld(_page) -> dict | None:
    """No-op: LinkedIn no longer serves JSON-LD on profile pages."""
    return None


# ---------------------------------------------------------------------------
# Helper: Scrape via page.evaluate (most robust; bypasses selector churn)
# ---------------------------------------------------------------------------

async def _scrape_profile_data(page) -> dict:
    """
    Extract all profile fields.
    Uses text-based extraction + detail page navigation for collapsed sections.
    """
    profile_url = page.url

    _extract_json_ld(page)  # no-op, kept for signature compatibility

    data = await _extract_main_page_fields(page)

    consent_name = (data.get("name") or "")
    if any(kw in consent_name for kw in ["Datenschutz", "Privatsph", "privacy", "Privacy"]):
        raise RuntimeError(
            "Cookie consent page detected – profile not yet loaded. "
            "Call POST /login and retry."
        )

    # Experience/Education from detail pages (collapsed on main page)
    data["experience"] = await _field_experience(page, profile_url)
    data["education"]  = await _field_education(page, profile_url)

    # Skills from detail page
    data["skills"] = await _field_skills_full(page, profile_url)

    data["profile_url"] = profile_url
    return data


async def _extract_main_page_fields(page) -> dict:
    """Extract all fields visible on the main profile page via text-based JS."""
    fields = await page.evaluate(PROFILE_PAGE_EXTRACTOR)
    return fields


async def _field_skills_full(page, profile_url: str) -> list:
    """
    Navigate to /overlay/top-skills-details/ to get ALL skills.
    Falls back to main page skills on failure.
    """

    # Build the skills detail URL from the current profile URL
    base = profile_url.split("?")[0].rstrip("/")
    # Use overlay URL which shows the full skills list as an overlay page
    skills_url = f"{base}/overlay/top-skills-details/"

    async def _dismiss_inline_consent() -> bool:
        selectors = [
            'button[action-type="ACCEPT"]',
            '#onetrust-accept-btn-handler',
            'button:has-text("Alle akzeptieren")',
            'button:has-text("Akzeptieren")',
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
        ]
        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(900)
                    return True
            except Exception:
                pass
        try:
            js_clicked = await page.evaluate("""() => {
                const pat = /(accept|akzept|zustimmen|consent)/i;
                const candidates = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"]'));
                for (const el of candidates) {
                    const t = ((el.innerText || el.textContent || el.value || '') + ' ' + (el.getAttribute('aria-label') || '')).trim();
                    if (!t || !pat.test(t)) continue;
                    el.click();
                    return true;
                }
                return false;
            }""")
            if js_clicked:
                await page.wait_for_timeout(900)
                return True
        except Exception:
            pass
        return False

    try:
        await page.goto(skills_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)


        # Dismiss consent if needed
        if await _dismiss_inline_consent():
            await page.goto(skills_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2200)

        # Scroll to load all skills
        sh = await page.evaluate("document.body.scrollHeight")
        pos = 0
        while pos < sh:
            pos = min(pos + 500, sh)
            await page.evaluate(f"window.scrollTo(0, {pos})")
            await page.wait_for_timeout(100)
        await page.wait_for_timeout(1000)

        skills = await page.evaluate("""() => {
            const lines = document.body.innerText.split('\\n').map(l => l.trim()).filter(l => l.length > 2);
            const results = [];
            const seen = new Set();
            // Skip nav/header lines (notification count, nav items, etc.)
            const noise = [/^\\d+ (?:Benachrichtigung|Verbindung)/i, /^Start$/, /^Jobs$/, /^Nachrichten$/,
                /^Mein Netzwerk$/i, /^Ich$/i, /^linkedin$/i, /^Produkte$/i, /^weiter/i, /^skip/i, /^zum feed$/i,
                /bearbeiten|edit|hinzufügen|add|anzeigen|show|anzeige|zertifikat/i
            ];
            const isNoise = s => noise.some(p => p.test(s));

            // Find text after "Kenntnisse" or "Skills" heading, or just collect short lines
            let collecting = true;
            for (const line of lines) {
                if (/^(kenntnisse|skills|fähigkeiten)$/i.test(line)) { collecting = true; continue; }
                if (collecting) {
                    if (line.length > 60 || isNoise(line) || seen.has(line)) continue;
                    // Filter out non-skill lines
                    if (/^top-kenntnisse|^\\d+\\s*kenntnisbestätigung|kenntnisse$/i.test(line)) continue;
                    // Stop at non-skill sections
                    if (/^vorschläge|^analyse|^aktivität|^personen|^vielleicht|^info$/i.test(line)) break;
                    seen.add(line);
                    results.push(line);
                    if (results.length >= 50) break;
                }
            }
            // If heading not found, fall back to any short lines from main content
            if (results.length < 3) {
                for (const line of lines) {
                    if (line.length > 40 || line.length < 3 || isNoise(line) || seen.has(line)) continue;
                    if (/^[a-z]/.test(line)) continue;
                    if (/^(?:vorschläge|analyse|aktiv|info$|service|im fokus)/i.test(line)) break;
                    seen.add(line);
                    results.push(line);
                    if (results.length >= 50) break;
                }
            }
            return results;
        }""")

        if skills:
            # Navigate back to profile (for any subsequent operations)
            await page.goto(profile_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            return skills

    except Exception:
        pass

    # Fallback: get skills from main profile page (the 2 visible ones)
    await page.goto(profile_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    fields = await page.evaluate(PROFILE_PAGE_EXTRACTOR)
    return fields.get("skills", [])


def _deduplicate_text(text: str) -> str:
    """
    LinkedIn renders text twice in the DOM (once visible, once for screen readers).
    This creates strings like "Creator von High-X Creator von High-X".
    We detect and remove the duplicated half using a sliding-window approach.
    """
    if not text or len(text) < 10:
        return text
    mid = len(text) // 2
    # Try exact half-split
    for split in range(mid - 20, mid + 20):
        if 0 < split < len(text):
            first, second = text[:split], text[split:]
            if second.startswith(first[:30]):
                return first.strip()
    return text


PROFILE_PAGE_EXTRACTOR = """
() => {
    const HEADING_MAP = {
        'info': 'about', 'about': 'about', 'über': 'about',
        'zertifikate': 'certifications', 'certifications': 'certifications',
        'bescheinigungen': 'certifications', 'licens': 'certifications',
        'projekte': 'projects', 'projects': 'projects',
        'sprachen': 'languages', 'languages': 'languages',
        'ehrenamt': 'volunteering', 'volunteering': 'volunteering',
        'freiwillig': 'volunteering',
        'auszeichnungen': 'honors', 'honors': 'honors', 'preise': 'honors',
        'kenntnisse': 'skills', 'skills': 'skills', 'fähigkeiten': 'skills',
    };

    const seen = {};
    const getOrSet = (key) => { if (!seen[key]) seen[key] = new Set(); return seen[key]; };

    const result = {
        name: null, headline: null, location: null, about: null,
        connections: null, profile_picture: null,
        skills: [], certifications: [], projects: [], languages: [],
        volunteering: [], honors: []
    };

    // ── Top card: extract name, headline, location ────────────────────────
    const topCardSection = document.querySelector('section[componentkey*="MTopcard"]');
    if (topCardSection) {
        // Name is in the h2 heading of the top card
        const h2 = topCardSection.querySelector('h2');
        if (h2) {
            const n = h2.textContent.trim();
            if (n && n.length > 2 && n.length < 100) result.name = n;
        }
        // Headline: first <p> after the h2 with substantial text
        let h2Found = false;
        topCardSection.querySelectorAll('p').forEach(p => {
            const t = p.textContent.trim();
            if (!t || t.length < 5) return;
            if (!h2Found && t.length > 5 && t.length < 300 && !/bearbeiten|edit|hinzufügen|add/i.test(t)) {
                // Check if this IS the name (repeated in a <p> after h2)
                if (result.name && t.startsWith(result.name.substring(0, 10))) return;
                result.headline = t;
                h2Found = true;
            }
        });
        // Location: find element whose text content starts with a city pattern
        topCardSection.querySelectorAll('a, span, p').forEach(el => {
            const t = (el.textContent || '').trim();
            if (t && /^[A-Z\u00c4\u00d6\u00dc][a-z\u00e4\u00f6\u00fc\u00df\u00f6]{3,},\\s*(?:Deutschland|Germany|Austria|\u00d6sterreich|Schweiz)/.test(t)) {
                result.location = t.replace(/\\s+/g, ' ').trim();
            }
        });
    }

    // ── Profile picture ───────────────────────────────────────────────────
    const img = document.querySelector('img[src*="licdn"][alt*="Profil"], img[src*="licdn"][alt*="Profile"]');
    if (img) result.profile_picture = img.src;

    // ── Connections from mynetwork link ───────────────────────────────────
    const connMatch = document.body.innerText.match(/(\\d[\\d,.]*|\\d+\\+?)\\s*(followers?|Follower|connections?|Kontakte)/i);
    if (connMatch) result.connections = connMatch[0];
    else {
        const link = document.querySelector('a[href*="/mynetwork/"]');
        if (link) { const t = link.textContent.trim(); if (t && t.length < 50) result.connections = t; }
    }

    // ── Section-based extraction (via h2 headings) ────────────────────────
    document.querySelectorAll('section').forEach(card => {
        const hEl = card.querySelector('h2');
        if (!hEl) return;
        const heading = hEl.textContent.trim().toLowerCase();
        const key = Object.keys(HEADING_MAP).find(k => heading.includes(k));
        if (!key) return;
        const section = HEADING_MAP[key];

        if (section === 'about') {
            const originalHeading = hEl.textContent.trim();
            const txt = card.textContent.replace(originalHeading, '').trim().substring(0, 2000);
            if (txt && txt.length > 20) result.about = txt;
        }
        else if (section === 'certifications') {
            card.querySelectorAll('p').forEach(p => {
                const t = p.textContent.trim();
                if (t && !getOrSet('certs').has(t) && t.length > 2 && t.length < 200
                    && !t.toLowerCase().startsWith('zertifikat')) {
                    getOrSet('certs').add(t);
                    result.certifications.push({ name: t, issuer: null, issued: null, expires: null });
                }
            });
        }
        else if (section === 'projects') {
            card.querySelectorAll('p').forEach(p => {
                const t = p.textContent.trim();
                if (t && !getOrSet('proj').has(t) && t.length > 2 && t.length < 200
                    && !t.toLowerCase().startsWith('projekt')) {
                    getOrSet('proj').add(t);
                    result.projects.push({ name: t, description: null });
                }
            });
        }
        else if (section === 'languages') {
            card.querySelectorAll('p').forEach(p => {
                const t = p.textContent.trim();
                if (t && !getOrSet('lang').has(t) && t.length > 1 && t.length < 100
                    && !t.toLowerCase().match(/^(sprache|language)/)) {
                    getOrSet('lang').add(t);
                    const parts = t.split(/[•·|]/);
                    result.languages.push({ language: parts[0].trim(), proficiency: parts[1] ? parts[1].trim() : null });
                }
            });
        }
        else if (section === 'volunteering') {
            card.querySelectorAll('p').forEach(p => {
                const t = p.textContent.trim();
                if (t && !getOrSet('vol').has(t) && t.length > 2 && t.length < 200
                    && !t.toLowerCase().startsWith('ehrenamt') && !t.toLowerCase().startsWith('volunteer')) {
                    getOrSet('vol').add(t);
                    result.volunteering.push({ title: t, organization: null, description: null });
                }
            });
        }
        else if (section === 'skills') {
            card.querySelectorAll('p').forEach(p => {
                const t = p.textContent.trim();
                if (t && t.length > 1 && t.length < 80 && !getOrSet('skill').has(t)
                    && !/alle|anzeigen|anzeige|show|skill|kenntnis/i.test(t)) {
                    getOrSet('skill').add(t);
                    if (!result.skills) result.skills = [];
                    result.skills.push(t);
                }
            });
        }
        else if (section === 'honors') {
            card.querySelectorAll('p').forEach(p => {
                const t = p.textContent.trim();
                if (t && !getOrSet('hon').has(t) && t.length > 2 && t.length < 200
                    && !t.toLowerCase().startsWith('auszeichnung') && !t.toLowerCase().startsWith('honor')) {
                    getOrSet('hon').add(t);
                    result.honors.push({ title: t, issuer: null });
                }
            });
        }
    });

    return result;
}
"""


async def _field_experience(page, profile_url: str) -> list:
    """Navigate to /details/experience/ detail page and extract items."""
    base = profile_url.split("?")[0].rstrip("/")
    exp_url = f"{base}/details/experience/"
    try:
        await page.goto(exp_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await _dismiss_detail_consent(page)

        # Scroll to the bottom to force any lazy-loaded entries to render.
        try:
            for _ in range(5):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(500)
        except Exception:
            pass

        items = await page.evaluate("""() => {
            // DOM-based extraction: LinkedIn renders each experience as an
            // <a href=".../details/experience/..."> anchor whose <p> children
            // are title/company/period/location; the description lives in a
            // sibling span[data-testid="expandable-text-box"]. Entries whose
            // company has no LinkedIn page keep company/period/location as
            // sibling <p>s of the anchor. Widget noise (e.g. "Mit KI
            // optimieren") and side-panel text never appear inside these
            // anchors, so the old innerText line-parsing artifacts are gone.
            const results = [];
            const seen = new Set();
            const isPeriod = s => /(?:Jan|Feb|M\\u00e4r|Apr|Mai|Jun|Jul|Aug|Sep|Okt|Nov|Dez)\\.?\\s*\\d{4}|\\d{4}\\s*\\u2013|Heute|\\bJahr\\b|\\bMonat\\b/i.test(s) && s.length < 60;
            const isLocation = s => /(?:Hamburg|Berlin|M\\u00fcnchen|Frankfurt|K\\u00f6ln|Kiel|D\\u00fcsseldorf|Stuttgart|Deutschland|Remote|Vor Ort|Hybrid)\\b|\\u00b7\\s*(?:Vollzeit|Teilzeit|Hybrid|Remote|Freiberuflich|Selbstst\\u00e4ndig)/.test(s) && s.length < 90;
            const links = [...document.querySelectorAll('a[href*="/details/experience/"]')];
            for (const a of links) {
                const container = a.parentElement;
                if (!container) continue;
                const descTexts = new Set([...container.querySelectorAll('span[data-testid="expandable-text-box"]')].map(s => s.textContent.trim()));
                const ps = [...container.querySelectorAll('p')]
                    .map(p => p.textContent.trim()).filter(Boolean)
                    .filter(t => !descTexts.has(t) && t.length <= 300);
                const anchorPs = [...a.querySelectorAll('p')].map(p => p.textContent.trim()).filter(Boolean);
                if (!anchorPs.length) continue;
                const title = anchorPs[0];
                if (title.length < 2 || title.length > 90) continue;
                if (/^alle anzeigen|^show all/i.test(title)) continue;
                if (seen.has(title)) continue;
                seen.add(title);
                let company = null, period = null, location = null;
                let idx = ps.indexOf(title);
                if (idx < 0) idx = 0;
                idx += 1;
                if (idx < ps.length && !isPeriod(ps[idx]) && ps[idx].length < 90) { company = ps[idx]; idx++; }
                if (idx < ps.length && isPeriod(ps[idx])) { period = ps[idx]; idx++; }
                for (; idx < ps.length; idx++) { if (isLocation(ps[idx])) { location = ps[idx]; break; } }
                let description = null;
                for (const s of container.querySelectorAll('span[data-testid="expandable-text-box"]')) {
                    const t = s.textContent.trim();
                    if (!description || t.length > description.length) description = t;
                }
                results.push({ title, company, period, location, description });
            }
            return results;
        }""")
        return items
    except Exception:
        return []
    finally:
        try:
            await page.goto(profile_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
        except Exception:
            pass


async def _field_education(page, profile_url: str) -> list:
    """Navigate to /details/education/ detail page and extract items."""
    base = profile_url.split("?")[0].rstrip("/")
    edu_url = f"{base}/details/education/"
    try:
        await page.goto(edu_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await _dismiss_detail_consent(page)

        items = await page.evaluate("""() => {
            const raw = document.body.innerText;
            const headingMatch = raw.match(/(?:Ausbildung|Bildung|Education)(?:\\n|$)/i);
            if (!headingMatch) return [];
            const afterHeading = raw.substring(headingMatch.index + headingMatch[0].length);
            const blocks = afterHeading.split('\\n').map(l => l.trim()).filter(l => l.length > 2);
            const seen = new Set();
            const results = [];
            for (let i = 0; i < blocks.length - 1; i += 2) {
                const institution = blocks[i];
                const degree = blocks[i + 1] || null;
                const key = institution + '|' + (degree || '');
                if (seen.has(key)) continue;
                seen.add(key);
                // Skip if institution looks like a description
                if (!institution || institution.length < 3 || institution.length > 80) continue;
                if (/^[a-z]/.test(institution)) continue;
                if (/bearbeiten|edit|hinzufügen|add|anzeigen|show|^durch|^hier|^als|^mit/i.test(institution)) continue;
                // Skip period lines
                if (/^\\d{4}[-–]/.test(institution) || /^[A-Z][a-z]{2,}\\.?\\s+\\d{4}/.test(institution)) continue;
                // Skip single-word language entries
                if (/^(?:Deutsch|English|Französisch|Spanisch|Chinesisch|Italienisch|Portugiesisch|Russisch|Japanisch)$/i.test(institution)) continue;
                // Check that degree also looks valid (not a description)
                if (degree && degree.length > 80) degree = null;
                if (degree && /^[a-z]/.test(degree)) degree = null;
                results.push({ institution, degree, period: null, note: null });
                if (results.length >= 3) break;
            }
            return results;
        }""")
        return items
    except Exception:
        return []
    finally:
        try:
            await page.goto(profile_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
        except Exception:
            pass


async def _dismiss_detail_consent(page):
    """Dismiss any consent dialogs on detail pages."""
    for sel in ['button[action-type="ACCEPT"]', '#onetrust-accept-btn-handler',
                'button:has-text("Alle akzeptieren")', 'button:has-text("Akzeptieren")',
                'button:has-text("Accept all")', 'button:has-text("Accept")']:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click(timeout=1500)
                await page.wait_for_timeout(900)
        except Exception:
            pass


async def _field_connections(page) -> str | None:
    return await page.evaluate("""() => {
        const match = document.body.innerText.match(/(\\d[\\d,.]*|\\d+\\+?)\\s*(followers?|Follower|connections?|Kontakte)/i);
        if (match) return match[0];
        const link = document.querySelector('a[href*="/mynetwork/"]');
        if (link) { const t = link.textContent.trim(); if (t && t.length < 50) return t; }
        return null;
    }""")


async def _field_profile_picture(page) -> str | None:
    return await page.evaluate("""() => {
        const img = document.querySelector('img[src*="licdn"][alt*="Profil"], img[src*="licdn"][alt*="Profile"]');
        return img ? img.src : null;
    }""")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "browser_open": browser_mgr.browser is not None if browser_mgr else False,
    }


@app.get("/check")
async def check():
    if not browser_mgr:
        return {"error": NOT_READY}
    async with browser_mgr.page_lock:
        logged_in = await browser_mgr.check_login()
        if not logged_in and browser_mgr.session_manager.has_valid_session():
            try:
                await browser_mgr._goto_with_fallback(f"{settings.LINKEDIN_URL}/feed/")
                await browser_mgr.page.wait_for_timeout(1200)
                await browser_mgr.dismiss_cookie_consent()
                await browser_mgr.page.wait_for_timeout(700)
                logged_in = await browser_mgr.check_login()
                if not logged_in:
                    await browser_mgr._goto_with_fallback(f"{settings.LINKEDIN_URL}/in/me/")
                    await browser_mgr.page.wait_for_timeout(900)
                    current_url = browser_mgr.page.url
                    logged_in = (
                        "linkedin.com" in current_url
                        and not any(seg in current_url for seg in ["/login", "/authwall", "/checkpoint"])
                    )
            except Exception:
                pass
        current_url = browser_mgr.page.url if browser_mgr and browser_mgr.page else ""
        if not logged_in and "linkedin.com" in current_url:
            if not any(seg in current_url for seg in ["/login", "/authwall", "/checkpoint"]):
                logged_in = True
        if logged_in:
            await browser_mgr.save()
        return {
            "logged_in": logged_in,
            "session_saved": browser_mgr.session_manager.has_valid_session(),
            "current_url": current_url,
        }


@app.post("/login")
async def login():
    if not browser_mgr:
        return {"error": NOT_READY}
    async with browser_mgr.page_lock:
        if await browser_mgr.check_login():
            await browser_mgr.save()
            return {"logged_in": True, "message": "Already logged in"}
        await browser_mgr._goto_with_fallback(settings.LINKEDIN_LOGIN_URL)
        await browser_mgr.dismiss_cookie_consent()
        return {
            "logged_in": False,
            "message": "Browser open at login page. Log in manually – session auto-saves.",
        }


@app.post("/save")
async def save():
    if not browser_mgr:
        return {"error": NOT_READY}
    async with browser_mgr.page_lock:
        if await browser_mgr.check_login():
            await browser_mgr.save()
            return {"saved": True}
        return {"saved": False, "error": "Not logged in"}


@app.delete("/session")
async def clear():
    if not browser_mgr:
        return {"error": NOT_READY}
    browser_mgr.session_manager.clear_session()
    return {"cleared": True}


@app.get("/debug")
async def debug():
    """Returns raw HTML of the profile page (for diagnostic purposes)."""
    if not browser_mgr or not browser_mgr.page:
        return {"error": NOT_READY}
    async with browser_mgr.page_lock:
        try:
            await browser_mgr.navigate_to_profile()
        except Exception as e:
            return {"error": f"Navigation failed: {str(e)}"}
        html = await browser_mgr.page.content()
        return {"html": html, "url": browser_mgr.page.url, "length": len(html)}


@app.get("/debug/selectors")
async def debug_selectors():
    """
    Diagnostic endpoint: shows which DOM sections were found on the profile page.
    Use this to diagnose null-value issues without running the full scrape.
    Does NOT return profile data – only presence/absence of expected elements.
    """
    if not browser_mgr or not browser_mgr.page:
        return {"error": NOT_READY}

    async with browser_mgr.page_lock:
        if not await browser_mgr.check_login():
            return {"error": "Not logged in"}

        await browser_mgr.navigate_to_profile()

        report = await browser_mgr.page.evaluate("""() => {
            const check = (label, selector) => {
                const el = document.querySelector(selector);
                return {
                    found: !!el,
                    text_preview: el ? el.textContent.trim().substring(0, 80) : null
                };
            };
            // Detect sections by SDUI component key
            const sduiSections = {};
            document.querySelectorAll('section[componentkey]').forEach(s => {
                const key = s.getAttribute('componentkey');
                const h2 = s.querySelector('h2');
                sduiSections[key] = {
                    h2_text: h2 ? h2.textContent.trim().substring(0, 60) : null,
                    text_preview: s.textContent.trim().substring(0, 60)
                };
            });
            // Detect sections by h2 heading text (localized)
            const headingSections = {};
            document.querySelectorAll('section').forEach(s => {
                const h2 = s.querySelector('h2');
                if (h2) headingSections[h2.textContent.trim().toLowerCase()] =
                    s.textContent.trim().substring(0, 60);
            });
            return {
                profile_url:   window.location.href,
                topcard:       check('MTopcard', 'section[componentkey*="MTopcard"]'),
                about:         check('MAbout', 'section[componentkey*="MAbout"]'),
                services:      check('MServices', 'section[componentkey*="MServices"]'),
                featured:      check('MFeatured', 'section[componentkey*="MFeatured"]'),
                mynetwork:     check('connections', 'a[href*="/mynetwork/"]'),
                h2_headings:   headingSections,
                component_keys: sduiSections,
            };
        }""")

        return {"url": browser_mgr.page.url, "selectors": report}


@app.get("/debug/dom")
async def debug_dom():
    """
    Lightweight DOM inspector: returns every section's id, aria-label, and first
    50 chars of text. Plus all unique aria-labels on the page.
    Use this to discover the real selectors LinkedIn is currently using.
    """
    if not browser_mgr or not browser_mgr.page:
        return {"error": NOT_READY}
    async with browser_mgr.page_lock:
        if not await browser_mgr.check_login():
            return {"error": "Not logged in"}

        nav_ok = await browser_mgr.navigate_to_profile()
        if not nav_ok:
            return {"error": "Session expired"}

        result = await browser_mgr.page.evaluate("""() => {
            // All sections with any attribute
            const sections = Array.from(document.querySelectorAll('section')).map(s => ({
                id:         s.id || null,
                aria_label: s.getAttribute('aria-label') || null,
                data_section: s.getAttribute('data-section') || null,
                class_snippet: s.className ? s.className.substring(0, 60) : null,
                text_preview: s.textContent.trim().substring(0, 60)
            }));

            // All unique aria-labels on the page
            const allAria = [...new Set(
                Array.from(document.querySelectorAll('[aria-label]'))
                    .map(el => el.getAttribute('aria-label'))
                    .filter(Boolean)
            )].slice(0, 60);

            // All section IDs
            const sectionIds = Array.from(document.querySelectorAll('[id]'))
                .map(el => el.id)
                .filter(id => id && id.length > 2)
                .filter(id => /about|experience|education|skill|feature|project|recommend|language|certif/i.test(id))
                .slice(0, 30);

            // Check if JSON-LD exists
            const ldJsonScripts = Array.from(document.querySelectorAll("script[type='application/ld+json']"))
                .map(s => { try { return JSON.parse(s.textContent); } catch(e) { return null; } })
                .filter(Boolean);

            return { sections, allAria, sectionIds, ldJsonScripts };
        }""")

        return {"url": browser_mgr.page.url, "dom": result}


@app.get("/profile")
async def profile(lang: str = "de"):
    """
    Main endpoint consumed by LM Studio.
    lang: 'de' (default) | 'en' | 'both'
      - 'de'   → German profile (linkedin.com/in/me/)
      - 'en'   → English profile (linkedin.com/in/me/?locale=en_US)
      - 'both' → Both profiles returned as { "de": {...}, "en": {...} }
    """
    if not browser_mgr:
        return {"error": NOT_READY}
    async with browser_mgr.page_lock:
        if not await browser_mgr.check_login():
            if browser_mgr.session_manager.has_valid_session():
                try:
                    await browser_mgr._goto_with_fallback(f"{settings.LINKEDIN_URL}/feed/")
                    await browser_mgr.page.wait_for_timeout(2000)
                except Exception:
                    pass
            if not await browser_mgr.check_login():
                return {"error": "Not logged in. Call POST /login first."}

        # Determine locales to scrape
        locales = ["de", "en"] if lang == "both" else [lang if lang in ("de", "en") else "de"]

        results = {}
        for locale in locales:
            try:
                nav_ok = await browser_mgr.navigate_to_profile(locale=locale)
            except TargetClosedError:
                # Browser window was closed underneath us – relaunch and retry once.
                if not await browser_mgr.restart():
                    return {"error": "Browser closed and relaunch failed. Call POST /login."}
                nav_ok = False
            if not nav_ok:
                try:
                    recovered = await browser_mgr.recover_from_consent(locale=locale)
                except TargetClosedError:
                    if not await browser_mgr.restart():
                        return {"error": "Browser closed and relaunch failed. Call POST /login."}
                    recovered = False
                if recovered:
                    nav_ok = await browser_mgr.navigate_to_profile(locale=locale)
            if not nav_ok:
                return {
                    "error": "Session expired. LinkedIn redirected to login/authwall. "
                             "Please call POST /login to re-authenticate."
                }
            if await browser_mgr.has_cookie_banner():
                await browser_mgr.dismiss_cookie_consent()
                await browser_mgr.page.wait_for_timeout(900)
            try:
                data = await _scrape_profile_data(browser_mgr.page)
                results[locale] = data
            except Exception as e:
                error_message = str(e)
                if "Cookie consent page detected" in error_message:
                    recovered = await browser_mgr.recover_from_consent(locale=locale)
                    if recovered:
                        retry_nav_ok = await browser_mgr.navigate_to_profile(locale=locale)
                        if retry_nav_ok:
                            try:
                                results[locale] = await _scrape_profile_data(browser_mgr.page)
                                continue
                            except Exception as retry_error:
                                error_message = str(retry_error)
                results[locale] = {"error": f"Scraping failed: {error_message}"}

        await browser_mgr.save()

        # For single-language requests, return the data directly (backwards-compatible)
        if lang != "both":
            return results.get(lang, results.get("de", {}))

        return results


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8766))
    uvicorn.run(app, host="0.0.0.0", port=port)
