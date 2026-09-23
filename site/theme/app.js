(() => {
  const root = new URL(document.body.dataset.root.replace(/\/?$/, '/'), location.href);

  // Navigation stays open on wide screens and collapses on narrow ones.
  const navigation = document.querySelector('.navigation');
  const mobile = matchMedia('(max-width: 680px)');
  navigation.open = !mobile.matches;
  mobile.addEventListener('change', event => { navigation.open = !event.matches; });

  // Wide tables scroll inside their own focusable region.
  document.querySelectorAll('article table').forEach(table => {
    const wrapper = document.createElement('div');
    wrapper.className = 'table-scroll'; wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region'); wrapper.setAttribute('aria-label', 'Scrollable table');
    table.before(wrapper); wrapper.append(table);
  });

  // Every code block gets a copy button.
  const announce = document.createElement('p');
  announce.className = 'visually-hidden'; announce.setAttribute('role', 'status');
  document.body.append(announce);
  document.querySelectorAll('article pre').forEach(pre => {
    const code = pre.querySelector('code');
    if (!code || !navigator.clipboard) return;
    const frame = document.createElement('div');
    frame.className = 'code-frame';
    pre.before(frame); frame.append(pre);
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'copy-button'; button.textContent = 'Copy';
    button.setAttribute('aria-label', 'Copy code to clipboard');
    button.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(code.innerText.replace(/\n$/, ''));
        button.textContent = 'Copied'; announce.textContent = 'Code copied to clipboard';
      } catch {
        button.textContent = 'Copy failed'; announce.textContent = 'Could not copy. Select the code instead.';
      }
      setTimeout(() => { button.textContent = 'Copy'; }, 1800);
    });
    frame.append(button);
  });

  // The contents rail highlights the section being read.
  const tocLinks = [...document.querySelectorAll('.toc a')];
  if (tocLinks.length && 'IntersectionObserver' in window) {
    const sections = tocLinks.map(link => document.getElementById(decodeURIComponent(link.hash.slice(1)))).filter(Boolean);
    const mark = id => tocLinks.forEach(link => {
      const current = decodeURIComponent(link.hash.slice(1)) === id;
      link.classList.toggle('is-current', current);
      if (current) link.setAttribute('aria-current', 'location'); else link.removeAttribute('aria-current');
    });
    const visible = new Set();
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => entry.isIntersecting ? visible.add(entry.target.id) : visible.delete(entry.target.id));
      const first = sections.find(section => visible.has(section.id));
      if (first) mark(first.id);
    }, { rootMargin: '0px 0px -70% 0px' });
    sections.forEach(section => observer.observe(section));
  }

  // The generated provider reference has its own filter.
  const filter = document.getElementById('provider-filter');
  if (filter) {
    const providers = [...document.querySelectorAll('.provider')];
    const update = () => {
      const query = filter.value.trim().toLowerCase();
      providers.forEach(item => { item.hidden = !item.querySelector('summary').textContent.toLowerCase().includes(query); });
      document.getElementById('provider-count').textContent = `${providers.filter(item => !item.hidden).length} of ${providers.length} providers`;
    };
    filter.addEventListener('input', update); update();
    const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (target?.classList.contains('provider')) { target.open = true; target.scrollIntoView(); }
  }

  // Site search reads MkDocs' prebuilt index on first use.
  const search = document.getElementById('site-search');
  const status = document.getElementById('search-status');
  const results = document.getElementById('search-results');
  const index = () => fetch(new URL('search/search_index.json', root)).then(response => {
    if (!response.ok) throw new Error('Search index unavailable');
    return response.json();
  }).then(data => {
    const pages = new Map(data.docs.filter(doc => !doc.location.includes('#')).map(doc => [doc.location, doc.title]));
    return data.docs.map(doc => ({ ...doc, page: pages.get(doc.location.split('#')[0]) || '', haystack: `${doc.title} ${doc.text}`.toLowerCase() }));
  });
  const snippet = (text, term) => {
    const plain = text.replace(/\s+/g, ' ').trim();
    const at = plain.toLowerCase().indexOf(term);
    if (at < 0) return plain.slice(0, 110) + (plain.length > 110 ? '…' : '');
    const start = Math.max(0, at - 40);
    return (start ? '…' : '') + plain.slice(start, start + 120) + (start + 120 < plain.length ? '…' : '');
  };
  const state = { request: null, generation: 0 };
  search.addEventListener('input', async () => {
    const generation = ++state.generation;
    const query = search.value.trim().toLowerCase();
    results.replaceChildren();
    if (!query) { status.textContent = ''; return; }
    status.textContent = 'Searching…';
    try {
      state.request ??= index();
      const docs = await state.request;
      if (generation !== state.generation) return;
      const terms = query.split(/\s+/);
      const count = (text, term) => text.split(term).length - 1;
      // Rank by title matches, then by how densely a section uses the terms,
      // favoring exact phrases and the start-here pages newcomers need most.
      const found = docs.filter(doc => terms.every(term => doc.haystack.includes(term))).map(doc => {
        const title = doc.title.toLowerCase();
        const inTitle = terms.filter(term => title.includes(term)).length;
        const hits = terms.reduce((sum, term) => sum + count(doc.haystack, term), 0);
        const density = Math.min(4 * hits / Math.sqrt(1 + doc.haystack.length / 300), 12);
        const phrase = terms.length > 1 && doc.haystack.includes(query) ? 10 : 0;
        const start = /^docs\/(concepts|getting-started|agents)\//.test(doc.location) ? 4 : 0;
        return { doc, score: inTitle * 12 + density + phrase + start + (doc.location.includes('#') ? 1 : 0) };
      });
      // A matching section is more useful than its whole page, unless the page title itself matches.
      const sectioned = new Set(found.filter(item => item.doc.location.includes('#')).map(item => item.doc.location.split('#')[0]));
      const matches = found.filter(item => item.doc.location.includes('#') || !sectioned.has(item.doc.location)
          || terms.some(term => item.doc.title.toLowerCase().includes(term)))
        .sort((a, b) => b.score - a.score).map(item => item.doc);
      matches.slice(0, 8).forEach(doc => {
        const url = new URL(doc.location, root);
        if (url.origin !== root.origin || !url.pathname.startsWith(root.pathname)) return;
        const item = document.createElement('li'); const link = document.createElement('a');
        link.href = url.href;
        const title = document.createElement('span'); title.className = 'result-title'; title.textContent = doc.title;
        link.append(title);
        if (doc.page && doc.page !== doc.title) {
          const page = document.createElement('span'); page.className = 'result-page'; page.textContent = doc.page;
          link.append(page);
        }
        const text = document.createElement('span'); text.className = 'result-text'; text.textContent = snippet(doc.text, terms[0]);
        link.append(text);
        item.append(link); results.append(item);
      });
      status.textContent = matches.length ? `Showing ${Math.min(matches.length, 8)} of ${matches.length} results` : 'No matches. Try a tool name, or browse the guides.';
    } catch {
      if (generation !== state.generation) return;
      state.request = null;
      status.textContent = 'Search could not load. Browse the guides, or type again to retry.';
    }
  });
})();
