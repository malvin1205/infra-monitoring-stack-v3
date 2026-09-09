/* Skins a native <select> into an accessible custom dropdown.
 * Pure DOM — operates only on the passed element. Moved verbatim from
 * InstancesPage._enhanceSelect (no `this` was used). */
export function enhanceSelect(sel) {
    if (!sel || sel.dataset.ddEnhanced || !sel.parentNode) return;
    sel.dataset.ddEnhanced = '1';

    // A hidden required control blocks native form submission ("not
    // focusable") — these forms all validate in JS anyway (see
    // _submitAddTarget), so drop it.
    sel.removeAttribute('required');

    const block = sel.classList.contains('form-select');
    const wrap = document.createElement('span');
    wrap.className = 'dd' + (block ? ' dd-block' : '');
    sel.parentNode.insertBefore(wrap, sel.nextSibling);
    sel.classList.add('dd-native');
    wrap.appendChild(sel);

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'job-dd-trigger';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    const al = sel.getAttribute('aria-label') || sel.getAttribute('title');
    if (al) trigger.setAttribute('aria-label', al);
    const label = document.createElement('span');
    label.className = 'job-dd-label';
    trigger.appendChild(label);
    trigger.insertAdjacentHTML('beforeend',
      '<svg aria-hidden="true" focusable="false" class="job-dd-caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>');
    const menu = document.createElement('ul');
    menu.className = 'job-dd-menu hidden';
    menu.setAttribute('role', 'listbox');
    menu.tabIndex = -1;
    wrap.appendChild(trigger);
    wrap.appendChild(menu);

    const enabled = () => Array.from(menu.children).filter(li => li.dataset.disabled !== '1');
    const setActive = li => {
      if (!li) return;
      menu.querySelectorAll('.job-dd-option-active').forEach(el => el.classList.remove('job-dd-option-active'));
      li.classList.add('job-dd-option-active');
      li.scrollIntoView({ block: 'nearest' });
    };
    const rebuild = () => {
      menu.innerHTML = '';
      Array.from(sel.options).forEach(o => {
        if (o.hidden) return;
        const li = document.createElement('li');
        li.className = 'job-dd-option';
        li.setAttribute('role', 'option');
        li.dataset.value = o.value;
        li.textContent = o.textContent;
        if (o.disabled) { li.dataset.disabled = '1'; li.setAttribute('aria-disabled', 'true'); }
        li.setAttribute('aria-selected', String(o.value === sel.value));
        menu.appendChild(li);
      });
      const cur = sel.options[sel.selectedIndex];
      label.textContent = cur ? cur.textContent : '';
      trigger.disabled = sel.disabled;
    };
    const open = () => {
      if (sel.disabled) return;
      rebuild();
      menu.classList.remove('hidden');
      trigger.setAttribute('aria-expanded', 'true');
      setActive(menu.querySelector('[aria-selected="true"]:not([aria-disabled])') || enabled()[0]);
      menu.focus();
    };
    const close = () => {
      if (menu.classList.contains('hidden')) return;
      menu.classList.add('hidden');
      trigger.setAttribute('aria-expanded', 'false');
    };
    const pick = li => {
      if (!li || li.dataset.disabled === '1') return;
      if (sel.value !== li.dataset.value) {
        sel.value = li.dataset.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
      rebuild();
      close();
      trigger.focus();
    };

    trigger.addEventListener('click', e => { e.stopPropagation(); if (menu.classList.contains('hidden')) open(); else close(); });
    trigger.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
    menu.addEventListener('click', e => { const li = e.target.closest('.job-dd-option'); if (li) pick(li); });
    menu.addEventListener('keydown', e => {
      const list = enabled();
      if (!list.length) return;
      const i = Math.max(0, list.findIndex(li => li.classList.contains('job-dd-option-active')));
      switch (e.key) {
        case 'ArrowDown': e.preventDefault(); setActive(list[Math.min(list.length - 1, i + 1)]); break;
        case 'ArrowUp': e.preventDefault(); setActive(list[Math.max(0, i - 1)]); break;
        case 'Home': e.preventDefault(); setActive(list[0]); break;
        case 'End': e.preventDefault(); setActive(list[list.length - 1]); break;
        case 'Enter': case ' ': e.preventDefault(); pick(list[i]); break;
        case 'Escape': e.preventDefault(); close(); trigger.focus(); break;
        case 'Tab': close(); break;
      }
    });
    document.addEventListener('click', e => { if (!wrap.contains(e.target)) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !menu.classList.contains('hidden')) { close(); trigger.focus(); } });

    // Runtime-injected <option>s (endpoint list, target-URL list, dependency
    // parents) + programmatic disabled toggles → rebuild trigger + menu.
    new MutationObserver(() => rebuild()).observe(sel, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] });
    // ponytail: a bare `sel.value = x` with no dispatched 'change' leaves the
    // trigger label stale until the next open(); the value stays correct and
    // open() re-reads it, so not worth patching the value setter.
    sel.addEventListener('change', () => rebuild());

    rebuild();
}

export function enhanceAllSelects() {
  // #jobSelect keeps its bespoke controller (gear / Default badge / popover).
  document.querySelectorAll('select:not(#jobSelect)').forEach(enhanceSelect);
}
