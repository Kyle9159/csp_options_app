/**
 * trading_buttons.js - Add Order Execution Buttons to Dashboard
 *
 * This script dynamically adds "Sell Put" and "Close Trade" buttons
 * to scanner opportunities and open positions.
 */

// Add trading UI to scanner opportunity tiles
function addTradingButtonsToOpportunities() {
    console.log('Adding trading buttons to scanner opportunities...');

    // This function should be called after opportunities are loaded
    // It finds all opportunity tiles and adds trading controls

    const tiles = document.querySelectorAll('.tile, .csp-tile, [class*="opportunity"], [class*="trade-card"]');

    tiles.forEach(tile => {
        // Skip if already has trading buttons
        if (tile.querySelector('.trading-controls')) {
            return;
        }

        // Try to extract opportunity data from tile
        const symbol = extractSymbol(tile);
        const strike = extractStrike(tile);
        const expiration = extractExpiration(tile);
        const premium = extractPremium(tile);

        if (!symbol || !strike || !expiration) {
            console.log('Could not extract data from tile, skipping...');
            return;
        }

        // Create trading controls HTML
        const tradingHTML = `
            <div class="trading-controls" style="margin-top:20px; padding:16px; background:rgba(16,185,129,0.15); border-radius:10px; border:1px solid #10b981;">
                <div style="color:#6ee7b7; font-weight:bold; margin-bottom:12px;">⚡ Quick Trade Execution</div>

                <!-- Live Quote Display -->
                <div class="live-quote" data-symbol="${symbol}" data-strike="${strike}" data-expiration="${expiration}"
                     style="margin-bottom:12px; padding:10px; background:rgba(59,130,246,0.15); border-radius:6px;">
                    <div style="color:#60a5fa; font-size:0.9rem; margin-bottom:6px;">📊 Live Market Data</div>
                    <div style="color:#cbd5e1; font-size:0.85rem;">
                        Bid: <span class="quote-bid">--</span> | Ask: <span class="quote-ask">--</span> | Mark: <span class="quote-mark">--</span>
                    </div>
                </div>

                <!-- Order Entry Form -->
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px;">
                    <div>
                        <label style="color:#94a3b8; font-size:0.85rem; display:block; margin-bottom:4px;">Contracts:</label>
                        <input type="number" class="trade-quantity" value="1" min="1" max="10"
                               style="width:100%; padding:8px; background:#1e293b; color:white; border:1px solid #334155; border-radius:6px;">
                    </div>
                    <div>
                        <label style="color:#94a3b8; font-size:0.85rem; display:block; margin-bottom:4px;">Limit Price:</label>
                        <input type="number" class="trade-limit-price" value="${premium || 0}" step="0.05"
                               style="width:100%; padding:8px; background:#1e293b; color:white; border:1px solid #334155; border-radius:6px;">
                    </div>
                </div>

                <!-- Action Button -->
                <button class="btn-sell-put"
                        data-symbol="${symbol}"
                        data-strike="${strike}"
                        data-expiration="${expiration}"
                        style="width:100%; padding:12px; background:linear-gradient(135deg, #10b981, #059669); color:white;
                               border:none; border-radius:8px; font-weight:bold; font-size:1rem; cursor:pointer;
                               transition:all 0.3s ease;">
                    🚀 SELL PUT TO OPEN
                </button>

                <div class="order-status" style="margin-top:8px; color:#fbbf24; font-size:0.85rem; text-align:center; min-height:20px;"></div>
            </div>
        `;

        // Append to tile
        tile.insertAdjacentHTML('beforeend', tradingHTML);

        // Load live quote
        loadLiveQuote(symbol, strike, expiration, tile);
    });

    // Attach event listeners
    attachTradingEventListeners();
}

// Extract symbol from tile (tries multiple strategies)
function extractSymbol(tile) {
    // Try data attributes first
    if (tile.dataset.symbol) return tile.dataset.symbol;

    // Try finding in text content
    const text = tile.textContent;
    const match = text.match(/\b([A-Z]{1,5})\b/);
    return match ? match[1] : null;
}

