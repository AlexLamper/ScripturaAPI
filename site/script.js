function showEl(id, show) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.toggle('hidden', !show);
}

function showProSubscribeCards(show) {
    showEl('proSubscribeMonthlyCard', show);
    showEl('proSubscribeYearlyCard', show);
}

const BIJBELAPI_LS_API_KEY = 'bijbelapi_api_key';
const BIJBELAPI_LS_BILLING_EMAIL = 'bijbelapi_billing_email';

function looksLikeBijbelapiKey(v) {
    const s = String(v || '').trim();
    return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(s);
}

/**
 * Sla ingevoerde sleutel op in localStorage en ververs Pro-UI. Optioneel: silent (geen fout bij half typen).
 */
async function persistSavedApiKeyFromInput(savedApiInput, options = {}) {
    const { requireValidEntry = false, silent = false } = options;
    if (!savedApiInput) return false;
    const v = savedApiInput.value.trim();
    const hint = document.getElementById('savedApiKeyHint');

    if (!v) {
        if (requireValidEntry && hint) {
            hint.classList.remove('hidden');
            hint.textContent = 'Plak eerst je API-sleutel.';
        }
        return false;
    }
    if (!looksLikeBijbelapiKey(v)) {
        if (!silent && hint) {
            hint.classList.remove('hidden');
            hint.textContent =
                'Ongeldig formaat. Verwacht een UUID (bijv. 1cf235a5-2c34-4ae3-9152-49ee4c54547f).';
        }
        return false;
    }
    try {
        localStorage.setItem(BIJBELAPI_LS_API_KEY, v);
    } catch {
        if (hint) {
            hint.classList.remove('hidden');
            hint.textContent =
                'Lokaal opslaan mislukt (privacy-modus, quota of browser blokkeert storage).';
        }
        return false;
    }
    if (hint) {
        hint.classList.add('hidden');
        hint.textContent = '';
    }
    await refreshBillingUiFromSavedKey();
    return true;
}

function stripUrlQueryParam(param) {
    const url = new URL(window.location.href);
    if (!url.searchParams.has(param)) return;
    url.searchParams.delete(param);
    const q = url.searchParams.toString();
    window.history.replaceState({}, '', `${url.pathname}${q ? `?${q}` : ''}${url.hash}`);
}

async function pollCheckoutSessionForApiKey(sessionId) {
    const valEl = document.getElementById('checkoutKeyRevealValue');
    const maxAttempts = 24;
    for (let i = 0; i < maxAttempts; i += 1) {
        try {
            const res = await fetch(
                `/billing/checkout-success?session_id=${encodeURIComponent(sessionId)}`,
                { headers: { Accept: 'application/json' } },
            );
            const data = await res.json().catch(() => ({}));
            if (res.ok && data.ready && data.api_key) {
                if (valEl) valEl.textContent = data.api_key;
                showEl('checkoutKeyReveal', true);
                if (data.billing_email) {
                    localStorage.setItem(BIJBELAPI_LS_BILLING_EMAIL, data.billing_email);
                    const pe = document.getElementById('portalEmail');
                    if (pe) pe.value = data.billing_email;
                }
                const remember = document.getElementById('chkRememberRevealedKey');
                if (!remember || remember.checked) {
                    localStorage.setItem(BIJBELAPI_LS_API_KEY, data.api_key);
                    const inp = document.getElementById('savedApiKeyInput');
                    if (inp) inp.value = data.api_key;
                }
                stripUrlQueryParam('session_id');
                await refreshBillingUiFromSavedKey();
                return;
            }
            billingDebugLog(`checkout-success poll attempt=${i + 1} ready=${Boolean(data.ready)}`);
        } catch (e) {
            billingDebugLog(`poll checkout-success error ${e && e.message ? e.message : String(e)}`);
        }
        await new Promise((r) => setTimeout(r, 750));
    }
}

