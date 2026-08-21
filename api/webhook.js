export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(200).send('OK');

  try {
    const update = req.body;
    if (update && update.callback_query) {
      const query = update.callback_query;
      const callbackId = query.id;
      const data = query.data;
      const botToken = process.env.TELEGRAM_BOT_TOKEN;
      const supabaseUrl = process.env.SUPABASE_URL;
      const supabaseKey = process.env.SUPABASE_KEY;

      let popupText = "تم التحديث بنجاح!";
      let newStatus = null;
      let ticker = null;

      if (data.startsWith('act_')) {
        ticker = data.replace('act_', '');
        newStatus = 'ACTIVE';
        popupText = "✅ تم تفعيل المراقبة بنجاح!";
      } else if (data.startsWith('dis_')) {
        ticker = data.replace('dis_', '');
        newStatus = 'DISMISSED';
        popupText = "❌ تم إلغاء متابعة الصفقة.";
      } else if (data.startsWith('cls_')) {
        ticker = data.replace('cls_', '');
        newStatus = 'CLOSED';
        popupText = "🏁 تم إغلاق الصفقة يدوياً.";
      }

      if (newStatus && ticker && supabaseUrl && supabaseKey) {
        await fetch(`${supabaseUrl}/rest/v1/active_positions?ticker=eq.${ticker}`, {
          method: 'PATCH',
          headers: {
            'apikey': supabaseKey,
            'Authorization': `Bearer ${supabaseKey}`,
            'Content-Type': 'application/json',
            'Prefer': 'return=minimal'
          },
          body: JSON.stringify({ status: newStatus })
        });
      }

      await fetch(`https://api.telegram.org/bot${botToken}/answerCallbackQuery`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          callback_query_id: callbackId,
          text: popupText,
          show_alert: true
        })
      });
    }
  } catch (err) {
    console.error(err);
  }

  return res.status(200).send('OK');
}