// Extract strike from tile
function extractStrike(tile) {
    if (tile.dataset.strike) return parseFloat(tile.dataset.strike);

    const text = tile.textContent;
    const match = text.match(/\$(\d+(?:\.\d{2})?)\s*P/i) || text.match(/Strike:\s*\$?(\d+(?:\.\d{2})?)/i);
    return match ? parseFloat(match[1]) : null;
}

// Extract expiration from tile
function extractExpiration(tile) {
    if (tile.dataset.expiration) return tile.dataset.expiration;

    const text = tile.textContent;
    // Try YYYY-MM-DD format
    const match = text.match(/(\d{4}-\d{2}-\d{2})/);
    if (match) return match[1];

    // Try DTE and calculate expiration
    const dteMatch = text.match(/(\d+)\s*DTE/i);
    if (dteMatch) {
        const dte = parseInt(dteMatch[1]);
        const exp = new Date();
        exp.setDate(exp.getDate() + dte);
        return exp.toISOString().split('T')[0]; // YYYY-MM-DD
    }

    return null;
}

// Extract premium from tile
function extractPremium(tile) {
    const text = tile.textContent;
    const match = text.match(/Premium:\s*\$(\d+\.\d{2})/i) || text.match(/\$(\d+\.\d{2})/);
    return match ? parseFloat(match[1]) : 0;
}

// Load live quote for an option
async function loadLiveQuote(symbol, strike, expiration, tile) {
    try {
        const response = await fetch(`/api/option/quote?symbol=${symbol}&strike=${strike}&expiration=${expiration}`);
        const quote = await response.json();

        const quoteDiv = tile.querySelector('.live-quote');
        if (quoteDiv) {
            quoteDiv.querySelector('.quote-bid').textContent = `$${quote.bid.toFixed(2)}`;
            quoteDiv.querySelector('.quote-ask').textContent = `$${quote.ask.toFixed(2)}`;
            quoteDiv.querySelector('.quote-mark').textContent = `$${quote.mark.toFixed(2)}`;

            // Update limit price to mark if available
            const limitInput = tile.querySelector('.trade-limit-price');
            if (limitInput && quote.mark > 0) {
                limitInput.value = quote.mark.toFixed(2);
            }
        }
    } catch (e) {
        console.error('Failed to load quote:', e);
    }
}

// Attach event listeners to trading buttons
function attachTradingEventListeners() {
    document.querySelectorAll('.btn-sell-put').forEach(btn => {
        // Remove existing listener if any
        btn.replaceWith(btn.cloneNode(true));
    });

    document.querySelectorAll('.btn-sell-put').forEach(btn => {
        btn.addEventListener('click', async function() {
            const symbol = this.dataset.symbol;
            const strike = parseFloat(this.dataset.strike);
            const expiration = this.dataset.expiration;

            const tile = this.closest('.tile, .csp-tile, [class*="opportunity"], [class*="trade-card"]');
            const quantity = parseInt(tile.querySelector('.trade-quantity').value);
            const limitPrice = parseFloat(tile.querySelector('.trade-limit-price').value);
            const statusDiv = tile.querySelector('.order-status');

            // Confirmation
            const totalCredit = (limitPrice * quantity * 100).toFixed(2);
            const capitalRequired = (strike * quantity * 100).toLocaleString();

            const confirmed = confirm(
                `Sell ${quantity} contract(s) of ${symbol} $${strike}P @ $${limitPrice}?\n\n` +
                `Total Credit: $${totalCredit}\n` +
                `Capital Required: $${capitalRequired}\n\n` +
                `This will place a LIVE order in your Schwab account!`
            );

            if (!confirmed) return;

            // Show loading
            this.disabled = true;
            this.textContent = '⏳ Placing Order...';
            statusDiv.textContent = 'Sending order to Schwab...';
            statusDiv.style.color = '#fbbf24';

            try {
                const response = await fetch('/api/order/sell_put', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        symbol,
                        strike,
                        expiration,
                        quantity,
                        limit_price: limitPrice
                    })
                });

                const result = await response.json();

                if (result.success) {
                    statusDiv.style.color = '#10b981';
                    statusDiv.textContent = `✅ ${result.message} (Order ID: ${result.order_id})`;
                    this.textContent = '✅ Order Placed';
                    this.style.background = '#059669';
                } else {
                    statusDiv.style.color = '#ef4444';
                    statusDiv.textContent = `❌ ${result.message}`;
                    this.textContent = '🚀 SELL PUT TO OPEN';
                    this.disabled = false;
                }
            } catch (e) {
                statusDiv.style.color = '#ef4444';
                statusDiv.textContent = `❌ Error: ${e.message}`;
                this.textContent = '🚀 SELL PUT TO OPEN';
                this.disabled = false;
            }
        });
    });
}

