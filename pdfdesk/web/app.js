'use strict';

const $ = id => document.getElementById(id);
const state = {
  documents: [], current: null, selection: 0, uploading: false,
  summaries: new Map(), answers: new Map(),
  pendingSummaries: new Set(), pendingAnswers: new Map(),
  summaryErrors: new Map(), answerErrors: new Map()
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function visible(id, show) { $(id).classList.toggle('hidden', !show); }
function notice(id, message) { $(id).textContent = message || ''; visible(id, Boolean(message)); }
function busyStatus(id, message) { $(id).textContent = message || ''; $(id).classList.toggle('loading', Boolean(message)); }
function fileURL(id, page) { return `/api/documents/${encodeURIComponent(id)}/file${page ? `#page=${Number(page)}` : ''}`; }
function sourceLink(id, page, label) {
  const a = element('a', 'page-link', label || `Page ${page} ↗`);
  a.href = fileURL(id, page); a.target = '_blank'; a.rel = 'noopener';
  a.setAttribute('aria-label', `Open original PDF at page ${page} in a new tab`);
  return a;
}
async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, options);
  let data;
  try { data = await response.json(); } catch { throw new Error(`The server returned an unreadable response (${response.status}).`); }
  if (!response.ok) throw new Error(data.error?.message || data.error || data.detail || `Request failed (${response.status}).`);
  return data;
}
function showTab(name, focus = false) {
  document.querySelectorAll('[data-tab]').forEach(tab => {
    const selected = tab.dataset.tab === name;
    tab.classList.toggle('active', selected); tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
    visible(`panel-${tab.dataset.tab}`, selected);
    if (selected && focus) tab.focus();
  });
}
function renderLibrary() {
  const list = $('document-list'); list.replaceChildren();
  $('document-count').textContent = state.documents.length;
  if (!state.documents.length) list.append(element('p', 'library-empty', 'A quiet shelf. Add a PDF to begin.'));
  for (const doc of state.documents) {
    const button = element('button', `document-item${state.current?.id === doc.id ? ' selected' : ''}`);
    button.type = 'button'; button.setAttribute('aria-current', state.current?.id === doc.id ? 'true' : 'false');
    const copy = element('span'); copy.append(element('span', 'document-name', doc.name), element('span', 'document-detail', `${doc.page_count} ${doc.page_count === 1 ? 'page' : 'pages'} · PDF document`));
    button.append(element('span', 'document-icon', 'PDF'), copy); button.addEventListener('click', () => selectDocument(doc.id)); list.append(button);
  }
}
function evidenceBlock(evidence, id) {
  const box = element('div', 'evidence');
  box.append(element('span', 'source-label', 'EXACT SOURCE EXCERPT'), element('blockquote', '', evidence.quote || ''));
  const foot = element('div', 'evidence-footer'); foot.append(sourceLink(id, evidence.page));
  if (evidence.passage_id) foot.append(element('span', '', `Passage ${evidence.passage_id}`));
  box.append(foot); return box;
}
function renderSummary(data) {
  const results = $('summary-results'); results.replaceChildren();
  visible('summary-empty', !data); $('generate-summary').textContent = data ? 'Refresh summaries ✦' : 'Summarize pages ✦';
  if (!data || !state.current) return;
  const id = state.current.id;
  if (data.warnings?.length) results.append(element('div', 'notice warning', data.warnings.join(' ')));
  for (const page of data.pages || []) {
    const card = element('article', 'summary-card');
    const heading = element('div', 'card-heading'); heading.append(element('span', 'page-label', `PAGE ${String(page.page).padStart(2, '0')}`), sourceLink(id, page.page, 'View original page ↗'));
    card.append(heading, element('span', 'interpretation-label', 'GENERATED INTERPRETATION'), element('p', 'summary-copy', page.summary || 'No summary was returned for this page.'));
    if (page.key_points?.length) {
      const points = element('ul', 'key-points');
      for (const point of page.key_points) {
        const item = element('li', 'key-point'); item.append(element('p', 'point-text', point.text));
        for (const evidence of point.evidence || []) item.append(evidenceBlock(evidence, id));
        points.append(item);
      }
      card.append(points);
    }
    results.append(card);
  }
  results.append(element('p', 'result-meta', `Generated with ${data.model || 'the configured model'}. Interpretations are model-generated; quoted excerpts come from the extracted PDF text.`));
}
function renderAnswer(data) {
  const results = $('answer-results'); results.replaceChildren();
  if (!data || !state.current) return;
  const card = element('article', `answer-card${data.supported ? '' : ' unsupported'}`);
  card.append(element('p', 'answer-question', data.question || ''), element('span', 'source-label', data.supported ? 'ANSWER · SOURCE EXTRACT' : 'INSUFFICIENT SUPPORT'), element('p', 'answer-copy', data.answer || 'No supported answer was returned.'));
  if (data.evidence?.length) for (const item of data.evidence) card.append(evidenceBlock(item, state.current.id));
  card.append(element('p', 'result-meta', data.supported ? `Selected with ${data.model || 'the configured model'}. Check the exact excerpts in their original context.` : 'The document did not provide sufficient evidence for this question. Try a more specific question or inspect the original.'));
  results.append(card);
}
function renderRequestState(id) {
  if (state.current?.id !== id) return;
  const summarizing = state.pendingSummaries.has(id);
  const answering = state.pendingAnswers.has(id);
  $('generate-summary').disabled = summarizing;
  $('ask-question').disabled = answering;
  notice('summary-error', state.summaryErrors.get(id));
  notice('answer-error', state.answerErrors.get(id));
  busyStatus('summary-status', summarizing ? 'Reading each page and checking its supporting passages. This may take a moment…' : '');
  busyStatus('answer-status', answering ? 'Finding relevant passages and checking the evidence…' : '');
}
function renderSource(doc) {
  const pages = $('source-pages'); pages.replaceChildren();
  for (const page of doc.pages || []) {
    const details = element('details', 'source-page');
    const summary = element('summary'); summary.append(element('span', '', `Page ${page.page}`), sourceLink(doc.id, page.page, 'Open original ↗'));
    const content = element('div', 'source-content'); content.append(element('pre', '', page.text?.trim() ? page.text : 'No text was extracted from this page. Inspect the original PDF for images or scanned content.'));
    details.append(summary, content); pages.append(details);
  }
}
async function selectDocument(id) {
  const selection = ++state.selection;
  notice('global-error', '');
  try {
    const doc = await api(`/documents/${encodeURIComponent(id)}`);
    if (selection !== state.selection) return;
    state.current = doc;
    $('document-title').textContent = doc.name;
    $('document-meta').textContent = `${doc.page_count} ${doc.page_count === 1 ? 'page' : 'pages'} · Original and extracted text available`;
    $('original-link').href = fileURL(doc.id);
    notice('document-warnings', (doc.warnings || []).join(' '));
    renderRequestState(id);
    $('question').value = state.pendingAnswers.get(id) ?? state.answers.get(id)?.question ?? '';
    renderSummary(state.summaries.get(id)); renderAnswer(state.answers.get(id)); renderSource(doc); renderLibrary();
    visible('welcome', false); visible('document-workspace', true); showTab('summary');
    document.title = `${doc.name} · PDF Desk`;
  } catch (error) { if (selection === state.selection) notice('global-error', error.message); }
}
async function refreshDocuments(preferred) {
  const result = await api('/documents');
  state.documents = Array.isArray(result) ? result : result.documents || [];
  renderLibrary();
  const id = preferred || state.current?.id || state.documents[0]?.id;
  if (id && state.documents.some(doc => doc.id === id)) await selectDocument(id);
  else { state.current = null; state.selection++; visible('welcome', true); visible('document-workspace', false); document.title = 'PDF Desk · Read with evidence'; renderLibrary(); }
}
async function upload(file) {
  if (!file || state.uploading) return;
  notice('global-error', '');
  if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') { notice('global-error', 'Choose a PDF file to add to your library.'); return; }
  if (file.size > 10 * 1024 * 1024) { notice('global-error', 'This PDF is larger than the 10 MB upload limit.'); return; }
  state.uploading = true; $('file-input').disabled = true; busyStatus('upload-status', 'Adding PDF…');
  try {
    const doc = await api('/documents', { method: 'POST', headers: { 'Content-Type': 'application/pdf', 'X-Filename': encodeURIComponent(file.name) }, body: file });
    await refreshDocuments(doc.id || doc.document?.id);
    $('upload-status').textContent = 'PDF added to your library.';
  } catch (error) { notice('global-error', error.message); $('upload-status').textContent = 'PDF could not be added.'; }
  finally { state.uploading = false; $('file-input').disabled = false; $('file-input').value = ''; $('upload-status').classList.remove('loading'); }
}
async function generateSummary() {
  const doc = state.current; if (!doc || state.pendingSummaries.has(doc.id)) return;
  state.pendingSummaries.add(doc.id); state.summaryErrors.delete(doc.id); renderRequestState(doc.id);
  try {
    const result = await api(`/documents/${encodeURIComponent(doc.id)}/summary`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    state.summaries.set(doc.id, result);
    if (state.current?.id === doc.id) renderSummary(result);
  } catch (error) { state.summaryErrors.set(doc.id, error.message); }
  finally { state.pendingSummaries.delete(doc.id); renderRequestState(doc.id); }
}
async function askQuestion(event) {
  event.preventDefault(); const doc = state.current; const question = $('question').value.trim();
  if (!doc || !question || state.pendingAnswers.has(doc.id)) return;
  state.pendingAnswers.set(doc.id, question); state.answerErrors.delete(doc.id); renderRequestState(doc.id);
  try {
    const result = await api(`/documents/${encodeURIComponent(doc.id)}/answer`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question }) });
    result.question = result.question || question; state.answers.set(doc.id, result);
    if (state.current?.id === doc.id) renderAnswer(result);
  } catch (error) { state.answerErrors.set(doc.id, error.message); }
  finally { state.pendingAnswers.delete(doc.id); renderRequestState(doc.id); }
}
async function deleteDocument() {
  const doc = state.current; if (!doc || !window.confirm(`Delete “${doc.name}” and its local original from PDF Desk?`)) return;
  $('delete-document').disabled = true;
  try { await api(`/documents/${encodeURIComponent(doc.id)}`, { method: 'DELETE' }); state.summaries.delete(doc.id); state.answers.delete(doc.id); state.summaryErrors.delete(doc.id); state.answerErrors.delete(doc.id); state.current = null; await refreshDocuments(); }
  catch (error) { notice('global-error', error.message); }
  finally { $('delete-document').disabled = false; }
}
async function checkHealth() {
  try {
    const health = await api('/health');
    const ready = health.model_ready ?? health.ready ?? health.model?.ready ?? health.model?.available ?? health.model_available ?? false;
    const name = typeof health.model === 'string' ? health.model : health.model?.model || health.model?.name;
    $('model-status').textContent = ready ? `● ${name || 'Local model'} ready` : '○ Model setup needed';
    $('model-status').classList.toggle('unavailable', !ready);
    $('model-status').title = health.model_error || health.error || health.message || 'Model status from the local server. Summaries and answers require a configured model.';
  } catch (error) { $('model-status').textContent = '○ Model unavailable'; $('model-status').classList.add('unavailable'); $('model-status').title = error.message; }
}