async function refreshBillingUiFromSavedKey() {
    let key = '';
    try {
        key = localStorage.getItem(BIJBELAPI_LS_API_KEY) || '';
    } catch {
        const hint = document.getElementById('savedApiKeyHint');
        if (hint) {
            hint.classList.remove('hidden');
            hint.textContent = 'Kan geen sleutel lezen uit lokale opslag (browser of privacy-modus).';
        }
        return;
    }
    const inp = document.getElementById('savedApiKeyInput');
    if (inp && !inp.value && key) inp.value = key;
    const hint = document.getElementById('savedApiKeyHint');

    if (!key) {
        showEl('billingProActivePanel', false);
        showEl('checkoutEmailBlock', true);
        showProSubscribeCards(true);
        showEl('proFairUseFootnote', true);
        if (hint) {
            hint.classList.add('hidden');
            hint.textContent = '';
        }
        return;
    }

    try {
        const res = await fetch('/billing/status', {
            headers: { Accept: 'application/json', 'x-api-key': key },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.active) {
            showEl('billingProActivePanel', false);
            showEl('checkoutEmailBlock', true);
            showProSubscribeCards(true);
            showEl('proFairUseFootnote', true);
            if (hint) {
                hint.classList.remove('hidden');
                hint.textContent =
                    res.status === 404
                        ? 'API-sleutel onbekend. Controleer of je de juiste sleutel plaatst.'
                        : 'Geen actief Pro-abonnement voor deze sleutel.';
            }
            return;
        }

        const detail = document.getElementById('billingProActiveDetail');
        if (detail) {
            const em = data.email_masked || localStorage.getItem(BIJBELAPI_LS_BILLING_EMAIL) || '';
            detail.textContent = `Plan: ${data.plan || 'pro'}. Status: ${data.status || 'active'}.${em ? ` Account: ${em}.` : ''}`;
        }
        showEl('billingProActivePanel', true);
        showEl('checkoutEmailBlock', false);
        showProSubscribeCards(false);
        showEl('proFairUseFootnote', false);
        const pem = localStorage.getItem(BIJBELAPI_LS_BILLING_EMAIL);
        const portalEmail = document.getElementById('portalEmail');
        if (portalEmail && pem && !portalEmail.value) portalEmail.value = pem;
        if (hint) {
            hint.classList.remove('hidden');
            hint.textContent = 'Pro actief voor opgeslagen sleutel.';
        }
    } catch {
        if (hint) {
            hint.classList.remove('hidden');
            hint.textContent = 'Kon status niet laden.';
        }
    }
}

const BILLING_DEBUG_ENABLED = new URLSearchParams(window.location.search).get('debug') === '1';
function billingDebugLog(message) {
    const force = String(message || '').indexOf('[ERR]') !== -1;
    if (!BILLING_DEBUG_ENABLED && !force) return;
    const debugEl = document.getElementById('billingDebugLog');
    const line = `${new Date().toISOString()} ${message}`;
    if (debugEl) {
        debugEl.classList.remove('hidden');
        debugEl.textContent += `${line}\n`;
    }
    if (force) {
        // Always mirror hard errors to browser console for production diagnostics.
        // eslint-disable-next-line no-console
        console.error(`[billing] ${line}`);
    }
}

function stripeErrorMessage(data, res) {
    if (data && typeof data.message === 'string') return data.message;
    if (data && typeof data.detail === 'string') return data.detail;
    if (data && data.detail) return JSON.stringify(data.detail);
    return `Er ging iets mis (${res.status}).`;
}

function setButtonLoading(btn, loading, labelBusy) {
    if (!btn) return;
    if (loading) {
        if (!btn.dataset.labelIdle) btn.dataset.labelIdle = btn.textContent.trim();
        btn.disabled = true;
        btn.textContent = labelBusy;
        btn.setAttribute('aria-busy', 'true');
    } else {
        btn.disabled = false;
        if (btn.dataset.labelIdle) btn.textContent = btn.dataset.labelIdle;
        btn.removeAttribute('aria-busy');
    }
}

// --- Checkout Modal ---
let _modalCurrentPlan = 'pro_monthly';

function openCheckoutModal(plan) {
    _modalCurrentPlan = plan || 'pro_monthly';
    window._modalCurrentPlan = _modalCurrentPlan;
    const modal = document.getElementById('checkoutModal');
    if (!modal) { startStripeCheckout(_modalCurrentPlan, null); return; }
    const planLabel = document.getElementById('modalPlanLabel');
    if (planLabel) {
        planLabel.textContent = _modalCurrentPlan === 'pro_yearly'
            ? 'Pro Jaarlijks — €99,99 / jaar'
            : 'Pro Maandelijks - €9,99 / maand';
    }
    const emailInput = document.getElementById('modalCheckoutEmail');
    if (emailInput) {
        try { const s = localStorage.getItem(BIJBELAPI_LS_BILLING_EMAIL); emailInput.value = s || ''; } catch { emailInput.value = ''; }
    }
    const errEl = document.getElementById('modalCheckoutError');
    if (errEl) { errEl.textContent = ''; errEl.classList.add('hidden'); }
    if (!modal.open) modal.showModal();
    if (emailInput) setTimeout(() => emailInput.focus(), 50);
}

function closeCheckoutModal() {
    const modal = document.getElementById('checkoutModal');
    if (modal && modal.open) modal.close();
}

function openPortalModal() {
    const modal = document.getElementById('portalModal');
    if (!modal) { openStripePortal(); return; }
    const emailInput = document.getElementById('portalModalEmail');
    if (emailInput) {
        try { const s = localStorage.getItem(BIJBELAPI_LS_BILLING_EMAIL); emailInput.value = s || ''; } catch { emailInput.value = ''; }
    }
    const errEl = document.getElementById('portalModalError');
    if (errEl) { errEl.textContent = ''; errEl.classList.add('hidden'); }
    if (!modal.open) modal.showModal();
    if (emailInput) setTimeout(() => emailInput.focus(), 50);
}

function closePortalModal() {
    const modal = document.getElementById('portalModal');
    if (modal && modal.open) modal.close();
}

window.openCheckoutModal = openCheckoutModal;
window.closeCheckoutModal = closeCheckoutModal;
window.openPortalModal = openPortalModal;
window.closePortalModal = closePortalModal;
window.startStripeCheckout = startStripeCheckout;
window.openStripePortal = openStripePortal;

async function startStripeCheckout(plan, emailArg) {
    billingDebugLog(`startStripeCheckout plan=${plan}`);
    const modalOpen = document.getElementById('checkoutModal')?.open;
    const errEl = modalOpen
        ? document.getElementById('modalCheckoutError')
        : document.getElementById('checkoutError');
    if (errEl) { errEl.textContent = ''; errEl.classList.add('hidden'); }

    let email = (typeof emailArg === 'string') ? emailArg.trim() : null;
    if (email === null) {
        const modalEmailEl = document.getElementById('modalCheckoutEmail');
        const inlineEmailEl = document.getElementById('checkoutEmail');
        const el = (modalOpen && modalEmailEl) ? modalEmailEl : (inlineEmailEl || modalEmailEl);
        email = el ? el.value.trim() : '';
    }

    billingDebugLog(`checkout email present=${Boolean(email)} valid=${Boolean(email && email.includes('@'))}`);
    if (!email || !email.includes('@')) {
        if (errEl) {
            errEl.textContent = 'Vul een geldig e-mailadres in voor Checkout.';
            errEl.classList.remove('hidden');
        }
        billingDebugLog('checkout blocked: invalid email');
        return;
    }
    if (errEl) {
        errEl.textContent = 'Doorsturen naar Stripe Checkout...';
        errEl.classList.remove('hidden');
    }
    const btn = modalOpen
        ? document.getElementById('btnModalPay')
        : (plan === 'pro_yearly'
            ? document.getElementById('btnCheckoutYearly')
            : document.getElementById('btnCheckoutMonthly'));
    setButtonLoading(btn, true, 'Bezig…');
    try {
        billingDebugLog('sending POST /billing/checkout-session');
        const res = await fetch('/billing/checkout-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ email, plan }),
        });
        const data = await res.json().catch(() => ({}));
        billingDebugLog(`checkout response status=${res.status} ok=${res.ok} keys=${Object.keys(data || {}).join(',')}`);
        if (!res.ok) {
            billingDebugLog(`[ERR] checkout non-ok status=${res.status} detail=${stripeErrorMessage(data, res)}`);
            if (errEl) {
                errEl.textContent = stripeErrorMessage(data, res);
                errEl.classList.remove('hidden');
            }
            return;
        }
        if (data.checkout_url) {
            billingDebugLog('redirecting to Stripe checkout URL');
            window.location.href = data.checkout_url;
            return;
        }
        if (errEl) {
            errEl.textContent = 'Geen checkout-URL ontvangen. Controleer de Stripe-configuratie.';
            errEl.classList.remove('hidden');
        }
    } catch (e) {
        billingDebugLog(`[ERR] checkout fetch failed: ${e && e.message ? e.message : String(e)}`);
        if (errEl) {
            errEl.textContent =
                'Netwerkfout of geen verbinding met de server. Controleer of je op de live site zit (niet file://) en of de API bereikbaar is.';
            errEl.classList.remove('hidden');
        }
    } finally {
        billingDebugLog('checkout flow finished');
        setButtonLoading(document.getElementById('btnModalPay'), false);
        setButtonLoading(document.getElementById('btnCheckoutMonthly'), false);
        setButtonLoading(document.getElementById('btnCheckoutYearly'), false);
    }
}