// Add Close Trade buttons to Open CSP positions
function addCloseTradeButtons() {
    console.log('Adding close trade buttons to open positions...');

    // Find open position tiles (these typically have different selectors)
    const positionTiles = document.querySelectorAll('[class*="open"], [class*="position"], .tile_csp');

    positionTiles.forEach(tile => {
        // Skip if already has close button
        if (tile.querySelector('.btn-close-trade')) {
            return;
        }

        // Extract position data
        const symbol = extractSymbol(tile);
        const strike = extractStrike(tile);
        const expiration = extractExpiration(tile);
        const currentPremium = extractCurrentPremium(tile);

        if (!symbol || !strike || !expiration) {
            return;
        }

        // Create close trade button HTML
        const closeHTML = `
            <div class="close-trade-controls" style="margin-top:16px; padding:14px; background:rgba(239,68,68,0.15); border-radius:10px; border:1px solid #ef4444;">
                <div style="color:#f87171; font-weight:bold; margin-bottom:10px;">🔒 Close Position</div>

                <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;">
                    <div>
                        <label style="color:#94a3b8; font-size:0.85rem; display:block; margin-bottom:4px;">Contracts:</label>
                        <input type="number" class="close-quantity" value="1" min="1" max="10"
                               style="width:100%; padding:8px; background:#1e293b; color:white; border:1px solid #334155; border-radius:6px;">
                    </div>
                    <div>
                        <label style="color:#94a3b8; font-size:0.85rem; display:block; margin-bottom:4px;">Max Price:</label>
                        <input type="number" class="close-limit-price" value="${currentPremium || 0}" step="0.05"
                               style="width:100%; padding:8px; background:#1e293b; color:white; border:1px solid #334155; border-radius:6px;">
                    </div>
                </div>

                <button class="btn-close-trade"
                        data-symbol="${symbol}"
                        data-strike="${strike}"
                        data-expiration="${expiration}"
                        style="width:100%; padding:10px; background:linear-gradient(135deg, #ef4444, #dc2626); color:white;
                               border:none; border-radius:8px; font-weight:bold; cursor:pointer; transition:all 0.3s ease;">
                    🔒 BUY TO CLOSE
                </button>

                <div class="close-status" style="margin-top:8px; color:#fbbf24; font-size:0.85rem; text-align:center; min-height:20px;"></div>
            </div>
        `;

        tile.insertAdjacentHTML('beforeend', closeHTML);
    });

    // Attach close button event listeners
    attachCloseTradeEventListeners();
}

// Extract current premium from tile (for close orders)
function extractCurrentPremium(tile) {
    const text = tile.textContent;
    const match = text.match(/Current.*\$(\d+\.\d{2})/i) || text.match(/Mark.*\$(\d+\.\d{2})/i);
    return match ? parseFloat(match[1]) : 0;
}

