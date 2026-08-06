// Master SoA consolidation. Originally a JS port of a markdown-table reconciliation script;
// replaced with a thin mapper over the output of `worker/master_schedule_core.py`'s
// `--schedule` mode (the user's improved `protocol_extractor.py`, ported to Python — see that
// file's docstring). That script parses the *original* PDF's raw word positions directly
// (Schedule of Activities + Appendix 2 lab panels, with fuzzy analyte/panel-to-visit matching),
// which is far more robust than reconciling already-extracted Camelot table markdown, so the
// consolidation now happens entirely in Python; this file only maps its JSON result into the
// {columns, rows} shape master-table.js's editable grid expects.
(function () {
  // ============================================================================
  // NON-CLS ROW FILTERING — still used by master-table.js's `createFromPageTables` when the
  // user manually combines "SoA" thumbnail pages via Combine Tables (a separate, still-Camelot-
  // markdown-based feature, unrelated to Master SoA's own regeneration below).
  // ============================================================================

  const NON_CLS_PATTERNS = [
    /\bIWRS\b/i, /\bIVRS\b/i, /register visit/i, /randomi[sz]ation/i, /randomi[sz]e/i,
    /dispense/i, /returns?\s+unused/i, /study intervention compliance/i,
    /administer study intervention/i, /observe participant/i, /injection training/i,
    /injection diary/i, /kwikpen/i, /demo device/i, /ancillary supplies/i,
    /dosing (diary|report)/i, /visit detail/i, /visit number/i, /visit interval/i,
    /weeks from/i, /^weeks$/i, /^table \d+/i, /^\(electronic\)$/i,
    /informed consent/i, /inclusion.{0,4}exclusion/i, /confirmation of eligibility/i,
    /eligibility/i, /demographics/i, /\bmedical history\b/i, /preexisting condition/i,
    /pre-existing condition/i, /prior treatments? for indication/i, /surgical history/i,
    /family history/i, /education level/i, /substance use/i, /prespecified.{0,20}history/i,
    /vital signs/i, /physical exam/i, /physical assessment/i, /\bheight\b/i, /\bweight\b/i,
    /waist circumference/i, /hip circumference/i, /\bBMI\b/i, /\bABI\b/i, /\b6-?MWT\b/i,
    /12-?lead ECG/i, /\bECG\b/i, /electrocardiogram/i, /\bDXA\b/i, /fundus photograph/i,
    /MRI of spine/i, /treadmill/i, /clinician assessment of sensory/i,
    /symptom-directed/i, /lifestyle counsel/i, /diet and physical activity/i,
    /nutrition and physical activity/i, /treatment goal/i, /medication (washout|intensity)/i,
    /antihypertensive/i, /diary/i, /\bdBM\b/i, /digital biomarker device/i,
    /EQ-5D/i, /PHQ-9/i, /C-SSRS/i, /NRS\b/i, /\bPGI-?[CS]/i, /PGI-Stat/i, /PROMIS/i,
    /SF-?36/i, /IWQOL/i, /WPAI/i, /WPI-/i, /API-/i, /RMDQ/i, /\bDN4\b/i, /\bNPSI\b/i,
    /\bPCS\b/i, /\bCoEQ\b/i, /\bFNQ\b/i, /VascuQoL/i, /\bVAS\b/i, /body map/i,
    /pain interference/i, /pain catastrophizing/i, /global impression/i, /mental health/i,
    /patient-?reported/i, /rescue medication/i, /pain (concomitant )?med/i,
    /concomitant medication/i, /adverse event/i, /\bAEs?\b/i, /endpoint event/i,
    /participant education/i,
  ];

  const CLS_KEEP_PATTERNS = [
    /\bPK\b/i, /pharmacokinetic/i, /immunogenicity/i, /\bADA\b/i, /genetics? sample/i,
    /epigenetics/i, /biomarker/i, /pregnancy (test|—|-)?.{0,10}(serum|blood)/i,
    /serum pregnancy/i, /\bsample\b/i, /specimen/i, /\bassay\b/i, /\bblood\b/i, /\bserum\b/i,
    /urinalysis/i, /urine drug screen/i, /hematology|haematology/i, /clinical chemistry/i,
    /lipid|glucose|insulin|hba1c|egfr|tsh|fsh|calcitonin|amylase|lipase|creatinine/i,
  ];

  function isNonCLSRow(testName) {
    const name = String(testName || "").trim();
    if (!name) return false;
    if (CLS_KEEP_PATTERNS.some((re) => re.test(name))) return false;
    return NON_CLS_PATTERNS.some((re) => re.test(name));
  }

  // ============================================================================
  // Schedule JSON -> editable master-table grid
  // ============================================================================

  // scheduleData: the raw JSON from `camelot-worker --schedule-text-file` (see
  // worker/clinical_mapper.py), shaped as:
  //   { schedule_of_activities: { tests: [{name, is_panel, visits: [visitNum,...]}], visits: [] },
  //     lab_appendix: { panels: [{name, is_standalone, is_lilly_designated_lab, tests: [analyte,...]}] },
  //     master_schedule: [{visit_number, week, visit_type, tests: [...]}] }
  // clinical_mapper.py's own `visits` list is always empty (it never actually populates it — see
  // the script's own parse_schedule/build_master_schedule), so visit *columns* are derived from
  // master_schedule's own visit_number field instead, which is populated correctly.
  //
  // Two row sources, matching how the SoA and Lab Appendix sections relate to each other:
  // 1. Every schedule_of_activities.tests entry — this has real per-visit truth directly.
  // 2. Every lab_appendix panel's individual analyte, for panels/analytes not already covered by
  //    (1) — grouped under that panel's name as Category, inheriting the panel's own visit
  //    schedule if the panel's name itself matches a schedule test (same "panel visit schedule
  //    applies to its analytes unless individually listed" idea used by the previous engine).
  function scheduleToMasterTable(scheduleData) {
    const soa = scheduleData.schedule_of_activities || {};
    const scheduleTests = soa.tests || [];
    const panels = (scheduleData.lab_appendix || {}).panels || [];
    const masterSchedule = scheduleData.master_schedule || [];

    const visitNumbers = masterSchedule
      .map((entry) => entry.visit_number)
      .filter((v) => v !== null && v !== undefined)
      .sort((a, b) => a - b);

    const columns = [
      { name: "Category", type: "text" },
      { name: "Test", type: "text" },
      ...visitNumbers.map((v) => ({ name: `Visit ${v}`, type: "text" })),
      { name: "Note", type: "text" },
    ];

    const byName = new Map(); // normalized test name -> schedule test entry, for panel-name lookups
    scheduleTests.forEach((t) => byName.set(String(t.name || "").trim().toLowerCase(), t));

    function visitCellsFor(visits) {
      const set = new Set(visits || []);
      return visitNumbers.map((v) => (set.has(v) ? "X" : ""));
    }

    const rows = [];
    const covered = new Set(); // normalized names already added as a row

    scheduleTests.forEach((t) => {
      const key = String(t.name || "").trim().toLowerCase();
      covered.add(key);
      const panelMatch = panels.find((p) => String(p.name || "").trim().toLowerCase() === key);
      rows.push([panelMatch ? panelMatch.name : "Other", t.name || "", ...visitCellsFor(t.visits), ""]);
    });

    panels.forEach((panel) => {
      const panelKey = String(panel.name || "").trim().toLowerCase();
      const panelSchedule = byName.get(panelKey);
      const analytes = panel.tests && panel.tests.length ? panel.tests : panel.is_standalone ? [panel.name] : [];
      analytes.forEach((analyteName) => {
        const key = String(analyteName || "").trim().toLowerCase();
        if (!key || covered.has(key)) return;
        covered.add(key);
        rows.push([
          panel.name || "",
          analyteName,
          ...visitCellsFor(panelSchedule ? panelSchedule.visits : []),
          "",
        ]);
      });
    });

    return { columns, rows };
  }

  window.MasterSchedule = {
    isNonCLSRow,
    scheduleToMasterTable,
  };
})();