async function openStripePortal() {
    billingDebugLog('openStripePortal clicked');
    const errEl = document.getElementById('portalError');
    if (errEl) {
        errEl.textContent = '';
        errEl.classList.add('hidden');
    }
    const emailInput = document.getElementById('portalEmail') || document.getElementById('portalModalEmail');
    const email = emailInput ? emailInput.value.trim() : '';
    if (!email || !email.includes('@')) {
        if (errEl) {
            errEl.textContent = 'Vul het e-mailadres in waarmee je Pro hebt afgenomen.';
            errEl.classList.remove('hidden');
        }
        return;
    }
    const btn = document.getElementById('btnPortal');
    setButtonLoading(btn, true, 'Bezig…');
    try {
        billingDebugLog('sending POST /billing/portal-session');
        const res = await fetch('/billing/portal-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ email }),
        });
        const data = await res.json().catch(() => ({}));
        billingDebugLog(`portal response status=${res.status} ok=${res.ok}`);
        if (!res.ok) {
            billingDebugLog(`[ERR] portal non-ok status=${res.status} detail=${stripeErrorMessage(data, res)}`);
            if (errEl) {
                errEl.textContent = stripeErrorMessage(data, res);
                errEl.classList.remove('hidden');
            }
            return;
        }
        if (data.portal_url) {
            billingDebugLog('redirecting to Stripe portal URL');
            window.location.href = data.portal_url;
            return;
        }
        if (errEl) {
            errEl.textContent = 'Geen portaal-URL ontvangen.';
            errEl.classList.remove('hidden');
        }
    } catch (e) {
        billingDebugLog(`[ERR] portal fetch failed: ${e && e.message ? e.message : String(e)}`);
        if (errEl) {
            errEl.textContent = 'Netwerkfout. Probeer het later opnieuw.';
            errEl.classList.remove('hidden');
        }
    } finally {
        setButtonLoading(btn, false);
    }
}

