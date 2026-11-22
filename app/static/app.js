(function () {
  document.querySelectorAll('[data-utc]').forEach(el => {
    const d = new Date(el.dataset.utc);
    if (!Number.isNaN(d.getTime())) el.textContent = new Intl.DateTimeFormat('ru-RU', { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit' }).format(d);
  });
  const kind = document.getElementById('source-kind');
  const address = document.getElementById('source-address');
  const help = document.getElementById('source-help');
  if (kind && address && help) {
    const update = () => {
      const tg = kind.value === 'telegram';
      address.placeholder = tg ? '@channelname или -100...' : 'https://example.com/feed.xml';
      help.textContent = tg ? 'Telegram: бот должен быть администратором разрешённого канала. Историю старых постов бот не видит.' : 'RSS загружается автоматически, не чаще выбранного интервала.';
    };
    kind.addEventListener('change', update); update();
  }
  const provider = document.getElementById('ai-provider');
  const model = document.getElementById('ai-model');
  if (provider && model) {
    provider.addEventListener('change', () => {
      if (model.value === 'openai/gpt-oss-20b' || model.value === 'gemini-3.1-flash-lite' || !model.value) {
        model.value = provider.value === 'gemini' ? 'gemini-3.1-flash-lite' : 'openai/gpt-oss-20b';
      }
    });
  }
  const editor = document.getElementById('draft-editor');
  const preview = document.getElementById('draft-preview');
  const count = document.getElementById('char-count');
  if (editor && preview && count) editor.addEventListener('input', () => {
    preview.textContent = editor.value;
    count.textContent = editor.value.length;
  });
  // Refresh job status and generated drafts only if work is pending.
  if (document.body.dataset.hasPending === '1') {
    const activePage = new URLSearchParams(location.search).get('page') || 'dashboard';
    if (['dashboard', 'drafts', 'topics'].includes(activePage)) {
      setTimeout(() => { if (!document.hidden) location.reload(); }, 20000);
    }
  }
})();
