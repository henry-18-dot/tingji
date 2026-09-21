for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async () => {
    const example = button.closest('.guide-copy-example'), text = document.getElementById(button.dataset.copy)?.textContent.trim(), status = example.querySelector('[role="status"]');
    if (!text) return;
    example.querySelector('.guide-manual-copy')?.remove();
    try {
      await navigator.clipboard.writeText(text);
      status.textContent = '已复制';
    } catch {
      const field = document.createElement('textarea'); field.className = 'guide-manual-copy'; field.readOnly = true; field.value = text; field.rows = 3; field.setAttribute('aria-label', '手动复制内容'); example.append(field); field.focus(); field.select(); status.textContent = '请选择文字后复制';
    }
  });
}
for (const link of document.querySelectorAll('a[href^="#"]')) {
  link.addEventListener('click', () => {
    const target = document.getElementById(link.getAttribute('href').slice(1));
    if (target) requestAnimationFrame(() => target.focus({preventScroll:true}));
  });
}