$('file-input').addEventListener('change', event => upload(event.target.files[0]));
$('welcome-upload').addEventListener('click', () => $('file-input').click());
$('generate-summary').addEventListener('click', generateSummary);
$('question-form').addEventListener('submit', askQuestion);
$('delete-document').addEventListener('click', deleteDocument);
document.querySelectorAll('[data-tab]').forEach(tab => {
  tab.addEventListener('click', () => showTab(tab.dataset.tab));
  tab.addEventListener('keydown', event => {
    const names = ['summary', 'questions', 'source']; const current = names.indexOf(tab.dataset.tab); let next;
    if (event.key === 'ArrowRight') next = (current + 1) % names.length;
    if (event.key === 'ArrowLeft') next = (current + names.length - 1) % names.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = names.length - 1;
    if (next !== undefined) { event.preventDefault(); showTab(names[next], true); }
  });
});
const dropzone = $('upload-zone');
dropzone.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('file-input').click(); } });
['dragenter', 'dragover'].forEach(name => dropzone.addEventListener(name, event => { event.preventDefault(); dropzone.classList.add('dragging'); }));
['dragleave', 'drop'].forEach(name => dropzone.addEventListener(name, event => { event.preventDefault(); dropzone.classList.remove('dragging'); }));
dropzone.addEventListener('drop', event => upload(event.dataTransfer.files[0]));
refreshDocuments().catch(error => notice('global-error', error.message));
checkHealth();