function initBillingAndBanners() {
    if (window.__billingInitDone) return;
    window.__billingInitDone = true;
    billingDebugLog('initBillingAndBanners start');
    const params = new URLSearchParams(window.location.search);
    if (params.get('checkout') === 'success') showEl('checkoutBanner', true);
    if (params.get('checkout') === 'cancelled') showEl('checkoutCancelledBanner', true);
    const sessionId = params.get('session_id');
    if (params.get('checkout') === 'success' && sessionId) {
        pollCheckoutSessionForApiKey(sessionId);
    }

    const monthlyBtn = document.getElementById('btnCheckoutMonthly');
    const yearlyBtn = document.getElementById('btnCheckoutYearly');
    const portalBtn = document.getElementById('btnPortal');

    if (monthlyBtn) monthlyBtn.addEventListener('click', () => openCheckoutModal('pro_monthly'));
    if (yearlyBtn) yearlyBtn.addEventListener('click', () => openCheckoutModal('pro_yearly'));
    if (portalBtn) portalBtn.addEventListener('click', openPortalModal);
    billingDebugLog(`billing buttons found monthly=${Boolean(monthlyBtn)} yearly=${Boolean(yearlyBtn)} portal=${Boolean(portalBtn)}`);

    // Additional portal/checkout trigger buttons (header, footer, text link)
    const btnPortalTextLink = document.getElementById('btnPortalTextLink');
    if (btnPortalTextLink) btnPortalTextLink.addEventListener('click', openPortalModal);
    const btnHeaderPro = document.getElementById('btnHeaderPro');
    if (btnHeaderPro) btnHeaderPro.addEventListener('click', () => openCheckoutModal('pro_monthly'));
    const btnMobilePro = document.getElementById('btnMobilePro');
    if (btnMobilePro) btnMobilePro.addEventListener('click', () => {
        const menu = document.getElementById('mobileMenu');
        if (menu) menu.classList.add('hidden');
        openCheckoutModal('pro_monthly');
    });

    // Checkout modal wiring
    const checkoutModal = document.getElementById('checkoutModal');
    const btnModalPay = document.getElementById('btnModalPay');
    const btnModalClose = document.getElementById('btnModalClose');
    if (btnModalPay) btnModalPay.addEventListener('click', () => startStripeCheckout(_modalCurrentPlan, null));
    if (btnModalClose) btnModalClose.addEventListener('click', closeCheckoutModal);
    if (checkoutModal) {
        checkoutModal.addEventListener('click', (e) => { if (e.target === checkoutModal) closeCheckoutModal(); });
        const emailEl = document.getElementById('modalCheckoutEmail');
        if (emailEl) {
            emailEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); startStripeCheckout(_modalCurrentPlan, null); } });
        }
    }

    // Portal modal wiring
    const portalModal = document.getElementById('portalModal');
    const btnPortalModalSubmit = document.getElementById('btnPortalModalSubmit');
    const btnPortalModalClose = document.getElementById('btnPortalModalClose');
    if (btnPortalModalSubmit) {
        btnPortalModalSubmit.addEventListener('click', async () => {
            const emailEl = document.getElementById('portalModalEmail');
            const errEl = document.getElementById('portalModalError');
            const email = emailEl ? emailEl.value.trim() : '';
            if (!email || !email.includes('@')) {
                if (errEl) { errEl.textContent = 'Vul het e-mailadres in waarmee je Pro hebt afgenomen.'; errEl.classList.remove('hidden'); }
                return;
            }
            const btn = document.getElementById('btnPortalModalSubmit');
            setButtonLoading(btn, true, 'Bezig…');
            try {
                const res = await fetch('/billing/portal-session', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                    body: JSON.stringify({ email }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    if (errEl) { errEl.textContent = stripeErrorMessage(data, res); errEl.classList.remove('hidden'); }
                    return;
                }
                if (data.portal_url) { closePortalModal(); window.location.href = data.portal_url; return; }
                if (errEl) { errEl.textContent = 'Geen portal-URL ontvangen.'; errEl.classList.remove('hidden'); }
            } catch {
                if (errEl) { errEl.textContent = 'Netwerkfout. Probeer het later opnieuw.'; errEl.classList.remove('hidden'); }
            } finally {
                setButtonLoading(btn, false);
            }
        });
    }
    if (btnPortalModalClose) btnPortalModalClose.addEventListener('click', closePortalModal);
    if (portalModal) {
        portalModal.addEventListener('click', (e) => { if (e.target === portalModal) closePortalModal(); });
        const emailEl = document.getElementById('portalModalEmail');
        if (emailEl) {
            emailEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); document.getElementById('btnPortalModalSubmit')?.click(); } });
        }
    }

    if (BILLING_DEBUG_ENABLED) {
        fetch('/billing/debug/ping', { headers: { Accept: 'application/json' } })
            .then((r) => r.json().then((data) => ({ status: r.status, data })))
            .then(({ status, data }) => {
                billingDebugLog(`debug ping status=${status} ok=${Boolean(data && data.ok)} billing_enforced=${Boolean(data && data.billing_enforced)}`);
            })
            .catch((err) => {
                billingDebugLog(`debug ping failed: ${err && err.message ? err.message : String(err)}`);
            });
    }

    const savedApiInput = document.getElementById('savedApiKeyInput');
    let storedKey = null;
    try {
        storedKey = localStorage.getItem(BIJBELAPI_LS_API_KEY);
    } catch {
        storedKey = null;
    }
    if (savedApiInput && storedKey) savedApiInput.value = storedKey;

    const btnSaveLocal = document.getElementById('btnSaveApiKeyLocal');
    if (btnSaveLocal) {
        btnSaveLocal.addEventListener('click', async () => {
            await persistSavedApiKeyFromInput(savedApiInput, { requireValidEntry: true });
        });
    }
    if (savedApiInput) {
        savedApiInput.addEventListener('blur', () => {
            persistSavedApiKeyFromInput(savedApiInput, { silent: true });
        });
        savedApiInput.addEventListener('paste', () => {
            setTimeout(() => persistSavedApiKeyFromInput(savedApiInput, { silent: true }), 0);
        });
    }

    const btnClearLocal = document.getElementById('btnClearSavedApiKey');
    if (btnClearLocal) {
        btnClearLocal.addEventListener('click', async () => {
            try {
                localStorage.removeItem(BIJBELAPI_LS_API_KEY);
                localStorage.removeItem(BIJBELAPI_LS_BILLING_EMAIL);
            } catch {
                /* ignore */
            }
            if (savedApiInput) savedApiInput.value = '';
            await refreshBillingUiFromSavedKey();
        });
    }

    const btnCopyKey = document.getElementById('btnCopyRevealedKey');
    if (btnCopyKey) {
        btnCopyKey.addEventListener('click', async () => {
            const el = document.getElementById('checkoutKeyRevealValue');
            const text = el ? el.textContent.trim() : '';
            if (!text) return;
            try {
                await navigator.clipboard.writeText(text);
            } catch {
                /* ignore */
            }
        });
    }

    void refreshBillingUiFromSavedKey();
}