// Attach event listeners to close trade buttons
function attachCloseTradeEventListeners() {
    document.querySelectorAll('.btn-close-trade').forEach(btn => {
        btn.replaceWith(btn.cloneNode(true));
    });

    document.querySelectorAll('.btn-close-trade').forEach(btn => {
        btn.addEventListener('click', async function() {
            const symbol = this.dataset.symbol;
            const strike = parseFloat(this.dataset.strike);
            const expiration = this.dataset.expiration;

            const tile = this.closest('.tile, .tile_csp, [class*="position"]');
            const quantity = parseInt(tile.querySelector('.close-quantity').value);
            const limitPrice = parseFloat(tile.querySelector('.close-limit-price').value);
            const statusDiv = tile.querySelector('.close-status');

            // Confirmation
            const totalCost = (limitPrice * quantity * 100).toFixed(2);

            const confirmed = confirm(
                `Buy to close ${quantity} contract(s) of ${symbol} $${strike}P @ $${limitPrice}?\n\n` +
                `Total Cost: $${totalCost}\n\n` +
                `This will place a LIVE order in your Schwab account!`
            );

            if (!confirmed) return;

            // Show loading
            this.disabled = true;
            this.textContent = '⏳ Closing Position...';
            statusDiv.textContent = 'Sending close order to Schwab...';
            statusDiv.style.color = '#fbbf24';

            try {
                const response = await fetch('/api/order/close_put', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        symbol,
                        strike,
                        expiration,
                        quantity,
                        limit_price: limitPrice
                    })
                });

                const result = await response.json();

                if (result.success) {
                    statusDiv.style.color = '#10b981';
                    statusDiv.textContent = `✅ ${result.message} (Order ID: ${result.order_id})`;
                    this.textContent = '✅ Close Order Placed';
                    this.style.background = '#059669';
                } else {
                    statusDiv.style.color = '#ef4444';
                    statusDiv.textContent = `❌ ${result.message}`;
                    this.textContent = '🔒 BUY TO CLOSE';
                    this.disabled = false;
                }
            } catch (e) {
                statusDiv.style.color = '#ef4444';
                statusDiv.textContent = `❌ Error: ${e.message}`;
                this.textContent = '🔒 BUY TO CLOSE';
                this.disabled = false;
            }
        });
    });
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        setTimeout(() => {
            addTradingButtonsToOpportunities();
            addCloseTradeButtons();
            addSyncPositionsButton();
        }, 1000); // Wait for data to load
    });
} else {
    setTimeout(() => {
        addTradingButtonsToOpportunities();
        addCloseTradeButtons();
        addSyncPositionsButton();
    }, 1000);
}

// Also run when tab changes (for SPAs)
document.addEventListener('tabchange', () => {
    addTradingButtonsToOpportunities();
    addCloseTradeButtons();
});

// Add Sync Positions button to dashboard
function addSyncPositionsButton() {
    console.log('Adding sync positions button...');

    // Check if button already exists
    if (document.getElementById('sync-positions-btn')) {
        return;
    }

    // Find a good location - try the Open CSPs tab or create a floating button
    const openCSPsTab = document.getElementById('open-csps') || document.querySelector('[data-tab="open-csps"]');
    const body = document.body;

    // Create sync button HTML
    const syncButtonHTML = `
        <div id="sync-positions-container" style="position:fixed; bottom:20px; right:20px; z-index:9999;">
            <button id="sync-positions-btn"
                    style="padding:14px 24px; background:linear-gradient(135deg, #3b82f6, #2563eb); color:white;
                           border:none; border-radius:12px; font-weight:bold; font-size:1rem; cursor:pointer;
                           box-shadow:0 4px 12px rgba(59,130,246,0.4); transition:all 0.3s ease;
                           display:flex; align-items:center; gap:8px;">
                <span>🔄</span>
                <span>Sync Positions from Schwab</span>
            </button>
            <div id="sync-status" style="margin-top:8px; padding:8px 12px; background:#1e293b; border-radius:8px;
                                        color:#cbd5e1; font-size:0.85rem; text-align:center; display:none;
                                        box-shadow:0 2px 8px rgba(0,0,0,0.3);"></div>
        </div>
    `;

    // Add button to page
    body.insertAdjacentHTML('beforeend', syncButtonHTML);

    // Add hover effect
    const btn = document.getElementById('sync-positions-btn');
    btn.addEventListener('mouseenter', () => {
        btn.style.transform = 'translateY(-2px)';
        btn.style.boxShadow = '0 6px 16px rgba(59,130,246,0.5)';
    });
    btn.addEventListener('mouseleave', () => {
        btn.style.transform = 'translateY(0)';
        btn.style.boxShadow = '0 4px 12px rgba(59,130,246,0.4)';
    });

    // Attach click handler
    attachSyncButtonListener();
}

