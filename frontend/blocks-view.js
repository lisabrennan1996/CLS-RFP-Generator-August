// Renders the "Blocks" tab: a source-page image with colored bbox overlays on the left,
// and a matching colored list of layout blocks on the right — clicking either side selects
// the block on both, matching the DataLab playground's results view. Selection is a shared
// Set<blockId> that app.js reads to drive the export action bar.
(function () {
  const LABEL_COLORS = {
    'Caption': '#8a8a93',
    'Footnote': '#8a8a93',
    'Equation-Block': '#7c5cff',
    'List-Group': '#0d9488',
    'Page-Header': '#9ca3af',
    'Page-Footer': '#9ca3af',
    'Image': '#e08a3c',
    'Section-Header': '#1f6feb',
    'Table': '#c2760f',
    'Text': '#2563eb',
    'Complex-Block': '#a855f7',
    'Code-Block': '#16a34a',
    'Form': '#db2777',
    'Table-Of-Contents': '#6b7280',
    'Figure': '#e08a3c',
    'Chemical-Block': '#0891b2',
    'Diagram': '#0891b2',
    'Bibliography': '#6b7280',
    'Blank-Page': '#d1d5db',
    'TableGroup': '#c2760f',
    'FigureGroup': '#7c3aed',
    'Page': '#2563eb',
  };

  function colorFor(label) {
    return LABEL_COLORS[label] || '#2563eb';
  }

  let currentBlocks = [];
  const selection = new Set();
  let onSelectionChange = function () {};

  function setOnSelectionChange(fn) {
    onSelectionChange = fn;
  }

  function notify() {
    onSelectionChange(Array.from(selection));
  }

  function toggleSelect(id, additive) {
    if (additive) {
      if (selection.has(id)) selection.delete(id);
      else selection.add(id);
    } else {
      const wasOnlySelected = selection.has(id) && selection.size === 1;
      selection.clear();
      if (!wasOnlySelected) selection.add(id);
    }
    applySelectionStyles();
    notify();
  }

  function clearSelection() {
    selection.clear();
    applySelectionStyles();
    notify();
  }

  function getSelectedBlocks() {
    return currentBlocks.filter((b) => selection.has(b.id));
  }

  function applySelectionStyles() {
    document.querySelectorAll('.block-card').forEach((el) => {
      el.classList.toggle('selected', selection.has(el.dataset.blockId));
    });
    document.querySelectorAll('.bbox-rect').forEach((el) => {
      el.classList.toggle('selected', selection.has(el.dataset.blockId));
    });
  }

  function scrollCardIntoView(id) {
    const card = document.querySelector(`.block-card[data-block-id="${id}"]`);
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function setCardBody(card, block) {
    const body = card.querySelector('.block-card-body');
    const renderToggle = document.getElementById('renderHtmlToggle');
    const renderHtml = renderToggle ? renderToggle.checked : true;
    if (renderHtml) {
      body.classList.remove('raw-html');
      body.innerHTML = block.html;
    } else {
      body.classList.add('raw-html');
      body.textContent = block.html;
    }
  }

  function refreshRenderMode() {
    currentBlocks.forEach((block) => {
      const card = document.querySelector(`.block-card[data-block-id="${block.id}"]`);
      if (card) setCardBody(card, block);
    });
  }

  function renderPage(blocks, imageDataUrl) {
    currentBlocks = blocks || [];
    selection.clear();
    notify();

    const img = document.getElementById('pageImage');
    img.src = imageDataUrl;

    const svg = document.getElementById('bboxOverlay');
    svg.innerHTML = '';
    svg.setAttribute('viewBox', '0 0 1000 1000');
    svg.setAttribute('preserveAspectRatio', 'none');

    const list = document.getElementById('blocksList');
    list.innerHTML = '';

    if (currentBlocks.length === 0) {
      list.innerHTML = '<p class="empty-hint">No blocks parsed for this page.</p>';
      return;
    }

    currentBlocks.forEach((block) => {
      const [x0, y0, x1, y1] = block.bbox;
      const color = colorFor(block.label);

      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('x', x0);
      rect.setAttribute('y', y0);
      rect.setAttribute('width', Math.max(x1 - x0, 2));
      rect.setAttribute('height', Math.max(y1 - y0, 2));
      rect.classList.add('bbox-rect');
      rect.dataset.blockId = block.id;
      rect.style.setProperty('--bbox-color', color);
      rect.addEventListener('click', (e) => {
        toggleSelect(block.id, e.ctrlKey || e.metaKey);
        scrollCardIntoView(block.id);
      });
      svg.appendChild(rect);

      const card = document.createElement('div');
      card.className = 'block-card';
      card.dataset.blockId = block.id;
      card.style.setProperty('--label-color', color);
      card.innerHTML =
        '<div class="block-card-header">' + escapeHtml(block.label.toUpperCase()) + '</div>' +
        '<div class="block-card-body"></div>';
      card.addEventListener('click', (e) => toggleSelect(block.id, e.ctrlKey || e.metaKey));
      list.appendChild(card);
      setCardBody(card, block);
    });

    applySelectionStyles();
  }

  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  window.BlocksView = {
    renderPage,
    clearSelection,
    getSelectedBlocks,
    setOnSelectionChange,
    refreshRenderMode,
    colorFor,
  };
})();
