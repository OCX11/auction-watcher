/**
 * content.js — Auction Watcher extension
 * Runs on BaT / C&B / PCM listing pages.
 * Injects a star button and talks to the local API server.
 */

const API = 'https://api.trycloudflare.com';

function detectPlatform() {
  const h = location.hostname;
  if (h.includes('bringatrailer.com')) return 'bat';
  if (h.includes('carsandbids.com'))   return 'cnb';
  if (h.includes('pcarmarket.com'))    return 'pcm';
  return null;
}

function getListingUrl() {
  return `${location.protocol}//${location.hostname}${location.pathname}`;
}

async function findListingId(canonicalUrl) {
  try {
    const r = await fetch(`${API}/feed`);
    const d = await r.json();
    const match = (d.listings || []).find(l =>
      l.listing_url && l.listing_url.replace(/\/$/, '') === canonicalUrl.replace(/\/$/, '')
    );
    return match ? { id: match.id, saved: match.saved } : null;
  } catch (e) {
    console.warn('[AuctionWatcher] API unreachable — is the Mac Mini server running?', e);
    return null;
  }
}

function createButton(saved) {
  const btn = document.createElement('button');
  btn.id = 'aw-star-btn';
  btn.className = saved ? 'aw-starred' : '';
  btn.title = saved ? 'Saved to Auction Watcher' : 'Save to Auction Watcher';
  btn.innerHTML = `<span class="aw-star">${saved ? '★' : '☆'}</span><span class="aw-label">${saved ? 'Saved' : 'Watch'}</span>`;
  return btn;
}

function updateButton(btn, saved) {
  btn.className = saved ? 'aw-starred' : '';
  btn.title = saved ? 'Saved to Auction Watcher' : 'Save to Auction Watcher';
  btn.querySelector('.aw-star').textContent = saved ? '★' : '☆';
  btn.querySelector('.aw-label').textContent = saved ? 'Saved' : 'Watch';
}

async function init() {
  const platform = detectPlatform();
  if (!platform) return;
  const url = getListingUrl();
  const result = await findListingId(url);
  const btn = createButton(result ? result.saved : false);

  let listingId = result ? result.id : null;
  let isSaved   = result ? result.saved : false;
  let loading   = false;

  btn.addEventListener('click', async () => {
    if (loading) return;
    if (!listingId) {
      btn.querySelector('.aw-label').textContent = 'Not in feed yet';
      setTimeout(() => updateButton(btn, isSaved), 2000);
      return;
    }
    loading = true;
    btn.classList.add('aw-loading');
    try {
      const endpoint = isSaved ? '/watchlist/unstar' : '/watchlist/star';
      await fetch(`${API}${endpoint}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({listing_id: listingId})
      });
      isSaved = !isSaved;
      updateButton(btn, isSaved);
    } catch (e) {
      btn.querySelector('.aw-label').textContent = 'Error';
      setTimeout(() => updateButton(btn, isSaved), 2000);
    } finally {
      loading = false;
      btn.classList.remove('aw-loading');
    }
  });

  document.body.appendChild(btn);

  // If not yet in feed, retry once after 5s (scraper cycle may not have hit it yet)
  if (!result) {
    setTimeout(async () => {
      const r2 = await findListingId(url);
      if (r2) { listingId = r2.id; isSaved = r2.saved; updateButton(btn, isSaved); }
    }, 5000);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
