function normalizeTicker(symbol) {
  if (!symbol || typeof symbol !== 'string') return '';
  let t = symbol.trim().toUpperCase();
  if (!t) return '';
  if (!t.endsWith('.CA')) t = `${t}.CA`;
  return t;
}

const JOIN_CALLBACK_PREFIX = 'join_trade';
const USER_PORTFOLIO_TABLE = 'user_portfolio';
const TRACK_LABELS = {
  scalping: '⚡ مضاربة لحظية (Scalp)',
  swing: '📈 تداول سوينغ (Swing)',
  investment: '🏛️ استثمار طويل (Invest)',
};
const SHARIAH_COMPLIANT_BASE = new Set([
  'ABUK', 'AMOC', 'SWDY', 'TMGH', 'HELI', 'ORAS', 'EFIH', 'ADIB', 'FAIT',
  'SAUD', 'ETEL', 'FWRY', 'JUFO', 'EFID', 'ISPH', 'SKPC', 'OLFI', 'ORWE',
]);
const SHARIAH_NON_COMPLIANT_BASE = new Set(['COMI', 'EAST']);

function shariahFlag(symbol) {
  const base = normalizeTicker(symbol).replace('.CA', '');
  if (SHARIAH_NON_COMPLIANT_BASE.has(base)) return '⛔ غير متوافق (Non-Compliant)';
  if (SHARIAH_COMPLIANT_BASE.has(base)) return '✅ متوافق (Compliant)';
  return '⚠️ قيد المراجعة (Needs Review)';
}

function trackLabel(strategy) {
  const key = String(strategy || '').trim().toLowerCase();
  return TRACK_LABELS[key] || '📈 تداول سوينغ (Swing)';
}

