(() => {
  const navigation = document.querySelector('.navigation');
  const mobile = matchMedia('(max-width: 680px)');
  navigation.open = !mobile.matches;
  mobile.addEventListener('change', event => { navigation.open = !event.matches; });
  document.querySelectorAll('article table').forEach(table => {
    const wrapper = document.createElement('div');
    wrapper.className = 'table-scroll'; wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region'); wrapper.setAttribute('aria-label', 'Scrollable reference table');
    table.before(wrapper); wrapper.append(table);
  });
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
  const search = document.getElementById('site-search');
  const status = document.getElementById('search-status');
  const results = document.getElementById('search-results');
  const root = new URL(`${document.body.dataset.root}/`, location.href);
  const index = () => fetch(new URL('search/search_index.json', root)).then(response => {
    if (!response.ok) throw new Error('Search index unavailable');
    return response.json();
  });
  const state = { request: null, generation: 0 };
  search.addEventListener('input', async () => {
    const generation = ++state.generation;
    const query = search.value.trim().toLowerCase();
    results.replaceChildren();
    if (!query) { status.textContent = ''; return; }
    status.textContent = 'Searching…';
    try {
      state.request ??= index();
      const data = await state.request;
      if (generation !== state.generation) return;
      const terms = query.split(/\s+/);
      const matches = data.docs.filter(doc => terms.every(term => `${doc.title} ${doc.text}`.toLowerCase().includes(term)));
      matches.slice(0, 8).forEach(doc => {
        const item = document.createElement('li'); const link = document.createElement('a');
        const url = new URL(doc.location, root);
        if (url.origin !== root.origin || !url.pathname.startsWith(root.pathname)) return;
        link.href = url.href; link.textContent = doc.title; item.append(link); results.append(item);
      });
      status.textContent = matches.length ? `Showing ${Math.min(matches.length, 8)} of ${matches.length} results` : 'No matches. Try a tool name, or browse the guides.';
    } catch {
      if (generation !== state.generation) return;
      state.request = null;
      status.textContent = 'Search could not load. Browse the guides, or type again to retry.';
    }
  });
})();