/** Ook aanroepbaar vanuit inline fallback op de homepage (`bind()` in index.html). */
window.persistSavedApiKeyFromInput = persistSavedApiKeyFromInput;
window.refreshBillingUiFromSavedKey = refreshBillingUiFromSavedKey;

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function stripHtmlAndTrim(value) {
    const raw = String(value || '');
    const noTags = raw.replace(/<[^>]*>/g, ' ');
    return noTags.replace(/\s+/g, ' ').trim();
}

/** Eén zin voor kaarten (eerste punt / uitroepteken / vraagteken); anders inhoud capped. */
function firstSentenceFromDescription(raw) {
    const t = stripHtmlAndTrim(raw);
    if (!t) return '';
    const m = t.match(/^(.+?[.!?])(\s+|$)/);
    if (m) return m[1].trim();
    if (t.length > 220) return `${t.slice(0, 217).trim()}…`;
    return t;
}

function versionsDisplayTitle(key, apiName, fallbackKey) {
    const k = String(key || '').toLowerCase();
    if (k === 'sv') return 'Statenvertaling (sv)';
    if (k === 'hs1917') return 'De Heilige Schrift 1917 (hs1917)';
    if (k === 'canisius') return 'Canisiusbijbel (canisius)';
    return String(apiName || fallbackKey || '');
}

