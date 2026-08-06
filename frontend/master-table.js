// Combines per-page tables (extracted from camelot-worker's Markdown via `convert_markdown`,
// format: 'json') into named, independently editable "master tables" — for documents where a
// table continues across several pages (one row per country/site, paginated). Mirrors the
// self-contained-module pattern in blocks-view.js: this file owns its own state and renders
// itself into the static containers app.js/index.html provide (#masterTableList,
// #masterTableGrid); app.js only wires the screen-level buttons (combine trigger, back, export)
// and calls into window.MasterTableView for everything table-specific.
//
// Projects and the permanent nav-rail placeholder tabs ("Consolidation and Tracking"/"RFP
// Generator"/"PSA review") are gone -- the app is single-RFP-focused now (Intake/Information/
// Countries/Schedule of Activities/Analytes/Specimens sections, see app.js), which has no
// equivalent of "file this table into a project" or "switch between several in-flight RFPs".
// Master SoA's real functionality (regenerate the consolidated schedule from the current PDF)
// is exposed here as a plain function the Schedule of Activities section calls directly, rather
// than living behind a placeholder tab.
(function () {
  const COLUMN_TYPES = ['text', 'number', 'date', 'tags'];
  const WIDE_TABLE_COLUMN_THRESHOLD = 6;
  // Reserved id for the auto-generated consolidated schedule. Living in the same `tables` array
  // as ordinary master tables means every existing mutation (add/delete row & column, drag-select
  // bulk delete, typed cells, wide-table fullscreen prompt) already works on it -- it's excluded
  // from the normal list purely by id, not by any special-cased grid logic.
  const MASTER_SOA_ID = '__master_soa__';

  let tables = []; // [{ id, name, columns: [{name, type}], rows: [[cellValue...]] }]
  let activeId = null;
  let sourceDocument = null; // { path, name } of the PDF currently open in the workspace
  let sourceDocumentTextProvider = () => ''; // set by app.js — returns the combined page text
  let masterSoaBusy = false;
  let onMasterSoaStateChange = function () {};

  // Delegates to app.js's own HTTP-backed invoke() (window.__invoke, exposed for exactly this
  // reason) rather than duplicating the fetch/session-id logic here -- keeps a single shared
  // session id across every file that talks to the backend.
  function invoke(cmd, args) {
    return window.__invoke(cmd, args);
  }

  // Called by app.js whenever the picked file changes, so Master SoA can run
  // `extract_master_schedule` against whatever document is currently loaded.
  function setSourceDocument(doc) {
    sourceDocument = doc || null;
    onMasterSoaStateChange();
  }

  // clinical_mapper.py (worker/clinical_mapper.py) parses already-extracted plain text, not the
  // original PDF — this provider (registered by app.js) returns the current document's combined
  // page text on demand, so Master SoA doesn't need to re-read anything from disk.
  function setSourceDocumentTextProvider(fn) {
    sourceDocumentTextProvider = typeof fn === 'function' ? fn : () => '';
  }

  // Called by app.js's Schedule of Activities section so it can show "Generating…"/disable its
  // own button while a regenerate is in flight, without master-table.js needing to know about
  // that section's markup.
  function setOnMasterSoaStateChange(fn) {
    onMasterSoaStateChange = typeof fn === 'function' ? fn : function () {};
  }
  function isMasterSoaBusy() {
    return masterSoaBusy;
  }
  function hasSourceDocument() {
    return !!sourceDocument;
  }

  // Column/row drag-multi-select for bulk delete. Not persisted per-table — switching the
  // active table clears it, since indices only make sense for whatever grid is currently
  // rendered.
  let dragSelection = { type: null, indices: new Set() }; // type: 'column' | 'row' | null
  let isDragging = false;
  let dragAnchor = null;

  function uid() {
    return 'mt_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
  }

  function clearDragSelection() {
    dragSelection = { type: null, indices: new Set() };
  }

  function getActive() {
    return tables.find((t) => t.id === activeId) || null;
  }

  function setActive(id) {
    activeId = id;
    clearDragSelection();
    render();
  }

  function emptyCellFor(type) {
    return type === 'tags' ? [] : '';
  }

  // Per-page header, widened to the widest row actually seen on that page — never truncated
  // down to the header's own width. A model-emitted header row that's one cell short of its
  // own data rows (e.g. a missing trailing "Comments" label while the data still has the
  // value) just gets a generic "Column N" name for the extra slot instead of losing that
  // column's data entirely.
  function widenedHeaderForTable(table) {
    const header = table.rows[0];
    const maxWidth = Math.max(header.length, ...table.rows.slice(1).map((r) => r.length), 1);
    const widened = [];
    for (let i = 0; i < maxWidth; i++) widened.push(header[i] || `Column ${i + 1}`);
    return widened;
  }

  // pageTables: [{ page, table: { rows: string[][] } | null }] in page order. `table.rows[0]`
  // is each page's own header row (same convention markdown_to_table.rs's table_to_markdown
  // already assumes, and every page of a real multi-page table reprints).
  //
  // Combine Tables always grows DOWN (more rows), never sideways (more columns): the column set
  // is fixed from the first page that has a table, widened to the widest row seen across every
  // selected page so nothing gets truncated (the earlier "Comments column dropped" bug), and
  // every page's data rows are appended positionally underneath it.
  function createFromPageTables(name, pageTables) {
    const skippedPages = [];
    const validPageTables = [];
    pageTables.forEach(({ page, table }) => {
      if (!table || !table.rows || table.rows.length === 0) skippedPages.push(page);
      else validPageTables.push(table);
    });

    let columnNames = ['Column 1'];
    if (validPageTables.length > 0) {
      const firstHeader = widenedHeaderForTable(validPageTables[0]);
      const maxWidth = Math.max(
        firstHeader.length,
        ...validPageTables.map((t) => Math.max(0, ...t.rows.map((r) => r.length)))
      );
      columnNames = [];
      for (let i = 0; i < maxWidth; i++) columnNames.push(firstHeader[i] || `Column ${i + 1}`);
    }

    const rows = [];
    validPageTables.forEach((table) => {
      table.rows.slice(1).forEach((row) => {
        const keyValue = (row[0] || '').trim();
        if (!keyValue) return;
        // Same non-CLS row exclusion Master SoA already applies (register visit, IWRS,
        // vitals, ECG, questionnaires, etc.) — only for SoA tables, since that's what the
        // pattern list was written against; Lab Appendix/LTS/Referral Lab pass through as-is.
        if (name === 'SoA' && window.MasterSchedule && window.MasterSchedule.isNonCLSRow(keyValue)) return;
        const padded = [];
        for (let i = 0; i < columnNames.length; i++) padded.push(row[i] ?? '');
        rows.push(padded);
      });
    });

    const columns = columnNames.map((n) => ({ name: n, type: 'text' }));

    const newTable = {
      id: uid(),
      name: (name || '').trim() || 'Untitled table',
      columns,
      rows,
    };
    tables.push(newTable);
    activeId = newTable.id;
    clearDragSelection();
    render();
    return { table: newTable, skippedPages };
  }

  function renameTable(id, name) {
    const t = tables.find((tt) => tt.id === id);
    if (t) {
      t.name = name.trim() || t.name;
      render();
    }
  }

  function deleteTable(id) {
    tables = tables.filter((t) => t.id !== id);
    if (activeId === id) {
      activeId = tables.length ? tables[0].id : null;
    }
    clearDragSelection();
    render();
  }

  function addRow(tableId) {
    const t = tables.find((tt) => tt.id === tableId);
    if (!t) return;
    t.rows.push(t.columns.map((c) => emptyCellFor(c.type)));
    render();
  }

  function deleteRowInternal(t, rowIndex) {
    t.rows.splice(rowIndex, 1);
  }

  function deleteRow(tableId, rowIndex) {
    const t = tables.find((tt) => tt.id === tableId);
    if (!t) return;
    deleteRowInternal(t, rowIndex);
    render();
  }

  function addColumn(tableId, name, type) {
    const t = tables.find((tt) => tt.id === tableId);
    if (!t) return;
    const colType = COLUMN_TYPES.includes(type) ? type : 'text';
    t.columns.push({ name: (name || '').trim() || `Column ${t.columns.length + 1}`, type: colType });
    t.rows.forEach((row) => row.push(emptyCellFor(colType)));
    render();
  }

  function deleteColumnInternal(t, colIndex) {
    t.columns.splice(colIndex, 1);
    t.rows.forEach((row) => row.splice(colIndex, 1));
  }

  function deleteColumn(tableId, colIndex) {
    const t = tables.find((tt) => tt.id === tableId);
    if (!t) return;
    deleteColumnInternal(t, colIndex);
    render();
  }

  function deleteSelected() {
    const t = getActive();
    if (!t || !dragSelection.type || dragSelection.indices.size === 0) return;
    // Highest index first so removing one doesn't shift the position of the next one to remove.
    const sorted = Array.from(dragSelection.indices).sort((a, b) => b - a);
    if (dragSelection.type === 'column') {
      sorted.forEach((idx) => deleteColumnInternal(t, idx));
    } else {
      sorted.forEach((idx) => deleteRowInternal(t, idx));
    }
    clearDragSelection();
    render();
  }

  function renameColumn(tableId, colIndex, name) {
    const t = tables.find((tt) => tt.id === tableId);
    if (t && t.columns[colIndex]) {
      t.columns[colIndex].name = name.trim() || t.columns[colIndex].name;
      render();
    }
  }

  function convertCellValue(value, toType) {
    if (toType === 'tags') {
      if (Array.isArray(value)) return value;
      const s = String(value == null ? '' : value).trim();
      return s ? s.split(',').map((x) => x.trim()).filter(Boolean) : [];
    }
    if (Array.isArray(value)) return value.join(', ');
    return value == null ? '' : String(value);
  }

  function setColumnType(tableId, colIndex, newType) {
    const t = tables.find((tt) => tt.id === tableId);
    if (!t || !t.columns[colIndex] || !COLUMN_TYPES.includes(newType)) return;
    t.columns[colIndex].type = newType;
    t.rows.forEach((row) => {
      row[colIndex] = convertCellValue(row[colIndex], newType);
    });
    render();
  }

  function setCellValue(tableId, rowIndex, colIndex, value) {
    const t = tables.find((tt) => tt.id === tableId);
    if (t && t.rows[rowIndex]) {
      t.rows[rowIndex][colIndex] = value;
    }
  }

  function addTag(tableId, rowIndex, colIndex, tag) {
    const t = tables.find((tt) => tt.id === tableId);
    if (!t || !t.rows[rowIndex]) return;
    const cell = t.rows[rowIndex][colIndex];
    const arr = Array.isArray(cell) ? cell.slice() : [];
    const trimmed = String(tag || '').trim();
    if (trimmed && !arr.includes(trimmed)) arr.push(trimmed);
    t.rows[rowIndex][colIndex] = arr;
    render();
  }

  function removeTag(tableId, rowIndex, colIndex, tag) {
    const t = tables.find((tt) => tt.id === tableId);
    if (!t || !t.rows[rowIndex]) return;
    const cell = t.rows[rowIndex][colIndex];
    if (Array.isArray(cell)) {
      t.rows[rowIndex][colIndex] = cell.filter((x) => x !== tag);
      render();
    }
  }

  // Every master table matching `name` (case-insensitive) — Combine Tables' "Crop & Merge" is
  // repeatable, so a real SoA can end up as several same-named tables built up over multiple
  // runs (see app.js's Generate RFP action, and the Schedule of Activities/Analytes/Specimens
  // sections, which all merge same-named tables together for display).
  function getTablesByName(name) {
    const target = String(name || '').trim().toLowerCase();
    return tables
      .filter((t) => t.id !== MASTER_SOA_ID && String(t.name || '').trim().toLowerCase() === target)
      .map((t) => ({
        id: t.id,
        name: t.name,
        headers: t.columns.map((c) => c.name),
        rows: t.rows.map((row) => row.map((cell, i) => cellDisplay(cell, t.columns[i].type))),
      }));
  }

  // ---------------- Master SoA (consolidated schedule) ----------------

  // Runs `extract_master_schedule` (worker/master_schedule_core.py, --schedule mode) against
  // the PDF currently open in the workspace and maps the result into the editable grid. Reads
  // the currently parsed document's own already-extracted text, which is what
  // clinical_mapper.py's section/column detection actually needs.
  async function regenerateMasterSoa() {
    const protocolText = sourceDocumentTextProvider();
    if (!sourceDocument || !protocolText || !protocolText.trim()) {
      window.alert('Parse a protocol document first (Intake), then generate Master SoA from it.');
      return;
    }
    if (masterSoaBusy) return;
    masterSoaBusy = true;
    onMasterSoaStateChange();
    try {
      const raw = await invoke('extract_master_schedule', { protocolText });
      const scheduleData = JSON.parse(raw);
      const { columns, rows } = window.MasterSchedule.scheduleToMasterTable(scheduleData);

      if (rows.length === 0) {
        window.alert(
          'No panels/analytes were found in this document — it may not follow the expected ' +
            'Lilly Harmonized Protocol SoA/Appendix 2 layout.'
        );
        return;
      }

      const generated = { id: MASTER_SOA_ID, name: 'Master SoA', columns, rows };
      const existingIndex = tables.findIndex((t) => t.id === MASTER_SOA_ID);
      if (existingIndex >= 0) tables[existingIndex] = generated;
      else tables.push(generated);

      activeId = MASTER_SOA_ID;
      clearDragSelection();
    } catch (err) {
      window.alert('Could not generate Master SoA: ' + err);
    } finally {
      masterSoaBusy = false;
      onMasterSoaStateChange();
      render();
    }
  }

  // ---------------- export content generation ----------------
  function cellDisplay(value, type) {
    if (type === 'tags') return Array.isArray(value) ? value.join(', ') : String(value || '');
    return value == null ? '' : String(value);
  }

  function toJson(t) {
    return JSON.stringify(
      {
        name: t.name,
        columns: t.columns,
        rows: t.rows.map((row) =>
          row.map((cell, i) => (t.columns[i].type === 'tags' ? (Array.isArray(cell) ? cell : []) : cellDisplay(cell, t.columns[i].type)))
        ),
      },
      null,
      2
    );
  }

  function escapeMdCell(s) {
    return String(s).replace(/\|/g, '\\|').replace(/\n/g, ' ');
  }

  function toMarkdown(t) {
    const header = t.columns.map((c) => c.name);
    const lines = [`# ${t.name}`, ''];
    lines.push('| ' + header.map(escapeMdCell).join(' | ') + ' |');
    lines.push('| ' + header.map(() => '---').join(' | ') + ' |');
    t.rows.forEach((row) => {
      const cells = row.map((cell, i) => escapeMdCell(cellDisplay(cell, t.columns[i].type)));
      lines.push('| ' + cells.join(' | ') + ' |');
    });
    return lines.join('\n') + '\n';
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function toHtml(t) {
    const header = t.columns.map((c) => `<th>${escapeHtml(c.name)}</th>`).join('');
    const rows = t.rows
      .map((row) => '<tr>' + row.map((cell, i) => `<td>${escapeHtml(cellDisplay(cell, t.columns[i].type))}</td>`).join('') + '</tr>')
      .join('\n');
    return `<h2>${escapeHtml(t.name)}</h2>\n<table>\n<thead><tr>${header}</tr></thead>\n<tbody>\n${rows}\n</tbody>\n</table>\n`;
  }

  function exportActive(format) {
    const t = getActive();
    if (!t) return null;
    if (format === 'json') return toJson(t);
    if (format === 'markdown') return toMarkdown(t);
    if (format === 'html') return toHtml(t);
    return null;
  }

  function getActiveName() {
    const t = getActive();
    return t ? t.name : 'master-table';
  }

  // ---------------- drag-to-select (columns/rows) ----------------
  function startColumnDrag(index) {
    isDragging = true;
    dragAnchor = index;
    dragSelection = { type: 'column', indices: new Set([index]) };
    renderGrid();
  }

  function startRowDrag(index) {
    isDragging = true;
    dragAnchor = index;
    dragSelection = { type: 'row', indices: new Set([index]) };
    renderGrid();
  }

  function extendDragTo(index) {
    if (!isDragging || dragAnchor == null) return;
    const lo = Math.min(dragAnchor, index);
    const hi = Math.max(dragAnchor, index);
    const indices = new Set();
    for (let i = lo; i <= hi; i++) indices.add(i);
    dragSelection = { type: dragSelection.type, indices };
    renderGrid();
  }

  document.addEventListener('mousemove', (e) => {
    if (!isDragging) return;
    const el = document.elementFromPoint(e.clientX, e.clientY);
    if (!el) return;
    if (dragSelection.type === 'column') {
      const th = el.closest('th[data-col-index]');
      if (th) extendDragTo(Number(th.dataset.colIndex));
    } else if (dragSelection.type === 'row') {
      const tr = el.closest('tr[data-row-index]');
      if (tr) extendDragTo(Number(tr.dataset.rowIndex));
    }
  });
  document.addEventListener('mouseup', () => {
    isDragging = false;
    dragAnchor = null;
  });

  // ---------------- fullscreen (wide tables) ----------------
  function toggleFullscreen() {
    const screenEl = document.getElementById('masterTableScreen');
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (screenEl && screenEl.requestFullscreen) {
      screenEl.requestFullscreen().catch(() => {});
    }
  }

  function updateFullscreenButtonLabel() {
    const btn = document.getElementById('masterTableFullscreenBtn');
    if (btn) btn.textContent = document.fullscreenElement ? 'Exit Fullscreen' : 'View Fullscreen';
  }
  document.addEventListener('fullscreenchange', updateFullscreenButtonLabel);

  // ---------------- rendering ----------------
  function render() {
    renderList();
    renderGrid();
  }

  function renderList() {
    const list = document.getElementById('masterTableList');
    if (!list) return;
    list.innerHTML = '';
    const visibleTables = tables.filter((t) => t.id !== MASTER_SOA_ID);
    if (visibleTables.length === 0) {
      list.innerHTML = '<p class="empty-hint">No master tables yet.</p>';
      return;
    }
    visibleTables.forEach((t) => {
      const item = document.createElement('div');
      item.className = 'master-table-list-item' + (t.id === activeId ? ' active' : '');
      item.addEventListener('click', () => setActive(t.id));

      const nameInput = document.createElement('input');
      nameInput.className = 'master-table-name-input';
      nameInput.value = t.name;
      nameInput.addEventListener('click', (e) => e.stopPropagation());
      nameInput.addEventListener('change', () => renameTable(t.id, nameInput.value));

      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'icon-btn';
      delBtn.title = 'Delete table';
      delBtn.textContent = '×';
      delBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteTable(t.id);
      });

      item.appendChild(nameInput);
      item.appendChild(delBtn);
      list.appendChild(item);
    });
  }

  function renderCellEditor(t, rowIndex, colIndex, col, value) {
    if (col.type === 'tags') {
      const wrap = document.createElement('div');
      wrap.className = 'tag-cell';
      (Array.isArray(value) ? value : []).forEach((tag) => {
        const pill = document.createElement('span');
        pill.className = 'tag-pill';
        pill.textContent = tag;
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'tag-pill-remove';
        x.textContent = '×';
        x.addEventListener('click', () => removeTag(t.id, rowIndex, colIndex, tag));
        pill.appendChild(x);
        wrap.appendChild(pill);
      });
      const input = document.createElement('input');
      input.className = 'tag-cell-input';
      input.placeholder = '+ add';
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && input.value.trim()) {
          e.preventDefault();
          addTag(t.id, rowIndex, colIndex, input.value);
        }
      });
      wrap.appendChild(input);
      return wrap;
    }

    const input = document.createElement('input');
    input.className = 'master-cell-input';
    input.type = col.type === 'date' ? 'date' : col.type === 'number' ? 'number' : 'text';
    input.value = value == null ? '' : value;
    input.addEventListener('change', () => setCellValue(t.id, rowIndex, colIndex, input.value));
    return input;
  }

  function renderSelectionBar(container) {
    if (!dragSelection.type || dragSelection.indices.size === 0) return;
    const bar = document.createElement('div');
    bar.className = 'master-table-selection-bar';

    const label = document.createElement('span');
    const noun = dragSelection.type === 'column' ? 'column' : 'row';
    label.textContent = `${dragSelection.indices.size} ${noun}${dragSelection.indices.size === 1 ? '' : 's'} selected`;

    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    deleteBtn.className = 'btn btn-secondary';
    deleteBtn.textContent = 'Delete';
    deleteBtn.addEventListener('click', deleteSelected);

    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'icon-btn';
    clearBtn.title = 'Clear selection';
    clearBtn.textContent = '×';
    clearBtn.addEventListener('click', () => {
      clearDragSelection();
      renderGrid();
    });

    bar.appendChild(label);
    bar.appendChild(deleteBtn);
    bar.appendChild(clearBtn);
    container.appendChild(bar);
  }

  function renderWideTableBanner(container, columnCount) {
    if (columnCount <= WIDE_TABLE_COLUMN_THRESHOLD) return;
    const banner = document.createElement('div');
    banner.className = 'wide-table-banner';

    const text = document.createElement('span');
    text.textContent = `This table has ${columnCount} columns — for easier editing, view in fullscreen.`;

    const fsBtn = document.createElement('button');
    fsBtn.type = 'button';
    fsBtn.id = 'masterTableFullscreenBtn';
    fsBtn.className = 'btn btn-primary';
    fsBtn.textContent = document.fullscreenElement ? 'Exit Fullscreen' : 'View Fullscreen';
    fsBtn.addEventListener('click', toggleFullscreen);

    banner.appendChild(text);
    banner.appendChild(fsBtn);
    container.appendChild(banner);
  }

  function renderGrid() {
    const container = document.getElementById('masterTableGrid');
    if (!container) return;
    container.innerHTML = '';
    const t = getActive();
    if (!t) {
      container.innerHTML = '<p class="empty-hint">Select or create a master table.</p>';
      return;
    }

    const toolbar = document.createElement('div');
    toolbar.className = 'master-table-grid-toolbar';
    const addColBtn = document.createElement('button');
    addColBtn.type = 'button';
    addColBtn.className = 'btn';
    addColBtn.textContent = '+ Add Column';
    addColBtn.addEventListener('click', () => {
      const name = window.prompt('Column name?', `Column ${t.columns.length + 1}`);
      if (name === null) return;
      addColumn(t.id, name, 'text');
    });
    const addRowBtn = document.createElement('button');
    addRowBtn.type = 'button';
    addRowBtn.className = 'btn btn-primary';
    addRowBtn.textContent = '+ Add Row';
    addRowBtn.addEventListener('click', () => addRow(t.id));
    toolbar.appendChild(addColBtn);
    toolbar.appendChild(addRowBtn);
    container.appendChild(toolbar);

    renderWideTableBanner(container, t.columns.length);
    renderSelectionBar(container);

    const table = document.createElement('table');
    table.className = 'master-table-grid';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    t.columns.forEach((col, colIndex) => {
      const th = document.createElement('th');
      th.dataset.colIndex = String(colIndex);
      th.classList.toggle('col-selected', dragSelection.type === 'column' && dragSelection.indices.has(colIndex));

      const grip = document.createElement('span');
      grip.className = 'col-drag-handle';
      grip.title = 'Drag to select multiple columns';
      grip.textContent = '⋮⋮';
      grip.addEventListener('mousedown', (e) => {
        e.preventDefault();
        startColumnDrag(colIndex);
      });

      const nameInput = document.createElement('input');
      nameInput.className = 'master-col-name-input';
      nameInput.value = col.name;
      nameInput.addEventListener('change', () => renameColumn(t.id, colIndex, nameInput.value));

      const typeSelect = document.createElement('select');
      typeSelect.className = 'master-col-type-select';
      COLUMN_TYPES.forEach((type) => {
        const opt = document.createElement('option');
        opt.value = type;
        opt.textContent = type;
        if (type === col.type) opt.selected = true;
        typeSelect.appendChild(opt);
      });
      typeSelect.addEventListener('change', () => setColumnType(t.id, colIndex, typeSelect.value));

      const delColBtn = document.createElement('button');
      delColBtn.type = 'button';
      delColBtn.className = 'icon-btn';
      delColBtn.title = 'Delete column';
      delColBtn.textContent = '×';
      delColBtn.addEventListener('click', () => deleteColumn(t.id, colIndex));

      th.appendChild(grip);
      th.appendChild(nameInput);
      th.appendChild(typeSelect);
      th.appendChild(delColBtn);
      headRow.appendChild(th);
    });
    headRow.appendChild(document.createElement('th'));
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    t.rows.forEach((row, rowIndex) => {
      const tr = document.createElement('tr');
      tr.dataset.rowIndex = String(rowIndex);
      tr.classList.toggle('row-selected', dragSelection.type === 'row' && dragSelection.indices.has(rowIndex));

      t.columns.forEach((col, colIndex) => {
        const td = document.createElement('td');
        td.appendChild(renderCellEditor(t, rowIndex, colIndex, col, row[colIndex]));
        tr.appendChild(td);
      });
      const actionTd = document.createElement('td');
      const rowGrip = document.createElement('span');
      rowGrip.className = 'row-drag-handle';
      rowGrip.title = 'Drag to select multiple rows';
      rowGrip.textContent = '⋮⋮';
      rowGrip.addEventListener('mousedown', (e) => {
        e.preventDefault();
        startRowDrag(rowIndex);
      });
      const delRowBtn = document.createElement('button');
      delRowBtn.type = 'button';
      delRowBtn.className = 'icon-btn';
      delRowBtn.title = 'Delete row';
      delRowBtn.textContent = '×';
      delRowBtn.addEventListener('click', () => deleteRow(t.id, rowIndex));
      actionTd.appendChild(rowGrip);
      actionTd.appendChild(delRowBtn);
      tr.appendChild(actionTd);
      tbody.appendChild(tr);
    });
    if (t.rows.length === 0) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = t.columns.length + 1;
      td.className = 'empty-hint';
      td.textContent = 'No rows yet — use "+ Add Row" to start.';
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    container.appendChild(table);
  }

  window.MasterTableView = {
    createFromPageTables,
    exportActive,
    getActiveName,
    setSourceDocument,
    setSourceDocumentTextProvider,
    getTablesByName,
    setActiveById: setActive,
    regenerateMasterSoa,
    setOnMasterSoaStateChange,
    isMasterSoaBusy,
    hasSourceDocument,
    refresh: render,
  };
})();