// Attach event listener to sync button
function attachSyncButtonListener() {
    const btn = document.getElementById('sync-positions-btn');
    const statusDiv = document.getElementById('sync-status');

    if (!btn) return;

    btn.addEventListener('click', async function() {
        const confirmed = confirm(
            'Sync all open option positions from your Schwab account to Google Sheets?\n\n' +
            'This will add any new positions found in your account.'
        );

        if (!confirmed) return;

        // Show loading state
        btn.disabled = true;
        btn.innerHTML = '<span>⏳</span><span>Syncing...</span>';
        statusDiv.style.display = 'block';
        statusDiv.style.color = '#fbbf24';
        statusDiv.textContent = 'Fetching positions from Schwab...';

        try {
            const response = await fetch('/api/sync_positions', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'}
            });

            const result = await response.json();

            if (result.success) {
                statusDiv.style.color = '#10b981';
                statusDiv.textContent = `✅ Synced ${result.synced_count} new position(s) out of ${result.total_positions} total`;

                btn.innerHTML = '<span>✅</span><span>Sync Complete</span>';
                btn.style.background = 'linear-gradient(135deg, #10b981, #059669)';

                // Show detailed results if any new positions
                if (result.new_positions && result.new_positions.length > 0) {
                    const positions = result.new_positions.map(p =>
                        `${p.Symbol} $${p.Strike}P x${p.Quantity}`
                    ).join(', ');

                    setTimeout(() => {
                        alert(`New positions synced:\n\n${positions}\n\nRefresh the page to see them in Open CSPs.`);
                    }, 500);
                } else {
                    setTimeout(() => {
                        statusDiv.textContent = 'All positions already tracked';
                    }, 2000);
                }

                // Reset button after 5 seconds
                setTimeout(() => {
                    btn.disabled = false;
                    btn.innerHTML = '<span>🔄</span><span>Sync Positions from Schwab</span>';
                    btn.style.background = 'linear-gradient(135deg, #3b82f6, #2563eb)';
                    statusDiv.style.display = 'none';
                }, 5000);

            } else {
                statusDiv.style.color = '#ef4444';
                statusDiv.textContent = `❌ Sync failed: ${result.error || 'Unknown error'}`;

                btn.innerHTML = '<span>❌</span><span>Sync Failed</span>';
                btn.disabled = false;

                // Reset button after 5 seconds
                setTimeout(() => {
                    btn.innerHTML = '<span>🔄</span><span>Sync Positions from Schwab</span>';
                    statusDiv.style.display = 'none';
                }, 5000);
            }

        } catch (e) {
            statusDiv.style.color = '#ef4444';
            statusDiv.textContent = `❌ Error: ${e.message}`;

            btn.innerHTML = '<span>🔄</span><span>Sync Positions from Schwab</span>';
            btn.disabled = false;

            setTimeout(() => {
                statusDiv.style.display = 'none';
            }, 5000);
        }
    });
}

// Export for manual triggering
window.addTradingButtons = addTradingButtonsToOpportunities;
window.addCloseButtons = addCloseTradeButtons;
window.addSyncButton = addSyncPositionsButton;

console.log('Trading buttons script loaded. Call window.addTradingButtons(), window.addCloseButtons(), or window.addSyncButton() to manually add buttons.');