function versionsDisplayDescription(key, apiDescription) {
    const k = String(key || '').toLowerCase();
    if (k === 'sv') return 'De Nederlandse Statenvertaling';
    if (k === 'hs1917') return 'De Heilige Schrift 1917, Rooms-Katholieke bijbelvertaling';
    if (k === 'canisius') return 'De Canisiusbijbel, Rooms-Katholieke vertaling uit 1939';
    const one = firstSentenceFromDescription(apiDescription);
    return one || 'Geen beschrijving beschikbaar.';
}

async function loadAvailableVersions() {
    const loadingEl = document.getElementById('versionsLoading');
    const errorEl = document.getElementById('versionsError');
    const gridEl = document.getElementById('versionsGrid');
    const debugEl = document.getElementById('versionsDebug');
    if (!loadingEl || !errorEl || !gridEl) return;

    const debugEnabled = new URLSearchParams(window.location.search).get('debug') === '1';
    const startedAt = Date.now();
    const debugLog = (msg) => {
        if (!debugEnabled || !debugEl) return;
        debugEl.classList.remove('hidden');
        debugEl.textContent += `${new Date().toISOString()} ${msg}\n`;
    };

    try {
        debugLog('start loadAvailableVersions');
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        const res = await fetch('/api/versions', {
            headers: { Accept: 'application/json' },
            signal: controller.signal,
        });
        clearTimeout(timeout);
        debugLog(`fetch complete status=${res.status}`);
        const data = await res.json().catch((err) => {
            debugLog(`json parse error: ${err && err.message ? err.message : String(err)}`);
            return null;
        });
        if (!res.ok || !Array.isArray(data)) {
            debugLog(`invalid response ok=${res.ok} isArray=${Array.isArray(data)}`);
            throw new Error('Kon vertalingen niet laden.');
        }
        debugLog(`versions count=${data.length}`);

        loadingEl.classList.add('hidden');
        errorEl.classList.add('hidden');

        gridEl.innerHTML = data
            .map((item) => {
                const key = escapeHtml(item.key || '-');
                const name = escapeHtml(versionsDisplayTitle(item.key, item.name, item.key));
                const shortname = escapeHtml(item.shortname || '-');
                const module = escapeHtml(item.module || '-');
                const year = escapeHtml(item.year || '-');
                const lang = escapeHtml(item.lang || '-');
                const description = escapeHtml(versionsDisplayDescription(item.key, item.description));
                const endpoint = escapeHtml(`/api/verse?book=Genesis&chapter=1&verse=1&version=${item.key || ''}`);
                return `
                    <article class="flex h-full flex-col items-center rounded-xl border border-slate-200 bg-slate-50 p-5 text-center">
                        <div class="mb-3 flex items-start justify-between gap-3">
                            <h3 class="text-base font-semibold text-slate-900">${name}</h3>
                            <code class="shrink-0 rounded-md border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700">${key}</code>
                        </div>
                        <div class="mb-3 flex flex-wrap justify-center gap-2 text-xs">
                            <span class="rounded-md bg-slate-200/70 px-2 py-1 text-slate-700"><span class="font-medium text-slate-500">shortname</span> ${shortname}</span>
                            <span class="rounded-md bg-slate-200/70 px-2 py-1 text-slate-700"><span class="font-medium text-slate-500">module</span> ${module}</span>
                            <span class="rounded-md bg-slate-200/70 px-2 py-1 text-slate-700"><span class="font-medium text-slate-500">lang</span> ${lang}</span>
                            <span class="rounded-md bg-slate-200/70 px-2 py-1 text-slate-700"><span class="font-medium text-slate-500">jaar</span> ${year}</span>
                        </div>
                        <p class="mb-4 flex-grow text-sm leading-relaxed text-slate-600">${description}</p>
                        <p class="text-xs text-slate-500">Voorbeeld endpoint:</p>
                        <code class="mt-1 block max-w-full overflow-x-auto rounded bg-white px-2 py-1 text-xs text-slate-700">${endpoint}</code>
                    </article>
                `;
            })
            .join('');
        debugLog(`render complete in ${Date.now() - startedAt}ms`);
    } catch {
        loadingEl.classList.add('hidden');
        errorEl.classList.remove('hidden');
        errorEl.textContent = 'Kon beschikbare vertalingen momenteel niet laden.';
        debugLog(`catch reached after ${Date.now() - startedAt}ms`);
    }
}