async function tgPost(botToken, method, payload) {
  try {
    const r = await fetch(`https://api.telegram.org/bot${botToken}/${method}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const body = await r.text().catch(() => '');
    return { status: r.status, body };
  } catch (e) {
    return { status: 0, body: `error:${e}` };
  }
}

function parseJoinCallback(data) {
  try {
    const raw = String(data || '').trim();
    if (!raw.startsWith(`${JOIN_CALLBACK_PREFIX}:`)) return null;
    const parts = raw.split(':');
    if (parts.length < 2) return null;
    const ticker = normalizeTicker(parts[1]);
    if (!ticker) return null;
    let tradeId = 0;
    if (parts.length >= 3 && parts[2].trim()) {
      tradeId = parseInt(parts[2], 10);
      if (!Number.isFinite(tradeId) || tradeId < 0) tradeId = 0;
    }
    return { ticker, tradeId };
  } catch (e) {
    return null;
  }
}

async function fetchLatestSentAlert(supabaseUrl, supabaseKey, ticker) {
  try {
    const url = `${supabaseUrl}/rest/v1/sent_alerts?ticker=eq.${encodeURIComponent(ticker)}&order=created_at.desc&limit=1&select=*`;
    const r = await fetch(url, {
      method: 'GET',
      headers: {
        apikey: supabaseKey,
        Authorization: `Bearer ${supabaseKey}`,
        'Content-Type': 'application/json',
      },
    });
    if (!r.ok) return null;
    const rows = JSON.parse(await r.text().catch(() => '[]'));
    if (Array.isArray(rows) && rows.length > 0) return rows[0];
    return null;
  } catch (e) {
    console.error(`[JOIN ERROR] fetchLatestSentAlert failed:`, e);
    return null;
  }
}

async function upsertUserPortfolio(supabaseUrl, supabaseKey, payload) {
  const headers = {
    apikey: supabaseKey,
    Authorization: `Bearer ${supabaseKey}`,
    'Content-Type': 'application/json',
  };
  // Preferred: merge-duplicates on UNIQUE(user_id, symbol).
  try {
    const r = await fetch(
      `${supabaseUrl}/rest/v1/${USER_PORTFOLIO_TABLE}?on_conflict=user_id,symbol`,
      {
        method: 'POST',
        headers: { ...headers, Prefer: 'resolution=merge-duplicates,return=minimal' },
        body: JSON.stringify(payload),
      },
    );
    if (r.status === 200 || r.status === 201 || r.status === 204) return { ok: true, already: false };
    if (r.status === 409) return { ok: true, already: true };
    console.error(`[JOIN ERROR] user_portfolio upsert ${r.status}: ${await r.text().catch(() => '')}`);
  } catch (e) {
    console.error(`[JOIN ERROR] user_portfolio upsert failed:`, e);
  }
  // Fallback: plain insert - 409 means the user already joined.
  try {
    const r2 = await fetch(`${supabaseUrl}/rest/v1/${USER_PORTFOLIO_TABLE}`, {
      method: 'POST',
      headers: { ...headers, Prefer: 'return=minimal' },
      body: JSON.stringify(payload),
    });
    if (r2.status === 200 || r2.status === 201 || r2.status === 204) return { ok: true, already: false };
    if (r2.status === 409) return { ok: true, already: true };
    console.error(`[JOIN ERROR] user_portfolio insert ${r2.status}: ${await r2.text().catch(() => '')}`);
  } catch (e) {
    console.error(`[JOIN ERROR] user_portfolio insert failed:`, e);
  }
  return { ok: false, already: false };
}

function buildFullDmCard(ticker, alert) {
  const bare = normalizeTicker(ticker).replace('.CA', '');
  const num = (v) => parseFloat(v) || 0;
  const sep = '------------------------------------';
  const lines = [
    '🟢 <b>[كارت انضمام للصفقة]</b>',
    sep,
    `🔹 <b>السهم:</b> <code>${bare}</code> ${shariahFlag(ticker)}`,
    `🏷️ <b>المسار:</b> ${trackLabel(alert && alert.strategy)}`,
    sep,
    `💵 <b>الدخول:</b> ${num(alert && alert.entry_price).toFixed(2)} EGP`,
    `🔴 <b>وقف الخسارة (SL):</b> <b>${num(alert && alert.current_stop_loss).toFixed(2)}</b> EGP`,
    `🥇 الهدف الأول: <b>${num(alert && alert.target_1).toFixed(2)}</b> EGP`,
    `🥈 الهدف الثاني: <b>${num(alert && alert.target_2).toFixed(2)}</b> EGP`,
    `🥉 الهدف الثالث: <b>${num(alert && alert.target_3).toFixed(2)}</b> EGP`,
    sep,
    '<i>تداول فوري (Spot) فقط - شراء ثم بيع</i>',
    '🔒 إشعارات الإغلاق والتقرير الأسبوعي تصلك هنا في الخاص',
  ];
  return lines.join('\n');
}

async function handleJoinTrade(query, botToken, supabaseUrl, supabaseKey) {
  const diag = { ok: false, answered: false, saved: false, already: false, dm_sent: false };
  try {
    const parsed = parseJoinCallback(query.data || '');
    if (!parsed) {
      diag.error = 'bad-format';
      return diag;
    }
    const userId = String(((query.from || {}).id) || '').trim();
    const cqId = String(query.id || '');
    if (!userId) {
      diag.error = 'missing-user-id';
      await tgPost(botToken, 'answerCallbackQuery', {
        callback_query_id: cqId,
        text: '⚠️ تعذر تحديد هويتك - حاول مرة أخرى',
        show_alert: false,
      });
      return diag;
    }

    let alert = null;
    let saved = false;
    let already = false;
    if (supabaseUrl && supabaseKey) {
      alert = await fetchLatestSentAlert(supabaseUrl, supabaseKey, parsed.ticker);
      const result = await upsertUserPortfolio(supabaseUrl, supabaseKey, {
        user_id: userId,
        trade_id: parsed.tradeId,
        symbol: parsed.ticker,
        snapshot: {
          strategy: (alert || {}).strategy || '',
          entry_price: (alert || {}).entry_price ?? null,
          current_stop_loss: (alert || {}).current_stop_loss ?? null,
          target_1: (alert || {}).target_1 ?? null,
          target_2: (alert || {}).target_2 ?? null,
          target_3: (alert || {}).target_3 ?? null,
          source: 'vercel_webhook',
        },
        status: 'TRACKING',
        joined_at: new Date().toISOString(),
      });
      saved = result.ok;
      already = result.already;
    } else {
      console.warn('[JOIN] Supabase config missing - registration skipped');
    }

    // Answer immediately - kill the spinner.
    const popup = already
      ? 'ℹ️ أنت تتابع هذه الصفقة بالفعل'
      : `✅ تم تسجيل متابعتك لصفقة ${parsed.ticker.replace('.CA', '')}`;
    const ansRes = await tgPost(botToken, 'answerCallbackQuery', {
      callback_query_id: cqId,
      text: popup,
      show_alert: false,
    });
    diag.answered = ansRes.status === 200;

    // FULL detail card as a DM to the user's private chat.
    const dmRes = await tgPost(botToken, 'sendMessage', {
      chat_id: userId,
      text: buildFullDmCard(parsed.ticker, alert),
      parse_mode: 'HTML',
    });
    diag.dm_sent = dmRes.status === 200;

    diag.saved = saved;
    diag.already = already;
    diag.ok = true;
    console.log(`[JOIN] user=${userId} ticker=${parsed.ticker} diag=${JSON.stringify(diag)}`);
    return diag;
  } catch (e) {
    console.error('[JOIN ERROR] handleJoinTrade crashed:', e);
    diag.error = String(e);
    return diag;
  }
}

export default async function handler(req, res) {
  let httpResponded = false;
  if (req.method !== 'POST') return res.status(200).send('OK');

  // Ensure body is parsed (Vercel may not auto-parse)
  let update = req.body;
  if (typeof update === 'string') {
    try {
      update = JSON.parse(update);
    } catch (e) {
      console.error('[WEBHOOK] Failed to parse body string:', e);
    }
  }

  try {
    if (update && update.callback_query) {
      const query = update.callback_query;
      const callbackId = query.id;
      const data = query.data || "";
      const botToken = process.env.TELEGRAM_BOT_TOKEN;
      const supabaseUrl = process.env.SUPABASE_URL;
      const supabaseKey = process.env.SUPABASE_KEY || process.env.SUPABASE_SERVICE_ROLE_KEY;

      console.log(`[WEBHOOK] Received callback_query id=${callbackId} data=${data}`);

      // ---- Multi-tenant join_trade flow: user_portfolio + private full card DM ----
      if (typeof data === 'string' && data.startsWith(`${JOIN_CALLBACK_PREFIX}:`)) {
        const joinDiag = await handleJoinTrade(query, botToken, supabaseUrl, supabaseKey);
        console.log(`[WEBHOOK][JOIN] diag=${JSON.stringify(joinDiag)}`);
        if (!httpResponded && !res.writableEnded && !res.headersSent) {
          res.status(200).send('OK');
          httpResponded = true;
        }
        return;
      }

      let popupText = "تم التحديث بنجاح!";
      let newStatus = null;
      let ticker = null;

      try {
        if (data.startsWith('act_')) {
          // Extract ticker from callback_data (supports enhanced act_{ticker}|... or legacy act_{ticker}) – normalize strictly
          const payload = data.replace('act_', '');
          const parts = payload.split('|');
          ticker = normalizeTicker(parts[0] || "");
          newStatus = 'ACTIVE';
          // Required popup per spec for activation
          popupText = "✅ تم تفعيل الصفقة بنجاح وحفظها في Supabase!";
          console.log(`[WEBHOOK] Act activation for ticker=${ticker}`);
        } else if (data.startsWith('dis_')) {
          const raw = data.replace('dis_', '');
          ticker = normalizeTicker(raw.split('|')[0] || raw);
          newStatus = 'DISMISSED';
          popupText = "❌ تم إلغاء متابعة الصفقة.";
        } else if (data.startsWith('cls_')) {
          const raw = data.replace('cls_', '');
          ticker = normalizeTicker(raw.split('|')[0] || raw);
          newStatus = 'CLOSED';
          popupText = "🏁 تم إغلاق الصفقة يدوياً.";
        }
      } catch (e) {
        console.error('[WEBHOOK ERROR] Failed to parse callback_data:', e);
      }

      // Answer Telegram callback query immediately to avoid loading spinner timeout (per requirement)
      if (botToken && callbackId) {
        try {
          console.log(`[TELEGRAM] Sending immediate answerCallbackQuery id=${callbackId} text=${popupText}`);
          const immediateRes = await fetch(`https://api.telegram.org/bot${botToken}/answerCallbackQuery`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              callback_query_id: callbackId,
              text: popupText,
              show_alert: true,
            }),
          });
          const immediateBody = await immediateRes.text().catch(() => '');
          console.log(`[TELEGRAM] Immediate answerCallbackQuery ${callbackId} -> ${popupText} status: ${immediateRes.status} body: ${immediateBody}`);
        } catch (e) {
          console.error(`[TELEGRAM ERROR] Immediate answerCallbackQuery failed:`, e);
        }
      }

      // Immediately acknowledge HTTP 200 before DB work to kill Telegram retry loop
      try {
        if (!res.writableEnded && !res.headersSent) {
          res.status(200).send('OK');
          httpResponded = true;
          console.log('[WEBHOOK] Immediate HTTP 200 sent (before DB) to prevent retry storm');
        }
      } catch (e) {
        console.error('[WEBHOOK] Immediate HTTP 200 failed:', e);
      }

      // Handle Supabase actions: act -> insert, dis/cls -> explicit DELETE (with normalized ticker)
      if (newStatus && ticker && supabaseUrl && supabaseKey) {
        try {
          console.log(`[SUPABASE] Starting callback flow for ${ticker} -> ${newStatus}`);

          // Handle إغلاق الصفقة / غير مهتم -> explicit DELETE FROM active_positions WHERE ticker = 'TICKER'
          if (data.startsWith('dis_') || data.startsWith('cls_')) {
            try {
              console.log(`[SUPABASE] Explicit DELETE for ${ticker} (action ${data.slice(0,3)})`);
              // Build normalized ticker variants (upper/lower, with/without .CA)
              const baseTicker = ticker.trim();
              const variants = new Set([
                baseTicker,
                baseTicker.toUpperCase(),
                baseTicker.toLowerCase(),
                baseTicker.replace(/\.CA$/i, ''),
                baseTicker.replace(/\.CA$/i, '').toUpperCase(),
                baseTicker.replace(/\.CA$/i, '').toLowerCase(),
                `${baseTicker.replace(/\.CA$/i, '')}.CA`,
                `${baseTicker.replace(/\.CA$/i, '').toUpperCase()}.CA`,
                `${baseTicker.replace(/\.CA$/i, '').toLowerCase()}.CA`,
              ]);
              // Also handle direct ticker without .CA
              if (!baseTicker.toUpperCase().endsWith('.CA')) {
                variants.add(`${baseTicker}.CA`);
                variants.add(`${baseTicker.toUpperCase()}.CA`);
              }
              let deleted = false;
              for (const variant of Array.from(variants).slice(0, 6)) {
                if (!variant || !variant.trim()) continue;
                const safeVar = variant.trim();
                try {
                  // Explicit DELETE query: DELETE FROM active_positions WHERE ticker = 'variant'
                  const delUrl = `${supabaseUrl}/rest/v1/active_positions?ticker=eq.${encodeURIComponent(safeVar)}`;
                  console.log(`[SUPABASE] DELETE ${safeVar} -> ${delUrl}`);
                  const delRes = await fetch(delUrl, {
                    method: 'DELETE',
                    headers: {
                      'apikey': supabaseKey,
                      'Authorization': `Bearer ${supabaseKey}`,
                      'Content-Type': 'application/json',
                      'Prefer': 'return=representation',
                    },
                  });
                  const delBody = await delRes.text().catch(() => '');
                  console.log(`[SUPABASE] DELETE ${safeVar} -> ${delRes.status} body: ${delBody}`);
                  if (delRes.status === 200 || delRes.status === 204 || delRes.status === 202) {
                    deleted = true;
                    // Try to capture deleted rows if representation returned
                    console.log(`[SUPABASE] Deleted ${safeVar} successfully`);
                  }
                  // Also try lower/upper normalized explicit check
                  if (delRes.status === 200 && delBody && delBody !== '[]') {
                    // Successfully deleted some rows
                  }
                } catch (delErr) {
                  console.error(`[SUPABASE ERROR] DELETE failed for ${safeVar}:`, delErr);
                }
              }
              // Fallback: try ilike case-insensitive delete via or filter if direct eq didn't delete
              if (!deleted) {
                try {
                  // Try deleting via ticker ilike (case-insensitive) – best effort
                  const base = baseTicker.replace(/\.CA$/i, '');
                  const ilikeUrl = `${supabaseUrl}/rest/v1/active_positions?ticker=ilike.${encodeURIComponent(base)}%`;
                  const ilikeRes = await fetch(ilikeUrl, {
                    method: 'DELETE',
                    headers: {
                      'apikey': supabaseKey,
                      'Authorization': `Bearer ${supabaseKey}`,
                    },
                  });
                  const ilikeBody = await ilikeRes.text().catch(() => '');
                  console.log(`[SUPABASE] DELETE ilike ${base}% -> ${ilikeRes.status} ${ilikeBody}`);
                } catch (e) {
                  console.error(`[SUPABASE ERROR] ilike DELETE fallback failed:`, e);
                }
              }
              console.log(`[SUPABASE] DELETE flow completed for ${ticker}, deleted=${deleted}`);
            } catch (deleteErr) {
              console.error(`[SUPABASE ERROR] Explicit DELETE failed for ${ticker}:`, deleteErr);
            }
          } else if (data.startsWith('act_')) {
            let tradeDetails = null;

            // 1) Fetch latest alert details for that ticker from sent_alerts
            try {
              const sentUrl = `${supabaseUrl}/rest/v1/sent_alerts?ticker=eq.${encodeURIComponent(ticker)}&order=created_at.desc&limit=1&select=*`;
              console.log(`[SUPABASE] Fetching from sent_alerts: ${sentUrl}`);
              const getRes = await fetch(sentUrl, {
                method: 'GET',
                headers: {
                  'apikey': supabaseKey,
                  'Authorization': `Bearer ${supabaseKey}`,
                  'Content-Type': 'application/json',
                },
              });
              const getBody = await getRes.text().catch(() => '');
              console.log(`[SUPABASE] GET sent_alerts ${ticker} -> ${getRes.status} body: ${getBody}`);

              if (getRes.ok) {
                try {
                  const parsed = JSON.parse(getBody);
                  if (Array.isArray(parsed) && parsed.length > 0) {
                    const latest = parsed[0];
                    tradeDetails = {
                      ticker: latest.ticker || ticker,
                      entry_price: latest.entry_price,
                      current_stop_loss: latest.current_stop_loss,
                      target_1: latest.target_1,
                      target_2: latest.target_2,
                      target_3: latest.target_3,
                    };
                    console.log(`[SUPABASE] Found sent_alert for ${ticker}:`, tradeDetails);
                  } else {
                    console.log(`[SUPABASE] No sent_alert found for ${ticker}, will try fallback from callback_data`);
                  }
                } catch (parseErr) {
                  console.error(`[SUPABASE ERROR] Failed to parse sent_alerts JSON for ${ticker}:`, parseErr);
                }
              } else {
                console.error(`[SUPABASE ERROR] GET sent_alerts failed ${getRes.status}: ${getBody}`);
              }
            } catch (fetchErr) {
              console.error(`[SUPABASE ERROR] Fetch sent_alerts failed for ${ticker}:`, fetchErr);
            }

            // Fallback: if no sent_alerts record, try extracting from callback_data pipes (enhanced payload)
            if (!tradeDetails || tradeDetails.entry_price == null) {
              try {
                if (data.includes('|')) {
                  const parts = data.replace('act_', '').split('|');
                  if (parts.length >= 6) {
                    tradeDetails = {
                      ticker: parts[0].trim(),
                      entry_price: parseFloat(parts[1]),
                      current_stop_loss: parseFloat(parts[2]),
                      target_1: parseFloat(parts[3]),
                      target_2: parseFloat(parts[4]),
                      target_3: parseFloat(parts[5]),
                    };
                    console.log(`[SUPABASE] Fallback extracted from callback_data for ${ticker}:`, tradeDetails);
                  }
                }
              } catch (e) {
                console.error(`[SUPABASE ERROR] Fallback extraction failed:`, e);
              }
            }

            // Helper to sanitize payload – prevents PGRST204 schema cache errors (handles timestamp/created_at)
            function sanitizeActivePayload(payload) {
              const allowed = new Set(['ticker','entry_price','current_stop_loss','target_1','target_2','target_3','trade_track','status','created_at']);
              const sanitized = {};
              // Normalize timestamp -> created_at
              if (payload.timestamp && !payload.created_at) {
                sanitized.created_at = payload.timestamp;
              } else if (payload.created_at) {
                sanitized.created_at = payload.created_at;
              }
              for (const key of allowed) {
                if (key === 'created_at') continue;
                if (payload[key] !== undefined && payload[key] !== null) {
                  sanitized[key] = payload[key];
                }
              }
              if (sanitized.created_at) {
                // keep it
              }
              return sanitized;
            }

            // 2) Insert retrieved trade details into active_positions (sanitized, graceful fallback for schema errors)
            if (tradeDetails && tradeDetails.entry_price != null) {
              try {
                const rawPayload = {
                  ticker: tradeDetails.ticker,
                  entry_price: parseFloat(tradeDetails.entry_price),
                  current_stop_loss: parseFloat(tradeDetails.current_stop_loss) || parseFloat(tradeDetails.entry_price),
                  target_1: parseFloat(tradeDetails.target_1) || parseFloat(tradeDetails.entry_price),
                  target_2: parseFloat(tradeDetails.target_2) || parseFloat(tradeDetails.entry_price),
                  target_3: parseFloat(tradeDetails.target_3) || parseFloat(tradeDetails.entry_price),
                  trade_track: 'Scalp',
                  status: 'ACTIVE',
                  created_at: new Date().toISOString(),
                };
                const insertPayload = sanitizeActivePayload(rawPayload);
                console.log(`[SUPABASE] Inserting into active_positions (sanitized):`, insertPayload);
                let postRes = await fetch(`${supabaseUrl}/rest/v1/active_positions?on_conflict=ticker,trade_track`, {
                  method: 'POST',
                  headers: {
                    'apikey': supabaseKey,
                    'Authorization': `Bearer ${supabaseKey}`,
                    'Content-Type': 'application/json',
                    'Prefer': 'resolution=merge-duplicates,return=representation',
                  },
                  body: JSON.stringify(insertPayload),
                });
                let postBody = await postRes.text().catch(() => '');
                console.log(`[SUPABASE] POST active_positions ${ticker} -> ${postRes.status} body: ${postBody}`);
                // Gracefully handle PGRST204 and 42P10 schema errors – fallback to plain POST
                if (!postRes.ok && (postBody.includes('PGRST204') || postBody.includes('42P10') || postBody.includes('ON CONFLICT') || postBody.includes('schema cache'))) {
                  console.log(`[SUPABASE] Fallback: retrying without on_conflict for ${ticker}`);
                  const plainRes = await fetch(`${supabaseUrl}/rest/v1/active_positions`, {
                    method: 'POST',
                    headers: {
                      'apikey': supabaseKey,
                      'Authorization': `Bearer ${supabaseKey}`,
                      'Content-Type': 'application/json',
                      'Prefer': 'return=representation',
                    },
                    body: JSON.stringify(insertPayload),
                  });
                  const plainBody = await plainRes.text().catch(() => '');
                  console.log(`[SUPABASE] Plain POST fallback ${ticker} -> ${plainRes.status} body: ${plainBody}`);
                  if (!plainRes.ok && plainRes.status === 409) {
                    // Already exists – try PATCH
                    const patchRes = await fetch(`${supabaseUrl}/rest/v1/active_positions?ticker=eq.${encodeURIComponent(ticker)}&trade_track=eq.Scalp`, {
                      method: 'PATCH',
                      headers: {
                        'apikey': supabaseKey,
                        'Authorization': `Bearer ${supabaseKey}`,
                        'Content-Type': 'application/json',
                        'Prefer': 'return=minimal',
                      },
                      body: JSON.stringify(insertPayload),
                    });
                    const patchBody = await patchRes.text().catch(() => '');
                    console.log(`[SUPABASE] PATCH fallback ${ticker} -> ${patchRes.status} body: ${patchBody}`);
                    postRes = patchRes;
                    postBody = patchBody;
                  } else {
                    postRes = plainRes;
                    postBody = plainBody;
                  }
                }
                if (!postRes.ok) {
                  console.error(`[SUPABASE ERROR] Insert into active_positions failed ${postRes.status}: ${postBody}`);
                } else {
                  console.log(`[SUPABASE] Insert succeeded for ${ticker}`);
                }
              } catch (insertErr) {
                console.error(`[SUPABASE ERROR] Insert active_positions failed for ${ticker}:`, insertErr);
              }
            } else {
              console.error(`[SUPABASE ERROR] No tradeDetails available for ${ticker}, cannot insert into active_positions`);
              // Last resort minimal insert to ensure row exists (sanitized)
              try {
                const minimalRaw = {
                  ticker: ticker,
                  entry_price: 0,
                  current_stop_loss: 0,
                  target_1: 0,
                  target_2: 0,
                  target_3: 0,
                  trade_track: 'Scalp',
                  status: 'ACTIVE',
                  created_at: new Date().toISOString(),
                };
                const minimal = sanitizeActivePayload(minimalRaw);
                let fallbackRes = await fetch(`${supabaseUrl}/rest/v1/active_positions?on_conflict=ticker,trade_track`, {
                  method: 'POST',
                  headers: {
                    'apikey': supabaseKey,
                    'Authorization': `Bearer ${supabaseKey}`,
                    'Content-Type': 'application/json',
                    'Prefer': 'resolution=merge-duplicates,return=representation',
                  },
                  body: JSON.stringify(minimal),
                });
                let fallbackBody = await fallbackRes.text().catch(() => '');
                console.log(`[SUPABASE] Fallback minimal POST ${ticker} -> ${fallbackRes.status} ${fallbackBody}`);
                if (!fallbackRes.ok && (fallbackBody.includes('PGRST204') || fallbackBody.includes('42P10'))) {
                  const plainFallback = await fetch(`${supabaseUrl}/rest/v1/active_positions`, {
                    method: 'POST',
                    headers: {
                      'apikey': supabaseKey,
                      'Authorization': `Bearer ${supabaseKey}`,
                      'Content-Type': 'application/json',
                      'Prefer': 'return=representation',
                    },
                    body: JSON.stringify(minimal),
                  });
                  const plainBody = await plainFallback.text().catch(() => '');
                  console.log(`[SUPABASE] Plain fallback minimal POST ${ticker} -> ${plainFallback.status} ${plainBody}`);
                }
              } catch (e) {
                console.error(`[SUPABASE ERROR] Fallback minimal insert failed:`, e);
              }
            }
          }
        } catch (e) {
          console.error(`[SUPABASE ERROR] Callback flow failed for ${ticker} -> ${newStatus}:`, e);
          // Explicit fallback POST attempt
          try {
            const fallback = await fetch(`${supabaseUrl}/rest/v1/active_positions`, {
              method: 'POST',
              headers: {
                'apikey': supabaseKey,
                'Authorization': `Bearer ${supabaseKey}`,
                'Content-Type': 'application/json',
                'Prefer': 'resolution=merge-duplicates,return=representation',
              },
              body: JSON.stringify({ ticker, status: newStatus }),
            });
            const fbBody = await fallback.text().catch(() => '');
            console.log(`[SUPABASE] Emergency fallback POST ${ticker} -> ${fallback.status} ${fbBody}`);
          } catch (e2) {
            console.error(`[SUPABASE ERROR] Emergency fallback failed:`, e2);
          }
        }
      } else if (newStatus && ticker) {
        console.warn(`[SUPABASE] Skipping Supabase update: missing URL or Key (url=${!!supabaseUrl}, key=${!!supabaseKey})`);
      }

      // answerCallbackQuery already sent immediately at top – no duplicate needed
      console.log(`[TELEGRAM] Callback answer already sent immediately for ${callbackId}`);
    } else {
      console.log('[WEBHOOK] No callback_query in update, ignoring');
    }
  } catch (err) {
    console.error('[WEBHOOK ERROR] Top-level handler error:', err);
  }

  if (!httpResponded && !res.writableEnded && !res.headersSent) {
    return res.status(200).send('OK');
  }
  return;
