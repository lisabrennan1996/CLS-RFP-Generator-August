// Orchestrates the whole UI: model bootstrap, file selection, running OCR via Tauri
// commands, and wiring the Configuration/Results panels. The Blocks-tab-specific rendering
// (bbox overlay + block list + selection) lives in blocks-view.js (window.BlocksView).
(function () {
  // ---------------- HTTP backend bridge (replaces Tauri IPC) ----------------
  // Every one of this file's ~27 `invoke(cmd, args)` call sites is unchanged from the desktop
  // app -- only this function's own implementation and a handful of file-dialog/drag-drop
  // touch points below needed rewriting. Session id scopes all uploaded/converted/generated
  // files server-side (a browser tab has no native file handles the way a desktop process
  // does); the rest of this app's own `state` object still lives entirely in memory here,
  // unchanged.
  const SESSION_ID =
    window.crypto && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

  // Defaults to same-origin ('') -- unchanged behavior when served by the FastAPI backend
  // itself. Lets the single-file build (see build-single-file.js) point at a backend running
  // elsewhere, via ?api=http://host:port in the URL or window.RFP_API_BASE set before this
  // script runs.
  const API_BASE = (function () {
    try {
      const q = new URLSearchParams(window.location.search).get('api');
      if (q) return q.replace(/\/$/, '');
    } catch (e) { /* ignore */ }
    if (typeof window.RFP_API_BASE === 'string') return window.RFP_API_BASE.replace(/\/$/, '');
    return '';
  })();

  async function apiPost(path, body) {
    const res = await fetch(`${API_BASE}/api/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session-Id': SESSION_ID },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(text || `${path} failed (${res.status})`);
    }
    return res.json();
  }

  async function apiGet(path) {
    const res = await fetch(`${API_BASE}/api/${path}`, {
      headers: { 'X-Session-Id': SESSION_ID },
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(text || `${path} failed (${res.status})`);
    }
    return res.json();
  }

  function pickFileViaInput(accept) {
    return new Promise((resolve, reject) => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = accept;
      input.style.display = 'none';
      document.body.appendChild(input);
      let settled = false;
      const cleanup = () => {
        input.remove();
        window.removeEventListener('focus', onFocus);
      };
      const onChange = () => {
        if (settled) return;
        const file = input.files && input.files[0];
        settled = true;
        cleanup();
        if (!file) reject(new Error('No file selected'));
        else resolve(file);
      };
      const onCancel = () => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(new Error('No file selected'));
      };
      // Modern Chromium/Edge fire a native 'cancel' event on the input when the file dialog is
      // dismissed with no selection -- the reliable signal to use. The window 'focus' listener
      // below is a fallback ONLY, for a browser old enough to lack 'cancel' -- it used to be
      // the ONLY detection mechanism, racing a 300ms timer against the dialog's own 'change'
      // event. Confirmed directly as a real bug: a real file selection whose 'change' is
      // delayed past 300ms (slower machine, antivirus scanning the picker, plain OS timing)
      // got misdetected as "cancelled" -- and since isCancelled() suppresses the error toast
      // for exactly that message, the whole attach action silently did nothing, which reads
      // identically to "the file wasn't accepted." Given 'cancel' now handles the fast/common
      // path, this fallback's window can be generous without making a real cancel feel laggy.
      const onFocus = () => setTimeout(() => { if (!settled && document.body.contains(input)) onCancel(); }, 1500);
      input.addEventListener('change', onChange, { once: true });
      input.addEventListener('cancel', onCancel, { once: true });
      window.addEventListener('focus', onFocus, { once: true });
      input.click();
    });
  }

  async function uploadFileToServer(file) {
    const form = new FormData();
    form.append('file', file, file.name);
    const res = await fetch(`${API_BASE}/api/upload`, {
      method: 'POST',
      headers: { 'X-Session-Id': SESSION_ID },
      body: form,
    });
    if (!res.ok) throw new Error((await res.text().catch(() => '')) || 'upload failed');
    return res.json(); // {file_id, name, ext}
  }

  // Uploads several files in one request (used by the CLIPS/Non-PKPD multi-attach picker below) --
  // HTTP equivalent of the Tauri desktop app's multi-select `pick_documents` command.
  async function uploadMultiFilesToServer(files) {
    const form = new FormData();
    Array.from(files).forEach((file) => form.append('files', file, file.name));
    const res = await fetch(`${API_BASE}/api/upload-multi`, {
      method: 'POST',
      headers: { 'X-Session-Id': SESSION_ID },
      body: form,
    });
    if (!res.ok) throw new Error((await res.text().catch(() => '')) || 'upload failed');
    return res.json(); // [{file_id, name, ext}, ...]
  }

  // Browser analog of the Tauri desktop app's native multi-file picker (`pick_documents`) --
  // resolves with a FileList once the user picks file(s) via the hidden <input type="file" multiple>
  // at #clipsNonPkpdFileInput, or rejects with "No file selected" if the dialog is dismissed.
  function pickMultipleFilesViaInput(input) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const cleanup = () => {
        input.removeEventListener('change', onChange);
        input.removeEventListener('cancel', onCancel);
        window.removeEventListener('focus', onFocus);
      };
      const onChange = () => {
        if (settled) return;
        // input.files is a LIVE reference, not a snapshot -- confirmed directly: clearing
        // input.value below (needed so this persistent, reusable input can be attached-to
        // again later) retroactively empties this SAME FileList object, so checking
        // files.length after that point is always 0 regardless of what was actually picked.
        // This was the real, 100%-reproducible cause of CLIPS/Non-PKPD attachment silently
        // doing nothing -- materialize a real array of the File objects themselves (which
        // aren't affected by clearing the input) before touching .value at all.
        const files = input.files && input.files.length ? Array.from(input.files) : null;
        settled = true;
        cleanup();
        input.value = '';
        if (!files) reject(new Error('No file selected'));
        else resolve(files);
      };
      const onCancel = () => {
        if (settled) return;
        settled = true;
        cleanup();
        input.value = '';
        reject(new Error('No file selected'));
      };
      // See pickFileViaInput()'s own comment -- same fix, same reason: the native 'cancel'
      // event is the reliable signal, the focus-based timer is a long-window fallback only,
      // not a 300ms race against a real (possibly delayed) 'change' event.
      const onFocus = () => setTimeout(() => { if (!settled) onCancel(); }, 1500);
      input.addEventListener('change', onChange, { once: true });
      input.addEventListener('cancel', onCancel, { once: true });
      window.addEventListener('focus', onFocus, { once: true });
      input.click();
    });
  }

  async function triggerDownload(fileId, suggestedName) {
    const res = await fetch(`${API_BASE}/api/download/${encodeURIComponent(fileId)}?name=${encodeURIComponent(suggestedName)}`, {
      headers: { 'X-Session-Id': SESSION_ID },
    });
    if (!res.ok) throw new Error((await res.text().catch(() => '')) || 'download failed');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = suggestedName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  const invoke = async (cmd, args) => {
    args = args || {};
    switch (cmd) {
      case 'pick_document': {
        // .docx (not .doc -- python-docx can't read the legacy binary format) is kept here
        // for "Previous RFP" attachment, which reads a .docx directly and never needs
        // conversion; the main protocol upload only accepts a PDF in practice (Word
        // conversion isn't supported -- see documents.py).
        const file = await pickFileViaInput('.pdf,.docx');
        const uploaded = await uploadFileToServer(file);
        return { path: uploaded.file_id, name: uploaded.name };
      }
      case 'pick_save_path':
        // No native "Save As" location picker in a browser -- the real save happens as a
        // triggered download once populate_rfp_docx/export_output complete (see those cases
        // below). This just hands back a display-only name for the success toast.
        return args.suggestedName;
      case 'export_output': {
        const result = await apiPost('export-text', { content: args.content, suggestedName: args.suggestedName });
        await triggerDownload(result.file_id, result.name);
        return null;
      }
      case 'ensure_pdf_path': {
        const result = await apiPost('convert-to-pdf', { path: args.path });
        return result.file_id;
      }
      case 'rasterize_document':
        return apiPost('rasterize', args);
      case 'extract_tables':
        return apiPost('extract-tables', args);
      case 'extract_rfp_schema':
        // The Tauri command returned a JSON-encoded string here (call sites already
        // `JSON.parse()` the result) -- re-stringify so that contract still holds.
        return JSON.stringify(await apiPost('extract-schema', args));
      case 'populate_rfp_docx': {
        const result = await apiPost('generate-rfp', args);
        if (result.status === 'complete') {
          await triggerDownload(result.file_id, args.outputPath || 'RFP.docx');
        }
        return JSON.stringify(result);
      }
      case 'extract_master_schedule':
        return JSON.stringify(await apiPost('master-schedule', args));
      case 'preview_clips_nonpkpd_files':
        // Same JSON-encoded-string contract as extract_rfp_schema above -- call sites already
        // JSON.parse() the result, matching the Tauri command's own return shape.
        return JSON.stringify(await apiPost('clips-nonpkpd-preview', { paths: args.paths }));
      case 'preview_previous_rfp':
        return JSON.stringify(await apiPost('preview-previous-rfp', { path: args.path }));
      case 'fetch_fabric_design_fields':
        return JSON.stringify(await apiPost('fabric-design-fields', { protocolAlias: args.protocolAlias }));
      default:
        throw new Error(`Unknown command: ${cmd}`);
    }
  };
  // No server-push events are needed in the web version -- every `listen(...)` call site in
  // this file was the Tauri drag-drop event, replaced below with a real browser `drop` handler.
  const listen = () => {};
  // Exposed so master-table.js's own invoke() can delegate here instead of duplicating the
  // fetch/session-id logic (and, critically, so both files share one session id).
  window.__invoke = invoke;

  // Fixed rasterization DPI used for every page preview — needed again at parse time to
  // convert a drawn region's screen-pixel rectangle into the PDF-point-space coordinates
  // Camelot's `table_areas` expects (see regionToTableArea).
  const RASTERIZE_DPI = 150;

  // Camelot parsing flavor is fixed to 'lattice' (ruled tables — ie. the vast majority of real
  // protocol/SoA documents) throughout the app; there is deliberately no UI to change it anymore
  // (confirmed with the user: the Lattice/Stream choice was more confusion than value in
  // practice — every document they use is ruled). Kept as a named constant, not inlined, so the
  // handful of `extract_tables` call sites below still read clearly.
  const FLAVOR = 'lattice';

  // Outward padding (page-image pixel space, at RASTERIZE_DPI) applied to every hand-drawn
  // region before it's converted to Camelot's point-space table_regions string — see
  // regionToTableArea. Confirmed directly against a real PDF: a drawn box that is even slightly
  // too tight around a table's outer ruling lines can fail to detect any table at all, while a
  // slightly loose box still succeeds. This constant absorbs ordinary drawing imprecision
  // without being so large it risks pulling in an adjacent, unrelated table.
  const CROP_PAD_PX = 15;

  const state = {
    pickedFile: null, // { path, name }
    pageImages: [], // [{ page, mime, base64, width, height }] — page previews/thumbnails only
    pageResults: [], // [{ page, markdown, blocks }] — blocks are real table regions once parsed
    currentPage: 0,
    screen: 'workspace', // 'workspace' | 'masterTable' | 'tableCrop'
    selectedCombinePages: new Set(), // pages staged for the *next* group about to be queued
    combineQueue: [], // [{ id, name, pages: [idx,...] }] — groups queued, not yet cropped
    regionPage: 0, // which page the Table Region drawing step is currently showing
    regionsByPage: {}, // { [pageIndex]: {x0,y0,x1,y1} in page-image pixel space } — drawn boxes
    tablePagesConfig: null, // Set<pageIndex> once "Tables Only" detection has run in Configuration
    tablesOnlyConfig: false, // whether the Configuration/region step is filtered to table pages
    autoDetectCache: null, // { results } from the "Tables Only" detection pass, reused by
    // runParse() when no region overrides would make its own parse differ.

    // ---- Combine Tables' crop step (Crop & Merge) ----
    cropQueue: [], // snapshot of combineQueue being worked through right now: [{id, name, pages}]
    cropQueueIndex: 0, // which group in cropQueue is currently on the crop screen
    cropPages: [], // page indices being cropped this session (the pages picked in Combine Tables)
    cropIndex: 0, // position within cropPages currently shown
    cropPageImages: {}, // { [pageIndex]: PageImage } for just cropPages (rasterize_document's pages filter)
    cropTableName: '', // the current group's name, carried into this screen
    // { [pageIndex]: [{x0,y0,x1,y1}, ...] } in page-image pixel space — one or more drawn boxes
    // per page (e.g. a header row and a data row further down, cropped as two separate drags).
    // Deliberately separate from regionsByPage (the original Configuration step's own single-
    // region-per-page state) so this screen's multi-region support can't change that flow.
    cropRegionsByPage: {},

    // ---- Table-work layout (auto-collapsed source panel + nav-rail thumbnail-mode) ----
    sourcePanelManuallyExpanded: false, // user override of the auto-collapse; reset on fresh parse
    // User override that keeps the 6-item section nav visible even while table work is
    // "active" (see tableWorkActive/updateTableWorkLayout) -- without this, once a document is
    // parsed the thumbnail rail permanently replaces Intake/Information/Countries/etc. for the
    // rest of the session, with no way back short of clearing the file. Toggled by
    // #showSectionsBtn/#showThumbnailsBtn, and forced true by the Home button so Home reliably
    // restores the section nav from any state.
    navRailShowSections: false,
    tablesOnlyThumbnails: false, // filters the thumbnail rail (and Prev/Next) to pages with a detected table
    thumbnailRailWidth: 320, // px -- user-resizable via the drag handle, persists for the session
    thumbnailModalOpen: false, // full-screen preview modal (see wireThumbnailModal)
    thumbnailModalPageIndex: 0, // which group of THUMBNAIL_MODAL_PAGE_SIZE pages is showing

    // ---- Generate RFP (master-table screen) ----
    previousRfpDoc: null, // { path, name } | null
    // {table_role: {"table_idx", "columns": [{key, table_role, col_index, base_label,
    // display_label, tag, has_data}], "rows": {row_label: {col_key: value}}}} from
    // POST /api/preview-previous-rfp (see build_specimen.py's own docstring for the exact
    // shape) -- null until a Previous RFP is attached and its preview call resolves.
    previousRfpPreview: null,
    // {table_role: [column_key, ...]} -- which of previousRfpPreview's real columns the
    // user has checked. Defaults to every column with has_data=true once the preview
    // loads, but stays live-editable via checkboxes; sent as previousRfpColumnSelection
    // in the generate-rfp payload. null until a preview has loaded at least once.
    previousRfpColumnSelection: null,
    // Design/ops fields (Therapeutic Area, Phase, Pediatric flag, Country Allocation,
    // enrollment/screen counts, trial milestones) fetched automatically from Fabric by whatever
    // protocol alias extract_rfp_schema found -- see maybeAutoSearchFabricDesignFields. Kept
    // separate from extractedFields (rather than only merged in once) so it survives
    // refreshExtractedFieldsPreview's own wholesale reassignment of that map.
    fabricDesignFields: null,
    fabricProtocolAlias: '', // the alias last successfully searched -- shown in the attachments status line
    fabricAutoSearchedAlias: '', // the alias last *attempted* (success or not) -- guards against re-searching the same alias on every parse/attach re-run
    // Alternative to previousRfpDoc for Referral Lab/Storage Samples: CLIPS forms / Non-PK Data
    // Mgmt Worksheets read directly instead of reverse-parsing a previous RFP -- see
    // attachClipsNonPkpdFiles. [{path, name, docType, column, fields}] -- `path` is a session
    // file id (see /api/upload-multi); `column` is a specimen_columns.py registry `key` (e.g.
    // "storage_wide:2"), NOT a bare column name -- starts at whatever the preview endpoint's
    // auto-detected base label maps to (see attachClipsNonPkpdFiles), but can be overridden
    // per-row in the UI. Multiple files may share the same column key -- populate_rfp.py gives
    // each its own real "<label> (1)"/"(2)" Word table column rather than overwriting one another.
    clipsNonPkpdFiles: [],
    // [{key, table_role, col_index, base_label, display_label, tag}, ...] (snake_case, unchanged
    // from the raw JSON response) fetched once from GET /api/specimen-columns
    // (specimen_columns.py) -- every real column in the template's Specimen Management /
    // Referral Lab tables, the single source of truth for the dropdown (replaces a hardcoded
    // 6-name list that had drifted from the template's actual 12 columns).
    specimenColumns: null,

    // ---- Top-level nav section (Intake/Information/Countries/Schedule/Analytes/Specimens) ----
    section: 'intake',
    sidebarCollapsed: false,

    // ---- Information section ----
    sponsorContacts: [], // [{id, name, role, email, tag}] -- first entry feeds the docx's one
    // existing "requestor contact" line; the rest are reference-only for now (see plan).
    protocolNumber: '', // manual override -- blank means keep auto-extraction
    phase: '', // 'I' | 'II' | 'III' | 'IV' | '' (auto)
    therapeuticArea: '',

    // ---- Countries section ----
    manualCountries: [], // plain strings, feeds answers.country_allocation

    // ---- Specimens section ----
    storageConditions: '', // 'Ambient' | 'Refrigerated (2–8°C)' | 'Frozen (-20°C)' | ''
    kitType: '', // 'Standard collection kit' | 'Custom kit' | ''

    // ---- Live-preview of extract_rfp_schema's findings (Information/Countries) ----
    // Flat {fieldName: value} map, rebuilt after parsing and after attaching a Previous RFP —
    // whichever of those last ran. null until the first successful extraction.
    extractedFields: null,
  };

  function $(sel) {
    return document.querySelector(sel);
  }
  function $all(sel) {
    return Array.from(document.querySelectorAll(sel));
  }
  function isCancelled(err) {
    return String(err).toLowerCase().includes('no file selected');
  }

  function showToast(message, kind) {
    const stack = $('#toastStack');
    const el = document.createElement('div');
    el.className = 'toast toast-' + (kind || 'info');
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(() => el.remove(), 5000);
  }

  // ---------------- generic tab wiring ----------------
  function wireTabs(tabSelector, panelAttr, datasetKey) {
    $all(tabSelector).forEach((btn) => {
      btn.addEventListener('click', () => {
        $all(tabSelector).forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        const key = btn.dataset[datasetKey];
        $all('[' + panelAttr + ']').forEach((panel) => {
          panel.hidden = panel.getAttribute(panelAttr) !== key;
        });
      });
    });
  }

  // ---------------- engine bootstrap ----------------
  // The desktop app bundled a separate camelot-worker.exe sidecar process and needed to
  // confirm it was present ('get_worker_status') before enabling Parse Document. The webapp
  // has no such sidecar at all -- Camelot/pdfplumber run in-process inside the FastAPI
  // backend itself (see backend/services/tables_extract.py) -- so there's nothing to check
  // here; a real import/dependency problem would surface as a clear error from the actual
  // /api/extract-tables call instead. This just marks the engine ready immediately so Parse
  // Document is enabled as soon as a file is picked.
  function setModelStatus(kind, text) {
    $('#modelStatusDot').className = 'status-dot status-' + kind;
    $('#modelStatusText').textContent = text;
  }

  async function bootstrapModel() {
    setModelStatus('ready', 'Ready');
    updateParseButtonState();
  }

  function modelIsReady() {
    return $('#modelStatusDot').classList.contains('status-ready');
  }

  function updateParseButtonState() {
    $('#parseBtn').disabled = !(state.pickedFile && modelIsReady());
  }

  // ---------------- source panel ----------------
  async function selectFile(droppedFile) {
    try {
      let picked;
      if (droppedFile) {
        // A real File object from a browser drag-drop (see wireSourcePanel below) -- a browser
        // can't read an arbitrary local path by string the way the desktop app could, so this
        // always uploads actual file bytes rather than referencing a "path".
        const uploaded = await uploadFileToServer(droppedFile);
        picked = { path: uploaded.file_id, name: uploaded.name };
      } else {
        picked = await invoke('pick_document');
      }

      // Word documents are converted to a temp PDF here, once, before anything else in the
      // pipeline (rasterize_document, extract_tables) ever sees the path — ensure_pdf_path
      // returns an already-PDF path unchanged, so this is always safe/cheap to call regardless
      // of file type. The original name (e.g. "Protocol.docx") still shows in the picked-file
      // label; only the internal working path becomes the converted PDF's.
      const isWordDoc = /\.(docx|doc)$/i.test(picked.name);
      if (isWordDoc) {
        $('#pickedFileName').textContent = `${picked.name} (converting to PDF…)`;
        $('#pickedFile').hidden = false;
      }
      let workingPath;
      try {
        workingPath = await invoke('ensure_pdf_path', { path: picked.path });
      } catch (err) {
        showToast('Could not convert ' + picked.name + ' to PDF: ' + err, 'error');
        if (isWordDoc) $('#pickedFile').hidden = true;
        return;
      }
      picked = { path: workingPath, name: picked.name };

      state.pickedFile = picked;
      state.regionsByPage = {};
      state.regionPage = 0;
      state.tablePagesConfig = null;
      state.tablesOnlyConfig = false;
      state.autoDetectCache = null;
      $('#regionTablesOnlyBtn').classList.remove('active');
      $('#regionTablesOnlyHint').hidden = true;
      if (window.MasterTableView) window.MasterTableView.setSourceDocument(picked);
      $('#pickedFileName').textContent = picked.name;
      $('#pickedFile').hidden = false;
      updateParseButtonState();

      // Rasterized right away (not at parse time) so the Table Region step has page previews
      // to draw on before the user ever clicks Parse Document.
      $('#regionStep').hidden = true;
      try {
        const images = await invoke('rasterize_document', { path: picked.path, dpi: RASTERIZE_DPI });
        state.pageImages = images;
        if (images.length > 0) {
          $('#regionStep').hidden = false;
          renderRegionStep();
        }
      } catch (err) {
        showToast('Could not preview pages: ' + err, 'error');
      }
    } catch (err) {
      if (isCancelled(err)) return;
      showToast('Could not read file: ' + err, 'error');
    }
  }

  function clearFile() {
    state.pickedFile = null;
    state.pageImages = [];
    state.pageResults = [];
    state.currentPage = 0;
    state.regionsByPage = {};
    state.tablePagesConfig = null;
    state.tablesOnlyConfig = false;
    state.autoDetectCache = null;
    state.selectedCombinePages.clear();
    state.combineQueue = [];
    state.extractedFields = null;
    // A new document may have a different protocol alias -- let the next parse's
    // refreshExtractedFieldsPreview auto-trigger a fresh Fabric lookup for it, and drop whatever
    // the previous document's lookup filled in rather than carrying it over to an unrelated protocol.
    state.fabricDesignFields = null;
    state.fabricProtocolAlias = '';
    state.fabricAutoSearchedAlias = '';
    $('#regionTablesOnlyBtn').classList.remove('active');
    $('#regionTablesOnlyHint').hidden = true;
    if (window.MasterTableView) window.MasterTableView.setSourceDocument(null);
    $('#pickedFile').hidden = true;
    $('#regionStep').hidden = true;
    updateParseButtonState();
    updateTableWorkLayout();
  }

  // ---------------- shared "step through a filtered page list" helper ----------------
  // Used by both the Configuration/region step and the Results tab's "Tables Only" toggles:
  // `list` is either every page index (filter off) or just the indices with detected tables
  // (filter on). Stepping from a page that isn't itself in `list` (e.g. the filter was just
  // turned on while sitting on a non-table page) jumps to the nearest list entry in the
  // requested direction instead of silently doing nothing.
  function stepInList(list, current, delta) {
    if (!list.length) return current;
    const pos = list.indexOf(current);
    if (pos === -1) {
      if (delta > 0) return list.find((p) => p > current) ?? list[list.length - 1];
      return [...list].reverse().find((p) => p < current) ?? list[0];
    }
    return list[Math.max(0, Math.min(list.length - 1, pos + delta))];
  }

  function allPageIndices(count) {
    return Array.from({ length: count }, (_, i) => i);
  }

  // ---------------- table region drawing ----------------
  function renderRegionOverlay() {
    const svg = $('#regionOverlay');
    svg.innerHTML = '';
    svg.setAttribute('viewBox', '0 0 1000 1000');
    svg.setAttribute('preserveAspectRatio', 'none');

    const region = state.regionsByPage[state.regionPage];
    const img = state.pageImages[state.regionPage];
    if (!region || !img) return;

    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    const x0 = (Math.min(region.x0, region.x1) / img.width) * 1000;
    const y0 = (Math.min(region.y0, region.y1) / img.height) * 1000;
    const w = (Math.abs(region.x1 - region.x0) / img.width) * 1000;
    const h = (Math.abs(region.y1 - region.y0) / img.height) * 1000;
    rect.setAttribute('x', x0);
    rect.setAttribute('y', y0);
    rect.setAttribute('width', w);
    rect.setAttribute('height', h);
    rect.classList.add('region-rect');
    svg.appendChild(rect);
  }

  function configPageList() {
    return state.tablesOnlyConfig && state.tablePagesConfig
      ? Array.from(state.tablePagesConfig).sort((a, b) => a - b)
      : allPageIndices(state.pageImages.length);
  }

  function renderRegionStep() {
    const img = state.pageImages[state.regionPage];
    if (!img) return;
    $('#regionPreviewImage').src = `data:${img.mime};base64,${img.base64}`;
    const list = configPageList();
    if (state.tablesOnlyConfig && state.tablePagesConfig) {
      const pos = list.indexOf(state.regionPage);
      $('#regionPageLabel').textContent =
        `Table page ${pos + 1} / ${list.length} (page ${state.regionPage + 1} of ${state.pageImages.length} overall)`;
    } else {
      $('#regionPageLabel').textContent = `Page ${state.regionPage + 1} / ${state.pageImages.length}`;
    }
    const pos = list.indexOf(state.regionPage);
    $('#regionPrevBtn').disabled = pos <= 0;
    $('#regionNextBtn').disabled = pos === -1 || pos === list.length - 1;
    renderRegionOverlay();
  }

  function updateRegionTablesOnlyHint() {
    const hint = $('#regionTablesOnlyHint');
    if (!state.tablePagesConfig) {
      hint.hidden = true;
      return;
    }
    hint.hidden = false;
    hint.textContent = `${state.tablePagesConfig.size} of ${state.pageImages.length} page(s) have detected tables.`;
  }

  async function toggleRegionTablesOnly() {
    if (!state.tablesOnlyConfig) {
      if (!state.tablePagesConfig) {
        const btn = $('#regionTablesOnlyBtn');
        btn.disabled = true;
        btn.textContent = 'Detecting…';
        try {
          // Whole-document autodetect (no manual regions) — the same call "Parse Document"
          // itself makes, just run early so the region step can filter by it. Cached so a later
          // Parse Document click (if the user hasn't drawn any regions since) can reuse this
          // instead of re-parsing.
          const results = await invoke('extract_tables', {
            path: state.pickedFile.path,
            flavor: FLAVOR,
            tableAreasByPage: {},
            flavorByPage: {},
          });
          state.tablePagesConfig = new Set(results.filter((r) => r.blocks.length > 0).map((r) => r.page));
          state.autoDetectCache = { results };
        } catch (err) {
          showToast('Could not detect tables: ' + err, 'error');
          btn.disabled = false;
          btn.textContent = 'Tables Only';
          return;
        }
        btn.disabled = false;
        btn.textContent = 'Tables Only';
      }
      state.tablesOnlyConfig = true;
    } else {
      state.tablesOnlyConfig = false;
    }
    $('#regionTablesOnlyBtn').classList.toggle('active', state.tablesOnlyConfig);
    updateRegionTablesOnlyHint();
    if (state.tablesOnlyConfig && state.tablePagesConfig && !state.tablePagesConfig.has(state.regionPage)) {
      const list = configPageList();
      if (list.length) state.regionPage = list[0];
    }
    renderRegionStep();
  }

  // Converts a drawn rectangle (page-image pixel space, origin top-left, y-down) into Camelot's
  // own `table_regions`/`table_areas` string convention: "x1,y1,x2,y2" where (x1,y1) is left-TOP
  // and (x2,y2) is right-BOTTOM, in PDF point space (origin bottom-left, y-up) — confirmed
  // directly against a real PDF that this is the opposite y-order from a returned block's own
  // bbox (see worker/parse_tables.py's module docstring). `scale` converts pixels back to points
  // using the fixed rasterization DPI (points = pixels * 72 / dpi). The region is padded outward
  // by CROP_PAD_PX first (clamped to the image bounds) — see that constant's own comment.
  function regionToTableArea(region, img) {
    const scale = 72 / RASTERIZE_DPI;
    const x0 = Math.max(0, Math.min(region.x0, region.x1) - CROP_PAD_PX);
    const x1 = Math.min(img.width, Math.max(region.x0, region.x1) + CROP_PAD_PX);
    const y0 = Math.max(0, Math.min(region.y0, region.y1) - CROP_PAD_PX);
    const y1 = Math.min(img.height, Math.max(region.y0, region.y1) + CROP_PAD_PX);
    const xLeft = x0 * scale;
    const xRight = x1 * scale;
    const yTop = (img.height - y0) * scale;
    const yBottom = (img.height - y1) * scale;
    return `${xLeft},${yTop},${xRight},${yBottom}`;
  }

  function toImagePixel(e, img) {
    const rect = img.getBoundingClientRect();
    const pageImg = state.pageImages[state.regionPage];
    const relX = (e.clientX - rect.left) / rect.width;
    const relY = (e.clientY - rect.top) / rect.height;
    return {
      x: Math.max(0, Math.min(pageImg.width, relX * pageImg.width)),
      y: Math.max(0, Math.min(pageImg.height, relY * pageImg.height)),
    };
  }

  function wireRegionStep() {
    const wrap = $('#regionPreviewWrap');
    const img = $('#regionPreviewImage');
    let dragging = false;
    let anchor = null;

    wrap.addEventListener('mousedown', (e) => {
      if (!state.pageImages[state.regionPage]) return;
      dragging = true;
      anchor = toImagePixel(e, img);
      state.regionsByPage[state.regionPage] = { x0: anchor.x, y0: anchor.y, x1: anchor.x, y1: anchor.y };
      renderRegionOverlay();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const p = toImagePixel(e, img);
      state.regionsByPage[state.regionPage] = { x0: anchor.x, y0: anchor.y, x1: p.x, y1: p.y };
      renderRegionOverlay();
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      // A stray click without a real drag draws a degenerate (near-zero-size) box — treat that
      // as "no region drawn" rather than sending Camelot a table_areas sliver that can only
      // ever find zero tables.
      const region = state.regionsByPage[state.regionPage];
      if (region && Math.abs(region.x1 - region.x0) < 4 && Math.abs(region.y1 - region.y0) < 4) {
        delete state.regionsByPage[state.regionPage];
        renderRegionOverlay();
      }
    });

    $('#regionPrevBtn').addEventListener('click', () => {
      state.regionPage = stepInList(configPageList(), state.regionPage, -1);
      renderRegionStep();
    });
    $('#regionNextBtn').addEventListener('click', () => {
      state.regionPage = stepInList(configPageList(), state.regionPage, 1);
      renderRegionStep();
    });
    $('#regionClearBtn').addEventListener('click', () => {
      delete state.regionsByPage[state.regionPage];
      renderRegionOverlay();
    });
    $('#regionTablesOnlyBtn').addEventListener('click', toggleRegionTablesOnly);
  }

  function wireSourcePanel() {
    $('#browseBtn').addEventListener('click', () => selectFile());
    $('#clearFileBtn').addEventListener('click', clearFile);
    $('#fetchUrlBtn').addEventListener('click', () => {
      // A browser can't read an arbitrary local filesystem path by string the way the desktop
      // app could (no OS-level file access outside a user-driven picker/drop gesture) -- this
      // field's original purpose (paste a path from Explorer's "Copy as path") has no web
      // equivalent, so it's disabled here rather than silently failing on every use.
      if ($('#urlInput').value.trim()) {
        showToast('Pasting a file path isn’t supported in the browser version — use Browse or drag a file in instead.', 'error');
      }
    });

    const dz = $('#dropzone');
    ['dragenter', 'dragover'].forEach((evt) =>
      dz.addEventListener(evt, (e) => {
        e.preventDefault();
        dz.classList.add('dragover');
      })
    );
    dz.addEventListener('dragleave', (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
    });
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) selectFile(file);
    });
  }

  // ---------------- configuration panel ----------------
  function wireConfigPanel() {
    $('#parseBtn').addEventListener('click', runParse);
  }

  // ---------------- run extraction ----------------
  async function runParse() {
    if (!state.pickedFile) return;
    const btn = $('#parseBtn');
    btn.disabled = true;
    btn.textContent = 'Parsing…';
    $('#configError').hidden = true;

    try {
      // Page previews were already rasterized right after file selection (see selectFile) so
      // the Table Region step had something to draw on — no need to fetch them again here.
      const tableAreasByPage = {};
      Object.keys(state.regionsByPage).forEach((pageIdxStr) => {
        const region = state.regionsByPage[pageIdxStr];
        const img = state.pageImages[Number(pageIdxStr)];
        if (img) tableAreasByPage[pageIdxStr] = [regionToTableArea(region, img)];
      });

      // If "Tables Only" already ran a whole-document autodetect and no regions have been drawn
      // since, reuse it instead of re-parsing the whole document a second time.
      const noOverrides = Object.keys(tableAreasByPage).length === 0;
      let results;
      if (noOverrides && state.autoDetectCache) {
        results = state.autoDetectCache.results;
      } else {
        results = await invoke('extract_tables', {
          path: state.pickedFile.path,
          flavor: FLAVOR,
          tableAreasByPage,
          flavorByPage: {},
        });
      }
      state.pageResults = results;
      state.currentPage = 0;
      // A fresh parse means auto-collapse should win again even if the user had manually
      // re-expanded the source panel while reviewing a previous document this session.
      state.sourcePanelManuallyExpanded = false;
      // Same idea: a fresh parse should show its own thumbnails automatically, even if the user
      // had switched to the section nav while reviewing a previous document this session.
      state.navRailShowSections = false;
      $('[data-panel-tab="results"]').click();
      renderCurrentPage();
      updateTableWorkLayout();
      refreshExtractedFieldsPreview();
    } catch (err) {
      $('#configError').hidden = false;
      $('#configError').textContent = 'Parse failed: ' + err;
      showToast('Parse failed: ' + err, 'error');
    } finally {
      btn.textContent = 'Parse Document';
      updateParseButtonState();
    }
  }

  // ---------------- results rendering ----------------
  function currentPageResult() {
    return state.pageResults[state.currentPage];
  }
  function currentPageImage() {
    return state.pageImages[state.currentPage];
  }

  function resultsPageList() {
    return state.tablesOnlyThumbnails
      ? state.pageResults
          .map((r, i) => (r.blocks && r.blocks.length > 0 ? i : -1))
          .filter((i) => i !== -1)
      : allPageIndices(state.pageResults.length);
  }

  function renderCurrentPage() {
    const result = currentPageResult();
    const image = currentPageImage();
    if (!result || !image) return;

    const list = resultsPageList();
    const pos = list.indexOf(state.currentPage);
    if (state.tablesOnlyThumbnails) {
      $('#pageIndicator').textContent =
        `Table page ${pos + 1} / ${list.length} (page ${state.currentPage + 1} of ${state.pageResults.length} overall)`;
    } else {
      $('#pageIndicator').textContent = `Page ${state.currentPage + 1} / ${state.pageResults.length}`;
    }
    $('#prevPageBtn').disabled = pos <= 0;
    $('#nextPageBtn').disabled = pos === -1 || pos === list.length - 1;

    const dataUrl = `data:${image.mime};base64,${image.base64}`;
    window.BlocksView.renderPage(result.blocks, dataUrl);

    renderJsonTab();
    renderMarkdownTab();
    renderHtmlTab();
    renderThumbnails();
  }

  async function renderHtmlTab() {
    const result = currentPageResult();
    if (!result) return;
    $('#htmlOutput').textContent = 'Converting…';
    try {
      const html = await invoke('convert_markdown', { markdown: result.markdown, format: 'html' });
      $('#htmlOutput').textContent = html;
    } catch (err) {
      $('#htmlOutput').textContent = '(conversion failed: ' + err + ')';
    }
  }

  // camelot-worker embeds tables as raw HTML directly inline in its Markdown output — valid
  // CommonMark, but literal <table> tags if pasted somewhere that only renders plain
  // Markdown. `convert_markdown` (format: 'markdown') swaps each one for a real GFM pipe
  // table so the tab/download is portable Markdown, not Markdown-with-embedded-HTML.
  async function renderMarkdownTab() {
    const result = currentPageResult();
    if (!result) return;
    $('#markdownOutput').textContent = 'Converting…';
    try {
      const markdown = await invoke('convert_markdown', { markdown: result.markdown, format: 'markdown' });
      $('#markdownOutput').textContent = markdown;
    } catch (err) {
      $('#markdownOutput').textContent = '(conversion failed: ' + err + ')';
    }
  }

  // The JSON tab is the page's structured data: any tables camelot-worker found (parsed into
  // rows/columns, not left as opaque HTML) plus the sparse visual-region blocks.
  async function renderJsonTab() {
    const result = currentPageResult();
    if (!result) return;
    $('#jsonOutput').textContent = 'Converting…';
    try {
      const tablesJson = await invoke('convert_markdown', { markdown: result.markdown, format: 'json' });
      const tables = JSON.parse(tablesJson);
      $('#jsonOutput').textContent = JSON.stringify(
        { page: result.page, tables, blocks: result.blocks },
        null,
        2
      );
    } catch (err) {
      // No tables on this page isn't an error condition for the JSON tab — just show blocks.
      $('#jsonOutput').textContent = JSON.stringify(
        { page: result.page, tables: [], blocks: result.blocks },
        null,
        2
      );
    }
  }

  // Thumbnails live in the nav-rail's thumbnail-mode rail (see tableWorkActive/
  // updateTableWorkLayout) and are always selectable — no separate "combine mode" toggle.
  // Clicking a thumbnail's checkbox toggles it into the pending selection for the *next* queued
  // group; clicking the thumbnail body itself (any other part) previews that page in the
  // Markdown/Blocks/JSON/HTML tabs. Both are always available at once, on every thumbnail.
  // Shared by both the sidebar rail and the full-screen preview modal (see wireThumbnailModal)
  // so selection/preview behavior never drifts between the two: a checkbox toggles that page
  // into the pending combine selection; clicking anywhere else on the thumbnail previews it in
  // the Markdown/Blocks/JSON/HTML tabs.
  function createThumbnailElement(idx) {
    const img = state.pageImages[idx];
    const wrap = document.createElement('div');
    wrap.className = 'thumbnail-wrap';
    wrap.classList.toggle('selected', state.selectedCombinePages.has(idx));

    const badge = document.createElement('span');
    badge.className = 'thumbnail-page-badge';
    badge.textContent = `p.${idx + 1}`;
    wrap.appendChild(badge);

    const thumb = document.createElement('img');
    thumb.src = `data:${img.mime};base64,${img.base64}`;
    thumb.className = 'thumbnail' + (idx === state.currentPage ? ' active' : '');
    wrap.appendChild(thumb);

    const check = document.createElement('span');
    check.className = 'thumbnail-check';
    check.title = 'Select for combining';
    check.addEventListener('click', (e) => {
      e.stopPropagation();
      if (state.selectedCombinePages.has(idx)) state.selectedCombinePages.delete(idx);
      else state.selectedCombinePages.add(idx);
      renderCombineQueueUI();
      renderThumbnails();
    });
    wrap.appendChild(check);

    wrap.addEventListener('click', () => {
      state.currentPage = idx;
      renderCurrentPage();
    });
    return wrap;
  }

  function currentThumbnailIndices() {
    return state.tablesOnlyThumbnails ? resultsPageList() : allPageIndices(state.pageImages.length);
  }

  function renderThumbnails() {
    const strip = $('#navThumbnailScroll');
    if (!strip) return;
    strip.innerHTML = '';
    currentThumbnailIndices().forEach((idx) => strip.appendChild(createThumbnailElement(idx)));
    // Keeps the full-screen preview modal (if open) in sync with every existing call site that
    // already re-renders the sidebar rail (parse, selection toggle, page navigation, filter
    // toggle) rather than needing a separate render call sprinkled into each of them.
    if (state.thumbnailModalOpen) renderThumbnailModal();
  }

  // ---------------- full-screen thumbnail preview modal (4 at a time) ----------------
  const THUMBNAIL_MODAL_PAGE_SIZE = 4;

  function renderThumbnailModal() {
    const grid = $('#thumbnailModalGrid');
    const indices = currentThumbnailIndices();
    const pageCount = Math.max(1, Math.ceil(indices.length / THUMBNAIL_MODAL_PAGE_SIZE));
    if (state.thumbnailModalPageIndex >= pageCount) state.thumbnailModalPageIndex = pageCount - 1;
    if (state.thumbnailModalPageIndex < 0) state.thumbnailModalPageIndex = 0;

    const start = state.thumbnailModalPageIndex * THUMBNAIL_MODAL_PAGE_SIZE;
    const visible = indices.slice(start, start + THUMBNAIL_MODAL_PAGE_SIZE);
    grid.innerHTML = '';
    visible.forEach((idx) => grid.appendChild(createThumbnailElement(idx)));

    $('#thumbnailModalPageLabel').textContent = indices.length
      ? `${state.thumbnailModalPageIndex + 1} / ${pageCount}`
      : 'No pages';
    $('#thumbnailModalPrevBtn').disabled = state.thumbnailModalPageIndex <= 0;
    $('#thumbnailModalNextBtn').disabled = state.thumbnailModalPageIndex >= pageCount - 1;
  }

  function openThumbnailModal() {
    state.thumbnailModalOpen = true;
    state.thumbnailModalPageIndex = 0;
    $('#thumbnailModalBackdrop').hidden = false;
    renderThumbnailModal();
  }

  function closeThumbnailModal() {
    state.thumbnailModalOpen = false;
    $('#thumbnailModalBackdrop').hidden = true;
  }

  function wireThumbnailModal() {
    $('#expandThumbnailsBtn').addEventListener('click', openThumbnailModal);
    $('#thumbnailModalCloseBtn').addEventListener('click', closeThumbnailModal);
    // Clicking the dimmed backdrop itself (not the panel) also closes it -- standard modal
    // convention -- but a click that started inside the panel and only bubbled up from a normal
    // page interaction should not.
    $('#thumbnailModalBackdrop').addEventListener('click', (e) => {
      if (e.target.id === 'thumbnailModalBackdrop') closeThumbnailModal();
    });
    $('#thumbnailModalPrevBtn').addEventListener('click', () => {
      state.thumbnailModalPageIndex = Math.max(0, state.thumbnailModalPageIndex - 1);
      renderThumbnailModal();
    });
    $('#thumbnailModalNextBtn').addEventListener('click', () => {
      state.thumbnailModalPageIndex += 1;
      renderThumbnailModal();
    });
  }

  // Always gives explicit feedback on click (a toast with the actual before/after count) so
  // there's never any ambiguity about whether the filter did anything -- a real clinical
  // protocol can easily have a detected table (even a small key/value form) on nearly every
  // page, in which case filtering to "has a table" legitimately changes little or nothing, and
  // that needs to be visible/explained rather than look like a dead button.
  function wireThumbnailsTablesOnly() {
    $('#thumbnailsTablesOnlyBtn').addEventListener('click', () => {
      state.tablesOnlyThumbnails = !state.tablesOnlyThumbnails;
      $('#thumbnailsTablesOnlyBtn').classList.toggle('active', state.tablesOnlyThumbnails);
      if (state.tablesOnlyThumbnails) {
        const list = resultsPageList();
        const total = state.pageResults.length;
        if (!list.length) {
          showToast('No tables detected on any page.', 'info');
        } else if (list.length === total) {
          showToast(`All ${total} page(s) have a detected table — nothing to filter out.`, 'info');
        } else {
          showToast(`Showing ${list.length} of ${total} page(s) with a detected table.`, 'info');
        }
        if (list.length && !list.includes(state.currentPage)) {
          state.currentPage = list[0];
        }
      }
      // Called directly (not only via renderCurrentPage's early-return guard) so the thumbnail
      // list is guaranteed to refresh even in an edge case where currentPageResult()/
      // currentPageImage() come back empty.
      renderThumbnails();
      renderCurrentPage();
    });
  }

  // ---------------- combine tables into a master table (queue → crop & merge all) ----------------
  function pendingSelectionName() {
    const selected = $('#masterTableNameInput').value;
    return selected === '__custom__' ? $('#masterTableCustomNameInput').value.trim() : selected;
  }

  // Renders the queued-groups list and keeps the "+ Queue Group"/"Crop & Merge All" buttons'
  // enabled state and label in sync with both the queue and whatever's currently selected but
  // not yet queued (so the common single-group case still works in one click on the Crop & Merge
  // All button — see startCropQueue's auto-queue-on-click behavior).
  function renderCombineQueueUI() {
    const count = state.selectedCombinePages.size;
    $('#combineSelectedCount').textContent = `${count} selected`;
    const hasPendingSelection = count > 0 && !!pendingSelectionName();
    $('#queueGroupBtn').disabled = !hasPendingSelection;

    const list = $('#queuedGroupsList');
    list.innerHTML = '';
    state.combineQueue.forEach((group, i) => {
      const row = document.createElement('div');
      row.className = 'queued-group-row';
      const label = document.createElement('span');
      label.textContent = `${group.name} — ${group.pages.length} page${group.pages.length === 1 ? '' : 's'}`;
      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'icon-btn';
      removeBtn.title = 'Remove from queue';
      removeBtn.textContent = '×';
      removeBtn.addEventListener('click', () => {
        state.combineQueue.splice(i, 1);
        renderCombineQueueUI();
      });
      row.appendChild(label);
      row.appendChild(removeBtn);
      list.appendChild(row);
    });
    list.hidden = state.combineQueue.length === 0;

    const effectiveCount = state.combineQueue.length + (hasPendingSelection ? 1 : 0);
    $('#cropMergeAllBtn').disabled = effectiveCount === 0;
    $('#cropMergeAllBtn').textContent = effectiveCount > 0 ? `Crop & Merge All (${effectiveCount})` : 'Crop & Merge All';
  }

  // Validates the current selection + chosen name and pushes them onto combineQueue. Returns
  // true on success (used by startCropQueue to auto-queue a still-pending selection).
  function queueCurrentSelection() {
    const indices = Array.from(state.selectedCombinePages).sort((a, b) => a - b);
    if (!indices.length) {
      showToast('Select at least one page before queueing.', 'error');
      return false;
    }
    const name = pendingSelectionName();
    if (!name) {
      showToast('Choose a table type (or enter a custom name) before queueing.', 'error');
      return false;
    }
    state.combineQueue.push({ id: Date.now(), name, pages: indices });
    state.selectedCombinePages.clear();
    $('#masterTableNameInput').value = '';
    $('#masterTableCustomNameInput').value = '';
    $('#masterTableCustomNameInput').hidden = true;
    renderThumbnails();
    renderCombineQueueUI();
    return true;
  }

  function wireCombineTables() {
    $('#masterTableNameInput').addEventListener('change', (e) => {
      $('#masterTableCustomNameInput').hidden = e.target.value !== '__custom__';
      if (e.target.value === '__custom__') $('#masterTableCustomNameInput').focus();
      renderCombineQueueUI();
    });
    $('#masterTableCustomNameInput').addEventListener('input', renderCombineQueueUI);
    $('#queueGroupBtn').addEventListener('click', queueCurrentSelection);

    $('#cancelCombineBtn').addEventListener('click', () => {
      state.selectedCombinePages.clear();
      state.combineQueue = [];
      $('#masterTableNameInput').value = '';
      $('#masterTableCustomNameInput').value = '';
      $('#masterTableCustomNameInput').hidden = true;
      renderThumbnails();
      renderCombineQueueUI();
    });

    $('#cropMergeAllBtn').addEventListener('click', startCropQueue);
    $('#backToResultsBtn').addEventListener('click', () => showScreen('workspace'));
    $('#exportMasterJsonBtn').addEventListener('click', () => exportMasterTable('json'));
    $('#exportMasterMarkdownBtn').addEventListener('click', () => exportMasterTable('markdown'));
    $('#exportMasterHtmlBtn').addEventListener('click', () => exportMasterTable('html'));

    window.MasterTableView.setSourceDocumentTextProvider(() =>
      state.pageResults.map((r) => r.markdown).join('\n\n')
    );
    // Nothing else calls render() until the user acts (combining tables, etc.) — without this,
    // the master-table list/grid containers are simply empty (correctly so) on first launch, but
    // this keeps the same "always render once at startup" habit the rest of this app follows.
    window.MasterTableView.refresh();
  }

  function showScreen(name) {
    state.screen = name;
    $('#workspaceScreen').hidden = name !== 'workspace';
    $('#masterTableScreen').hidden = name !== 'masterTable';
    $('#tableCropScreen').hidden = name !== 'tableCrop';
    if (name === 'masterTable') renderAttachmentsStatus();
    updateTableWorkLayout();
  }

  // ================= table-work layout: auto-collapse source panel, swap nav-rail for the big
  // thumbnail rail (see §2 of the plan — items #1/#5 from the user's batch) =================
  function tableWorkActive() {
    return (state.screen === 'workspace' && state.pageResults.length > 0) || state.screen === 'tableCrop';
  }

  function updateTableWorkLayout() {
    const active = tableWorkActive();
    // The thumbnail rail only actually takes over the nav-rail slot when table work is active
    // AND the user hasn't switched back to the section nav (see navRailShowSections above).
    const showThumbRail = active && !state.navRailShowSections;
    const collapsed = active && !state.sourcePanelManuallyExpanded;
    const toggleBtn = $('#sourcePanelExpandBtn');
    // Visible the whole time table work is active, in both states -- it's a real toggle (expand
    // when collapsed, collapse again when expanded), not a one-way "show me the panel" button.
    toggleBtn.hidden = !active;
    toggleBtn.classList.toggle('expanded', !collapsed);
    toggleBtn.title = collapsed ? 'Show protocol upload/attachments' : 'Hide protocol upload/attachments';
    $('.source-panel').classList.toggle('collapsed', collapsed);
    const navRail = $('#navRail');
    navRail.classList.toggle('thumbnail-mode', showThumbRail);
    // Applies the user's own drag-resized width (see wireNavRailResize) whenever the thumbnail
    // rail is shown; reverts to the CSS-defined width for the normal 6-section nav otherwise.
    navRail.style.width = showThumbRail ? `${state.thumbnailRailWidth}px` : '';
    if (showThumbRail) renderThumbnails();
    // Banner + link back to thumbnails, shown only when there's actually a parsed document to
    // go back to and the user is currently looking at the section nav instead of it.
    $('#thumbnailsCollapsedBanner').hidden = !(active && state.navRailShowSections);
  }

  function wireSourcePanelCollapse() {
    $('#sourcePanelExpandBtn').addEventListener('click', () => {
      state.sourcePanelManuallyExpanded = !state.sourcePanelManuallyExpanded;
      updateTableWorkLayout();
    });
  }

  // Two-way toggle between the thumbnail rail and the normal section nav while table work is
  // active -- #showSectionsBtn lives in the thumbnail rail's own toolbar; #showThumbnailsBtn is
  // the banner shown in Intake's main content once you've switched away from thumbnails, so
  // there's always a visible way back in either direction (see updateTableWorkLayout).
  function wireSectionsThumbnailsToggle() {
    $('#showSectionsBtn').addEventListener('click', () => {
      state.navRailShowSections = true;
      updateTableWorkLayout();
    });
    $('#showThumbnailsBtn').addEventListener('click', () => {
      state.navRailShowSections = false;
      updateTableWorkLayout();
    });
  }

  // Click-and-drag resize for the thumbnail rail (left column, replacing the 6-section nav
  // during table work) -- lets the user make page previews as big as they need instead of being
  // stuck with a fixed width.
  const THUMBNAIL_RAIL_MIN_WIDTH = 240;
  const THUMBNAIL_RAIL_MAX_WIDTH = 640;

  function wireNavRailResize() {
    const handle = $('#navRailResizeHandle');
    const navRail = $('#navRail');
    let dragging = false;
    let startX = 0;
    let startWidth = 0;

    handle.addEventListener('mousedown', (e) => {
      dragging = true;
      startX = e.clientX;
      startWidth = navRail.getBoundingClientRect().width;
      document.body.classList.add('resizing-col');
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const next = Math.max(
        THUMBNAIL_RAIL_MIN_WIDTH,
        Math.min(THUMBNAIL_RAIL_MAX_WIDTH, startWidth + (e.clientX - startX))
      );
      state.thumbnailRailWidth = next;
      navRail.style.width = `${next}px`;
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove('resizing-col');
    });
  }

  // ================= top-level nav sections (Intake/Information/Countries/Schedule of
  // Activities/Analytes/Specimens) =================
  const SECTION_TITLES = {
    intake: 'Intake',
    information: 'Information',
    countries: 'Countries',
    schedule: 'Schedule of Activities',
    analytes: 'Analytes',
    specimen: 'Specimens',
  };

  function showSection(name) {
    state.section = name;
    $('#sectionTitle').textContent = SECTION_TITLES[name] || name;
    $all('.nav-item').forEach((btn) => btn.classList.toggle('active', btn.dataset.section === name));
    $all('.section-body').forEach((panel) => {
      panel.hidden = panel.getAttribute('data-section-panel') !== name;
    });
    if (name === 'schedule') renderScheduleSection();
    if (name === 'analytes') renderScopedPreview('Lab Appendix', '#analytesTablePreview');
    if (name === 'specimen') renderSpecimenSection();
  }

  function wireSidebar() {
    $all('.nav-item').forEach((btn) => {
      btn.addEventListener('click', () => showSection(btn.dataset.section));
    });
    $('#sidebarToggleBtn').addEventListener('click', () => {
      state.sidebarCollapsed = !state.sidebarCollapsed;
      $('#navRail').classList.toggle('collapsed', state.sidebarCollapsed);
    });
  }

  // ---------------- Information: Sponsor Contacts ----------------
  function contactInitials(name) {
    const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return '?';
    return (parts[0][0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
  }

  function renderSponsorContacts() {
    const list = $('#sponsorContactList');
    list.innerHTML = '';
    const count = state.sponsorContacts.length;
    $('#sponsorContactCount').textContent = `${count} contact${count === 1 ? '' : 's'} assigned`;
    state.sponsorContacts.forEach((c, i) => {
      const row = document.createElement('div');
      row.className = 'contact-row';

      const avatar = document.createElement('span');
      avatar.className = 'contact-avatar';
      avatar.textContent = contactInitials(c.name);

      const details = document.createElement('div');
      details.className = 'contact-details';
      const nameRow = document.createElement('div');
      nameRow.className = 'contact-name-row';
      const nameSpan = document.createElement('span');
      nameSpan.className = 'contact-name';
      nameSpan.textContent = c.name;
      const rolePill = document.createElement('span');
      rolePill.className = 'contact-role-pill';
      rolePill.textContent = c.role || (i === 0 ? 'Primary Owner' : 'Contact');
      nameRow.appendChild(nameSpan);
      nameRow.appendChild(rolePill);
      const meta = document.createElement('div');
      meta.className = 'contact-meta';
      meta.textContent = [c.email, c.tag].filter(Boolean).join(' · ');
      details.appendChild(nameRow);
      details.appendChild(meta);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'icon-btn';
      removeBtn.title = 'Remove contact';
      removeBtn.textContent = '×';
      removeBtn.addEventListener('click', () => {
        state.sponsorContacts.splice(i, 1);
        renderSponsorContacts();
      });

      row.appendChild(avatar);
      row.appendChild(details);
      row.appendChild(removeBtn);
      list.appendChild(row);
    });
  }

  function addSponsorContact() {
    const name = window.prompt('Contact name?');
    if (!name || !name.trim()) return;
    const role = window.prompt('Role? (e.g. Study manager)', 'Contact') || '';
    const email = window.prompt('Email?', '') || '';
    state.sponsorContacts.push({ id: Date.now(), name: name.trim(), role: role.trim(), email: email.trim(), tag: '' });
    renderSponsorContacts();
  }

  // ---------------- Information: Protocol Details phase segmented control ----------------
  function wirePhaseSegmented() {
    $all('#phaseSegmented .segmented-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.phase = state.phase === btn.dataset.phase ? '' : btn.dataset.phase;
        $all('#phaseSegmented .segmented-btn').forEach((b) => {
          b.classList.toggle('active', b.dataset.phase === state.phase);
          // A real click always supersedes the auto-preview highlight; restored below if the
          // user toggled back to blank ("auto") and an extracted value still exists.
          b.classList.remove('auto');
        });
        if (!state.phase) applyExtractedFieldPreviews();
      });
    });
  }

  // Clears a field's auto-filled styling the moment the user actually edits it -- from then on
  // it's just a normal manually-entered value, indistinguishable from typing it fresh.
  function wireAutoFillClear(selector) {
    $(selector).addEventListener('input', () => $(selector).classList.remove('auto-filled'));
  }

  function wireInformationSection() {
    $('#addSponsorContactBtn').addEventListener('click', addSponsorContact);
    wirePhaseSegmented();
    ['#protocolNumberInput', '#therapeuticAreaInput', '#dateSubmittedInput', '#dateBudgetInput'].forEach(wireAutoFillClear);
    renderSponsorContacts();
  }

  // ---------------- Countries: chip list ----------------
  // Shows the user's own manually-added chips once there are any; otherwise previews whatever
  // extract_rfp_schema found (read-only, no remove button -- purely informational) so the section
  // isn't blank before the user has typed anything. Adding one manual chip switches the whole list
  // to manual-only, matching the card's own hint text ("overrides whatever's auto-extracted").
  function renderCountryChips() {
    const list = $('#countryChipList');
    list.innerHTML = '';
    if (state.manualCountries.length) {
      state.manualCountries.forEach((name) => {
        const chip = document.createElement('span');
        chip.className = 'data-chip';
        const label = document.createElement('span');
        label.textContent = name;
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.textContent = '×';
        removeBtn.addEventListener('click', () => {
          state.manualCountries = state.manualCountries.filter((c) => c !== name);
          renderCountryChips();
        });
        chip.appendChild(label);
        chip.appendChild(removeBtn);
        list.appendChild(chip);
      });
      return;
    }
    const autoCountries = (state.extractedFields && state.extractedFields['Country Allocation table']) || [];
    autoCountries.forEach((c) => {
      if (!c || !c.name) return;
      const chip = document.createElement('span');
      chip.className = 'data-chip auto';
      chip.textContent = c.name;
      list.appendChild(chip);
    });
  }

  function addCountryChip() {
    const input = $('#countryChipInput');
    const value = input.value.trim();
    if (!value || state.manualCountries.includes(value)) return;
    state.manualCountries.push(value);
    input.value = '';
    renderCountryChips();
  }

  function wireCountriesSection() {
    $('#addCountryBtn').addEventListener('click', addCountryChip);
    $('#countryChipInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        addCountryChip();
      }
    });
  }

  // ---------------- Live preview of extract_rfp_schema's findings (Information/Countries) ----------------
  // Flattens extract_rfp_schema's structured {protocol_fields,design_fields,rfp_engine_fields}
  // response into a plain {fieldName: value} map -- the same flattening rfp_cli_bridge.py performs
  // server-side (see backend/engine/rfp_cli_bridge.py), done here too so the UI can preview the
  // same values Generate RFP would use before the user ever clicks it.
  function flattenSchemaFields(schema) {
    const flat = {};
    ['protocol_fields', 'design_fields', 'rfp_engine_fields'].forEach((key) => {
      (schema[key] || []).forEach((spec) => {
        flat[spec.field] = spec.value;
      });
    });
    return flat;
  }

  // Pre-fills a text input with an extracted value, but only while it's still either empty or
  // itself still showing a previous auto-fill (never overwrites something the user actually
  // typed). Marked `.auto-filled` (muted styling; also suppresses the required-field pink
  // highlight via a CSS override -- there's a real value now, even if unconfirmed) until the
  // user's own `input` event clears it (see wireAutoFillClear).
  function applyAutoFillInput(selector, value) {
    const input = $(selector);
    if (!input || !value) return;
    if (input.value && !input.classList.contains('auto-filled')) return;
    input.value = value;
    input.classList.add('auto-filled');
  }

  function applyExtractedFieldPreviews() {
    const fields = state.extractedFields;
    if (!fields) return;
    applyAutoFillInput('#protocolNumberInput', fields['Protocol alias']);
    applyAutoFillInput('#therapeuticAreaInput', fields['Therapeutic Area']);
    applyAutoFillInput('#dateSubmittedInput', fields['Date RFP submitted']);
    applyAutoFillInput('#dateBudgetInput', fields['Date budget required']);

    if (!state.phase) {
      const extractedPhase = fields['Phase'];
      $all('#phaseSegmented .segmented-btn').forEach((b) => {
        b.classList.toggle('auto', !!extractedPhase && b.dataset.phase === extractedPhase);
      });
    }

    renderCountryChips();
  }

  // Fire-and-forget: called after parsing and after attaching Previous RFP, since that changes
  // what extract_rfp_schema would return. No error toast -- this is passive background
  // enrichment for the Information/Countries live previews, not a user-initiated action; a
  // failure just leaves those fields showing their placeholder hint text, same as before this
  // feature existed. Design/ops fields come from the Fabric extract lookup (see
  // maybeAutoSearchFabricDesignFields below), not a design_text extraction pass -- design_text
  // is always empty now that Design Elements attachment has been removed.
  async function refreshExtractedFieldsPreview() {
    if (!state.pageResults.length) return;
    try {
      const protocolText = state.pageResults.map((r) => r.markdown).join('\n\n');
      const designText = '';
      const previousRfpPath = state.previousRfpDoc ? state.previousRfpDoc.path : null;
      const schemaRaw = await invoke('extract_rfp_schema', { protocolText, designText, previousRfpPath });
      state.extractedFields = flattenSchemaFields(JSON.parse(schemaRaw));
      // A Fabric result always wins over this fresh auto-extraction for whichever fields it
      // covers -- otherwise the very next parse would silently discard it, since this assignment
      // above replaces state.extractedFields wholesale.
      if (state.fabricDesignFields) Object.assign(state.extractedFields, state.fabricDesignFields);
      applyExtractedFieldPreviews();
      // Fire-and-forget: kicks off (or re-applies) the Fabric lookup now that a "Protocol alias"
      // value may be freshly available -- not awaited so this preview refresh isn't held up
      // waiting on a network round trip.
      maybeAutoSearchFabricDesignFields();
    } catch (err) {
      // Swallowed deliberately -- see comment above.
    }
  }

  // Fetches design/ops fields (Therapeutic Area, Phase, Pediatric flag, Country Allocation,
  // enrollment/screen counts, trial milestones) from the local Fabric extract, keyed by the
  // protocol alias extract_rfp_schema already found in the uploaded protocol -- there's no UI for
  // this at all (no input, no button): it's triggered automatically from
  // refreshExtractedFieldsPreview once a "Protocol alias" value is known, and only re-runs if that
  // alias actually changes (fabricAutoSearchedAlias guards against re-searching on every
  // parse/attach re-run for the same protocol). A "not found" or error result is left quiet, same
  // as extract_rfp_schema's own best-effort background enrichment -- the alias may just not be in
  // Fabric yet, or the study may be too new for today's extract.
  async function maybeAutoSearchFabricDesignFields() {
    const alias = state.extractedFields && state.extractedFields['Protocol alias'];
    if (!alias || alias === state.fabricAutoSearchedAlias) return;
    state.fabricAutoSearchedAlias = alias;
    try {
      const raw = await invoke('fetch_fabric_design_fields', { protocolAlias: alias });
      const result = JSON.parse(raw);
      if (result.status !== 'ok') return; // not_found/error -- silent, best-effort background fill
      state.fabricDesignFields = result.fields;
      state.fabricProtocolAlias = alias;
      if (!state.extractedFields) state.extractedFields = {};
      Object.assign(state.extractedFields, state.fabricDesignFields);
      applyExtractedFieldPreviews();
      renderAttachmentsStatus();
    } catch (err) {
      // Swallowed deliberately -- see comment above.
    }
  }

  // ---------------- Schedule of Activities / Analytes: scoped read-only previews ----------------
  // Reuses master-table.js's getTablesByName + the same "grow width, never truncate, vertical
  // concatenation" merge rule generateRfpFromMasterTables() already applies -- these previews
  // just need to render, not re-derive, the merged shape.
  function renderScopedPreview(name, containerSelector) {
    const container = $(containerSelector);
    const merged = mergeNamedTables(window.MasterTableView.getTablesByName(name));
    if (!merged) {
      container.innerHTML =
        `<p class="empty-hint">No "${name}" table yet — build one from Intake's Combine Tables step.</p>` +
        `<div class="scoped-preview-actions"><button class="btn" data-goto-intake>Go to Intake</button></div>`;
      container.querySelector('[data-goto-intake]').addEventListener('click', () => showSection('intake'));
      return;
    }
    const table = document.createElement('table');
    table.className = 'scoped-table-preview';
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    merged.headers.forEach((h) => {
      const th = document.createElement('th');
      th.textContent = h;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    merged.rows.forEach((row) => {
      const tr = document.createElement('tr');
      row.forEach((cell) => {
        const td = document.createElement('td');
        td.textContent = cell;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    const actions = document.createElement('div');
    actions.className = 'scoped-preview-actions';
    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'btn';
    editBtn.textContent = 'Edit in Intake';
    editBtn.addEventListener('click', () => {
      const matches = window.MasterTableView.getTablesByName(name);
      if (matches.length) {
        window.MasterTableView.setActiveById(matches[0].id);
        showSection('intake');
        showScreen('masterTable');
      }
    });
    actions.appendChild(editBtn);

    container.innerHTML = '';
    container.appendChild(table);
    container.appendChild(actions);
  }

  function renderScheduleSection() {
    renderScopedPreview('SoA', '#scheduleTablePreview');
    const btn = $('#regenerateMasterSoaBtn');
    btn.disabled = window.MasterTableView.isMasterSoaBusy() || !window.MasterTableView.hasSourceDocument();
    btn.textContent = window.MasterTableView.isMasterSoaBusy() ? 'Generating…' : 'Regenerate consolidated schedule';
  }

  function wireScheduleSection() {
    $('#regenerateMasterSoaBtn').addEventListener('click', () => window.MasterTableView.regenerateMasterSoa());
    window.MasterTableView.setOnMasterSoaStateChange(() => {
      if (state.section === 'schedule') renderScheduleSection();
    });
  }

  // ---------------- Specimens ----------------
  function wireSpecimenSegmented() {
    $all('#storageConditionsSegmented .segmented-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.storageConditions = state.storageConditions === btn.dataset.storage ? '' : btn.dataset.storage;
        $all('#storageConditionsSegmented .segmented-btn').forEach((b) =>
          b.classList.toggle('active', b.dataset.storage === state.storageConditions)
        );
      });
    });
    $all('#kitTypeSegmented .segmented-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.kitType = state.kitType === btn.dataset.kit ? '' : btn.dataset.kit;
        $all('#kitTypeSegmented .segmented-btn').forEach((b) => b.classList.toggle('active', b.dataset.kit === state.kitType));
      });
    });
  }

  function renderSpecimenSection() {
    $('#specimenPreviousRfpStatus').textContent = state.previousRfpDoc
      ? `Previous RFP: ${state.previousRfpDoc.name}`
      : 'Previous RFP: not attached';
    const container = $('#specimenTablePreview');
    const ltsMerged = mergeNamedTables(window.MasterTableView.getTablesByName('LTS'));
    const referralMerged = mergeNamedTables(window.MasterTableView.getTablesByName('Referral Lab'));
    if (!ltsMerged && !referralMerged) {
      container.innerHTML = '<p class="empty-hint">No LTS/Referral Lab master table yet — build one from Intake\'s Combine Tables step, or attach a Previous RFP above.</p>';
      return;
    }
    container.innerHTML = '';
    [['LTS', ltsMerged], ['Referral Lab', referralMerged]].forEach(([label, merged]) => {
      if (!merged) return;
      const heading = document.createElement('p');
      heading.className = 'empty-hint';
      heading.textContent = label;
      container.appendChild(heading);
      const table = document.createElement('table');
      table.className = 'scoped-table-preview';
      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      merged.headers.forEach((h) => {
        const th = document.createElement('th');
        th.textContent = h;
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      table.appendChild(thead);
      const tbody = document.createElement('tbody');
      merged.rows.forEach((row) => {
        const tr = document.createElement('tr');
        row.forEach((cell) => {
          const td = document.createElement('td');
          td.textContent = cell;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      container.appendChild(table);
    });
  }

  function wireSpecimenSection() {
    wireSpecimenSegmented();
  }

  // Previous RFP is attached from the source panel, on a different screen than where Generate
  // RFP lives -- this one-line status keeps what's attached visible without needing to go back
  // and check.
  function renderAttachmentsStatus() {
    const parts = [];
    parts.push(state.previousRfpDoc ? `Previous RFP: ${state.previousRfpDoc.name}` : 'Previous RFP: not attached');
    parts.push(state.fabricProtocolAlias ? `Fabric: ${state.fabricProtocolAlias}` : 'Fabric: not searched');
    if (state.clipsNonPkpdFiles.length) {
      const unassigned = state.clipsNonPkpdFiles.filter((f) => !f.column).length;
      parts.push(
        `CLIPS/Non-PKPD: ${state.clipsNonPkpdFiles.length} file(s)` +
          (unassigned ? ` (${unassigned} needs review)` : '')
      );
    }
    $('#rfpAttachmentsStatus').textContent = parts.join('  ·  ');
    updateSpecimenSourceUI();
  }

  // Referral Lab & Storage Samples has exactly one source at a time (see attachPreviousRfp/
  // attachClipsNonPkpdFiles's own guards) -- this keeps both attachment controls' disabled state
  // and the explanatory hint in sync with whichever source (if either) currently has data.
  function updateSpecimenSourceUI() {
    const hasPrevious = !!state.previousRfpDoc;
    const hasClips = state.clipsNonPkpdFiles.length > 0;
    $('#attachPreviousRfpBtn').disabled = hasClips;
    $('#clearPreviousRfpBtn').hidden = !hasPrevious;
    $('#attachClipsNonPkpdBtn').disabled = hasPrevious;
    const hint = $('#specimenSourceHint');
    if (hasPrevious) {
      hint.hidden = false;
      hint.textContent = 'Using Previous RFP for Referral Lab & Storage Samples — remove it to use CLIPS/Non-PKPD worksheets instead.';
    } else if (hasClips) {
      hint.hidden = false;
      hint.textContent = 'Using CLIPS/Non-PKPD worksheets for Referral Lab & Storage Samples — remove them to use a Previous RFP instead.';
    } else {
      hint.hidden = true;
    }
  }

  // Rasterizes one queued group's pages and resets the crop-screen state to show them — shared
  // by both starting the crop queue (first group) and confirmCropMerge advancing to the next one.
  // Throws (without touching cropQueue/cropQueueIndex) if rasterize_document fails, so callers can
  // decide how to handle a mid-queue failure without corrupting queue bookkeeping.
  async function startCropForGroup(group) {
    const images = await invoke('rasterize_document', {
      path: state.pickedFile.path,
      dpi: RASTERIZE_DPI,
      pages: group.pages,
    });
    state.cropPageImages = {};
    images.forEach((img) => {
      state.cropPageImages[img.page] = img;
    });
    state.cropPages = group.pages;
    state.cropIndex = 0;
    // Fresh every group (this flow is repeatable — SoA pages, then Lab Appendix pages, then a
    // second SoA pass, all in one queue or across sessions) so a page index reused across groups
    // never carries a stale region drawn during an earlier, unrelated group.
    state.cropRegionsByPage = {};
    state.cropTableName = group.name;
    $('#cropTableName').textContent = group.name;
    renderCropStep();
  }

  // Starts the Crop & Merge flow for every queued group in sequence. A still-pending selection
  // (page(s) picked + a name chosen, but not yet queued) is auto-queued first so a single click
  // on this button is enough for the common single-group case.
  async function startCropQueue() {
    if (state.selectedCombinePages.size > 0) {
      if (!queueCurrentSelection()) return; // validation already toasted an error
    }
    if (!state.combineQueue.length) {
      showToast('Select pages and queue at least one group first.', 'error');
      return;
    }
    const btn = $('#cropMergeAllBtn');
    btn.disabled = true;
    btn.textContent = 'Preparing…';
    try {
      const queue = state.combineQueue.slice();
      // Set before starting the first group (not after) so renderCropStep's "Group N of M" label
      // is correct from the very first page shown, not just once a *second* group is reached.
      // combineQueue itself is only cleared once startCropForGroup below actually succeeds, so a
      // rasterize failure here leaves the still-queued groups intact to retry.
      state.cropQueue = queue;
      state.cropQueueIndex = 0;
      await startCropForGroup(queue[0]);
      state.combineQueue = [];
      renderCombineQueueUI();
      showScreen('tableCrop');
    } catch (err) {
      state.cropQueue = [];
      state.cropQueueIndex = 0;
      showToast('Could not prepare pages for cropping: ' + err, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Crop & Merge All';
    }
  }

  // ---------------- Combine Tables' crop step ----------------
  // Reuses the same math as the Configuration tab's region-drawing step (toImagePixel,
  // regionToTableArea, the SVG-overlay convention) but as separate functions bound to this
  // screen's own elements, scoped to state.cropPages instead of the whole document — deliberately
  // NOT refactored to share code with wireRegionStep/renderRegionStep/renderRegionOverlay, so the
  // already-working Configuration flow can't regress from this addition. Unlike that flow, a
  // page here can carry MULTIPLE drawn regions (state.cropRegionsByPage[page] is an array) —
  // e.g. a header row and a data row further down, cropped as two separate drags.
  function currentCropPage() {
    return state.cropPages[state.cropIndex];
  }

  function renderCropOverlay() {
    const svg = $('#cropOverlay');
    svg.innerHTML = '';
    svg.setAttribute('viewBox', '0 0 1000 1000');
    svg.setAttribute('preserveAspectRatio', 'none');

    const page = currentCropPage();
    const regions = state.cropRegionsByPage[page];
    const img = state.cropPageImages[page];
    if (!regions || !img) return;

    regions.forEach((region) => {
      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      const x0 = (Math.min(region.x0, region.x1) / img.width) * 1000;
      const y0 = (Math.min(region.y0, region.y1) / img.height) * 1000;
      const w = (Math.abs(region.x1 - region.x0) / img.width) * 1000;
      const h = (Math.abs(region.y1 - region.y0) / img.height) * 1000;
      rect.setAttribute('x', x0);
      rect.setAttribute('y', y0);
      rect.setAttribute('width', w);
      rect.setAttribute('height', h);
      rect.classList.add('region-rect');
      svg.appendChild(rect);
    });
  }

  function updateCropClearLabel() {
    const count = (state.cropRegionsByPage[currentCropPage()] || []).length;
    $('#cropClearBtn').textContent = count > 1 ? `Clear ${count} regions (autodetect)` : 'Clear region (autodetect)';
  }

  function renderCropStep() {
    const page = currentCropPage();
    const img = state.cropPageImages[page];
    if (!img) return;
    $('#cropPreviewImage').src = `data:${img.mime};base64,${img.base64}`;
    const pageLabel = `Page ${page + 1} (${state.cropIndex + 1} / ${state.cropPages.length})`;
    $('#cropPageLabel').textContent =
      state.cropQueue.length > 1
        ? `Group ${state.cropQueueIndex + 1} of ${state.cropQueue.length}: ${state.cropTableName} — ${pageLabel}`
        : pageLabel;
    $('#cropPrevBtn').disabled = state.cropIndex <= 0;
    $('#cropNextBtn').disabled = state.cropIndex >= state.cropPages.length - 1;
    updateCropClearLabel();
    renderCropOverlay();
  }

  function toCropImagePixel(e) {
    const img = $('#cropPreviewImage');
    const rect = img.getBoundingClientRect();
    const pageImg = state.cropPageImages[currentCropPage()];
    const relX = (e.clientX - rect.left) / rect.width;
    const relY = (e.clientY - rect.top) / rect.height;
    return {
      x: Math.max(0, Math.min(pageImg.width, relX * pageImg.width)),
      y: Math.max(0, Math.min(pageImg.height, relY * pageImg.height)),
    };
  }

  function wireCropScreen() {
    const wrap = $('#cropPreviewWrap');
    let dragging = false;
    let anchor = null;

    wrap.addEventListener('mousedown', (e) => {
      if (!state.cropPageImages[currentCropPage()]) return;
      dragging = true;
      anchor = toCropImagePixel(e);
      const page = currentCropPage();
      if (!state.cropRegionsByPage[page]) state.cropRegionsByPage[page] = [];
      // Each new drag ADDS a region rather than replacing the page's existing one(s) — this is
      // what makes multiple crops per page possible (e.g. draw the header row, then draw a data
      // row further down as a second, separate box).
      state.cropRegionsByPage[page].push({ x0: anchor.x, y0: anchor.y, x1: anchor.x, y1: anchor.y });
      renderCropOverlay();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const p = toCropImagePixel(e);
      const regions = state.cropRegionsByPage[currentCropPage()];
      regions[regions.length - 1] = { x0: anchor.x, y0: anchor.y, x1: p.x, y1: p.y };
      renderCropOverlay();
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      // A stray click without a real drag draws a degenerate (near-zero-size) box — drop just
      // that one (not the page's other, already-confirmed regions).
      const regions = state.cropRegionsByPage[currentCropPage()];
      const region = regions[regions.length - 1];
      if (region && Math.abs(region.x1 - region.x0) < 4 && Math.abs(region.y1 - region.y0) < 4) {
        regions.pop();
        renderCropOverlay();
      }
      updateCropClearLabel();
    });

    $('#cropPrevBtn').addEventListener('click', () => {
      state.cropIndex = Math.max(0, state.cropIndex - 1);
      renderCropStep();
    });
    $('#cropNextBtn').addEventListener('click', () => {
      state.cropIndex = Math.min(state.cropPages.length - 1, state.cropIndex + 1);
      renderCropStep();
    });
    $('#cropClearBtn').addEventListener('click', () => {
      delete state.cropRegionsByPage[currentCropPage()];
      renderCropStep();
    });

    $('#backToWorkspaceFromCropBtn').addEventListener('click', returnToWorkspaceFromCrop);
    $('#confirmCropMergeBtn').addEventListener('click', confirmCropMerge);
  }

  // Abandons only the not-yet-merged remainder of the crop queue (the group in progress plus any
  // still-queued after it) -- groups already merged earlier in this same pass are already real
  // master tables via their own createFromPageTables call, so they're kept. Shared by the crop
  // screen's own "← Back" button and the Home button (see wireHomeButton).
  function returnToWorkspaceFromCrop() {
    const totalGroups = state.cropQueue.length;
    const remaining = totalGroups - state.cropQueueIndex;
    state.cropQueue = [];
    state.cropQueueIndex = 0;
    if (totalGroups > 1 && remaining > 0) {
      showToast(`${remaining} queued group(s) (including the one in progress) were not cropped — select and queue them again if needed.`, 'info');
    }
    showScreen('workspace');
  }

  // Always lands on Intake's main workspace screen, regardless of which screen/section the user
  // is currently on -- pure navigation, no state (parsed document, master tables, form entries,
  // combine queue) is ever cleared. If a crop sequence is mid-flight, this gracefully abandons
  // just the not-yet-merged remainder (see returnToWorkspaceFromCrop) instead of silently
  // discarding it.
  function wireHomeButton() {
    $('#homeBtn').addEventListener('click', () => {
      // Forced true (not just left as whatever it was) so Home reliably brings back the section
      // nav from any state -- previously this only reset `screen`/`section`, which did nothing
      // to un-hide the section nav whenever a parsed document was still making table work
      // "active" (see tableWorkActive/updateTableWorkLayout); Home looked like it did nothing.
      state.navRailShowSections = true;
      if (state.screen === 'tableCrop') {
        returnToWorkspaceFromCrop();
      } else {
        showScreen('workspace');
      }
      showSection('intake');
    });
  }

  // Parses the simple, uniform `<table><tr><td>...</td></tr>...</table>` HTML the worker itself
  // always generates (see worker/parse_tables.py's rows_to_html_table) into a plain rows array.
  // Using DOMParser rather than a regex — safe here since this HTML only ever comes back from
  // our own worker/Tauri round-trip, never from unreviewed external content.
  function parseSimpleHtmlTable(html) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    return Array.from(doc.querySelectorAll('tr')).map((tr) =>
      Array.from(tr.querySelectorAll('td')).map((td) => td.textContent)
    );
  }

  // Extracts one page's combined table straight from extract_tables' own `blocks` (bbox + html
  // per detected table region) — no extra convert_markdown round trip needed. A page cropped
  // with multiple regions comes back as multiple blocks; they're sorted top-to-bottom by bbox
  // (bbox[1] = normalized top edge, smaller = higher on the page) before concatenating rows, so
  // e.g. a header-row crop always ends up first regardless of the order it was drawn in or the
  // order Camelot happened to return it in, and result.blocks[i] is then reduced to plain rows.
  function pageTableFromBlocks(result) {
    if (!result || !result.blocks.length) return null;
    const sorted = [...result.blocks].sort((a, b) => a.bbox[1] - b.bbox[1]);
    const rows = [];
    sorted.forEach((block) => rows.push(...parseSimpleHtmlTable(block.html)));
    return rows.length ? { rows } : null;
  }

  async function confirmCropMerge() {
    const btn = $('#confirmCropMergeBtn');
    btn.disabled = true;
    btn.textContent = 'Merging…';
    try {
      const tableAreasByPage = {};
      state.cropPages.forEach((page) => {
        const regions = state.cropRegionsByPage[page];
        const img = state.cropPageImages[page];
        if (regions && regions.length && img) {
          tableAreasByPage[page] = regions.map((r) => regionToTableArea(r, img));
        }
      });

      // Scoped to just the pages being merged -- without this, Camelot re-scans the ENTIRE
      // source document on every single merge click regardless of how few pages actually
      // matter here (confirmed to cost the same as a full parse on a real ~100-page protocol
      // even when merging just 2 pages -- see worker/parse_tables.py's own --pages docs).
      const results = await invoke('extract_tables', {
        path: state.pickedFile.path,
        flavor: FLAVOR,
        tableAreasByPage,
        flavorByPage: {},
        pages: state.cropPages,
      });

      // Fallback: a manually-cropped page that still came back with zero tables (an unusually
      // imprecise drag even CROP_PAD_PX/table_regions couldn't recover) is retried once with
      // that page's override removed entirely, letting Camelot autodetect it instead of leaving
      // it empty outright.
      const zeroTablePages = state.cropPages.filter((page) => {
        if (!tableAreasByPage[page]) return false;
        const result = results.find((r) => r.page === page);
        return !result || result.blocks.length === 0;
      });
      if (zeroTablePages.length) {
        const retryAreas = {};
        Object.keys(tableAreasByPage).forEach((page) => {
          if (!zeroTablePages.includes(Number(page))) retryAreas[page] = tableAreasByPage[page];
        });
        const retryResults = await invoke('extract_tables', {
          path: state.pickedFile.path,
          flavor: FLAVOR,
          tableAreasByPage: retryAreas,
          flavorByPage: {},
          pages: zeroTablePages,
        });
        zeroTablePages.forEach((page) => {
          const idx = results.findIndex((r) => r.page === page);
          const retried = retryResults.find((r) => r.page === page);
          if (idx !== -1 && retried) results[idx] = retried;
        });
      }

      const pageTables = state.cropPages.map((page) => ({
        page,
        table: pageTableFromBlocks(results.find((r) => r.page === page)),
      }));

      const mergedName = state.cropTableName;
      const { skippedPages } = window.MasterTableView.createFromPageTables(mergedName, pageTables);
      if (skippedPages.length) {
        showToast(`No table found on page(s) ${skippedPages.map((p) => p + 1).join(', ')} — skipped.`, 'info');
      }

      // Advance to the next queued group and stay on the crop screen, unless this was the last
      // one (or the next group fails to prepare) -- either way falls through to the shared
      // end-of-sequence cleanup below.
      if (state.cropQueueIndex + 1 < state.cropQueue.length) {
        const prevIndex = state.cropQueueIndex;
        const nextIndex = prevIndex + 1;
        const nextGroup = state.cropQueue[nextIndex];
        try {
          // Set before starting the group (not after) so renderCropStep's "Group N of M" label
          // is correct immediately, from this group's very first page -- reverted below if
          // starting it fails, so the label doesn't get ahead of what's actually on screen.
          state.cropQueueIndex = nextIndex;
          await startCropForGroup(nextGroup);
          const remaining = state.cropQueue.length - nextIndex - 1;
          showToast(`Merged "${mergedName}" — next: "${nextGroup.name}" (${remaining} more queued)`, 'success');
          return;
        } catch (err) {
          state.cropQueueIndex = prevIndex;
          showToast(
            `Merged "${mergedName}", but could not start the next queued group ("${nextGroup.name}"): ${err}. ` +
              'Remaining queued group(s) were dropped — select and queue them again.',
            'error'
          );
          // fall through to the shared cleanup below rather than leaving the crop screen stuck
          // on the just-merged group with a queue that no longer matches its own index.
        }
      }

      state.cropQueue = [];
      state.cropQueueIndex = 0;
      state.selectedCombinePages.clear();
      renderThumbnails();
      renderCombineQueueUI();
      showScreen('masterTable');
    } catch (err) {
      showToast('Failed to merge cropped tables: ' + err, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Merge into table';
    }
  }

  async function exportMasterTable(format) {
    const content = window.MasterTableView.exportActive(format);
    if (!content) {
      showToast('No master table selected.', 'error');
      return;
    }
    const ext = format === 'json' ? 'json' : format === 'html' ? 'html' : 'md';
    const name = window.MasterTableView.getActiveName().replace(/[\\/:*?"<>|]/g, '_');
    try {
      await invoke('export_output', { suggestedName: `${name}.${ext}`, content });
      showToast('Exported.', 'success');
    } catch (err) {
      if (!isCancelled(err)) showToast('Export failed: ' + err, 'error');
    }
  }

  function wirePageNav() {
    $('#prevPageBtn').addEventListener('click', () => {
      state.currentPage = stepInList(resultsPageList(), state.currentPage, -1);
      renderCurrentPage();
    });
    $('#nextPageBtn').addEventListener('click', () => {
      state.currentPage = stepInList(resultsPageList(), state.currentPage, 1);
      renderCurrentPage();
    });
  }

  // ---------------- Blocks tab extras ----------------
  function wireResultTabExtras() {
    $('#renderHtmlToggle').addEventListener('change', () => window.BlocksView.refreshRenderMode());
    $('#bboxToggleChip').addEventListener('click', () => {
      const overlay = $('#bboxOverlay');
      const nowHidden = overlay.style.display !== 'none';
      overlay.style.display = nowHidden ? 'none' : '';
      $('#bboxToggleChip').classList.toggle('active', !nowHidden);
    });
  }

  // ---------------- selection + export ----------------
  function wireSelection() {
    window.BlocksView.setOnSelectionChange((ids) => {
      $('#selectionActionBar').hidden = ids.length === 0;
      $('#selectionCount').textContent = `${ids.length} selected`;
      if (ids.length === 0) $('#blocksView').classList.remove('dim-unselected');
    });
    $('#clearSelectionBtn').addEventListener('click', () => window.BlocksView.clearSelection());
    $('#highlightSelectionBtn').addEventListener('click', () => {
      const active = $('#blocksView').classList.toggle('dim-unselected');
      $('#highlightSelectionBtn').classList.toggle('active', active);
    });
    $all('[data-export-format]').forEach((btn) =>
      btn.addEventListener('click', () => exportSelection(btn.dataset.exportFormat))
    );
  }

  async function exportSelection(format) {
    const result = currentPageResult();
    const selected = window.BlocksView.getSelectedBlocks();
    const blocks = selected.length ? selected : result ? result.blocks : [];
    if (!blocks.length) return;

    // Blocks are always image tags now (sparse visual-region markers) — no HTML<->Markdown
    // conversion needed either way, unlike the earlier per-block-type layout content.
    const combinedHtml = blocks.map((b) => b.html).join('\n');
    const content = format === 'json' ? JSON.stringify(blocks, null, 2) : combinedHtml;
    const ext = format === 'json' ? 'json' : format === 'html' ? 'html' : 'md';
    try {
      await invoke('export_output', { suggestedName: `ovisocr2-export.${ext}`, content });
      showToast('Exported.', 'success');
    } catch (err) {
      if (!isCancelled(err)) showToast('Export failed: ' + err, 'error');
    }
  }

  // ---------------- full-page copy/download toolbars ----------------
  function wireOutputToolbars() {
    const targets = { json: '#jsonOutput', html: '#htmlOutput', markdown: '#markdownOutput' };
    const exts = { json: 'json', html: 'html', markdown: 'md' };

    $all('[data-copy]').forEach((btn) =>
      btn.addEventListener('click', () => {
        navigator.clipboard.writeText($(targets[btn.dataset.copy]).textContent);
        showToast('Copied to clipboard.', 'success');
      })
    );
    $all('[data-download]').forEach((btn) =>
      btn.addEventListener('click', async () => {
        const key = btn.dataset.download;
        try {
          await invoke('export_output', {
            suggestedName: `ovisocr2-page.${exts[key]}`,
            content: $(targets[key]).textContent,
          });
        } catch (err) {
          if (!isCancelled(err)) showToast('Download failed: ' + err, 'error');
        }
      })
    );
  }

  // ---------------- Generate RFP (master-table screen) ----------------
  // Calls into the separate RFP-population engine (extract_rfp_schema, populate_rfp_docx),
  // reading its inputs from the main workspace's currently-open document and whatever master
  // tables already exist.
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Same "grow width to the widest row, never truncate, vertical concatenation" rule used
  // throughout this app (master-table.js's createFromPageTables) — here merging every master
  // table sharing a name (Crop & Merge is repeatable, so there may be more than one
  // "SoA"/"Lab Appendix" table to combine).
  function mergeNamedTables(tables) {
    if (!tables.length) return null;
    const firstHeader = tables[0].headers;
    const maxWidth = Math.max(firstHeader.length, ...tables.map((t) => Math.max(0, ...t.rows.map((r) => r.length))));
    const headers = [];
    for (let i = 0; i < maxWidth; i++) headers.push(firstHeader[i] || `Column ${i + 1}`);
    const rows = [];
    tables.forEach((t) => {
      t.rows.forEach((row) => {
        const padded = [];
        for (let i = 0; i < headers.length; i++) padded.push(row[i] ?? '');
        rows.push(padded);
      });
    });
    return { headers, rows };
  }

  // Referral Lab & Storage Samples has exactly one source at a time -- Previous RFP or
  // CLIPS/Non-PKPD worksheets, never both (updateSpecimenSourceUI already disables each button
  // while the other has data; this is the defensive backstop in case either gets called anyway).
  async function attachPreviousRfp() {
    if (state.clipsNonPkpdFiles.length) {
      showToast('Remove the attached CLIPS/Non-PKPD worksheet(s) first — only one Referral/Storage source can be used at a time.', 'error');
      return;
    }
    const btn = $('#attachPreviousRfpBtn');
    const original = btn.textContent;
    try {
      const picked = await invoke('pick_document');
      state.previousRfpDoc = { path: picked.path, name: picked.name };
      btn.textContent = `Previous RFP: ${picked.name}`;
      renderAttachmentsStatus();
      refreshExtractedFieldsPreview();

      btn.textContent = 'Loading tables…';
      try {
        state.previousRfpPreview = JSON.parse(await invoke('preview_previous_rfp', { path: picked.path }));
        // Nothing is pre-included -- every column starts excluded, regardless of has_data,
        // so only what you actually click to include ends up in the output. Per direct
        // instruction: a has-data-based default let columns into the RFP that were never
        // explicitly chosen, which is exactly the opposite of what this selection UI is for.
        // has_data still drives the "(no data)" hint text in the grid, just not the default
        // selection state. Only computed on a successful preview -- see the catch below for
        // why a failed preview must NOT leave this as an empty (but non-null) object.
        state.previousRfpColumnSelection = {};
        for (const tableRole of Object.keys(state.previousRfpPreview)) {
          state.previousRfpColumnSelection[tableRole] = [];
        }
      } catch (err) {
        state.previousRfpPreview = null;
        // Leave this null (not {}) -- an empty object is still truthy, so it would get
        // JSON.stringify'd and sent as an explicit "keep zero columns" selection at
        // generate time, deleting every real column from every specimen table. null is
        // omitted entirely instead, so the backend auto-selects has-data columns from
        // its own independent re-parse of the file (see populate_rfp.main()'s own
        // previous_rfp_column_selection docstring).
        state.previousRfpColumnSelection = null;
        showToast('Could not preview Previous RFP tables: ' + err, 'error');
      }
      btn.textContent = `Previous RFP: ${picked.name}`;
      renderPreviousRfpPreview();
    } catch (err) {
      btn.textContent = original;
      if (!isCancelled(err)) showToast('Could not read Previous RFP document: ' + err, 'error');
    }
  }

  function clearPreviousRfp() {
    state.previousRfpDoc = null;
    state.previousRfpPreview = null;
    state.previousRfpColumnSelection = null;
    $('#attachPreviousRfpBtn').textContent = '+ Previous RFP';
    renderPreviousRfpPreview();
    renderAttachmentsStatus();
    refreshExtractedFieldsPreview();
  }

  const PREVIOUS_RFP_TABLE_TITLES = {
    referral: 'Referral Lab',
    storage_wide: 'Storage Samples',
    storage_narrow: 'Storage Samples (RNA/Tissue)',
  };

  // ---- Previous RFP column selection: drag-select grid (mirrors master-table.js's own
  // column drag-select -- see startColumnDrag/extendDragTo/renderSelectionBar there --
  // scoped per table role since three independent grids render at once here instead of one
  // active table). Column-only (these rows are the template's own fixed labels, not
  // freely-structured like a parsed SoA table) and reversible (unlike Master Table's one-way
  // delete: some real columns legitimately have no data, and the user may want to bring one
  // back without re-attaching the file) -- see this app's own design discussion for why.
  let previousRfpDrag = { tableRole: null, indices: new Set() };
  let previousRfpDragging = false;
  let previousRfpDragAnchor = null;

  function clearPreviousRfpDrag() {
    previousRfpDrag = { tableRole: null, indices: new Set() };
  }

  function startPreviousRfpColumnDrag(tableRole, colIndex) {
    previousRfpDragging = true;
    previousRfpDragAnchor = colIndex;
    previousRfpDrag = { tableRole, indices: new Set([colIndex]) };
    renderPreviousRfpPreview();
  }

  function extendPreviousRfpColumnDragTo(tableRole, colIndex) {
    if (!previousRfpDragging || previousRfpDrag.tableRole !== tableRole || previousRfpDragAnchor == null) return;
    const lo = Math.min(previousRfpDragAnchor, colIndex);
    const hi = Math.max(previousRfpDragAnchor, colIndex);
    const indices = new Set();
    for (let i = lo; i <= hi; i++) indices.add(i);
    previousRfpDrag = { tableRole, indices };
    renderPreviousRfpPreview();
  }

  // Same reach limitation master-table.js's own column drag-select has for a wide table
  // (see its wide-table-banner/fullscreen affordance): a column has to actually be on screen
  // for elementFromPoint() below to find it -- scroll a wide grid manually first (or use the
  // single-click toggle on any header, which works regardless of scroll position) to reach a
  // column further along. An auto-scroll-while-dragging enhancement was tried and dropped: it
  // fought with this view's own re-render-on-every-state-change (confirmed directly -- rapid
  // drag ticks caused the scroll position to visibly oscillate instead of advancing), which
  // would be a worse experience than this well-understood, already-precedented limitation.
  document.addEventListener('mousemove', (e) => {
    if (!previousRfpDragging) return;
    const el = document.elementFromPoint(e.clientX, e.clientY);
    const th = el && el.closest('th[data-pr-col-index]');
    if (th && th.dataset.prTableRole === previousRfpDrag.tableRole) {
      extendPreviousRfpColumnDragTo(previousRfpDrag.tableRole, Number(th.dataset.prColIndex));
    }
  });
  document.addEventListener('mouseup', () => {
    previousRfpDragging = false;
    previousRfpDragAnchor = null;
  });

  function togglePreviousRfpColumn(tableRole, key) {
    const current = new Set(state.previousRfpColumnSelection[tableRole] || []);
    if (current.has(key)) current.delete(key);
    else current.add(key);
    state.previousRfpColumnSelection[tableRole] = [...current];
    renderPreviousRfpPreview();
  }

  function setPreviousRfpColumnsIncluded(tableRole, keys, included) {
    const current = new Set(state.previousRfpColumnSelection[tableRole] || []);
    keys.forEach((k) => (included ? current.add(k) : current.delete(k)));
    state.previousRfpColumnSelection[tableRole] = [...current];
    clearPreviousRfpDrag();
    renderPreviousRfpPreview();
  }

  function renderPreviousRfpSelectionBar(container, tableRole, columns) {
    if (previousRfpDrag.tableRole !== tableRole || previousRfpDrag.indices.size === 0) return;
    const keys = columns
      .map((c, i) => (previousRfpDrag.indices.has(i) ? c.key : null))
      .filter((k) => k !== null);

    const bar = document.createElement('div');
    bar.className = 'master-table-selection-bar';

    const label = document.createElement('span');
    label.textContent = `${keys.length} column${keys.length === 1 ? '' : 's'} selected`;

    const excludeBtn = document.createElement('button');
    excludeBtn.type = 'button';
    excludeBtn.className = 'btn btn-secondary';
    excludeBtn.textContent = 'Exclude';
    excludeBtn.addEventListener('click', () => setPreviousRfpColumnsIncluded(tableRole, keys, false));

    const includeBtn = document.createElement('button');
    includeBtn.type = 'button';
    includeBtn.className = 'btn btn-secondary';
    includeBtn.textContent = 'Include';
    includeBtn.addEventListener('click', () => setPreviousRfpColumnsIncluded(tableRole, keys, true));

    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'icon-btn';
    clearBtn.title = 'Clear selection';
    clearBtn.textContent = '×';
    clearBtn.addEventListener('click', () => {
      clearPreviousRfpDrag();
      renderPreviousRfpPreview();
    });

    bar.appendChild(label);
    bar.appendChild(excludeBtn);
    bar.appendChild(includeBtn);
    bar.appendChild(clearBtn);
    container.appendChild(bar);
  }

  // Renders each of the previous RFP's 3 specimen tables as a real data grid (actual cell
  // values, not just column names) with the same drag-select-columns interaction as the
  // Master Table view (master-table.js) -- column-only and reversible, see the design note
  // above. Every column NOT included at Generate RFP time is deleted from that table in the
  // output entirely, not just left blank -- see populate_rfp.py's own
  // previous_rfp_column_selection handling.
  function renderPreviousRfpPreview() {
    const container = $('#previousRfpPreview');
    // This rebuilds the whole container on every call (including a single-column toggle or a
    // drag-select tick) -- without capturing/restoring each grid's own scroll position, a user
    // who scrolled a wide table right to reach a column would find it snapped back to the left
    // on their very next click, and an in-progress column drag would lose track of where it
    // started scrolling from (confirmed directly: starting a drag itself triggers a re-render).
    const scrollByRole = {};
    container.querySelectorAll('.previous-rfp-grid-wrap[data-pr-table-role]').forEach((el) => {
      scrollByRole[el.dataset.prTableRole] = { left: el.scrollLeft, top: el.scrollTop };
    });

    container.innerHTML = '';
    if (!state.previousRfpPreview) {
      container.hidden = true;
      return;
    }
    container.hidden = false;

    for (const [tableRole, title] of Object.entries(PREVIOUS_RFP_TABLE_TITLES)) {
      const tableData = state.previousRfpPreview[tableRole];
      if (!tableData || !tableData.columns.length) continue;
      const columns = tableData.columns;
      const selected = new Set(state.previousRfpColumnSelection[tableRole] || []);
      const rowLabels = Object.keys(tableData.rows);

      const group = document.createElement('div');
      group.className = 'previous-rfp-table-group';
      const heading = document.createElement('div');
      heading.className = 'previous-rfp-table-title';
      heading.textContent = title;
      group.appendChild(heading);

      const wrap = document.createElement('div');
      wrap.className = 'previous-rfp-grid-wrap';
      wrap.dataset.prTableRole = tableRole;
      const table = document.createElement('table');
      table.className = 'master-table-grid previous-rfp-grid';

      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      headRow.appendChild(document.createElement('th')); // row-label column spacer
      columns.forEach((col, colIndex) => {
        const th = document.createElement('th');
        th.dataset.prTableRole = tableRole;
        th.dataset.prColIndex = String(colIndex);
        const isExcluded = !selected.has(col.key);
        th.classList.toggle('col-excluded', isExcluded);
        th.classList.toggle('col-selected', previousRfpDrag.tableRole === tableRole && previousRfpDrag.indices.has(colIndex));

        const grip = document.createElement('span');
        grip.className = 'col-drag-handle';
        grip.title = 'Drag to select multiple columns';
        grip.textContent = '⋮⋮';
        grip.addEventListener('mousedown', (e) => {
          e.preventDefault();
          e.stopPropagation();
          startPreviousRfpColumnDrag(tableRole, colIndex);
        });

        const label = document.createElement('span');
        label.textContent = col.display_label + (col.has_data ? '' : ' (no data)');
        label.title = isExcluded ? 'Excluded -- click to include' : 'Click to exclude';
        label.style.cursor = 'pointer';
        label.addEventListener('click', () => togglePreviousRfpColumn(tableRole, col.key));

        th.appendChild(grip);
        th.appendChild(label);
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      table.appendChild(thead);

      const tbody = document.createElement('tbody');
      rowLabels.forEach((rowLabel) => {
        const tr = document.createElement('tr');
        const labelTd = document.createElement('td');
        labelTd.textContent = rowLabel;
        tr.appendChild(labelTd);
        columns.forEach((col, colIndex) => {
          const td = document.createElement('td');
          td.classList.toggle('col-excluded', !selected.has(col.key));
          const val = tableData.rows[rowLabel] ? tableData.rows[rowLabel][col.key] : undefined;
          td.textContent = val == null || val === '' ? '—' : val;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      group.appendChild(wrap);

      renderPreviousRfpSelectionBar(group, tableRole, columns);

      container.appendChild(group);
      // Must happen AFTER container.appendChild(group) -- scrollLeft/scrollTop assignments on
      // a subtree that isn't connected to the document yet get silently clamped to 0 (no
      // layout has run, so the browser doesn't know the real scrollable range), which is
      // exactly why this looked like scroll position was never actually being restored.
      if (scrollByRole[tableRole]) {
        wrap.scrollLeft = scrollByRole[tableRole].left;
        wrap.scrollTop = scrollByRole[tableRole].top;
      }
    }
  }

  // Lazily fetches + caches the real column registry (specimen_columns.py, via
  // GET /api/specimen-columns) -- the single source of truth for the dropdown below, replacing
  // a hardcoded 6-name list that had silently drifted from the template's actual 12 real
  // columns (Limited use bmkr x3, LTS Urine, LTS CSF, LTS Tissue were all unreachable before
  // this). Fetched once per session and reused; a failure here just leaves the dropdown
  // (and any base_label auto-match below) empty rather than blocking file attachment.
  async function ensureSpecimenColumns() {
    if (state.specimenColumns) return state.specimenColumns;
    try {
      state.specimenColumns = await apiGet('specimen-columns');
    } catch (err) {
      state.specimenColumns = [];
      showToast('Could not load Specimen Management column list: ' + err, 'error');
    }
    return state.specimenColumns;
  }

  // Alternative to attachPreviousRfp() for Referral Lab/Storage Samples: lets the user attach
  // several CLIPS forms/Non-PK Data Mgmt Worksheets at once (a real trial has one CLIPS form per
  // analyte/matrix and one Non-PKPD worksheet per assay) instead of reverse-parsing a previous
  // RFP's own tables. Data extraction/field-mapping (see clips_nonpkpd_parser.py, called via
  // /api/clips-nonpkpd-preview) is always automatic; the column each file lands in is
  // auto-detected (CLIPS -> LTS PK; Non-PKPD's own Assay Type/Specimen Type fields ->
  // Immunogenicity/DNA/Serum/Plasma/RNA -- still only those 6, since auto-detect can't guess
  // Urine/CSF/Tissue/bmkr from a form's own contents) and pre-filled as a dropdown default per
  // file -- editable, not just informational, since auto-detection can legitimately fail on a
  // real file's own formatting quirks (see renderClipsNonPkpdList). Always re-run detection
  // against the full accumulated file list (not just the newly-picked ones) so it stays
  // consistent as more files are added, but a column the user already picked for an existing
  // file is preserved rather than clobbered by a fresh auto-detect. `path` on each entry is a
  // session file id (see /api/upload-multi), not a filesystem path -- there's no local file path
  // to reference in a browser, only the uploaded bytes' server-side id. Two or more files may
  // freely be assigned the *same* column now (e.g. both "LTS Serum") -- populate_rfp.py gives
  // each its own real "LTS Serum (1)"/"(2)" Word table column rather than one overwriting the
  // other, so there's no client-side de-dup/blocking here.
  async function attachClipsNonPkpdFiles() {
    if (state.previousRfpDoc) {
      showToast('Remove the attached Previous RFP first — only one Referral/Storage source can be used at a time.', 'error');
      return;
    }
    const btn = $('#attachClipsNonPkpdBtn');
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Picking…';
    try {
      const [files, columns] = await Promise.all([
        pickMultipleFilesViaInput($('#clipsNonPkpdFileInput')),
        ensureSpecimenColumns(),
      ]);
      btn.textContent = 'Uploading…';
      const uploaded = await uploadMultiFilesToServer(files);
      btn.textContent = 'Extracting…';
      // `state.clipsNonPkpdFiles[i].path` must stay the bare session file id (what
      // /api/upload-multi returned, and what generate-rfp's own _find_session_file()
      // re-resolves later via a `{file_id}.*` glob) -- NOT result.files[i].path below, which
      // is the server's already-resolved absolute on-disk path for that same file. Confirmed
      // directly: passing that absolute path back into _find_session_file() at generate time
      // builds a glob pattern that can never match anything, since the two are entirely
      // different strings for the same file. Likewise result.files[i].name is derived from
      // that resolved path (Path(path).name), not the name the user actually picked -- a
      // meaningless hex string in its place reads exactly like "the file wasn't accepted"
      // even when extraction succeeded. `entries` keeps both the correct id and the real
      // name (plus any prior manual column choice) alongside each request, in the same
      // order as `paths`, so result.files[i] can be zipped back to entries[i] by position
      // rather than by trying to match on either of those server-derived fields.
      const existingEntries = state.clipsNonPkpdFiles.map((f) => ({ fileId: f.path, name: f.name, column: f.column }));
      const existingIds = new Set(existingEntries.map((e) => e.fileId));
      const newEntries = uploaded
        .filter((f) => !existingIds.has(f.file_id))
        .map((f) => ({ fileId: f.file_id, name: f.name, column: null }));
      if (!newEntries.length) return;
      const entries = [...existingEntries, ...newEntries];
      const raw = await invoke('preview_clips_nonpkpd_files', { paths: entries.map((e) => e.fileId) });
      const result = JSON.parse(raw);
      if (result.status === 'error') {
        showToast('Could not extract CLIPS/Non-PKPD file(s): ' + result.message, 'error');
        return;
      }
      // clips_nonpkpd_parser.py's auto-detect still returns a bare base label (e.g. "LTS Serum"),
      // not a registry key -- map it to the one matching entry's key (unambiguous for every
      // auto-detectable label; only the three identical "Limited use bmkr" columns share a
      // base_label, and auto-detect never returns that one).
      const keyForBaseLabel = (label) => (columns.find((c) => c.base_label === label) || {}).key || '';
      // `column` is ONLY ever set by the dropdown's own change handler now (or preserved from
      // a prior attach) -- never auto-populated from auto-detection. Per direct instruction:
      // an auto-detected-but-never-looked-at guess should not silently make it into the RFP.
      // The guess itself is kept as `suggestedColumn` purely for the dropdown's own hint text
      // (see renderClipsNonPkpdList), so the user still sees it -- they just have to actually
      // pick it (or something else) for it to count as chosen.
      state.clipsNonPkpdFiles = result.files.map((f, i) => ({
        path: entries[i].fileId,
        name: entries[i].name,
        docType: f.doc_type,
        column: entries[i].column || null,
        suggestedColumn: keyForBaseLabel(f.column) || null,
        fields: f.fields,
        error: f.error || null,
      }));
      renderClipsNonPkpdList();
      renderAttachmentsStatus();
      // Built from the final state (real names) rather than result.unmapped (garbled
      // on-disk names -- see above). Includes files with an auto-detected suggestion that
      // hasn't actually been picked yet, not just ones auto-detect couldn't guess at all --
      // per direct instruction, an unconfirmed guess must not silently make it into the RFP.
      const unmappedNames = state.clipsNonPkpdFiles.filter((f) => !f.column).map((f) => f.name);
      if (unmappedNames.length) {
        showToast(
          `Pick (or confirm) a Referral/Storage column for: ${unmappedNames.join(', ')} ` +
            `— data was still extracted from these file(s), but a column choice must be actively ` +
            `made from the dropdown for each one so it's included when the RFP is generated.`,
          'error'
        );
      }
      const failedFiles = state.clipsNonPkpdFiles.filter((f) => f.error);
      if (failedFiles.length) {
        showToast(
          `Could not extract data from: ${failedFiles.map((f) => `${f.name} (${f.error})`).join('; ')}`,
          'error'
        );
      }
    } catch (err) {
      if (!isCancelled(err)) showToast('Could not attach CLIPS/Non-PKPD file(s): ' + err, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  // Each row shows the file name and a column dropdown -- pre-filled with the auto-detected
  // column (or blank if auto-detection couldn't tell), always user-editable so a wrong or missing
  // guess never means that file's already-extracted data just gets silently dropped. Options are
  // grouped into two <optgroup>s (Referral Lab / Specimen Management) using the fetched registry's
  // own display_label (already carries the "(referral)"/"(LTS)" suffix and any "(1)"/"(2)"/"(3)"
  // disambiguation for the three identical "Limited use bmkr" columns).
  function renderClipsNonPkpdList() {
    const container = $('#clipsNonPkpdList');
    container.innerHTML = '';
    container.hidden = state.clipsNonPkpdFiles.length === 0;
    const columns = state.specimenColumns || [];
    const referralCols = columns.filter((c) => c.tag === 'referral');
    const ltsCols = columns.filter((c) => c.tag !== 'referral');
    state.clipsNonPkpdFiles.forEach((file, i) => {
      const row = document.createElement('div');
      row.className = 'clips-nonpkpd-row' + (file.column ? '' : ' unmapped');

      const name = document.createElement('span');
      name.className = 'clips-nonpkpd-row-name';
      name.title = file.error ? `Extraction failed: ${file.error}` : file.name;
      name.textContent = file.error ? `⚠ ${file.name}` : file.name;
      row.appendChild(name);

      const select = document.createElement('select');
      select.className = 'text-input';
      const blankOpt = document.createElement('option');
      blankOpt.value = '';
      // The auto-detected guess is shown as a hint here, not applied automatically -- the
      // dropdown always starts on this blank option regardless of suggestedColumn, so picking
      // (even re-picking the suggested one) is a real, explicit action that fires 'change'.
      const suggestedLabel = file.suggestedColumn
        ? (columns.find((c) => c.key === file.suggestedColumn) || {}).display_label
        : null;
      blankOpt.textContent = file.column
        ? '(unassign)'
        : suggestedLabel
          ? `Choose column… (suggested: ${suggestedLabel})`
          : 'Choose column…';
      select.appendChild(blankOpt);
      [
        ['Referral Lab', referralCols],
        ['Specimen Management (LTS)', ltsCols],
      ].forEach(([groupLabel, cols]) => {
        if (!cols.length) return;
        const group = document.createElement('optgroup');
        group.label = groupLabel;
        cols.forEach((col) => {
          const opt = document.createElement('option');
          opt.value = col.key;
          opt.textContent = col.display_label;
          if (col.key === file.column) opt.selected = true;
          group.appendChild(opt);
        });
        select.appendChild(group);
      });
      select.addEventListener('change', () => {
        state.clipsNonPkpdFiles[i].column = select.value;
        renderClipsNonPkpdList();
        renderAttachmentsStatus();
      });
      row.appendChild(select);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'icon-btn';
      removeBtn.title = 'Remove file';
      removeBtn.textContent = '×';
      removeBtn.addEventListener('click', () => {
        state.clipsNonPkpdFiles.splice(i, 1);
        renderClipsNonPkpdList();
        renderAttachmentsStatus();
      });
      row.appendChild(removeBtn);

      container.appendChild(row);
    });
  }

  // No inline result panel -- a toast is enough to confirm success/failure (the user found the
  // old inline fill-report panel unnecessary). The full field-by-field report is still written
  // to disk as its own .md file next to the generated docx (populate_rfp.py's report_path,
  // read back by rfp.rs only to build the now-removed panel) for anyone who wants the detail.
  function showRfpGenerateResult(result) {
    if (result.status !== 'complete') {
      showToast(result.message || 'RFP generation failed', 'error');
      return;
    }
    const c = result.coverage || {};
    showToast(
      `Generated ${result.output_path} -- ${c.filled ?? '?'} filled, ${c.computed ?? '?'} computed, ` +
        `${c.review ?? '?'} need review (of ${c.total ?? '?'})`,
      'success'
    );
  }

  // Reads the Study Details panel straight from the DOM (no separate JS state to keep in sync —
  // the inputs themselves are the source of truth, same pattern #renderHtmlToggle already uses)
  // into the exact `answers` shape populate_rfp.py has always accepted but this app never
  // exposed a UI for (oncology_override/decentralized/penalties_incentives/anatomic_pathology/
  // hepatic_calc) -- gates whether whole optional template sections are kept or deleted.
  function readStudyAnswers() {
    return {
      oncology_override: $('#studyOncologySelect').value,
      immuno_oncology_override: $('#studyImmunoOncologySelect').value,
      decentralized: $('#studyDecentralizedCb').checked,
      penalties_incentives: $('#studyPenaltiesCb').checked,
      anatomic_pathology: $('#studyAnatomicPathologyCb').checked,
      hepatic_calc: $('#studyHepaticSelect').value,
      // Countries section -- feeds extract_country_check_row's existing manual-override path
      // (populate_rfp.py already reads this key; there was just never a UI for it before).
      country_allocation: state.manualCountries.join(', '),
      // Information section's Budget & Timeline toggle -- a real, previously-unfilled template
      // row (see plan).
      rfp_for_other_studies: $('#otherStudiesToggle').checked,
      // Specimens section -- no matching template row exists yet, so these are report-only
      // (rec()-tracked) on the engine side for now; captured here regardless so they're not lost
      // once a real destination is identified.
      storage_conditions: state.storageConditions,
      kit_type: state.kitType,
      shipping_frequency: $('#shippingFrequencyInput').value.trim(),
      data_transfer_format: $('#dataTransferFormatInput').value.trim(),
    };
  }

  // Manual overrides from the Information section -- merged into fieldOverrides (which otherwise
  // is exactly extract_rfp_schema's own auto-extraction) so a manually-entered value always wins,
  // via the same `_ov()`-wins-when-non-empty precedence populate_rfp.py already uses everywhere.
  function computeManualFieldOverrides() {
    const overrides = {};
    if (state.sponsorContacts.length) {
      const primary = state.sponsorContacts[0];
      const label = [primary.name, primary.email].filter(Boolean).join(', ');
      if (label) overrides['Requestor contact'] = label;
    }
    const dateSubmitted = $('#dateSubmittedInput').value.trim();
    if (dateSubmitted) overrides['Date RFP submitted'] = dateSubmitted;
    const dateBudget = $('#dateBudgetInput').value.trim();
    if (dateBudget) overrides['Date budget required'] = dateBudget;
    const protocolNumber = $('#protocolNumberInput').value.trim();
    if (protocolNumber) overrides['Protocol alias'] = protocolNumber;
    if (state.phase) overrides['Phase'] = state.phase;
    const ta = $('#therapeuticAreaInput').value.trim();
    if (ta) overrides['Therapeutic Area'] = ta;
    return overrides;
  }

  // Always visible (previously hidden unless Oncology study was explicitly set to "yes" -- but
  // if a user left that on Auto-detect even for a genuinely oncology protocol, the Immuno
  // Oncology field stayed hidden and readStudyAnswers() silently discarded whatever answer was
  // in it, since the value was only read when the wrapper was visible). Kept as a real, standing
  // field like the other yes/no toggles in this panel so an answer here is never dropped.

  async function generateRfpFromMasterTables() {
    // An unmapped CLIPS/Non-PKPD file (auto-detect couldn't tell which Referral/Storage
    // column it belongs to, e.g. Urine/CSF/Tissue/"Limited use bmkr" specimen types can
    // never be auto-detected -- see clips_nonpkpd_parser.py's own module docstring) is
    // silently excluded from clipsNonPkpdAssigned below rather than guessed. The only
    // earlier feedback about this is a one-time toast right after attaching (see
    // attachClipsNonPkpdFiles), easy to miss if Generate happens later -- block here
    // too, instead of quietly generating an RFP with that file's data left out
    // entirely, which looks identical to "the Referral/Storage tables didn't populate."
    const unmappedClipsFiles = state.clipsNonPkpdFiles.filter((f) => !f.column);
    if (unmappedClipsFiles.length) {
      showToast(
        `Pick a Referral/Storage column for: ${unmappedClipsFiles.map((f) => f.name).join(', ')} ` +
          `— or remove the file(s) — before generating, otherwise their data won't be included.`,
        'error'
      );
      return;
    }

    const soaMerged = mergeNamedTables(window.MasterTableView.getTablesByName('SoA'));
    const labMerged = mergeNamedTables(window.MasterTableView.getTablesByName('Lab Appendix'));
    const soaTableOverride = soaMerged ? { headers: soaMerged.headers, rows: soaMerged.rows, footnotes: '' } : null;
    const labTableOverride = labMerged ? labMerged.rows.map((r) => [r[0] || '', r[1] || '']) : null;

    const btn = $('#generateRfpBtn');
    btn.disabled = true;
    btn.textContent = 'Generating…';
    try {
      let outputPath;
      try {
        outputPath = await invoke('pick_save_path', { suggestedName: 'RFP.docx' });
      } catch (err) {
        if (!isCancelled(err)) showToast('Could not choose a save location: ' + err, 'error');
        return;
      }

      const protocolText = state.pageResults.map((r) => r.markdown).join('\n\n');
      const designText = ''; // Design Elements attachment removed -- Fabric extract lookup covers these fields instead.
      const previousRfpPath = state.previousRfpDoc ? state.previousRfpDoc.path : null;

      // Reuses the extraction the Information/Countries live previews already fetched (see
      // refreshExtractedFieldsPreview, triggered right after parsing) instead of re-running
      // extract_rfp_schema here every time -- only falls back to a fresh call if that background
      // refresh hasn't completed yet (e.g. Generate RFP clicked immediately after parsing).
      let extractedFields = state.extractedFields;
      if (!extractedFields) {
        const schemaRaw = await invoke('extract_rfp_schema', { protocolText, designText, previousRfpPath });
        extractedFields = flattenSchemaFields(JSON.parse(schemaRaw));
        if (state.fabricDesignFields) Object.assign(extractedFields, state.fabricDesignFields);
        state.extractedFields = extractedFields;
      }

      // Information section's manual overrides win over the auto-extraction wherever they're
      // non-empty (same precedence populate_rfp.py's own _ov() already applies). Building this as
      // a genuinely flat object -- rather than spreading extract_rfp_schema's structured
      // protocol_fields/design_fields/rfp_engine_fields shape, as this used to do -- is what
      // actually lets these overrides reach populate_rfp.py; see backend/engine/rfp_cli_bridge.py's
      // flattening logic, which otherwise ignores sibling flat keys on a structured-shaped payload.
      const manualOverrides = computeManualFieldOverrides();
      const fieldOverrides = JSON.stringify({ ...extractedFields, ...manualOverrides });

      // Only files the user has confirmed a column for -- an unassigned/needs-review file is
      // left out rather than guessed at generation time (its own row already flags this in the
      // attachments status line and the per-file review list).
      const clipsNonPkpdAssigned = state.clipsNonPkpdFiles.filter((f) => f.column);
      const clipsNonpkpdAssignments = clipsNonPkpdAssigned.length
        ? JSON.stringify(clipsNonPkpdAssigned.map((f) => ({ path: f.path, column: f.column })))
        : null;

      // Only sent when a Previous RFP is actually attached AND its preview succeeded
      // (state.previousRfpColumnSelection is null otherwise -- see attachPreviousRfp).
      // populate_rfp.py auto-selects has-data columns itself for any table role that's
      // absent here (including when this whole field is omitted), so a failed preview
      // no longer means "delete every column" -- only an explicit, present selection
      // (even an empty one) is honored as a deliberate choice.
      const previousRfpColumnSelection = previousRfpPath && state.previousRfpColumnSelection
        ? JSON.stringify(state.previousRfpColumnSelection)
        : null;

      const raw = await invoke('populate_rfp_docx', {
        protocolText,
        outputPath,
        designText,
        soaTableOverride: soaTableOverride ? JSON.stringify(soaTableOverride) : null,
        labTableOverride: labTableOverride ? JSON.stringify(labTableOverride) : null,
        protocolPdfPath: state.pickedFile ? state.pickedFile.path : null,
        previousRfpPath,
        previousRfpColumnSelection,
        fieldOverrides,
        answers: JSON.stringify(readStudyAnswers()),
        clipsNonpkpdAssignments,
      });
      showRfpGenerateResult(JSON.parse(raw));
    } catch (err) {
      showToast('RFP generation failed: ' + err, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Generate RFP';
    }
  }

  // attachPreviousRfp/attachClipsNonPkpdFiles' buttons live in the source panel now (attach at
  // the start, alongside the protocol upload) -- wired here regardless, since IDs are unique
  // and this function already owns all of the master-table screen's RFP-related wiring. There's
  // no Design Elements control to wire (removed -- Fabric extract lookup covers those fields
  // instead) and no Fabric search control to wire either -- see
  // maybeAutoSearchFabricDesignFields, triggered automatically instead.
  function wireMasterTableRfp() {
    $('#attachPreviousRfpBtn').addEventListener('click', attachPreviousRfp);
    $('#clearPreviousRfpBtn').addEventListener('click', clearPreviousRfp);
    $('#attachClipsNonPkpdBtn').addEventListener('click', attachClipsNonPkpdFiles);
    $('#generateRfpBtn').addEventListener('click', generateRfpFromMasterTables);
  }

  document.addEventListener('DOMContentLoaded', () => {
    wireTabs('[data-source-tab]', 'data-source-panel', 'sourceTab');
    wireTabs('[data-panel-tab]', 'data-panel', 'panelTab');
    wireTabs('[data-result-tab]', 'data-result-panel', 'resultTab');
    wireConfigPanel();
    wireSourcePanel();
    wireSourcePanelCollapse();
    wireSectionsThumbnailsToggle();
    wireNavRailResize();
    wireThumbnailsTablesOnly();
    wireThumbnailModal();
    wireRegionStep();
    wireSelection();
    wireOutputToolbars();
    wirePageNav();
    wireResultTabExtras();
    wireCombineTables();
    wireCropScreen();
    wireMasterTableRfp();
    wireSidebar();
    wireHomeButton();
    wireInformationSection();
    wireCountriesSection();
    wireScheduleSection();
    wireSpecimenSection();
    showSection('intake');
    bootstrapModel();
  });
})();