function initFadeInSections() {
    const els = document.querySelectorAll('.fade-in-section');
    if (!els.length) return;
    if ('IntersectionObserver' in window) {
        const obs = new IntersectionObserver((entries) => {
            entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('visible'); obs.unobserve(e.target); } });
        }, { threshold: 0.08 });
        els.forEach((el) => obs.observe(el));
    } else {
        els.forEach((el) => el.classList.add('visible'));
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        initBillingAndBanners();
        loadAvailableVersions();
        initFadeInSections();
    });
} else {
    initBillingAndBanners();
    loadAvailableVersions();
    initFadeInSections();
}

// Zoek een specifiek vers
const verseForm = document.getElementById('verseForm');
const verseResult = document.getElementById('verseResult');
if (verseForm) {
verseForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!verseResult) return;
    verseResult.textContent = 'Bezig met zoeken...';
    const bookInput = document.getElementById('book');
    const chapterInput = document.getElementById('chapter');
    const verseInput = document.getElementById('verse');
    const book = bookInput ? bookInput.value : '';
    const chapter = chapterInput ? chapterInput.value : '';
    const verse = verseInput ? verseInput.value : '';
    try {
        const res = await fetch(`/api/verse?book=${encodeURIComponent(book)}&chapter=${chapter}&verse=${verse}`);
        if (!res.ok) throw new Error('Niet gevonden of fout in API');
        const data = await res.json();
        verseResult.innerHTML = `<b>${data.book} ${data.chapter}:${data.verse}</b><br>${data.text}`;
    } catch (err) {
        verseResult.textContent = 'Kon vers niet ophalen. Controleer je invoer.';
    }
});
}

// Dagtekst ophalen
const daytextBtn = document.getElementById('getDaytext');
const daytextResult = document.getElementById('daytextResult');
if (daytextBtn) {
daytextBtn.addEventListener('click', async () => {
    if (!daytextResult) return;
    daytextResult.textContent = 'Bezig met ophalen...';
    try {
        const res = await fetch('/api/daytext');
        if (!res.ok) throw new Error('Fout in API');
        const data = await res.json();
        daytextResult.innerHTML = `<b>${data.book} ${data.chapter}:${data.verse}</b><br>${data.text}`;
    } catch (err) {
        console.error('Kon dagtekst niet ophalen:', err);
        daytextResult.textContent = 'Kon dagtekst niet ophalen. ' + (err.message || err);
    }
});
}

// Laad boekenlijst in dropdown
async function loadBooksDropdown() {
    const select = document.getElementById('bookChapterSelect');
    if (!select) return;
    select.innerHTML = '<option value="">Laden...</option>';
    try {
        const res = await fetch('/api/books');
        const books = await res.json();
        if (!Array.isArray(books)) {
            select.innerHTML = '<option>Ongeldig antwoord</option>';
            return;
        }
        select.innerHTML = books.map((book) => `<option value="${book}">${book}</option>`).join('');
    } catch {
        select.innerHTML = '<option>Fout bij laden</option>';
    }
}
loadBooksDropdown();

// Hoofdstuk ophalen
const chapterForm = document.getElementById('chapterForm');
const chapterResult = document.getElementById('chapterResult');
if (chapterForm) {
chapterForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!chapterResult) return;
    chapterResult.textContent = 'Bezig met laden...';
    const bookSelect = document.getElementById('bookChapterSelect');
    const chapterNum = document.getElementById('chapterNum');
    const book = bookSelect ? bookSelect.value : '';
    const chapter = chapterNum ? chapterNum.value : '';
    try {
        const res = await fetch(`/api/chapter?book=${encodeURIComponent(book)}&chapter=${chapter}`);
        if (!res.ok) throw new Error('Niet gevonden of fout in API');
        const data = await res.json();
        let html = `<b>${data.book} ${data.chapter}</b><br><div class="card">`;
        for (const [vers, tekst] of Object.entries(data.verses)) {
            html += `<b>${vers}</b> ${tekst}<br>`;
        }
        html += '</div>';
        chapterResult.innerHTML = html;
    } catch (err) {
        chapterResult.textContent = 'Kon hoofdstuk niet ophalen. Controleer je invoer.';
    }
});
}

// Kopieerknoppen
function setupCopyButtons() {
    const btns = document.querySelectorAll('.copy-btn');
    btns.forEach((btn) => {
        btn.addEventListener('click', () => {
            const url = btn.getAttribute('data-url');
            navigator.clipboard.writeText(url);
            btn.textContent = 'Gekopieerd!';
            btn.classList.add('copied');
            setTimeout(() => {
                btn.textContent = 'Kopieer';
                btn.classList.remove('copied');
            }, 1200);
        });
    });
}
setupCopyButtons();

// API test buttons
function setupApiTestButtons() {
    const btns = document.querySelectorAll('.api-test-btn');
    btns.forEach((btn) => {
        btn.addEventListener('click', async () => {
            const url = btn.getAttribute('data-url');
            const parent = btn.parentElement;
            const resultDiv = parent ? parent.querySelector('.api-test-result') : null;
            if (!resultDiv) return;
            resultDiv.textContent = 'Bezig...';
            try {
                const res = await fetch(url);
                const data = await res.json();
                resultDiv.innerHTML = `<pre>${JSON.stringify(data, null, 2)}</pre>`;
            } catch (err) {
                resultDiv.textContent = 'Fout bij ophalen of geen geldige JSON.';
            }
        });
    });
}
setupApiTestButtons();

// Extra debug hooks for startup issues (visible with ?debug=1)
window.addEventListener('error', (event) => {
    billingDebugLog(`[ERR] window.error: ${event.message}`);
    const debugEl = document.getElementById('versionsDebug');
    const debugEnabled = new URLSearchParams(window.location.search).get('debug') === '1';
    if (!debugEnabled || !debugEl) return;
    debugEl.classList.remove('hidden');
    debugEl.textContent += `${new Date().toISOString()} window.error: ${event.message}\n`;
});

window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason && event.reason.message ? event.reason.message : String(event.reason);
    billingDebugLog(`[ERR] unhandledrejection: ${reason}`);
    const debugEl = document.getElementById('versionsDebug');
    const debugEnabled = new URLSearchParams(window.location.search).get('debug') === '1';
    if (!debugEnabled || !debugEl) return;
    debugEl.classList.remove('hidden');
    debugEl.textContent += `${new Date().toISOString()} unhandledrejection: ${reason}\n`;
});
